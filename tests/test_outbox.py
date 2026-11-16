"""Agent file send: one offer, one download, nothing left behind.

`assist send-file` uploads a file's bytes; every signed-in browser sees the
offer, and the first download takes it. The cases here are the ones where a
key file could leak or linger: two devices tapping at once, a hostile name,
a download cut off mid-stream, an offer outliving its 15 minutes.

Run: .venv/bin/python3 -m unittest tests.test_outbox
"""

import io
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()

from flask import Flask  # noqa: E402

import shared.auth as auth  # noqa: E402
from shared import auth_gate, devices, outbox, paths  # noqa: E402
from routes import outbox as outbox_routes  # noqa: E402

TOKEN = "outbox-test-token"


def scratch_outbox(case):
    """Point the outbox at a per-test directory: reset() empties it, and run
    alone this module must never empty a live server's outbox."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patcher = patch.object(paths, "state_dir", lambda: Path(tmp.name) / "state")
    patcher.start()
    case.addCleanup(patcher.stop)
    outbox.reset()
    case.addCleanup(outbox.reset)


class OutboxStoreTests(unittest.TestCase):
    def setUp(self):
        scratch_outbox(self)

    def _offer(self, data=b"key material", name="id_ed25519", now=None):
        return outbox.create(io.BytesIO(data), name, "claude · dev", limit=1024, now=now)

    def test_create_then_pending(self):
        offer = self._offer()
        self.assertEqual(offer["state"], "pending")
        self.assertEqual(offer["size"], 12)
        self.assertEqual(offer["name"], "id_ed25519")
        self.assertEqual(offer["sender"], "claude · dev")
        self.assertEqual([o["id"] for o in outbox.pending()], [offer["id"]])
        self.assertEqual((outbox.outbox_dir() / offer["id"]).read_bytes(), b"key material")

    def test_claim_once(self):
        offer = self._offer()
        results = []
        barrier = threading.Barrier(8)

        def tap():
            barrier.wait()
            results.append(outbox.claim(offer["id"], "Pixel"))

        threads = [threading.Thread(target=tap) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r is not None), 1)
        for handle in results:
            if handle is not None:
                handle.close()
        record = outbox.get(offer["id"])
        self.assertEqual(record["state"], "picked_up")
        self.assertEqual(record["picked_up_by"], "Pixel")
        self.assertEqual(outbox.pending(), [])

    def test_discard_after_claim_deletes_file(self):
        offer = self._offer()
        path = outbox.outbox_dir() / offer["id"]
        with outbox.claim(offer["id"], "Pixel"):
            self.assertTrue(path.exists())
        outbox.discard_file(offer["id"])
        self.assertFalse(path.exists())

    def test_sweep_right_after_claim_cannot_take_the_winners_file(self):
        """Astra 781 #1: the sweep unlinked a claimed file before the route opened it."""
        offer = self._offer()
        handle = outbox.claim(offer["id"], "Pixel")
        outbox.sweep()
        self.assertFalse((outbox.outbox_dir() / offer["id"]).exists())
        with handle:
            self.assertEqual(handle.read(), b"key material")
        self.assertIsNone(outbox.claim(offer["id"], "Pixel"))

    def test_claim_of_a_vanished_file_does_not_mark_pickup(self):
        offer = self._offer()
        (outbox.outbox_dir() / offer["id"]).unlink()
        self.assertIsNone(outbox.claim(offer["id"], "Pixel"))
        self.assertIsNone(outbox.get(offer["id"])["picked_up_by"])

    def test_dismiss_deletes_file(self):
        offer = self._offer()
        self.assertTrue(outbox.dismiss(offer["id"]))
        self.assertFalse((outbox.outbox_dir() / offer["id"]).exists())
        self.assertEqual(outbox.get(offer["id"])["state"], "dismissed")
        self.assertIsNone(outbox.claim(offer["id"], "Pixel"))
        self.assertFalse(outbox.dismiss(offer["id"]))

    def test_sweep_expires_at_expiry(self):
        offer = self._offer(now=1000.0)
        self.assertEqual(outbox.sweep(now=1000.0 + outbox.EXPIRY_SECONDS - 1), 0)
        self.assertEqual(outbox.get(offer["id"], now=1000.0 + outbox.EXPIRY_SECONDS - 1)["state"], "pending")
        self.assertEqual(outbox.sweep(now=1000.0 + outbox.EXPIRY_SECONDS + 1), 1)
        self.assertEqual(outbox.get(offer["id"])["state"], "expired")
        self.assertFalse((outbox.outbox_dir() / offer["id"]).exists())

    def test_get_expires_overdue_offer_without_sweep(self):
        offer = self._offer(now=1000.0)
        record = outbox.get(offer["id"], now=1000.0 + outbox.EXPIRY_SECONDS + 1)
        self.assertEqual(record["state"], "expired")
        self.assertFalse((outbox.outbox_dir() / offer["id"]).exists())

    def test_claim_refuses_overdue_offer(self):
        offer = self._offer(now=1000.0)
        self.assertIsNone(outbox.claim(offer["id"], "Pixel", now=1000.0 + outbox.EXPIRY_SECONDS + 1))

    def test_default_expiry_is_fifteen_minutes(self):
        if "ASSIST_OUTBOX_EXPIRY_SECONDS" not in os.environ:
            self.assertEqual(outbox.EXPIRY_SECONDS, 900)

    def test_too_large_removes_partial(self):
        with self.assertRaises(outbox.OutboxTooLarge):
            outbox.create(io.BytesIO(b"x" * 2048), "big.bin", "shell", limit=1024)
        self.assertEqual(list(outbox.outbox_dir().iterdir()), [])
        self.assertEqual(outbox.pending(), [])

    def test_eleventh_pending_refused(self):
        for i in range(outbox.MAX_PENDING):
            self._offer(name=f"f{i}.txt")
        with self.assertRaises(outbox.OutboxFull):
            self._offer(name="one-too-many.txt")
        self.assertEqual(len(outbox.pending()), 10)

    def test_ended_offers_do_not_count_toward_the_cap(self):
        for i in range(outbox.MAX_PENDING):
            outbox.dismiss(self._offer(name=f"f{i}.txt")["id"])
        self._offer(name="fresh.txt")

    def test_zero_byte_offer(self):
        offer = self._offer(data=b"", name="empty.txt")
        self.assertEqual(offer["size"], 0)
        with outbox.claim(offer["id"], "Pixel") as handle:
            self.assertEqual(handle.read(), b"")

    def test_hostile_name_stays_in_dir(self):
        offer = self._offer(name='../../x\n"y')
        self.assertEqual(sorted(p.name for p in outbox.outbox_dir().iterdir()), [offer["id"]])
        self.assertFalse((outbox.outbox_dir().parent.parent / "x").exists())

    def test_reset_empties_dir(self):
        self._offer()
        (outbox.outbox_dir() / "leftover.part").write_bytes(b"x")
        outbox.reset()
        self.assertEqual(list(outbox.outbox_dir().iterdir()), [])
        self.assertEqual(outbox.pending(), [])

    def test_file_modes(self):
        offer = self._offer()
        self.assertEqual(stat.S_IMODE(outbox.outbox_dir().stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((outbox.outbox_dir() / offer["id"]).stat().st_mode), 0o600)

    def test_mime_type(self):
        self.assertEqual(self._offer(name="report.pdf")["mime"], "application/pdf")
        self.assertEqual(self._offer(name="id_ed25519")["mime"], "application/octet-stream")

    def test_secret_hint_fires(self):
        for name in ("id_ed25519", "id_rsa", "server.pem", "tls.key", "cert.p12", "putty.ppk"):
            self.assertTrue(outbox.secret_hint(name, b""), name)
        self.assertTrue(outbox.secret_hint("blob.txt", b"-----BEGIN OPENSSH " b"PRIVATE KEY-----\n"))

    def test_secret_hint_does_not_fire(self):
        for name in ("report.pdf", "notes.txt", "keyboard.png", "monkey.txt"):
            self.assertFalse(outbox.secret_hint(name, b"hello"), name)
        self.assertFalse(outbox.secret_hint("x.txt", b"a" * 5000 + b"PRIVATE KEY-----"))

    def test_record_carries_secret_hint(self):
        self.assertTrue(self._offer(name="id_ed25519")["secret_hint"])
        self.assertFalse(self._offer(name="report.pdf", data=b"%PDF")["secret_hint"])


class OutboxRouteTests(unittest.TestCase):
    def setUp(self):
        scratch_outbox(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = auth._token_cache
        auth._token_cache = TOKEN
        self.addCleanup(setattr, auth, "_token_cache", saved)
        patcher = patch.object(devices, "DEVICES_FILE", Path(tmp.name) / "devices.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        devices.reload()
        self.addCleanup(devices.reload)
        device = devices.admit(devices.TokenLogin(), ip="127.0.0.1", ua="seed")
        devices.rename(device["id"], "Pixel")
        self.cookie = auth.device_cookie(device["id"])

        app = Flask(__name__)
        app.logger.disabled = True
        auth_gate.register_auth_gate(app)
        app.register_blueprint(outbox_routes.outbox_bp)
        self.app = app

    def anon(self):
        return self.app.test_client()

    def script(self):
        client = self.app.test_client()
        client.environ_base["HTTP_X_ASSIST_TOKEN"] = TOKEN
        return client

    def phone(self):
        client = self.app.test_client()
        client.set_cookie(auth.COOKIE_NAME, self.cookie)
        return client

    def offer(self, data=b"key material", name="id_ed25519", client=None):
        response = (client or self.script()).post(
            "/api/outbox",
            data={"file": (io.BytesIO(data), "upload.bin"), "name": name, "pane": ""},
            content_type="multipart/form-data",
        )
        return response

    def test_unauthenticated_is_401(self):
        offer_id = self.offer().get_json()["offer"]["id"]
        anon = self.anon()
        self.assertEqual(self.offer(client=anon).status_code, 401)
        self.assertEqual(anon.get(f"/api/outbox/{offer_id}").status_code, 401)
        self.assertEqual(anon.get(f"/api/outbox/{offer_id}/download").status_code, 401)
        self.assertEqual(anon.post(f"/api/outbox/{offer_id}/dismiss").status_code, 401)
        self.assertEqual(outbox.get(offer_id)["state"], "pending")

    def test_cookie_cannot_create(self):
        response = self.offer(client=self.phone())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"], "header_token_required")
        self.assertEqual(outbox.pending(), [])

    def test_create_download_once(self):
        response = self.offer()
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["browsers"], 1)
        offer = body["offer"]
        self.assertEqual(offer["name"], "id_ed25519")
        self.assertEqual(offer["sender"], "shell")
        self.assertEqual([o["id"] for o in outbox_routes.poll_block()], [offer["id"]])
        self.assertEqual(set(outbox_routes.poll_block()[0]),
                         {"id", "name", "size", "sender", "expires_at", "secret_hint"})

        phone = self.phone()
        got = phone.get(f"/api/outbox/{offer['id']}/download")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.data, b"key material")
        self.assertEqual(got.headers["Cache-Control"], "no-store")
        self.assertEqual(got.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(got.headers["Content-Type"], "application/octet-stream")
        self.assertEqual(got.headers["Content-Disposition"],
                         "attachment; filename=\"id_ed25519\"; filename*=UTF-8''id_ed25519")
        got.close()
        self.assertFalse((outbox.outbox_dir() / offer["id"]).exists())

        again = self.phone().get(f"/api/outbox/{offer['id']}/download")
        self.assertEqual(again.status_code, 410)
        self.assertEqual(again.get_json()["error"], "picked_up")
        self.assertEqual(outbox_routes.poll_block(), [])
        status = self.script().get(f"/api/outbox/{offer['id']}").get_json()["offer"]
        self.assertEqual(status["state"], "picked_up")
        self.assertEqual(status["picked_up_by"], "Pixel")

    def test_head_does_not_consume_the_offer(self):
        """Astra 781 #2: Flask answers HEAD with the GET view, which claimed the file."""
        offer_id = self.offer().get_json()["offer"]["id"]
        head = self.phone().head(f"/api/outbox/{offer_id}/download")
        self.assertEqual(head.status_code, 405)
        self.assertEqual(outbox.get(offer_id)["state"], "pending")
        got = self.phone().get(f"/api/outbox/{offer_id}/download")
        self.assertEqual(got.data, b"key material")
        got.close()

    def test_script_pickup_is_named_script(self):
        offer_id = self.offer().get_json()["offer"]["id"]
        self.script().get(f"/api/outbox/{offer_id}/download").close()
        self.assertEqual(outbox.get(offer_id)["picked_up_by"], "script")

    def test_pdf_type(self):
        offer_id = self.offer(data=b"%PDF-1.7", name="report.pdf").get_json()["offer"]["id"]
        got = self.phone().get(f"/api/outbox/{offer_id}/download")
        self.assertEqual(got.headers["Content-Type"], "application/pdf")
        got.close()

    def test_dismiss_then_download_is_410(self):
        offer_id = self.offer().get_json()["offer"]["id"]
        self.assertEqual(self.phone().post(f"/api/outbox/{offer_id}/dismiss").status_code, 200)
        got = self.phone().get(f"/api/outbox/{offer_id}/download")
        self.assertEqual(got.status_code, 410)
        self.assertEqual(got.get_json()["error"], "dismissed")
        self.assertEqual(self.phone().post(f"/api/outbox/{offer_id}/dismiss").status_code, 410)

    def test_unknown_offer_is_404(self):
        self.assertEqual(self.phone().get("/api/outbox/nope/download").status_code, 404)
        self.assertEqual(self.script().get("/api/outbox/nope").status_code, 404)

    def test_hostile_name_gives_one_well_formed_header(self):
        offer_id = self.offer(name='a"b\r\nX-Evil: 1\u00e9.txt').get_json()["offer"]["id"]
        got = self.phone().get(f"/api/outbox/{offer_id}/download")
        disposition = got.headers["Content-Disposition"]
        got.close()
        self.assertNotIn("X-Evil", got.headers)
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertTrue(disposition.startswith('attachment; filename="a_bX-Evil__1_.txt"; '), disposition)
        self.assertIn("filename*=UTF-8''a%22bX-Evil%3A%201", disposition)
        self.assertIn("%C3%A9.txt", disposition)

    def test_cut_off_download_still_ends_offer(self):
        offer_id = self.offer(data=b"x" * (3 * 1024 * 1024), name="big.bin").get_json()["offer"]["id"]
        got = self.phone().get(f"/api/outbox/{offer_id}/download", buffered=False)
        next(iter(got.response))
        got.close()
        self.assertEqual(outbox.get(offer_id)["state"], "picked_up")
        self.assertFalse((outbox.outbox_dir() / offer_id).exists())

    def test_download_never_started_still_deletes_file(self):
        offer_id = self.offer().get_json()["offer"]["id"]
        got = self.phone().get(f"/api/outbox/{offer_id}/download", buffered=False)
        got.close()
        self.assertFalse((outbox.outbox_dir() / offer_id).exists())

    def test_too_large_is_413(self):
        with patch.object(outbox_routes.state, "MAX_UPLOAD_SIZE", 1024):
            response = self.offer(data=b"x" * 4096)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(outbox.pending(), [])

    def test_full_is_429(self):
        for i in range(outbox.MAX_PENDING):
            self.assertEqual(self.offer(name=f"f{i}").status_code, 201)
        response = self.offer(name="eleventh")
        self.assertEqual(response.status_code, 429)
        self.assertIn("already waiting", response.get_json()["error"])

    def test_restart_empties_pending(self):
        offer_id = self.offer().get_json()["offer"]["id"]
        outbox.reset()
        self.assertEqual(outbox_routes.poll_block(), [])
        self.assertEqual(self.phone().get(f"/api/outbox/{offer_id}/download").status_code, 404)

    def test_sender_for_bad_pane_is_shell(self):
        self.assertEqual(outbox_routes.describe_sender(""), "shell")
        self.assertEqual(outbox_routes.describe_sender("dev:0.0"), "shell")
        self.assertEqual(outbox_routes.describe_sender("%999999"), "shell")


class SendFileCliTests(OutboxRouteTests.__base__):
    """`assist send-file` over real HTTP: the routes on a loopback werkzeug
    server, the CLI module pointed at it."""

    def setUp(self):
        OutboxRouteTests.setUp(self)
        from types import SimpleNamespace
        from werkzeug.serving import make_server

        from cli import http as cli_http, send_file

        self.send_file = send_file
        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.api = f"http://127.0.0.1:{server.server_port}"
        resolved = SimpleNamespace(token=TOKEN, api=self.api, home=Path(tempfile.gettempdir()))
        for target, value in ((cli_http, "resolve"), (send_file, "POLL_INTERVAL")):
            patcher = patch.object(target, value, (lambda: resolved) if value == "resolve" else 0.05)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"ASSIST_NO_HINTS": "1", "TMUX_PANE": ""})
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.file = self.dir / "report.pdf"
        self.file.write_bytes(b"%PDF-1.7 hello")

    # OutboxRouteTests' helpers, without inheriting its tests.
    script = OutboxRouteTests.script
    phone = OutboxRouteTests.phone

    def run_cli(self, path=None, name=None, no_wait=False, timeout=None, status_id=None, cancel_id=None):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.send_file.command(path, name, no_wait, timeout, status_id, cancel_id)
        return code, out.getvalue(), err.getvalue()

    def run_cli_async(self, **kwargs):
        result = {}
        thread = threading.Thread(target=lambda: result.update(zip(("code", "out", "err"),
                                                                   self.run_cli(**kwargs))))
        thread.start()
        for _ in range(200):
            if outbox.pending():
                break
            threading.Event().wait(0.02)
        return thread, result

    def test_directory_is_refused(self):
        code, _, err = self.run_cli(path=str(self.dir))
        self.assertEqual(code, 2)
        self.assertIn("not a regular file", err)
        self.assertEqual(outbox.pending(), [])

    def test_missing_file_is_refused(self):
        code, _, err = self.run_cli(path=str(self.dir / "nope.pdf"))
        self.assertEqual(code, 2)
        self.assertIn("no such file", err)

    def test_over_cap_refused_before_upload(self):
        with patch.object(outbox_routes.state, "MAX_UPLOAD_SIZE", 4):
            code, _, err = self.run_cli(path=str(self.file))
        self.assertEqual(code, 2)
        self.assertIn("the limit is", err)
        self.assertEqual(outbox.pending(), [])

    def test_no_wait_offers_and_returns(self):
        code, out, _ = self.run_cli(path=str(self.file), no_wait=True)
        self.assertEqual(code, 0)
        self.assertRegex(out, r"^offered report.pdf \(14 B\) to 1 signed-in browser · expires \d\d:\d\d · id \S+")
        [offer] = outbox.pending()
        self.assertEqual(offer["sender"], "shell")
        got = self.phone().get(f"/api/outbox/{offer['id']}/download")
        self.assertEqual(got.data, b"%PDF-1.7 hello")
        got.close()

    def test_file_growing_during_send_uploads_one_consistent_snapshot(self):
        """Astra 781 #3: Content-Length came from an earlier stat than the bytes sent."""
        real_get = self.send_file.http.get

        def grow_then_get(path):
            with self.file.open("ab") as out:
                out.write(b" appended later")
            return real_get(path)

        with patch.object(self.send_file.http, "get", grow_then_get):
            code, out, _ = self.run_cli(path=str(self.file), no_wait=True)
        self.assertEqual(code, 0, out)
        [offer] = outbox.pending()
        got = self.phone().get(f"/api/outbox/{offer['id']}/download")
        self.assertIn(got.data, (b"%PDF-1.7 hello", b"%PDF-1.7 hello appended later"))
        self.assertEqual(len(got.data), offer["size"])
        got.close()

    def test_file_shrinking_during_send_uploads_one_consistent_snapshot(self):
        real_get = self.send_file.http.get

        def shrink_then_get(path):
            self.file.write_bytes(b"%PDF")
            return real_get(path)

        with patch.object(self.send_file.http, "get", shrink_then_get):
            code, out, _ = self.run_cli(path=str(self.file), no_wait=True)
        self.assertEqual(code, 0, out)
        [offer] = outbox.pending()
        got = self.phone().get(f"/api/outbox/{offer['id']}/download")
        self.assertEqual(len(got.data), offer["size"])
        got.close()

    def test_name_ending_in_backslash(self):
        """Astra 781 #5: a trailing backslash escaped the multipart filename's quote."""
        code, _, err = self.run_cli(path=str(self.file), name="name\\", no_wait=True)
        self.assertEqual(code, 0, err)
        self.assertEqual(outbox.pending()[0]["name"], "name\\")

    def test_name_override(self):
        self.run_cli(path=str(self.file), name="Q3 report.pdf", no_wait=True)
        self.assertEqual(outbox.pending()[0]["name"], "Q3 report.pdf")

    def test_wait_until_picked_up(self):
        thread, result = self.run_cli_async(path=str(self.file))
        [offer] = outbox.pending()
        self.script().get(f"/api/outbox/{offer['id']}/download").close()
        thread.join(10)
        self.assertEqual(result["code"], 0)
        self.assertIn("picked up by script at", result["out"])

    def test_wait_until_dismissed(self):
        thread, result = self.run_cli_async(path=str(self.file))
        [offer] = outbox.pending()
        self.phone().post(f"/api/outbox/{offer['id']}/dismiss")
        thread.join(10)
        self.assertEqual(result["code"], 21)
        self.assertIn("dismissed", result["out"])

    def test_cancel(self):
        self.run_cli(path=str(self.file), no_wait=True)
        [offer] = outbox.pending()
        code, out, _ = self.run_cli(cancel_id=offer["id"])
        self.assertEqual(code, 21)
        self.assertEqual(self.phone().get(f"/api/outbox/{offer['id']}/download").status_code, 410)

    def test_timeout_is_75_and_offer_stays_open(self):
        code, out, _ = self.run_cli(path=str(self.file), timeout=0.2)
        self.assertEqual(code, 75)
        self.assertIn("still waiting", out)
        self.assertEqual(len(outbox.pending()), 1)

    def test_expiry_while_waiting_without_timeout(self):
        with patch.object(outbox, "EXPIRY_SECONDS", 0.5):
            code, out, _ = self.run_cli(path=str(self.file))
        self.assertEqual(code, 20)
        self.assertIn("expired unclaimed", out)

    def test_status_codes(self):
        self.run_cli(path=str(self.file), no_wait=True)
        offer_id = outbox.pending()[0]["id"]
        self.assertEqual(self.run_cli(status_id=offer_id)[0], 75)
        self.script().get(f"/api/outbox/{offer_id}/download").close()
        self.assertEqual(self.run_cli(status_id=offer_id)[0], 0)
        self.assertEqual(self.run_cli(status_id="nope")[0], 20)

    def test_full_outbox_refused_before_upload(self):
        for i in range(outbox.MAX_PENDING):
            self.run_cli(path=str(self.file), name=f"f{i}", no_wait=True)
        code, _, err = self.run_cli(path=str(self.file), no_wait=True)
        self.assertEqual(code, 2)
        self.assertIn("already waiting", err)


ROOT = Path(__file__).resolve().parent.parent

BROWSER_PRELUDE = r"""
const _create = document.createElement;
globalThis.clicks = [];
document.createElement = tag => {
    const el = _create(tag);
    el.dataset = {};
    el.click = () => clicks.push(el.href);
    return el;
};
document.getElementById = id => document.body.all().find(e => e.id === id) || null;
const badge = document.createElement('button');
badge.id = 'outbox-badge';
badge.hidden = true;
document.body.appendChild(badge);
globalThis.statusReply = {ok: true, offer: {state: 'pending'}};
globalThis.fetch = async (url, options) => {
    fetchCalls.push({url, method: (options && options.method) || 'GET'});
    return {ok: statusReply.ok !== false, json: async () => statusReply};
};
vm.runInThisContext(fs.readFileSync('js/outbox.js', 'utf8'), {filename: 'js/outbox.js'});
globalThis.offer = o => Object.assign({id: 'abc', name: 'id_ed25519', size: 411,
    sender: 'claude · del_vps', expires_at: Date.now() / 1000 + 600, secret_hint: true}, o || {});
globalThis.poll = offers => { _applyFileOffers(offers); renderOutboxTray(offers); };
globalThis.filePopups = () => popups().filter(p => p.classList.contains('file'));
globalThis.sheet = () => document.getElementById('outbox-sheet');
"""


def run_browser_js(body):
    from test_prompt_popup import run_popup_js
    return run_popup_js(BROWSER_PRELUDE + body)


class OutboxBrowserTests(unittest.TestCase):
    def test_file_popup_once_and_never_auto_hides(self):
        result = run_browser_js(r"""
poll([offer()]);
poll([offer()]);
const shown = filePopups().length;
const delays = liveDelays();
fireTimers(5000);
const p = filePopups()[0];
console.log(JSON.stringify({shown, delays, still: filePopups().length,
    title: p.find('prompt-popup-where').textContent,
    detail: p.find('prompt-popup-detail').textContent,
    note: p.find('prompt-popup-note') && p.find('prompt-popup-note').textContent,
    labels: p.labels()}));
""")
        self.assertEqual(result["shown"], 1)
        self.assertEqual(result["delays"], [])
        self.assertEqual(result["still"], 1)
        self.assertEqual(result["title"], "File from claude · del_vps")
        self.assertEqual(result["detail"], "id_ed25519 · 411 B")
        self.assertEqual(result["note"], "looks like a private key")
        self.assertEqual(result["labels"], ["Download"])

    def test_no_key_note_without_secret_hint(self):
        result = run_browser_js(r"""
poll([offer({name: 'report.pdf', size: 2400000, secret_hint: false})]);
const p = filePopups()[0];
console.log(JSON.stringify({note: !!p.find('prompt-popup-note'),
    detail: p.find('prompt-popup-detail').textContent}));
""")
        self.assertFalse(result["note"])
        self.assertEqual(result["detail"], "report.pdf · 2.3 MB")

    def test_offer_gone_from_poll_removes_popup_row_and_badge(self):
        result = run_browser_js(r"""
poll([offer(), offer({id: 'def', name: 'report.pdf', secret_hint: false})]);
const badgeText = badge.textContent, badgeHidden = badge.hidden;
openOutboxSheet();
const rows = sheet().all().filter(e => e.classList.contains('outbox-row')).length;
poll([offer({id: 'def', name: 'report.pdf', secret_hint: false})]);
fireTimers(200);
const rowsAfter = sheet().all().filter(e => e.classList.contains('outbox-row')).length;
const popupsAfter = filePopups().length;
poll([]);
fireTimers(200);
console.log(JSON.stringify({badgeText, badgeHidden, rows, rowsAfter, popupsAfter,
    sheetGone: sheet() === null, hiddenAtZero: badge.hidden}));
""")
        self.assertEqual(result["badgeText"], "⬇ 2")
        self.assertFalse(result["badgeHidden"])
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["rowsAfter"], 1)
        self.assertEqual(result["popupsAfter"], 1)
        self.assertTrue(result["sheetGone"])
        self.assertTrue(result["hiddenAtZero"])

    def test_download_navigates_to_the_one_time_link(self):
        result = run_browser_js(r"""
poll([offer({id: 'a/b c'})]);
click(filePopups()[0], 'Download');
await tick(); await tick();
console.log(JSON.stringify({clicks, fetchCalls, badgeHidden: badge.hidden}));
""")
        self.assertEqual(result["fetchCalls"], [{"url": "/api/outbox/a%2Fb%20c", "method": "GET"}])
        self.assertEqual(result["clicks"], ["/api/outbox/a%2Fb%20c/download"])
        self.assertTrue(result["badgeHidden"])

    def test_download_after_another_device_took_it_says_so(self):
        result = run_browser_js(r"""
statusReply = {ok: true, offer: {state: 'picked_up'}};
poll([offer()]);
await outboxDownload('abc');
console.log(JSON.stringify({clicks, flashes}));
""")
        self.assertEqual(result["clicks"], [])
        self.assertEqual(result["flashes"], [["error", "Already picked up on another device"]])

    def test_failed_dismiss_keeps_the_offer(self):
        """Astra 781 #4: a 500 or 403 on dismiss hid a file still waiting on the server."""
        result = run_browser_js(r"""
poll([offer()]);
openOutboxSheet();
for (const status of [500, 403]) {
    statusReply = {ok: false, error: 'boom'};
    globalThis.fetch = async (url, options) => {
        fetchCalls.push({url, method: (options && options.method) || 'GET'});
        return {ok: false, status, json: async () => statusReply};
    };
    await outboxDismiss('abc');
}
console.log(JSON.stringify({flashes, popups: filePopups().length, sheet: sheet() !== null,
    badgeHidden: badge.hidden}));
""")
        self.assertEqual(result["flashes"], [["error", "Could not dismiss the file; try again"]] * 2)
        self.assertEqual(result["popups"], 1)
        self.assertTrue(result["sheet"])
        self.assertFalse(result["badgeHidden"])

    def test_failed_preflight_keeps_the_offer(self):
        result = run_browser_js(r"""
poll([offer()]);
globalThis.fetch = async () => ({ok: false, status: 500, json: async () => ({ok: false})});
await outboxDownload('abc');
console.log(JSON.stringify({clicks, flashes, badgeHidden: badge.hidden, popups: filePopups().length}));
""")
        self.assertEqual(result["clicks"], [])
        self.assertEqual(result["flashes"], [["error", "Could not check the file; try again"]])
        self.assertFalse(result["badgeHidden"])
        self.assertEqual(result["popups"], 1)

    def test_dismiss_of_an_ended_offer_forgets_it(self):
        result = run_browser_js(r"""
poll([offer()]);
globalThis.fetch = async () => ({ok: false, status: 410, json: async () => ({ok: false, error: 'expired'})});
await outboxDismiss('abc');
console.log(JSON.stringify({flashes, badgeHidden: badge.hidden}));
""")
        self.assertEqual(result["flashes"], [["error", "Expired"]])
        self.assertTrue(result["badgeHidden"])

    def test_dismiss_posts_and_forgets(self):
        result = run_browser_js(r"""
poll([offer()]);
openOutboxSheet();
await outboxDismiss('abc');
fireTimers(200);
console.log(JSON.stringify({fetchCalls, popups: filePopups().length, sheetGone: sheet() === null}));
""")
        self.assertEqual(result["fetchCalls"], [{"url": "/api/outbox/abc/dismiss", "method": "POST"}])
        self.assertEqual(result["popups"], 0)
        self.assertTrue(result["sheetGone"])

    def test_sheet_actions_are_registered(self):
        registry = (ROOT / "js" / "actions-registry.js").read_text(encoding="utf-8")
        source = (ROOT / "js" / "outbox.js").read_text(encoding="utf-8")
        import re
        names = set(re.findall(r"_outboxButton\([^,]+, '(\w+)'", source))
        self.assertEqual(names, {"closeOutboxSheet", "outboxDismiss", "outboxDownload"})
        for name in names | {"openOutboxSheet"}:
            self.assertRegex(registry, rf"\n\s+{name}: ", name)


if __name__ == "__main__":
    unittest.main()
