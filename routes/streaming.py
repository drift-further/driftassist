"""routes/streaming.py — WebSocket terminal streaming."""

import json
import threading
import time

from flask import request

import shared.auth as auth
import shared.state as state
from shared import devices
from shared.security import origin_allowed
from shared.tmux import (
    bind_pane_key,
    bind_pane_key_status,
    capture_pane,
    pane_key_binding_current,
    capture_pane_by_key,
    capture_pane_if_changed,
    parse_pane_key,
    set_ws_send_timeout,
)

# The sock route is registered via register_streaming() called from serve.py.
# We cannot use a Blueprint for @sock.route — flask-sock requires the app-level Sock instance.

_sock = None  # Set by register_streaming()

# Default scrollback lines for a stream when the client doesn't specify.
# Used both before the first message arrives and as the parse fallback.
_DEFAULT_LINES = 2000



def _clamp_lines(raw):
    """A subscribe's line count, capped by limits.max_capture_lines (0 = that cap)."""
    cap = state.get_setting("limits", "max_capture_lines")
    lines = int(raw)
    return cap if lines <= 0 else min(lines, cap)

# A pane whose change marker (activity, history size, cursor, size) has not
# moved is not re-captured, except once per this many seconds, which catches a
# redraw that moves none of them. See shared/tmux.py:capture_reusable.
STREAM_FORCED_REFRESH_SEC = 2.0
# A busy pane's marker moves every second, which alone would re-capture it on
# every 0.1 s tick. Within a second whose marker has not moved, wait this long.
STREAM_MIN_RECAPTURE_SEC = 0.3

def _tui_flag(msg):
    """Normalize a subscribe message's per-pane TUI override.

    True/False pin the capture range to match the mode the chip is showing;
    absent (None) leaves detection to capture_pane, as before the toggle.
    """
    v = msg.get("tui")
    return None if v is None else bool(v)


def _stream_subscription(value):
    """(target, binding) for a subscribe target, or None for a dead key.

    `session:w.p` and `%N` stream as sent, as before. A durable pane key
    (shared/tmux.py:PaneRef.key) keeps itself as the subscription's name and
    carries its PaneKeyBinding: the streamer captures the key's `%N` and checks
    the binding before every frame, so a key never streams another pane — not
    after its pane moves (it follows it), is replaced at its old address, or
    its tmux server restarts (it expires with the not-found frame).
    """
    if isinstance(value, str) and parse_pane_key(value.strip()):
        binding = bind_pane_key(value.strip())
        return (binding.key, binding) if binding else None
    return value, None


def _not_found_frame(target):
    return json.dumps({"type": "error", "error": "Target not found", "target": target})


def _key_capture(binding, lines, tui, prev=None, max_age=2.0, min_interval=0.0):
    """capture_pane_by_key, plus one re-bind when the capture failed: a
    transient tmux failure is not the key expiring. Returns (content, info,
    prev_next, expired)."""
    content, info, prev_next = capture_pane_by_key(
        binding, lines, tui=tui, prev=prev, max_age=max_age, min_interval=min_interval,
    )
    if info is not None:
        return content, info, prev_next, False
    if not pane_key_binding_current(binding):
        return None, None, None, True  # the server or its socket is gone
    rebound, status = bind_pane_key_status(binding.key)
    # A timeout or a tmux that cannot be asked is not the key expiring.
    return None, None, None, status == "gone" or (status == "bound" and rebound != binding)


def _frame(target, content, info, ts, binding=None):
    msg = {"type": "full", "content": content, "target": target, "info": info, "ts": ts}
    if binding is not None:
        # Where the pane sat when the key was bound: presentation only.
        msg["display_target"] = binding.display_target
    return json.dumps(msg)


def _full_frame(target, lines, tui=None, binding=None):
    """Capture `target` and return the JSON 'full' frame, or an 'error' frame
    when the capture fails (dead/unknown target) so the client doesn't sit on
    a healthy-looking but frozen terminal. Returns (frame_json, info)."""
    if binding is not None:
        content, info, _prev, _expired = _key_capture(binding, lines, tui)
    else:
        content, info = capture_pane(target, lines, tui=tui)
    if content is None:
        return _not_found_frame(target), None
    return _frame(target, content, info, time.time(), binding), info


def _close(ws):
    try:
        ws.close()
    except Exception:
        pass


def register_streaming(sock_instance):
    """Register the WebSocket route on the given Sock instance.

    Called from serve.py after app and sock are created.
    """
    global _sock
    _sock = sock_instance
    _sock.route("/terminal/stream")(terminal_stream_handler)


def terminal_stream_handler(ws):
    """WebSocket endpoint for real-time terminal streaming.

    flask-sock runs this inside the handshake's Flask request, after the auth
    gate. The credential is read again here to learn which device (None for
    the header token) owns the socket, before any subscription is read; the
    socket is registered under the devices lock, refused if that device was
    revoked meanwhile, and re-checked once more before the first frame. A
    later revoke closes it (shared.devices).
    """
    # WS handshakes bypass CORS entirely — enforce the same Origin
    # allowlist as the HTTP before_request hook (shared/security.py).
    if not origin_allowed(ws.environ.get("HTTP_ORIGIN")):
        _close(ws)
        return

    device = auth.request_device(request)
    if device is None and not auth.header_authenticated(request):
        _close(ws)
        return
    device_id = device["id"] if device else None

    target = None
    lines = _DEFAULT_LINES
    tui = None

    try:
        raw = ws.receive(timeout=5)
        if raw:
            msg = json.loads(raw)
            target = msg.get("target", state.tmux_target)
            lines = _clamp_lines(msg.get("lines", _DEFAULT_LINES))
            tui = _tui_flag(msg)
    except Exception:
        target = state.tmux_target

    # One entry per connection, mutated in place on resubscribe. The lock
    # serializes ws.send() across the three threads that write to this
    # socket (handler, streamer, autoyes broadcaster) — simple_websocket's
    # send() has no internal lock and can interleave partial writes.
    client = {
        "ws": ws,
        "lock": threading.Lock(),
        "target": target,
        "lines": lines,
        "tui": tui,
        "last_send": time.time(),
        "device_id": device_id,
    }

    if not target:
        try:
            _send_to(client, json.dumps({"type": "error", "error": "No target specified"}))
        except Exception:
            pass
        return

    requested = target
    subscription = _stream_subscription(target)
    if subscription is None:
        try:
            _send_to(client, _not_found_frame(requested))
        except Exception:
            pass
        return
    target, binding = subscription
    client["target"] = target
    client["binding"] = binding

    def _add():
        with state.ws_lock:
            state.ws_last_content.pop(f"{target}:{lines}:{tui}", None)
            state.ws_clients.append(client)

    if not devices.register_stream(device_id, client, _add):
        _close(ws)
        return

    try:
        # First frame goes out immediately — after one more check that the
        # device was not revoked between registration and now.
        try:
            if not devices.stream_device_active(device_id):
                _close(ws)
                return
            frame, _info = _full_frame(target, lines, tui, binding)
            _send_to(client, frame)
        except Exception:
            return

        set_ws_send_timeout(ws)
        _ensure_streamer()

        try:
            while True:
                raw = ws.receive()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                    if msg.get("type") == "ping":
                        _send_to(client, json.dumps({"type": "pong"}))
                        continue
                    if msg.get("type") == "subscribe":
                        subscription = _stream_subscription(msg.get("target", target))
                        if subscription is None:
                            # A key naming no live pane: say so, keep streaming
                            # what this socket already watches.
                            _send_to(client, _not_found_frame(msg.get("target")))
                            continue
                        new_target, new_binding = subscription
                        new_lines = _clamp_lines(msg.get("lines", lines))
                        new_tui = _tui_flag(msg)
                        with state.ws_lock:
                            cache_key = f"{new_target}:{new_lines}:{new_tui}"
                            state.ws_last_content.pop(cache_key, None)
                            client["target"] = new_target
                            client["binding"] = new_binding
                            client["lines"] = new_lines
                            client["tui"] = new_tui
                        target = new_target
                        binding = new_binding
                        lines = new_lines
                        tui = new_tui
                        _ensure_streamer()
                        frame, _info = _full_frame(target, lines, tui, binding)
                        _send_to(client, frame)
                except Exception:
                    pass
        except Exception:
            pass
    finally:
        _remove_ws_client(ws)
        devices.unregister_stream(client)


def _terminal_streamer():
    """Background thread: polls tmux, pushes changes to WebSocket clients."""
    _empty_count = 0
    # cache_key -> the last capture's (marker, captured_at), for skipping a
    # capture of a pane that has not changed since.
    last_capture = {}
    try:
        while True:
            try:
                with state.ws_lock:
                    if not state.ws_clients:
                        _empty_count += 1
                        if _empty_count > 50:
                            # Exit decision and flag flip must be atomic:
                            # a client connecting in between would see a
                            # live-but-doomed streamer and get no frames.
                            # Inside ws_lock, ws_clients is still empty here
                            # and _ensure_streamer will start a fresh thread
                            # for the next connect.
                            state.ws_streamer_running = False
                            return
                        clients = []
                    else:
                        _empty_count = 0
                        # Snapshot (entry, target, lines, tui) under the lock —
                        # subscribe mutates entries in place.
                        clients = [
                            (c, c["target"], c["lines"], c.get("tui"), c.get("binding"))
                            for c in state.ws_clients
                        ]

                if not clients:
                    # Sleep OUTSIDE ws_lock — holding it here starves
                    # connecting handlers and the autoyes broadcaster.
                    time.sleep(0.1)
                    continue

                now = time.time()

                # Grouped by TUI override too: two clients on the same pane with
                # different overrides need different capture ranges, so they
                # can't share a capture or a dedup entry.
                # A key subscription is named by its key (never an address),
                # so its group holds only clients bound to that pane.
                targets = {}
                bindings = {}
                for client, target, lines, tui, binding in clients:
                    if target is None:
                        continue  # an expired key: heartbeats only
                    key = (target, lines, tui)
                    if key not in targets:
                        targets[key] = []
                        bindings[key] = binding
                    targets[key].append(client)

                for (target, lines, tui), group in targets.items():
                    cache_key = f"{target}:{lines}:{tui}"
                    binding = bindings[(target, lines, tui)]
                    try:
                        if binding is not None:
                            content, info, prev, expired = _key_capture(
                                binding, lines, tui,
                                prev=last_capture.get(cache_key),
                                max_age=STREAM_FORCED_REFRESH_SEC,
                                min_interval=STREAM_MIN_RECAPTURE_SEC,
                            )
                            if expired:
                                _expire(group, target, binding)
                                continue
                        else:
                            content, info, prev = capture_pane_if_changed(
                                target, lines, tui=tui,
                                prev=last_capture.get(cache_key),
                                max_age=STREAM_FORCED_REFRESH_SEC,
                                min_interval=STREAM_MIN_RECAPTURE_SEC,
                            )
                    except Exception:
                        continue
                    if prev:
                        last_capture[cache_key] = prev
                    if content is None:
                        continue

                    with state.ws_lock:
                        prev_content = state.ws_last_content.get(cache_key)

                    if prev_content == content:
                        continue

                    with state.ws_lock:
                        state.ws_last_content[cache_key] = content

                    try:
                        msg = _frame(target, content, info, now, binding)
                    except Exception:
                        continue

                    for client in group:
                        try:
                            _send_to(client, msg)
                        except Exception:
                            _remove_ws_client(client["ws"])

                # Per-client heartbeats: a client watching a quiet target
                # still needs traffic inside the frontend's 8s inactivity
                # window even while OTHER targets are busy — a global
                # sent_any flag starves it into a reconnect loop.
                for client, _t, _l, _tui, _b in clients:
                    if now - client["last_send"] >= state.WS_HEARTBEAT_INTERVAL:
                        try:
                            _send_to(client, json.dumps({"type": "heartbeat", "ts": now}))
                        except Exception:
                            _remove_ws_client(client["ws"])

                # Keys are (target, lines, tui) and the dedup cache is written as
                # f"{target}:{lines}:{tui}" (see cache_key above) — both the arity
                # and the format have to match, or this silently raises and the
                # whole cleanup below is skipped for the life of the process.
                active_keys = {f"{t}:{l}:{tui}" for t, l, tui in targets}
                with state.ws_lock:
                    stale = [k for k in state.ws_last_content if k not in active_keys]
                    for k in stale:
                        del state.ws_last_content[k]
                for k in [k for k in last_capture if k not in active_keys]:
                    del last_capture[k]

            except Exception:
                time.sleep(0.5)

            time.sleep(0.1)
    except Exception:
        pass
    finally:
        with state.ws_lock:
            # Only clear the flag if no replacement streamer has started
            # since (the normal-exit path above already cleared it, which
            # lets _ensure_streamer spawn a successor before this runs).
            if state.ws_streamer_thread is threading.current_thread():
                state.ws_streamer_running = False


def _expire(group, target, binding):
    """A key's pane or server is gone: say so once and stop that
    subscription. The socket stays open for the client to subscribe anew."""
    frame = _not_found_frame(target)
    for client in group:
        with state.ws_lock:
            if client.get("binding") is not binding:
                continue  # resubscribed meanwhile
            client["target"] = None
            client["binding"] = None
        try:
            _send_to(client, frame)
        except Exception:
            _remove_ws_client(client["ws"])


def _send_to(client, msg):
    """Send on a client's socket, serialized by its per-client lock.

    simple_websocket's send() is not thread-safe; the streamer, the WS
    handler thread, and the autoyes broadcaster can all write to the same
    socket. Also stamps last_send for per-client heartbeat bookkeeping.
    """
    with client["lock"]:
        client["ws"].send(msg)
        client["last_send"] = time.time()


def _remove_ws_client(ws):
    with state.ws_lock:
        state.ws_clients[:] = [c for c in state.ws_clients if c["ws"] is not ws]


def _ensure_streamer():
    with state.ws_lock:
        if state.ws_streamer_running and (
            state.ws_streamer_thread is None or not state.ws_streamer_thread.is_alive()
        ):
            state.ws_streamer_running = False
        if not state.ws_streamer_running:
            state.ws_streamer_running = True
            state.ws_streamer_thread = threading.Thread(
                target=_terminal_streamer, daemon=True
            )
            state.ws_streamer_thread.start()


def broadcast_autoyes_event(target, event, prompt_type):
    """Send auto-yes event to all connected WebSocket clients.

    Called by routes/autoyes.py to broadcast countdown/fired/cancelled events.
    """
    with state.autoyes_lock:
        cd = state.autoyes_countdowns.get(target)
        remaining = (
            max(0, cd["deadline"] - time.time()) if cd and event == "countdown" else 0
        )
        delay = cd.get("delay", state.AUTOYES_DELAY) if cd else state.AUTOYES_DELAY
        summary = cd.get("summary") if cd else None

    msg = json.dumps(
        {
            "type": "autoyes",
            "event": event,
            "target": target,
            "prompt_type": prompt_type,
            "remaining": round(remaining, 1),
            "delay": delay,
            "summary": summary,
        }
    )

    with state.ws_lock:
        clients = list(state.ws_clients)
    for client in clients:
        try:
            _send_to(client, msg)
        except Exception:
            _remove_ws_client(client["ws"])
