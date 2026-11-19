"""A durable pane key, a tmux pane id and a display target address the same pane
(effort 621 Phase 7 step A).

The durable key `<generation>:<pane_id>` qualifies `%N` with the tmux server it
belongs to (socket path, server pid and start time, hashed; see
shared/tmux.py:generation_of). `shared/utils.py:resolve_target` and the stream
subscribe accept it beside `%N` and `session:w.p`, and all three reach the same
pane for /type, /key, capture and subscribe. A key from another server
generation, or for a pane that is gone, resolves to nothing: never to whatever
pane now holds that `%N`, and never to the server-wide active target.

Step A changed no stored state; step B (tests/test_pane_state_cutover.py)
moves drafts and snoozes to durable keys, pinned here through a real poll.

Run: .venv/bin/python3 -m unittest tests.test_pane_keys
"""

import contextlib
import io
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import shared.state as state
from shared import tmux
from shared.utils import resolve_target
from tests._tmux_box import TmuxBox, wait_for_bytes

KEY_RE = re.compile(r"[0-9a-f]{16}:%\d+")
FIXED_CLOCK = 1_800_000_000.0


def _app(*blueprints):
    app = Flask(__name__)
    for bp in blueprints:
        app.register_blueprint(bp)
    return app.test_client()


class _FakeWS:
    def __init__(self, messages):
        self.environ = {"HTTP_ORIGIN": "http://127.0.0.1:8089"}
        self._messages = list(messages)
        self.sent = []
        self.closed = False

    def receive(self, timeout=None):
        return self._messages.pop(0) if self._messages else None

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True


class _PaneCase(unittest.TestCase):
    """dev has two panes (dev:1.1, dev:1.2), dev2 one, under base-index 1; each
    pane runs a raw reader that records every byte it receives."""

    def setUp(self):
        self.box = TmuxBox(self)
        self.recv = {}
        self.ids = {}
        for name, session, split in (("a", "dev", False), ("b", "dev", True), ("c", "dev2", False)):
            path, command = self.box.reader_command(name)
            self.recv[name] = path
            self.ids[name] = self.box.split(session, command) if split else self.box.new_session(session, command)
        for path in self.recv.values():
            wait_for_bytes(path, b"ready")
        refs = {r.pane_id: r for r in tmux.list_panes()}
        self.ref = refs[self.ids["b"]]
        self.assertEqual(self.ref.display_target, "dev:1.2")
        self.forms = {"key": self.ref.key, "pane_id": self.ref.pane_id, "display": "dev:1.2"}
        patcher = patch.object(state, "tmux_target", "dev2:1.1")
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_only_b_received(self, expected):
        wait_for_bytes(self.recv["b"], expected)
        # The other panes got nothing: a byte sent straight to each now is all it has.
        for name in ("a", "c"):
            self.box.tmux("send-keys", "-t", self.ids[name], "-l", "|")
            wait_for_bytes(self.recv[name], b"|")


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class ThreeFormsTests(_PaneCase):
    def setUp(self):
        super().setUp()
        import routes.input as input_routes
        import routes.terminal as terminal
        for p in (patch.object(input_routes.state, "touch_activity"),
                  patch.object(input_routes, "add_to_history")):
            p.start()
            self.addCleanup(p.stop)
        self.client = _app(input_routes.input_bp, terminal.terminal_bp)

    def test_resolve_target_takes_all_three_forms(self):
        self.assertEqual(resolve_target({"target": self.ref.key}), "dev:1.2")
        self.assertEqual(resolve_target({"target": self.ref.pane_id}), self.ref.pane_id)
        self.assertEqual(resolve_target({"target": "dev:1.2"}), "dev:1.2")
        self.assertEqual(resolve_target({"target": ""}), "dev2:1.1")
        for form in self.forms.values():
            target = resolve_target({"target": form})
            self.assertEqual(tmux.expected_target_identity(target).pane_id, self.ids["b"])

    def test_three_target_forms_address_same_pane_for_type_key_capture_subscribe(self):
        from routes import streaming

        for name, form in self.forms.items():
            with self.subTest(form=name, via="/type"):
                response = self.client.post("/type", json={
                    "target": form, "text": "t", "enter": False, "raw": True, "no_history": True})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assert_only_b_received(b"t")
            with self.subTest(form=name, via="/key"):
                response = self.client.post("/key", json={
                    "target": form, "combo": {"key": "k", "ctrl": False, "alt": False, "shift": False}})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assert_only_b_received(b"k")
            with self.subTest(form=name, via="capture"):
                response = self.client.get("/terminal/capture", query_string={"target": form, "lines": 20})
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertIn("PANE-b", response.get_json()["content"])
                self.assertNotIn("PANE-a", response.get_json()["content"])
            for via, messages in (
                ("subscribe", [json.dumps({"target": form, "lines": 20})]),
                ("resubscribe", [json.dumps({"target": "dev2:1.1", "lines": 20}),
                                 json.dumps({"type": "subscribe", "target": form, "lines": 20})]),
            ):
                with self.subTest(form=name, via=via):
                    frames = self.stream(streaming, messages)
                    self.assertEqual(frames[-1]["type"], "full", frames)
                    self.assertIn("PANE-b", frames[-1]["content"])

    def stream(self, streaming, messages):
        ws = _FakeWS(messages)
        with patch.object(streaming, "origin_allowed", return_value=True), \
                patch.object(streaming.auth, "request_device", return_value=None), \
                patch.object(streaming.auth, "header_authenticated", return_value=True), \
                patch.object(streaming, "_ensure_streamer"), \
                patch.object(streaming, "set_ws_send_timeout"), \
                Flask(__name__).test_request_context("/terminal/stream"):
            streaming.terminal_stream_handler(ws)
        self.assertNotIn(ws, [c["ws"] for c in state.ws_clients])
        return [json.loads(m) for m in ws.sent]

    def test_a_key_from_another_server_generation_reaches_no_pane(self):
        from routes import streaming

        # Same %N, wrong generation: what a tmux server restart leaves behind.
        orphan = f"{'0' * 16}:{self.ref.pane_id}"
        gone = f"{self.ref.generation}:%999"
        for form in (orphan, gone):
            with self.subTest(form=form):
                self.assertIsNone(resolve_target({"target": form}))
                response = self.client.post("/type", json={
                    "target": form, "text": "x", "enter": False, "raw": True, "no_history": True})
                self.assertEqual(response.status_code, 409)
                response = self.client.post("/key", json={
                    "target": form, "combo": {"key": "y"}})
                self.assertEqual(response.status_code, 409)
                response = self.client.get("/terminal/capture", query_string={"target": form})
                self.assertEqual(response.status_code, 404)
                frames = self.stream(streaming, [json.dumps({"target": form})])
                self.assertEqual([f["type"] for f in frames], ["error"])
                # Nothing landed anywhere, the active target (dev2) included.
                for name in ("a", "b", "c"):
                    self.box.tmux("send-keys", "-t", self.ids[name], "-l", "|")
                    wait_for_bytes(self.recv[name], b"|")


class _StopTick(BaseException):
    """Ends one streamer pass at its closing sleep."""


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class KeyIdentityLifetimeTests(_PaneCase):
    """A durable key keeps naming its own pane between lookup and use
    (milestone 2 sign-off F1/F2). Each schedule interleaves a real tmux change
    at the point where an address-based lookup would pick another pane: a swap
    (the key's pane moves, another takes its address), a replacement (the
    key's pane dies, a new one takes its address) and a tmux server restart
    that reuses `%N`. Each has an unchanged-key control that must still work."""

    def setUp(self):
        super().setUp()
        import routes.input as input_routes
        import routes.terminal as terminal
        self.inputs = input_routes
        self.terminal = terminal
        self.touched = []
        for p in (patch.object(input_routes.state, "touch_activity", side_effect=self.touched.append),
                  patch.object(input_routes, "add_to_history")):
            p.start()
            self.addCleanup(p.stop)
        self.client = _app(input_routes.input_bp, terminal.terminal_bp)
        self.key = self.ref.key

    # --- the three changes ------------------------------------------------
    def swap(self):
        self.box.tmux("swap-pane", "-s", self.ids["a"], "-t", self.ids["b"])
        self.assertEqual(tmux.resolve_pane_key(self.key).display_target, "dev:1.1")

    def replace(self):
        self.box.tmux("kill-pane", "-t", self.ids["b"])
        path, command = self.box.reader_command("replacement")
        self.recv["replacement"] = path
        self.ids["replacement"] = self.box.split("dev", command)
        wait_for_bytes(path, b"ready")
        self.assertEqual(self.box.display(self.ids["replacement"], "#{pane_index}"), "2")

    def restart(self):
        self.box.kill_server()
        for name, session, split in (("a2", "dev", False), ("b2", "dev", True)):
            path, command = self.box.reader_command(name)
            self.recv[name] = path
            self.ids[name] = self.box.split(session, command) if split else self.box.new_session(session, command)
            wait_for_bytes(path, b"ready")
        # The new server reuses the key's `%N`, at the same address.
        self.assertEqual(self.ids["b2"], self.ids["b"])
        self.assertIsNone(tmux.resolve_pane_key(self.key))

    def no_bytes(self, *names):
        for name in names:
            self.box.tmux("send-keys", "-t", self.ids[name], "-l", "|")
            wait_for_bytes(self.recv[name], b"|")

    # --- input ------------------------------------------------------------
    def _send(self, via, change=None):
        real = self.inputs.expected_target_identity
        def change_then_lookup(target):
            if change is not None:
                change()
            return real(target)
        body = ({"target": self.key, "text": "T", "enter": False, "raw": True, "no_history": True}
                if via == "/type" else {"target": self.key, "combo": {"key": "k"}})
        with patch.object(self.inputs, "expected_target_identity", side_effect=change_then_lookup):
            return self.client.post(via, json=body)

    def test_unchanged_key_control_types_and_keys_into_its_pane(self):
        for via, sent in (("/type", b"T"), ("/key", b"k")):
            with self.subTest(via=via):
                response = self._send(via)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assert_only_b_received(sent)

    def test_swap_before_identity_lookup_still_reaches_the_keyed_pane(self):
        response = self._send("/type", self.swap)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_only_b_received(b"T")
        # The key's pane now sits at dev:1.1; /key by key still reaches it.
        response = self._send("/key")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_only_b_received(b"k")

    def test_swap_before_key_identity_lookup_still_reaches_the_keyed_pane(self):
        response = self._send("/key", self.swap)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_only_b_received(b"k")

    def test_identity_of_another_pane_is_refused(self):
        other = tmux.expected_target_identity(self.ids["a"])
        for via in ("/type", "/key"):
            with self.subTest(via=via), \
                    patch.object(self.inputs, "expected_target_identity", return_value=other):
                body = ({"target": self.key, "text": "T", "enter": False, "raw": True, "no_history": True}
                        if via == "/type" else {"target": self.key, "combo": {"key": "k"}})
                response = self.client.post(via, json=body)
                self.assertEqual(response.status_code, 409)
        # The same identity under its own `%N` (not a key) is delivered: the
        # refusal is the key check, not the identity.
        with patch.object(self.inputs, "expected_target_identity", return_value=other):
            response = self.client.post("/type", json={
                "target": self.ids["a"], "text": "A", "enter": False, "raw": True, "no_history": True})
        self.assertEqual(response.status_code, 200)
        wait_for_bytes(self.recv["a"], b"A")
        self.no_bytes("b", "c")

    def _refused_after(self, change, fresh):
        response = self._send("/type", change)
        self.assertEqual(response.status_code, 409, response.get_json())
        response = self._send("/key")
        self.assertEqual(response.status_code, 409, response.get_json())
        self.no_bytes(*fresh)
        self.assertEqual(self.touched, [])

    def test_replacement_before_identity_lookup_is_refused(self):
        self._refused_after(self.replace, ("a", "replacement", "c"))

    def test_restart_before_identity_lookup_is_refused(self):
        self._refused_after(self.restart, ("a2", "b2"))

    def test_replacement_before_key_identity_lookup_is_refused(self):
        response = self._send("/key", self.replace)
        self.assertEqual(response.status_code, 409, response.get_json())
        self.no_bytes("a", "replacement", "c")

    def test_carried_identity_with_a_dead_key_is_refused(self):
        carried = tmux.expected_target_identity(self.ids["b"]).as_dict()
        self.replace()
        for via in ("/type/client-resume", "/type"):
            with self.subTest(via=via):
                response = self.client.post(via, json={
                    "target": self.key, "text": "R", "enter": False, "raw": True,
                    "no_history": True, "expected_target_identity": carried})
                self.assertEqual(response.status_code, 409)
        self.assertEqual(self.touched, [])
        self.no_bytes("a", "replacement", "c")

    def test_carried_identity_must_be_the_keys_pane(self):
        other = tmux.expected_target_identity(self.ids["a"]).as_dict()
        response = self.client.post("/type/client-resume", json={
            "target": self.key, "text": "R", "enter": False, "raw": True,
            "no_history": True, "expected_target_identity": other})
        self.assertEqual(response.status_code, 409)
        self.no_bytes("a", "b", "c")
        own = tmux.expected_target_identity(self.ids["b"]).as_dict()
        response = self.client.post("/type/client-resume", json={
            "target": self.key, "text": "R", "enter": False, "raw": True,
            "no_history": True, "expected_target_identity": own})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assert_only_b_received(b"R")

    # --- capture ----------------------------------------------------------
    def _capture(self, change=None):
        real = tmux.capture_pane_if_changed
        def change_then_capture(*args, **kwargs):
            if change is not None:
                change()
            return real(*args, **kwargs)
        with patch.object(tmux, "capture_pane_if_changed", side_effect=change_then_capture):
            return self.client.get("/terminal/capture", query_string={"target": self.key, "lines": 20})

    def _captured(self, change):
        response = self._capture(change)
        self.assertEqual(response.status_code, 200, response.get_json())
        body = response.get_json()
        self.assertIn("PANE-b", body["content"])
        self.assertNotIn("PANE-a", body["content"])
        self.assertEqual(body["target"], self.key)

    def _capture_refused(self, change):
        response = self._capture(change)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "target_absent")
        self.assertNotIn("content", response.get_json())

    def test_capture_by_key_control(self):
        self._captured(None)

    def test_capture_by_key_follows_its_pane_through_a_swap(self):
        self._captured(self.swap)

    def test_capture_by_key_refuses_a_replacement(self):
        self._capture_refused(self.replace)

    def test_capture_by_key_refuses_a_server_restart(self):
        self._capture_refused(self.restart)

    # --- stream -----------------------------------------------------------
    def stream_after(self, change, ticks=1):
        """Subscribe with the key, apply `change`, then run real streamer passes."""
        from routes import streaming
        owner = self

        class WS(_FakeWS):
            def receive(self, timeout=None):
                if self._messages:
                    return super().receive(timeout)
                owner.clients = [c for c in state.ws_clients if c["ws"] is self]
                if change is not None:
                    change()
                for _ in range(ticks):
                    def stop(_seconds):
                        raise _StopTick
                    with patch.object(streaming, "time", SimpleNamespace(time=time.time, sleep=stop)):
                        try:
                            streaming._terminal_streamer()
                        except _StopTick:
                            pass
                return None

        ws = WS([json.dumps({"target": self.key, "lines": 20})])
        with patch.object(streaming, "origin_allowed", return_value=True), \
                patch.object(streaming.auth, "request_device", return_value=None), \
                patch.object(streaming.auth, "header_authenticated", return_value=True), \
                patch.object(streaming, "_ensure_streamer"), \
                patch.object(streaming, "set_ws_send_timeout"), \
                Flask(__name__).test_request_context("/terminal/stream"):
            streaming.terminal_stream_handler(ws)
        return [json.loads(frame) for frame in ws.sent]

    def _stream_kept(self, change):
        frames = self.stream_after(change)
        self.assertEqual([f["type"] for f in frames], ["full", "full"], frames)
        for frame in frames:
            self.assertIn("PANE-b", frame["content"])
            self.assertNotIn("PANE-a", frame["content"])
            self.assertEqual(frame["target"], self.key)
        self.assertEqual(frames[0]["display_target"], "dev:1.2")
        self.assertIsNotNone(self.clients[0]["binding"])

    def test_stream_by_key_control(self):
        self._stream_kept(None)

    def test_stream_by_key_keeps_its_pane_after_a_move(self):
        self._stream_kept(self.swap)

    def _stream_expired(self, change):
        frames = self.stream_after(change, ticks=3)
        self.assertEqual(frames[0]["type"], "full")
        self.assertIn("PANE-b", frames[0]["content"])
        # One not-found frame, then nothing: never the pane now at the address.
        self.assertEqual(frames[1:], [{"type": "error", "error": "Target not found",
                                       "target": self.key}])
        self.assertIsNone(self.clients[0]["target"])
        self.assertIsNone(self.clients[0]["binding"])

    def test_stream_by_key_expires_on_replacement_and_stops(self):
        self._stream_expired(self.replace)

    def test_stream_by_key_expires_on_server_restart_and_stops(self):
        self._stream_expired(self.restart)

    def test_stream_by_key_resubscribe_after_expiry_works(self):
        from routes import streaming
        self.replace()
        frames = ThreeFormsTests.stream(self, streaming, [
            json.dumps({"target": "dev2:1.1", "lines": 20}),
            json.dumps({"type": "subscribe", "target": self.key, "lines": 20}),
            json.dumps({"type": "subscribe", "target": self.ids["replacement"], "lines": 20}),
        ])
        self.assertEqual([f["type"] for f in frames], ["full", "error", "full"])
        self.assertIn("PANE-replacement", frames[2]["content"])

    def test_dead_key_stream_returns_before_registration(self):
        from routes import streaming
        self.replace()
        with patch.object(streaming.devices, "register_stream") as register:
            frames = ThreeFormsTests.stream(self, streaming, [json.dumps({"target": self.key})])
        register.assert_not_called()
        self.assertEqual(frames, [{"type": "error", "error": "Target not found", "target": self.key}])

    # --- no server at all, and a tmux that does not answer ------------------
    def kill(self):
        self.box.kill_server()
        self.assertFalse(self.box.socket.exists())
        self.assertIsNone(tmux.resolve_pane_key(self.key))

    def test_server_killed_before_identity_lookup_is_refused_not_a_crash(self):
        response = self._send("/type", self.kill)
        self.assertEqual(response.status_code, 409, response.get_json())
        self.assertEqual(response.get_json()["error"], "target_absent")
        self.assertEqual(self.touched, [])

    def test_server_killed_refuses_every_key_surface(self):
        from routes import streaming
        self.kill()
        for via, body in (("/type", {"target": self.key, "text": "x", "enter": False, "raw": True,
                                     "no_history": True}),
                          ("/key", {"target": self.key, "combo": {"key": "y"}}),
                          ("/key", {"target": self.key, "keys": "ctrl+l"})):
            response = self.client.post(via, json=body)
            self.assertEqual(response.status_code, 409, (via, response.get_json()))
            self.assertEqual(response.get_json()["error"], "target_absent")
        response = self.client.get("/terminal/capture", query_string={"target": self.key})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "target_absent")
        frames = ThreeFormsTests.stream(self, streaming, [json.dumps({"target": self.key})])
        self.assertEqual(frames, [{"type": "error", "error": "Target not found", "target": self.key}])
        self.assertEqual(self.touched, [])

    def test_capture_by_key_refuses_a_server_killed_during_it(self):
        self._capture_refused(self.kill)

    def test_stream_by_key_expires_when_the_server_is_killed_and_stops(self):
        self._stream_expired(self.kill)

    def test_a_tmux_that_does_not_answer_is_unavailable_not_gone(self):
        from routes import streaming
        hung = subprocess.TimeoutExpired(["tmux"], 5)
        with patch.object(tmux.subprocess, "run", side_effect=hung):
            for via, body in (("/type", {"target": self.key, "text": "x", "enter": False, "raw": True,
                                         "no_history": True}),
                              ("/key", {"target": self.key, "combo": {"key": "y"}})):
                response = self.client.post(via, json=body)
                self.assertEqual(response.status_code, 503, via)
                self.assertEqual(response.get_json()["error"], "tmux_unavailable")
            response = self.client.get("/terminal/capture", query_string={"target": self.key})
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.touched, [])
        self.no_bytes("a", "b", "c")
        # A stream whose capture fails while tmux does not answer keeps its
        # subscription: one hung call is not the key expiring.
        binding = tmux.bind_pane_key(self.key)
        with patch.object(streaming, "capture_pane_by_key", return_value=(None, None, None)), \
                patch.object(streaming, "bind_pane_key_status", return_value=(None, "unknown")):
            self.assertFalse(streaming._key_capture(binding, 20, None)[3])
        with patch.object(streaming, "capture_pane_by_key", return_value=(None, None, None)), \
                patch.object(streaming, "bind_pane_key_status", return_value=(None, "gone")):
            self.assertTrue(streaming._key_capture(binding, 20, None)[3])
        self.assertEqual(tmux.bind_pane_key_status(self.key)[1], "bound")

    # --- review findings 3 and 4 ------------------------------------------
    def test_one_proc_read_per_listing(self):
        with patch.object(tmux, "_process_start_time", wraps=tmux._process_start_time) as reads:
            refs = tmux.list_panes()
        self.assertEqual(len(refs), 3)
        self.assertEqual(reads.call_count, 1)

    def test_real_pre_restart_key_and_foreign_socket_key_reach_nothing(self):
        from routes import streaming
        stale = self.key
        self.restart()
        # A second live server on another socket, its own `%N` equal to the key's.
        foreign = TmuxBox.__new__(TmuxBox)
        foreign.root = Path(tempfile.mkdtemp(prefix="p7b-f-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, foreign.root, ignore_errors=True)
        foreign.socket = foreign.root / "s"
        foreign.conf = self.box.conf
        self.addCleanup(foreign.kill_server)
        foreign.tmux("new-session", "-d", "-s", "dev", "sleep 600")
        foreign.tmux("split-window", "-d", "-t", "=dev:", "sleep 600")
        out = foreign.tmux("display-message", "-p", "-t", "=dev:1.2", "#{pane_id}\t#{pid}\t#{socket_path}").strip()
        pane_id, pid, socket_path = out.split("\t")
        foreign_key = f"{tmux.generation_of(socket_path, int(pid), tmux._process_start_time(int(pid)))}:{pane_id}"
        self.assertEqual(pane_id, self.ids["b2"])
        for form in (stale, foreign_key):
            with self.subTest(form=form):
                self.assertIsNone(resolve_target({"target": form}))
                for via, body in (("/type", {"target": form, "text": "x", "enter": False, "raw": True,
                                             "no_history": True}),
                                  ("/key", {"target": form, "combo": {"key": "y"}}),
                                  ("/key", {"target": form, "keys": "ctrl+l"})):
                    self.assertEqual(self.client.post(via, json=body).status_code, 409, via)
                self.assertEqual(self.client.get("/terminal/capture",
                                                 query_string={"target": form}).status_code, 404)
                frames = ThreeFormsTests.stream(self, streaming, [json.dumps({"target": form})])
                self.assertEqual([f["type"] for f in frames], ["error"])
                self.no_bytes("a2", "b2")
        self.assertEqual(self.touched, [])


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class StoredStateTests(_PaneCase):
    def setUp(self):
        super().setUp()
        from shared import drafts, tab_state

        self.dir = Path(tempfile.mkdtemp(prefix="p7a-state-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.drafts_file = self.dir / "drafts.json"
        self.tab_file = self.dir / "tab_state.json"
        self.idle_file = self.dir / "idle_state.json"
        self.seed_drafts = {
            "dev:1.1": {"text": "first", "attachments": [], "enter_armed": False, "updated_at": 100.0},
            "old:1.1": {"text": "a gone pane's draft", "attachments": [], "enter_armed": True,
                        "updated_at": 50.0},
        }
        self.seed_tabs = {
            "pinned": ["dev"], "order": ["dev2", "dev"],
            "snoozed": {"dev:1.1": {"at": FIXED_CLOCK - 10, "was_busy": False}},
            "sort": "manual", "agent_declarations": {},
        }
        self.drafts_file.write_text(json.dumps(self.seed_drafts), encoding="utf-8")
        self.tab_file.write_text(json.dumps(self.seed_tabs), encoding="utf-8")
        legacy = self.drafts_file.read_bytes(), self.tab_file.read_bytes()
        self.addCleanup(lambda: self.assertEqual(
            (self.drafts_file.read_bytes(), self.tab_file.read_bytes()), legacy))
        for p in (patch.object(drafts, "DRAFTS_FILE", self.drafts_file),
                  patch.object(tab_state, "TAB_STATE_FILE", self.tab_file),
                  patch.object(drafts, "DRAFTS_V2_FILE", self.dir / "drafts.v2.json"),
                  patch.object(tab_state, "TAB_STATE_V2_FILE", self.dir / "tab_state.v2.json"),
                  patch.object(state, "_IDLE_STATE_FILE", self.idle_file),
                  patch.object(state, "pane_last_activity", {}),
                  patch.object(state, "pane_content_hash", {}),
                  patch("routes.studio.studio_poll_block", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        drafts.load_drafts()
        tab_state.load_tab_state()
        self.addCleanup(drafts.load_drafts)
        self.addCleanup(tab_state.load_tab_state)

    def test_old_client_writes_after_step_b_land_on_durable_keys(self):
        """Step A kept every stored key a `session:w.p`; step B (the cutover)
        converts on first use. After a real poll, an old client's draft save and
        snooze, the files are schema 2 keyed by durable key, every seeded field
        kept, the gone pane's draft an orphan, and idle state still display-keyed."""
        import routes.drafts as drafts_routes
        import routes.poll as poll
        import routes.tabstate as tabstate

        client = _app(poll.poll_bp, drafts_routes.drafts_bp, tabstate.tabstate_bp)
        body = client.get("/poll").get_json()
        self.assertTrue(body["sessions"])
        self.assertTrue(all(KEY_RE.fullmatch(p["key"]) for p in body["sessions"]))
        keys = {p["target"]: p["key"] for p in body["sessions"]}

        with patch("time.time", return_value=FIXED_CLOCK):
            response = client.put("/api/draft", json={"target": "dev:1.2", "text": "hello"})
            self.assertEqual(response.status_code, 200)
            response = client.post("/api/tab-state/snooze", json={"target": "dev2:1.1", "on": True})
            self.assertEqual(response.status_code, 200)

        # The converted store is drafts.v2.json; drafts.json stays as seeded
        # (checked at cleanup).
        v2 = json.loads((self.dir / "drafts.v2.json").read_text(encoding="utf-8"))
        drafts_on_disk = {"schema": v2["schema"], **v2["drafts"]}
        self.assertEqual(drafts_on_disk.pop("schema"), 2)
        self.assertEqual(drafts_on_disk[keys["dev:1.1"]],
                         {**self.seed_drafts["dev:1.1"], "legacy_from": "dev:1.1"})
        self.assertEqual(drafts_on_disk[keys["dev:1.2"]],
                         {"text": "hello", "attachments": [], "enter_armed": True,
                          "updated_at": FIXED_CLOCK})
        self.assertEqual(drafts_on_disk["old:1.1"]["text"], "a gone pane's draft")
        self.assertTrue(drafts_on_disk["old:1.1"]["orphan"])
        tabs_on_disk = json.loads((self.dir / "tab_state.v2.json").read_text(encoding="utf-8"))
        self.assertEqual(tabs_on_disk["schema"], 2)
        self.assertEqual(tabs_on_disk["pinned"], ["dev"])
        self.assertEqual(tabs_on_disk["order"], ["dev2", "dev"])
        self.assertEqual(set(tabs_on_disk["snoozed"]), {keys["dev:1.1"], keys["dev2:1.1"]})
        # The doc a browser reads still names panes by display target.
        doc = client.get("/api/tab-state").get_json()["tab_state"]
        self.assertEqual(set(doc["snoozed"]), {"dev:1.1", "dev2:1.1"})
        # Idle state is not pane-keyed state: it stays display-keyed.
        state.save_idle_state()
        self.assertIsNone(KEY_RE.search(self.idle_file.read_text(encoding="utf-8")))


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class CliTests(_PaneCase):
    def setUp(self):
        super().setUp()
        import routes.input as input_routes
        import routes.poll as poll
        import routes.terminal as terminal
        for p in (patch.object(state, "save_idle_state"),
                  patch.object(state, "pane_last_activity", {}),
                  patch.object(state, "pane_content_hash", {}),
                  patch.object(input_routes.state, "touch_activity"),
                  patch("routes.poll.tab_state.sweep_wakes"),
                  patch("routes.poll.drafts.poll_block", return_value=None),
                  patch("routes.studio.studio_poll_block", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        self.client = _app(poll.poll_bp, terminal.terminal_bp, input_routes.input_bp)
        self.posted = []

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_json()

    def post(self, path, data=None):
        self.posted.append((path, data))
        response = self.client.post(path, json=data)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_cli_ls_json_carries_key_and_session_verbs_need_no_key(self):
        from cli import session as cli_session

        out = io.StringIO()
        with patch.object(cli_session.http, "get", side_effect=self.get), \
                patch.object(cli_session.http, "post", side_effect=self.post), \
                patch.dict("os.environ", {"ASSIST_NO_HINTS": "1"}), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli_session.list_sessions(json_output=True), 0)
            listed = json.loads(out.getvalue())["sessions"]
            keys = {p["pane_id"]: p["key"] for p in listed}
            self.assertEqual(keys, {r.pane_id: r.key for r in tmux.list_panes()})
            self.assertTrue(all(KEY_RE.fullmatch(k) for k in keys.values()))

            # A session verb by name: no key needed, none sent, and it lands.
            # (--pane names the pane: the default `0.0` resolves to no pane
            # under base-index 1, before and after this step.)
            self.assertEqual(cli_session.send("dev2", "s", pane="1.1"), 0)
        [(path, payload)] = self.posted
        self.assertEqual(path, "/type")
        self.assertEqual(payload["target"], "dev2:1.1")
        self.assertNotIn("key", payload)
        wait_for_bytes(self.recv["c"], b"s")


if __name__ == "__main__":
    unittest.main()
