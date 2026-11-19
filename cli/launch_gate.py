"""Gated launch verification for `assist launch --role ... --brief FILE [--trust-cwd]`.

Verifies the launched agent's trust dialog, banner (model, effort, permissions
mode, ready composer), and brief delivery + working confirmation without ever
arming Auto-Yes or killing a failed pane.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from collections import Counter
from dataclasses import dataclass

from cli import http, wait as wait_cli
from shared import agent_model
from shared.agent_identity import _ANSI_ESCAPE_RE, _VERSION_CMD_RE


POLL_INTERVAL = wait_cli.POLL_INTERVAL
NO_CHANGE_GRACE_SECONDS = wait_cli.NO_CHANGE_GRACE_SECONDS
BANNER_SETTLE_SECONDS = 6.0
WORK_CONFIRM_GRACE_SECONDS = 10.0

_AGY_SLUG_EFFORT_RE = re.compile(r"^(?P<base>.+)-(?P<effort>low|medium|high|xhigh|max)$", re.IGNORECASE)
_CLAUDE_BYPASS_RE = re.compile(r"\bbypass permissions on\b", re.IGNORECASE)
_CODEX_YOLO_RE = re.compile(r"\bpermissions:\s*YOLO mode\b", re.IGNORECASE)

# Dedicated selected-Yes folder-trust matcher for `--trust-cwd` only.
# Auto-Yes NEVER uses this: folder trust is an explicit launch decision.
_AGY_TRUST_HDR_RE = re.compile(r"Do\s+you\s+trust\s+the\s+contents\s+of\s+this\s+project\?", re.IGNORECASE)
_AGY_TRUST_YES_SELECTED_RE = re.compile(r"^\s*[>❯›]\s*Yes,\s+I\s+trust\s+this\s+folder\b", re.IGNORECASE | re.MULTILINE)
_AGY_TRUST_FOOTER_RE = re.compile(r"Navigate\s*[·•]\s*enter\s+Confirm", re.IGNORECASE)
_AGY_MODAL_LINES = 36

_RULE_RE = re.compile(r"^\s*─{10,}")
_AGY_COMPOSER_RE = re.compile(r"^\s*>(?:\s|$)(?!\s*Yes,\s+I\s+trust\b)", re.IGNORECASE)
_CLAUDE_COMPOSER_RE = re.compile(r"^\s*❯(?:\s|$)(?!\s*(?:\d+[.)]|Yes\b))")
_CODEX_COMPOSER_RE = re.compile(r"^\s*›(?:\s|$)(?!\s*\d+[.)])")

# Stuck unsubmitted paste chips (Claude `[Pasted text ...]` and Codex `[Pasted Content ...]`).
_PASTED_STUCK_RE = re.compile(r"^\s*[>❯›]\s*\[Pasted (?:text|Content)\b", re.IGNORECASE)

# Positive working signals rendered by TUI working chrome (anchored rows, not brief prose).
_WORKING_CHROME_ROW_RE = re.compile(
    r"^\s*esc to (?:cancel|interrupt)(?:\s|$)"
    r"|[·•]\s*esc to (?:interrupt|cancel)\b"
    r"|^\s*[•●○◉✦✶✻✽✢·]\s*(?:Working|Thinking|Running|Thought|Pondering|Brewing|Simmering|Crunching|Explored|Reading|Searching)\b"
    r"|^\s*[✦✶✻✽✢·*]\s+(?-i:[A-Z])[A-Za-z'é-]+(?:…|\.{3})(?:\s*\([^()\n]*\))?\s*$"
    r"|^\s*(?:[•●⏺✻◦✦✶✽✢*]\s+)?(?-i:[A-Z])[A-Za-z'é-]+ed for \d"
    r"|^\s*[⣾⣽⣻⢿⡿⣟⣯⣷⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*\S"
    r"|^\s*[^\w\s]\s*\w+(?:…|\.{3})?\s*\((?=[^()\n]*\besc to (?:interrupt|cancel)\b)[^()\n]*\)\s*$"
    r"|^\W*Working\s*\(\d",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LaunchExpectation:
    transport: str           # "claude" | "codex" | "agy"
    agent_kind: str          # "claude" | "codex" | "gemini"
    model: str | None
    effort: str | None
    has_full_perms_flag: bool
    raw_command: str


@dataclass(frozen=True)
class BannerGateResult:
    ok: bool
    errors: tuple[str, ...]
    observed_model: str | None
    observed_effort: str | None
    permissions_ok: bool


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _strip_env_and_wrappers(argv: list[str]) -> list[str]:
    tokens = list(argv)
    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
        name, _sep, _val = tokens[0].partition("=")
        if not name.replace("_", "").isalnum():
            break
        tokens.pop(0)
    while tokens and tokens[0] in ("command", "exec"):
        tokens.pop(0)
    return tokens


def _detect_transport(tokens: list[str]) -> tuple[str, str, int]:
    """Return (transport, agent_kind, args_start_index)."""
    if not tokens:
        raise ValueError("empty launch command")
    exe = os.path.basename(tokens[0]).lower()
    if exe == "claude" or _VERSION_CMD_RE.fullmatch(exe):
        return "claude", "claude", 1
    if exe == "codex":
        return "codex", "codex", 1
    if exe in ("agy", "antigravity"):
        return "agy", "gemini", 1
    if exe in ("node", "nodejs") and len(tokens) > 1:
        idx = 2 if tokens[1] == "--" and len(tokens) > 2 else 1
        script = os.path.basename(tokens[idx]).lower()
        if script in ("agy", "agy.js"):
            return "agy", "gemini", idx + 1
    if exe == "npx" and len(tokens) > 1:
        for idx in range(1, len(tokens)):
            tok = tokens[idx]
            if tok.startswith("-"):
                continue
            low = tok.lower()
            if low.startswith("@anthropic-ai/claude-code"):
                return "claude", "claude", idx + 1
            if low.startswith("@openai/codex"):
                return "codex", "codex", idx + 1
            break
    raise ValueError(f"unsupported agent launch command for gated launch: {tokens[0]!r}")


def parse_roster_command(command: str) -> LaunchExpectation:
    """Extract expected transport, model, effort, and permissions flag from a roster command."""
    try:
        raw_tokens = shlex.split(command or "")
    except ValueError as exc:
        raise ValueError(f"cannot parse launch command: {exc}") from None
    tokens = _strip_env_and_wrappers(raw_tokens)
    transport, agent_kind, start = _detect_transport(tokens)
    args = tokens[start:]

    model = None
    effort = None
    has_full_perms_flag = False

    i = 0
    while i < len(args):
        arg = args[i]
        key, sep, val = arg.partition("=")
        if transport == "claude":
            if key == "--model":
                model = val if sep else (args[i + 1] if i + 1 < len(args) else None)
                if not sep:
                    i += 1
            elif key == "--effort":
                effort = val if sep else (args[i + 1] if i + 1 < len(args) else None)
                if not sep:
                    i += 1
            elif arg == "--dangerously-skip-permissions":
                has_full_perms_flag = True
            elif key == "--permission-mode":
                mode = val if sep else (args[i + 1] if i + 1 < len(args) else "")
                if not sep:
                    i += 1
                if mode == "bypassPermissions":
                    has_full_perms_flag = True
        elif transport == "codex":
            if key in ("-m", "--model"):
                model = val if sep else (args[i + 1] if i + 1 < len(args) else None)
                if not sep:
                    i += 1
            elif arg.startswith("-m") and len(arg) > 2:
                model = arg[2:]
            elif key in ("-c", "--config"):
                cfg = val if sep else (args[i + 1] if i + 1 < len(args) else "")
                if not sep:
                    i += 1
                ckey, csep, cval = cfg.partition("=")
                if csep and ckey.strip() == "model_reasoning_effort":
                    effort = cval.strip().strip("\"'")
                elif csep and ckey.strip() == "model":
                    model = cval.strip().strip("\"'")
            elif arg in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
                has_full_perms_flag = True
        elif transport == "agy":
            if key == "--model":
                model = val if sep else (args[i + 1] if i + 1 < len(args) else None)
                if not sep:
                    i += 1
            elif key == "--effort":
                effort = val if sep else (args[i + 1] if i + 1 < len(args) else None)
                if not sep:
                    i += 1
            elif arg == "--dangerously-skip-permissions":
                has_full_perms_flag = True
        i += 1

    if transport == "agy" and model:
        slug_match = _AGY_SLUG_EFFORT_RE.match(model)
        if slug_match:
            model = slug_match.group("base")
            if effort is None:
                effort = slug_match.group("effort").lower()

    if effort is not None:
        effort = effort.lower()

    return LaunchExpectation(
        transport=transport,
        agent_kind=agent_kind,
        model=model,
        effort=effort,
        has_full_perms_flag=has_full_perms_flag,
        raw_command=command,
    )


def _has_agy_composer_below(lines: list[str], below: int) -> bool:
    for i in range(below + 1, len(lines)):
        if _AGY_COMPOSER_RE.match(lines[i]):
            above = i - 1 >= 0 and _RULE_RE.match(lines[i - 1])
            under = i + 1 < len(lines) and _RULE_RE.match(lines[i + 1])
            if above and under:
                return True
    return False


def is_agy_trust_dialog_yes_selected(capture: str) -> bool:
    """True only when agy's live folder-trust dialog is on screen with Yes selected."""
    plain = _ANSI_ESCAPE_RE.sub("", capture or "").rstrip("\n")
    lines = plain.split("\n")
    start = max(0, len(lines) - _AGY_MODAL_LINES)
    bottom = "\n".join(lines[start:])
    if not (
        _AGY_TRUST_HDR_RE.search(bottom)
        and _AGY_TRUST_YES_SELECTED_RE.search(bottom)
        and _AGY_TRUST_FOOTER_RE.search(bottom)
    ):
        return False
    footer_idx = None
    for idx in range(len(lines) - 1, start - 1, -1):
        if _AGY_TRUST_FOOTER_RE.search(lines[idx]):
            footer_idx = idx
            break
    return footer_idx is not None and not _has_agy_composer_below(lines, footer_idx)


def _has_ready_composer(transport: str, plain_capture: str) -> bool:
    """True when the agent's idle composer is mounted (never true on a startup modal)."""
    lines = [ln for ln in plain_capture.splitlines() if ln.strip()]
    tail = lines[-20:]
    if transport == "agy":
        for i, ln in enumerate(tail):
            if _AGY_COMPOSER_RE.match(ln):
                above = i - 1 >= 0 and _RULE_RE.match(tail[i - 1])
                under = i + 1 < len(tail) and _RULE_RE.match(tail[i + 1])
                if above and under:
                    return True
        return False
    if transport == "claude":
        for i, ln in enumerate(tail):
            if _CLAUDE_COMPOSER_RE.match(ln):
                above = i - 1 >= 0 and _RULE_RE.match(tail[i - 1])
                under = i + 1 < len(tail) and _RULE_RE.match(tail[i + 1])
                if above and under:
                    return True
        return False
    if transport == "codex":
        return any(_CODEX_COMPOSER_RE.match(ln) for ln in tail)
    return False


def _model_matches(transport: str, expected: str, observed: str) -> bool:
    exp_toks = _tokens(expected)
    obs_toks = _tokens(observed)
    if not exp_toks or not obs_toks:
        return False
    if transport == "claude":
        if exp_toks[0] == "claude":
            exp_toks = exp_toks[1:]
        if obs_toks[0] == "claude":
            obs_toks = obs_toks[1:]
        if len(exp_toks) == 1 and exp_toks[0] in ("opus", "sonnet", "haiku", "fable"):
            return obs_toks[:1] == exp_toks
        return obs_toks == exp_toks
    return obs_toks == exp_toks


def _has_permissions_banner(expected: LaunchExpectation, plain_capture: str) -> tuple[bool, str | None]:
    if expected.transport == "claude":
        if not expected.has_full_perms_flag:
            return (
                False,
                "permissions flag missing (claude must be launched with --dangerously-skip-permissions)",
            )
        if _CLAUDE_BYPASS_RE.search(plain_capture):
            return True, None
        return False, 'permissions mode missing (expected "bypass permissions on" in banner)'
    if expected.transport == "codex":
        if not expected.has_full_perms_flag:
            return (
                False,
                "permissions flag missing (codex must be launched with --yolo)",
            )
        if _CODEX_YOLO_RE.search(plain_capture):
            return True, None
        return False, 'permissions mode missing (expected "permissions: YOLO mode" in banner)'
    if expected.transport == "agy":
        if not expected.has_full_perms_flag:
            return (
                False,
                "permissions mode missing (agy must be launched with --dangerously-skip-permissions)",
            )
        return True, None
    return False, f"unsupported transport {expected.transport!r}"


def evaluate_banner_gate(expected: LaunchExpectation, capture: str) -> BannerGateResult:
    """Verify no blocking modal, ready composer, model, effort, and permissions mode."""
    plain = _ANSI_ESCAPE_RE.sub("", capture or "")
    obs_model, obs_effort = agent_model.resolve_full(expected.agent_kind, plain)
    errors: list[str] = []

    if wait_cli.detect_prompt(plain, expected.agent_kind) is not None or (
        expected.transport == "agy" and is_agy_trust_dialog_yes_selected(plain)
    ):
        errors.append("blocking prompt or startup modal is still on screen")

    if expected.model is not None:
        if obs_model is None:
            errors.append(f"banner model not detected (expected {expected.model})")
        elif not _model_matches(expected.transport, expected.model, obs_model):
            errors.append(f"model mismatch (expected {expected.model}, saw {obs_model})")
    elif obs_model is None:
        errors.append("agent banner not detected")

    if expected.effort is not None:
        if obs_effort is None:
            errors.append(f"banner effort not detected (expected {expected.effort})")
        elif obs_effort.lower() != expected.effort.lower():
            errors.append(f"effort mismatch (expected {expected.effort}, saw {obs_effort})")

    perms_ok, perms_error = _has_permissions_banner(expected, plain)
    if not perms_ok and perms_error:
        errors.append(perms_error)

    if not _has_ready_composer(expected.transport, plain):
        errors.append("idle composer not ready (pane may still be starting or on a modal)")

    return BannerGateResult(
        ok=not errors,
        errors=tuple(errors),
        observed_model=obs_model,
        observed_effort=obs_effort,
        permissions_ok=perms_ok,
    )


def _capture(target: str) -> str:
    return wait_cli._capture_content(target)


def _poll_target(target: str) -> tuple[str | None, float, str | None]:
    poll_response = http.get("/poll")
    states = poll_response.get("states") or {}
    target_state = states.get(target)
    if not isinstance(target_state, dict):
        raise wait_cli.WaitError(f"assist: target not found in /poll: {target}")
    state = target_state.get("state")
    try:
        idle_seconds = float(target_state.get("idle_seconds") or 0)
    except (TypeError, ValueError):
        idle_seconds = 0.0
    agent_kind = wait_cli._agent_kind(poll_response, target)
    return state, idle_seconds, agent_kind


def wait_for_gated_ready(
    target: str,
    expected: LaunchExpectation,
    baseline_hash: str,
    timeout: float,
    trust_cwd: bool = False,
) -> tuple[int, str | None]:
    """Wait after sending the roster command until trust & banner gates pass.

    Returns `(0, None)` on pass, or `(exit_code, error_message)` on any failed
    gate. Never arms Auto-Yes and never kills the pane.
    """
    start = time.monotonic()
    deadline = start + max(0.0, float(timeout))
    no_change_deadline = min(deadline, start + NO_CHANGE_GRACE_SECONDS)
    current_baseline = baseline_hash
    content_changed = False
    trust_accepted = False
    last_seen_hash: str | None = None
    stable_since = start
    stable_polls = 0
    first_poll = True
    last_capture = ""

    while True:
        now = time.monotonic()
        if not first_poll and now >= deadline:
            if not content_changed:
                if trust_accepted:
                    return 10, f"agy folder-trust dialog did not clear after accepting in {target}"
                return 3, f"launch command produced no visible change in {target}"
            gate = evaluate_banner_gate(expected, last_capture)
            detail = "; ".join(gate.errors) if gate.errors else "banner did not settle"
            return 75, f"timed out waiting for ready banner in {target}: {detail}"

        state, _idle_seconds, poll_kind = _poll_target(target)
        capture = _capture(target)
        last_capture = capture
        current_hash = wait_cli._content_hash(capture)

        changed_this_poll = False
        if not content_changed:
            if current_hash != current_baseline:
                content_changed = True
                changed_this_poll = True
                last_seen_hash = current_hash
                stable_since = now
                stable_polls = 0
        else:
            if current_hash != last_seen_hash:
                changed_this_poll = True
                last_seen_hash = current_hash
                stable_since = now
                stable_polls = 0
            else:
                stable_polls += 1

        if content_changed and not changed_this_poll:
            effective_kind = (
                poll_kind
                if poll_kind in ("claude", "codex", "gemini")
                else expected.agent_kind
            )
            prompt = wait_cli.detect_prompt(capture, effective_kind)
            if prompt is not None or (
                expected.transport == "agy" and is_agy_trust_dialog_yes_selected(capture)
            ):
                if expected.transport == "agy" and is_agy_trust_dialog_yes_selected(capture):
                    if not trust_cwd:
                        return (
                            10,
                            f"agy folder-trust dialog waiting in {target}; "
                            "re-run with --trust-cwd or answer it in the pane",
                        )
                    if trust_accepted:
                        return (
                            10,
                            f"agy folder-trust dialog still present after accepting in {target}",
                        )
                    current_baseline = current_hash
                    http.post(
                        "/type",
                        {
                            "target": target,
                            "text": "",
                            "enter": True,
                            "no_history": True,
                            "raw": True,
                        },
                    )
                    trust_accepted = True
                    content_changed = False
                    last_seen_hash = None
                    stable_polls = 0
                    no_change_deadline = min(deadline, time.monotonic() + NO_CHANGE_GRACE_SECONDS)
                    first_poll = True
                    continue
                prompt_type, summary = prompt if prompt is not None else ("prompt", "waiting")
                return (
                    10,
                    f"blocking prompt in {target} ({prompt_type}: {summary}); "
                    "pane left for inspection",
                )

            gate = evaluate_banner_gate(expected, capture)
            # Require at least one unchanged poll (`stable_polls >= 1`) so a
            # modal drawn immediately after the banner is never raced, and never
            # fail early on a transient Poll-1 `state == "shell"` before fork.
            if gate.ok and stable_polls >= 1:
                return 0, None
            if not gate.ok and stable_polls >= 1 and (
                state == "idle" or (now - stable_since) >= BANNER_SETTLE_SECONDS
            ):
                return 1, f"banner gate failed in {target}: " + "; ".join(gate.errors)

        if not content_changed:
            now = time.monotonic()
            if (not first_poll and now >= no_change_deadline) or now >= deadline:
                if trust_accepted:
                    return 10, f"agy folder-trust dialog did not clear after accepting in {target}"
                return 3, f"launch command produced no visible change in {target}"

        first_poll = False
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0:
            if not content_changed:
                if trust_accepted:
                    return 10, f"agy folder-trust dialog did not clear after accepting in {target}"
                return 3, f"launch command produced no visible change in {target}"
            gate = evaluate_banner_gate(expected, last_capture)
            detail = "; ".join(gate.errors) if gate.errors else "banner did not settle"
            return 75, f"timed out waiting for ready banner in {target}: {detail}"

        sleep_for = min(POLL_INTERVAL, remaining)
        if not content_changed:
            grace_remaining = no_change_deadline - now
            if grace_remaining > 0:
                sleep_for = min(sleep_for, grace_remaining)
        time.sleep(sleep_for)


def _working_rows(capture: str) -> Counter[str]:
    plain = _ANSI_ESCAPE_RE.sub("", capture or "")
    lines = [ln.rstrip() for ln in plain.splitlines() if ln.strip()]
    # Only scan the recent visible tail so old scrollback never counts.
    return Counter(ln for ln in lines[-30:] if _WORKING_CHROME_ROW_RE.search(ln))


def _paste_stuck_in_composer(capture: str) -> bool:
    plain = _ANSI_ESCAPE_RE.sub("", capture or "")
    lines = [ln for ln in plain.splitlines() if ln.strip()]
    tail = lines[-12:]
    stuck_idx = None
    for idx, ln in enumerate(tail):
        if _PASTED_STUCK_RE.match(ln):
            stuck_idx = idx
    if stuck_idx is None:
        return False
    below = tail[stuck_idx + 1 :]
    return not any(_WORKING_CHROME_ROW_RE.search(ln) for ln in below)


def _has_working_signal(capture: str, baseline_capture: str) -> bool:
    """True only when capture shows a new TUI working signal and no stuck paste."""
    if _paste_stuck_in_composer(capture):
        return False
    before = _working_rows(baseline_capture)
    after = _working_rows(capture)
    return any(count > before[row] for row, count in after.items())


def deliver_brief_and_confirm_working(
    target: str,
    expected: LaunchExpectation,
    brief_text: str,
    timeout: float,
) -> tuple[int, int, str | None]:
    """Paste the brief as one message and confirm a real working signal appears.

    Returns `(exit_code, sent_chars, error_message)`.
    """
    baseline_capture = _capture(target)
    baseline_hash = wait_cli._content_hash(baseline_capture)
    response = http.post(
        "/type",
        {
            "target": target,
            "text": brief_text,
            "enter": True,
            "no_history": True,
            "raw": True,
        },
    )
    sent_chars = response.get("sent_chars")
    if sent_chars is None:
        sent_chars = len(brief_text)

    start = time.monotonic()
    confirm_deadline = start + min(max(0.0, float(timeout)), WORK_CONFIRM_GRACE_SECONDS)
    stuck_polls = 0
    last_stuck_hash: str | None = None
    saw_change = False

    while True:
        now = time.monotonic()
        state, _idle_seconds, poll_kind = _poll_target(target)
        capture = _capture(target)
        current_hash = wait_cli._content_hash(capture)

        if current_hash != baseline_hash:
            saw_change = True
            effective_kind = (
                poll_kind
                if poll_kind in ("claude", "codex", "gemini")
                else expected.agent_kind
            )
            prompt = wait_cli.detect_prompt(capture, effective_kind)
            if prompt is not None:
                return (
                    10,
                    int(sent_chars),
                    f"blocking prompt after brief in {target} ({prompt[0]}: {prompt[1]})",
                )
            if state == "shell" and poll_kind == "shell":
                return 1, int(sent_chars), f"agent exited to shell after brief in {target}"
            if _has_working_signal(capture, baseline_capture):
                return 0, int(sent_chars), None
            if _paste_stuck_in_composer(capture):
                if current_hash == last_stuck_hash:
                    stuck_polls += 1
                else:
                    last_stuck_hash = current_hash
                    stuck_polls = 1
                if stuck_polls >= 3 or now >= confirm_deadline:
                    return (
                        1,
                        int(sent_chars),
                        f"brief pasted into composer in {target} but agent did not start working",
                    )

        if now >= confirm_deadline:
            if last_stuck_hash is not None:
                return (
                    1,
                    int(sent_chars),
                    f"brief pasted into composer in {target} but agent did not start working",
                )
            if saw_change:
                return (
                    3,
                    int(sent_chars),
                    f"brief was sent to {target} but agent did not start working",
                )
            return (
                3,
                int(sent_chars),
                f"brief was sent to {target} but pane did not change",
            )

        remaining = confirm_deadline - time.monotonic()
        if remaining <= 0:
            if last_stuck_hash is not None:
                return (
                    1,
                    int(sent_chars),
                    f"brief pasted into composer in {target} but agent did not start working",
                )
            if saw_change:
                return (
                    3,
                    int(sent_chars),
                    f"brief was sent to {target} but agent did not start working",
                )
            return 3, int(sent_chars), f"brief was sent to {target} but pane did not change"
        time.sleep(min(POLL_INTERVAL, remaining))
