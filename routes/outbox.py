"""routes/outbox.py — files an agent offers to the signed-in browsers.

POST   /api/outbox                 the CLI uploads a file (header token only)
GET    /api/outbox/<id>            offer status, for `assist send-file` waiting
GET    /api/outbox/<id>/download   one download; every later request is 410
POST   /api/outbox/<id>/dismiss    end the offer without downloading

The store and its rules are shared/outbox.py. /poll lists pending offers.
"""

import re
import subprocess
import threading
import time
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

import shared.auth as auth
import shared.state as state
from shared import agent_identity, devices, outbox
from shared.tmux import tmux_exact_target

outbox_bp = Blueprint("outbox_bp", __name__)

_PANE_ID = re.compile(r"^%\d+$")
_CHUNK = 1024 * 1024


def describe_sender(pane_id):
    """`<agent> · <session>` for the pane that ran send-file, or "shell"."""
    if not pane_id or not _PANE_ID.match(pane_id):
        return "shell"
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", tmux_exact_target(pane_id),
             "#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_pid}\t#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "shell"
    parts = proc.stdout.rstrip("\n").split("\t")
    if proc.returncode != 0 or len(parts) != 6 or parts[0] != pane_id:
        return "shell"
    _, session, window, pane, pid, command = parts
    try:
        kind = agent_identity.resolve(f"{session}:{window}.{pane}", int(pid), command)
    except (ValueError, OSError):
        kind = "unknown"
    return f"{kind if kind and kind != 'unknown' else 'shell'} · {session}"


def _picker_name():
    device = auth.request_device(request)
    return device["name"] if device else "script"


def _active_browsers():
    try:
        return sum(1 for d in devices.list_devices() if not d.get("revoked_at"))
    except Exception:
        return 0


def _ended(offer_id):
    offer = outbox.get(offer_id)
    if offer is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": False, "error": offer["state"]}), 410


def content_disposition(name):
    """`attachment` with an ASCII fallback and the exact UTF-8 name (RFC 6266)."""
    fallback = re.sub(r'[^A-Za-z0-9._-]', "_", name).strip(".") or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def poll_block():
    """The pending offers /poll hands every browser."""
    return [
        {k: offer[k] for k in ("id", "name", "size", "sender", "expires_at", "secret_hint")}
        for offer in outbox.pending()
    ]


@outbox_bp.route("/api/outbox", methods=["POST"])
def create_offer():
    # An agent action: a browser cookie may not push files to other browsers.
    if not auth.header_authenticated(request):
        return jsonify({"ok": False, "error": "header_token_required"}), 403
    f = request.files.get("file")
    if f is None:
        return jsonify({"ok": False, "error": "No file provided"}), 400
    name = (request.form.get("name") or f.filename or "").strip()
    name = "".join(ch for ch in name if ch.isprintable())[:255]
    if not name:
        return jsonify({"ok": False, "error": "No filename"}), 400
    limit = state.MAX_UPLOAD_SIZE
    too_large = f"File too large ({limit // (1024 * 1024)}MB max)"
    if request.content_length and request.content_length > limit + 64 * 1024:
        return jsonify({"ok": False, "error": too_large}), 413
    sender = describe_sender(request.form.get("pane", ""))
    try:
        offer = outbox.create(f.stream, name, sender, limit)
    except outbox.OutboxTooLarge:
        return jsonify({"ok": False, "error": too_large}), 413
    except outbox.OutboxFull:
        return jsonify({
            "ok": False,
            "error": f"{outbox.MAX_PENDING} files are already waiting; pick one up or dismiss it first",
        }), 429
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Write failed: {exc.strerror}"}), 500
    return jsonify({"ok": True, "offer": offer, "browsers": _active_browsers()}), 201


@outbox_bp.route("/api/outbox", methods=["GET"])
def outbox_limits():
    """What the CLI checks before it uploads anything."""
    return jsonify({
        "ok": True,
        "max_bytes": state.MAX_UPLOAD_SIZE,
        "max_pending": outbox.MAX_PENDING,
        "pending": len(outbox.pending()),
    })


@outbox_bp.route("/api/outbox/<offer_id>", methods=["GET"])
def offer_status(offer_id):
    offer = outbox.get(offer_id)
    if offer is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    return jsonify({"ok": True, "offer": offer})


@outbox_bp.route("/api/outbox/<offer_id>/download", methods=["GET"])
def download_offer(offer_id):
    # Flask answers HEAD with this view; a header probe must not use up the
    # one download.
    if request.method == "HEAD":
        return jsonify({"ok": False, "error": "method_not_allowed"}), 405, {"Allow": "GET"}
    offer = outbox.get(offer_id)
    handle = outbox.claim(offer_id, _picker_name())
    if handle is None:
        return _ended(offer_id)

    # Claimed before the first byte: a download cut off mid-stream still ends
    # the offer, and the file goes whether the phone got all of it or not.
    def stream():
        try:
            while True:
                chunk = handle.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
        finally:
            handle.close()
            outbox.discard_file(offer_id)

    response = Response(stream(), mimetype=offer["mime"])
    # A generator closed before its first chunk never runs its finally.
    response.call_on_close(lambda: (handle.close(), outbox.discard_file(offer_id)))
    response.headers["Content-Disposition"] = content_disposition(offer["name"])
    response.headers["Content-Length"] = str(offer["size"])
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@outbox_bp.route("/api/outbox/<offer_id>/dismiss", methods=["POST"])
def dismiss_offer(offer_id):
    if outbox.dismiss(offer_id):
        return jsonify({"ok": True})
    return _ended(offer_id)


def outbox_sweeper():
    """Background pass: expire overdue offers and delete their files.

    Sleeps FIRST, like the drafts sweeper. /api/outbox/<id> and /poll also
    expire on read, so a 30 s cadence only bounds how long a file lingers.
    """
    while True:
        time.sleep(30)
        try:
            outbox.sweep()
        except Exception as exc:  # a sweep must never kill the daemon
            print(f"[outbox] sweep failed: {exc}", flush=True)


def start_sweeper():
    """Empty what a previous process left, then sweep in the background."""
    outbox.reset()
    threading.Thread(target=outbox_sweeper, daemon=True).start()
