"""Per-pane model + reasoning-effort parsing, keyed off agent_kind.

`resolve()` is pure — (agent_kind, tail) -> (model, effort) — so it can be
checked against captured footers without touching state. `observe()` wraps it
with the memo and the wall-clock-gated confirmation (see
`_MIN_CONFIRM_SECONDS` below).

The text exists only because a CLI prints it in its status line: Claude Code's
comes from a custom statusline script, Codex's from `status_line` in
~/.codex/config.toml. A stock install may legitimately show nothing. See the
"Known limitation" section of Studio spec 1981.
"""

import time
import re

# Deliberate cross-module use of two private patterns rather than a second copy
# of each: routes/poll.py already imports _VERSION_CMD_RE the same way.
from shared.agent_identity import _ANSI_ESCAPE_RE, _CODEX_FINGERPRINT_RE
import shared.state as state

# Codex's six real reasoning levels.
_CODEX_RE = re.compile(
    r"(?P<model>gpt-\d+(?:\.\d+)*-[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?:\s*(?:[·•]\s*)?(?P<effort>minimal|low|medium|high|xhigh|ultra)\b)?",
    re.IGNORECASE,
)
_CODEX_BANNER_MODEL_RE = re.compile(
    r"\bmodel:\s*(?P<model>gpt-\d+(?:\.\d+)*-[a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?:\s+(?P<effort>minimal|low|medium|high|xhigh|ultra)\b)?",
    re.IGNORECASE,
)
_CODEX_VERSION_PREFIX_RE = re.compile(r"^gpt-\d+(?:\.\d+)*-", re.IGNORECASE)
_CODEX_FAMILY = {"sol", "luna", "terra"}

_CLAUDE_RE = re.compile(r"\b(Opus|Sonnet|Haiku|Fable)\s+(\d+(?:\.\d+)?)\b")
_CLAUDE_ID_RE = re.compile(r"\bclaude-(opus|sonnet|haiku|fable)-(\d+(?:[.-]\d+)?)\b")
_CLAUDE_EFFORT_RE = re.compile(
    r"\bwith\s+(?P<e1>low|medium|high|xhigh|max)\s+effort\b"
    r"|[●◐○◉]\s*(?P<e2>low|medium|high|xhigh|max)\s*[·•]\s*/effort\b"
    r"|\beffort:\s*(?P<e3>low|medium|high|xhigh|max)\b",
    re.IGNORECASE,
)

# Antigravity (agy) renders model + effort in two places:
#   startup banner:  "Gemini 4 Neon (High)"
#   status footer:   "Gemini 4 Neon · high"
# Neither matches a raw shell command echo (`agy --model gemini-4-neon-high`).
_AGY_MODEL_NAME = (
    r"(?:Gemini\s+\d+(?:\.\d+)?\s+[A-Za-z0-9-]+"
    r"|Claude\s+(?:Opus|Sonnet|Haiku|Fable)\s+\d+(?:\.\d+)?"
    r"|GPT-OSS\s+[A-Za-z0-9-]+)"
)
_AGY_FOOTER_RE = re.compile(
    rf"\b(?P<model>{_AGY_MODEL_NAME})\s*[·•]\s*(?P<effort>low|medium|high|xhigh|max)\b",
    re.IGNORECASE,
)
_AGY_BANNER_RE = re.compile(
    rf"\b(?P<model>{_AGY_MODEL_NAME})\s*\((?P<effort>low|medium|high|xhigh|max)\)",
    re.IGNORECASE,
)

# Permission-mode patterns (matched strictly on live footer chrome, never prose or shell echoes).
_CLAUDE_RULE_RE = re.compile(r"^\s*─{10,}\s*$")
_CLAUDE_COMPOSER_LINE_RE = re.compile(r"^\s*❯(?:\s|$)(?!\s*(?:\d+[.)]|Yes\b))")
_CLAUDE_FOOTER_CHROME_RE = re.compile(r"[⏵⏸]|\(shift\+tab to cycle\)", re.IGNORECASE)
_CLAUDE_PERM_BYPASS_RE = re.compile(r"\bbypass permissions on\b", re.IGNORECASE)
_CLAUDE_PERM_ACCEPT_RE = re.compile(r"\b(?:auto-)?accept edits on\b", re.IGNORECASE)
_CLAUDE_PERM_PLAN_RE = re.compile(r"\bplan mode on\b", re.IGNORECASE)
_CLAUDE_PERM_DONTASK_RE = re.compile(r"\b(?:don't ask on|dontAsk)\b", re.IGNORECASE)

_CODEX_PERM_RE = re.compile(
    r"\bpermissions:\s*(?P<mode>YOLO mode|full[- ]auto|read[- ]only|workspace[- ]write|on[- ]request|default)\b",
    re.IGNORECASE,
)

# Context-usage patterns (percent USED, 0..100).
# Claude custom statusline: "░░░░░░░░░░ 42.5% (340k/800k) │ 12m"
_CLAUDE_CTX_BAR_RE = re.compile(
    r"(?:[░▒▓█]+\s*)?(?P<pct>\d+(?:\.\d+)?)%\s*\(\s*\d+(?:\.\d+)?[kKmM]?\s*/\s*\d+(?:\.\d+)?[kKmM]?\s*\)"
)
# Claude built-in auto-compact countdown: "Context left until auto-compact: 15%"
_CLAUDE_CTX_LEFT_RE = re.compile(
    r"\bcontext\s+left(?:\s+until\s+auto-compact)?:\s*(?P<pct>\d+(?:\.\d+)?)%",
    re.IGNORECASE,
)
# Codex status line: "· Context 97% left" or "· Context 45% used"
_CODEX_CTX_RE = re.compile(
    r"\bcontext\s+(?P<pct>\d+(?:\.\d+)?)%\s*(?P<dir>left|used)\b",
    re.IGNORECASE,
)

CLAUDE_CONTEXT_WARN_PCT = 80.0


def _footer_lines(tail, count):
    """Last `count` non-empty lines of a capture, ANSI stripped.

    Same region and same ANSI regex agent_identity.refine_with_content() uses.
    """
    normalized = _ANSI_ESCAPE_RE.sub("", tail or "")
    nonempty = [line for line in normalized.splitlines() if line.strip()]
    return nonempty[-count:]


def _short_codex_model(model):
    """gpt-5.6-sol -> sol. gpt-5.3-codex -> 5.3-codex (unfamiliar, still readable)."""
    lowered = model.lower()
    rest = _CODEX_VERSION_PREFIX_RE.sub("", lowered)
    if rest in _CODEX_FAMILY:
        return rest
    return lowered[len("gpt-") :] if lowered.startswith("gpt-") else lowered


def _parse_codex_full(tail):
    region = _footer_lines(tail, 5)
    # Anchor to the status line — the one carrying "· Context NN% left/used".
    # Without this, a prompt that merely NAMES a model ("switch to
    # gpt-5.3-codex") outranks the real status line below it, and the
    # time-based confirmation (_MIN_CONFIRM_SECONDS) cannot catch it because
    # prose persists across polls just as long as the real status line does.
    anchored_indices = [
        idx for idx, line in enumerate(region) if _CODEX_FINGERPRINT_RE.search(line)
    ]
    for idx in reversed(anchored_indices):
        match = _CODEX_RE.search(region[idx])
        if match and match.group("effort"):
            return match.group("model").lower(), match.group("effort").lower()
        # A multi-segment Codex status_line can wrap between `· GPT-6.1-Sol ·`
        # and `medium · Context 100% left`. Join contiguous status-bar lines
        # ending at the Context row (stopping before any composer prompt `›`).
        join_start = idx
        while join_start > 0 and idx - join_start < 3:
            prev = region[join_start - 1].lstrip()
            if prev.startswith(("›", "❯", ">", "╭", "╰", "│", "─")):
                break
            join_start -= 1
        joined_line = " ".join(ln.strip() for ln in region[join_start : idx + 1])
        jmatch = _CODEX_RE.search(joined_line)
        if jmatch:
            effort = jmatch.group("effort")
            return jmatch.group("model").lower(), effort.lower() if effort else None
        if match:
            return match.group("model").lower(), None
    # Startup banner box (`model: gpt-6.1-sol medium`) when the footer has not
    # rendered or scrolled into view yet.
    for line in reversed(_footer_lines(tail, 20)):
        match = _CODEX_BANNER_MODEL_RE.search(line)
        if match:
            effort = match.group("effort")
            return match.group("model").lower(), effort.lower() if effort else None
    fallback = None
    for line in reversed(region):
        # Skip echoed launch commands (`codex -m gpt-...`) so an unstarted shell
        # command never masquerades as a live Codex status line.
        if re.search(r"\b(?:codex)\b.*(?:-m|--model)\b", line):
            continue
        match = _CODEX_RE.search(line)
        if match:
            model = match.group("model").lower()
            effort = match.group("effort")
            if effort:
                return model, effort.lower()
            if fallback is None:
                fallback = (model, None)
    return fallback if fallback is not None else (None, None)


def _parse_codex(tail):
    model, effort = _parse_codex_full(tail)
    if model is None:
        return None, None
    return _short_codex_model(model), effort


def _claude_effort(lines):
    for line in reversed(lines):
        match = _CLAUDE_EFFORT_RE.search(line)
        if match:
            effort = match.group("e1") or match.group("e2") or match.group("e3")
            return effort.lower()
    return None


def _parse_claude(tail):
    # Bottom-up: the status line is the last thing drawn, so a lower match
    # beats a higher one. Fall back to the startup banner (`Opus 5.5 with high
    # effort · Claude Max`) in the last 20 non-empty lines when the custom
    # status line is absent.
    footer = _footer_lines(tail, 3)
    banner_region = _footer_lines(tail, 20)
    effort = _claude_effort(footer) or _claude_effort(banner_region)
    for region in (footer, banner_region):
        for line in reversed(region):
            if re.search(r"\bclaude\b.*--model\b", line):
                continue
            match = _CLAUDE_RE.search(line)
            if match:
                return f"{match.group(1).lower()} {match.group(2)}", effort
            match = _CLAUDE_ID_RE.search(line)
            if match:
                return f"{match.group(1)} {match.group(2)}", effort
    return None, None


def _parse_gemini(tail):
    """Parse Antigravity (agy) model + effort from status footer or banner."""
    # Bottom-up footer first: always reflects mid-session model/effort switches.
    for line in reversed(_footer_lines(tail, 5)):
        match = _AGY_FOOTER_RE.search(line)
        if match:
            model = " ".join(match.group("model").split()).lower()
            return model, match.group("effort").lower()
    # Startup banner ("Gemini 4 Neon (High)") sits above the composer box.
    for line in reversed(_footer_lines(tail, 20)):
        match = _AGY_BANNER_RE.search(line)
        if match:
            model = " ".join(match.group("model").split()).lower()
            return model, match.group("effort").lower()
    return None, None


# One entry per agent_kind that prints a model.
_PARSERS = {
    "codex": _parse_codex,
    "claude": _parse_claude,
    "gemini": _parse_gemini,
}

_FULL_PARSERS = {
    "codex": _parse_codex_full,
    "claude": _parse_claude,
    "gemini": _parse_gemini,
}


def resolve(agent_kind, tail):
    """(model, effort) for a pane, or (None, None) when not recognised."""
    parser = _PARSERS.get(agent_kind)
    if parser is None:
        return None, None
    return parser(tail)


def resolve_full(agent_kind, tail):
    """Full un-shortened (model, effort) for exact banner verification."""
    parser = _FULL_PARSERS.get(agent_kind)
    if parser is None:
        return None, None
    return parser(tail)


def _perms_from_argv(agent_kind, argv):
    """Infer permission mode from process argv when banner/footer is absent."""
    if not argv:
        return None
    tokens = [str(tok) for tok in argv]
    if agent_kind == "claude":
        for idx, tok in enumerate(tokens):
            if tok == "--dangerously-skip-permissions":
                return "bypass"
            if tok == "--permission-mode" and idx + 1 < len(tokens):
                mode = tokens[idx + 1]
                return _normalize_claude_perm_arg(mode)
            if tok.startswith("--permission-mode="):
                return _normalize_claude_perm_arg(tok.split("=", 1)[1])
        return "default"
    if agent_kind == "codex":
        for idx, tok in enumerate(tokens):
            if tok in ("--yolo", "--dangerously-bypass-approvals-and-sandbox"):
                return "yolo"
            if tok == "--full-auto":
                return "full-auto"
            if tok in ("-s", "--sandbox") and idx + 1 < len(tokens):
                if tokens[idx + 1] == "read-only":
                    return "read-only"
                if tokens[idx + 1] == "danger-full-access":
                    return "yolo"
            if tok.startswith("--sandbox="):
                val = tok.split("=", 1)[1]
                if val == "read-only":
                    return "read-only"
                if val == "danger-full-access":
                    return "yolo"
        return "default"
    if agent_kind == "gemini":
        for tok in tokens:
            if tok == "--dangerously-skip-permissions":
                return "bypass"
            if tok in ("--yolo", "-y"):
                return "yolo"
        return "default"
    return None


def _normalize_claude_perm_arg(mode):
    low = (mode or "").strip().lower()
    if low == "bypasspermissions":
        return "bypass"
    if low == "acceptedits":
        return "accept-edits"
    if low == "plan":
        return "plan"
    if low == "dontask":
        return "dont-ask"
    return low or "default"


def _claude_perm_from_line(line):
    if re.search(r"\bclaude\b.*(?:--dangerously-skip-permissions|--permission-mode)\b", line):
        return None
    if _CLAUDE_PERM_BYPASS_RE.search(line):
        return "bypass"
    if _CLAUDE_PERM_ACCEPT_RE.search(line):
        return "accept-edits"
    if _CLAUDE_PERM_PLAN_RE.search(line):
        return "plan"
    if _CLAUDE_PERM_DONTASK_RE.search(line):
        return "dont-ask"
    return None


def resolve_permissions(agent_kind, tail, argv=None):
    """Parse active permission mode from live footer/banner chrome, with process argv fallback."""
    if agent_kind == "claude":
        recent = _footer_lines(tail, 12)
        footer_below = None
        for idx in range(len(recent) - 1, 0, -1):
            if not _CLAUDE_COMPOSER_LINE_RE.match(recent[idx]):
                continue
            if not _CLAUDE_RULE_RE.match(recent[idx - 1]):
                continue
            close_idx = None
            for cand in range(idx + 1, min(len(recent), idx + 5)):
                if _CLAUDE_RULE_RE.match(recent[cand]):
                    close_idx = cand
                    break
            if close_idx is not None:
                footer_below = recent[close_idx + 1 :]
                break
        if footer_below is not None:
            # Restrict strictly to lines below the framed composer's closing rule so
            # conversation prose, prompt echoes, and permission menus never spoof mode.
            for line in reversed(footer_below):
                found = _claude_perm_from_line(line)
                if found is not None:
                    return found
            # A live status footer below the framed composer with no mode badge means
            # default permissions (including after Shift-Tab out of bypass).
            if footer_below:
                return "default"
            return _perms_from_argv(agent_kind, argv)
        # No framed composer in view (e.g. working screen or permission menu):
        # accept only anchored Claude footer chrome in the bottom 4 lines, else argv.
        for line in reversed(_footer_lines(tail, 4)):
            if not _CLAUDE_FOOTER_CHROME_RE.search(line):
                continue
            found = _claude_perm_from_line(line)
            if found is not None:
                return found
        return _perms_from_argv(agent_kind, argv)
    if agent_kind == "codex":
        for line in reversed(_footer_lines(tail, 25)):
            if re.search(r"\bcodex\b.*(?:--yolo|--full-auto|--sandbox)\b", line):
                continue
            match = _CODEX_PERM_RE.search(line)
            if match:
                raw = match.group("mode").strip().lower()
                if "yolo" in raw:
                    return "yolo"
                return raw.replace(" ", "-")
        return _perms_from_argv(agent_kind, argv)
    if agent_kind == "gemini":
        # agy never prints a permission banner in TUI chrome; read strictly from argv.
        return _perms_from_argv(agent_kind, argv)
    return None


def _clamp_pct(value):
    return round(max(0.0, min(100.0, float(value))), 1)


def resolve_context_pct(agent_kind, tail):
    """Parse percent context USED (0.0..100.0) when shown by the agent, else None."""
    lines = _footer_lines(tail, 8)
    if agent_kind == "claude":
        for line in reversed(lines):
            match = _CLAUDE_CTX_BAR_RE.search(line)
            if match:
                return _clamp_pct(match.group("pct"))
            match = _CLAUDE_CTX_LEFT_RE.search(line)
            if match:
                return _clamp_pct(100.0 - float(match.group("pct")))
        return None
    if agent_kind == "codex":
        for line in reversed(lines):
            match = _CODEX_CTX_RE.search(line)
            if match:
                val = float(match.group("pct"))
                if match.group("dir").lower() == "left":
                    return _clamp_pct(100.0 - val)
                return _clamp_pct(val)
        return None
    if agent_kind == "gemini":
        for line in reversed(lines):
            match = _CODEX_CTX_RE.search(line)
            if match:
                val = float(match.group("pct"))
                if match.group("dir").lower() == "left":
                    return _clamp_pct(100.0 - val)
                return _clamp_pct(val)
        return None
    return None


def context_warning(agent_kind, context_pct):
    """True when Claude context usage exceeds the warning threshold (>80%)."""
    return (
        agent_kind == "claude"
        and context_pct is not None
        and float(context_pct) > CLAUDE_CONTEXT_WARN_PCT
    )


_MIN_CONFIRM_SECONDS = 4.0  # candidate must hold this long, not just "two calls in a row"


def observe(target, agent_kind, pane_id, tail):
    """Confirm and memoise a pane's model. Returns (model, effort, changed_at).

    A parse is published only once two agreeing polls also span at least
    `_MIN_CONFIRM_SECONDS` of wall-clock time. Call-adjacency alone is not
    enough: every open browser polls independently (phone + desktop is the
    normal case here), so two different clients can confirm the same
    one-frame flap milliseconds apart and defeat the damping this exists for.
    A miss never blanks an established value: a full-screen dialog or a
    scrolled-back pane hides the footer, and the last confirmed value stands.
    """
    parsed = resolve(agent_kind, tail)
    now = time.time()

    with state._activity_lock:
        entry = state.pane_model.get(target)

        # The pane's identity changed under us: Claude exited to a shell (or
        # a different CLI took over — `kind` differs), or tmux recycled this
        # target onto a different pane before a poll observed the gap
        # (`pane_id` differs even though `kind` is unchanged). Either way the
        # old model is not "temporarily hidden", it is gone — the never-blank
        # rule below must not display it forever or hand it to a new pane.
        if entry is not None and (
            entry.get("kind") != agent_kind or entry.get("pane_id") != pane_id
        ):
            entry = None

        if entry is None:
            entry = {
                "model": None,
                "effort": None,
                "changed_at": 0.0,
                "candidate": None,
                "candidate_since": 0.0,
                "kind": agent_kind,
                "pane_id": pane_id,
                "permission_mode": None,
                "context_pct": None,
            }
            state.pane_model[target] = entry

        if parsed == (None, None):
            # Drop the pending candidate only; the published value stands.
            entry["candidate"] = None
        elif entry["candidate"] == parsed:
            # Same candidate as last time — but agreement alone no longer
            # publishes. candidate_since was stamped below the FIRST time
            # this candidate appeared and is deliberately left untouched
            # while it keeps agreeing; only elapsed time against that
            # original stamp can satisfy the gate.
            if now - entry["candidate_since"] >= _MIN_CONFIRM_SECONDS:
                published = (entry["model"], entry["effort"])
                if published != parsed:
                    # A pane's first value is not a change from anything.
                    # Stamping it would caret every tab after an `assist
                    # restart`.
                    entry["changed_at"] = 0.0 if published == (None, None) else now
                    entry["model"], entry["effort"] = parsed
        else:
            entry["candidate"] = parsed
            entry["candidate_since"] = now

        return entry["model"], entry["effort"], entry["changed_at"]


def observe_details(target, agent_kind, pane_id, tail, argv=None):
    """Observe model/effort (damped) plus permission_mode and context_pct (immediate)."""
    model, effort, changed_at = observe(target, agent_kind, pane_id, tail)
    perm = resolve_permissions(agent_kind, tail, argv=argv)
    ctx = resolve_context_pct(agent_kind, tail)
    with state._activity_lock:
        entry = state.pane_model.get(target)
        if entry is not None and entry.get("kind") == agent_kind and entry.get("pane_id") == pane_id:
            if perm is not None:
                entry["permission_mode"] = perm
            if ctx is not None:
                entry["context_pct"] = ctx
            published_perm = entry.get("permission_mode")
            published_ctx = entry.get("context_pct")
        else:
            published_perm = perm
            published_ctx = ctx
    return {
        "model": model,
        "effort": effort,
        "changed_at": changed_at,
        "permission_mode": published_perm,
        "context_pct": published_ctx,
    }
