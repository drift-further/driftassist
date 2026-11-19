"""Tests for Phase 1 delegation CLI fixes:
1. `assist launch --role` never arms Auto-Yes and never recommends arming it.
2. Bare `assist send S --enter` sends Enter (while text + --file stays refused).
3. `assist send --wait` exits non-zero when pane content never changes after send.
"""

import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()

from flask import Flask  # noqa: E402

from cli import session, wait as wait_cli  # noqa: E402
from routes.input import input_bp  # noqa: E402
from tests._tmux_box import TmuxBox, wait_for_bytes  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_ROSTER = ROOT / "roster.example.toml"


def _load_bin_assist():
    loader = importlib.machinery.SourceFileLoader("bin_assist_mod", str(ROOT / "bin" / "assist"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
        loader.exec_module(mod)
    return mod


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class LaunchRoleNoAutoyesTests(unittest.TestCase):
    def test_launch_role_never_arms_autoyes_or_suggests_it(self):
        """`assist launch --role` must never call /autoyes/* or hint `--on`."""
        http_calls: list[tuple[str, str, object]] = []
        captures = iter(["$ ", "$ codex --yolo\nready\n"])

        def fake_post(path, payload=None):
            http_calls.append(("POST", path, payload))
            if path == "/terminal/launch":
                return {
                    "session": "del_worker",
                    "target": "del_worker:0.0",
                    "venv": None,
                    "venv_activated": False,
                    "existed": False,
                }
            if path == "/type":
                return {"ok": True, "via": "tmux", "sent_chars": len((payload or {}).get("text", ""))}
            raise AssertionError(f"unexpected POST {path}")

        def fake_get(path):
            http_calls.append(("GET", path, None))
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": next(captures, "$ codex --yolo\nready\n")}
            if path == "/poll":
                return {
                    "states": {"del_worker:0.0": {"state": "idle", "idle_seconds": 35.0}},
                    "sessions": [{"target": "del_worker:0.0", "agent_kind": "codex"}],
                }
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        clock = _FakeClock()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch.dict("os.environ", {"ASSIST_NO_HINTS": "0"}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.launch(
                "del_worker",
                role="judge",
                against="claude",
                roster_path=EXAMPLE_ROSTER,
            )

        self.assertEqual(code, 0)
        autoyes_calls = [call for call in http_calls if "/autoyes" in call[1]]
        self.assertEqual(autoyes_calls, [], f"launch --role touched Auto-Yes: {autoyes_calls}")
        self.assertIn("next:", stderr.getvalue())
        self.assertNotIn("autoyes", stderr.getvalue().lower())
        self.assertNotIn("--on", stderr.getvalue())
        self.assertNotIn("continue", stderr.getvalue().lower())
        self.assertIn("assist send del_worker --file BRIEF.md --enter --wait", stderr.getvalue())


class BareSendEnterTests(unittest.TestCase):
    def test_bare_send_enter_posts_empty_text_with_enter(self):
        posts = []

        def fake_post(path, payload=None):
            posts.append((path, payload))
            return {"ok": True, "via": "tmux", "sent_chars": 0}

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(session, "reply_address", return_value=None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.send("demo", enter=True)

        self.assertEqual(code, 0)
        self.assertIn("sent Enter to demo:0.0", stdout.getvalue())
        self.assertEqual(
            posts,
            [
                (
                    "/type",
                    {
                        "target": "demo:0.0",
                        "text": "",
                        "enter": True,
                        "no_history": True,
                        "raw": True,
                    },
                )
            ],
        )

    def test_cli_parser_accepts_bare_send_enter(self):
        bin_assist = _load_bin_assist()
        parsed = bin_assist.build_parser().parse_args(["send", "demo", "--enter"])
        self.assertEqual(parsed.verb, "send")
        self.assertEqual(parsed.session, "demo")
        self.assertIsNone(parsed.text)
        self.assertIsNone(parsed.file_path)
        self.assertTrue(parsed.enter)

    def test_send_still_refuses_text_and_file_together(self):
        for enter in (False, True):
            with self.subTest(enter=enter):
                stderr = io.StringIO()
                with mock.patch.object(session.http, "post") as post, \
                        redirect_stderr(stderr), \
                        self.assertRaises(SystemExit) as raised:
                    session.send("demo", text="hello", file_path="/tmp/x", enter=enter)
                self.assertEqual(raised.exception.code, 2)
                post.assert_not_called()

    def test_send_still_refuses_no_text_no_file_no_enter(self):
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post") as post, \
                redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as raised:
            session.send("demo")
        self.assertEqual(raised.exception.code, 2)
        post.assert_not_called()

    def test_bare_send_enter_delivers_cr_through_type_route_to_real_tmux(self):
        """End-to-end: `session.send(S, enter=True)` -> POST /type -> real tmux pane."""
        box = TmuxBox(self)
        recv_path, cmd = box.reader_command("enter")
        box.new_session("demo", cmd)
        wait_for_bytes(recv_path, b"ready")

        app = Flask(__name__)
        app.register_blueprint(input_bp)
        client = app.test_client()

        def fake_post(path, payload=None):
            resp = client.post(path, json=payload)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            return resp.get_json()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(session, "reply_address", return_value=None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.send("demo", pane="1.1", enter=True)

        self.assertEqual(code, 0)
        wait_for_bytes(recv_path, b"\r")


class SendWaitNoSilentZeroTests(unittest.TestCase):
    def _run_send_wait(self, poll_seq, capture_seq, *, text="hello", enter=True, timeout=30.0):
        clock = _FakeClock()
        polls = iter(poll_seq)
        captures = iter(capture_seq)
        last_poll = poll_seq[-1]
        last_cap = capture_seq[-1]

        def fake_get(path):
            if path == "/poll":
                return next(polls, last_poll)
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": next(captures, last_cap)}
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        def fake_post(path, payload=None):
            if path == "/type":
                return {"ok": True, "via": "tmux", "sent_chars": len((payload or {}).get("text", ""))}
            raise AssertionError(f"unexpected POST {path}")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(session, "reply_address", return_value=None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.send(
                "demo",
                text=text,
                enter=enter,
                wait_for_completion=True,
                timeout=timeout,
            )
        return code, stdout.getvalue(), stderr.getvalue(), clock.now - 1000.0

    def test_send_wait_exits_nonzero_when_already_idle_pane_never_changes(self):
        """Studio finding 1435: already-idle pane (idle_seconds >= 5) with unchanged content must NOT exit 0."""
        idle_poll = {
            "states": {"demo:0.0": {"state": "idle", "idle_seconds": 60.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }
        code, _out, err, elapsed = self._run_send_wait(
            [idle_poll],
            ["❯ \n"],
            timeout=60.0,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(code, 3)
        self.assertIn("input was sent", err.lower())
        self.assertIn("no visible change", err.lower())
        self.assertNotIn("may not have landed", err.lower())
        self.assertNotIn("next:", err)
        self.assertLessEqual(elapsed, wait_cli.NO_CHANGE_GRACE_SECONDS + 0.01)

    def test_send_wait_short_timeout_exits_3_not_75_when_unchanged(self):
        """Short timeout (< NO_CHANGE_GRACE_SECONDS) with no change exits 3 via top-of-loop deadline check."""
        idle_poll = {
            "states": {"demo:0.0": {"state": "idle", "idle_seconds": 60.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }
        code, _out, err, elapsed = self._run_send_wait(
            [idle_poll],
            ["❯ \n"],
            timeout=3.0,
        )
        self.assertEqual(code, 3)
        self.assertAlmostEqual(elapsed, 3.0, places=2)
        self.assertIn("no visible change", err.lower())
        self.assertNotIn("next:", err)

    def test_send_wait_with_autoyes_forwards_require_change_and_restores_autoyes(self):
        """`send --wait --autoyes` forwards require_change=True and restores Auto-Yes on exit 3."""
        clock = _FakeClock()
        autoyes_posts = []
        autoyes_enabled = False
        idle_poll = {
            "states": {"demo:0.0": {"state": "idle", "idle_seconds": 60.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }

        def fake_get(path):
            if path == "/autoyes/status":
                return {"sessions": {"demo": autoyes_enabled}}
            if path == "/poll":
                return idle_poll
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": "❯ \n"}
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        def fake_post(path, payload=None):
            nonlocal autoyes_enabled
            if path == "/type":
                return {"ok": True, "via": "tmux", "sent_chars": len((payload or {}).get("text", ""))}
            if path == "/autoyes/set":
                autoyes_posts.append(dict(payload or {}))
                autoyes_enabled = bool((payload or {}).get("enabled"))
                return {"ok": True}
            raise AssertionError(f"unexpected POST {path}")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(session, "reply_address", return_value=None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.send(
                "demo",
                text="hello",
                enter=True,
                wait_for_completion=True,
                timeout=60.0,
                autoyes=True,
            )

        self.assertEqual(code, 3)
        self.assertEqual(
            autoyes_posts,
            [
                {"session": "demo", "enabled": True},
                {"session": "demo", "enabled": False},
            ],
        )
        self.assertIn("no visible change", stderr.getvalue().lower())

    def test_send_wait_exits_nonzero_when_shell_pane_never_changes(self):
        shell_poll = {
            "states": {"demo:0.0": {"state": "shell", "idle_seconds": 15.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": None}],
        }
        code, _out, err, _elapsed = self._run_send_wait(
            [shell_poll],
            ["$ \n"],
            timeout=60.0,
        )
        self.assertEqual(code, 3)
        self.assertIn("no visible change", err.lower())

    def test_send_wait_exits_nonzero_when_recently_active_agent_never_changes(self):
        """A `node` pane reports `running` for 30s after last output; unchanged send still fails after grace."""
        running_poll = {
            "states": {"demo:0.0": {"state": "running", "idle_seconds": 5.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }
        code, _out, err, elapsed = self._run_send_wait(
            [running_poll],
            ["❯ \n"],
            timeout=110.0,
        )
        self.assertEqual(code, 3)
        self.assertIn("no visible change", err.lower())
        self.assertLessEqual(elapsed, wait_cli.NO_CHANGE_GRACE_SECONDS + 0.01)

    def test_bare_send_enter_wait_exits_nonzero_when_enter_does_not_land(self):
        idle_poll = {
            "states": {"demo:0.0": {"state": "idle", "idle_seconds": 40.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }
        code, _out, err, _elapsed = self._run_send_wait(
            [idle_poll],
            ["[Pasted text #1 +20 lines]\n"],
            text=None,
            enter=True,
            timeout=30.0,
        )
        self.assertEqual(code, 3)
        self.assertIn("no visible change", err.lower())

    def test_send_wait_waits_past_stale_idle_until_content_changes_then_settles(self):
        """Slow TUI render: Poll 1 sees stale idle + unchanged capture; Poll 2 sees change; Poll 3 settles."""
        poll_seq = [
            {"states": {"demo:0.0": {"state": "idle", "idle_seconds": 60.0}}, "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}]},
            {"states": {"demo:0.0": {"state": "running", "idle_seconds": 0.5}}, "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}]},
            {"states": {"demo:0.0": {"state": "idle", "idle_seconds": 31.0}}, "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}]},
        ]
        # Capture 1: baseline before POST /type
        # Capture 2: Poll 1 (still unchanged!)
        # Capture 3: Poll 2 (changed!)
        # Capture 4: Poll 3 (prompt check before returning idle)
        capture_seq = [
            "❯ \n",
            "❯ \n",
            "❯ hello\nThinking...\n",
            "❯ hello\nDone.\n❯ \n",
        ]
        code, _out, err, elapsed = self._run_send_wait(poll_seq, capture_seq, timeout=60.0)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(elapsed, 4.0)
        self.assertIn("next:", err)

    def test_send_wait_fast_shell_command_exits_zero(self):
        shell_poll = {
            "states": {"demo:0.0": {"state": "shell", "idle_seconds": 0.1}},
            "sessions": [{"target": "demo:0.0", "agent_kind": None}],
        }
        code, _out, err, _elapsed = self._run_send_wait(
            [shell_poll],
            ["$ \n", "$ echo hi\nhi\n$ \n"],
            text="echo hi",
            enter=True,
            timeout=30.0,
        )
        self.assertEqual(code, 0)
        self.assertIn("next:", err)

    def test_send_wait_prompt_after_change_exits_10(self):
        poll_seq = [
            {"states": {"demo:0.0": {"state": "running", "idle_seconds": 0.2}}, "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}]},
            {"states": {"demo:0.0": {"state": "running", "idle_seconds": 3.0}}, "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}]},
        ]
        capture_seq = [
            "$ \n",
            "Writing config.toml\nOverwrite the existing file? (y/n/a)\n",
            "Writing config.toml\nOverwrite the existing file? (y/n/a)\n",
        ]
        code, out, _err, _elapsed = self._run_send_wait(poll_seq, capture_seq, timeout=30.0)
        self.assertEqual(code, 10)
        self.assertTrue(out.strip())

    def test_standalone_wait_on_already_idle_pane_still_exits_zero(self):
        """Regression guard: standalone `assist wait` does not require a change."""
        clock = _FakeClock()
        idle_poll = {
            "states": {"demo:0.0": {"state": "idle", "idle_seconds": 60.0}},
            "sessions": [{"target": "demo:0.0", "agent_kind": "claude"}],
        }

        def fake_get(path):
            if path == "/poll":
                return idle_poll
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": "❯ \n"}
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = wait_cli.command("demo", timeout=10.0)

        self.assertEqual(code, 0)

    def test_launch_without_role_wait_still_exits_zero(self):
        """Regression guard: `assist launch --wait` (no --role) uses baseline_hash=None and exits 0."""
        clock = _FakeClock()

        def fake_post(path, payload=None):
            self.assertEqual(path, "/terminal/launch")
            return {
                "session": "demo",
                "target": "demo:0.0",
                "venv": None,
                "venv_activated": False,
                "existed": False,
            }

        def fake_get(path):
            if path == "/poll":
                return {
                    "states": {"demo:0.0": {"state": "shell", "idle_seconds": 0.5}},
                    "sessions": [{"target": "demo:0.0", "agent_kind": None}],
                }
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": "$ \n"}
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                mock.patch.object(session, "reply_address", return_value=None), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.launch("demo", wait_for_completion=True, timeout=10.0)

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
