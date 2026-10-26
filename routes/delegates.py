"""routes/delegates.py — the delegate watcher, its health, and GET /api/delegates.

Spec 7185 §5.2–§5.4. Every WATCH_INTERVAL_SEC one `list-panes -a`; for the
panes of sessions named with a `delegates.prefixes` prefix only, a sample is
built here — the capture (by pane id, through shared/pane_scan.py), the
process observation, the attention observation, a locked copy of the
Auto-Yes records and the claude session status — and classified by the pure
shared/delegate_state.py. It runs whether or not a browser is open and
whether or not Auto-Yes is armed, and it never types into a pane.

Health, and errors versus empty (§5.3): a failed `list-panes` keeps every
published state, sets `watch.error` and collects nothing; one pane's exception
marks only that pane `stale`. "No delegates" is `panes: []` with
`error: null`; a watch that is off is `watch: null`.
"""

import json
import logging
import os
import secrets
import subprocess
import time
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, jsonify, request

import shared.state as state
from routes.autoyes import (
    _prompt_identity, autoyes_will_consider, detection_window_lines, observe_attention,
)
from shared import delegate_signals, pane_scan, resume_line
from shared.delegate_channel import Channel, SEED_OPTIONS, seed
from shared.tmux import tmux_exact_target
from shared.agent_identity import ROOT_UNKNOWN, observe_process_tree, refine_with_content, pane_root_start
from shared.delegate_state import classify, identity_of

log = logging.getLogger(__name__)
delegates_bp = Blueprint("delegates", __name__)

WATCH_INTERVAL_SEC = 5
CAPTURE_MAX_AGE_SEC = 30
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
# The title is last: it is free text and may hold a tab.
_LIST_FORMAT = "\t".join((
    "#{session_name}", "#{session_id}", "#{window_index}", "#{pane_index}", "#{pane_id}",
    "#{pane_pid}", "#{pane_dead}", "#{pane_width}", "#{pane_height}", "#{window_activity}",
    "#{history_size}", "#{cursor_x}", "#{cursor_y}", "#{alternate_on}",
    *("#{@assist_" + key + "}" for key in SEED_OPTIONS), "#{pane_title}",
))
_FIELDS = ("session", "session_id", "window", "pane", "pane_id", "pane_pid", "pane_dead",
           "pane_width", "pane_height", "window_activity", "history_size", "cursor_x",
           "cursor_y", "alternate_on", *("assist_" + key for key in SEED_OPTIONS), "pane_title")
_ALERT_SETTING = {"waiting": "alert_waiting", "finished": "alert_finished", "dead": "alert_dead"}

# pane_id -> classify memory, and the facts kept across samples of one pane.
_MEMORY = {}
_FIRST_SEEN = {}
_EPOCH = secrets.token_hex(4)
_LOGGED = set()
_CHANNEL = Channel()


def _settings():
    return state.get_settings().get("delegates") or dict(state.DEFAULT_SETTINGS["delegates"])


@lru_cache(maxsize=1)
def _boot_and_ticks():
    """(boot time, clock ticks per second) for turning a /proc start into epoch seconds."""
    with open("/proc/stat", encoding="utf-8") as f:
        btime = next(int(line.split()[1]) for line in f if line.startswith("btime "))
    return btime, os.sysconf("SC_CLK_TCK")


def _born(pane_id, root_start, now):
    """When this pane generation started: its root process (Linux) or first sight."""
    if root_start and str(root_start).isdigit():
        try:
            btime, ticks = _boot_and_ticks()
            return btime + int(root_start) / ticks
        except (OSError, ValueError, StopIteration):
            pass
    return _FIRST_SEEN.setdefault((pane_id, root_start), now)


def _claude_status(agent_pid, agent_start, directory=None):
    """`status` from claude's session file, only when it names this process start."""
    if agent_pid is None or not agent_start:
        return None
    path = Path(directory or CLAUDE_SESSIONS_DIR) / f"{int(agent_pid)}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or str(record.get("procStart")) != str(agent_start):
        return None
    status = record.get("status")
    return status if isinstance(status, str) else None


def _capture(pane_id):
    """capture-pane by pane id, never by name: a reused name is another pane."""
    cap = subprocess.run(
        ["tmux", "capture-pane", "-e", "-p", "-t", tmux_exact_target(pane_id), "-S", f"-{detection_window_lines()}"],
        capture_output=True, text=True, timeout=5,
    )
    return cap.stdout if cap.returncode == 0 else None


def _autoyes_copy(target, session, process_kind, agent_kind):
    """The scanner's live records for this target, copied under autoyes_lock."""
    enabled, source = state.autoyes_enabled_for(session)
    armed = autoyes_will_consider(target, enabled, source, process_kind, agent_kind)
    with state.autoyes_lock:
        countdown = state.autoyes_countdowns.get(target)
        answered = state.autoyes_answered.get(target)
        disposition = state.autoyes_disposition.get(target)
        return {
            "armed": armed,
            "countdown": dict(countdown) if countdown else None,
            "answered": tuple(answered) if answered else None,
            "disposition": dict(disposition) if disposition else None,
        }


def _validated_root(observation, pane_pid):
    """The pane generation's root, from the observation itself: its start
    time when the observer read it and its closing recheck agreed (no stat
    error for the root); None where there is no /proc; else ROOT_UNKNOWN."""
    errors = observation.errors
    if (None, "/proc", "unsupported") in errors:
        return None
    root_stat = f"/proc/{pane_pid}/stat"
    if observation.root_start and not any(path == root_stat for _pid, path, _name in errors):
        return observation.root_start
    return ROOT_UNKNOWN


def _uncertain_sample(row, now, settings, identity):
    """No pane identity could be established: an incomplete observation for
    the generation already remembered, so the classifier keeps the published
    state with health unknown, unless tmux says the pane is dead. No capture is taken; there is nothing to
    publish it under."""
    memory = _MEMORY.get(row["pane_id"])
    return {
        "now": now, "pane_id": row["pane_id"], "session_id": row["session_id"],
        "target": row["target"],
        "generation": memory["generation"] if memory else (row["pane_id"], None),
        # tmux's own dead flag is a fact the root read is not needed for: a
        # retained dead pane still reaches rule 1 (DEAD).
        "generation_born": now, "pane_dead": row["pane_dead"] == "1", "pane_title": row["pane_title"],
        "pane_width": None, "tail": "", "content_rev": None, "changed_at": None,
        "identity": {**identity, "status": "incomplete"}, "content_kind": None,
        "attention": None, "request_id": None,
        "autoyes": {"armed": False, "countdown": None, "answered": None, "disposition": None},
        "chrome": delegate_signals.chrome("", ""), "claude_status": None,
        "report_line": None, "stall": None, "capture_ok": True, "listing_ok": True,
        "config": {key: settings[key] for key in ("alert_hold_sec", "finish_quiet_sec", "stall_sec")},
    }


def _build_sample(row, now, settings):
    """The §4.2 sample for one delegate pane. Reads tmux, /proc and the Auto-Yes maps."""
    pane_id, target = row["pane_id"], row["target"]
    observation = observe_process_tree(row["pane_pid"])
    identity = identity_of(observation)
    row["observation_complete"] = observation.complete
    # One pane-generation discriminator for the cache and the classifier: the
    # root this observation validated. An unknown root proves nothing, so the
    # pane is uncertain rather than matched to the generation remembered.
    root = _validated_root(observation, row["pane_pid"])
    if root is ROOT_UNKNOWN:
        row["resume"], row["resume_reason"] = resume_line.RESOLVER.resolve(
            pane_id, row["session"], observation, now=now,
            pane_dead=row["pane_dead"] == "1", tail=(_capture(pane_id) or "") if row["pane_dead"] == "1" else "")
        return _uncertain_sample(row, now, settings, identity)
    root_start = root
    marker = (row["window_activity"], row["history_size"], row["cursor_x"], row["cursor_y"],
              row["pane_width"], row["pane_height"], row["alternate_on"], pane_id)
    process_kind = identity["kind"] if identity["status"] == "present" else None
    sampled = pane_scan.sample(
        target, pane_id, marker, now, CAPTURE_MAX_AGE_SEC,
        capture=lambda _target: _capture(pane_id),
        kind_of=lambda tail: refine_with_content(process_kind, tail),
        root=root,
    )
    raw = sampled.raw if sampled else ""
    row["resume"], row["resume_reason"] = resume_line.RESOLVER.resolve(
        pane_id, row["session"], observation, now=now, tail=raw, pane_dead=row["pane_dead"] == "1")
    content_kind = refine_with_content(process_kind, raw)
    agent_kind = process_kind or content_kind
    width = int(row["pane_width"]) if row["pane_width"].isdigit() else None
    attention = observe_attention(raw, agent_kind, width) if sampled else None
    request_id = None
    if attention and attention["kind"] == "answerable":
        request_id = _prompt_identity(raw, attention["answer"][0], agent_kind)
    title = row["pane_title"]
    return {
        "now": now, "pane_id": pane_id, "session_id": row["session_id"], "target": target,
        "generation": (pane_id, root_start), "generation_born": _born(pane_id, root_start, now),
        "pane_dead": row["pane_dead"] == "1", "pane_title": title, "pane_width": width,
        "tail": sampled.tail if sampled else "", "content_rev": sampled.rev if sampled else None,
        "changed_at": sampled.changed_at if sampled else None,
        "identity": identity, "content_kind": content_kind,
        "attention": attention, "request_id": request_id,
        "autoyes": _autoyes_copy(target, row["session"], process_kind or content_kind, agent_kind),
        "chrome": delegate_signals.chrome(raw, title),
        "claude_status": _claude_status(identity["agent_pid"], identity["agent_start"])
        if agent_kind == "claude" else None,
        "report_line": delegate_signals.report_line(raw),
        "stall": delegate_signals.stall_counter(raw),
        "capture_ok": sampled is not None, "listing_ok": True,
        "config": {key: settings[key] for key in ("alert_hold_sec", "finish_quiet_sec", "stall_sec")},
    }


def _wire_alert(result, settings):
    """The current alert, derived each tick from the published state and key
    and the settings as they are now: null unless an alerting state is
    published and its setting is on. `at` is when the key was first published,
    so the same alert reads the same across ticks and re-publications; each
    browser keeps its own seen-key set (spec 7185 §6.1)."""
    state_name, key = result["state"], result["key"]
    setting = _ALERT_SETTING.get(state_name)
    if not key or not setting or settings.get(setting) != "on":
        return None
    return {"key": key, "state": state_name, "reason": result["reason"],
            "detail": result["detail"], "at": result["key_at"]}


def _entry(row, result, settings):
    alert = _wire_alert(result, settings)
    return {
        "pane_id": row["pane_id"], "target": row["target"], "session": row["session"],
        "session_id": row["session_id"],
        "resume": row.get("resume"), "resume_reason": row.get("resume_reason"),
        "state": result["state"], "reason": result["reason"], "detail": result["detail"],
        "since": result["since"], "episode": result["episode"], "health": result["health"],
        "observed_at": result["observed_at"], "key": result["key"], "alert": alert,
    }


def _missing(row, now, settings):
    """The entry for a pane this tick could not sample, on every failure path.

    Its memory gets a missing sample (rule 0): the published state, key and
    first publication time are kept, health is stale, and no consecutive
    evidence survives the gap. The entry is then built by the same _entry the
    healthy path uses, so its alert follows the current settings."""
    pane_id = row["pane_id"]
    memory = _MEMORY.get(pane_id)
    if memory is None:
        return {
            "pane_id": pane_id, "target": row["target"], "session": row["session"],
            "session_id": row["session_id"], "state": "unknown", "reason": "", "detail": "",
            "since": None, "episode": 0, "health": "stale", "observed_at": None, "key": None,
            "alert": None,
        }
    with state.delegate_lock:
        previous = state.delegate_states.get(pane_id, {})
    row = {**row, "resume": previous.get("resume"), "resume_reason": previous.get("resume_reason")}
    result, _MEMORY[pane_id] = classify(
        {"now": now, "pane_id": pane_id, "generation": memory["generation"],
         "capture_ok": False, "listing_ok": False}, memory)
    return _entry(row, result, settings)


def _fail_listing(now, error, settings):
    """No inventory this tick (a failed or raising list-panes, or the loop's
    own failure): every published pane goes through _missing, nothing is
    collected, and watch.error says why."""
    with state.delegate_lock:
        previous = dict(state.delegate_states)
    published = {pane_id: _missing(entry, now, settings) for pane_id, entry in previous.items()}
    for pane_id in set(_MEMORY) - set(published):
        _missing({"pane_id": pane_id, "target": None, "session": None, "session_id": None},
                 now, settings)
    with state.delegate_lock:
        state.delegate_states.clear()
        state.delegate_states.update(published)
    _set_watch(last_tick_at=now, error=error[:300])
    _publish_channel(published, now, settings)


def _set_watch(**fields):
    with state.delegate_lock:
        watch = dict(state.delegate_watch or {
            "epoch": _EPOCH, "rev": 0, "last_tick_at": None, "last_ok_at": None, "error": None,
        })
        watch.update(fields)
        watch["rev"] += 1
        state.delegate_watch = watch


def _parse(stdout):
    rows = []
    for line in stdout.splitlines():
        parts = line.split("\t", len(_FIELDS) - 1)
        if len(parts) != len(_FIELDS):
            continue
        row = dict(zip(_FIELDS, parts))
        row["target"] = f"{row['session']}:{row['window']}.{row['pane']}"
        rows.append(row)
    return rows


def _no_server(stderr):
    """No tmux server is running: an empty inventory, not an error. A missing
    socket or a refused connection (a socket left by a dead server) both mean
    that; anything else, such as a permission error, means cannot tell."""
    return "no server running" in stderr or (
        "error connecting to" in stderr
        and ("(No such file or directory)" in stderr or "(Connection refused)" in stderr)
    )


def _publish_channel(published, now, settings, rows=None):
    live_ids = None if rows is None else {r[k] for r in rows for k in ("pane_id", "session_id")}
    enabled = settings.get("watch") == "on" and settings.get("tmux_channel") == "on"
    try:
        try:
            _CHANNEL.publish(list(published.values()), _MEMORY, _EPOCH, now,
                             enabled, live_ids=live_ids)
        except RuntimeError:
            # A disappearing id aborts tmux's command queue. Retry only when
            # a successful new inventory proves a target vanished, and only once.
            proc = subprocess.run(
                ["tmux", "list-panes", "-a", "-F", _LIST_FORMAT],
                capture_output=True, text=True, timeout=5)
            if proc.returncode:
                raise
            current = _parse(proc.stdout)
            current_ids = {r[k] for r in current for k in ("pane_id", "session_id")}
            previous_ids = (live_ids or set(published)) | set(_CHANNEL.panes) | set(_CHANNEL.sessions)
            if not previous_ids - current_ids:
                raise
            remaining = {pid: entry for pid, entry in published.items() if pid in current_ids}
            _CHANNEL.publish(list(remaining.values()), _MEMORY, _EPOCH, now,
                             enabled, live_ids=current_ids)
            for pid in set(published) - set(remaining):
                _MEMORY.pop(pid, None)
                with state.delegate_lock:
                    state.delegate_states.pop(pid, None)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if not _no_server(str(exc)):
            _set_watch(error=str(exc)[:300])


def tick(now=None):
    """One watcher pass. `now` is injectable so tests need not sleep."""
    now = time.time() if now is None else now
    settings = _settings()
    try:
        proc = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _LIST_FORMAT], capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        # A listing that raised (TimeoutExpired, a missing tmux) is a failed listing.
        _fail_listing(now, f"list-panes: {exc}", settings)
        return
    if proc.returncode != 0:
        if not _no_server(proc.stderr or ""):
            # Cannot tell: keep every published state and collect nothing.
            _fail_listing(now, (proc.stderr or "list-panes failed").strip(), settings)
            return
        rows = []
    else:
        rows = _parse(proc.stdout)
    if settings.get("watch") != "on" or settings.get("tmux_channel") != "on":
        _CHANNEL.discover(rows)
    if settings.get("watch") != "on":
        _publish_channel({}, now, settings, rows)
        with state.delegate_lock:
            state.delegate_states.clear()
            state.delegate_watch = None
        _MEMORY.clear()
        return
    prefixes = tuple(p for p in settings.get("prefixes", ()) if p)
    delegates = [r for r in rows if prefixes and r["session"].startswith(prefixes)]
    published = {}
    for row in delegates:
        pane_id = row["pane_id"]
        try:
            sample = _build_sample(row, now, settings)
            _CHANNEL.restore(row, sample["generation"])
            if pane_id not in _MEMORY:
                seeded = seed(row, sample["generation"])
                if seeded:
                    _MEMORY[pane_id] = seeded
            previous_observed = (_MEMORY.get(pane_id) or {}).get("observed_at")
            result, _MEMORY[pane_id] = classify(sample, _MEMORY.get(pane_id))
            if row.get("observation_complete") is False and row.get("pane_dead") != "1":
                result["health"] = _MEMORY[pane_id]["health"] = "unknown"
                result["observed_at"] = _MEMORY[pane_id]["observed_at"] = previous_observed
            published[pane_id] = _entry(row, result, settings)
        except Exception:
            if pane_id not in _LOGGED:
                _LOGGED.add(pane_id)
                log.exception("delegate watcher: sample failed for %s", pane_id)
            published[pane_id] = _missing(row, now, settings)
    live = {r["pane_id"] for r in delegates}
    for gone in set(_MEMORY) - live:
        _MEMORY.pop(gone, None)
    for key in [k for k in _FIRST_SEEN if k[0] not in live]:
        _FIRST_SEEN.pop(key, None)
    with state.delegate_lock:
        state.delegate_states.clear()
        state.delegate_states.update(published)
    pane_scan.forget_missing({r["target"] for r in rows}, True)
    resume_line.RESOLVER.forget_missing({r["pane_id"] for r in rows})
    _set_watch(last_tick_at=now, last_ok_at=now, error=None)
    _publish_channel(published, now, settings, rows)


def snapshot():
    """{"watch", "panes"} as /api/delegates and /poll serve them."""
    with state.delegate_lock:
        watch = dict(state.delegate_watch) if state.delegate_watch else None
        panes = [dict(entry) for _id, entry in sorted(state.delegate_states.items())]
    return {"watch": watch, "panes": panes}


def delegate_watcher():
    """The watcher thread, started by serve.py beside the Auto-Yes scanner."""
    while True:
        started = time.monotonic()
        try:
            tick()
        except Exception:
            log.exception("delegate watcher tick failed")
            try:
                _fail_listing(time.time(), "watcher tick failed", _settings())
            except Exception:
                log.exception("delegate watcher could not record the failed tick")
                _set_watch(last_tick_at=time.time(), error="watcher tick failed")
        time.sleep(max(0.5, WATCH_INTERVAL_SEC - (time.monotonic() - started)))


@delegates_bp.route("/api/delegates")
def api_delegates():
    return jsonify(snapshot())


def resume_for_pane(pane, fresh=False, tail=None):
    if not fresh:
        cached = resume_line.RESOLVER.cached(pane['pane_id'], pane['session'],
                                              pane_root_start(pane['pane_pid']), time.time())
        if cached is not None:
            return cached
    observation = observe_process_tree(pane['pane_pid'])
    if observation.complete and not observation.agents and tail is None:
        tail = _capture(pane['pane_id']) or ''
    return resume_line.RESOLVER.resolve(pane['pane_id'], pane['session'], observation,
                                        fresh=fresh, cache_seconds=0 if fresh else 30, tail=tail or '',
                                        pane_dead=pane.get('pane_dead') == '1')


@delegates_bp.route('/api/resume')
def api_resume():
    target = request.args.get('target', '').strip()
    if not target:
        return jsonify(error='target required'), 400
    try:
        proc = subprocess.run(['tmux', 'display-message', '-p', '-t', tmux_exact_target(target),
                               _LIST_FORMAT], capture_output=True, text=True, timeout=5)
        rows = _parse(proc.stdout) if proc.returncode == 0 else []
        if not rows:
            return jsonify(resume=None, resume_reason='target unavailable'), 404
        record, reason = resume_for_pane(rows[0], fresh=request.args.get('fresh') == '1')
        return jsonify(resume=record, resume_reason=reason)
    except (OSError, subprocess.SubprocessError):
        return jsonify(resume=None, resume_reason='observation unavailable'), 503
