"""Tests for Phase 3 part (a):
- shared/agent_model.py: permission_mode, context_pct, context_warning, observe_details
- routes/poll.py: exposes permission_mode & context_pct (incl. agy argv via single tree walk)
- assist status S [--pane P] [--json]: one-line & JSON status (model, effort, perms, context%, working/idle/asking)
- assist status (no args): preserves server status dispatch
- assist ls [--json] [--cwd]: adds model, effort, perms, context%, unified state columns
- assist view S [--plain]: strips ANSI escape sequences while default remains byte-identical
"""

import importlib.machinery
import importlib.util
import io
import json
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

from cli import http, proc, session  # noqa: E402
from routes.poll import poll_bp  # noqa: E402
from routes.terminal import terminal_bp  # noqa: E402
from shared import agent_identity, agent_model, state  # noqa: E402
from tests._tmux_box import TmuxBox  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
_RULE = "─" * 60


def _load_bin_assist():
    loader = importlib.machinery.SourceFileLoader("bin_assist_mod_p3a", str(ROOT / "bin" / "assist"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
        loader.exec_module(mod)
    return mod


CLAUDE_READY = "\n".join(
    [
        " ▐▛███▛█   Claude Code v2.1.310",
        "▝▜██████▀  Sonnet 5.5 with high effort · Claude Max",
        " ▝▝   ▝▝   /tmp/project",
        _RULE,
        "❯ ",
        _RULE,
        "  ▓▓▓▓░░░░░░ 42.5% (340k/800k) │ 12m                    ● high · /effort",
        "  project ⌀ │ ✎ 3 │ Sonnet 5.5",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
)

CLAUDE_HIGH_CTX = "\n".join(
    [
        _RULE,
        "❯ ",
        _RULE,
        "  ████████░░ 84.2% (673k/800k) │ 25m                    ● high · /effort",
        "  project ⌀ │ ✎ 9 │ Opus 5.5",
        "  ⏵⏵ accept edits on (shift+tab to cycle)",
    ]
)

CODEX_READY = "\n".join(
    [
        "╭──────────────────────────────────────────────────────╮",
        "│ >_ OpenAI Codex (v0.164.0)                           │",
        "│ model:       gpt-6.1-sol medium   /model to change   │",
        "│ directory:   /tmp/project                            │",
        "│ permissions: YOLO mode                               │",
        "╰──────────────────────────────────────────────────────╯",
        "› Ask Codex to do anything",
        "  GPT-6.1-Sol medium · /tmp/project · Context 97% left",
    ]
)

AGY_READY = "\n".join(
    [
        "      ▄▀▀▄        Antigravity CLI 1.5.12",
        "     ▀▀▀▀▀▀       user@example.com (Google AI Ultra)",
        "    ▀▀▀▀▀▀▀▀      Gemini 4 Neon (High)",
        "   ▄▀▀    ▀▀▄     ~/source/project",
        _RULE,
        "> ",
        _RULE,
        "? for shortcuts                                         Gemini 4 Neon · high",
    ]
)

AGY_TRUST = "\n".join(
    [
        "Accessing workspace:",
        "",
        "/tmp/example-project",
        "",
        "Do you trust the contents of this project?",
        "",
        "> Yes, I trust this folder",
        "  No, exit",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "                                                        Gemini 4 Neon · high",
    ]
)


class AgentModelPermissionsAndContextTests(unittest.TestCase):
    def setUp(self):
        with state._activity_lock:
            state.pane_model.clear()

    def test_resolve_permissions_claude_codex_and_agy(self):
        self.assertEqual(agent_model.resolve_permissions("claude", CLAUDE_READY), "bypass")
        self.assertEqual(agent_model.resolve_permissions("claude", CLAUDE_HIGH_CTX), "accept-edits")
        self.assertEqual(
            agent_model.resolve_permissions("claude", "  plan mode on (shift+tab to cycle)\n"),
            "plan",
        )
        # Echoed shell command must not fake a live banner
        echo = "user@host:~$ claude --model sonnet --dangerously-skip-permissions\n"
        self.assertIsNone(agent_model.resolve_permissions("claude", echo))
        # Argv fallback works when banner scrolled off
        self.assertEqual(
            agent_model.resolve_permissions(
                "claude", "some output\n", argv=["claude", "--dangerously-skip-permissions"]
            ),
            "bypass",
        )

        self.assertEqual(agent_model.resolve_permissions("codex", CODEX_READY), "yolo")
        self.assertEqual(
            agent_model.resolve_permissions("codex", "│ permissions: read-only │\n"),
            "read-only",
        )
        self.assertEqual(
            agent_model.resolve_permissions("codex", "› working\n", argv=["codex", "-m", "gpt-6.1-sol", "--yolo"]),
            "yolo",
        )
        self.assertEqual(
            agent_model.resolve_permissions("codex", "› working\n", argv=["codex", "-m", "gpt-6.1-sol"]),
            "default",
        )

        self.assertEqual(
            agent_model.resolve_permissions(
                "gemini", AGY_READY, argv=["agy", "--model", "gemini-4-neon-high", "--dangerously-skip-permissions"]
            ),
            "bypass",
        )
        self.assertEqual(
            agent_model.resolve_permissions("gemini", AGY_READY, argv=["agy", "--model", "gemini-4-neon-high"]),
            "default",
        )

    def test_prose_spoofing_never_fakes_bypass_on_default_agy_or_claude_and_shifttab_clears(self):
        # Finding 6986: agy never prints permission banners; prose mentioning YOLO/bypass must NOT spoof perms
        agy_prose = (
            "Reviewing CLAUDE.md: codex uses permissions: YOLO mode and claude uses bypass permissions on.\n"
            + AGY_READY
        )
        self.assertEqual(
            agent_model.resolve_permissions("gemini", agy_prose, argv=["agy", "--model", "gemini-4-carbon-high"]),
            "default",
        )
        self.assertIsNone(agent_model.resolve_permissions("gemini", agy_prose, argv=None))

        # Finding 6986: Claude prose above the composer box must NOT spoof bypass/accept-edits/plan
        claude_prose_default = "\n".join(
            [
                "The docs say: bypass permissions on and accept edits on and plan mode on.",
                _RULE,
                "❯ ",
                _RULE,
                "  ░░░░░░░░░░ 12.0% (96k/800k) │ 3m                    ● high · /effort",
                "  project ⌀ │ ✎ 1 │ Opus 5.5",
            ]
        )
        self.assertEqual(
            agent_model.resolve_permissions("claude", claude_prose_default, argv=["claude", "--model", "opus"]),
            "default",
        )
        # Finding 6983: live Claude footer below composer without bypass badge reflects Shift-Tab back to default
        self.assertEqual(
            agent_model.resolve_permissions(
                "claude",
                claude_prose_default,
                argv=["claude", "--model", "opus", "--dangerously-skip-permissions"],
            ),
            "default",
        )

        # Unanchored single line without chrome icon or shift+tab hint is ignored
        self.assertIsNone(
            agent_model.resolve_permissions("claude", "Discussion about bypass permissions on in docs.\n")
        )

    def test_claude_working_and_menu_asking_bypass_panes_keep_bypass_perms(self):
        """Finding 6990: unframed ❯ (prompt echo or menu cursor) must NOT flip bypass panes to default."""
        bypass_argv = ["claude", "--model", "sonnet", "--effort", "high", "--dangerously-skip-permissions"]

        # 1. Real captured Claude 2.1.328 working fixture (framed composer + bypass footer)
        working_fixture = (ROOT / "tests" / "fixtures" / "claude-21328-working.txt").read_text(encoding="utf-8")
        self.assertEqual(agent_model.resolve_permissions("claude", working_fixture, argv=bypass_argv), "bypass")

        # 2. Working Claude screen where composer is scrolled off / hidden below an echoed ❯ user prompt
        working_no_composer = "\n".join(
            [
                " ▐▛███▛█   Claude Code v2.1.328",
                "▝▜██████▀  Sonnet 5.5 with high effort · Claude Max",
                "❯ Fix the auth middleware",
                "",
                "● Reading routes/auth.py…",
                "* Determining… (esc to interrupt)",
            ]
        )
        self.assertEqual(
            agent_model.resolve_permissions("claude", working_no_composer, argv=bypass_argv),
            "bypass",
        )

        # 3. Claude permission / numbered menu asking screen (❯ 1. Yes cursor, no framed composer)
        menu_asking = "\n".join(
            [
                "Bash command",
                "  pytest tests/test_auth.py",
                "Do you want to proceed?",
                "❯ 1. Yes",
                "  2. Yes, and don't ask again for pytest",
                "  3. No, tell Claude what to do differently",
                "Esc to cancel · Tab to amend",
            ]
        )
        self.assertEqual(
            agent_model.resolve_permissions("claude", menu_asking, argv=bypass_argv),
            "bypass",
        )

        # 4. Framed composer whose status footer below the closing rule has no mode badge -> default
        framed_default = "\n".join(
            [
                "Prose above mentioning bypass permissions on.",
                _RULE,
                "❯ ",
                _RULE,
                "  ░░░░░░░░░░ 5.0% (40k/800k) │ 1m                    ● high · /effort",
                "  ✎ 0 │ Sonnet 5.5",
            ]
        )
        self.assertEqual(
            agent_model.resolve_permissions("claude", framed_default, argv=bypass_argv),
            "default",
        )

    def test_resolve_context_pct_and_claude_warning(self):
        self.assertAlmostEqual(agent_model.resolve_context_pct("claude", CLAUDE_READY), 42.5)
        self.assertFalse(agent_model.context_warning("claude", 42.5))

        self.assertAlmostEqual(agent_model.resolve_context_pct("claude", CLAUDE_HIGH_CTX), 84.2)
        self.assertTrue(agent_model.context_warning("claude", 84.2))
        self.assertFalse(agent_model.context_warning("claude", 80.0))

        # 0.0% must return 0.0, not None
        zero_claude = "  ░░░░░░░░░░ 0.0% (0k/800k) │ 0m\n"
        self.assertEqual(agent_model.resolve_context_pct("claude", zero_claude), 0.0)

        # Claude auto-compact left banner -> converts to % used
        compact = "Context left until auto-compact: 15%\n"
        self.assertAlmostEqual(agent_model.resolve_context_pct("claude", compact), 85.0)
        self.assertTrue(agent_model.context_warning("claude", 85.0))

        # Codex "Context 97% left" -> 3.0% used; "Context 45% used" -> 45.0% used
        self.assertAlmostEqual(agent_model.resolve_context_pct("codex", CODEX_READY), 3.0)
        self.assertAlmostEqual(
            agent_model.resolve_context_pct("codex", "  GPT-6.1-Sol · Context 45% used\n"),
            45.0,
        )
        self.assertFalse(agent_model.context_warning("codex", 90.0))

        # agy does not show context % -> None
        self.assertIsNone(agent_model.resolve_context_pct("gemini", AGY_READY))

        # Random prose percentage must not parse as context %
        prose = "Tests improved coverage by 99% today.\n❯ \n"
        self.assertIsNone(agent_model.resolve_context_pct("claude", prose))

    def test_observe_details_updates_immediately_preserves_zero_and_resets_on_pane_change(self):
        zero_codex = "  GPT-6.1-Sol medium · /tmp/p · Context 100% left\n"
        details = agent_model.observe_details(
            "s:0.0", "codex", "%1", zero_codex, argv=["codex", "--yolo"]
        )
        self.assertEqual(details["permission_mode"], "yolo")
        self.assertEqual(details["context_pct"], 0.0)

        # Transient miss (blank/scrolled screen) retains established values for same pane
        details2 = agent_model.observe_details("s:0.0", "codex", "%1", "scrolling...\n", argv=None)
        self.assertEqual(details2["permission_mode"], "yolo")
        self.assertEqual(details2["context_pct"], 0.0)

        # Recycled pane_id resets stale values immediately
        details3 = agent_model.observe_details("s:0.0", "codex", "%2", "fresh screen\n", argv=None)
        self.assertIsNone(details3["permission_mode"])
        self.assertIsNone(details3["context_pct"])


class AssistStatusCliTests(unittest.TestCase):
    def test_bare_assist_status_still_calls_server_status(self):
        bin_assist = _load_bin_assist()
        fake_resolved = mock.MagicMock()
        fake_resolved.home = ROOT
        with mock.patch.object(bin_assist, "resolve", return_value=fake_resolved), \
                mock.patch.object(bin_assist.paths, "enter_runtime"), \
                mock.patch.object(bin_assist.proc, "status", return_value=0) as server_status:
            code = bin_assist.main(["status"])
        self.assertEqual(code, 0)
        server_status.assert_called_once_with(fake_resolved)

    def test_status_json_without_session_is_refused(self):
        bin_assist = _load_bin_assist()
        fake_resolved = mock.MagicMock()
        fake_resolved.home = ROOT
        err = io.StringIO()
        with mock.patch.object(bin_assist, "resolve", return_value=fake_resolved), \
                mock.patch.object(bin_assist.paths, "enter_runtime"), \
                redirect_stderr(err):
            code = bin_assist.main(["status", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("SESSION", err.getvalue())

    def test_status_one_line_and_json_for_claude_working_and_high_context(self):
        poll_resp = {
            "sessions": [
                {
                    "target": "del_build:0.0",
                    "session": "del_build",
                    "pane_id": "%5",
                    "key": "gen:%5",
                    "agent_kind": "claude",
                    "model": "sonnet 5.5",
                    "model_effort": "high",
                    "permission_mode": "bypass",
                    "context_pct": 42.5,
                    "is_subpane": False,
                }
            ],
            "states": {"del_build:0.0": {"state": "running", "idle_seconds": 3.0}},
            "scan": [{"target": "del_build:0.0", "tail": CLAUDE_READY}],
        }

        def fake_get(path):
            if path == "/poll":
                return poll_resp
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": CLAUDE_READY}
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "1"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = session.status("del_build", json_output=False)
        self.assertEqual(code, 0)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertIn("del_build", line)
        self.assertIn("state=working", line)
        self.assertIn("model=sonnet 5.5", line)
        self.assertIn("effort=high", line)
        self.assertIn("perms=bypass", line)
        self.assertIn("context=42.5%", line)

        # JSON output with high Claude context (>80%) sets context_warn=True
        def fake_get_high(path):
            if path == "/poll":
                return {
                    "sessions": [
                        {
                            "target": "del_build:0.0",
                            "session": "del_build",
                            "pane_id": "%5",
                            "key": "gen:%5",
                            "agent_kind": "claude",
                            "is_subpane": False,
                        }
                    ],
                    "states": {"del_build:0.0": {"state": "idle", "idle_seconds": 45.0}},
                    "scan": [{"target": "del_build:0.0", "tail": CLAUDE_HIGH_CTX}],
                }
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": CLAUDE_HIGH_CTX}
            raise AssertionError(f"unexpected GET {path}")

        out_j = io.StringIO()
        err_j = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get_high), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "0"}), \
                redirect_stdout(out_j), redirect_stderr(err_j):
            code = session.status("del_build", json_output=True)
        self.assertEqual(code, 0)
        self.assertEqual(err_j.getvalue(), "")
        data = json.loads(out_j.getvalue())
        self.assertEqual(data["state"], "idle")
        self.assertEqual(data["model"], "opus 5.5")
        self.assertEqual(data["effort"], "high")
        self.assertEqual(data["permission_mode"], "accept-edits")
        self.assertAlmostEqual(data["context_pct"], 84.2)
        self.assertTrue(data["context_warn"])

    def test_status_reports_asking_on_agy_trust_dialog_and_handles_base_index_1(self):
        def fake_get(path):
            if path == "/poll":
                return {
                    "sessions": [
                        {
                            "target": "del_agy:1.1",
                            "session": "del_agy",
                            "pane_id": "%9",
                            "key": "gen:%9",
                            "agent_kind": "gemini",
                            "permission_mode": "bypass",
                            "is_subpane": False,
                        }
                    ],
                    "states": {"del_agy:1.1": {"state": "idle", "idle_seconds": 40.0}},
                    "scan": [{"target": "del_agy:1.1", "tail": AGY_TRUST}],
                }
            if path.startswith("/terminal/capture"):
                self.assertIn("del_agy%3A1.1", path)
                return {"ok": True, "content": AGY_TRUST}
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                redirect_stdout(out):
            code = session.status("del_agy", json_output=True)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["target"], "del_agy:1.1")
        self.assertEqual(data["state"], "asking")
        self.assertEqual(data["model"], "gemini 4 neon")
        self.assertEqual(data["effort"], "high")
        self.assertEqual(data["permission_mode"], "bypass")
        self.assertIsNone(data["context_pct"])
        self.assertIsNotNone(data["prompt"])

    def test_status_missing_session_and_http_error_are_distinct_errors(self):
        err = io.StringIO()
        with mock.patch.object(
            session.http, "get", return_value={"sessions": [], "states": {}, "scan": []}
        ), redirect_stderr(err):
            code = session.status("no_such_session", json_output=True)
        self.assertEqual(code, 1)
        self.assertIn("no_such_session", err.getvalue())

        with mock.patch.object(
            session.http, "get", side_effect=http.ApiError("connection", "refused", exit_code=1)
        ):
            with self.assertRaises(http.ApiError):
                session.status("del_x", json_output=True)

        # Missing capture content fails explicitly instead of faking an empty/idle pane
        def fake_missing_content(path):
            if path == "/poll":
                return {
                    "sessions": [{"target": "del_x:0.0", "session": "del_x", "agent_kind": "claude"}],
                    "states": {"del_x:0.0": {"state": "idle", "idle_seconds": 10.0}},
                }
            return {"ok": False}

        err2 = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_missing_content), \
                redirect_stderr(err2):
            code2 = session.status("del_x", json_output=True)
        self.assertEqual(code2, 1)
        self.assertIn("no content", err2.getvalue())

        # Missing poll state in ls yields None (rendered as '-'), never fake 'idle'
        derived = session._derive_pane_status(
            {"session": "gone", "target": "gone:0.0", "agent_kind": "claude"},
            {},
            tail=None,
        )
        self.assertIsNone(derived["state"])

    def test_status_pane_selector_targets_explicit_subpane(self):
        def fake_get(path):
            if path == "/poll":
                return {
                    "sessions": [
                        {
                            "target": "multi:0.0",
                            "session": "multi",
                            "pane_id": "%1",
                            "agent_kind": "claude",
                            "is_subpane": False,
                        },
                        {
                            "target": "multi:0.1",
                            "session": "multi",
                            "pane_id": "%2",
                            "agent_kind": "codex",
                            "permission_mode": "yolo",
                            "is_subpane": True,
                        },
                    ],
                    "states": {
                        "multi:0.0": {"state": "idle", "idle_seconds": 50.0},
                        "multi:0.1": {"state": "running", "idle_seconds": 2.0},
                    },
                }
            if "multi%3A0.1" in path:
                return {"ok": True, "content": CODEX_READY}
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                redirect_stdout(out):
            code = session.status("multi", pane="0.1", json_output=True)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["target"], "multi:0.1")
        self.assertEqual(data["state"], "working")
        self.assertEqual(data["model"], "gpt-6.1-sol")


class AssistLsColumnsTests(unittest.TestCase):
    def test_ls_includes_model_effort_perms_context_and_unified_state(self):
        sessions_resp = {
            "sessions": [
                {
                    "target": "del_c:0.0",
                    "session": "del_c",
                    "agent_kind": "claude",
                    "command_display": "claude",
                    "is_subpane": False,
                },
                {
                    "target": "del_x:0.0",
                    "session": "del_x",
                    "agent_kind": "codex",
                    "command_display": "codex",
                    "is_subpane": False,
                },
                {
                    "target": "del_g:0.0",
                    "session": "del_g",
                    "agent_kind": "gemini",
                    "command_display": "agy",
                    "is_subpane": False,
                },
            ]
        }
        poll_resp = {
            "sessions": [
                {
                    "target": "del_c:0.0",
                    "session": "del_c",
                    "agent_kind": "claude",
                    "model": "sonnet 5.5",
                    "model_effort": "high",
                    "permission_mode": "bypass",
                    "context_pct": 42.5,
                },
                {
                    "target": "del_x:0.0",
                    "session": "del_x",
                    "agent_kind": "codex",
                    "model": "sol",
                    "model_effort": "medium",
                    "permission_mode": "yolo",
                    "context_pct": 3.0,
                },
                {
                    "target": "del_g:0.0",
                    "session": "del_g",
                    "agent_kind": "gemini",
                    "model": "gemini 4 neon",
                    "model_effort": "high",
                    "permission_mode": "bypass",
                    "context_pct": None,
                },
            ],
            "states": {
                "del_c:0.0": {"state": "running", "idle_seconds": 5.0},
                "del_x:0.0": {"state": "idle", "idle_seconds": 60.0},
                "del_g:0.0": {"state": "idle", "idle_seconds": 40.0},
            },
            "scan": [
                {"target": "del_c:0.0", "tail": CLAUDE_READY},
                {"target": "del_x:0.0", "tail": CODEX_READY},
                {"target": "del_g:0.0", "tail": AGY_TRUST},
            ],
        }

        def fake_get(path):
            if path == "/terminal/sessions":
                return sessions_resp
            if path == "/poll":
                return poll_resp
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "1"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = session.list_sessions(json_output=False)
        self.assertEqual(code, 0)
        rows = out.getvalue().strip().splitlines()
        self.assertEqual(len(rows), 3)
        self.assertIn("working", rows[0])
        self.assertIn("sonnet 5.5", rows[0])
        self.assertIn("high", rows[0])
        self.assertIn("bypass", rows[0])
        self.assertIn("42.5%", rows[0])

        self.assertIn("idle", rows[1])
        self.assertIn("gpt-6.1-sol", rows[1])
        self.assertIn("medium", rows[1])
        self.assertIn("yolo", rows[1])
        self.assertIn("3%", rows[1])

        self.assertIn("asking", rows[2])
        self.assertIn("gemini 4 neon", rows[2])

        # JSON output carries all structured fields
        out_j = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                redirect_stdout(out_j):
            code = session.list_sessions(json_output=True)
        self.assertEqual(code, 0)
        parsed = json.loads(out_j.getvalue())["sessions"]
        by_name = {p["session"]: p for p in parsed}
        self.assertEqual(by_name["del_c"]["state"], "working")
        self.assertEqual(by_name["del_c"]["permission_mode"], "bypass")
        self.assertAlmostEqual(by_name["del_c"]["context_pct"], 42.5)
        self.assertEqual(by_name["del_x"]["state"], "idle")
        self.assertEqual(by_name["del_x"]["effort"], "medium")
        self.assertEqual(by_name["del_g"]["state"], "asking")


class AssistViewPlainTests(unittest.TestCase):
    def test_view_default_keeps_ansi_and_plain_strips_it(self):
        raw = "\x1b[32mOK\x1b[0m \x1b]0;title\x07\x1b[1;36mGemini 4 Neon · high\x1b[0m\n"

        def fake_get(path):
            if path.startswith("/terminal/capture"):
                return {"ok": True, "content": raw}
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        out_raw = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "1"}), \
                redirect_stdout(out_raw):
            code = session.view("demo", plain=False)
        self.assertEqual(code, 0)
        self.assertEqual(out_raw.getvalue(), raw)

        out_plain = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "1"}), \
                redirect_stdout(out_plain):
            code = session.view("demo", plain=True)
        self.assertEqual(code, 0)
        self.assertEqual(out_plain.getvalue(), "OK Gemini 4 Neon · high\n")
        self.assertNotIn("\x1b", out_plain.getvalue())

    def test_view_parser_accepts_plain_flag(self):
        bin_assist = _load_bin_assist()
        parsed = bin_assist.build_parser().parse_args(["view", "demo", "--plain", "-n", "20"])
        self.assertTrue(parsed.plain)
        self.assertEqual(parsed.lines, 20)


class RealTmuxStatusEndToEndTests(unittest.TestCase):
    def test_status_and_view_plain_against_real_isolated_tmux_and_flask(self):
        box = TmuxBox(self)
        banner_script = (
            "printf '\\033[36m  GPT-6.1-Sol medium · /tmp/p · Context 92%% left\\033[0m\\n'; "
            "printf '│ permissions: YOLO mode │\\n'; "
            "sleep 30"
        )
        box.new_session("statbox", f"bash -c {json.dumps(banner_script)}")

        app = Flask(__name__)
        app.register_blueprint(poll_bp)
        app.register_blueprint(terminal_bp)
        client = app.test_client()

        def fake_get(path):
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            return resp.get_json()

        # Wait briefly for bash printf to render into pane
        import time

        for _ in range(25):
            cap = fake_get("/terminal/capture?target=statbox:1.1").get("content", "")
            if "Context 92% left" in cap:
                break
            time.sleep(0.1)

        out_plain = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.dict(os.environ, {"ASSIST_NO_HINTS": "1"}), \
                redirect_stdout(out_plain):
            self.assertEqual(session.view("statbox", pane="1.1", plain=True), 0)
        self.assertNotIn("\x1b", out_plain.getvalue())
        self.assertIn("Context 92% left", out_plain.getvalue())

        out_status = io.StringIO()
        with mock.patch.object(session.http, "get", side_effect=fake_get), \
                redirect_stdout(out_status):
            self.assertEqual(session.status("statbox", json_output=True), 0)
        data = json.loads(out_status.getvalue())
        self.assertEqual(data["session"], "statbox")
        self.assertEqual(data["agent_kind"], "codex")
        self.assertEqual(data["model"], "gpt-6.1-sol")
        self.assertEqual(data["effort"], "medium")
        self.assertEqual(data["permission_mode"], "yolo")
        self.assertAlmostEqual(data["context_pct"], 8.0)

    def test_poll_propagates_agy_argv_from_process_tree_without_banner_perms(self):
        """Finding 6988/6979: real /poll -> agent_identity._scan_process_tree -> last_agent_argv -> permission_mode."""
        from routes import poll as poll_mod
        from shared.tmux import PaneRef

        for label, argv, want_perm in (
            (
                "bypass",
                ["agy", "--model", "gemini-4-neon-high", "--dangerously-skip-permissions"],
                "bypass",
            ),
            (
                "default",
                ["agy", "--model", "gemini-4-neon-high"],
                "default",
            ),
        ):
            with self.subTest(case=label):
                with state._activity_lock:
                    state.pane_model.clear()
                with agent_identity._PROCESS_CACHE_LOCK:
                    agent_identity._PROCESS_CACHE.clear()
                    agent_identity._LAST_AGENT_ARGV.clear()
                poll_mod._SCAN_CACHE.clear()

                ref = PaneRef(
                    pane_id="%77",
                    generation="gen1",
                    session="del_agy",
                    display_target="del_agy:0.0",
                    window=0,
                    pane=0,
                    pane_pid="100",
                    command="node",
                    width=120,
                    height=40,
                    session_activity=0,
                    session_created=0,
                    pane_tty="/dev/pts/99",
                )
                snapshot = {
                    "children": {100: [200]},
                    "start_times": {100: "1000", 200: "2000"},
                    "process_info": {100: ("bash", ["bash"]), 200: ("agy", list(argv))},
                }
                app = Flask(__name__)
                app.register_blueprint(poll_bp)
                client = app.test_client()

                def fake_start(pid):
                    return {100: "1000", 200: "2000"}.get(pid)

                with mock.patch.object(poll_mod, "list_panes", return_value=[ref]), \
                        mock.patch.object(poll_mod, "_capture_tail", return_value=AGY_READY), \
                        mock.patch.object(poll_mod, "enrich_panes_with_agents"), \
                        mock.patch.object(poll_mod, "delegate_snapshot", return_value={"watch": {}, "panes": []}), \
                        mock.patch.object(poll_mod, "resume_for_pane", return_value=(None, "no session yet")), \
                        mock.patch.object(agent_identity, "_get_process_snapshot", return_value=snapshot), \
                        mock.patch.object(agent_identity, "_proc_start_time", side_effect=fake_start):
                    resp = client.get("/poll")

                self.assertEqual(resp.status_code, 200)
                panes = resp.get_json()["sessions"]
                self.assertEqual(len(panes), 1)
                self.assertEqual(panes[0]["agent_kind"], "gemini")
                self.assertEqual(panes[0]["permission_mode"], want_perm)
                self.assertIsNone(panes[0]["context_pct"])


if __name__ == "__main__":
    unittest.main()
