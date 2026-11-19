"""Session discovery and pane capture commands for the Assist CLI."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

from cli import http, launch_gate, roster as roster_cli, wait as wait_cli
from shared import agent_identity, agent_model


NEXT_COMMANDS = {
    "launch": (
        'assist send {session} "continue" --enter',
        "assist send {session} --enter",
        "assist wait {session}",
        "assist view {session}",
    ),
    "send": (
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist send {session} --enter",
        "assist view {session}",
    ),
    "view": (
        "assist view {session}",
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist kill {session}",
    ),
    "wait": (
        "assist view {session}",
        "assist wait {session}",
        'assist send {session} "continue" --enter',
        "assist kill {session}",
    ),
    "status": (
        "assist view {session} --plain",
        "assist wait {session}",
        'assist send {session} "continue" --enter',
    ),
    "ls": (
        "assist launch --session {session} --cwd . --wait",
        "assist status {session}",
        "assist view {session}",
        "assist ls",
    ),
}


CALLBACK_HINT = (
    "callback: to be told instead of polling, send this too —\n"
    "  when you are done, or if you hit an issue or question, run: "
    'assist send {reply_to} "<status>" --enter'
)


def print_next_hint(verb: str, session: str | None) -> None:
    """Print the measured next commands for a successful session verb."""
    if os.environ.get("ASSIST_NO_HINTS") == "1" or not session:
        return
    commands = NEXT_COMMANDS.get(verb)
    if commands is None:
        return
    rendered = (command.format(session=session) for command in commands)
    print("next: " + "  ·  ".join(rendered), file=sys.stderr)


def print_role_launch_hint(session: str | None) -> None:
    if os.environ.get("ASSIST_NO_HINTS") == "1" or not session:
        return
    print(
        f"next: assist view {session}  ·  "
        f"assist send {session} --file BRIEF.md --enter --wait  ·  "
        f"assist wait {session}",
        file=sys.stderr,
    )


def print_gated_launch_hint(session: str | None) -> None:
    if os.environ.get("ASSIST_NO_HINTS") == "1" or not session:
        return
    print(
        f"next: assist view {session}  ·  assist wait {session}",
        file=sys.stderr,
    )


def reply_address() -> str | None:
    """Return the caller's own tmux session — the address a delegate replies to."""
    override = os.environ.get("ASSIST_REPLY_TO")
    if override:
        return override.strip() or None
    if not os.environ.get("TMUX"):
        return None
    command = ["tmux", "display-message", "-p"]
    pane_id = os.environ.get("TMUX_PANE")
    if pane_id:
        command += ["-t", pane_id]
    command.append("#S")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def print_callback_hint(target_session: str | None) -> None:
    """Offer the sender a callback, so it can stop polling the pane it just wrote to."""
    if os.environ.get("ASSIST_NO_HINTS") == "1":
        return
    reply_to = reply_address()
    if not reply_to or reply_to == target_session:
        return
    print(CALLBACK_HINT.format(reply_to=shlex.quote(reply_to)), file=sys.stderr)


def resolve_target(session: str | None, pane: str = "0.0") -> str:
    """Return an explicit tmux target, refusing the server-side fallback."""
    if not session:
        print("assist: no session given", file=sys.stderr)
        raise SystemExit(2)
    return f"{session}:{pane}"


def _single_space(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _format_context_pct(value: object, warn: bool = False) -> str:
    if value is None or value == "":
        return "-"
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return "-"
    text = f"{int(round(pct))}%" if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}%"
    return f"{text}!" if warn else text


def _derive_pane_status(
    pane: dict,
    poll_state: dict,
    tail: str | None = None,
) -> dict:
    """Compute unified status fields (model, effort, perms, context%, working/idle/asking)."""
    agent_kind = pane.get("agent_kind")
    if (not agent_kind or agent_kind == "unknown") and tail:
        agent_kind = agent_identity.refine_with_content(None, tail)

    live_model, live_effort = (
        agent_model.resolve_full(agent_kind, tail) if tail else (None, None)
    )
    model = _single_space(live_model if live_model is not None else pane.get("model"))
    effort = _single_space(
        live_effort if live_effort is not None else pane.get("model_effort")
    )

    live_perm = (
        agent_model.resolve_permissions(agent_kind, tail) if tail else None
    )
    permission_mode = _single_space(
        live_perm if live_perm is not None else pane.get("permission_mode")
    )

    live_ctx = agent_model.resolve_context_pct(agent_kind, tail) if tail else None
    context_pct = live_ctx if live_ctx is not None else pane.get("context_pct")
    context_warn = agent_model.context_warning(agent_kind, context_pct)

    prompt = None
    if tail:
        found = wait_cli.detect_prompt(tail, agent_kind)
        if found is not None:
            prompt = {"type": found[0], "summary": found[1]}

    raw_state = poll_state.get("state")
    if prompt is not None:
        unified_state = "asking"
    elif raw_state == "running":
        unified_state = "working"
    elif raw_state in ("idle", "shell"):
        unified_state = "idle"
    else:
        unified_state = None

    idle_seconds = poll_state.get("idle_seconds", pane.get("idle_seconds"))
    return {
        "session": pane.get("session"),
        "target": pane.get("target"),
        "pane_id": pane.get("pane_id"),
        "key": pane.get("key"),
        "agent_kind": agent_kind,
        "state": unified_state,
        "poll_state": raw_state,
        "idle_seconds": idle_seconds,
        "model": model,
        "effort": effort,
        "permission_mode": permission_mode,
        "context_pct": context_pct,
        "context_warn": context_warn,
        "prompt": prompt,
    }


def _merged_sessions(include_cwd: bool = False) -> dict:
    sessions_response = http.get("/terminal/sessions")
    poll_response = http.get("/poll")
    states = poll_response.get("states") or {}
    poll_panes = {p.get("target"): p for p in poll_response.get("sessions") or []}
    scan_by_target = {s.get("target"): s for s in poll_response.get("scan") or []}

    panes = []
    for pane in sessions_response.get("sessions") or []:
        merged = dict(pane)
        target = pane.get("target")
        pane_state = states.get(target) or {}
        merged.update(pane_state)
        current = poll_panes.get(target, {})
        for key in (
            "agent_kind",
            "model",
            "model_effort",
            "permission_mode",
            "context_pct",
            "delegate",
            "resume",
            "resume_reason",
        ):
            if key in current:
                merged[key] = current[key]
        tail = (scan_by_target.get(target) or {}).get("tail")
        derived = _derive_pane_status(merged, pane_state, tail=tail)
        merged["agent_kind"] = derived["agent_kind"]
        merged["state"] = derived["state"]
        merged["poll_state"] = derived["poll_state"]
        merged["model"] = derived["model"]
        merged["effort"] = derived["effort"]
        merged["permission_mode"] = derived["permission_mode"]
        merged["context_pct"] = derived["context_pct"]
        merged["context_warn"] = derived["context_warn"]
        merged["prompt"] = derived["prompt"]
        if include_cwd:
            cwd_target = merged.get("target") or merged.get("session")
            if cwd_target:
                path = "/terminal/cwd?" + urlencode({"session": cwd_target})
                merged["cwd"] = http.get(path).get("cwd")
            else:
                merged["cwd"] = None
        panes.append(merged)

    merged_response = dict(sessions_response)
    merged_response["sessions"] = panes
    return merged_response


def _text(value: object) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _idle_text(value: object) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{int(float(value))}s"
    except (TypeError, ValueError):
        return "-"


def _display_rows(panes: list[dict], include_cwd: bool) -> list[list[str]]:
    rows = []
    for pane in panes:
        name = pane.get("target") if pane.get("is_subpane") else pane.get("session")
        row = [
            _text(name),
            _text(pane.get("state")),
            _text(pane.get("agent_kind")),
            _text(pane.get("model")),
            _text(pane.get("effort") if pane.get("effort") is not None else pane.get("model_effort")),
            _text(pane.get("permission_mode")),
            _format_context_pct(pane.get("context_pct"), bool(pane.get("context_warn"))),
            _idle_text(pane.get("idle_seconds")),
            _text(pane.get("command_display")),
        ]
        if include_cwd:
            row.append(_text(pane.get("cwd")))
        delegate = pane.get("delegate") or {}
        row.append(f"{delegate['state']}:{delegate.get('reason', '')}" if delegate.get("state") else "-")
        rows.append(row)
    return rows


def _print_aligned(rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]) - 1)]
    for row in rows:
        columns = [
            value.ljust(widths[index]) if index < len(widths) else value
            for index, value in enumerate(row)
        ]
        print("  ".join(columns))


def list_sessions(json_output: bool = False, include_cwd: bool = False) -> int:
    response = _merged_sessions(include_cwd=include_cwd)
    if json_output:
        print(json.dumps(response, indent=4))
        return 0

    rows = _display_rows(response.get("sessions") or [], include_cwd)
    _print_aligned(rows)
    if rows:
        print_next_hint("ls", rows[0][0])
    return 0


def status(
    session: str | None,
    pane: str | None = None,
    json_output: bool = False,
) -> int:
    """Print one-line or JSON status (model, effort, perms, context%, working/idle/asking)."""
    if not session:
        resolve_target(session)

    poll_response = http.get("/poll")
    panes = poll_response.get("sessions") or []
    states = poll_response.get("states") or {}

    matched: dict | None = None
    if pane is not None:
        want = f"{session}:{pane}"
        matched = next((p for p in panes if p.get("target") == want), None)
        if matched is None:
            print(f"assist: target not found: {want}", file=sys.stderr)
            return 1
    else:
        want_zero = f"{session}:0.0"
        matched = next((p for p in panes if p.get("target") == want_zero), None)
        if matched is None:
            session_panes = [p for p in panes if p.get("session") == session]
            matched = next(
                (p for p in session_panes if not p.get("is_subpane")),
                session_panes[0] if session_panes else None,
            )
        if matched is None:
            print(f"assist: session not found: {session}", file=sys.stderr)
            return 1

    target = str(matched.get("target") or f"{session}:0.0")
    capture_resp = http.get("/terminal/capture?" + urlencode({"target": target}))
    capture_content = capture_resp.get("content")
    if not isinstance(capture_content, str):
        print(f"assist: capture returned no content for {target}", file=sys.stderr)
        return 1
    tail = capture_content

    record = _derive_pane_status(matched, states.get(target) or {}, tail=tail)
    if json_output:
        print(json.dumps(record, indent=2))
        return 0

    name = record["target"] if matched.get("is_subpane") else record["session"]
    parts = [
        _text(name),
        f"state={_text(record['state'])}",
        f"agent={_text(record['agent_kind'])}",
        f"model={_text(record['model'])}",
        f"effort={_text(record['effort'])}",
        f"perms={_text(record['permission_mode'])}",
        f"context={_format_context_pct(record['context_pct'], bool(record['context_warn']))}",
    ]
    if record["prompt"] is not None:
        parts.append(f"prompt={record['prompt']['type']}: {record['prompt']['summary']}")
    print("  ".join(parts))
    print_next_hint("status", session)
    return 0


def view(
    session: str | None,
    pane: str = "0.0",
    lines: int | None = None,
    plain: bool = False,
) -> int:
    target = resolve_target(session, pane)
    query = {"target": target}
    if lines is not None:
        query["lines"] = lines
    response = http.get("/terminal/capture?" + urlencode(query))
    content = response.get("content")
    if content is not None:
        rendered = str(content)
        if plain:
            rendered = agent_identity._ANSI_ESCAPE_RE.sub("", rendered)
        sys.stdout.write(rendered)
    print_resume_hint(target)
    print_next_hint("view", session)
    return 0


def launch(
    session: str | None,
    cwd: str | None = None,
    cols: int | None = None,
    rows: int | None = None,
    wait_for_completion: bool = False,
    timeout: float = wait_cli.DEFAULT_TIMEOUT,
    role: str | None = None,
    family: str | None = None,
    against: str | None = None,
    roster_path: Path | None = None,
    brief_path: str | None = None,
    trust_cwd: bool = False,
    approved_by_daniel: bool = False,
    approved_by_karen: bool = False,
) -> int:
    if not session:
        resolve_target(session)
    if role is None and (family is not None or against is not None):
        print("assist: --family and --against require --role", file=sys.stderr)
        raise SystemExit(2)
    if role is None and (
        brief_path is not None or trust_cwd or approved_by_daniel or approved_by_karen
    ):
        print(
            "assist: --brief, --trust-cwd, --approved-by-daniel, and --approved-by-karen require --role",
            file=sys.stderr,
        )
        raise SystemExit(2)

    brief_text: str | None = None
    if brief_path is not None:
        if brief_path == "-":
            brief_text = sys.stdin.read()
        else:
            try:
                brief_text = Path(brief_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                print(f"assist: unable to read {brief_path}: {exc}", file=sys.stderr)
                raise SystemExit(1) from None
        if not brief_text.strip():
            print(f"assist: brief file is empty: {brief_path}", file=sys.stderr)
            raise SystemExit(2)

    launch_command = None
    expected = None
    if role is not None:
        if roster_path is None:
            raise ValueError("roster_path is required with role")
        loaded_roster = roster_cli.load(roster_path)
        entry_key, entry = roster_cli.resolve_entry(
            loaded_roster, role, family=family, against=against
        )
        roster_cli.enforce_launch_policy(
            loaded_roster,
            role,
            entry_key,
            entry,
            approved_by_daniel=approved_by_daniel,
            approved_by_karen=approved_by_karen,
        )
        launch_command = entry.command
        if brief_path is not None or trust_cwd:
            try:
                expected = launch_gate.parse_roster_command(launch_command)
            except ValueError as exc:
                print(f"assist: {exc}", file=sys.stderr)
                raise SystemExit(2) from None
            if trust_cwd and expected.transport != "agy":
                print(
                    f"assist: --trust-cwd is only valid for agy (gemini) launches, not {expected.transport}",
                    file=sys.stderr,
                )
                raise SystemExit(2)

    payload: dict[str, object] = {
        "project": session,
        "skip_init": True,
    }
    parent = reply_address()
    if parent:
        payload["parent"] = parent
    if cwd is not None:
        payload["cwd"] = cwd
    if cols is not None:
        payload["cols"] = cols
    if rows is not None:
        payload["rows"] = rows

    response = http.post("/terminal/launch", payload)
    for key in ("session", "target", "venv", "venv_activated", "existed"):
        print(f"{key}: {_text(response.get(key))}")

    if launch_command is not None and response.get("existed"):
        print(
            f"assist: session {response['session']} already exists "
            f"(target {response['target']}); role command not sent — use assist send",
            file=sys.stderr,
        )
        return 2

    if wait_for_completion:
        exit_code = wait_cli.run_wait(
            response["session"],
            response["target"],
            timeout,
            baseline_hash=None,
        )
        if exit_code != 0:
            if launch_command is not None:
                print(
                    "assist: role command not sent (pane not idle after launch)",
                    file=sys.stderr,
                )
            return exit_code
    if launch_command is not None:
        print(f"command: {launch_command}")
        target = str(response["target"])
        pane = target.split(":", 1)[1] if ":" in target else "0.0"
        if expected is not None:
            gated_start = launch_gate.time.monotonic()
            baseline_hash = wait_cli.capture_hash(target)
            http.post(
                "/type",
                {
                    "target": target,
                    "text": launch_command,
                    "enter": True,
                    "no_history": True,
                    "raw": True,
                },
            )
            gate_code, gate_error = launch_gate.wait_for_gated_ready(
                target,
                expected,
                baseline_hash,
                timeout=timeout,
                trust_cwd=trust_cwd,
            )
            if gate_code != 0:
                print(f"assist: {gate_error}", file=sys.stderr)
                return gate_code
            if brief_text is not None:
                elapsed = max(0.0, launch_gate.time.monotonic() - gated_start)
                remaining = max(0.0, float(timeout) - elapsed)
                work_code, sent_chars, work_error = (
                    launch_gate.deliver_brief_and_confirm_working(
                        target,
                        expected,
                        brief_text,
                        timeout=remaining,
                    )
                )
                if work_code != 0:
                    print(f"assist: {work_error}", file=sys.stderr)
                    return work_code
                print(f"brief: sent {sent_chars} chars to {target}", flush=True)
                print("working: confirmed", flush=True)
                print_callback_hint(response.get("session"))
                print_gated_launch_hint(response.get("session"))
                return 0
            print_role_launch_hint(response.get("session"))
            return 0
        exit_code = send(
            response["session"],
            text=launch_command,
            enter=True,
            pane=pane,
            wait_for_completion=True,
            timeout=timeout,
            autoyes=False,
            show_next_hint=False,
        )
        if exit_code != 0:
            return exit_code
        print_role_launch_hint(response.get("session"))
        return 0
    print_next_hint("launch", response.get("session"))
    return 0


def send(
    session: str | None,
    text: str | None = None,
    file_path: str | None = None,
    enter: bool = False,
    pane: str = "0.0",
    wait_for_completion: bool = False,
    timeout: float = wait_cli.DEFAULT_TIMEOUT,
    autoyes: bool = False,
    show_next_hint: bool = True,
) -> int:
    target = resolve_target(session, pane)
    if autoyes and not wait_for_completion:
        print("assist: --autoyes requires --wait", file=sys.stderr)
        raise SystemExit(2)
    if text is not None and file_path is not None:
        print(
            "assist: text and --file cannot be used together",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if text is None and file_path is None and not enter:
        print(
            "assist: text, --file, or --enter is required",
            file=sys.stderr,
        )
        raise SystemExit(2)

    bare_enter = text is None and file_path is None
    if file_path == "-":
        send_text = sys.stdin.read()
    elif file_path is not None:
        try:
            send_text = Path(file_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"assist: unable to read {file_path}: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
    elif text is not None:
        send_text = text
    else:
        send_text = ""

    baseline_hash = None
    if wait_for_completion:
        baseline_hash = wait_cli.capture_hash(target)

    response = http.post(
        "/type",
        {
            "target": target,
            "text": send_text,
            "enter": enter,
            "no_history": True,
            "raw": True,
        },
    )
    sent_chars = response.get("sent_chars")
    if sent_chars is None:
        sent_chars = 0
    if bare_enter:
        print(f"sent Enter to {target}", flush=True)
    else:
        print(f"sent {sent_chars} chars to {target}", flush=True)
    print_callback_hint(session)
    if wait_for_completion:
        exit_code = wait_cli.run_wait(
            session,
            target,
            timeout,
            baseline_hash,
            autoyes=autoyes,
            require_change=True,
        )
        if exit_code != 0:
            return exit_code
    if show_next_hint:
        print_next_hint("send", session)
    return 0


def _http_error_detail(error: http.ApiError) -> str:
    try:
        response = json.loads(error.body)
    except (json.JSONDecodeError, TypeError):
        response = {}
    detail = response.get("error") if isinstance(response, dict) else None
    if not detail:
        detail = str(error)
    return " ".join(str(detail).split())


def kill(session: str | None, pane: str = "0.0") -> int:
    """Kill a tmux session through Assist's existing terminal endpoint."""
    target = resolve_target(session, pane)
    print_resume_hint(target, fresh=True)
    try:
        response = http.post("/terminal/kill", {"session": session})
    except http.ApiError as exc:
        if exc.kind != "http":
            raise
        print(f"assist: {_http_error_detail(exc)}", file=sys.stderr)
        return exc.exit_code
    print(f"killed {response.get('session') or session}")
    return 0


def _autoyes_state(response: dict, session: str) -> tuple[bool, object | None]:
    enabled = bool((response.get("sessions") or {}).get(session))
    delay = (response.get("delays") or {}).get(session)
    return enabled, delay


def _delay_text(delay: object | None) -> str:
    if delay is None:
        return "server default"
    try:
        return f"{float(delay):g}s"
    except (TypeError, ValueError):
        return str(delay)


def _print_autoyes_state(
    session: str,
    enabled: bool,
    delay: object | None,
    global_on: bool = False,
    global_delay: object | None = None,
    source: str | None = None,
) -> None:
    # Say WHY it is on: under the global switch a session nobody touched reads
    # "on", and "on (global…)" is the difference between that and a hand-armed
    # session whose setting survives the switch being turned off. The delay is
    # the global one either way while the switch is on.
    if enabled and global_on and source == "global":
        print(f"autoyes {session}: on (global, delay: {_delay_text(global_delay)})")
    elif enabled and global_on:
        print(f"autoyes {session}: on (set here, delay: {_delay_text(global_delay)})")
    elif enabled:
        print(f"autoyes {session}: on (delay: {_delay_text(delay)})")
    elif global_on and source == "global":
        # In scope for the switch but nothing to act on: the switch covers agent
        # panes only, and this session has none. Not the same as an opt-out.
        print(f"autoyes {session}: off (no agent pane)")
    elif global_on:
        print(f"autoyes {session}: off (opted out of global)")
    else:
        print(f"autoyes {session}: off")


def _autoyes_global(mode: str, delay: float | None) -> int:
    """Set or inspect the all-sessions switch."""
    if mode != "status":
        payload: dict[str, object] = {"enabled": mode == "on"}
        if delay is not None:
            payload["delay"] = delay
        http.post("/autoyes/global", payload)
    status = http.get("/autoyes/status")
    block = status.get("global") or {}
    if block.get("enabled"):
        print(f"autoyes --global: on (delay: {_delay_text(block.get('delay'))})")
        armed = sorted(s for s, on in (status.get("sessions") or {}).items() if on)
        print(f"  armed sessions: {', '.join(armed) if armed else '(none live)'}")
    else:
        print("autoyes --global: off")
    return 0


def autoyes(
    session: str | None,
    mode: str,
    delay: float | None = None,
    global_switch: bool = False,
) -> int:
    """Persistently set or inspect auto-yes without accidentally re-toggling it."""
    # Checked before the --global branch so the rule guards both forms.
    if delay is not None and mode != "on":
        print("assist: --delay requires --on", file=sys.stderr)
        raise SystemExit(2)
    if global_switch:
        return _autoyes_global(mode, delay)
    if not session:
        resolve_target(session)

    status = http.get("/autoyes/status")
    global_block = status.get("global") or {}
    global_on = bool(global_block.get("enabled"))
    enabled, current_delay = _autoyes_state(status, session)
    if mode != "status":
        requested = mode == "on"
        if enabled != requested:
            payload: dict[str, object] = {"session": session, "enabled": requested}
            # The global switch owns the delay; sending one would be ignored.
            if requested and delay is not None and not global_on:
                payload["delay"] = delay
            http.post("/autoyes/set", payload)
            status = http.get("/autoyes/status")
            enabled, current_delay = _autoyes_state(status, session)

    _print_autoyes_state(
        session,
        enabled,
        current_delay,
        global_on,
        global_block.get("delay"),
        (status.get("sources") or {}).get(session),
    )
    return 0


def print_resume_hint(target, fresh=False):
    if os.environ.get('ASSIST_NO_HINTS') == '1':
        return
    try:
        response = http.get('/api/resume?' + urlencode({'target': target, 'fresh': int(fresh)}))
    except http.ApiError:
        return  # An optional hint cannot prevent viewing or killing a pane.
    record = response.get('resume')
    if record:
        label = 'resume' if record.get('verified', True) else 'resume (unverified)'
        print(label + ': ' + record['line'], file=sys.stderr, flush=True)


def print_delegate_hint(target):
    if os.environ.get('ASSIST_NO_HINTS') == '1':
        return
    try:
        poll = http.get('/poll')
    except http.ApiError:
        return
    for pane in poll.get('sessions') or []:
        if pane.get('target') == target:
            d = pane.get('delegate') or {}
            if d.get('state'):
                detail = (' — ' + d['detail']) if d.get('detail') else ''
                print(f"delegate: {d['state']} ({d.get('reason', '')}){detail}", file=sys.stderr)
            return


def resume(session, pane='0.0', agent_only=False):
    target = resolve_target(session, pane)
    response = http.get('/api/resume?' + urlencode({'target':target, 'fresh':1}))
    record = response.get('resume')
    if not record:
        print('assist: ' + (response.get('resume_reason') or 'no resume available'), file=sys.stderr)
        return 1
    print(record['command'] if agent_only else record['line'])
    return 0
