"""shared/drafts.py — Per-target composer drafts (text + attachments + Enter lock).

The composer used to hold exactly one message, globally, and sending was the
only way to clear it. It is per tmux target now: switching tabs swaps text,
attachment tray and Enter lock together, and unsent text simply IS that tab's
draft — there is no mode to toggle and no save command.

Server-side rather than localStorage (spec 2023 §3): Assist is phone-first and
a draft begun on the desktop has to be on the phone. Shaped after
shared/tab_state.py — one lock, one atomically-written JSON file, normalized on
every load so a hand-edited file cannot poison the app.

Keyed by PANE, not session: a draft belongs to the pane you were typing at.
Since effort 621 Phase 7 step B the stored key is the pane's durable key
`<generation>:%N` (shared/tmux.py:PaneRef.key), not `session:w.p`, which names
a position another pane can take. A pre-upgrade file is converted once, before
the first canonical read or write (`ensure_converted`, `schema: 2` marker):

- a legacy `session:w.p` (or `%N`) naming exactly one live pane becomes that
  pane's key, every field kept, `legacy_from` recorded;
- one naming no live pane stays under its legacy key as `orphan: true`; a name
  carries no generation, so it is never assigned to a later pane by name;
- a collision (the pane already has a canonical entry) keeps both: the
  canonical entry stays and the legacy one becomes an orphan.

Schema 2 lives in its own file, `drafts.v2.json`; `drafts.json` is only ever
read (never written, never moved aside), so a code rollback to a pre-7B
Assist finds its own file exactly as it left it (sign-off M3 F5, the owner's
decision on question 7039). v2 records the legacy content it was made from
(sha256 and each entry's updated_at); on a later start, legacy entries that
are new or changed since then (an older Assist ran in between) are imported
as `legacy-after-rollback` orphans, never by name and never over a v2 entry.
The two stores never share an upload file either (sign-off M3 R2-F2): every
upload drafts.json references gets its own v2 name (a hard link, else a copy)
before v2 metadata names it, so the pre-7B sweeper unlinking its own expired
reference leaves a v2 draft's file usable, and the v2 sweeper never unlinks a
path drafts.json references. Only an upload confirmed gone is dropped; a full
or failing /tmp publishes nothing and is retried (sign-off M3 R3-F2). A
durable key from another tmux server generation is an orphan too. Orphans are
listed by `list_orphans`, reach a pane only through the explicit
`copy_orphan` (the orphan stays: sign-off M3 R3, the owner's decision of
2026-11-16), and leave only through `dismiss_orphan` or 30 days after they
were orphaned. A dead pane's durable key still addresses its orphan, so an
ordinary write to it never shrinks it: one adding text or a file is kept
beside it as its own `late-save` orphan (sign-off M3 R4-F1).
Old clients still send `session:w.p`: every read and write resolves it to the
live pane's key, and `poll_block` projects marks and revisions per protocol.

**An entry is deleted when it carries nothing worth remembering** (spec §7:
"empty drafts are deleted, not stored") — but a disarmed Enter lock IS worth
remembering, so `is_default()` requires an armed lock as well as empty content.
Disarming the lock on an empty composer and then typing must not lose the lock.
The tab marker keys off `has_content()`, never off entry existence, so the
marker and the stored map still cannot disagree.
"""

import copy
import errno
import hashlib
import json
import logging
import os
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

from shared import state, tmux

log = logging.getLogger(__name__)

DRAFTS_FILE = state.DATA_DIR / "drafts.json"        # pre-7B format: read, never written
DRAFTS_V2_FILE = state.DATA_DIR / "drafts.v2.json"  # schema 2: the only file written

# A draft untouched for this long is dropped, along with any uploads only it
# referenced. Without it both the map and /tmp grow without bound, because with
# per-tab drafts the tray is no longer freed by clearing on send.
# 30 days is a guess (spec §7) and is the number here most worth revisiting.
EXPIRY_SEC = 30 * 24 * 60 * 60

# How often the background sweep runs. Expiry is measured in days; an hourly
# pass is far more resolution than that needs and costs one lock acquisition.
SWEEP_INTERVAL_SEC = 60 * 60

# routes/input.py:upload_file() writes every upload as /tmp/assist_<uuid>_<name>.
# The sweep refuses to unlink anything that does not match, so a hand-edited
# drafts.json cannot turn expiry into an arbitrary-delete primitive.
_UPLOAD_DIR = Path("/tmp")
_UPLOAD_PREFIX = "assist_"

SCHEMA = 2

# A conversion or rollback-era import that could not make its upload files
# (ENOSPC, EIO…) publishes nothing and is tried again: the conversion on every
# read, a pending import at most this often.
RETRY_SEC = 60

# Patched by tests; the live pane list the converter and the lookups read.
def _list_panes():
    # Late-bound: shared.agent_identity imports this module while shared.tmux
    # is still initialising.
    return tmux.list_panes()



_lock = threading.Lock()
_drafts = {}  # pane key -> {text, attachments, enter_armed, updated_at, ...}
# True once the in-memory map is canonical (schema 2). A legacy file loads as
# False and is converted before anything is served from it.
_converted = True
# The legacy drafts.json content the store was made from: {"sha256", "entries":
# {name: updated_at}}. Kept in v2 so a later start can tell what an older
# Assist wrote after it.
_source = {"sha256": None, "entries": {}}
# Upload paths drafts.json references (read at load; the new code never writes
# that file). A v2 entry never stores one of these: it gets its own file.
_legacy_paths = set()
# (sha256, entries) of drafts.json still to import into a loaded v2 store, or
# None; retried from ensure_converted no sooner than _retry_at (monotonic).
_pending_import = None
_retry_at = 0.0
# The last retryable failure logged, so a retry loop logs it once.
_retry_note = None
_retry_key = None


def _default_entry():
    return {"text": "", "attachments": [], "enter_armed": True, "updated_at": 0.0}


def _normalize_attachment(raw):
    """Coerce one tray entry, or None if it carries no usable upload path."""
    if not isinstance(raw, dict):
        return None
    path = raw.get("path")
    if not isinstance(path, str) or not path:
        return None
    try:
        size = int(raw.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or ""),
        "size": max(0, size),
        "path": path,
    }


def _normalize_entry(raw):
    """Coerce one stored draft into the canonical shape."""
    if not isinstance(raw, dict):
        raw = {}
    text = raw.get("text")
    attachments = []
    for item in raw.get("attachments") or []:
        entry = _normalize_attachment(item)
        if entry is not None:
            attachments.append(entry)
    try:
        updated_at = float(raw.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        updated_at = 0.0
    out = {
        "text": text if isinstance(text, str) else "",
        "attachments": attachments,
        # Armed by default — today's one-liner reflex is untouched until the
        # lock is deliberately disarmed (spec §4).
        "enter_armed": bool(raw.get("enter_armed", True)),
        "updated_at": updated_at,
    }
    # Provenance from the step B conversion, an orphan copy (`moved_from` is
    # what the earlier move wrote) and a re-upgrade; `copied_to` marks an
    # orphan already copied onto a pane.
    for name in ("legacy_from", "copied_from", "moved_from", "orphan_reason", "copied_to",
                 "saved_for"):
        if isinstance(raw.get(name), str) and raw[name]:
            out[name] = raw[name]
    if raw.get("orphan") is True:
        out["orphan"] = True
        try:
            out["orphaned_at"] = float(raw.get("orphaned_at") or 0.0)
        except (TypeError, ValueError):
            out["orphaned_at"] = 0.0
    elif raw.get("orphaned_at") is not None:
        # A durable key whose pane's death a complete listing confirmed.
        try:
            out["orphaned_at"] = float(raw["orphaned_at"])
        except (TypeError, ValueError):
            pass
    if raw.get("copied_at") is not None:
        try:
            out["copied_at"] = float(raw["copied_at"])
        except (TypeError, ValueError):
            pass
    return out


def _normalize(data):
    if not isinstance(data, dict):
        return {}
    out = {}
    for target, raw in data.items():
        # "schema" is the file's marker, never a pane (a pane key holds ':').
        if not isinstance(target, str) or not target or target == "schema":
            continue
        out[target] = _normalize_entry(raw)
    return out


def has_content(entry):
    """True when this draft is something the user would want a marker for."""
    return bool((entry.get("text") or "").strip() or entry.get("attachments"))


def _is_default(entry):
    """True when the entry holds nothing worth persisting.

    Note the enter_armed clause: an empty composer with a DISARMED lock is a
    deliberate setting, not an empty draft, and must survive.
    """
    return not has_content(entry) and entry.get("enter_armed", True)


def _read_legacy():
    """(sha256 or None, normalized entries) of drafts.json. Read only: a file
    that does not parse is logged and left exactly as it is (it belongs to the
    pre-7B code), and counts as holding nothing."""
    try:
        raw = DRAFTS_FILE.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        log.error("drafts: cannot read %s (%s); left as is", DRAFTS_FILE, exc)
        return None, {}
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw)
    except ValueError:
        log.error("drafts: %s does not parse; left as is, nothing imported", DRAFTS_FILE)
        return digest, {}
    return digest, _normalize(data if isinstance(data, dict) else {})


def _attachment_paths(entries):
    return {a["path"] for e in entries.values() for a in e.get("attachments") or ()}


class AttachmentCopyFailed(Exception):
    """An upload that exists could not be given its own file: no space, an
    I/O or permission error. Retryable, never "the file is gone": nothing is
    published and the caller keeps its previous state."""

    def __init__(self, path, exc):
        super().__init__(f"{path}: {exc}")
        self.path = path
        self.errno = getattr(exc, "errno", None)

    @property
    def error(self):
        return "storage_full" if self.errno in (errno.ENOSPC, errno.EDQUOT) else "attachment_copy_failed"

    @property
    def status(self):
        return 507 if self.error == "storage_full" else 500


def _source_gone(src):
    """True only when the file is confirmed absent."""
    try:
        os.lstat(src)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _discard(path):
    """Remove a half-made copy; a leftover unreferenced file is acceptable."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _owned_copy(path):
    """A new upload name for the file at `path`, for v2 alone: a hard link,
    or a copy where linking fails (another filesystem). Created before any v2
    metadata names it, so a crash in between leaves at most an unreferenced
    file; the name keeps the original's part after its uuid.

    None only when there is confirmed to be no regular file to own: the source
    is absent (ENOENT/ENOTDIR) or not a regular file (a symlink no upload ever
    is). Any other failure raises AttachmentCopyFailed (sign-off M3 R3-F2):
    a full /tmp must not read as a vanished upload."""
    src = Path(path)
    rest = src.name[len(_UPLOAD_PREFIX):].split("_", 1)[-1] or "file"
    try:
        st = os.lstat(src)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise AttachmentCopyFailed(path, exc) from exc
    if not stat.S_ISREG(st.st_mode):
        return None
    for _ in range(8):
        dst = _UPLOAD_DIR / f"{_UPLOAD_PREFIX}{uuid.uuid4().hex[:8]}_{rest}"
        try:
            os.link(src, dst, follow_symlinks=False)
            return str(dst)
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.ENOENT and _source_gone(src):
                return None
            # Otherwise (another filesystem, links refused, …) the copy decides.
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         stat.S_IMODE(st.st_mode) or 0o600)
        except FileExistsError:
            continue
        except OSError as exc:
            raise AttachmentCopyFailed(path, exc) from exc
        try:
            with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
                while chunk := inp.read(1024 * 1024):
                    out.write(chunk)
            return str(dst)
        except OSError as exc:
            _discard(dst)
            if isinstance(exc, (FileNotFoundError, NotADirectoryError)) and _source_gone(src):
                return None
            raise AttachmentCopyFailed(path, exc) from exc
    raise AttachmentCopyFailed(path, FileExistsError(errno.EEXIST, "no free upload name"))


def _own_attachments(entries, legacy_paths):
    """(new entries, changed): every upload drafts.json references, in a copy
    of `entries`, replaced by v2's own file. `entries` is not modified, so the
    caller publishes the result only once every file exists (the files are
    made before the metadata naming them is saved). A path that is not one of
    our /tmp uploads is left as it is (neither sweeper ever unlinks it, and
    copying it would read an arbitrary path). An upload confirmed gone is
    dropped with a warning: v2 never names a file it does not own, and
    drafts.json still holds its record. Any other failure raises
    AttachmentCopyFailed after removing the files this call made."""
    made = {}
    out = {}
    changed = False
    try:
        for key, e in entries.items():
            kept = []
            for att in e["attachments"]:
                path = att["path"]
                if path in legacy_paths and _is_sweepable_upload(path):
                    if path not in made:
                        made[path] = _owned_copy(path)
                    changed = True
                    if made[path] is None:
                        log.warning("drafts: %s's upload %s is gone; dropped from v2 "
                                    "(drafts.json keeps it)", key, path)
                        continue
                    att = {**att, "path": made[path]}
                kept.append(att)
            out[key] = {**e, "attachments": kept}
    except AttachmentCopyFailed:
        for own in made.values():
            if own:
                _discard(own)
        raise
    return out, changed


def _note_retry(what, exc):
    """Log a retryable failure once, however often it is retried. Keyed on
    the source path and errno, not the message: an OSError names the fresh
    random destination it tried, so its text differs on every retry."""
    global _retry_note, _retry_key
    key = (what, getattr(exc, "path", None), getattr(exc, "errno", None))
    _retry_note = f"{what}: {exc}"
    if key != _retry_key:
        log.error("drafts: %s; nothing published, will retry", _retry_note)
        _retry_key = key


def retry_reason():
    """Why the drafts store is held back, for the 503 body: the upload that
    could not be given its own file, or None. A permanent cause (a file
    another user owns) is then findable without the server log."""
    if _retry_key is None:
        return None
    _what, path, err = _retry_key
    return {"reason": "attachment_copy_failed", "path": path,
            "errno": errno.errorcode.get(err, err) if err is not None else None}


def _clear_retry():
    global _retry_note, _retry_key
    if _retry_note is not None:
        log.info("drafts: retry succeeded (%s)", _retry_note)
        _retry_note = None
    _retry_key = None


def _read_legacy_quiet():
    """drafts.json's entries, or {} when it is absent or unreadable, without
    logging (the hourly sweep reads it)."""
    try:
        data = json.loads(DRAFTS_FILE.read_bytes())
    except (OSError, ValueError):
        return {}
    return _normalize(data if isinstance(data, dict) else {})


def _snapshot(digest, entries):
    return {"sha256": digest, "entries": {k: e["updated_at"] for k, e in entries.items()}}


def _import_after_rollback(into, entries, now):
    """Legacy entries new or changed since the v2 snapshot: an older Assist
    wrote them after the upgrade. Each becomes an orphan in `into` under its
    legacy name (or that name with a suffix if `into` already uses it): never
    assigned to a pane by name, never over a v2 entry. Returns how many."""
    before = _source.get("entries") or {}
    count = 0
    for name, entry in entries.items():
        if name in before and before[name] == entry["updated_at"]:
            continue
        if not has_content(entry):
            continue  # a bare Enter-lock setting carries nothing to recover
        key, n = name, 1
        # A durable key is where its live pane's own saves go: an orphan
        # stored there would be rewritten by the next ordinary save.
        while key in into or tmux.parse_pane_key(key):
            n += 1
            key = f"{name} (after rollback{'' if n == 2 else ' ' + str(n - 1)})"
        into[key] = {**entry, "orphan": True, "orphaned_at": now,
                        "orphan_reason": "legacy-after-rollback", "legacy_from": name}
        count += 1
    return count


def _finish_import_locked():
    """Import what an older Assist wrote into drafts.json since the v2
    snapshot, and give v2 its own file for every upload drafts.json
    references, staged: published (and the snapshot consumed) only once every
    file exists. On a retryable failure nothing changes and the import stays
    pending (sign-off M3 R3-F2). Caller holds _lock. True when done."""
    global _drafts, _source, _pending_import, _retry_at
    if _pending_import is None:
        return True
    digest, legacy = _pending_import
    fresh = digest != _source.get("sha256")
    staged = dict(_drafts)
    imported = _import_after_rollback(staged, legacy, time.time()) if fresh else 0
    try:
        staged, owned = _own_attachments(staged, _legacy_paths)
    except AttachmentCopyFailed as exc:
        _retry_at = time.monotonic() + RETRY_SEC
        _note_retry("importing drafts.json", exc)
        return False
    _drafts = staged
    _pending_import = None
    if fresh:
        _source = _snapshot(digest, legacy)
    if imported:
        log.warning("drafts: %d draft(s) written by an older Assist kept as orphans", imported)
    if fresh or owned:
        _save_locked()
    _clear_retry()
    return True


def load_drafts():
    """Load from disk. Called at import and by tests (an Assist restart).

    With a v2 file, that is the store, plus any legacy entries an older Assist
    wrote since (imported as orphans). Without one, the legacy entries are
    held unconverted: converting needs the live pane list, so it runs on the
    first canonical read or write (`ensure_converted`), and serve.py calls it
    once at start so a rename cannot come first. A v2 file that does not
    parse is moved aside (`.corrupt-<ts>`) and rebuilt from drafts.json.
    An import whose upload files cannot be made yet stays pending and is
    retried (`_finish_import_locked`), while the v2 store serves as it was.
    """
    global _drafts, _converted, _source, _legacy_paths, _pending_import, _retry_at
    v2 = state.read_json_state(DRAFTS_V2_FILE, None, expect=dict)
    digest, legacy = _read_legacy()
    with _lock:
        _legacy_paths = _attachment_paths(legacy)
        _pending_import = None
        _retry_at = 0.0
        if v2 is not None and v2.get("schema") == SCHEMA:
            _drafts = _unshadow(_normalize(v2.get("drafts")))
            _source = v2.get("source") if isinstance(v2.get("source"), dict) else {}
            _converted = True
            # Imported drafts, and a store written before v2 owned its files,
            # get their own copy of every upload drafts.json references.
            _pending_import = (digest, legacy)
            _finish_import_locked()
        else:
            _drafts = legacy
            _source = _snapshot(digest, legacy)
            # No legacy file: nothing to convert (v2 is written on first save).
            _converted = digest is None


def _free_key(base, used, label):
    """`base (label)`, `base (label 2)`, … : the first name not in `used`.
    Never a durable key, so no ordinary write can address it."""
    n = 1
    while True:
        key = f"{base} ({label}{'' if n == 1 else ' ' + str(n)})"
        if key not in used:
            return key
        n += 1


def _unshadow(entries):
    """An entry flagged `orphan` under a durable key (an earlier build's
    rollback-era import of a key a new page sent an older Assist) moved to a
    free name, content untouched, so the live pane's saves cannot reach it."""
    if not any(e.get("orphan") and tmux.parse_pane_key(k) for k, e in entries.items()):
        return entries
    out = {}
    for key, e in entries.items():
        if e.get("orphan") and tmux.parse_pane_key(key):
            key = _free_key(key, set(entries) | set(out), "after rollback")
        out[key] = e
    return out


def _save_locked():
    """Write drafts.v2.json. Caller must hold _lock. Only a canonical map is
    written, and drafts.json never is."""
    if not _converted:
        return
    try:
        state.atomic_write_json(DRAFTS_V2_FILE, {"schema": SCHEMA, "source": _source, "drafts": _drafts})
    except OSError:
        pass


class DraftsUnavailable(Exception):
    """The live pane list cannot say which pane a target is: unreadable, or a
    listed pane has no durable key. Nothing is written (503)."""


def _complete(panes):
    """A listing every pane of which has its durable key. A pane whose tmux
    server start time could not be read (PaneRef.key None) has no name that is
    safe to store under, so such a listing counts as unreadable: its display
    target is never used as a storage key (R-P7B F2)."""
    return panes is not None and all(ref.key for ref in panes)


def _live_panes():
    """(panes, ok). No tmux server is a valid empty listing: every pane from
    before is gone. Any other failure, or a listed pane without a durable key,
    is (None, False): try again later."""
    try:
        panes = _list_panes()
        return (panes, True) if _complete(panes) else (None, False)
    except tmux.TmuxListError as exc:
        return ([], True) if exc.no_server else (None, False)
    except FileNotFoundError:
        return [], True
    except (OSError, subprocess.TimeoutExpired):
        return None, False


def convert_entries(entries, panes, now):
    """The step B conversion of one {legacy key: value} map (pure).

    Shared with shared/tab_state.py. Returns a new map in the same order:
    matched legacy keys become the live pane's key with `legacy_from`, the rest
    (unmatched, colliding, matching a pane with no durable key, or not a pane
    name at all) stay under their own key flagged `orphan` with `orphaned_at`.
    ensure_converted defers rather than reach the key-less case.
    """
    canonical = {k for k in entries if tmux.parse_pane_key(k)}
    taken = set(canonical)
    out = {}
    for key, value in entries.items():
        if key in canonical:
            out[key] = value
            continue
        ref = tmux.find_pane(key, panes) if key else None
        new_key = ref.key if ref is not None else None
        if new_key and new_key not in taken:
            taken.add(new_key)
            out[new_key] = {**value, "legacy_from": key}
        else:
            out[key] = {**value, "orphan": True, "orphaned_at": now}
    return out


def ensure_converted(panes=None):
    """Convert a legacy map to schema 2 once. True when the map is canonical.

    False when the pane list could not be read for a reason other than
    "no server", or holds a pane without a durable key, or an upload file
    could not be made (no space, an I/O error): nothing is converted, written
    or served, and the next call tries again. drafts.json is left as it is.
    A converted store with an import still pending retries it when due.
    """
    global _drafts, _converted
    if _converted:
        if _pending_import is not None and time.monotonic() >= _retry_at:
            with _lock:
                _finish_import_locked()
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
        try:
            converted, _owned = _own_attachments(convert_entries(_drafts, panes, time.time()),
                                                 _legacy_paths)
        except AttachmentCopyFailed as exc:
            _note_retry("converting drafts.json", exc)
            return False
        _drafts = converted
        _converted = True
        _save_locked()
        _clear_retry()
        log.info("drafts: converted to schema %d", SCHEMA)
    return True


def _resolve(target, panes=None):
    """The storage key for a client's target, or None for no live pane.

    A durable key is its own storage key, live or not: it can only ever name
    its own pane. `session:w.p` and `%N` resolve through the live list; a list
    that cannot be read, or whose pane has no durable key, raises
    DraftsUnavailable (a display target is never a storage key).
    """
    if not target:
        return None
    if tmux.parse_pane_key(target):
        return target
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            raise DraftsUnavailable(target)
    elif not _complete(panes):
        raise DraftsUnavailable(target)
    ref = tmux.find_pane(target, panes)
    return ref.key if ref is not None else None


def _resolve_write(target, panes=None):
    """(storage key or None, live) for a write. A durable key may name a dead
    pane, which only a complete listing can tell: unreadable, or a listed pane
    without its key, raises DraftsUnavailable, never "dead" and never "live"."""
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            raise DraftsUnavailable(target)
    elif not _complete(panes):
        raise DraftsUnavailable(target)
    key = _resolve(target, panes)
    return key, key is not None and key in {ref.key for ref in panes}


def _adds_to(candidate, kept):
    """True when `candidate` carries text or a file `kept` does not."""
    text = candidate["text"]
    if text.strip() and text != kept["text"]:
        return True
    paths = {a["path"] for a in kept["attachments"]}
    return any(a["path"] not in paths for a in candidate["attachments"])


_PROVENANCE = ("copied_to", "copied_at", "copied_from", "legacy_from", "moved_from")


def _confirmed_gone_locked(key):
    """A complete listing already stamped this durable key's pane gone, and a
    durable key never comes back: dead, whatever an earlier listing said.
    Closes the listing-to-lock window (a copy made while a write waited)."""
    entry = _drafts.get(key)
    return entry is not None and ("orphaned_at" in entry or bool(entry.get("orphan")))


def _keep_late_save_locked(key, patch):
    """An ordinary write to a dead pane's durable key (sign-off M3 R4-F1).

    What is stored for that key is an orphan, so it is never shrunk,
    replaced or emptied: only Dismiss and expiry remove it. A write that adds
    nothing to it, or to a late save already kept for it, changes nothing
    (empty, whitespace, an Enter-lock flip, a subset of its files, the same
    text again). One that adds text or a file is a pane's final flush with
    something the orphan lacks: with nothing stored it becomes the key's own
    entry, otherwise its own `late-save` orphan beside it, under a name no
    write can address, each upload another entry names given its own file.
    Caller holds _lock. AttachmentCopyFailed with nothing changed."""
    current = _drafts.get(key)
    base = current or _default_entry()
    candidate = _normalize_entry({**base, **patch})
    if not has_content(candidate):
        return
    kept = [current] if current is not None else []
    kept += [e for e in _drafts.values() if e.get("saved_for") == key]
    if kept and not all(_adds_to(candidate, e) for e in kept):
        return
    now = time.time()
    if current is None or not has_content(current):
        entry = {**candidate, "updated_at": now,
                 "orphaned_at": base.get("orphaned_at") or now}
        if _legacy_paths & _attachment_paths({key: entry}):
            entry = _own_attachments({key: entry}, _legacy_paths)[0][key]
        _drafts[key] = entry
        _save_locked()
        return
    shared = _attachment_paths(_drafts) | _legacy_paths
    attachments, made = [], []
    try:
        for att in candidate["attachments"]:
            if att["path"] in shared and _is_sweepable_upload(att["path"]):
                own = _owned_copy(att["path"])
                if own is None:
                    log.warning("drafts: late save for %s: upload %s is gone; left out",
                                key, att["path"])
                    continue
                made.append(own)
                att = {**att, "path": own}
            attachments.append(att)
    except AttachmentCopyFailed:
        for own in made:
            _discard(own)
        raise
    # The orphan's provenance is not the late save's: inheriting `copied_to`
    # would label the only copy of the final flush "copied", inviting Dismiss.
    late = {k: v for k, v in candidate.items() if k not in _PROVENANCE}
    _drafts[_free_key(key, _drafts, "late save")] = {
        **late, "attachments": attachments, "updated_at": now,
        "orphan": True, "orphaned_at": now, "orphan_reason": "late-save", "saved_for": key,
    }
    _save_locked()


def get_draft(target, panes=None):
    """Return one pane's draft (deep copy). Absent panes get the default.

    Never an orphan: a legacy name is not looked up as a stored key.
    """
    if not target or not ensure_converted():
        return _default_entry()
    try:
        key = _resolve(target, panes)
    except DraftsUnavailable:
        return _default_entry()
    with _lock:
        entry = _drafts.get(key) if key else None
        return copy.deepcopy(entry) if entry is not None else _default_entry()


def set_draft(target, text=None, attachments=None, enter_armed=None, panes=None):
    """Partially update one draft and persist. Returns the resulting entry.

    Any field left None keeps its stored value, so the client can save text
    without re-sending the tray and vice versa. An update that leaves the entry
    at its default deletes it instead of storing an empty row.

    None when the store is not converted yet or the target names no live pane:
    nothing is written over what is stored, so a stale tab can never write
    over an orphan that happens to carry its name, nor a late save to a dead
    pane's key shrink its orphan (a late save adding to it is kept beside it,
    `_keep_late_save_locked`). DraftsUnavailable when the live list cannot
    name the pane (unreadable, or the pane has no durable key).
    AttachmentCopyFailed, with nothing changed, when a resent drafts.json
    upload exists but cannot be given v2's own file.

    Last writer wins. There is no revision precondition: an orphan copied
    onto this pane is still the orphan's, so a late save cannot lose it.
    """
    global _drafts
    if not target:
        return _default_entry()
    if not ensure_converted():
        return None
    target, live = _resolve_write(target, panes)
    if target is None:
        return None
    patch = {}
    if text is not None:
        patch["text"] = text
    if attachments is not None:
        patch["attachments"] = attachments
    if enter_armed is not None:
        patch["enter_armed"] = enter_armed
    with _lock:
        if not live or _confirmed_gone_locked(target):
            _keep_late_save_locked(target, patch)
            return None
        current = _drafts.get(target) or _default_entry()
        entry = _normalize_entry({**current, **patch})
        if _legacy_paths & _attachment_paths({target: entry}):
            # An older page re-sent a tray holding a drafts.json upload.
            entry = _own_attachments({target: entry}, _legacy_paths)[0][target]
        entry["updated_at"] = time.time()
        if _is_default(entry):
            _drafts.pop(target, None)
            # Report "nothing stored", not the instant we stored nothing. The
            # client mirrors this into its revision map and compares it against
            # poll_block()'s `rev`, which has no key for a deleted row: a fresh
            # stamp here would leave the writer permanently ahead of the server
            # and make its own next poll look like "another device deleted this",
            # blanking a composer the user had already typed into again.
            entry["updated_at"] = 0.0
        else:
            _drafts[target] = entry
        _save_locked()
        return copy.deepcopy(entry)


def delete_draft(target, panes=None):
    """Drop one live pane's draft entirely. A dead pane's key holds an orphan,
    which only Dismiss and expiry remove: nothing happens. DraftsUnavailable
    when the live list cannot say which (sign-off M3 R4-F1)."""
    global _drafts
    if not target or not ensure_converted():
        return
    target, live = _resolve_write(target, panes)
    if target is None or not live:
        return
    with _lock:
        if _confirmed_gone_locked(target):
            return
        if _drafts.pop(target, None) is not None:
            _save_locked()


def _orphan_reason(key, entry, live_keys):
    # Anything stored under a name that is not a durable key belongs to no
    # pane (nothing reads it by name), so it is listed rather than hidden.
    if entry.get("orphan") or not tmux.parse_pane_key(key):
        return entry.get("orphan_reason") or "legacy"
    if key not in live_keys:
        return "gone"
    return None


def _stamp_gone_locked(live_keys, now):
    """Record when a complete listing first showed a durable key's pane gone.
    Caller holds _lock and has a complete, successful listing. Never renewed:
    an entry that already has its time keeps it. True when something changed."""
    changed = False
    for key, e in _drafts.items():
        if ("orphaned_at" not in e and not e.get("orphan")
                and tmux.parse_pane_key(key) and key not in live_keys):
            e["orphaned_at"] = now
            changed = True
    return changed


def poll_block(panes, proto=1):
    """The /poll payload: what every browser needs to render markers and resync.

    Deliberately NOT the draft bodies. A poll runs every 5s per open browser and
    a draft can be thousands of characters; the client fetches a body from
    GET /api/draft only when it switches to that tab or sees `rev` move.

    `panes` is /poll's own listing (None when it failed: then there is no
    block, rather than one that reads as "every draft was deleted"). Only live
    panes appear, under the name the client understands: `proto` 2 gets
    durable keys plus an `orphans` count, an older client gets `session:w.p`.

    - marks: panes whose draft has content -> the .has-draft tab markers
    - rev:   every stored live pane -> updated_at, so a second device notices a
             change to the tab it is sitting on (spec §9.3) and re-fetches
    - at:    when this snapshot was taken, on the same clock as updated_at.
             /poll builds the block mid-request and ships it later, so a save
             can land in between; the client reads a target missing from `rev`
             as "deleted elsewhere" only when `at` is not older than its own
             last save, otherwise it would blank a composer it just saved
    """
    if not _complete(panes) or not ensure_converted(panes):
        return None
    names = {ref.key: ref.key if proto == 2 else ref.display_target for ref in panes}
    with _lock:
        if _stamp_gone_locked(names, time.time()):
            _save_locked()
        live = [(names[k], e) for k, e in _drafts.items()
                if k in names and not e.get("orphan")]
        block = {
            "marks": [name for name, e in live if has_content(e)],
            "rev": {name: e["updated_at"] for name, e in live},
            "at": time.time(),
        }
        if proto == 2:
            block["orphans"] = sum(
                1 for k, e in _drafts.items() if _orphan_reason(k, e, names)
            )
    return block


def list_orphans(panes=None):
    """Every orphaned draft, newest first, bodies included (the orphan sheet).

    `legacy`: a pre-upgrade name that matched no live pane, or collided.
    `gone`: a durable key whose pane, or whose tmux server, is gone.
    None when the store cannot be read yet.
    """
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            return None
    if not _complete(panes) or not ensure_converted(panes):
        return None
    live_keys = {ref.key for ref in panes}
    with _lock:
        if _stamp_gone_locked(live_keys, time.time()):
            _save_locked()
        rows = []
        for key, e in _drafts.items():
            reason = _orphan_reason(key, e, live_keys)
            if reason is None:
                continue
            rows.append({
                "key": key,
                "reason": reason,
                "text": e["text"],
                "attachments": copy.deepcopy(e["attachments"]),
                "enter_armed": e["enter_armed"],
                "updated_at": e["updated_at"],
                "orphaned_at": e.get("orphaned_at"),
                "legacy_from": e.get("legacy_from"),
                "copied_at": e.get("copied_at"),
            })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows


def _orphan_locked(source, live_keys):
    """(entry, None) for an orphan, else (None, error). Caller holds _lock."""
    src = _drafts.get(source)
    if src is None:
        return None, "not_found"
    if _orphan_reason(source, src, live_keys) is None:
        return None, "not_orphan"
    return src, None


def copy_orphan(source, target, panes=None):
    """The one way an orphan reaches a pane: an explicit "Copy to this pane".

    Returns (entry, None) or (None, error). Refused, with nothing changed, when
    `source` is not an orphan, the target names no live pane, the target
    already holds a draft with content (never overwritten), or an upload file
    could not be made (`storage_full` / `attachment_copy_failed`, retryable). The target's Enter
    lock is kept; the text and tray come from the orphan, and each upload gets
    the destination's own file, made before the metadata naming it is saved,
    so dismissing or expiring either draft never unlinks the other's.

    The orphan stays exactly as it was (content, `updated_at`, `orphaned_at`),
    marked `copied_to`/`copied_at`, until it is dismissed or expires: whatever
    later happens to the destination (a stale save, another device, a clock
    step), the text is never lost (sign-off M3 R3).
    """
    global _drafts
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            return None, "unavailable"
    if not _complete(panes) or not ensure_converted(panes):
        return None, "unavailable"
    live_keys = {ref.key for ref in panes}
    dest_key = _resolve(target, panes)
    if dest_key is None or dest_key not in live_keys:
        return None, "target_absent"
    with _lock:
        src, error = _orphan_locked(source, live_keys)
        if error:
            return None, error
        dest = _drafts.get(dest_key)
        if dest is not None and has_content(dest):
            return None, "destination_has_draft"
        attachments = []
        try:
            for att in src["attachments"]:
                if _is_sweepable_upload(att["path"]):
                    own = _owned_copy(att["path"])
                    if own is None:
                        log.warning("drafts: orphan %s's upload %s is gone; left out of the copy",
                                    source, att["path"])
                        continue
                    att = {**att, "path": own}
                attachments.append(dict(att))
        except AttachmentCopyFailed as exc:
            # Retryable: nothing written, and no half-made file left referenced.
            for att in attachments:
                if att["path"] not in {a["path"] for a in src["attachments"]}:
                    _discard(att["path"])
            log.warning("drafts: copying orphan %s failed (%s); nothing written", source, exc)
            return None, exc.error
        now = time.time()
        copied = {
            "text": src["text"],
            "attachments": attachments,
            "enter_armed": dest["enter_armed"] if dest is not None else src["enter_armed"],
            "updated_at": now,
            "copied_from": source,
        }
        _drafts[dest_key] = copied
        src["copied_to"] = dest_key
        src["copied_at"] = now
        _save_locked()
        return copy.deepcopy(copied), None


def dismiss_orphan(source, panes=None):
    """Delete one orphan for good: the orphan sheet's Dismiss.

    Returns (removed_paths, None) or (None, error).
    Only an orphan can be dismissed, and only its own upload files are
    unlinked: none another draft names, none drafts.json references.
    """
    global _drafts
    if panes is None:
        panes, ok = _live_panes()
        if not ok:
            return None, "unavailable"
    if not _complete(panes) or not ensure_converted(panes):
        return None, "unavailable"
    live_keys = {ref.key for ref in panes}
    protected = _legacy_paths | _attachment_paths(_read_legacy_quiet())
    with _lock:
        src, error = _orphan_locked(source, live_keys)
        if error:
            return None, error
        _drafts.pop(source)
        candidates = {a["path"] for a in src["attachments"]}
        still_used = _attachment_paths(_drafts) | protected
        _save_locked()
    return _unlink_uploads(candidates - still_used), None


def rename_session(old, new):
    """Keep drafts across a tmux session rename.

    A durable key carries no session name, so a canonical map needs nothing,
    and an orphan is never re-attached by name. Only a map not yet converted
    is re-keyed by its legacy names (tmux has already renamed the session when
    this runs), then converted against the live list, so a rename before the
    first draft read still lands every draft on its pane.
    """
    global _drafts
    if not old or not new or old == new:
        return
    old_prefix = old + ":"
    new_prefix = new + ":"
    with _lock:
        if _converted:
            return
        renamed = {}
        changed = False
        for target, entry in _drafts.items():
            if target.startswith(old_prefix):
                renamed[new_prefix + target[len(old_prefix):]] = entry
                changed = True
            else:
                renamed[target] = entry
        if changed:
            _drafts = renamed
    ensure_converted()


def _is_sweepable_upload(path):
    """Only ever unlink files this app wrote into /tmp (routes/input.py)."""
    try:
        p = Path(path)
    except (TypeError, ValueError):
        return False
    return (
        p.is_absolute()
        and p.parent == _UPLOAD_DIR
        and p.name.startswith(_UPLOAD_PREFIX)
    )


def sweep_expired(max_age_sec=None, now=None):
    """Drop drafts untouched for max_age_sec and unlink their orphaned uploads.

    An upload is only removed once NO surviving draft still references it — two
    drafts can legitimately carry the same path (the same file attached twice),
    and deleting one must not break the other.

    An orphan's clock starts when it was orphaned (sign-off M3 F6): at the
    conversion for a legacy name, and for a durable key when a complete,
    successful listing first showed its pane gone (stamped here too, so a
    pane that died since the last poll is stamped rather than expired). With
    no complete listing the sweep cannot tell a newly dead key from a live
    one, so it leaves unstamped durable keys alone.

    Returns (dropped_targets, removed_paths) for logging and tests.
    """
    global _drafts
    max_age = EXPIRY_SEC if max_age_sec is None else max_age_sec
    now = time.time() if now is None else now
    panes, listed = _live_panes()
    # Never unlink a file drafts.json references (it belongs to the pre-7B
    # code): what it held at load, and what it holds now.
    protected = _legacy_paths | _attachment_paths(_read_legacy_quiet())

    with _lock:
        if not _converted:
            return [], []
        stamped = listed and _stamp_gone_locked({ref.key for ref in panes}, now)
        expired = [
            t for t, e in _drafts.items()
            if (listed or "orphaned_at" in e or not tmux.parse_pane_key(t))
            and (now - max(e.get("updated_at", 0.0), e.get("orphaned_at") or 0.0)) > max_age
        ]
        if not expired:
            if stamped:
                _save_locked()
            return [], []
        orphan_candidates = set()
        for target in expired:
            for att in _drafts[target]["attachments"]:
                orphan_candidates.add(att["path"])
            _drafts.pop(target, None)
        still_used = {
            att["path"] for e in _drafts.values() for att in e["attachments"]
        } | protected
        _save_locked()

    return expired, _unlink_uploads(orphan_candidates - still_used)


def _unlink_uploads(paths):
    """Unlink each of our /tmp uploads in `paths` (nothing else); the ones removed."""
    removed = []
    for path in sorted(paths):
        if not _is_sweepable_upload(path):
            continue
        try:
            Path(path).unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


load_drafts()
