"""Per-device browser sign-in: the registry behind the v2 cookie.

Every browser that signs in — token at /login, the open-access window (or
`assist pair`, which opens it), or an approved device request — becomes one
record here, and its cookie names that record (shared/auth.py:device_cookie).
Revoking a record stops that one browser; scripts on the header token are not
devices and are never affected.

`devices.json` (0600) holds no secret: the cookie's mac is keyed by the token,
so a record id alone cannot be turned into a cookie.

Fail closed. A registry that cannot be read or parsed is never treated as
"no devices yet": the invalid bytes are copied aside first, then a durable
recovery marker replaces the original, and until `assist devices reset` (header
token) every cookie is refused and no device is minted. A missing file is the
one case that means empty — the first start, which is also the one-time cut
from the old shared cookie.

One lock serialises admission, revocation, reset, touch and stream
registration. Admission takes it first and then, inside, at most one of the
shared.auth locks; nothing in shared.auth reaches back for this one.
"""

import copy
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import shared.auth as auth

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_ACTIVE = 32
# Revoked rows are kept so the owner can see them, up to this many records in
# all; the oldest revoked row is dropped to make room. Active rows never are.
MAX_RECORDS = 64
NAME_MAX = 40
# last_seen is advisory; writing it on every request would rewrite the file
# several times a second while a phone polls.
TOUCH_INTERVAL = 300
SOURCES = ("login", "window", "approval", "pair")

# Overridden by tests; resolved lazily so importing this module reads nothing.
DEVICES_FILE = None

_lock = threading.Lock()
_registry = None          # the in-memory copy of devices.json, or None until loaded
_recovery = False         # True: cookies refused, nothing minted, until reset
_streams = {}             # id(client) -> (device_id | None, client)


class DeviceError(Exception):
    """A device operation was refused. `code` is the machine-readable reason."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TokenLogin:
    """The token typed at /login (already checked by the caller)."""


@dataclass(frozen=True)
class Window:
    """A document navigation on the open-access window."""


@dataclass(frozen=True)
class ApprovalClaim:
    """A waiting browser's claim on an approval request."""

    claim: str


@dataclass(frozen=True)
class AdmitRefused:
    """No device was minted. `reason` says why; nothing was consumed."""

    reason: str
    remaining_sec: int = 0


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------


def _path():
    if DEVICES_FILE is not None:
        return Path(DEVICES_FILE)
    from shared import paths

    return paths.data_dir() / "devices.json"


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_file(path, data):
    """Durable replace: temp file (0600) + fsync + rename + directory fsync."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            json.dump(data, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
    _fsync_dir(path.parent)


def _backup_invalid(path, raw):
    """Copy invalid registry bytes beside it, durably. The original stays."""
    stamp = int(time.time())
    for n in range(100):
        suffix = f".corrupt-{stamp}" + (f"-{n}" if n else "")
        target = path.with_name(path.name + suffix)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        _fsync_dir(path.parent)
        return target
    raise OSError(f"no free quarantine name beside {path}")


def _empty():
    return {"schema": SCHEMA, "devices": []}


def _valid(data):
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return False
    rows = data.get("devices")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            return False
    return isinstance(data.get("recovery_required", False), bool)


def _load_locked():
    """Read the registry into memory. Caller holds _lock."""
    global _registry, _recovery
    path = _path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        _registry, _recovery = _empty(), False
        return
    except OSError as exc:
        # Unreadable is not empty. Nothing is written: the file may be fine
        # and merely inaccessible to this process right now.
        log.error("devices: cannot read %s (%s); cookies refused until reset", path, exc)
        _registry, _recovery = _empty(), True
        return
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        data = None
    if _valid(data):
        _registry = data
        _recovery = bool(data.get("recovery_required"))
        if _recovery:
            log.error("devices: registry quarantined; run `assist devices reset`")
        return
    # Invalid: back the bytes up first, then replace the original with the
    # durable recovery marker. Any failure leaves the invalid file in place,
    # which the next start treats the same way — never a fresh registry.
    _registry, _recovery = _empty(), True
    try:
        backup = _backup_invalid(path, raw)
        _write_file(path, {"schema": SCHEMA, "recovery_required": True, "devices": []})
        log.error("devices: invalid registry moved to %s; cookies refused until reset", backup)
    except OSError as exc:
        log.error("devices: quarantine of %s incomplete (%s); cookies refused", path, exc)


def _ensure_loaded_locked():
    if _registry is None:
        _load_locked()


def _commit_locked(data):
    """Write `data` durably, then adopt it. OSError leaves memory unchanged."""
    global _registry
    _write_file(_path(), data)
    _registry = data


def reload():
    """Forget the in-memory registry and streams and read the file again.

    A restart, as far as this module is concerned.
    """
    global _registry, _recovery
    with _lock:
        _registry = None
        _recovery = False
        _streams.clear()
        _load_locked()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _find(data, device_id):
    for row in data["devices"]:
        if row["id"] == device_id:
            return row
    return None


def _active(row):
    return row is not None and not row.get("revoked_at")


def recovery_required():
    with _lock:
        _ensure_loaded_locked()
        return _recovery


def list_devices():
    with _lock:
        _ensure_loaded_locked()
        return copy.deepcopy(_registry["devices"])


def get(device_id):
    with _lock:
        _ensure_loaded_locked()
        row = _find(_registry, device_id)
        return copy.deepcopy(row) if row else None


def authenticate(cookie_value, ip=None):
    """The active device a cookie names, or None. Touches last_seen (throttled)."""
    parsed = auth.parse_device_cookie(cookie_value)
    if parsed is None:
        return None
    device_id = parsed[0]
    with _lock:
        _ensure_loaded_locked()
        if _recovery:
            return None
        row = _find(_registry, device_id)
        if not _active(row):
            return None
        _touch_locked(device_id, ip)
        return copy.deepcopy(_find(_registry, device_id))


def _touch_locked(device_id, ip):
    """Advance last_seen if it is stale. Re-reads the current registry, so a
    touch for a row revoked or reset meanwhile is simply dropped."""
    row = _find(_registry, device_id)
    if not _active(row):
        return
    now = time.time()
    if now - float(row.get("last_seen") or 0) < TOUCH_INTERVAL:
        return
    data = copy.deepcopy(_registry)
    fresh = _find(data, device_id)
    fresh["last_seen"] = now
    if ip:
        fresh["ip"] = ip
    try:
        _commit_locked(data)
    except OSError as exc:
        log.warning("devices: last_seen not written (%s)", exc)


def touch(device_id, ip=None):
    with _lock:
        _ensure_loaded_locked()
        if not _recovery:
            _touch_locked(device_id, ip)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"\s+")


def clean_name(name):
    """A label the owner typed: control characters out, whitespace folded,
    clamped to NAME_MAX. None when nothing printable is left."""
    if not isinstance(name, str):
        return None
    kept = "".join(
        ch for ch in name if not unicodedata.category(ch).startswith("C") or ch in " \t"
    )
    kept = _CONTROL.sub(" ", kept).strip()
    return kept[:NAME_MAX] or None


def ua_short(ua):
    """A default name from the self-reported User-Agent, e.g. "Android · Chrome"."""
    ua = ua or ""
    platform = next(
        (label for needle, label in (
            ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
            ("Macintosh", "Mac"), ("Windows", "Windows"), ("CrOS", "ChromeOS"),
            ("Linux", "Linux"),
        ) if needle in ua),
        None,
    )
    browser = next(
        (label for needle, label in (
            ("Edg/", "Edge"), ("Firefox/", "Firefox"), ("CriOS", "Chrome"),
            ("Chrome/", "Chrome"), ("Safari/", "Safari"),
        ) if needle in ua),
        None,
    )
    parts = [p for p in (platform, browser) if p]
    return clean_name(" · ".join(parts)) or "Browser"


def _new_record(source, ip, ua):
    now = time.time()
    return {
        "id": "d_" + secrets.token_hex(16),
        "name": ua_short(ua),
        "named": False,
        "created": now,
        "last_seen": now,
        "ip": ip or "",
        "ua": (ua or "")[:300],
        "source": source,
        "revoked_at": None,
    }


def _with_record(record):
    """A copy of the registry with `record` added, oldest revoked rows pruned
    to stay within MAX_RECORDS."""
    data = copy.deepcopy(_registry)
    data.pop("recovery_required", None)
    data["devices"].append(record)
    while len(data["devices"]) > MAX_RECORDS:
        revoked = [r for r in data["devices"] if r.get("revoked_at")]
        if not revoked:
            break
        oldest = min(revoked, key=lambda r: r.get("revoked_at") or 0)
        data["devices"].remove(oldest)
    return data


def _active_count():
    return sum(1 for r in _registry["devices"] if _active(r))


def admit(admission, ip="", ua=""):
    """Mint one device for one admission, as a single transaction.

    1. look the admission up (and replay a redeemed claim) — before any cap;
    2. refuse a fresh admission at MAX_ACTIVE active devices;
    3. reserve the admission under its shared.auth lock;
    4. write the record durably (failure: release, the admission is untouched);
    5. consume the admission;
    and the caller sets the cookie. Returns the record, or AdmitRefused.
    """
    with _lock:
        _ensure_loaded_locked()
        if _recovery:
            return AdmitRefused("recovery_required")

        rid = None
        if isinstance(admission, TokenLogin):
            source = "login"
        elif isinstance(admission, Window):
            if not auth.window_admissible(ip):
                return AdmitRefused("closed")
            source = None  # known once reserved
        elif isinstance(admission, ApprovalClaim):
            info = auth.lookup_claim(admission.claim, ip)
            if info is None or info["reserved"]:
                return AdmitRefused("expired")
            if info["redeemed_by"]:
                row = _find(_registry, info["redeemed_by"])
                if _active(row):
                    return copy.deepcopy(row)
                return AdmitRefused("expired")
            if info["status"] != "approved":
                return AdmitRefused(info["status"], info["remaining_sec"])
            rid = info["id"]
            source = "approval"
        else:
            raise TypeError(f"unknown admission {admission!r}")

        if _active_count() >= MAX_ACTIVE:
            return AdmitRefused("device_cap")

        reservation = None
        if isinstance(admission, Window):
            held = auth.reserve_window(ip)
            if held is None:
                return AdmitRefused("closed")
            reservation, source = held
        elif rid is not None and not auth.reserve_claim(rid):
            return AdmitRefused("expired")

        record = _new_record(source, ip, ua)
        try:
            _commit_locked(_with_record(record))
        except OSError as exc:
            log.error("devices: record not written (%s); admission left as it was", exc)
            if reservation is not None:
                auth.release_window_reservation(reservation)
            elif rid is not None:
                auth.release_claim(rid)
            return AdmitRefused("write_failed")

        if reservation is not None:
            auth.consume_reserved_window(reservation, ip, ua)
        elif rid is not None:
            auth.redeem_claim(rid, record["id"])
        print(f"[assist] device {record['id']} signed in via {source} from {ip}", flush=True)
        return copy.deepcopy(record)


# ---------------------------------------------------------------------------
# Owner actions
# ---------------------------------------------------------------------------


def _mutate(device_id, change):
    """Apply `change(row)` to a copy, write it durably, adopt it."""
    data = copy.deepcopy(_registry)
    row = _find(data, device_id)
    if row is None:
        raise DeviceError("not_found")
    change(row)
    try:
        _commit_locked(data)
    except OSError as exc:
        log.error("devices: write failed (%s)", exc)
        raise DeviceError("write_failed") from exc


def _guard_writable():
    _ensure_loaded_locked()
    if _recovery:
        raise DeviceError("recovery_required")


def rename(device_id, name):
    cleaned = clean_name(name)
    if cleaned is None:
        raise DeviceError("bad_name")
    with _lock:
        _guard_writable()

        def change(row):
            row["name"] = cleaned
            row["named"] = True

        _mutate(device_id, change)


def revoke(device_id, confirm_self=False, caller_id=None):
    """Revoke one device: written first, then its streams are closed."""
    with _lock:
        _guard_writable()
        if device_id == caller_id and not confirm_self:
            raise DeviceError("self_requires_confirm")
        row = _find(_registry, device_id)
        if row is None:
            raise DeviceError("not_found")
        if not row.get("revoked_at"):
            _mutate(device_id, lambda r: r.__setitem__("revoked_at", time.time()))
        _close_streams_locked(lambda did: did == device_id)
    print(f"[assist] device {device_id} revoked", flush=True)


def forget(device_id):
    """Drop a revoked row from the list. Active rows must be revoked first."""
    with _lock:
        _guard_writable()
        row = _find(_registry, device_id)
        if row is None:
            raise DeviceError("not_found")
        if not row.get("revoked_at"):
            raise DeviceError("not_revoked")
        data = copy.deepcopy(_registry)
        data["devices"] = [r for r in data["devices"] if r["id"] != device_id]
        try:
            _commit_locked(data)
        except OSError as exc:
            raise DeviceError("write_failed") from exc


def reset():
    """Bulk browser revocation, and the only way out of recovery mode.

    Writes an empty registry durably, clears recovery_required, closes every
    device-authenticated stream and leaves header-authenticated streams open.
    """
    global _recovery
    with _lock:
        _ensure_loaded_locked()
        try:
            _commit_locked(_empty())
        except OSError as exc:
            raise DeviceError("write_failed") from exc
        _recovery = False
        _close_streams_locked(lambda did: did is not None)
    print("[assist] device registry reset; every browser signs in again", flush=True)


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------


def register_stream(device_id, client, add):
    """Register a WebSocket client, under the devices lock.

    `add()` puts it into state.ws_clients. Refused (False, nothing added) when
    the device was revoked or reset between the handshake and now. A header-
    authenticated socket passes None and is never closed by a revoke.
    """
    with _lock:
        if device_id is not None:
            _ensure_loaded_locked()
            if _recovery or not _active(_find(_registry, device_id)):
                return False
        add()
        _streams[id(client)] = (device_id, client)
        return True


def unregister_stream(client):
    with _lock:
        _streams.pop(id(client), None)


def stream_device_active(device_id):
    """Re-check before a first frame: True for header sockets (None)."""
    if device_id is None:
        return True
    with _lock:
        _ensure_loaded_locked()
        return not _recovery and _active(_find(_registry, device_id))


def _close_streams_locked(match):
    from shared import state

    doomed = [(key, client) for key, (did, client) in _streams.items() if match(did)]
    for key, client in doomed:
        _streams.pop(key, None)
        with state.ws_lock:
            state.ws_clients[:] = [c for c in state.ws_clients if c is not client]
        try:
            client["ws"].close()
        except Exception:
            pass
