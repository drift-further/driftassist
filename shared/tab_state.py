"""shared/tab_state.py — Server-side tab order, pinning, and snoozing.

These three used to live in browser localStorage, so a phone and a desktop
disagreed about which tabs appeared where and which were tucked into the zZ
sheet. They are server state now; the browser is a renderer.

Keying is deliberately mixed: **order and pin are per session, snooze is per
target**. Pinning a single target lets a team lead float away from its agent
panes, which breaks the `is_subpane` / team-lead adjacency the UI derives from
pane order. Snooze stays per pane so one agent pane can be tucked away on its
own.

Since effort 621 Phase 7 step B a snooze is keyed by the pane's durable key
(shared/tmux.py:PaneRef.key), converted once from the legacy `session:w.p`
exactly like drafts (shared/drafts.py:convert_entries; unmatched or colliding
entries kept as orphans that never snooze a later pane, pruned after 30 days).
The converted doc lives in `tab_state.v2.json`; `tab_state.json` is only read,
never written (sign-off M3 F5), so a code rollback finds it as it left it. On
a later start the v2 doc is kept as it is: pin, order and snoozes an older
Assist changed meanwhile are not imported (a snooze or a tab order carries no
text to lose, and an old-window snooze is keyed by a display name that may now
be another pane, so ignoring it is the one choice that cannot attach it to a
stranger). The
browser doc still shows snoozes by display target, projected from the live
list, so an old and a new browser read the same doc. Pin and order are by
session and unchanged. The activity map (state.pane_last_activity) stays keyed
by display target; `_is_busy` translates a key through the live list.
"""

import copy
import json
import logging
import subprocess
import threading
import time

from shared import state

log = logging.getLogger(__name__)


def _tmux():
    # Imported on use: shared.tmux imports shared.agent_identity, which imports
    # this module, so a top-level import here is circular.
    from shared import tmux
    return tmux

TAB_STATE_FILE = state.DATA_DIR / "tab_state.json"        # pre-7B format: read, never written
TAB_STATE_V2_FILE = state.DATA_DIR / "tab_state.v2.json"  # the only file written

# A pane counts as busy while its last content change is newer than this — the
# same 30s boundary routes/poll.py uses to call a pane idle.
QUIET_SEC = 30

# A snooze always sticks for at least this long. Without it, snoozing a pane
# that is a second away from crossing the busy/quiet line would wake it again
# on the very next poll.
MIN_SNOOZE_SEC = 30

# How the strip is ordered. "manual" honours the explicit order list (what
# dragging a tab writes); the other two are computed fresh every poll, so a new
# session lands in the right place instead of at the end.
SORT_MODES = ("manual", "name", "created")

SCHEMA = 2
# An orphaned snooze (a pre-upgrade name that matched no live pane) is kept
# this long after it was orphaned, the same window as an orphaned draft.
ORPHAN_EXPIRY_SEC = 30 * 24 * 60 * 60

# Patched by tests; the live pane list the converter and lookups read.
def _list_panes():
    return _tmux().list_panes()


# True once snoozes are keyed canonically (schema 2).
_converted = True

_lock = threading.Lock()
_tab_state = {
    "pinned": [],
    "order": [],
    "snoozed": {},
    "sort": "manual",
    "agent_declarations": {},
}


def _dedup(values):
    """Strings only, first occurrence wins, order preserved.

    Anything that isn't a list becomes []. A bare string would otherwise
    iterate character by character and turn a hand-edited tab_state.json into
    a list of letters.
    """
    if not isinstance(values, (list, tuple)):
        return []
    seen = set()
    out = []
    for v in values:
        if isinstance(v, str) and v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _normalize(data):
    """Coerce a loaded or patched doc into the canonical shape."""
    if not isinstance(data, dict):
        data = {}
    snoozed = {}
    raw = data.get("snoozed")
    if isinstance(raw, dict):
        for target, meta in raw.items():
            if not isinstance(target, str) or not target or not isinstance(meta, dict):
                continue
            try:
                at = float(meta.get("at", 0.0))
            except (TypeError, ValueError):
                at = 0.0
            snoozed[target] = {"at": at, "was_busy": bool(meta.get("was_busy"))}
            if isinstance(meta.get("legacy_from"), str) and meta["legacy_from"]:
                snoozed[target]["legacy_from"] = meta["legacy_from"]
            if meta.get("orphan") is True:
                snoozed[target]["orphan"] = True
                try:
                    snoozed[target]["orphaned_at"] = float(meta.get("orphaned_at") or 0.0)
                except (TypeError, ValueError):
                    snoozed[target]["orphaned_at"] = 0.0
    sort = data.get("sort")
    declarations = {}
    raw_declarations = data.get("agent_declarations")
    if isinstance(raw_declarations, dict):
        for target, declaration in raw_declarations.items():
            if (
                not isinstance(target, str)
                or not target
                or not isinstance(declaration, dict)
            ):
                continue
            try:
                pane_pid = int(declaration.get("pane_pid"))
            except (TypeError, ValueError):
                continue
            proc_start_time = str(declaration.get("proc_start_time") or "")
            kind = declaration.get("kind")
            try:
                declared_at = float(declaration.get("declared_at"))
            except (TypeError, ValueError):
                continue
            if (
                pane_pid > 0
                and proc_start_time
                and isinstance(kind, str)
                and kind
                and declared_at > 0
            ):
                normalized = {
                    "pane_pid": pane_pid,
                    "proc_start_time": proc_start_time,
                    "kind": kind,
                    "declared_at": declared_at,
                }
                try:
                    agent_pid = int(declaration.get("agent_pid"))
                except (TypeError, ValueError):
                    agent_pid = 0
                agent_start_time = str(declaration.get("agent_start_time") or "")
                if agent_pid > 0 and agent_start_time:
                    normalized["agent_pid"] = agent_pid
                    normalized["agent_start_time"] = agent_start_time
                declarations[target] = normalized
    return {
        "pinned": _dedup(data.get("pinned")),
        "order": _dedup(data.get("order")),
        "snoozed": snoozed,
        "sort": sort if sort in SORT_MODES else "manual",
        "agent_declarations": declarations,
    }


def load_tab_state():
    """Load from disk. Called at import and by tests (an Assist restart).
    Snoozes are converted on first use (`ensure_converted`), not here."""
    global _tab_state, _converted
    v2 = state.read_json_state(TAB_STATE_V2_FILE, None, expect=dict)
    with _lock:
        if v2 is not None and v2.get("schema") == SCHEMA:
            _tab_state = _normalize(v2)
            _converted = v2.get("converted", True) is not False
            return
        data = _read_legacy()
        _tab_state = _normalize(data)
        # No legacy file: nothing to convert (v2 is written on first save).
        _converted = not TAB_STATE_FILE.exists()


def _read_legacy():
    """tab_state.json, read only: a file that does not parse is logged and
    left exactly as it is (it belongs to the pre-7B code)."""
    try:
        data = json.loads(TAB_STATE_FILE.read_bytes())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.error("tab_state: %s unreadable (%s); left as is", TAB_STATE_FILE, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_locked():
    """Write tab_state.v2.json. Caller must hold _lock. tab_state.json is never
    written. A doc not converted yet (pin/order written while the pane list
    could not be read) is saved with `converted: false` and converts later."""
    try:
        state.atomic_write_json(TAB_STATE_V2_FILE, {**_tab_state, "schema": SCHEMA, "converted": _converted})
    except OSError:
        pass


class SnoozeUnavailable(Exception):
    """The live pane list cannot say which pane a target is (unreadable, or
    a listed pane has no durable key): nothing is written (503)."""


def _complete(panes):
    """Every listed pane has its durable key (shared/drafts.py:_complete): a
    key-less pane's display target is never a snooze key (R-P7B F2)."""
    return panes is not None and all(ref.key for ref in panes)


def _live_panes():
    """(panes, ok), as shared/drafts.py:_live_panes (a key-less listing is
    not ok)."""
    try:
        panes = _list_panes()
        return (panes, True) if _complete(panes) else (None, False)
    except _tmux().TmuxListError as exc:
        return ([], True) if exc.no_server else (None, False)
    except FileNotFoundError:
        return [], True
    except (OSError, subprocess.TimeoutExpired):
        return None, False


def ensure_converted(panes=None):
    """Convert legacy snooze keys to durable keys once (shared/drafts.py
    explains the rules). True when the doc is canonical."""
    global _tab_state, _converted
    from shared.drafts import convert_entries

    if _converted:
        return True
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            return False
    if not _complete(panes):
        return False
    with _lock:
        if _converted:
            return True
        snoozed = convert_entries(_tab_state["snoozed"], panes, time.time())
        _tab_state = {**_tab_state, "snoozed": snoozed}
        _converted = True
        _save_locked()
    return True


def _snooze_key(target, panes=None):
    """The stored key for a client's target: a durable key as given, else the
    live pane's key; None when it names no live pane. SnoozeUnavailable when
    the list cannot be read or the pane has no durable key."""
    if not target:
        return None
    if _tmux().parse_pane_key(target):
        return target
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            raise SnoozeUnavailable(target)
    elif not _complete(panes):
        raise SnoozeUnavailable(target)
    ref = _tmux().find_pane(target, panes)
    return ref.key if ref is not None else None


def _project_snoozed(snoozed, panes):
    """Snoozes by display target for the live panes; orphans never shown."""
    names = {ref.key: ref.display_target for ref in panes or [] if ref.key}
    return {
        names[key]: {"at": meta["at"], "was_busy": meta["was_busy"]}
        for key, meta in snoozed.items()
        if key in names and not meta.get("orphan")
    }


def _browser_doc_locked(panes):
    """Copy only state intended for the tab-state API, snoozes projected."""
    doc = copy.deepcopy(
        {key: value for key, value in _tab_state.items() if key != "agent_declarations"}
    )
    doc["snoozed"] = _project_snoozed(doc["snoozed"], panes)
    return doc


def get_tab_state(panes=None):
    """Return the browser-visible tab doc (deep copy).

    `panes` is the caller's own listing (/poll's, [] when it failed); it is
    only projected through, never converted against, so a failed listing
    cannot orphan anything. Without it the panes are listed (and the doc
    converted) here. Snoozes appear under each live pane's display target.
    """
    if panes is None:
        panes, ok = _live_panes()
        if ok:
            ensure_converted(panes)
    with _lock:
        return _browser_doc_locked(panes or [])


def get_agent_declaration(target):
    """Return the server-only launch declaration for one pane, if any."""
    with _lock:
        return copy.deepcopy(_tab_state["agent_declarations"].get(target))


def set_agent_declaration(target, declaration):
    """Persist one server-only launch declaration in the tab-state file."""
    global _tab_state
    with _lock:
        declarations = dict(_tab_state["agent_declarations"])
        declarations[target] = declaration
        _tab_state = {**_tab_state, "agent_declarations": declarations}
        _save_locked()


def delete_agent_declaration(target):
    """Remove a stale launch declaration, if present."""
    global _tab_state
    with _lock:
        if target not in _tab_state["agent_declarations"]:
            return
        declarations = dict(_tab_state["agent_declarations"])
        declarations.pop(target, None)
        _tab_state = {**_tab_state, "agent_declarations": declarations}
        _save_locked()


def sweep_agent_declarations(live_targets):
    """Drop declarations for panes that no longer exist."""
    global _tab_state
    with _lock:
        declarations = {
            target: declaration
            for target, declaration in _tab_state["agent_declarations"].items()
            if target in live_targets
        }
        if declarations != _tab_state["agent_declarations"]:
            _tab_state = {**_tab_state, "agent_declarations": declarations}
            _save_locked()


def set_lists(pinned=None, order=None, sort=None):
    """Replace the pinned and/or order lists (session names), and/or sort mode.

    Writing an explicit order implies manual sort — otherwise the drag you just
    made would be silently overruled by a name or date sort still in force.
    """
    global _tab_state
    with _lock:
        merged = dict(_tab_state)
        if pinned is not None:
            merged["pinned"] = pinned
        if order is not None:
            merged["order"] = order
            if sort is None:
                sort = "manual"
        if sort is not None:
            merged["sort"] = sort
        _tab_state = _normalize(merged)
        _save_locked()
    return get_tab_state()


def _activity_name(target, panes):
    """The activity map's key (a display target) for a snooze key."""
    if not _tmux().parse_pane_key(target):
        return target
    ref = _tmux().find_pane(target, panes or [])
    return ref.display_target if ref is not None else None


def _is_busy(target, now=None, panes=None):
    """True while the pane's last content change is under QUIET_SEC old.

    `target` may be a durable key: the activity map is keyed by display
    target, so a key is read at its pane's current display target (`panes`,
    else listed now). A key naming no live pane is not busy.

    Takes state._activity_lock, so it must never be called while holding
    _lock — the lock order in this module is always _lock then _activity_lock.
    """
    now = now if now is not None else time.time()
    if _tmux().parse_pane_key(target) and panes is None:
        panes, _ok = _live_panes()
    name = _activity_name(target, panes)
    if name is None:
        return False
    with state._activity_lock:
        last = state.pane_last_activity.get(name)
    if last is None:
        return False
    return (now - last) < QUIET_SEC


def set_snooze(target, on, panes=None):
    """Snooze or wake one pane, named by display target, `%N` or durable key.

    Snoozing stamps the pane's current busy/quiet state so sweep_wakes() can
    spot the transition later. None (nothing written) when a snooze names no
    live pane; a wake of such a target is a no-op that still returns the doc.
    SnoozeUnavailable (nothing written) when the doc cannot be converted yet or
    the live list cannot name the pane (unreadable, or no durable key).
    """
    global _tab_state
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            raise SnoozeUnavailable(target)
    if not ensure_converted(panes):
        raise SnoozeUnavailable(target)
    key = _snooze_key(target, panes)
    if key is None:
        return None if on else get_tab_state(panes)
    # Outside the lock — _is_busy takes state._activity_lock.
    entry = {"at": time.time(), "was_busy": _is_busy(key, panes=panes)} if on else None
    with _lock:
        snoozed = dict(_tab_state["snoozed"])
        if entry is None:
            snoozed.pop(key, None)
        else:
            snoozed[key] = entry
        _tab_state = {**_tab_state, "snoozed": snoozed}
        _save_locked()
        return _browser_doc_locked(panes)


def sweep_wakes(panes, now=None):
    """Auto-wake snoozed panes whose busy/quiet state flipped, and GC dead ones.

    A snoozed pane wakes on the first busy<->quiet transition after it was
    snoozed: snooze a working pane and it returns when it finishes; snooze a
    quiet one and it returns when it starts up again.

    Waking on raw content change instead does not work — routes/poll.py hashes
    the entire capture including ANSI, so a TUI spinner re-triggers it every
    poll and snoozing a busy pane would be a no-op.

    `panes` is /poll's listing; None (the listing failed) sweeps nothing, so a
    tmux hiccup cannot drop every snooze. An orphaned legacy snooze is kept
    until ORPHAN_EXPIRY_SEC after it was orphaned; a keyed snooze whose pane
    is gone (or whose tmux server restarted) is dropped, as before.
    """
    global _tab_state
    # A key-less listing is incomplete: its panes would all read as gone.
    if not _complete(panes):
        return
    now = now if now is not None else time.time()
    if not ensure_converted(panes):
        return

    live = {ref.key for ref in panes}
    with _lock:
        targets = list(_tab_state["snoozed"])
    if not targets:
        return

    # Computed with _lock released — _is_busy takes state._activity_lock.
    busy_now = {t: _is_busy(t, now, panes=panes) for t in targets}

    with _lock:
        snoozed = dict(_tab_state["snoozed"])
        changed = False
        for target in targets:
            meta = snoozed.get(target)
            if meta is None:
                continue  # woken by a request between the two lock windows
            if meta.get("orphan"):
                if now - max(meta["at"], meta.get("orphaned_at") or 0.0) > ORPHAN_EXPIRY_SEC:
                    snoozed.pop(target, None)
                    changed = True
            elif target not in live:
                # The pane is gone; there is nothing left to come back to.
                snoozed.pop(target, None)
                changed = True
            elif now - meta["at"] < MIN_SNOOZE_SEC:
                continue
            elif busy_now[target] != meta["was_busy"]:
                snoozed.pop(target, None)
                changed = True
        if changed:
            _tab_state = {**_tab_state, "snoozed": snoozed}
            _save_locked()


def apply_order(panes):
    """Sort panes into display order and stamp `pinned` / `snoozed` on each.

    Panes are grouped by session and the groups are sorted; a session's panes
    stay contiguous and in their original tmux order, so the adjacency the UI
    reads for is_subpane and team-lead grouping survives the sort.

    Pinned sessions lead in every sort mode — a pin is a statement about where
    you want a session, and an automatic sort shouldn't overrule it.

    Mutates the pane dicts in place and returns the reordered list.
    """
    with _lock:
        doc = copy.deepcopy(_tab_state)
    pinned = set(doc["pinned"])
    order = doc["order"]
    snoozed = {k: m for k, m in doc["snoozed"].items() if not m.get("orphan")}
    sort = doc.get("sort", "manual")
    rank = {session: i for i, session in enumerate(order)}

    groups = []  # [(session, [panes])] in first-seen tmux order
    index = {}
    for pane in panes:
        session = pane.get("session", "")
        if session not in index:
            index[session] = len(groups)
            groups.append((session, []))
        groups[index[session]][1].append(pane)

    def within(session, group, position):
        """The sort key inside a pin tier."""
        if sort == "name":
            return (session.lower(), session)
        if sort == "created":
            # Oldest session first, so the strip reads as opening order.
            return (group[0].get("created", 0) or 0, session.lower())
        # manual: explicit order first, everything else in tmux order behind it
        return (rank.get(session, len(order)), position)

    def sort_key(item):
        position, (session, group) = item
        return (0 if session in pinned else 1, within(session, group, position))

    ordered = []
    for _, (session, group) in sorted(enumerate(groups), key=sort_key):
        is_pinned = session in pinned
        for pane in group:
            pane["pinned"] = is_pinned
            # By the pane's key: a snooze follows its pane to a new display
            # target, and never lands on a stranger that took the old one.
            # A pane with no key is never stamped by its display target.
            pane["snoozed"] = bool(pane.get("key")) and pane.get("key") in snoozed
            ordered.append(pane)
    return ordered


def rename_session(old, new):
    """Rewrite pin/order (and legacy snooze) references after a session rename.

    Server-side so a rename fixes up every connected device, not just the one
    that issued it. A keyed snooze carries no session name and an orphan is
    never re-attached by name, so only a doc not yet converted has its snooze
    names moved (then it converts against the live list, which tmux has
    already renamed).
    """
    global _tab_state
    if not old or not new or old == new:
        return
    old_prefix = old + ":"
    new_prefix = new + ":"
    with _lock:
        doc = _tab_state
        _tab_state = _normalize(
            {
                "sort": doc.get("sort", "manual"),
                "pinned": [new if s == old else s for s in doc["pinned"]],
                "order": [new if s == old else s for s in doc["order"]],
                "snoozed": doc["snoozed"] if _converted else {
                    (
                        new_prefix + t[len(old_prefix) :]
                        if t.startswith(old_prefix)
                        else t
                    ): meta
                    for t, meta in doc["snoozed"].items()
                },
                "agent_declarations": {
                    (
                        new_prefix + t[len(old_prefix) :]
                        if t.startswith(old_prefix)
                        else t
                    ): declaration
                    for t, declaration in doc["agent_declarations"].items()
                },
            }
        )
        _save_locked()
    ensure_converted()


def is_empty():
    """True when nothing has been pinned, ordered, snoozed, or sorted yet."""
    with _lock:
        return not (
            _tab_state["pinned"]
            or _tab_state["order"]
            or _tab_state["snoozed"]
            or _tab_state.get("sort", "manual") != "manual"
        )


def import_legacy(pinned_targets, order_targets, snoozed_targets):
    """One-time import of the old per-device localStorage lists.

    Pin and order were keyed by target; map each onto its session, first
    occurrence winning. Refuses to run once the server doc holds anything, so
    the first device to sync wins and a second one cannot clobber it.

    Returns (doc, imported).
    """
    global _tab_state
    if not is_empty():
        return get_tab_state(), False

    def sessions_of(targets):
        return _dedup(
            [_tmux().session_of(t, panes=[]) or t
             for t in targets or [] if isinstance(t, str) and t]
        )

    panes, ok = _live_panes()
    panes = panes if ok else []
    ensure_converted(panes) if ok else None
    now = time.time()
    snoozed = {}
    for t in snoozed_targets or []:
        key = _snooze_key(t, panes) if isinstance(t, str) and t else None
        if key is not None:
            snoozed[key] = {"at": now, "was_busy": _is_busy(key, now, panes=panes)}
    with _lock:
        if (
            _tab_state["pinned"]
            or _tab_state["order"]
            or _tab_state["snoozed"]
            or _tab_state.get("sort", "manual") != "manual"
        ):
            return _browser_doc_locked(panes), False
        _tab_state = _normalize(
            {
                "pinned": sessions_of(pinned_targets),
                "order": sessions_of(order_targets),
                "snoozed": snoozed,
                "agent_declarations": _tab_state["agent_declarations"],
            }
        )
        _save_locked()
        return _browser_doc_locked(panes), True


load_tab_state()
