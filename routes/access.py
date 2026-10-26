"""routes/access.py — temporary open access, device approval requests, and
the Devices list.

The window and request routes are over state that lives in `shared.auth`; the
/access/devices routes over the registry in `shared.devices`. The open/close/decide trio
is POST behind the normal auth gate, so the existing Origin allowlist gives
them CSRF protection and only an already-logged-in client can reach them.

The request/status pair is deliberately exempt from that gate — a device with
no token is exactly who calls them — and is fenced instead by the LAN
allowlist, a pending cap, a per-IP cooldown, and a secret claim that binds an
approval to the browser that asked for it.
"""

import math

from flask import Blueprint, jsonify, request

import shared.auth as auth
from shared import devices, state

access_bp = Blueprint("access_bp", __name__)


@access_bp.route("/access/open", methods=["POST"])
def access_open():
    """Open (or restart) the window."""
    if not auth.open_networks():
        # Fail loudly rather than opening a window that can admit no one.
        return jsonify({"ok": False, "error": "no networks configured"}), 400

    cfg = state.get_settings().get("access") or {}
    data = request.get_json(silent=True) or {}
    try:
        minutes = float(data.get("minutes", cfg.get("open_default_minutes", 5)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "minutes must be a number"}), 400
    if not math.isfinite(minutes):
        return jsonify({"ok": False, "error": "minutes must be a number"}), 400

    minutes = max(1.0, min(minutes, float(cfg.get("open_max_minutes", 60))))
    source = "pair" if data.get("source") == "pair" else "window"
    return jsonify({"ok": True, "access": auth.open_window(minutes * 60, source=source)})


@access_bp.route("/access/close", methods=["POST"])
def access_close():
    """Shut the window early. Idempotent."""
    return jsonify({"ok": True, "access": auth.close_window()})


@access_bp.route("/access/request", methods=["POST"])
def access_request():
    """A device on the LAN asks to be let in.

    Unauthenticated by necessity — the caller has no token, that being the
    whole point. The fence is the LAN allowlist plus the cap and cooldown in
    shared.auth, so this is no wider a surface than the open-access window
    already shipped.
    """
    result = auth.create_request(
        auth.client_ip(request), request.headers.get("User-Agent", "")
    )
    if not result["ok"]:
        status = 403 if result["error"] == "out_of_scope" else 429
        return jsonify(result), status
    return jsonify(result)


@access_bp.route("/access/request/status", methods=["GET"])
def access_request_status():
    """The waiting device polls here. Approval mints its device and cookie.

    A re-poll within a minute of that answer returns the same device's cookie
    again (the first response may have been lost); see shared.devices.admit.
    """
    result = devices.admit(
        devices.ApprovalClaim(request.args.get("claim", "")),
        ip=auth.client_ip(request),
        ua=request.headers.get("User-Agent", ""),
    )
    if isinstance(result, dict):
        response = jsonify({"ok": True, "status": "approved", "remaining_sec": 0})
        return auth.set_auth_cookie(response, result["id"])
    if result.reason in ("pending", "denied", "expired"):
        remaining = result.remaining_sec if result.reason == "pending" else 0
        return jsonify({"ok": True, "status": result.reason, "remaining_sec": remaining})
    if result.reason == "device_cap":
        return jsonify({"ok": False, "status": "device_cap", "remaining_sec": 0}), 409
    return jsonify({"ok": False, "status": "unavailable", "remaining_sec": 0}), 503


@access_bp.route("/access/decide", methods=["POST"])
def access_decide():
    """A logged-in session's verdict on a pending request."""
    data = request.get_json(silent=True) or {}
    try:
        rid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "id must be an integer"}), 400
    action = data.get("action")
    if action not in ("approve", "deny"):
        return jsonify({"ok": False, "error": "action must be approve or deny"}), 400
    moved = auth.decide_request(rid, action == "approve")
    return jsonify({"ok": moved, "access": auth.window_state()})


# ---------------------------------------------------------------------------
# Devices (Settings → Access → Devices, and `assist devices`)
# ---------------------------------------------------------------------------

_DEVICE_ERRORS = {
    "not_found": 404,
    "bad_name": 400,
    "self_requires_confirm": 409,
    "not_revoked": 409,
    "recovery_required": 409,
    "write_failed": 503,
}


def _device_error(exc):
    return jsonify({"ok": False, "error": exc.code}), _DEVICE_ERRORS.get(exc.code, 400)


def _public(row):
    return {
        key: row.get(key)
        for key in ("id", "name", "named", "created", "last_seen", "ip", "ua", "source", "revoked_at")
    }


@access_bp.route("/access/devices", methods=["GET"])
def access_devices():
    """Every device record, which one is the caller, and whether the registry
    is quarantined (then only the header can reach this, and reset clears it)."""
    caller = auth.request_device(request)
    recovery = devices.recovery_required()
    return jsonify({
        "ok": True,
        "devices": [_public(r) for r in devices.list_devices()],
        "this_device": caller["id"] if caller else None,
        "recovery_required": recovery,
        "message": (
            "registry quarantined; run assist devices reset" if recovery else ""
        ),
        "max_active": devices.MAX_ACTIVE,
    })


@access_bp.route("/access/devices/name", methods=["POST"])
def access_devices_name():
    """The name step right after admission: the caller names its own device."""
    caller = auth.request_device(request)
    if caller is None:
        return jsonify({"ok": False, "error": "no_device"}), 400
    data = request.get_json(silent=True) or {}
    try:
        devices.rename(caller["id"], data.get("name"))
    except devices.DeviceError as exc:
        return _device_error(exc)
    return jsonify({"ok": True, "device": _public(devices.get(caller["id"]))})


@access_bp.route("/access/devices/rename", methods=["POST"])
def access_devices_rename():
    data = request.get_json(silent=True) or {}
    try:
        devices.rename(str(data.get("id") or ""), data.get("name"))
    except devices.DeviceError as exc:
        return _device_error(exc)
    return jsonify({"ok": True})


@access_bp.route("/access/devices/revoke", methods=["POST"])
def access_devices_revoke():
    """Revoke one device. 200 only after the record is durably written;
    revoking the caller's own device needs `confirm_self`."""
    data = request.get_json(silent=True) or {}
    caller = auth.request_device(request)
    try:
        devices.revoke(
            str(data.get("id") or ""),
            confirm_self=data.get("confirm_self") is True,
            caller_id=caller["id"] if caller else None,
        )
    except devices.DeviceError as exc:
        return _device_error(exc)
    return jsonify({"ok": True})


@access_bp.route("/access/devices/forget", methods=["POST"])
def access_devices_forget():
    data = request.get_json(silent=True) or {}
    try:
        devices.forget(str(data.get("id") or ""))
    except devices.DeviceError as exc:
        return _device_error(exc)
    return jsonify({"ok": True})


@access_bp.route("/access/devices/reset", methods=["POST"])
def access_devices_reset():
    """Empty the registry: every browser signs in again. Header token only —
    a browser cookie cannot reset, and in recovery mode no cookie works."""
    if not auth.header_authenticated(request):
        return jsonify({"ok": False, "error": "header_token_required"}), 403
    try:
        devices.reset()
    except devices.DeviceError as exc:
        return _device_error(exc)
    return jsonify({"ok": True})
