"""routes/drafts.py — Per-target composer drafts: read, save, clear, expire.

The marker set and a per-target revision stamp ride along on every /poll
(shared/drafts.py:poll_block); these routes carry the draft BODIES, which are
too big to ship every 5s to every browser. The client reads one on tab switch
and on a revision bump, and writes it debounced.
"""

import threading
import time

from flask import Blueprint, jsonify, request

from shared import drafts

drafts_bp = Blueprint("drafts_bp", __name__)


def _target_of(data):
    """Drafts are always addressed explicitly.

    Deliberately NOT shared.utils.resolve_target(): that falls back to the
    server-wide active target, and a client whose active tab has drifted from
    the server's would silently read or overwrite a DIFFERENT tab's draft.
    """
    value = data.get("target")
    if not isinstance(value, str):
        return ""
    return value.strip()


# A target is `session:w.p` (an old client), `%N`, or a durable pane key (a
# client sending `proto: 2`); shared/drafts.py resolves each to the pane's
# stored key. Responses echo the target as sent, which is what the client
# keys its own cache by.

def _unavailable():
    # The pane list could not be read to convert the pre-upgrade file yet, or
    # an upload could not be given its own file (the body then says which).
    body = {"ok": False, "error": "drafts_unavailable"}
    why = drafts.retry_reason()
    if why:
        body.update(why)
    return jsonify(body), 503


@drafts_bp.route("/api/draft")
def get_draft_api():
    target = _target_of(request.args)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400
    if not drafts.ensure_converted():
        return _unavailable()
    return jsonify({"ok": True, "target": target, "draft": drafts.get_draft(target)})


@drafts_bp.route("/api/draft", methods=["PUT"])
def put_draft_api():
    """Partial save. Omitted fields keep their stored value."""
    data = request.get_json(silent=True) or {}
    target = _target_of(data)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400

    text = data.get("text")
    if text is not None and not isinstance(text, str):
        return jsonify({"ok": False, "error": "text must be a string"}), 400
    attachments = data.get("attachments")
    if attachments is not None and not isinstance(attachments, list):
        return jsonify({"ok": False, "error": "attachments must be a list"}), 400
    enter_armed = data.get("enter_armed")
    if enter_armed is not None and not isinstance(enter_armed, bool):
        return jsonify({"ok": False, "error": "enter_armed must be a boolean"}), 400
    if text is None and attachments is None and enter_armed is None:
        return jsonify({"ok": False, "error": "No data"}), 400
    if not drafts.ensure_converted():
        return _unavailable()

    try:
        entry = drafts.set_draft(
            target, text=text, attachments=attachments, enter_armed=enter_armed
        )
    except drafts.DraftsUnavailable:
        return _unavailable()
    except drafts.AttachmentCopyFailed as exc:
        # A resent upload exists but could not get its own file (no space, an
        # I/O error): retryable, and the stored draft is exactly as it was.
        return jsonify({"ok": False, "error": exc.error, "target": target}), exc.status
    if entry is None:
        # No live pane by that name: nothing stored is overwritten, so a stale
        # tab cannot write over an orphaned draft that carries the same name,
        # nor a late save to a dead pane's key shrink its orphan (text it
        # adds is kept beside it). The client drops a refused save: no retry.
        return jsonify({"ok": False, "error": "target_absent", "target": target}), 409
    return jsonify({"ok": True, "target": target, "draft": entry})


@drafts_bp.route("/api/draft", methods=["DELETE"])
def delete_draft_api():
    data = request.get_json(silent=True) or {}
    target = _target_of(data) or _target_of(request.args)
    if not target:
        return jsonify({"ok": False, "error": "target required"}), 400
    if not drafts.ensure_converted():
        return _unavailable()
    try:
        drafts.delete_draft(target)
    except drafts.DraftsUnavailable:
        return _unavailable()
    return jsonify({"ok": True, "target": target})


@drafts_bp.route("/api/drafts/orphans")
def list_orphans_api():
    """Drafts no live pane owns: pre-upgrade names that matched no pane, and
    keys whose pane or tmux server is gone. Bodies included (the orphan sheet)."""
    orphans = drafts.list_orphans()
    if orphans is None:
        return _unavailable()
    return jsonify({"ok": True, "orphans": orphans})


_ORPHAN_STATUS = {"not_found": 404, "unavailable": 503,
                  # Retryable: an upload file could not be made; nothing written.
                  "storage_full": 507, "attachment_copy_failed": 500}


@drafts_bp.route("/api/drafts/copy", methods=["POST"])
def copy_orphan_api():
    """The explicit "Copy to this pane": one orphan onto one live pane that
    holds no draft. The orphan stays until it is dismissed or expires.
    Nothing else ever attaches an orphan's text to a pane."""
    data = request.get_json(silent=True) or {}
    source = data.get("from")
    target = _target_of(data)
    if not isinstance(source, str) or not source or not target:
        return jsonify({"ok": False, "error": "from and target required"}), 400
    entry, error = drafts.copy_orphan(source, target)
    if error:
        return jsonify({"ok": False, "error": error}), _ORPHAN_STATUS.get(error, 409)
    return jsonify({"ok": True, "target": target, "draft": entry})


@drafts_bp.route("/api/drafts/dismiss", methods=["POST"])
def dismiss_orphan_api():
    """The orphan sheet's Dismiss: delete one orphan and its own uploads."""
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    if not isinstance(key, str) or not key:
        return jsonify({"ok": False, "error": "key required"}), 400
    _removed, error = drafts.dismiss_orphan(key)
    if error:
        return jsonify({"ok": False, "error": error}), _ORPHAN_STATUS.get(error, 409)
    return jsonify({"ok": True, "key": key})


def drafts_sweeper():
    """Background pass: drop expired drafts and the uploads only they held.

    Started from serve.py alongside the other daemons. Sleeps FIRST so a
    restart loop cannot turn startup into a hot sweep loop.
    """
    while True:
        time.sleep(drafts.SWEEP_INTERVAL_SEC)
        try:
            dropped, removed = drafts.sweep_expired()
            if dropped:
                print(
                    f"[drafts] expired {len(dropped)} draft(s), "
                    f"removed {len(removed)} orphaned upload(s)",
                    flush=True,
                )
        except Exception as exc:  # a sweep must never kill the daemon
            print(f"[drafts] sweep failed: {exc}", flush=True)


def start_sweeper():
    threading.Thread(target=drafts_sweeper, daemon=True).start()
