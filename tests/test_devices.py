"""Per-device browser sign-in (effort 621, Phase 1).

Before this, every browser held the same cookie — an HMAC of the token — so a
lost phone could only be locked out by rotating the token, which also logged
out every other device and every script. Each admission now mints a device
record and a cookie bound to it (`v2.<id>.<mac>`), and one device can be
revoked without touching anything else. The header token is unchanged.

These tests drive the real issuers (the login form, the open window through
the auth gate, the approval status route) with Flask's test client, and the
registry through its own module, against a scratch `devices.json`.
"""

import ast
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

import shared.auth as auth
import shared.devices as devices
from shared import auth_gate

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "test-token-not-the-real-one"
LAN_IP = "10.0.0.42"
OTHER_IP = "10.0.0.43"


class _Clock:
    """Stands in for the `time` module in shared.auth and shared.devices."""

    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_761_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall

    def advance(self, seconds):
        self.mono += seconds
        self.wall += seconds

    def sleep(self, seconds):
        time.sleep(seconds)


class _FakeWS:
    def __init__(self, messages=(), on_receive=None):
        self.environ = {"HTTP_ORIGIN": "http://127.0.0.1:8089"}
        self._messages = list(messages)
        self._on_receive = on_receive
        self.sent = []
        self.closed = False

    def receive(self, timeout=None):
        if self._on_receive is not None:
            hook, self._on_receive = self._on_receive, None
            hook()
        if self._messages:
            return self._messages.pop(0)
        return None

    def send(self, msg):
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(msg)

    def close(self):
        self.closed = True


def _reset_auth_state():
    auth._window_deadline = None
    auth._window_reservation = None
    auth._window_source = "window"
    auth._last_onboard = None
    auth._requests.clear()
    auth._request_last_create.clear()


class DeviceTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "devices.json"

        saved_token = auth._token_cache
        auth._token_cache = TOKEN
        self.addCleanup(setattr, auth, "_token_cache", saved_token)

        patches = [
            mock.patch.object(devices, "DEVICES_FILE", self.path),
            mock.patch.object(auth, "_networks_raw", return_value="10.0.0.0/24"),
            mock.patch.object(auth, "_request_cfg", return_value=(300.0, 10, 0.0)),
        ]
        self.clock = _Clock()
        patches.append(mock.patch.object(auth, "time", self.clock))
        patches.append(mock.patch.object(devices, "time", self.clock))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        _reset_auth_state()
        self.addCleanup(_reset_auth_state)
        devices.reload()
        self.addCleanup(devices.reload)

        from shared import state

        saved_clients = list(state.ws_clients)
        self.addCleanup(lambda: state.ws_clients.__setitem__(slice(None), saved_clients))

        self.app = self._make_app()

    def _make_app(self):
        from routes.access import access_bp
        from routes.static import static_bp

        app = Flask(__name__)
        auth_gate.register_auth_gate(app)
        app.register_blueprint(static_bp)
        app.register_blueprint(access_bp)
        return app

    # -- helpers ---------------------------------------------------------

    def client(self):
        return self.app.test_client()

    @staticmethod
    def cookie_of(response):
        for header in response.headers.getlist("Set-Cookie"):
            if header.startswith(auth.COOKIE_NAME + "="):
                return header.split(";", 1)[0].split("=", 1)[1]
        return None

    def login(self):
        c = self.client()
        r = c.post("/login", data={"token": TOKEN})
        self.assertEqual(r.status_code, 302, r.data)
        cookie = self.cookie_of(r)
        self.assertTrue(cookie, "login set no cookie")
        return cookie

    def cookie_client(self, cookie):
        c = self.client()
        c.set_cookie(auth.COOKIE_NAME, cookie)
        return c

    def get_with_cookie(self, cookie, path="/access/devices"):
        return self.cookie_client(cookie).get(path)

    def header(self):
        return {auth.HEADER_NAME: TOKEN}

    def registry_on_disk(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def active_ids(self):
        return [d["id"] for d in devices.list_devices() if not d["revoked_at"]]

    def fill_active(self, count):
        for _ in range(count):
            result = devices.admit(devices.TokenLogin(), ip="127.0.0.1", ua="seed")
            self.assertIsInstance(result, dict, result)

    def request_and_approve(self, ip=LAN_IP):
        c = self.client()
        r = c.post("/access/request", environ_base={"REMOTE_ADDR": ip})
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        rid = body["request"]["id"]
        claim = body["claim"]
        r = self.client().post(
            "/access/decide", json={"id": rid, "action": "approve"}, headers=self.header()
        )
        self.assertTrue(r.get_json()["ok"])
        return claim

    def poll_status(self, claim, ip=LAN_IP):
        return self.client().get(
            "/access/request/status",
            query_string={"claim": claim},
            environ_base={"REMOTE_ADDR": ip},
        )

    def navigate_index(self, ip=LAN_IP):
        return self.client().get(
            "/",
            headers={"Accept": "text/html", "Sec-Fetch-Dest": "document"},
            environ_base={"REMOTE_ADDR": ip},
        )


class CookieTests(DeviceTestBase):
    def test_v2_cookie_round_trip(self):
        cookie = self.login()
        self.assertTrue(cookie.startswith("v2."), cookie)
        r = self.get_with_cookie(cookie)
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body["devices"]), 1)
        self.assertEqual(body["this_device"], body["devices"][0]["id"])
        self.assertEqual(body["devices"][0]["source"], "login")

    def test_two_browsers_get_different_cookies(self):
        a = self.login()
        b = self.login()
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.active_ids()), 2)
        self.assertEqual(self.get_with_cookie(a).status_code, 200)
        self.assertEqual(self.get_with_cookie(b).status_code, 200)

    def test_v1_cookie_refused_after_upgrade(self):
        import hashlib
        import hmac

        v1 = hmac.new(TOKEN.encode(), b"assist-auth-v1", hashlib.sha256).hexdigest()
        r = self.get_with_cookie(v1)
        self.assertEqual(r.status_code, 401)
        # A browser navigation with the old cookie lands on the sign-in page.
        r = self.cookie_client(v1).get("/", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"))
        # The header still works.
        self.assertEqual(self.client().get("/access/devices", headers=self.header()).status_code, 200)

    def test_forged_and_malformed_v2_cookies_refused(self):
        cookie = self.login()
        _v, device_id, mac = cookie.split(".")
        other = self.login().split(".")[1]
        for bad in (
            f"v2.{other}.{mac}",          # another device's id under this mac
            f"v2.{device_id}.{'0' * 64}",  # wrong mac
            f"v2.{device_id}",             # truncated
            f"v3.{device_id}.{mac}",       # unknown version
            "",
        ):
            self.assertEqual(self.get_with_cookie(bad).status_code, 401, bad)

    def test_name_clamped_and_rendered_as_text(self):
        cookie = self.login()
        long_name = "<b>x</b>" + "y" * 100 + "\x07"
        r = self.cookie_client(cookie).post("/access/devices/name", json={"name": long_name})
        self.assertEqual(r.status_code, 200, r.data)
        record = devices.list_devices()[0]
        self.assertEqual(len(record["name"]), devices.NAME_MAX)
        self.assertTrue(record["name"].startswith("<b>x</b>"))
        self.assertNotIn("\x07", record["name"])
        self.assertTrue(record["named"])
        # Must-not-fire: an empty name is refused, not stored.
        r = self.cookie_client(cookie).post("/access/devices/name", json={"name": "   "})
        self.assertEqual(r.status_code, 400)
        # The sheet renders names as text, never as markup.
        source = (ROOT / "js" / "devices.js").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", source)
        self.assertIn("textContent", source)

    def test_last_seen_write_throttled_five_minutes(self):
        cookie = self.login()
        writes = []
        real = devices._write_file

        def counting(path, data):
            writes.append(data)
            return real(path, data)

        with mock.patch.object(devices, "_write_file", side_effect=counting):
            self.clock.advance(10)
            self.assertEqual(self.get_with_cookie(cookie).status_code, 200)
            self.assertEqual(writes, [])
            self.clock.advance(devices.TOUCH_INTERVAL)
            self.assertEqual(self.get_with_cookie(cookie).status_code, 200)
            self.assertEqual(len(writes), 1)
            self.clock.advance(30)
            self.assertEqual(self.get_with_cookie(cookie).status_code, 200)
            self.assertEqual(len(writes), 1)
        self.assertEqual(self.registry_on_disk()["devices"][0]["last_seen"], self.clock.wall - 30)


class RevocationTests(DeviceTestBase):
    def test_revoke_signs_out_only_that_device(self):
        a = self.login()
        b = self.login()
        a_id = a.split(".")[1]
        r = self.client().post("/access/devices/revoke", json={"id": a_id}, headers=self.header())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.get_with_cookie(a).status_code, 401)
        self.assertEqual(self.get_with_cookie(b).status_code, 200)
        self.assertEqual(self.client().get("/access/devices", headers=self.header()).status_code, 200)
        on_disk = {d["id"]: d for d in self.registry_on_disk()["devices"]}
        self.assertIsNotNone(on_disk[a_id]["revoked_at"])

    def test_revoking_own_device_needs_confirm_self(self):
        a = self.login()
        a_id = a.split(".")[1]
        r = self.cookie_client(a).post("/access/devices/revoke", json={"id": a_id})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.get_with_cookie(a).status_code, 200)
        r = self.cookie_client(a).post(
            "/access/devices/revoke", json={"id": a_id, "confirm_self": True}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get_with_cookie(a).status_code, 401)

    def test_revoke_ack_only_after_durable_write(self):
        a = self.login()
        a_id = a.split(".")[1]
        with mock.patch.object(devices, "_write_file", side_effect=OSError("disk full")):
            r = self.client().post("/access/devices/revoke", json={"id": a_id}, headers=self.header())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.get_with_cookie(a).status_code, 200)
        self.assertIsNone(self.registry_on_disk()["devices"][0]["revoked_at"])

    def test_forget_removes_only_revoked_rows(self):
        a = self.login()
        a_id = a.split(".")[1]
        r = self.client().post("/access/devices/forget", json={"id": a_id}, headers=self.header())
        self.assertEqual(r.status_code, 409)
        self.client().post("/access/devices/revoke", json={"id": a_id}, headers=self.header())
        r = self.client().post("/access/devices/forget", json={"id": a_id}, headers=self.header())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(devices.list_devices(), [])

    def test_pending_touch_cannot_restore_revoked_or_reset_row(self):
        a = self.login()
        a_id = a.split(".")[1]
        devices.revoke(a_id, confirm_self=True)
        self.clock.advance(devices.TOUCH_INTERVAL + 1)
        devices.touch(a_id, ip="10.0.0.9")
        rec = self.registry_on_disk()["devices"][0]
        self.assertIsNotNone(rec["revoked_at"])
        self.assertNotEqual(rec["ip"], "10.0.0.9")

        b = self.login()
        b_id = b.split(".")[1]
        devices.reset()
        self.clock.advance(devices.TOUCH_INTERVAL + 1)
        devices.touch(b_id, ip="10.0.0.9")
        self.assertEqual(self.registry_on_disk()["devices"], [])
        self.assertEqual(self.get_with_cookie(b).status_code, 401)

    def test_devices_reset_requires_header(self):
        a = self.login()
        r = self.cookie_client(a).post("/access/devices/reset")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.get_with_cookie(a).status_code, 200)
        r = self.client().post("/access/devices/reset", headers=self.header())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get_with_cookie(a).status_code, 401)


class StreamTests(DeviceTestBase):
    def _client(self):
        return {"ws": _FakeWS(), "lock": threading.Lock(), "target": "x:1.1", "lines": 10,
                "tui": None, "last_send": 0}

    def _register(self, device_id):
        from shared import state

        client = self._client()

        def add():
            with state.ws_lock:
                state.ws_clients.append(client)

        self.assertTrue(devices.register_stream(device_id, client, add))
        return client

    def test_revoke_closes_registered_stream(self):
        from shared import state

        a_id = self.login().split(".")[1]
        b_id = self.login().split(".")[1]
        ca = self._register(a_id)
        cb = self._register(b_id)
        devices.revoke(a_id, confirm_self=True)
        self.assertTrue(ca["ws"].closed)
        self.assertFalse(cb["ws"].closed)
        self.assertNotIn(ca, state.ws_clients)
        self.assertIn(cb, state.ws_clients)

    def test_header_stream_survives_all_revocations(self):
        a_id = self.login().split(".")[1]
        dev = self._register(a_id)
        hdr = self._register(None)
        devices.revoke(a_id, confirm_self=True)
        devices.reset()
        self.assertTrue(dev["ws"].closed)
        self.assertFalse(hdr["ws"].closed)

    def test_reset_closes_device_streams_keeps_header_streams(self):
        from shared import state

        ids = [self.login().split(".")[1] for _ in range(3)]
        dev_clients = [self._register(i) for i in ids]
        hdr = self._register(None)
        devices.reset()
        self.assertTrue(all(c["ws"].closed for c in dev_clients))
        self.assertFalse(hdr["ws"].closed)
        self.assertEqual(state.ws_clients[-1:], [hdr])
        self.assertEqual(devices.list_devices(), [])

    def test_revoke_closes_registered_stream_and_blocks_handshake_race(self):
        """Review focus 2: a revoke between handshake auth and registration."""
        from routes import streaming
        from shared import state

        cookie = self.login()
        device_id = cookie.split(".")[1]
        sub = json.dumps({"target": "s:1.1", "lines": 10})

        # Revoke lands while the handler waits for the subscription message,
        # i.e. after the handshake authenticated and before registration.
        ws = _FakeWS([sub], on_receive=lambda: devices.revoke(device_id, confirm_self=True))
        with mock.patch.object(streaming, "origin_allowed", return_value=True), \
                mock.patch.object(streaming, "capture_pane", return_value=("x", {})) as cap, \
                mock.patch.object(streaming, "_ensure_streamer"), \
                self.app.test_request_context(
                    "/terminal/stream", headers={"Cookie": f"{auth.COOKIE_NAME}={cookie}"}
                ):
            before = list(state.ws_clients)
            streaming.terminal_stream_handler(ws)
        self.assertEqual(state.ws_clients, before)
        self.assertTrue(ws.closed)
        self.assertFalse(any('"full"' in m for m in ws.sent))
        cap.assert_not_called()

        # Must-not-fire: the same flow without a revoke registers and streams.
        other = self.login()
        ws2 = _FakeWS([sub])
        registered = []

        def snapshot_then_end():
            registered.append(any(c["ws"] is ws2 for c in state.ws_clients))

        ws2._messages.append(None)
        with mock.patch.object(streaming, "origin_allowed", return_value=True), \
                mock.patch.object(streaming, "capture_pane", return_value=("x", {})), \
                mock.patch.object(streaming, "_ensure_streamer", side_effect=snapshot_then_end), \
                self.app.test_request_context(
                    "/terminal/stream", headers={"Cookie": f"{auth.COOKIE_NAME}={other}"}
                ):
            streaming.terminal_stream_handler(ws2)
        self.assertEqual(registered, [True])
        self.assertTrue(any('"full"' in m for m in ws2.sent))

    def test_stream_without_credential_closes_before_reading(self):
        from routes import streaming

        ws = _FakeWS([json.dumps({"target": "s:1.1"})])
        with mock.patch.object(streaming, "origin_allowed", return_value=True), \
                mock.patch.object(streaming, "capture_pane", return_value=("x", {})) as cap, \
                self.app.test_request_context("/terminal/stream"):
            streaming.terminal_stream_handler(ws)
        self.assertTrue(ws.closed)
        self.assertEqual(ws._messages, [json.dumps({"target": "s:1.1"})])
        cap.assert_not_called()


class RegistryIntegrityTests(DeviceTestBase):
    def _corrupt(self):
        self.path.write_bytes(b"{not json")

    def test_corrupt_registry_refuses_cookies_keeps_header_and_does_not_recreate(self):
        a = self.login()
        self._corrupt()
        devices.reload()
        self.assertEqual(self.get_with_cookie(a).status_code, 401)
        r = self.client().get("/access/devices", headers=self.header())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["recovery_required"])
        r = self.client().post("/login", data={"token": TOKEN})
        self.assertNotEqual(r.status_code, 302)
        self.assertIsNone(self.cookie_of(r))
        on_disk = self.registry_on_disk()
        self.assertTrue(on_disk["recovery_required"])
        self.assertEqual(on_disk["devices"], [])
        backups = list(self.dir.glob("devices.json.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), b"{not json")

    def test_wrong_schema_is_quarantined_too(self):
        self.path.write_text(json.dumps({"schema": 99, "devices": []}), encoding="utf-8")
        devices.reload()
        self.assertTrue(devices.recovery_required())

    def test_corrupt_registry_stays_read_only_across_restart_until_reset(self):
        self.login()
        self._corrupt()
        devices.reload()
        devices.reload()  # a second restart
        self.assertTrue(devices.recovery_required())
        self.assertIsInstance(devices.admit(devices.TokenLogin(), ip="127.0.0.1", ua=""), devices.AdmitRefused)
        r = self.client().post("/access/devices/reset", headers=self.header())
        self.assertEqual(r.status_code, 200)
        devices.reload()
        self.assertFalse(devices.recovery_required())
        self.login()

    def test_unreadable_registry_is_recovery_mode_without_writes(self):
        self.login()
        before = self.path.read_bytes()
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
            devices.reload()
        self.assertTrue(devices.recovery_required())
        self.assertEqual(self.path.read_bytes(), before)

    def test_interrupted_quarantine_before_and_after_marker_rename_never_yields_fresh_registry(self):
        # Point 1: the backup is written, the marker write fails. The invalid
        # file must still be in place, and this process is in recovery mode.
        self._corrupt()
        with mock.patch.object(devices, "_write_file", side_effect=OSError("crash")):
            devices.reload()
        self.assertTrue(devices.recovery_required())
        self.assertEqual(self.path.read_bytes(), b"{not json")
        # Restart: still recovery, never a fresh registry.
        devices.reload()
        self.assertTrue(devices.recovery_required())

        # Point 0: the backup itself fails: the original stays, recovery mode.
        self._corrupt()
        with mock.patch.object(devices, "_backup_invalid", side_effect=OSError("crash")):
            devices.reload()
        self.assertTrue(devices.recovery_required())
        self.assertEqual(self.path.read_bytes(), b"{not json")

        # Point 2: the marker rename happened, the directory fsync failed.
        self._corrupt()
        with mock.patch.object(devices, "_fsync_dir", side_effect=OSError("crash")):
            devices.reload()
        self.assertTrue(devices.recovery_required())
        devices.reload()
        self.assertTrue(devices.recovery_required())
        self.assertTrue(self.registry_on_disk()["recovery_required"])
        self.assertIsInstance(devices.admit(devices.TokenLogin(), ip="127.0.0.1", ua=""), devices.AdmitRefused)

    def test_missing_file_is_empty_registry_not_recovery(self):
        devices.reload()
        self.assertFalse(devices.recovery_required())
        self.assertFalse(self.path.exists())


class AdmissionTests(DeviceTestBase):
    def test_cap_32_active_refuses_33rd_without_consuming_admission(self):
        self.fill_active(devices.MAX_ACTIVE)
        auth.open_window(60)
        r = self.navigate_index()
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(self.cookie_of(r))
        self.assertTrue(auth.window_state()["open"])
        claim = self.request_and_approve(OTHER_IP)
        r = self.poll_status(claim, OTHER_IP)
        self.assertEqual(r.status_code, 409)
        self.assertIsNone(self.cookie_of(r))
        self.assertEqual(len(self.active_ids()), devices.MAX_ACTIVE)
        # The admission survives: revoke one, and both go through.
        devices.revoke(self.active_ids()[0], confirm_self=True)
        r = self.poll_status(claim, OTHER_IP)
        self.assertEqual(r.get_json()["status"], "approved")
        self.assertTrue(self.cookie_of(r))

    def test_revoked_rows_pruned_to_make_room_active_never_evicted(self):
        self.fill_active(devices.MAX_ACTIVE)
        for device_id in self.active_ids()[:4]:
            devices.revoke(device_id, confirm_self=True)
        for _ in range(4):
            self.login()
        self.assertEqual(len(self.active_ids()), devices.MAX_ACTIVE)
        self.assertLessEqual(len(devices.list_devices()), devices.MAX_RECORDS)

    def test_two_simultaneous_window_navigations_yield_one_device_one_admission(self):
        auth.open_window(60)
        real = devices._write_file
        barrier = threading.Barrier(2, timeout=5)
        results = []

        def slow_write(path, data):
            time.sleep(0.05)  # widen the window between reservation and consumption
            return real(path, data)

        def go(ip):
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            results.append(self.navigate_index(ip))

        with mock.patch.object(devices, "_write_file", side_effect=slow_write):
            threads = [threading.Thread(target=go, args=(ip,)) for ip in (LAN_IP, OTHER_IP)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        cookies = [self.cookie_of(r) for r in results]
        self.assertEqual(sum(1 for c in cookies if c), 1, [r.status_code for r in results])
        self.assertEqual(len(devices.list_devices()), 1)
        self.assertEqual(devices.list_devices()[0]["source"], "window")
        self.assertFalse(auth.window_state()["open"])

    def test_pair_window_records_pair_source(self):
        auth.open_window(60, source="pair")
        r = self.navigate_index()
        self.assertTrue(self.cookie_of(r))
        self.assertEqual(devices.list_devices()[0]["source"], "pair")

    def test_two_simultaneous_claim_polls_yield_one_device(self):
        claim = self.request_and_approve()
        real = devices._write_file
        barrier = threading.Barrier(2, timeout=5)
        results = []

        def slow_write(path, data):
            time.sleep(0.05)
            return real(path, data)

        def go():
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            results.append(self.poll_status(claim))

        with mock.patch.object(devices, "_write_file", side_effect=slow_write):
            threads = [threading.Thread(target=go) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        cookies = {self.cookie_of(r) for r in results}
        self.assertEqual(len(devices.list_devices()), 1)
        # Both answers are the one device (the second is a replay).
        self.assertEqual(len(cookies - {None}), 1)
        self.assertEqual(devices.list_devices()[0]["source"], "approval")

    def test_closed_expired_foreign_claims_allocate_nothing(self):
        # Closed window.
        r = self.navigate_index()
        self.assertEqual(r.status_code, 302)
        # Expired window.
        auth.open_window(5)
        self.clock.advance(6)
        r = self.navigate_index()
        self.assertEqual(r.status_code, 302)
        # Out-of-scope client on an open window.
        auth.open_window(60)
        r = self.navigate_index("192.168.9.9")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(auth.window_state()["open"])
        # Pending, foreign-IP, unknown and expired claims.
        c = self.client()
        pending = c.post("/access/request", environ_base={"REMOTE_ADDR": LAN_IP}).get_json()["claim"]
        self.assertEqual(self.poll_status(pending).get_json()["status"], "pending")
        claim = self.request_and_approve(OTHER_IP)
        self.assertEqual(self.poll_status(claim, LAN_IP).get_json()["status"], "expired")
        self.assertEqual(self.poll_status("nope").get_json()["status"], "expired")
        self.clock.advance(301)
        self.assertEqual(self.poll_status(claim, OTHER_IP).get_json()["status"], "expired")
        self.assertEqual(devices.list_devices(), [])

    def test_write_failure_before_consumption_leaves_admission_retryable(self):
        auth.open_window(60)
        claim = self.request_and_approve(OTHER_IP)
        with mock.patch.object(devices, "_write_file", side_effect=OSError("disk full")):
            r = self.navigate_index()
            self.assertEqual(r.status_code, 503)
            self.assertIsNone(self.cookie_of(r))
            r = self.poll_status(claim, OTHER_IP)
            self.assertEqual(r.status_code, 503)
            self.assertIsNone(self.cookie_of(r))
            r = self.client().post("/login", data={"token": TOKEN})
            self.assertEqual(r.status_code, 503)
        self.assertEqual(devices.list_devices(), [])
        self.assertTrue(auth.window_state()["open"])
        self.assertTrue(self.cookie_of(self.navigate_index()))
        self.assertTrue(self.cookie_of(self.poll_status(claim, OTHER_IP)))
        self.assertEqual(len(devices.list_devices()), 2)


class ReplayTests(DeviceTestBase):
    def test_lost_response_repoll_within_60s_returns_same_cookie(self):
        claim = self.request_and_approve()
        first = self.cookie_of(self.poll_status(claim))
        self.clock.advance(30)
        r = self.poll_status(claim)
        self.assertEqual(r.get_json()["status"], "approved")
        self.assertEqual(self.cookie_of(r), first)
        self.assertEqual(len(devices.list_devices()), 1)
        # Must-not-fire: past the replay window, nothing.
        self.clock.advance(31)
        r = self.poll_status(claim)
        self.assertEqual(r.get_json()["status"], "expired")
        self.assertIsNone(self.cookie_of(r))

    def test_repoll_at_cap_32_returns_same_cookie_without_allocation(self):
        self.fill_active(devices.MAX_ACTIVE - 1)
        claim = self.request_and_approve()
        first = self.cookie_of(self.poll_status(claim))
        self.assertTrue(first)
        self.assertEqual(len(self.active_ids()), devices.MAX_ACTIVE)
        r = self.poll_status(claim)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.cookie_of(r), first)
        self.assertEqual(len(self.active_ids()), devices.MAX_ACTIVE)

    def test_repoll_after_original_request_deadline_still_within_60s_of_redemption(self):
        claim = self.request_and_approve()
        self.clock.advance(295)  # the request expires at 300
        first = self.cookie_of(self.poll_status(claim))
        self.assertTrue(first)
        self.clock.advance(30)  # 325: past the request deadline, 30 s after redemption
        self.assertEqual(self.cookie_of(self.poll_status(claim)), first)

    def test_repoll_refused_after_revoke_or_reset(self):
        claim = self.request_and_approve()
        first = self.cookie_of(self.poll_status(claim))
        devices.revoke(first.split(".")[1], confirm_self=True)
        r = self.poll_status(claim)
        self.assertEqual(r.get_json()["status"], "expired")
        self.assertIsNone(self.cookie_of(r))
        self.assertEqual(len(devices.list_devices()), 1)

        claim = self.request_and_approve(OTHER_IP)
        self.assertTrue(self.cookie_of(self.poll_status(claim, OTHER_IP)))
        devices.reset()
        r = self.poll_status(claim, OTHER_IP)
        self.assertEqual(r.get_json()["status"], "expired")
        self.assertEqual(devices.list_devices(), [])

    def test_redeemed_claims_pruned_by_redemption_deadline_not_device(self):
        claim = self.request_and_approve()
        cookie = self.cookie_of(self.poll_status(claim))
        self.assertEqual(len(auth._requests), 1)
        self.clock.advance(61)
        auth.pending_requests()  # any call prunes
        self.assertEqual(auth._requests, {})
        self.assertEqual(self.get_with_cookie(cookie).status_code, 200)


class LockOrderTests(unittest.TestCase):
    def test_no_reverse_lock_nesting(self):
        """No section holding a shared.auth lock reaches for the devices lock.

        devices.admit takes its own lock and then one auth lock; the reverse
        nesting anywhere would deadlock against it. Standalone auth locking in
        open/close/status/decide stays allowed.
        """
        tree = ast.parse((ROOT / "shared" / "auth.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            names = {
                getattr(item.context_expr, "id", None) for item in node.items
            }
            if not names & {"_window_lock", "_requests_lock"}:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id in ("devices", "_devices"):
                    offenders.append(inner.lineno)
                if isinstance(inner, ast.Attribute) and inner.attr in ("admit", "_lock") and \
                        getattr(inner.value, "id", "") == "devices":
                    offenders.append(inner.lineno)
        self.assertEqual(offenders, [])
        # And shared.auth never imports the registry at module level.
        top_imports = [
            n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        for imp in top_imports:
            names = [a.name for a in imp.names]
            self.assertNotIn("devices", names)
            self.assertNotEqual(getattr(imp, "module", None), "shared.devices")


if __name__ == "__main__":
    unittest.main()


class DevicesCliTests(DeviceTestBase):
    """`assist devices`, routed through the real Flask app with the header."""

    def _route_http(self):
        from cli import http as cli_http

        client = self.client()

        def _call(method, path, data=None):
            if method == "GET":
                r = client.get(path, headers=self.header())
            else:
                r = client.post(path, json=data or {}, headers=self.header())
            if r.status_code >= 300:
                raise cli_http.ApiError(r.status_code, r.get_data(as_text=True), method=method, path=path)
            return r.get_json()

        return [
            mock.patch.object(cli_http, "get", side_effect=lambda p: _call("GET", p)),
            mock.patch.object(cli_http, "post", side_effect=lambda p, d=None: _call("POST", p, d)),
        ]

    def _run(self, argv):
        import contextlib
        import io

        from cli import devices as cli_devices

        out, err = io.StringIO(), io.StringIO()
        patches = self._route_http()
        for p in patches:
            p.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli_devices.command(argv)
        finally:
            for p in patches:
                p.stop()
        return code, out.getvalue(), err.getvalue()

    def test_list_rename_revoke_reset(self):
        a = self.login()
        a_id = a.split(".")[1]
        code, out, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertIn(a_id, out)
        code, out, _ = self._run(["list"])
        self.assertIn("login", out)

        code, _, _ = self._run(["rename", a_id, "Pixel 9"])
        self.assertEqual(code, 0)
        self.assertEqual(devices.get(a_id)["name"], "Pixel 9")

        code, out, _ = self._run(["revoke", a_id])
        self.assertEqual(code, 0)
        self.assertEqual(self.get_with_cookie(a).status_code, 401)
        code, out, _ = self._run([])
        self.assertIn("revoked", out)

        b = self.login()
        code, _, _ = self._run(["reset"])
        self.assertEqual(code, 0)
        self.assertEqual(self.get_with_cookie(b).status_code, 401)

    def test_unknown_id_fails_cleanly(self):
        code, _, err = self._run(["revoke", "d_nope"])
        self.assertNotEqual(code, 0)
        self.assertIn("not_found", err)

    def test_quarantined_registry_is_reported(self):
        self.path.write_bytes(b"garbage")
        devices.reload()
        code, out, _ = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("registry quarantined; run assist devices reset", out)

    def test_help_lists_devices_verb(self):
        from importlib.machinery import SourceFileLoader
        from importlib.util import module_from_spec, spec_from_loader

        loader = SourceFileLoader("assist_cli_devices_test", str(ROOT / "bin" / "assist"))
        spec = spec_from_loader(loader.name, loader)
        module = module_from_spec(spec)
        import os

        with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
            loader.exec_module(module)
        self.assertIn("devices", module.COMMANDS)
        text = module.help_text()
        self.assertIn("devices [list]", text)
        self.assertIn("devices revoke ID", text)
        self.assertIn("devices reset", text)
