"""Reviewed, generation-bound purging of content-idle tmux sessions."""

import hashlib
import os
import secrets
import subprocess
import threading
import time

from shared import state, tab_state
from shared.agent_identity import refine_with_content, resolve_process
from shared.tmux import PASSWORD_PROMPT_RE, _process_start_time, tmux_exact_target
from routes.autoyes import (
    _detect_autoyes_prompt, _PROMPT_TERMINATOR_RE, detection_window, detection_window_lines,
)

_PREVIEWS = {}
_LOCK = threading.Lock()
PREVIEW_TTL = 600


def _observe(session=None):
    # Share ticket A's activity normalization; never change the captured text.
    from routes.poll import _activity_tail

    command = ["tmux", "list-panes"]
    if session is None:
        command.append("-a")
    else:
        command.extend(["-s", "-t", tmux_exact_target(session)])
    command.extend(["-F",
         "#{session_name}\t#{session_id}\t#{pid}\t#{session_created}\t"
         "#{window_index}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t"
         "#{pane_current_command}\t#{@assist_owned}"])
    proc = subprocess.run(
        command,
        capture_output=True, text=True, timeout=5,
    )
    if proc.returncode:
        # An absent server is an empty list, other failures must not offer kills.
        if ("no server running" in proc.stderr or "error connecting" in proc.stderr
                or (session is not None and "can't find session" in proc.stderr)):
            return {}
        raise RuntimeError("Cannot list tmux sessions")
    now = time.time()
    sessions = {}
    changed = False
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 10:
            raise RuntimeError("Incomplete tmux session identity")
        name, session_id, server_pid, created, window, pane, pane_id, pane_pid, command, owned = parts
        target = f"{name}:{window}.{pane}"
        row = sessions.setdefault(name, {
            "session": name, "session_id": session_id,
            "generation": (server_pid, _process_start_time(server_pid), session_id, created),
            "panes": [], "activity": {}, "prompt": False, "owned": False,
        })
        row["owned"] |= (name in state.assist_owned_sessions or owned == "1"
                         or bool(os.environ.get("TMUX") and os.environ.get("TMUX_PANE") == pane_id))
        row["panes"].append((target, pane_id, pane_pid))
        cap = subprocess.run(
            ["tmux", "capture-pane", "-e", "-p", "-t", pane_id, "-S", f"-{detection_window_lines()}"],
            capture_output=True, text=True, timeout=5,
        )
        if cap.returncode:
            raise RuntimeError(f"Cannot capture {name}; refresh the list")
        tail = cap.stdout.rstrip("\n")
        kind = refine_with_content(resolve_process(target, pane_pid, command), tail)
        content_hash = hashlib.md5(_activity_tail(tail, kind).encode()).hexdigest()
        with state._activity_lock:
            previous = state.pane_content_hash.get(target)
            if previous is not None and previous != content_hash:
                state.pane_last_activity[target] = now
            state.pane_last_activity.setdefault(target, now)
            # Persisted name-based activity cannot predate this session's birth.
            activity = max(state.pane_last_activity[target], int(created))
            changed |= state.pane_last_activity[target] != activity
            state.pane_last_activity[target] = activity
            state.pane_content_hash[target] = content_hash
            row["activity"][target] = state.pane_last_activity[target]
            changed |= previous != content_hash
        window_tail = detection_window(tail)
        nonempty = [ln for ln in window_tail.splitlines() if ln.strip()]
        row["prompt"] |= bool(
            _detect_autoyes_prompt(window_tail, kind)
            or _PROMPT_TERMINATOR_RE.search("\n".join(nonempty[-20:]))
            or (nonempty and PASSWORD_PROMPT_RE.search(nonempty[-1]))
        )
    if changed:
        state.save_idle_state()
    for row in sessions.values():
        row["idle_seconds"] = max(0, now - max(row["activity"].values()))
        row["panes"].sort()
    return sessions


def preview(threshold=None, target=None):
    if threshold is None:
        threshold = state.get_setting("ui", "purge_idle_threshold_sec")
    if isinstance(threshold, bool) or threshold not in state.PURGE_IDLE_THRESHOLDS:
        raise ValueError("Idle threshold must be 1h, 4h, 12h, 24h or 3d")
    sessions = _observe()
    pinned = tab_state.get_tab_state().get("pinned", [])
    active_session = (target or "").split(":", 1)[0]
    rows, offered = [], {}
    for name, row in sorted(sessions.items()):
        if row["owned"] or row["idle_seconds"] <= threshold:
            continue
        reasons = []
        if name in pinned:
            reasons.append("pinned")
        if row["prompt"]:
            reasons.append("waiting at a prompt")
        if name == active_session:
            reasons.append("in view")
        rows.append({"session": name, "idle_seconds": int(row["idle_seconds"]),
                     "checked": not reasons, "reasons": reasons})
        offered[name] = row
    token = secrets.token_urlsafe(24)
    with _LOCK:
        now = time.monotonic()
        for key in list(_PREVIEWS):
            if now - _PREVIEWS[key][0] > PREVIEW_TTL:
                del _PREVIEWS[key]
        while len(_PREVIEWS) >= 128:
            del _PREVIEWS[next(iter(_PREVIEWS))]
        _PREVIEWS[token] = (now, threshold, offered)
    return {"ok": True, "preview": token, "idle": threshold, "sessions": rows}


def execute(token, selected, kill):
    if not isinstance(token, str) or not isinstance(selected, list) or any(
        not isinstance(name, str) for name in selected
    ):
        raise ValueError("Expected a preview and a list of sessions")
    with _LOCK:
        saved = _PREVIEWS.get(token)
        if not saved or time.monotonic() - saved[0] > PREVIEW_TTL:
            raise ValueError("Purge preview expired; refresh the list")
        _, threshold, offered = saved
        if any(name not in offered for name in selected):
            raise ValueError("Session was not in this purge preview")
        del _PREVIEWS[token]  # a reviewed selection can be executed only once
    killed, skipped = [], []
    for name in dict.fromkeys(selected):
        try:
            current = _observe(name).get(name)
            old = offered[name]
            if not current:
                reason = "session gone"
            elif current["owned"]:
                reason = "Assist server session"
            elif current["generation"] != old["generation"] or current["panes"] != old["panes"]:
                reason = "session recreated or panes changed"
            elif current["activity"] != old["activity"] or current["idle_seconds"] <= threshold:
                reason = "activity changed"
            else:
                # Hold off first-party activity writes through the common kill path.
                with state._activity_lock:
                    if any(state.pane_last_activity.get(t) != at for t, at in old["activity"].items()):
                        reason = "activity changed"
                    else:
                        response = kill(name, current["session_id"])
                        body = response[0] if isinstance(response, tuple) else response
                        if body.get_json().get("ok"):
                            killed.append(name)
                            continue
                        reason = body.get_json().get("error", "kill failed")
            skipped.append({"session": name, "reason": reason})
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            skipped.append({"session": name, "reason": str(exc)})
    return {"ok": True, "killed": killed, "skipped": skipped}
