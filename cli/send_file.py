"""`assist send-file`: offer a file to the signed-in browsers and wait for pickup.

The bytes go to the server (routes/outbox.py), never the path, so the server
does not follow a path an agent chose. Every signed-in browser shows the offer;
the first download takes it.
"""

import json
import os
import shutil
import sys
import tempfile
import time

from cli import http

POLL_INTERVAL = 1.0

EXIT_PICKED_UP = 0
EXIT_ERROR = 1
EXIT_REFUSED = 2
EXIT_EXPIRED = 20
EXIT_DISMISSED = 21
EXIT_WAITING = 75

_STATE_EXIT = {
    "picked_up": EXIT_PICKED_UP,
    "expired": EXIT_EXPIRED,
    "dismissed": EXIT_DISMISSED,
    "pending": EXIT_WAITING,
}


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def _hint(text: str) -> None:
    if os.environ.get("ASSIST_NO_HINTS") != "1":
        print(f"next: {text}", file=sys.stderr)


def _error_text(error: http.ApiError) -> str:
    try:
        return json.loads(error.body).get("error") or str(error)
    except (ValueError, AttributeError):
        return str(error)


def _report(offer: dict) -> int:
    """Print an ended or still-pending offer's outcome; return its exit code."""
    state = offer.get("state")
    name = offer.get("name", "file")
    if state == "picked_up":
        print(f"picked up by {offer.get('picked_up_by') or 'a browser'} at {_clock(offer['picked_up_at'])}"
              " · link closed, server copy deleted")
    elif state == "expired":
        print(f"{name} expired unclaimed · server copy deleted")
    elif state == "dismissed":
        print(f"{name} was dismissed · server copy deleted")
    else:
        print(f"{name} is still waiting · expires {_clock(offer['expires_at'])}")
        _hint(f"assist send-file --status {offer['id']}")
    return _STATE_EXIT.get(state, EXIT_ERROR)


def _status(offer_id: str) -> dict | None:
    try:
        return http.get(f"/api/outbox/{offer_id}")["offer"]
    except http.ApiError as error:
        if error.status == 404:
            return None
        raise


def _gone(offer_id: str) -> int:
    print(f"assist: no offer {offer_id} (it ended over an hour ago, or the server restarted)",
          file=sys.stderr)
    return EXIT_EXPIRED


def _wait(offer: dict, timeout: float | None) -> int:
    deadline = None if timeout is None else time.monotonic() + timeout
    print("waiting for pickup…  (--no-wait to return now)", file=sys.stderr)
    while True:
        current = _status(offer["id"])
        if current is None:
            return _gone(offer["id"])
        if current["state"] != "pending":
            return _report(current)
        if deadline is not None and time.monotonic() >= deadline:
            return _report(current)
        time.sleep(POLL_INTERVAL)


def _offer(path: str, name: str | None, no_wait: bool, timeout: float | None) -> int:
    if not os.path.exists(path):
        print(f"assist: no such file: {path}", file=sys.stderr)
        return EXIT_REFUSED
    if not os.path.isfile(path):
        print(f"assist: {path} is not a regular file", file=sys.stderr)
        return EXIT_REFUSED
    try:
        handle = open(path, "rb")
    except OSError as exc:
        print(f"assist: cannot read {path}: {exc.strerror}", file=sys.stderr)
        return EXIT_REFUSED
    with handle:
        limits = http.get("/api/outbox")
        # Upload a private snapshot, not the live file: a file still being
        # written would change size between the Content-Length and the body.
        # Read at most one byte past the cap, so an oversized file is caught
        # without copying all of it. The anonymous temp file has no name.
        snapshot = tempfile.TemporaryFile()
        shutil.copyfileobj(_bounded(handle, limits["max_bytes"] + 1), snapshot)
        size = snapshot.tell()
        snapshot.seek(0)
    with snapshot:
        if size > limits["max_bytes"]:
            print(f"assist: {path} is {human_size(size)}; the limit is {human_size(limits['max_bytes'])}",
                  file=sys.stderr)
            return EXIT_REFUSED
        if limits["pending"] >= limits["max_pending"]:
            print(f"assist: {limits['max_pending']} files are already waiting; "
                  "pick one up or dismiss it first", file=sys.stderr)
            return EXIT_REFUSED
        display = name or os.path.basename(path)
        fields = {"name": display, "pane": os.environ.get("TMUX_PANE", "")}
        try:
            response = http.post_multipart("/api/outbox", fields, snapshot, size)
        except http.ApiError as error:
            if error.status in (413, 429):
                print(f"assist: {_error_text(error)}", file=sys.stderr)
                return EXIT_REFUSED
            raise
    offer = response["offer"]
    browsers = response.get("browsers", 0)
    noun = "browser" if browsers == 1 else "browsers"
    print(f"offered {offer['name']} ({human_size(offer['size'])}) to {browsers} signed-in {noun}"
          f" · expires {_clock(offer['expires_at'])} · id {offer['id']}")
    if no_wait:
        _hint(f"assist send-file --status {offer['id']}")
        return EXIT_PICKED_UP
    return _wait(offer, timeout)


class _bounded:
    """A read-only view of `stream` that ends after `limit` bytes."""

    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.left = limit

    def read(self, size: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        size = self.left if size is None or size < 0 else min(size, self.left)
        chunk = self.stream.read(size)
        self.left -= len(chunk)
        return chunk


def _cancel(offer_id: str) -> int:
    try:
        http.post(f"/api/outbox/{offer_id}/dismiss")
    except http.ApiError as error:
        if error.status == 404:
            return _gone(offer_id)
        if error.status != 410:
            raise
        return _report(_status(offer_id) or {"id": offer_id, "state": "expired"})
    print("cancelled · server copy deleted")
    return EXIT_DISMISSED


def command(
    path: str | None,
    name: str | None,
    no_wait: bool,
    timeout: float | None,
    status_id: str | None,
    cancel_id: str | None,
) -> int:
    try:
        if status_id:
            offer = _status(status_id)
            return _gone(status_id) if offer is None else _report(offer)
        if cancel_id:
            return _cancel(cancel_id)
        if not path:
            print("assist: send-file needs a PATH (or --status ID / --cancel ID)", file=sys.stderr)
            return EXIT_REFUSED
        return _offer(path, name, no_wait, timeout)
    except http.ApiError as error:
        return http.report_error(error)
    except KeyboardInterrupt:
        print("\nstopped waiting; the offer is still open"
              " (assist send-file --cancel ID withdraws it)", file=sys.stderr)
        return EXIT_WAITING
