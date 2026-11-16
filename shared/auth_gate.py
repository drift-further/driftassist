"""The auth gate: every request needs a device cookie or the header token.

Lives outside serve.py so the tests can put the real gate — including the
open-window issuer — in front of a test app without importing serve.py, whose
import starts the background threads.
"""

from flask import g, jsonify, redirect, request

import shared.auth as auth
from shared import devices

# The Origin allowlist only stops a hostile *page* in the user's browser; it
# does nothing about a direct request from any device that can reach the port,
# and /api/commands/run runs arbitrary commands by design. This gate is what
# makes that unreachable without the token.
#
# /api/cli-proxy is exempt: it was called by containers on an isolated network
# that had no way to hold the token. It is parked (409 before any effect), and
# behind that fail-closed on its own ASSIST_CLI_ALLOWED allowlist; nginx
# restricts it to the container subnet.
# /health is a liveness probe (assist-ctl polls it to decide whether a restart
# succeeded) and carries no data worth gating.
# /login.js is the sign-in page's approval script: static, no secret, and
# needed by exactly the browser that has no cookie.
# The device-approval pair is exempt for the same reason /login is: the caller
# has no token yet. Both are fenced inside shared.auth by the LAN allowlist, a
# pending cap, a per-IP cooldown and a single-use claim, and neither can reach
# a command-exec surface.
AUTH_EXEMPT = frozenset({
    "static_bp.login",
    "static_bp.login_script",
    "poll_bp.cli_proxy",
    "poll_bp.health",
    "access_bp.access_request",
    "access_bp.access_request_status",
})

_CAP_PAGE = (
    "<!DOCTYPE html><meta charset=utf-8><title>Device limit</title>"
    "<p>This Assist already has the maximum number of signed-in devices. "
    "Revoke one in Settings → Access → Devices (or <code>assist devices "
    "revoke &lt;id&gt;</code> on the host), then load this page again.</p>"
)


def register_auth_gate(app):
    """Install the gate and the window's cookie issuer on `app`."""

    @app.before_request
    def _require_auth():
        if request.endpoint in AUTH_EXEMPT:
            return None
        if auth.request_authenticated(request):
            return None
        # Temporary open access. A live window admits ONE client from a
        # configured LAN range to this one endpoint; loading the app shell is
        # what mints its device, and the window is spent doing it. Every API
        # route stays behind the cookie, so an open window is never itself a
        # command-exec surface.
        if request.endpoint == "static_bp.index" and auth.is_onboarding_navigation(request):
            result = devices.admit(
                devices.Window(),
                ip=auth.client_ip(request),
                ua=request.headers.get("User-Agent", ""),
            )
            if isinstance(result, dict):
                g.assist_onboard_device = result["id"]
                return None
            if result.reason == "device_cap":
                return _CAP_PAGE, 409, {"Content-Type": "text/html; charset=utf-8"}
            if result.reason == "write_failed":
                return "Could not record this device; try again.", 503
        # A browser navigating to a page gets the login form; anything else
        # (fetch, WebSocket handshake, curl) gets a machine-readable 401.
        if "text/html" in (request.headers.get("Accept") or ""):
            return redirect("/login")
        return jsonify({"ok": False, "error": "Authentication required"}), 401

    @app.after_request
    def _issue_onboard_cookie(response):
        # Unconditional, including on a non-200: the window is already spent,
        # so handing the cookie over anyway costs a page reload rather than a
        # second window.
        device_id = g.get("assist_onboard_device")
        if device_id:
            auth.set_auth_cookie(response, device_id)
        return response
