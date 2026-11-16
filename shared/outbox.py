"""shared/outbox.py — files an agent has offered to the signed-in browsers.

`assist send-file` uploads a file's bytes; every signed-in browser sees the
offer on /poll, and the first download takes it. An offer ends on that
download, a dismiss, or EXPIRY_SECONDS, and its file is deleted when it ends.

Records live in memory only and the directory is emptied at server start, so
no offer survives a restart: a key file never outlives the process that was
asked to hand it over. Files are stored by offer id; the display name never
touches the filesystem.
"""

import logging
import mimetypes
import os
import re
import secrets
import threading
import time

from shared import paths

log = logging.getLogger(__name__)

EXPIRY_SECONDS = int(os.environ.get("ASSIST_OUTBOX_EXPIRY_SECONDS", "900"))
MAX_PENDING = 10
_CHUNK = 1024 * 1024
_HEAD = 4096
_KEEP_ENDED = 3600

# Names an SSH/TLS key usually goes by. Display only: it adds a warning line.
_SECRET_NAME = re.compile(r"^id_[a-z0-9_]+$|\.(pem|key|p12|pfx|ppk)$", re.IGNORECASE)

_lock = threading.Lock()
_offers = {}


class OutboxFull(Exception):
    pass


class OutboxTooLarge(Exception):
    pass


def outbox_dir():
    path = paths.state_dir() / "outbox"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def secret_hint(name, head):
    if _SECRET_NAME.search(name or ""):
        return True
    return b"PRIVATE KEY-----" in head[:_HEAD]


def _public(offer):
    return {k: v for k, v in offer.items() if not k.startswith("_")}


def _remove_file(offer_id):
    try:
        (outbox_dir() / offer_id).unlink()
    except FileNotFoundError:
        pass


def _expire_locked(offer, now):
    if offer["state"] == "pending" and now >= offer["expires_at"]:
        offer["state"] = "expired"
        _remove_file(offer["id"])
        log.info("outbox: %s (%d B) from %s expired", offer["name"], offer["size"], offer["sender"])
        return True
    return False


def reset():
    """Forget every offer and empty the directory (server start, tests)."""
    with _lock:
        _offers.clear()
        for entry in outbox_dir().iterdir():
            try:
                entry.unlink()
            except OSError:
                pass


def create(stream, name, sender, limit, now=None):
    """Stream `stream` into a new pending offer. Raises OutboxFull or OutboxTooLarge."""
    now = time.time() if now is None else now
    with _lock:
        for offer in _offers.values():
            _expire_locked(offer, now)
        if sum(1 for o in _offers.values() if o["state"] == "pending") >= MAX_PENDING:
            raise OutboxFull()
    offer_id = secrets.token_urlsafe(16)
    directory = outbox_dir()
    part = directory / f"{offer_id}.part"
    written = 0
    head = b""
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = stream.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise OutboxTooLarge()
                if len(head) < _HEAD:
                    head += chunk[:_HEAD - len(head)]
                out.write(chunk)
        os.replace(part, directory / offer_id)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    offer = {
        "id": offer_id,
        "name": name,
        "size": written,
        "mime": mimetypes.guess_type(name)[0] or "application/octet-stream",
        "sender": sender,
        "created": now,
        "expires_at": now + EXPIRY_SECONDS,
        "state": "pending",
        "secret_hint": secret_hint(name, head),
        "picked_up_by": None,
        "picked_up_at": None,
    }
    with _lock:
        if sum(1 for o in _offers.values() if o["state"] == "pending") >= MAX_PENDING:
            _remove_file(offer_id)
            raise OutboxFull()
        _offers[offer_id] = offer
    log.info("outbox: %s (%d B) offered by %s", name, written, sender)
    return _public(offer)


def get(offer_id, now=None):
    now = time.time() if now is None else now
    with _lock:
        offer = _offers.get(offer_id)
        if offer is None:
            return None
        _expire_locked(offer, now)
        return _public(offer)


def pending(now=None):
    now = time.time() if now is None else now
    with _lock:
        live = [o for o in _offers.values() if not _expire_locked(o, now) and o["state"] == "pending"]
        return [_public(o) for o in sorted(live, key=lambda o: o["created"])]


def claim(offer_id, device_name, now=None):
    """Take a pending offer for one download: an open binary handle, or None.

    The file is opened under the same lock that marks the pickup, so nothing
    (the sweep, a dismiss) can unlink it between the claim and the open; once
    open, an unlink no longer touches the bytes being served. Pickup is only
    recorded after the open succeeds.
    """
    now = time.time() if now is None else now
    with _lock:
        offer = _offers.get(offer_id)
        if offer is None or _expire_locked(offer, now) or offer["state"] != "pending":
            return None
        try:
            handle = (outbox_dir() / offer_id).open("rb")
        except OSError:
            return None
        offer["state"] = "picked_up"
        offer["picked_up_by"] = device_name
        offer["picked_up_at"] = now
    log.info("outbox: %s (%d B) from %s picked up by %s",
             offer["name"], offer["size"], offer["sender"], device_name)
    return handle


def discard_file(offer_id):
    _remove_file(offer_id)


def dismiss(offer_id):
    with _lock:
        offer = _offers.get(offer_id)
        if offer is None or offer["state"] != "pending":
            return False
        offer["state"] = "dismissed"
        _remove_file(offer_id)
    log.info("outbox: %s (%d B) from %s dismissed", offer["name"], offer["size"], offer["sender"])
    return True


def sweep(now=None):
    """Expire overdue offers and delete their files. The number expired.

    An ended record is kept for an hour past its expiry so a late
    `send-file --status` can still read the outcome, then forgotten.
    """
    now = time.time() if now is None else now
    with _lock:
        expired = sum(1 for offer in _offers.values() if _expire_locked(offer, now))
        # Belt for a download whose stream never reached its cleanup.
        for offer in _offers.values():
            if offer["state"] == "picked_up":
                _remove_file(offer["id"])
        for offer_id in [i for i, o in _offers.items()
                         if o["state"] != "pending" and now > o["expires_at"] + _KEEP_ENDED]:
            del _offers[offer_id]
        return expired
