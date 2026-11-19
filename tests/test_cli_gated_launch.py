"""Tests for Phase 2 gated launch:
`assist launch --role R --family F --brief FILE [--trust-cwd]`

Covers:
- CLI flags and pre-launch validation (--brief / --trust-cwd require --role;
  missing/empty brief refused before launching a pane).
- agy trust dialog: refused without --trust-cwd, accepted once with --trust-cwd
  (never Auto-Yes), refused if dialog does not clear, never accepts data wizard.
- Banner gate for Claude, Codex, and agy: model, effort, and permissions mode
  (claude "bypass permissions on", codex "permissions: YOLO mode", agy launched
  with --dangerously-skip-permissions). Shell echo alone must not pass.
- Brief pasted as one message only after all gates pass, then working confirmed.
- Failed gates leave the pane untouched (never kill), exit non-zero with clear
  reason, and NEVER arm Auto-Yes on any path.
- End-to-end isolated tmux test through real Flask routes.
"""

import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
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
from routes.poll import poll_bp  # noqa: E402
from routes.terminal import terminal_bp  # noqa: E402
from tests._tmux_box import TmuxBox  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]

ROSTER_TEXT = """\
updated = "2026-11-18"
note = "test roster"

[roles.implement]
claude = 'claude --model sonnet --effort high --dangerously-skip-permissions'
codex = 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" --yolo'
gemini = 'agy --model gemini-4-neon-high --dangerously-skip-permissions'

[roles.bad_perms]
claude = 'claude --model sonnet --effort high'
codex = 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium"'
gemini = 'agy --model gemini-4-neon-high'

[roles.model_only]
gemini = 'agy --model gemini-4-neon --dangerously-skip-permissions'
"""

_RULE = "─" * 60

AGY_TRUST = "\n".join(
    [
        "Accessing workspace:",
        "",
        "/tmp/example-project",
        "",
        "Do you trust the contents of this project?",
        "",
        "Antigravity CLI requires permission to read, edit, and execute files here.",
        "",
        "> Yes, I trust this folder",
        "  No, exit",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_TRUST_40COL = "\n".join(
    [
        "Accessing workspace:",
        "",
        "/tmp/example-project",
        "",
        "Do you trust the contents of this",
        "project?",
        "",
        "> Yes, I trust this folder",
        "  No, exit",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "Gemini 4 Neon · high",
    ]
)

AGY_DATA_WIZARD = "\n".join(
    [
        "Terms of Service & Data Use",
        "",
        *[f"  Terms paragraph line {i}" for i in range(1, 15)],
        "",
        "> [x] Yes, I agree to help improve Antigravity CLI by allowing Google to collect",
        "      and use my Interactions data",
        "  [ ] No, do not collect my Interactions data",
        "",
        "  ↑/↓ Navigate · enter Toggle",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_COLOR_SCHEME = "\n".join(
    [
        "Choose your color scheme:",
        "",
        "> Dark",
        "  Light",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_SETTINGS_ERROR = "\n".join(
    [
        "⚠ Settings Error",
        "",
        "Failed to parse settings.json",
        "",
        "> Continue with defaults",
        "  Exit",
        "",
        "  enter Confirm · esc Exit",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_BANNER_OK = "\n".join(
    [
        "user@host:~/project$ agy --model gemini-4-neon-high --dangerously-skip-permissions",
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

CLAUDE_BANNER_OK = "\n".join(
    [
        "user@host:~/project$ claude --model sonnet --effort high --dangerously-skip-permissions",
        " ▐▛███▛█   Claude Code v2.1.310",
        "▝▜██████▀  Sonnet 5.5 with high effort · Claude Max",
        " ▝▝   ▝▝   /tmp/project",
        _RULE,
        "❯ ",
        _RULE,
        "  ░░░░░░░░░░ 0.0% (0k/800k) │ 0m                    ● high · /effort",
        "  project ⌀ │ ✎ 0 │ Sonnet 5.5",
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    ]
)

CODEX_BANNER_OK = "\n".join(
    [
        'user@host:~/project$ codex -m gpt-6.1-sol -c model_reasoning_effort="medium" --yolo',
        "╭──────────────────────────────────────────────────────╮",
        "│ >_ OpenAI Codex (v0.164.0)                           │",
        "│ model:       gpt-6.1-sol medium   /model to change   │",
        "│ directory:   /tmp/project                            │",
        "│ permissions: YOLO mode                               │",
        "╰──────────────────────────────────────────────────────╯",
        "› Ask Codex to do anything",
        "  GPT-6.1-Sol medium · /tmp/project · Context 100% left",
    ]
)


def _load_bin_assist():
    loader = importlib.machinery.SourceFileLoader("bin_assist_mod_p2", str(ROOT / "bin" / "assist"))
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


class GatedLaunchCliValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = self.root / "roster.toml"
        self.roster.write_text(ROSTER_TEXT, encoding="utf-8")
        self.brief = self.root / "BRIEF.md"
        self.brief.write_text("# Task\nDo the work.\n", encoding="utf-8")

    def test_parser_accepts_brief_and_trust_cwd(self):
        bin_assist = _load_bin_assist()
        parsed = bin_assist.build_parser().parse_args(
            [
                "launch",
                "--session",
                "del_x",
                "--role",
                "implement",
                "--family",
                "gemini",
                "--brief",
                str(self.brief),
                "--trust-cwd",
            ]
        )
        self.assertEqual(parsed.brief_path, str(self.brief))
        self.assertTrue(parsed.trust_cwd)

    def test_brief_and_trust_cwd_require_role(self):
        for kwargs in ({"brief_path": str(self.brief)}, {"trust_cwd": True}):
            with self.subTest(kwargs=kwargs):
                err = io.StringIO()
                with mock.patch.object(session.http, "post") as post, \
                        redirect_stderr(err), \
                        self.assertRaises(SystemExit) as raised:
                    session.launch("del_x", **kwargs)
                self.assertEqual(raised.exception.code, 2)
                post.assert_not_called()

    def test_missing_or_empty_brief_refused_before_launch(self):
        missing = str(self.root / "nope.md")
        empty = self.root / "empty.md"
        empty.write_text("   \n\t\n", encoding="utf-8")
        for path in (missing, str(empty)):
            with self.subTest(path=path):
                err = io.StringIO()
                with mock.patch.object(session.http, "post") as post, \
                        redirect_stderr(err), \
                        self.assertRaises(SystemExit) as raised:
                    session.launch(
                        "del_x",
                        role="implement",
                        family="gemini",
                        roster_path=self.roster,
                        brief_path=path,
                    )
                self.assertNotEqual(raised.exception.code, 0)
                post.assert_not_called()

    def test_trust_cwd_refused_for_non_agy_family_before_launch(self):
        for family in ("claude", "codex"):
            with self.subTest(family=family):
                err = io.StringIO()
                with mock.patch.object(session.http, "post") as post, \
                        redirect_stderr(err), \
                        self.assertRaises(SystemExit) as raised:
                    session.launch(
                        "del_x",
                        role="implement",
                        family=family,
                        roster_path=self.roster,
                        brief_path=str(self.brief),
                        trust_cwd=True,
                    )
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("agy", err.getvalue().lower())
                post.assert_not_called()


class GatedLaunchFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster = self.root / "roster.toml"
        self.roster.write_text(ROSTER_TEXT, encoding="utf-8")
        self.brief = self.root / "BRIEF.md"
        self.brief.write_text("# Brief\nLine 1\nLine 2\n", encoding="utf-8")

    def _run_launch(
        self,
        *,
        role="implement",
        family="gemini",
        trust_cwd=False,
        captures=None,
        polls=None,
        timeout=30.0,
        agent_kind="gemini",
    ):
        clock = _FakeClock()
        http_calls = []
        cap_iter = iter(captures)
        last_cap = captures[-1]
        poll_seq = polls or [
            {
                "states": {"del_p2:0.0": {"state": "running", "idle_seconds": 2.0}},
                "sessions": [{"target": "del_p2:0.0", "agent_kind": agent_kind}],
            }
        ]
        poll_iter = iter(poll_seq)
        last_poll = poll_seq[-1]

        def fake_post(path, payload=None):
            http_calls.append(("POST", path, payload))
            if path == "/terminal/launch":
                return {
                    "session": "del_p2",
                    "target": "del_p2:0.0",
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
                return {"ok": True, "content": next(cap_iter, last_cap)}
            if path == "/poll":
                return next(poll_iter, last_poll)
            if path.startswith("/api/resume"):
                return {"resume": None}
            raise AssertionError(f"unexpected GET {path}")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.http, "post", side_effect=fake_post), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                mock.patch("cli.launch_gate.time.monotonic", side_effect=clock.monotonic), \
                mock.patch("cli.launch_gate.time.sleep", side_effect=clock.sleep), \
                mock.patch("cli.launch_gate.http.post", side_effect=fake_post), \
                mock.patch("cli.launch_gate.http.get", side_effect=fake_get), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = session.launch(
                "del_p2",
                role=role,
                family=family,
                roster_path=self.roster,
                brief_path=str(self.brief),
                trust_cwd=trust_cwd,
                timeout=timeout,
            )

        # Invariant across ALL paths: NEVER touch Auto-Yes and NEVER kill the pane!
        self.assertEqual([c for c in http_calls if "/autoyes" in c[1]], [])
        self.assertEqual([c for c in http_calls if "/terminal/kill" in c[1]], [])
        type_posts = [c[2] for c in http_calls if c[0] == "POST" and c[1] == "/type"]
        return code, stdout.getvalue(), stderr.getvalue(), type_posts

    def test_happy_path_claude_codex_and_agy(self):
        claude_21328_live = (
            ROOT / "tests" / "fixtures" / "claude-21328-working.txt"
        ).read_text(encoding="utf-8")
        claude_21328_modebar = (
            " ▐▛███▛█   Claude Code v2.1.328\n"
            "▝▜██████▀  Sonnet 5.5 with high effort · Claude Ultra\n"
            " ▝▝   ▝▝   /tmp/project\n\n"
            "✢ Jitterbugging…\n"
            "                                                                                                        ● high · /effort\n"
            f"{_RULE}\n"
            "❯ \n"
            f"{_RULE}\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents\n"
        )
        claude_subsecond_done = (
            CLAUDE_BANNER_OK + "\n❯ # Brief\n\n✻ Sautéed for 1s · done\n"
        )
        codex_subsecond_done = (
            CODEX_BANNER_OK + "\n• Worked for 1s\n"
        )
        cases = [
            (
                "claude-2.1.328-spinner",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                claude_21328_live,
            ),
            (
                "claude-2.1.328-modebar-esc",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                claude_21328_modebar,
            ),
            (
                "claude-subsecond-done",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                claude_subsecond_done,
            ),
            (
                "codex-working",
                "codex",
                "codex",
                CODEX_BANNER_OK,
                CODEX_BANNER_OK + "\n• Working (1s • esc to interrupt)\n",
            ),
            (
                "codex-subsecond-done",
                "codex",
                "codex",
                CODEX_BANNER_OK,
                codex_subsecond_done,
            ),
            (
                "agy-working",
                "gemini",
                "gemini",
                AGY_BANNER_OK,
                AGY_BANNER_OK + "\n⣾ Running...\n",
            ),
        ]
        for label, family, kind, banner, working in cases:
            with self.subTest(case=label):
                # Captures: pre-cmd baseline, poll1 banner, poll2 banner (stable),
                # pre-brief baseline, post-brief working
                code, out, err, posts = self._run_launch(
                    family=family,
                    agent_kind=kind,
                    captures=["$ ", banner, banner, banner, working],
                )
                self.assertEqual(code, 0, f"{label}: err={err!r} out={out!r}")
                self.assertEqual(len(posts), 2, f"expected cmd + single brief paste, got {posts}")
                self.assertEqual(posts[1]["text"], "# Brief\nLine 1\nLine 2\n")
                self.assertTrue(posts[1]["enter"])

    def test_agy_trust_dialog_refused_without_trust_cwd(self):
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            trust_cwd=False,
            captures=["$ ", AGY_TRUST, AGY_TRUST],
        )
        self.assertEqual(code, 10)
        self.assertIn("--trust-cwd", err)
        # Only the roster launch command was sent — neither Enter nor the brief!
        self.assertEqual(len(posts), 1)

    def test_agy_trust_dialog_accepted_with_trust_cwd_then_gated_and_brief_sent(self):
        working = AGY_BANNER_OK + "\n○ Thought for 1s\n"
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            trust_cwd=True,
            captures=["$ ", AGY_TRUST, AGY_TRUST, AGY_TRUST, AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK, working],
        )
        self.assertEqual(code, 0, f"err={err!r}")
        self.assertEqual(len(posts), 3)
        # Post 0: roster cmd; Post 1: bare Enter for trust dialog; Post 2: brief
        self.assertEqual(posts[1]["text"], "")
        self.assertTrue(posts[1]["enter"])
        self.assertEqual(posts[2]["text"], "# Brief\nLine 1\nLine 2\n")

    def test_agy_trust_dialog_not_clearing_after_one_enter_refuses(self):
        for label, caps in (
            ("redrawn-still-trust", ["$ ", AGY_TRUST, AGY_TRUST, AGY_TRUST, AGY_TRUST + " ", AGY_TRUST + " "]),
            ("unchanged-still-trust", ["$ ", AGY_TRUST, AGY_TRUST, AGY_TRUST, AGY_TRUST]),
        ):
            with self.subTest(case=label):
                code, _out, err, posts = self._run_launch(
                    family="gemini",
                    agent_kind="gemini",
                    trust_cwd=True,
                    captures=caps,
                )
                self.assertEqual(code, 10, f"{label}: err={err!r}")
                self.assertIn("trust", err.lower())
                # Must send at most ONE bare Enter, and NEVER paste the brief
                self.assertEqual(len(posts), 2)

    def test_agy_startup_modals_and_40col_trust_refused_never_pasted(self):
        # Finding 6970: real agy 1.5.12 modals carry "Gemini 4 Neon · high" at the bottom;
        # none may pass evaluate_banner_gate or receive BRIEF.md!
        for label, modal, trust_cwd in (
            ("24-row-data-wizard-with-trust", AGY_DATA_WIZARD, True),
            ("color-scheme-with-trust", AGY_COLOR_SCHEME, True),
            ("settings-error-with-trust", AGY_SETTINGS_ERROR, True),
            ("40col-trust-without-trust-cwd", AGY_TRUST_40COL, False),
        ):
            with self.subTest(case=label):
                code, _out, err, posts = self._run_launch(
                    family="gemini",
                    agent_kind="gemini",
                    trust_cwd=trust_cwd,
                    captures=["$ ", modal, modal, modal],
                )
                self.assertEqual(code, 10, f"{label}: err={err!r}")
                self.assertTrue(err.strip())
                self.assertEqual(len(posts), 1, f"{label}: must not accept or paste brief")

        # 40-col wrapped trust dialog WITH --trust-cwd must be accepted once, then pass banner
        working = AGY_BANNER_OK + "\n○ Thought for 1s\n"
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            trust_cwd=True,
            captures=["$ ", AGY_TRUST_40COL, AGY_TRUST_40COL, AGY_TRUST_40COL, AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK, working],
        )
        self.assertEqual(code, 0, f"40col with --trust-cwd failed: err={err!r}")
        self.assertEqual(len(posts), 3)

    def test_banner_followed_by_modal_on_next_poll_is_refused(self):
        # Finding 6969: banner must settle across a second capture before pasting the brief
        codex_modal = (
            CODEX_BANNER_OK
            + "\n› 1. Retry with a faster model\n  2. Dismiss and keep waiting\n"
            + "  Press enter to confirm or esc to cancel\n"
        )
        code, _out, err, posts = self._run_launch(
            family="codex",
            agent_kind="codex",
            captures=["$ ", CODEX_BANNER_OK, codex_modal, codex_modal],
            polls=[
                {
                    "states": {"del_p2:0.0": {"state": "idle", "idle_seconds": 35.0}},
                    "sessions": [{"target": "del_p2:0.0", "agent_kind": "codex"}],
                }
            ],
        )
        self.assertEqual(code, 10, f"err={err!r}")
        self.assertEqual(len(posts), 1, "must not paste brief before banner settles")

    def test_poll_state_shell_on_poll1_before_fork_does_not_abort_launch(self):
        # Finding 6971: Poll 1 observes bash (state="shell") right after command echo,
        # before the agent forks; launch must NOT fail on Poll 1.
        shell_echo = "user@host:~/project$ agy --model gemini-4-neon-high --dangerously-skip-permissions\n"
        working = AGY_BANNER_OK + "\n⣾ Running...\n"
        polls = [
            {"states": {"del_p2:0.0": {"state": "shell", "idle_seconds": 0.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "shell"}]},
            {"states": {"del_p2:0.0": {"state": "running", "idle_seconds": 1.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}]},
            {"states": {"del_p2:0.0": {"state": "running", "idle_seconds": 3.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}]},
            {"states": {"del_p2:0.0": {"state": "running", "idle_seconds": 0.5}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}]},
        ]
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            captures=["$ ", shell_echo, AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK, working],
            polls=polls,
        )
        self.assertEqual(code, 0, f"Poll 1 shell race aborted launch: err={err!r}")
        self.assertEqual(len(posts), 2)

    def test_banner_gate_rejects_wrong_model_effort_and_permissions(self):
        bad_cases = [
            (
                "claude wrong model",
                "implement",
                "claude",
                "claude",
                CLAUDE_BANNER_OK.replace("Sonnet 5.5", "Haiku 4.5"),
                "model",
            ),
            (
                "claude wrong effort",
                "implement",
                "claude",
                "claude",
                CLAUDE_BANNER_OK.replace("high effort", "low effort").replace("● high", "○ low"),
                "effort",
            ),
            (
                "claude missing bypass permissions",
                "implement",
                "claude",
                "claude",
                CLAUDE_BANNER_OK.replace("bypass permissions on", "accept edits on"),
                "permissions",
            ),
            (
                "codex wrong model version",
                "implement",
                "codex",
                "codex",
                CODEX_BANNER_OK.replace("gpt-6.1-sol", "gpt-6-sol").replace("GPT-6.1-Sol", "GPT-6-Sol"),
                "model",
            ),
            (
                "codex wrong effort",
                "implement",
                "codex",
                "codex",
                CODEX_BANNER_OK.replace("medium", "low"),
                "effort",
            ),
            (
                "codex missing YOLO permissions",
                "implement",
                "codex",
                "codex",
                CODEX_BANNER_OK.replace("permissions: YOLO mode", "permissions: on-request"),
                "permissions",
            ),
            (
                "agy wrong model",
                "implement",
                "gemini",
                "gemini",
                AGY_BANNER_OK.replace("Gemini 4 Neon", "Gemini 4 Argon"),
                "model",
            ),
            (
                "agy wrong effort",
                "implement",
                "gemini",
                "gemini",
                AGY_BANNER_OK.replace("(High)", "(Low)").replace("· high", "· low"),
                "effort",
            ),
            (
                "agy roster missing dangerously-skip-permissions",
                "bad_perms",
                "gemini",
                "gemini",
                AGY_BANNER_OK,
                "permissions",
            ),
            (
                "claude roster missing dangerously-skip-permissions flag",
                "bad_perms",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                "permissions",
            ),
            (
                "codex roster missing yolo flag",
                "bad_perms",
                "codex",
                "codex",
                CODEX_BANNER_OK,
                "permissions",
            ),
            (
                "shell echo only without banner (model + effort)",
                "implement",
                "gemini",
                "gemini",
                "user@host:~/project$ agy --model gemini-4-neon-high --dangerously-skip-permissions\n",
                "banner model not detected",
            ),
            (
                "shell echo only without banner (model only, no effort)",
                "model_only",
                "gemini",
                "gemini",
                "user@host:~/project$ agy --model gemini-4-neon --dangerously-skip-permissions\n",
                "banner model not detected",
            ),
            (
                "agy footer without idle composer (unknown modal)",
                "implement",
                "gemini",
                "gemini",
                "Some unexpected onboarding screen\nGemini 4 Neon · high\n",
                "composer",
            ),
        ]
        for label, role, family, kind, bad_capture, keyword in bad_cases:
            with self.subTest(case=label):
                code, _out, err, posts = self._run_launch(
                    role=role,
                    family=family,
                    agent_kind=kind,
                    captures=["$ ", bad_capture, bad_capture],
                    polls=[
                        {
                            "states": {"del_p2:0.0": {"state": "idle", "idle_seconds": 35.0}},
                            "sessions": [{"target": "del_p2:0.0", "agent_kind": kind}],
                        }
                    ],
                )
                self.assertNotEqual(code, 0, f"{label} should fail gate")
                self.assertIn(keyword, err.lower(), f"{label}: err={err!r}")
                # Brief must NEVER be pasted when any gate fails
                self.assertLessEqual(len(posts), 1, f"{label}: brief must not be pasted")

    def test_brief_delivery_refuses_when_pane_never_changes_after_paste(self):
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            captures=["$ ", AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK],
        )
        self.assertEqual(code, 3)
        self.assertIn("brief", err.lower())
        self.assertEqual(len(posts), 2)

    def test_brief_delivery_refuses_without_real_working_signal(self):
        # Finding 6968: stuck Claude chip, stuck Codex chip, and inline unsent text
        # must NEVER return 0 ("working: confirmed")!
        cases = [
            (
                "claude stuck paste chip",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                CLAUDE_BANNER_OK.replace("❯ ", "❯ [Pasted text #1 +40 lines]"),
            ),
            (
                "codex stuck paste chip",
                "codex",
                "codex",
                CODEX_BANNER_OK,
                CODEX_BANNER_OK.replace("› Ask Codex to do anything", "› [Pasted Content 2345 chars]"),
            ),
            (
                "claude inline unsubmitted text",
                "claude",
                "claude",
                CLAUDE_BANNER_OK,
                CLAUDE_BANNER_OK.replace("❯ ", "❯ # Brief\nLine 1\nLine 2"),
            ),
            (
                "agy inline unsubmitted text",
                "gemini",
                "gemini",
                AGY_BANNER_OK,
                AGY_BANNER_OK.replace("> ", "> # Brief\n  Line 1"),
            ),
        ]
        for label, family, kind, banner, unsubmitted in cases:
            with self.subTest(case=label):
                code, out, err, posts = self._run_launch(
                    family=family,
                    agent_kind=kind,
                    captures=["$ ", banner, banner, banner, unsubmitted, unsubmitted, unsubmitted, unsubmitted, unsubmitted, unsubmitted],
                )
                self.assertNotEqual(code, 0, f"{label} falsely reported working; out={out!r}")
                self.assertNotIn("working: confirmed", out)
                self.assertIn("working", err.lower())
                self.assertEqual(len(posts), 2)

    def test_brief_delivery_refuses_when_agent_exits_to_shell(self):
        polls = [
            {"states": {"del_p2:0.0": {"state": "running", "idle_seconds": 2.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}]},
            {"states": {"del_p2:0.0": {"state": "running", "idle_seconds": 4.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}]},
            {"states": {"del_p2:0.0": {"state": "shell", "idle_seconds": 0.0}},
             "sessions": [{"target": "del_p2:0.0", "agent_kind": "shell"}]},
        ]
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            captures=["$ ", AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK, "user@host$ "],
            polls=polls,
        )
        self.assertEqual(code, 1)
        self.assertIn("shell", err.lower())
        self.assertEqual(len(posts), 2)

    def test_brief_delivery_refuses_when_blocking_prompt_appears_after_paste(self):
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            captures=["$ ", AGY_BANNER_OK, AGY_BANNER_OK, AGY_BANNER_OK, AGY_DATA_WIZARD],
        )
        self.assertEqual(code, 10)
        self.assertIn("prompt", err.lower())
        self.assertEqual(len(posts), 2)

    def test_timeout_while_loading_banner_refuses_with_exit_75(self):
        # Content keeps changing (loading spinner) so it never settles before timeout
        spinner = [f"Loading {i}..." for i in range(1, 20)]
        code, _out, err, posts = self._run_launch(
            family="gemini",
            agent_kind="gemini",
            captures=["$ ", *spinner],
            timeout=6.0,
        )
        self.assertEqual(code, 75)
        self.assertIn("timed out", err.lower())
        self.assertEqual(len(posts), 1)

    def test_trust_cwd_without_brief_accepts_trust_and_gates_without_pasting(self):
        clock = _FakeClock()
        http_calls = []
        captures = iter(["$ ", AGY_TRUST, AGY_TRUST, AGY_TRUST, AGY_BANNER_OK, AGY_BANNER_OK])

        def fake_post(path, payload=None):
            http_calls.append(("POST", path, payload))
            if path == "/terminal/launch":
                return {
                    "session": "del_p2",
                    "target": "del_p2:0.0",
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
                return {"ok": True, "content": next(captures, AGY_BANNER_OK)}
            if path == "/poll":
                return {
                    "states": {"del_p2:0.0": {"state": "running", "idle_seconds": 2.0}},
                    "sessions": [{"target": "del_p2:0.0", "agent_kind": "gemini"}],
                }
            raise AssertionError(f"unexpected GET {path}")

        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=fake_post), \
                mock.patch.object(session.http, "get", side_effect=fake_get), \
                mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                mock.patch("cli.launch_gate.http.post", side_effect=fake_post), \
                mock.patch("cli.launch_gate.http.get", side_effect=fake_get), \
                mock.patch("cli.launch_gate.time.monotonic", side_effect=clock.monotonic), \
                mock.patch("cli.launch_gate.time.sleep", side_effect=clock.sleep), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8), \
                mock.patch.dict("os.environ", {"ASSIST_NO_HINTS": "0"}), \
                redirect_stdout(out), redirect_stderr(err):
            code = session.launch(
                "del_p2",
                role="implement",
                family="gemini",
                roster_path=self.roster,
                trust_cwd=True,
                timeout=30.0,
            )

        self.assertEqual(code, 0, f"err={err.getvalue()!r}")
        type_posts = [c[2] for c in http_calls if c[0] == "POST" and c[1] == "/type"]
        self.assertEqual(len(type_posts), 2)  # roster cmd + bare Enter for trust; no brief
        self.assertEqual(type_posts[1]["text"], "")
        self.assertIn("assist send del_p2 --file BRIEF.md --enter --wait", err.getvalue())
        self.assertEqual([c for c in http_calls if "/autoyes" in c[1]], [])

    def test_parse_roster_command_variants_and_unsupported_refusal(self):
        from cli import launch_gate

        exp_agy_sep = launch_gate.parse_roster_command(
            "agy --model=gemini-4-neon --effort xhigh --dangerously-skip-permissions"
        )
        self.assertEqual((exp_agy_sep.transport, exp_agy_sep.model, exp_agy_sep.effort, exp_agy_sep.has_full_perms_flag),
                         ("agy", "gemini-4-neon", "xhigh", True))

        exp_agy_claude = launch_gate.parse_roster_command(
            "agy --model claude-opus-5-5-high --dangerously-skip-permissions"
        )
        res = launch_gate.evaluate_banner_gate(
            exp_agy_claude,
            f"Antigravity CLI 1.5.12\nClaude Opus 5.5 (High)\n~/project\n{_RULE}\n> \n{_RULE}\n",
        )
        self.assertTrue(res.ok, res.errors)

        with self.assertRaises(ValueError):
            launch_gate.parse_roster_command("python3 -m http.server")

        # Pin each working-signal branch independently (Finding 6975)
        self.assertTrue(
            launch_gate._has_working_signal(
                CLAUDE_BANNER_OK + "\n  ⏵⏵ bypass permissions on · esc to interrupt · ← for agents\n",
                CLAUDE_BANNER_OK,
            )
        )
        self.assertTrue(
            launch_gate._has_working_signal(
                CLAUDE_BANNER_OK + "\n✢ Dilly-dallying…\n",
                CLAUDE_BANNER_OK,
            )
        )
        self.assertTrue(
            launch_gate._has_working_signal(
                CLAUDE_BANNER_OK + "\n* Determining…\n",
                CLAUDE_BANNER_OK,
            )
        )
        self.assertTrue(
            launch_gate._has_working_signal(
                CLAUDE_BANNER_OK + "\n✻ Sautéed for 1s\n",
                CLAUDE_BANNER_OK,
            )
        )
        self.assertFalse(
            launch_gate._has_working_signal(
                CLAUDE_BANNER_OK + "\n* lowercase markdown bullet...\n",
                CLAUDE_BANNER_OK,
            )
        )


class GatedLaunchRealTmuxTests(unittest.TestCase):
    """End-to-end gated launch over real isolated tmux and real Flask routes."""

    def test_e2e_agy_trust_gate_and_brief_paste_on_isolated_tmux(self):
        from shared import launch_provenance as provenance

        box = TmuxBox(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        prov_home = root / "assist-home"
        prov_home.mkdir(mode=0o700)
        prov_env = mock.patch.dict(
            os.environ,
            {
                "ASSIST_HOME": str(prov_home),
                "ASSIST_LAUNCH_PROVENANCE_ROOT": str(prov_home / provenance.ROOT_NAME),
            },
        )
        prov_env.start()
        self.addCleanup(prov_env.stop)
        provenance.initialize_epoch(
            assist_home=prov_home,
            receipt_path=root / "receipt.json",
            expect_empty=True,
        )
        received_file = root / "received_brief.txt"
        brief_file = root / "BRIEF.md"
        brief_text = "# Phase 2 E2E Brief\nLine two of brief.\n"
        brief_file.write_text(brief_text, encoding="utf-8")

        fake_agy = root / "agy"
        fake_agy.write_text(
            f"""#!{sys.executable}
import sys, time
sys.stdout.write("Accessing workspace:\\n\\n/tmp/demo\\n\\nDo you trust the contents of this project?\\n\\n")
sys.stdout.write("Antigravity CLI requires permission to read, edit, and execute files here.\\n\\n")
sys.stdout.write("> Yes, I trust this folder\\n  No, exit\\n\\n  ↑/↓ Navigate · enter Confirm\\n")
sys.stdout.flush()
sys.stdin.readline()
sys.stdout.write("\\x1b[2J\\x1b[H")
sys.stdout.write("      ▄▀▀▄        Antigravity CLI 1.5.12\\n")
sys.stdout.write("    ▀▀▀▀▀▀▀▀      Gemini 4 Neon (High)\\n")
sys.stdout.write("────────────────────────────────────────────────────────────\\n")
sys.stdout.write("> \\n")
sys.stdout.write("────────────────────────────────────────────────────────────\\n")
sys.stdout.write("? for shortcuts                     Gemini 4 Neon · high\\n")
sys.stdout.flush()
lines = []
while True:
    chunk = sys.stdin.readline()
    if not chunk:
        break
    lines.append(chunk)
    if "Line two of brief." in chunk:
        break
open({str(received_file)!r}, "w", encoding="utf-8").write("".join(lines))
sys.stdout.write("\\n⣾ Working on brief...\\n")
sys.stdout.flush()
time.sleep(30)
""",
            encoding="utf-8",
        )
        fake_agy.chmod(0o755)

        roster_file = root / "roster.toml"
        roster_file.write_text(
            f"[roles.implement]\ngemini = '{fake_agy} --model gemini-4-neon-high --dangerously-skip-permissions'\n",
            encoding="utf-8",
        )

        app = Flask(__name__)
        app.register_blueprint(terminal_bp)
        app.register_blueprint(input_bp)
        app.register_blueprint(poll_bp)
        client = app.test_client()

        def http_post(path, payload=None):
            resp = client.post(path, json=payload)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            return resp.get_json()

        def http_get(path):
            if path.startswith("/api/resume"):
                return {"resume": None}
            resp = client.get(path)
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
            return resp.get_json()

        # 1) Without --trust-cwd: must refuse at trust dialog and NOT create received_file
        err1 = io.StringIO()
        out1 = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=http_post), \
                mock.patch.object(session.http, "get", side_effect=http_get), \
                mock.patch.object(wait_cli.http, "get", side_effect=http_get), \
                mock.patch("cli.launch_gate.http.post", side_effect=http_post), \
                mock.patch("cli.launch_gate.http.get", side_effect=http_get), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch("cli.launch_gate.POLL_INTERVAL", 0.3), \
                mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8 if keys[-1] == "detection_depth" else 120), \
                redirect_stdout(out1), redirect_stderr(err1):
            code1 = session.launch(
                "e2e-notrust",
                cwd=str(root),
                role="implement",
                family="gemini",
                roster_path=roster_file,
                brief_path=str(brief_file),
                trust_cwd=False,
                timeout=15.0,
            )
        self.assertEqual(code1, 10, f"expected 10, got {code1}; err={err1.getvalue()!r}")
        self.assertIn("--trust-cwd", err1.getvalue())
        self.assertFalse(received_file.exists())

        # Retrying gated launch against the intact refused pane must keep refusing (never skip gates)
        err1_retry = io.StringIO()
        out1_retry = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=http_post), \
                mock.patch.object(session.http, "get", side_effect=http_get), \
                mock.patch.object(wait_cli.http, "get", side_effect=http_get), \
                mock.patch("cli.launch_gate.http.post", side_effect=http_post), \
                mock.patch("cli.launch_gate.http.get", side_effect=http_get), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch("cli.launch_gate.POLL_INTERVAL", 0.3), \
                mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8 if keys[-1] == "detection_depth" else 120), \
                redirect_stdout(out1_retry), redirect_stderr(err1_retry):
            code1_retry = session.launch(
                "e2e-notrust",
                cwd=str(root),
                role="implement",
                family="gemini",
                roster_path=roster_file,
                brief_path=str(brief_file),
                trust_cwd=True,
                timeout=15.0,
            )
        self.assertEqual(code1_retry, 2, f"retry on refused pane must exit 2, got {code1_retry}")
        self.assertIn("already exists", err1_retry.getvalue())
        self.assertFalse(received_file.exists())

        # 2) With --trust-cwd: accepts trust, passes banner gate, delivers brief, confirms working
        err2 = io.StringIO()
        out2 = io.StringIO()
        with mock.patch.object(session.http, "post", side_effect=http_post), \
                mock.patch.object(session.http, "get", side_effect=http_get), \
                mock.patch.object(wait_cli.http, "get", side_effect=http_get), \
                mock.patch("cli.launch_gate.http.post", side_effect=http_post), \
                mock.patch("cli.launch_gate.http.get", side_effect=http_get), \
                mock.patch.object(session, "reply_address", return_value=None), \
                mock.patch("cli.launch_gate.POLL_INTERVAL", 0.3), \
                mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8 if keys[-1] == "detection_depth" else 120), \
                redirect_stdout(out2), redirect_stderr(err2):
            code2 = session.launch(
                "e2e-trust",
                cwd=str(root),
                role="implement",
                family="gemini",
                roster_path=roster_file,
                brief_path=str(brief_file),
                trust_cwd=True,
                timeout=15.0,
            )
        self.assertEqual(code2, 0, f"expected 0, got {code2}; err={err2.getvalue()!r}")
        self.assertTrue(received_file.exists())
        got_text = received_file.read_text(encoding="utf-8")
        self.assertIn("# Phase 2 E2E Brief", got_text)
        self.assertIn("Line two of brief.", got_text)


if __name__ == "__main__":
    unittest.main()
