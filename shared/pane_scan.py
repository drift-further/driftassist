"""shared/pane_scan.py — one sample per pane: capture cache and activity, together.

/poll (one handler per browser) and the delegate watcher both read panes
through `sample()` (spec 7185 §5.1). The module owns:

- the capture cache, target -> entry, validated by the pane's identity: its
  `pane_id` AND its root process's start (`root`), because a restarted tmux
  server reuses `%N` and the new pane must not inherit the old one's capture
  or activity;
- the activity maps `state.pane_content_hash` / `state.pane_last_activity`
  (persisted by shared/state.py, read by tab_state and purge) — nothing else
  writes them; `tests/test_pane_scan.py` greps for that;
- `rev` / `gen` / `?since=`: `rev` rises only when a pane's browser tail
  changes; the epoch makes a `since` from before a restart read as "nothing".

Coalescing: a caller that finds a capture in flight for the pane waits for it
(bounded) instead of capturing again. Ordering: every capture draws a
monotonic `captured_seq` before it starts, and a publication is rejected —
cache and activity together — when the entry already holds a higher one, so a
late capture can never land an older hash or time after a newer one.
`capture-pane` itself runs with no lock held.
"""

import hashlib
import itertools
import re
import secrets
import threading
from dataclasses import dataclass

import shared.state as state
from shared.agent_identity import _ANSI_ESCAPE_RE, _CODEX_FINGERPRINT_RE, ROOT_UNKNOWN
from shared.tmux import capture_reusable
from routes.autoyes import detection_window

# target -> {"raw", "tail", "rev", "prev", "seq", "pane_id", "root", "content_hash",
#            "changed_at", "listed_at"}
_SCAN_CACHE = {}
_SCAN_LOCK = threading.Lock()
_SCAN_EPOCH = secrets.token_hex(4)
_scan_rev = 0
_seq = itertools.count(1)
# (target, pane_id, root) -> _Flight, the capture in flight for that pane.
_INFLIGHT = {}
INFLIGHT_WAIT_SEC = 2.0

_CODEX_QUOTA_RE = re.compile(
    r"\b(?!Context\b)((?:[a-z][a-z0-9_-]*|\d+h)\s+)\d+(%\s+left\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Sample:
    raw: str
    tail: str
    rev: int
    content_hash: str
    changed_at: float
    captured_seq: int
    generation: str
    # True when this call moved changed_at (or dropped the pane): the caller's
    # cue to persist idle state.
    changed: bool = False


def _activity_tail(tail, agent_kind):
    """Ignore account quota redraws only in Codex's bottom status bar."""
    if agent_kind != "codex":
        return tail
    lines = tail.split("\n")
    footer = [i for i, line in enumerate(lines) if _ANSI_ESCAPE_RE.sub("", line).strip()][-5:]
    in_status = False
    for i in footer:
        plain = _ANSI_ESCAPE_RE.sub("", lines[i])
        if _CODEX_FINGERPRINT_RE.search(plain):
            in_status = True
        elif not in_status or _CODEX_QUOTA_RE.sub("", plain).strip(" ·|\t"):
            # Only quota-only wrapped continuation rows belong to that footer.
            in_status = False
        if in_status:
            lines[i] = _CODEX_QUOTA_RE.sub(r"\1*\2", plain)
    return "\n".join(lines)


def activity_hash(tail, agent_kind):
    return hashlib.md5(_activity_tail(tail, agent_kind).encode()).hexdigest()


def gen():
    """The `gen` a browser echoes back as ?since=."""
    with _SCAN_LOCK:
        return f"{_SCAN_EPOCH}.{_scan_rev}"


def parse_since(value):
    """The rev a browser already holds, or None when it holds nothing we issued."""
    epoch, _, rev = (value or "").partition(".")
    if epoch != _SCAN_EPOCH or not rev.isdigit():
        return None
    return int(rev)


def _record_activity(target, content_hash, now, floor=None):
    """Under both locks: move changed_at when the hash moved. Returns changed."""
    prev_hash = state.pane_content_hash.get(target)
    if prev_hash is None:
        # First time seeing this pane — treat as just active.
        state.pane_last_activity.setdefault(target, now)
    elif content_hash != prev_hash:
        state.pane_last_activity[target] = now
    changed = content_hash != prev_hash
    if floor is not None:
        # Persisted name-based activity cannot predate this pane's birth.
        clamped = max(state.pane_last_activity[target], floor)
        changed |= state.pane_last_activity[target] != clamped
        state.pane_last_activity[target] = clamped
    state.pane_content_hash[target] = content_hash
    return changed


def _ident(entry):
    return (entry["pane_id"], entry.get("root"))


def _store(target, raw, prev, seq, pane_id, kind_of, now, cache, floor=None, root=None):
    """Under _SCAN_LOCK. Publish cache + activity, or reject both when stale.

    Returns (entry, changed, ok). Same pane: a capture older than the published
    one is rejected and the newer entry returned (ok). Another pane at this
    target: the listing taken later wins (`now` is when the caller listed the
    pane); an older listing is rejected outright (not ok), and only a newer one
    replaces the entry and restarts the target's activity, here, where the
    decision is made, never before it."""
    global _scan_rev
    entry = cache.get(target)
    if entry and _ident(entry) != (pane_id, root):
        if now < entry["listed_at"]:
            return entry, False, False
        cache.pop(target, None)
        with state._activity_lock:
            state.pane_content_hash.pop(target, None)
            state.pane_last_activity.pop(target, None)
        entry = None
    if entry and entry["seq"] > seq:
        return entry, False, True
    tail = detection_window(raw)
    if entry is None or entry["tail"] != tail:
        _scan_rev += 1
        rev = _scan_rev
    else:
        rev = entry["rev"]
    # Hashed over the capture as taken, not the detection window: the hash is
    # persisted (idle_state.json) and must stay comparable across restarts.
    content_hash = activity_hash(raw, kind_of(raw) if kind_of else None)
    with state._activity_lock:
        changed = _record_activity(target, content_hash, now, floor)
        changed_at = state.pane_last_activity[target]
    entry = {
        "raw": raw, "tail": tail, "rev": rev, "prev": prev, "seq": seq,
        "pane_id": pane_id, "root": root, "content_hash": content_hash, "changed_at": changed_at,
        "listed_at": now,
    }
    cache[target] = entry
    return entry, changed, True


def _sample_of(entry, changed=False):
    return Sample(
        raw=entry["raw"], tail=entry["tail"], rev=entry["rev"],
        content_hash=entry["content_hash"], changed_at=entry["changed_at"],
        captured_seq=entry["seq"], generation=entry["pane_id"], changed=changed,
    )


def _refresh_activity(target, entry, kind_of, now):
    """Under _SCAN_LOCK, on a reused capture: the activity maps may have been
    loaded from disk with a hash the current masking no longer produces."""
    raw = entry["raw"]
    content_hash = activity_hash(raw, kind_of(raw) if kind_of else None)
    with state._activity_lock:
        changed = _record_activity(target, content_hash, now)
        entry["content_hash"] = content_hash
        entry["changed_at"] = state.pane_last_activity[target]
    return changed


def store_capture(target, raw, prev, pane_id=None, kind_of=None, now=None, cache=None):
    """Publish a capture taken by the caller (the parity harness). Returns raw."""
    import time

    seq = next(_seq)
    with _SCAN_LOCK:
        entry, _, _ = _store(
            target, raw, prev, seq, pane_id, kind_of,
            now if now is not None else time.time(),
            _SCAN_CACHE if cache is None else cache,
        )
    return entry["raw"]


class _Flight:
    """One capture in progress for one pane. `ok` is set only when that capture
    was published (or found a newer one of the same pane) before `event`."""

    def __init__(self, seq):
        self.seq = seq
        self.ok = False
        self.event = threading.Event()


def sample(target, pane_id, marker, now, max_age, capture, kind_of=None, cache=None,
           reusable=None, fresh=False, floor=None, root=None):
    """One sample of a pane: the cached capture when its marker says the pane
    has not changed (and it is younger than max_age), else a fresh one.

    `capture(target) -> raw | None` runs `capture-pane` with no lock held.
    `kind_of(tail) -> agent_kind` decides the activity masking (`_activity_tail`).
    `cache` defaults to the module cache and `reusable` to capture_reusable;
    /poll passes its own bindings so the perf suite's patches of
    `routes.poll._SCAN_CACHE` and `routes.poll.capture_reusable` still apply.
    `fresh` always captures (purge's recheck) and `floor` clamps the activity
    time to the pane's birth; both still publish through the same ordering.
    `now` is when the caller listed the pane: a listing older than the pane
    already published at this target is obsolete.

    `root` is the pane root process's start (shared.agent_identity.
    pane_root_start): with `pane_id` it is the pane's identity, so every cache
    user must pass it; ROOT_UNKNOWN (a root that could not be read) gets None.
    Coalescing is per pane (target, pane_id and root). A waiter takes the flight's
    result only when that capture succeeded for the same pane and the same
    marker; a failed or timed-out flight sends it to one capture of its own.
    Returns a Sample, or None when the capture failed or the listing is obsolete.
    """
    if root is ROOT_UNKNOWN:
        # No identity to publish under or reuse for: no sample at all, so an
        # unreadable root can never borrow another generation's activity.
        return None
    if cache is None:
        cache = _SCAN_CACHE
    if reusable is None:
        reusable = capture_reusable
    ident = (pane_id, root)
    key = (target, pane_id, root)
    with _SCAN_LOCK:
        entry = cache.get(target)
        if entry and _ident(entry) != ident and now < entry["listed_at"]:
            return None
        if entry and _ident(entry) != ident:
            entry = None
        if not fresh and entry and reusable(entry["prev"], marker, now, max_age):
            changed = _refresh_activity(target, entry, kind_of, now)
            return _sample_of(entry, changed)
        # Join the flight in progress, or become it, in this one critical
        # section: two callers can never both find "no flight" and capture.
        other = None if fresh else _INFLIGHT.get(key)
        if other is None:
            flight = _Flight(next(_seq))
            if not fresh:
                _INFLIGHT[key] = flight
    if other is not None:
        # Someone else is capturing this pane: wait for their publication.
        if other.event.wait(INFLIGHT_WAIT_SEC) and other.ok:
            with _SCAN_LOCK:
                entry = cache.get(target)
                if (entry and _ident(entry) == ident and entry["seq"] >= other.seq
                        and entry["prev"] and entry["prev"][0] == marker):
                    return _sample_of(entry)
        # It failed, timed out or saw another listing: one capture of our own.
        with _SCAN_LOCK:
            flight = _Flight(next(_seq))
            if key not in _INFLIGHT:
                _INFLIGHT[key] = flight
    import time

    captured_at = time.time()
    try:
        raw = capture(target)
    except Exception:
        raw = None
    with _SCAN_LOCK:
        try:
            if raw is None:
                return None
            entry, changed, flight.ok = _store(
                target, raw.rstrip("\n"), (marker, captured_at), flight.seq, pane_id, kind_of,
                now, cache, floor, root,
            )
            return _sample_of(entry, changed) if flight.ok else None
        finally:
            if _INFLIGHT.get(key) is flight:
                _INFLIGHT.pop(key, None)
            flight.event.set()


def forget_missing(live_targets, listing_ok, cache=None):
    """Drop entries for panes no longer listed — only after a complete listing.
    Returns True when an activity map changed."""
    if not listing_ok:
        return False
    if cache is None:
        cache = _SCAN_CACHE
    changed = False
    with _SCAN_LOCK:
        for stale in set(cache) - set(live_targets):
            cache.pop(stale, None)
        with state._activity_lock:
            for stale in set(state.pane_content_hash) - set(live_targets):
                state.pane_content_hash.pop(stale, None)
                state.pane_last_activity.pop(stale, None)
                changed = True
    return changed
