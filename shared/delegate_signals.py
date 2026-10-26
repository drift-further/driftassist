"""Screen-derived fields of a delegate sample (spec 7185 §4.2, §4.7).

The sample adapter (routes/delegates.py) reads these from a pane's tail and
title and hands them to shared/delegate_state.py:classify as data, so the
classifier itself holds no screen pattern. Every pattern here looks only at
the bottom rows, where an agent draws its live chrome.
"""

import re

from shared.agent_identity import _ANSI_ESCAPE_RE

CHROME_ROWS = 12
REPORT_ROWS = 20
REPORT_MAX = 120

_WORKING_RE = re.compile(r"esc to interrupt|^\W*Working \(")
_CODEX_TURN_DONE_RE = re.compile(r"^[^A-Za-z0-9]*Worked for \d")
_CLAUDE_TURN_DONE_RE = re.compile(r"^\s*\S\s+\w+ed for \d+[smh]")
_STALL_RE = re.compile(r"Reconnecting\.\.\. (\d+)/(\d+)")
_REPORT_RE = re.compile(
    r"^\s*(?:[•●⏺✻◦]\s+)?(?:<[^>\n]{1,40}>\s+|[A-Z][A-Z0-9_-]{1,30}\s+)?(DONE|BLOCKED)\s+—\s+\S"
)


def _bottom(tail, rows):
    lines = [ln.rstrip() for ln in _ANSI_ESCAPE_RE.sub("", tail or "").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines[-rows:]


def chrome(tail, title):
    """{working, turn_done, title_busy} from the bottom rows and the pane title."""
    rows = _bottom(tail, CHROME_ROWS)
    return {
        "working": any(_WORKING_RE.search(ln) for ln in rows),
        "turn_done": any(_CODEX_TURN_DONE_RE.search(ln) or _CLAUDE_TURN_DONE_RE.search(ln) for ln in rows),
        "title_busy": bool(title) and "⠀" <= title[0] <= "⣿",
    }


def report_line(tail):
    """The last standalone DONE/BLOCKED report row in the bottom rows, trimmed; None if none."""
    for ln in reversed(_bottom(tail, REPORT_ROWS)):
        if _REPORT_RE.search(ln):
            return ln.strip()[:REPORT_MAX]
    return None


def stall_counter(tail):
    """codex's `Reconnecting... N/M` counter in the bottom rows, as "N/M"; None if absent."""
    for ln in reversed(_bottom(tail, CHROME_ROWS)):
        m = _STALL_RE.search(ln)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None
