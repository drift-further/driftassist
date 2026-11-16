"""routes/poll.py — Consolidated polling, health check, CLI proxy."""

import json as json_mod
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

import shared.auth as auth
import shared.drafts as drafts
from shared import execution_park as park
import shared.state as state
import shared.tab_state as tab_state
from shared.agent_identity import (
    ROOT_UNKNOWN,
    _ANSI_ESCAPE_RE,
    _VERSION_CMD_RE,
    child_pids,
    pane_root_start,
    refine_with_content,
    resolve_process_detail,
)
from shared.agent_model import observe as observe_model
from shared import pane_scan
# Re-exported: tests/test_poll_perf.py and tests/test_autoyes_parity.py patch and
# read the cache through this module. /poll passes its own binding to
# pane_scan.sample(), so a patched dict here is the one every write lands in.
from shared.pane_scan import _SCAN_CACHE, _SCAN_LOCK, _activity_tail
from shared.tmux import capture_reusable, prettify_command, sudo_prompt_waiting, tmux_exact_target
from routes.autoyes import (
    autoyes_will_consider,
    detection_depth,
    detection_window,
    detection_window_lines,
    prompt_popup_info,
)
from routes.delegates import snapshot as delegate_snapshot, resume_for_pane
from shared.delegate_parent import enrich_parents
from routes.terminal import enrich_panes_with_agents

poll_bp = Blueprint("poll_bp", __name__)

CLI_PROXY_TIMEOUT_CEILING = 600

# Each pane's tail goes to the browser cut to the detectors' shared input window
# (routes/autoyes.py:detection_window_lines), and the capture reaches back at
# least that far, so the browser detects on exactly the rows the scanner does.
# A pane whose change marker has not moved reuses its last capture, but never
# one older than this (shared/tmux.py:capture_reusable).
SCAN_RECAPTURE_SEC = 30
_DELEGATE_WIRE = ("state", "reason", "detail", "since", "episode", "health", "observed_at", "alert")
# Idle state is written when a pane's content changed, else at most this often.
IDLE_SAVE_INTERVAL_SEC = 30
# Git branch and status counts are reused for this long per project dir,
# unless .git/index or .git/HEAD moves first.
GIT_META_TTL_SEC = 10

_last_idle_save = 0.0

_GIT_META_CACHE = {}
_GIT_META_LOCK = threading.Lock()


def _capture_tail(target, pane_id=None, pane_pid=None):
    """capture-pane for pane_scan.sample(); None when tmux refuses.

    The listed pane is captured by its id (`%N`), so the bytes come from the
    pane the listing named wherever it sits now: a name reused, or swapped
    away and back, cannot hand over another pane's text. A `%N` is unique only
    within one tmux server's life, so the id is then read back and must still
    name the listed pane pid; a vanished id, another pid or no answer fails
    closed. Only a caller with no listed pane id falls back to the exact name."""
    cap = subprocess.run(
        ["tmux", "capture-pane", "-e", "-p", "-t", tmux_exact_target(pane_id or target),
         "-S", f"-{detection_window_lines()}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if cap.returncode != 0:
        return None
    if pane_id is None:
        return cap.stdout
    check = subprocess.run(
        ["tmux", "display-message", "-p", "-t", tmux_exact_target(pane_id), "#{pane_pid}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if not pane_pid or check.returncode != 0 or check.stdout.strip() != str(pane_pid):
        return None
    return cap.stdout


def _store_scan_capture(target, raw, prev):
    """Publish a capture taken by the caller; the rev rises only if the browser tail changed."""
    return pane_scan.store_capture(target, raw, prev, cache=_SCAN_CACHE)


def _find_project_dir(cwd):
    """Find the nearest project root for a tmux pane cwd."""
    check_dir = cwd
    for _ in range(6):
        if (check_dir / ".git").is_dir() or (check_dir / ".claude").is_dir() or (check_dir / ".opencode").is_dir():
            return check_dir
        parent = check_dir.parent
        if parent == check_dir:
            break
        check_dir = parent
    return cwd


def _run_git(project_dir, args, timeout=2):
    """Run a read-only git command without taking optional index locks.

    Claude/OpenCode status polling runs frequently from Assist. Plain `git status`
    may refresh the index and race an interactive `git commit`, producing the
    lock error shown in terminal panes. `GIT_OPTIONAL_LOCKS=0` asks git to avoid
    optional index refresh locks for status-ish reads.
    """
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(project_dir),
        env=env,
    )


def _read_json(path):
    try:
        if path.exists():
            return json_mod.loads(path.read_text())
    except Exception:
        pass
    return None


def _merge_opencode_meta(result, project_dir):
    """Merge optional OpenCode plugin cache into the Assist metadata payload."""
    candidates = [
        project_dir / ".claude" / "state" / "opencode-status.json",
        project_dir / ".opencode" / "status.json",
    ]
    for path in candidates:
        oc = _read_json(path)
        if not oc:
            continue
        result.setdefault("agent", "opencode")
        if oc.get("model"):
            result["model"] = oc.get("model")
        if oc.get("status"):
            result["session_status"] = oc.get("status")
        if oc.get("session_id"):
            result["session_id"] = oc.get("session_id")
        if oc.get("todos") is not None:
            result["open_tasks"] = oc.get("todos")
        if oc.get("updated"):
            result["agent_updated"] = oc.get("updated")
        return


def _git_meta(project_dir):
    """Branch and edited-file counts for a project dir, cached briefly.

    Every browser's /poll asks for the active pane's repo every 5 s. A repo
    with a large untracked tree made each ask a long `git status`, so the
    answer is reused for GIT_META_TTL_SEC, or until .git/index or .git/HEAD
    changes. `--untracked-files=normal` counts an untracked directory once,
    which is all the info bar shows, instead of walking into it.
    """
    stamp = []
    for name in ("index", "HEAD"):
        try:
            stamp.append((project_dir / ".git" / name).stat().st_mtime_ns)
        except OSError:
            stamp.append(None)
    stamp = tuple(stamp)
    key = str(project_dir)
    now = time.time()
    with _GIT_META_LOCK:
        cached = _GIT_META_CACHE.get(key)
    if cached and cached[0] == stamp and now - cached[1] < GIT_META_TTL_SEC:
        return dict(cached[2])

    meta = {}
    # Git branch
    try:
        git_proc = _run_git(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
        if git_proc.returncode == 0:
            meta["branch"] = git_proc.stdout.strip()
    except Exception:
        pass

    # Edited files. Use lock-free status polling so Assist does not race the
    # interactive agent for .git/index.lock.
    try:
        gs_proc = _run_git(project_dir, ["status", "--porcelain=v1", "--untracked-files=normal"])
        if gs_proc.returncode == 0:
            edited = staged = untracked = deleted = 0
            for line in gs_proc.stdout.strip().split("\n"):
                if not line:
                    continue
                x = line[0] if len(line) > 0 else " "
                y = line[1] if len(line) > 1 else " "
                if x == "?" and y == "?":
                    untracked += 1
                    edited += 1
                    continue
                if x != " ":
                    staged += 1
                if x == "D" or y == "D":
                    deleted += 1
                if x in "MADRCU" or y in "MADRCU":
                    edited += 1
            meta["edited_files"] = edited
            meta["staged_files"] = staged
            meta["untracked_files"] = untracked
            meta["deleted_files"] = deleted
    except Exception:
        pass

    with _GIT_META_LOCK:
        _GIT_META_CACHE[key] = (stamp, now, meta)
    return dict(meta)


def get_claude_meta(target):
    """Read Claude/OpenCode session metadata near the pane's cwd.

    Returns a dict with context usage, cost, task, branch, edit counts, etc.
    Claude Code can populate `.claude/state/context-usage.json` via a compatible
    statusline script. OpenCode can populate `.claude/state/opencode-status.json`
    via a lightweight plugin. Git metadata is gathered independently for both.
    """
    try:
        if not target:
            return {}

        # Get pane's current working directory
        proc = subprocess.run(
            ["tmux", "display-message", "-t", tmux_exact_target(target), "-p", "#{pane_current_path}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}

        cwd = Path(proc.stdout.strip())
        project_dir = _find_project_dir(cwd)
        state_dir = project_dir / ".claude" / "state"

        result = {}
        # The Studio resolver needs this path; computing it here means /poll
        # never spawns a second `tmux display-message` for the same pane.
        result["project_dir"] = str(project_dir)
        _merge_opencode_meta(result, project_dir)

        # Read context-usage.json
        try:
            ctx_file = state_dir / "context-usage.json"
            if ctx_file.exists():
                ctx = json_mod.loads(ctx_file.read_text())
                result["used_percentage"] = ctx.get("used_percentage")
                result["tokens"] = ctx.get("tokens")
                result["limit"] = ctx.get("limit")
                result["duration_ms"] = ctx.get("duration_ms")
                result["cost_usd"] = ctx.get("cost_usd")
                result["model"] = ctx.get("model")
        except Exception:
            pass

        # Read current_task.json
        try:
            task_file = state_dir / "current_task.json"
            if task_file.exists():
                task_data = json_mod.loads(task_file.read_text())
                result["task"] = task_data.get("task")
        except Exception:
            pass

        # Read block-info.json
        try:
            block_file = state_dir / "block-info.json"
            if block_file.exists():
                block = json_mod.loads(block_file.read_text())
                result["block_active"] = block.get("isActive", False)
                result["block_elapsed"] = block.get("elapsedSeconds")
                result["block_remaining"] = block.get("remainingSeconds")
        except Exception:
            pass

        result.update(_git_meta(project_dir))

        # Open tasks (sessions/tasks/*.md not containing "status: done/completed")
        try:
            tasks_dir = project_dir / "sessions" / "tasks"
            if tasks_dir.is_dir():
                open_count = 0
                for md_file in tasks_dir.glob("*.md"):
                    try:
                        head = (
                            md_file.read_bytes()[:500]
                            .decode("utf-8", errors="ignore")
                            .lower()
                        )
                        if (
                            "status: done" not in head
                            and "status: completed" not in head
                        ):
                            open_count += 1
                    except Exception:
                        pass
                result["open_tasks"] = open_count
        except Exception:
            pass

        return result
    except Exception:
        return {}


@poll_bp.route("/poll")
def consolidated_poll():
    """Combined health + sessions + states + scan in one request.

    Replaces 4 separate polling endpoints to reduce HTTP overhead.
    Called every 5s from the frontend.
    """
    ws_count = len(state.ws_clients)
    result = {
        "status": "ok",
        "tmux_target": state.tmux_target,
        "ws_clients": ws_count,
    }

    # --- Sessions ---
    proc = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_name}\t#{window_index}\t#{pane_index}\t"
            "#{pane_current_command}\t#{pane_width}\t#{pane_height}\t"
            "#{session_activity}\t#{pane_pid}\t#{pane_id}\t#{session_created}\t"
            "#{pane_tty}\t#{window_activity}\t#{history_size}\t#{cursor_x}\t"
            "#{cursor_y}\t#{alternate_on}\t#{@assist_parent}\t#{pane_dead}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    panes = []
    # Kept off the pane dicts: the browser has no use for a tty path.
    pane_ttys = {}
    # What says a pane changed since its last capture; same field order as
    # shared/tmux.py:capture_pane_if_changed.
    pane_markers = {}
    seen_sessions = set()
    now = time.time()
    if proc.returncode == 0:
        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                target = f"{parts[0]}:{parts[1]}.{parts[2]}"
                pane_pid = parts[7] if len(parts) >= 8 else ""
                pane_id = parts[8] if len(parts) >= 9 else ""
                # Epoch seconds the tmux session was opened — the key behind
                # "sort by opened date" in the tab strip.
                created = int(parts[9]) if len(parts) >= 10 and parts[9].isdigit() else 0
                pane_ttys[target] = parts[10] if len(parts) >= 11 else ""
                if len(parts) >= 16:
                    pane_markers[target] = (
                        parts[11], parts[12], parts[13], parts[14],
                        parts[4], parts[5], parts[15], pane_id,
                    )
                is_subpane = parts[0] in seen_sessions
                seen_sessions.add(parts[0])
                panes.append(
                    {
                        "target": target,
                        "session": parts[0],
                        "created": created,
                        "_assist_parent": parts[16] if len(parts) > 16 else "",
                        "pane_dead": parts[17] if len(parts) > 17 else "0",
                        "window": int(parts[1]),
                        "pane": int(parts[2]),
                        "command": parts[3],
                        "width": int(parts[4]),
                        "height": int(parts[5]),
                        "pane_pid": pane_pid,
                        "pane_id": pane_id,
                        "is_subpane": is_subpane,
                        "command_display": prettify_command(parts[3]),
                    }
                )

        enrich_panes_with_agents(panes)

    process_kinds = {}
    # Panes whose tree holds two same-kind agents (task 2304), read off the
    # same walk as the kind so the scan loop never walks a tree again.
    refused_targets = set()
    for pane in panes:
        process_kind, refused = resolve_process_detail(
            pane["target"], pane.get("pane_pid"), pane.get("command", "")
        )
        process_kinds[pane["target"]] = process_kind
        if refused:
            refused_targets.add(pane["target"])
        pane["agent_kind"] = process_kind or "unknown"

    result["sessions"] = panes
    result["active_target"] = state.tmux_target

    # --- Scan ---
    # Must run BEFORE states so content-based idle_seconds is available.
    global _last_idle_save
    scan_results = []
    sudo_prompts = []
    autoyes_by_session = {}
    live_targets = set()
    changed_at = {}
    resume_tails = {}
    idle_changed = False
    for pane in panes:
        target = pane["target"]
        live_targets.add(target)
        # One sample per pane through shared/pane_scan.py: a pane that has not
        # changed since its last capture reuses it (a capture-pane per pane per
        # browser per poll was most of /poll's cost); two browsers capturing at
        # once share one; and the capture's activity hash is published with it.
        process_kind = process_kinds.get(target)
        root = pane_root_start(pane.get("pane_pid"))
        sampled = pane_scan.sample(
            target, pane.get("pane_id"), pane_markers.get(target), now, SCAN_RECAPTURE_SEC,
            capture=lambda t, _id=pane.get("pane_id"), _pid=pane.get("pane_pid"): _capture_tail(t, _id, _pid),
            kind_of=lambda tail, _k=process_kind: refine_with_content(_k, tail),
            cache=_SCAN_CACHE,
            reusable=capture_reusable,
            root=root,
        )
        resume_tails[target] = sampled.tail if sampled else ""
        if sampled is None and root is ROOT_UNKNOWN:
            # No identity for this pane now: uncertain, so active rather than
            # idle on a time that may belong to another pane generation.
            changed_at[target] = now
        if sampled is not None:
            tail = sampled.raw
            changed_at[target] = sampled.changed_at
            idle_changed = idle_changed or sampled.changed
            agent_kind = refine_with_content(process_kinds.get(target), tail)
            pane["agent_kind"] = agent_kind
            # One regex pass over <=5 lines of a capture already taken. The
            # memo write happens here, on the /poll request path — which may
            # run concurrently, one handler per browser — so other request
            # handlers never resolve a model themselves, only read the memo.
            # Concurrent writers racing here are safe because observe_model()
            # gates publication on _MIN_CONFIRM_SECONDS of wall-clock agreement,
            # not merely two adjacent calls. pane_id keys the memo to this
            # generation of the pane (Step 1, S4) so a killed-and-recreated
            # pane at the same target does not inherit the old model.
            model, model_effort, model_changed_at = observe_model(
                target, agent_kind, pane.get("pane_id"), tail
            )
            pane["model"] = model
            pane["model_effort"] = model_effort
            pane["model_changed_at"] = model_changed_at
            if tail:
                # Every open browser pops these (js/prompt-popup.js), whichever
                # tab is in view.
                waiting = sudo_prompt_waiting(pane_ttys.get(target), tail)
                if waiting:
                    sudo_prompts.append(
                        {"target": target, "session": pane["session"], **waiting}
                    )
                # For the question popup. "Armed" follows the scanner's own scope
                # rule: a globally-armed session leaves plain shell panes manual.
                session = pane["session"]
                if session not in autoyes_by_session:
                    autoyes_by_session[session] = state.autoyes_enabled_for(session)
                enabled, source = autoyes_by_session[session]
                armed = autoyes_will_consider(
                    target, enabled, source, process_kinds.get(target), agent_kind,
                    refused=target in refused_targets,
                )
                prompt = prompt_popup_info(_ANSI_ESCAPE_RE.sub("", tail), agent_kind, armed)
                scan_results.append(
                    {
                        "target": target,
                        "session": pane["session"],
                        "command": pane["command"],
                        "agent_kind": agent_kind,
                        "prompt": prompt,
                    }
                )
    # Tails go out as the cache holds them, read in one pass with the rev they
    # are stamped against, and only those newer than what this browser holds.
    since = pane_scan.parse_since(request.args.get("since"))
    with _SCAN_LOCK:
        for entry in scan_results:
            cached = _SCAN_CACHE.get(entry["target"])
            if cached and (since is None or cached["rev"] > since):
                entry["tail"] = cached["tail"]
                entry["tail_rev"] = cached["rev"]
        result["gen"] = f"{pane_scan._SCAN_EPOCH}.{pane_scan._scan_rev}"
    result["scan"] = scan_results
    result["sudo_prompts"] = sudo_prompts
    # Files an agent offered with `assist send-file`, for the popup and tray.
    from routes.outbox import poll_block as outbox_poll_block
    result["outbox"] = outbox_poll_block()
    # js/actions.js windows the pane with this, so the action bar and Auto-Yes
    # agree on what is a prompt at any Settings depth.
    result["detection_depth"] = detection_depth()
    # The rows of each tail that detection reads, for js/poll-sync.js:detectionTail.
    result["detection_window"] = detection_window_lines()

    # Clean up stale targets no longer in tmux — only after a complete listing;
    # a failed list-panes must not read as "every pane vanished".
    if proc.returncode == 0:
        tab_state.sweep_agent_declarations(live_targets)
        with state._activity_lock:
            for stale in set(state.pane_model) - live_targets:
                state.pane_model.pop(stale, None)
    idle_changed |= pane_scan.forget_missing(live_targets, proc.returncode == 0, cache=_SCAN_CACHE)

    # Persist idle state when it changed, else now and then. An fsync'd write
    # on every poll from every browser was pure cost on a quiet host.
    if idle_changed or now - _last_idle_save >= IDLE_SAVE_INTERVAL_SEC:
        _last_idle_save = now
        state.save_idle_state()

    # --- States ---
    AGENT_COMMANDS = {
        "claude",
        "opencode",
        "node",
        "python",
        "python3",
        "npm",
        "pip",
        "pytest",
        "make",
        "cargo",
        "go",
        "docker",
        "git",
    }
    SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}
    states = {}
    for pane in panes:
        target = pane["target"]
        command = pane["command"]
        pane_pid = pane.get("pane_pid", "")

        last_active = changed_at.get(target)
        if last_active is None:
            # No sample this poll (capture failed): the last published time.
            with state._activity_lock:
                last_active = state.pane_last_activity.get(target, now)
        idle_seconds = now - last_active

        idle_thresh = state.get_setting("terminal", "idle_threshold_sec")
        if command in AGENT_COMMANDS or _VERSION_CMD_RE.fullmatch(command):
            st = "idle" if idle_seconds > 30 else "running"
        elif command in SHELL_COMMANDS:
            if pane_pid:
                try:
                    has_children = child_pids(pane_pid)
                    if has_children is None:
                        # No /proc (macOS): pgrep, one process-table scan per shell.
                        child_check = subprocess.run(
                            ["pgrep", "-P", pane_pid],
                            capture_output=True,
                            text=True,
                            timeout=3,
                        )
                        has_children = child_check.stdout.strip()
                    if has_children:
                        st = "running"
                    elif idle_seconds > idle_thresh:
                        st = "idle"
                    else:
                        st = "shell"
                except Exception:
                    st = "idle" if idle_seconds > idle_thresh else "shell"
            else:
                st = "idle" if idle_seconds > idle_thresh else "shell"
        else:
            st = "running"

        states[target] = {
            "state": st,
            "command": command,
            "idle_seconds": idle_seconds,
        }
    result["states"] = states

    # Backfill idle_seconds onto pane objects for frontend use
    for pane in panes:
        pane["idle_seconds"] = states.get(pane["target"], {}).get("idle_seconds", 0)

    # Delegate observability (spec 7185 §6.1): the watcher's classification on
    # each delegate pane, and its health block. /poll.states is not touched.
    watched = delegate_snapshot()
    result["watch"] = watched["watch"]
    by_pane = {entry["pane_id"]: entry for entry in watched["panes"]}
    for pane in panes:
        entry = by_pane.get(pane.get("pane_id"))
        if entry:
            pane["delegate"] = {key: entry[key] for key in _DELEGATE_WIRE}
            pane["resume"], pane["resume_reason"] = entry.get("resume"), entry.get("resume_reason")
        else:
            # Reuse the resolved kind: only supported agent panes need the
            # ownership observation; shells and unsupported agents stay cheap.
            kind = process_kinds.get(pane["target"])
            if kind in ("codex", "claude"):
                pane["resume"], pane["resume_reason"] = resume_for_pane(
                    pane, tail=resume_tails.get(pane["target"], ""))
            else:
                pane["resume"] = None
                pane["resume_reason"] = (
                    "unsupported" if kind in ("cursor", "opencode", "gemini") else "no agent")

    enrich_parents(panes, tuple(state.get_settings()["delegates"]["prefixes"]))

    # --- Tab order / pin / snooze ---
    # Runs after the states block so the wake sweep sees fresh activity times.
    # apply_order returns a new list, so result["sessions"] is reassigned.
    tab_state.sweep_wakes(live_targets, now)
    result["sessions"] = tab_state.apply_order(panes)
    result["tab_state"] = tab_state.get_tab_state()

    # --- Composer drafts ---
    # Markers + revision stamps only; the bodies come from GET /api/draft. A
    # draft can be thousands of characters and this payload ships every 5s to
    # every open browser.
    result["drafts"] = drafts.poll_block()

    # --- Claude session metadata ---
    if state.tmux_target:
        result["claude_meta"] = get_claude_meta(state.tmux_target)

    # --- Studio ---
    # Imported here, not at module scope: routes.studio imports this module for
    # _find_project_dir, so a top-level import would be circular. Reads the
    # refresher's snapshot only — /poll must never block on Studio.
    from routes.studio import studio_poll_block

    result["studio"] = studio_poll_block(state.tmux_target, result.get("claude_meta"))
    from shared.updates import manager as update_manager
    result['update'] = update_manager().snapshot()

    # --- Open-access window ---
    # Every client gets the onboard report until it expires; each browser
    # de-duplicates on its id. Reads must not consume it — a poll that dies in
    # flight would take the operator's only warning with it.
    result["access"] = auth.window_state()
    # Which device this browser is, for the name step and the Devices sheet.
    # Header-token requests (scripts, the CLI) are not devices: null.
    device = auth.request_device(request)
    result["device"] = (
        {"id": device["id"], "name": device["name"], "named": bool(device.get("named"))}
        if device else None
    )

    # Raw UTF-8, not jsonify's \uXXXX escapes: agent panes are full of box
    # drawing and bullets, which the escapes turned into six bytes each.
    return Response(
        json_mod.dumps(result, ensure_ascii=False, separators=(",", ":")),
        mimetype="application/json",
    )


@poll_bp.route("/health")
def health():
    return jsonify({"status": "ok"})


@poll_bp.route("/api/cli-proxy", methods=["POST"])
def cli_proxy():
    """Proxy CLI commands from Docker containers to a host-side CLI tool.

    Expects JSON: {"args": ["backup", "status"], "files": [{"name": "f.md", "data": "<b64>"}]}
    Returns JSON: {"stdout": "...", "stderr": "...", "returncode": 0}

    Configure via ASSIST_CLI_BIN (binary path), ASSIST_CLI_DIR (working dir),
    and ASSIST_CLI_ALLOWED (comma-separated allowed subcommands).

    Files sent from containers are saved to a temp dir on the host, and
    __PROXY_FILE_N__ placeholders in args are replaced with the real paths.
    """
    result = park.perform(park.Intent.CONFIGURED_CLI_PROXY, _cli_proxy_effect)
    if park.is_refusal(result):
        return jsonify(result.body()), result.http_status
    return result


def _cli_proxy_effect():
    """Materialise and execute one configured proxy request under the park lock."""
    data = request.get_json(force=True, silent=True) or {}
    args = data.get("args", [])
    if not args:
        return jsonify({"error": "missing args"}), 400

    allowed_env = os.environ.get("ASSIST_CLI_ALLOWED", "")
    allowed = {s.strip() for s in allowed_env.split(",") if s.strip()}
    # Fail closed: no allowlist means the proxy is disabled, not wide open.
    if not allowed:
        return (
            jsonify({"error": "CLI proxy disabled (ASSIST_CLI_ALLOWED not set)"}),
            403,
        )
    if args[0] not in allowed:
        return jsonify({"error": f"subcommand '{args[0]}' not allowed"}), 403

    cli_bin = os.environ.get("ASSIST_CLI_BIN")
    if not cli_bin:
        return (
            jsonify({"error": "ASSIST_CLI_BIN not configured", "returncode": -1}),
            500,
        )

    tmp_dir = None
    tmp_files = []
    try:
        proxy_files = data.get("files", [])
        if proxy_files:
            import base64
            import re
            import tempfile

            tmp_dir = tempfile.mkdtemp(prefix="assist-proxy-")
            for i, fobj in enumerate(proxy_files):
                # Client names are untrusted (absolute or ../ names escape
                # the temp dir via os.path.join) — generate names server-side
                # and keep only a plain extension so the CLI can sniff type.
                ext = os.path.splitext(os.path.basename(fobj.get("name", "")))[1]
                if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
                    ext = ""
                fpath = os.path.join(tmp_dir, f"file_{i}{ext}")
                with open(fpath, "wb") as f:
                    f.write(base64.b64decode(fobj.get("data", "")))
                tmp_files.append(fpath)

            resolved_args = []
            for arg in args:
                if arg.startswith("__PROXY_FILE_") and arg.endswith("__"):
                    try:
                        idx = int(arg[len("__PROXY_FILE_") : -2])
                        resolved_args.append(tmp_files[idx])
                    except (ValueError, IndexError):
                        resolved_args.append(arg)
                else:
                    resolved_args.append(arg)
            args = resolved_args

        cmd_timeout = 30
        if "--wait-reply" in args or "-w" in args:
            cmd_timeout = 330
            for i, a in enumerate(args):
                if a == "--timeout" and i + 1 < len(args):
                    try:
                        requested_timeout = int(args[i + 1])
                    except (TypeError, ValueError):
                        return (
                            jsonify(
                                {
                                    "error": (
                                        "invalid --timeout: expected a "
                                        "non-negative integer"
                                    )
                                }
                            ),
                            400,
                        )
                    if requested_timeout < 0:
                        return (
                            jsonify(
                                {
                                    "error": (
                                        "invalid --timeout: expected a "
                                        "non-negative integer"
                                    )
                                }
                            ),
                            400,
                        )
                    cmd_timeout = min(
                        requested_timeout + 30,
                        CLI_PROXY_TIMEOUT_CEILING,
                    )

        try:
            result = subprocess.run(
                [cli_bin] + args,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
                cwd=os.environ.get("ASSIST_CLI_DIR", os.path.expanduser("~")),
            )
            return jsonify(
                {
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "returncode": result.returncode,
                }
            )
        except subprocess.TimeoutExpired:
            return (
                jsonify({"error": f"timeout ({cmd_timeout}s)", "returncode": -1}),
                504,
            )
        except FileNotFoundError:
            return jsonify({"error": "CLI binary not found", "returncode": -1}), 500
    finally:
        if tmp_dir:
            # Clean the owned tree, including files created before they could
            # be added to tmp_files (for example, on malformed base64).
            shutil.rmtree(tmp_dir, ignore_errors=True)
