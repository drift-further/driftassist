"""Drift Assist — phone-first web terminal for Claude Code sessions.

App factory: imports all blueprints, registers them, starts background threads.
"""

import logging
import os
import threading

from flask import Flask, request
from flask_sock import Sock

import shared.auth as auth
from shared import auth_gate, listen, paths, security
from shared.security import origin_allowed

os.environ.setdefault("DISPLAY", ":0")

# Strip Claude Code session markers so tmux sessions launched from here
# can run `claude` without the "nested session" error.
_CLAUDE_ENV_VARS = ("CLAUDECODE",)
for _v in _CLAUDE_ENV_VARS:
    os.environ.pop(_v, None)


def create_app():
    """Create and configure the Flask application."""
    app = Flask(__name__)
    sock = Sock(app)

    # Host and Origin allowlists (shared/security.py) run before the auth gate:
    # a rebound or cross-origin request is refused without consulting a cookie.
    security.register_request_guards(app)

    # Shared-secret gate and the open window's device issuer (shared/auth_gate.py).
    auth_gate.register_auth_gate(app)
    # Content-Security-Policy on every response (shared/security.py).
    security.register_csp(app)

    # CORS — echo only fixed-allowlist Origins, never emit a wildcard.
    @app.after_request
    def _cors(response):
        origin = request.headers.get("Origin")
        if origin and origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, PATCH, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    from shared.utils import gzip_json_response

    @app.after_request
    def _gzip_json(response):
        return gzip_json_response(response, request.headers.get("Accept-Encoding", ""))

    # Register blueprints
    from routes.static import static_bp
    from routes.access import access_bp
    from routes.input import input_bp
    from routes.terminal import terminal_bp
    from routes.opencode import opencode_bp
    from routes.git import git_bp
    from routes.commands import commands_bp
    from routes.autoyes import autoyes_bp
    from routes.automate import automate_bp
    from routes.container import container_bp
    from routes.poll import poll_bp
    from routes.delegates import delegates_bp
    from routes.completion import completion_bp
    from routes.studio import studio_bp
    from routes.studio_context import context_bp
    from routes.tabstate import tabstate_bp
    from routes.drafts import drafts_bp
    from routes.updates import updates_bp

    app.register_blueprint(static_bp)
    app.register_blueprint(access_bp)
    app.register_blueprint(input_bp)
    app.register_blueprint(terminal_bp)
    app.register_blueprint(opencode_bp)
    app.register_blueprint(git_bp)
    app.register_blueprint(commands_bp)
    app.register_blueprint(autoyes_bp)
    app.register_blueprint(automate_bp)
    app.register_blueprint(container_bp)
    app.register_blueprint(poll_bp)
    app.register_blueprint(delegates_bp)
    app.register_blueprint(completion_bp)
    app.register_blueprint(studio_bp)
    app.register_blueprint(context_bp)
    app.register_blueprint(tabstate_bp)
    app.register_blueprint(drafts_bp)
    app.register_blueprint(updates_bp)

    from routes.settings import settings_bp, init_start_time

    app.register_blueprint(settings_bp)
    init_start_time()

    # Register WebSocket route (flask-sock requires app-level Sock, not Blueprint)
    from routes.streaming import register_streaming

    register_streaming(sock)

    return app


def start_application_backgrounds():
    """Start application work only after the process owns its listener.

    The activation-only candidate must establish its observation watermark and
    finish the old-generation snapshot without importing these modules or
    starting any Flask, AutoYes, Automate, drafts, or Studio work.
    """
    from routes.autoyes import autoyes_scanner, restore_autoyes_from_settings
    from routes.delegates import delegate_watcher
    from routes.automate import automate_recover
    from routes.drafts import start_sweeper as start_drafts_sweeper
    from routes.studio import studio_refresher
    from shared.studio_context import studio_observer, studio_context_refresher
    from shared.studio_events import studio_event_receiver, studio_event_consumer
    from shared.updates import manager as update_manager

    threading.Thread(target=autoyes_scanner, daemon=True).start()
    threading.Thread(target=delegate_watcher, daemon=True).start()
    threading.Thread(target=studio_refresher, daemon=True).start()
    threading.Thread(target=studio_observer, daemon=True).start()
    threading.Thread(target=studio_context_refresher, daemon=True).start()
    threading.Thread(target=studio_event_receiver, daemon=True).start()
    threading.Thread(target=studio_event_consumer, daemon=True).start()
    updater = update_manager()
    threading.Thread(target=updater.watch, daemon=True).start()
    start_drafts_sweeper()
    restore_autoyes_from_settings()
    automate_recover()


def start_extra_listeners(addresses, port, app):
    """Serve the same app on each ASSIST_BIND address, beside loopback.

    Bound here, synchronously, so a taken or unassignable address fails the
    start loudly instead of leaving a loopback-only server that looks healthy.
    """
    from werkzeug.serving import make_server

    for address in addresses:
        lan_server = make_server(address, port, app, threaded=True)
        threading.Thread(target=lan_server.serve_forever, daemon=True).start()
        print(f"[assist] also listening on {address}:{port}", flush=True)


def configure_logging():
    """Send this app's own log records to stderr, which assist-ctl tees to the log.

    Nothing configured logging before, so the root logger sat at its WARNING
    default and every `log.info` under routes/ and shared/ was discarded — the
    `autoyes: detected`/`FIRED` lines included. The 2026-09-05 luna-switch
    investigation could not establish from the server log whether auto-yes had
    pressed anything in either affected pane, only that nothing was recorded:
    8.5 MB of log, 22 `autoyes:`
    lines, all of them the same ERROR traceback. An automatic keystroke sender
    with no audit trail is not diagnosable after the fact.

    Scoped to this app's two logger namespaces rather than the root, so third
    party INFO chatter stays out; werkzeug keeps its own handler, so the access
    log is unchanged. ASSIST_LOG_LEVEL overrides for a quieter run.
    """
    level = os.environ.get("ASSIST_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    for name in ("routes", "shared"):
        logger = logging.getLogger(name)
        if any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            continue  # already configured — a second call must not double every line
        logger.setLevel(level)
        logger.addHandler(handler)
        logger.propagate = False


# WSGI/flask-run compatibility retains the established import-time app.  The
# direct executable path below is separate so activation can remain observer-
# only until the controller authorises the bind.
def _enter_runtime(handoff=False):
    """Layout ownership, the shared layout lock (held for this process's life)
    and the migration marker — before anything imports shared.state."""
    try:
        return paths.enter_runtime(handoff=handoff)
    except paths.LayoutError as exc:
        raise SystemExit(f"[assist] {exc}")


if __name__ != "__main__":
    _layout_lock = _enter_runtime()
    configure_logging()
    app = create_app()
    start_application_backgrounds()


if __name__ == "__main__":
    import argparse
    from werkzeug.serving import make_server

    parser = argparse.ArgumentParser(description="Drift Assist server")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ASSIST_PORT", "8089")),
        help="Port to listen on",
    )
    # Loopback always. A phone reaches Flask one of two ways: ASSIST_BIND (set
    # by `assist expose`) adds the host's LAN address as a second listener, or
    # nginx listens on the LAN address and forwards here. Never 0.0.0.0 --
    # shared.listen refuses a wildcard in ASSIST_BIND, because every endpoint
    # would then be on every interface, including ones nobody meant to serve.
    parser.add_argument("--host", default="127.0.0.1", help="Interface to bind")
    parser.add_argument("--park-handoff-fd", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    _layout_lock = _enter_runtime(handoff=args.park_handoff_fd is not None)
    try:
        addresses = listen.bind_addresses(args.host, os.environ.get("ASSIST_BIND"))
    except ValueError as exc:
        raise SystemExit(f"[assist] {exc}")

    handoff = None
    if args.park_handoff_fd is not None:
        # This import is deliberately the sole pre-bind subsystem.  candidate_prebind
        # does read-only observation and blocks until old-generation termination and
        # explicit bind authorisation.
        from shared.park_activation import candidate_prebind

        handoff = candidate_prebind(args.park_handoff_fd)

    if handoff is not None:
        from shared.park_activation import ExecutionObserver

        if not ExecutionObserver.final_quiescent(handoff.snapshot):
            raise RuntimeError("park_drain_pending")

    if args.park_handoff_fd is None:
        from shared.launch_provenance import initialize_for_startup

        initialize_for_startup()

    configure_logging()
    # Loopback origins follow the port actually bound, not only ASSIST_PORT.
    security.configure(args.port)
    app = create_app()
    server = make_server(addresses[0], args.port, app, threaded=True)
    start_extra_listeners(addresses[1:], args.port, app)
    # Binding is the activation milestone.  Publish it before recovery/scanner
    # startup, whose initial read-only sweeps may legitimately take longer than
    # the controller handshake timeout.
    if handoff is not None:
        from shared import execution_park
        from shared.park_activation import ACTIVATION_PATH

        execution_park.configure_activation(
            ACTIVATION_PATH, handoff.snapshot.get("units") or ()
        )
        execution_park.publish_parked()
        handoff.notify_bound()
    start_application_backgrounds()

    auth.print_startup_token_notice()
    server.serve_forever()
