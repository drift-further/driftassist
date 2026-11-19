"""Tests for Phase 2 agy recognition:
1. agy folder-trust dialog reads as asking (never idle 0 in assist wait),
   distinct from agy's first-run Terms/data-sharing wizard and quoted prose.
2. agy banner & status footer expose model and effort in shared/agent_model.py.
3. agy resume lines in shared/resume_line.py (and /poll wiring).
"""

import io
import json
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()

from cli import wait as wait_cli  # noqa: E402
from routes import autoyes, poll  # noqa: E402
from shared import agent_identity, agent_model, resume_line  # noqa: E402
from shared.agent_identity import ProcessObservation  # noqa: E402


ID1 = "11111111-1111-4111-8111-111111111111"
ID2 = "22222222-2222-4222-8222-222222222222"

_RULE = "─" * 60

AGY_TRUST_DIALOG = "\n".join(
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
        "",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_TRUST_WRAPPED_40COL = "\n".join(
    [
        "Accessing workspace:",
        "",
        "/tmp/example-project",
        "",
        "Do you trust the contents of this",
        "project?",
        "",
        "Antigravity CLI requires permission to",
        "read, edit, and execute files here.",
        "",
        "> Yes, I trust this folder",
        "  No, exit",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "Gemini 4 Neon · high",
    ]
)

AGY_TRUST_NO_SELECTED = "\n".join(
    [
        "Accessing workspace:",
        "",
        "/tmp/example-project",
        "",
        "Do you trust the contents of this project?",
        "",
        "Antigravity CLI requires permission to read, edit, and execute files here.",
        "",
        "  Yes, I trust this folder",
        "> No, exit",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "                                                        Gemini 4 Neon · high",
    ]
)

# Real agy 1.5.12 24-row Terms of Service & Data Use modal with bottom status row.
AGY_DATA_WIZARD = "\n".join(
    [
        "Terms of Service & Data Use",
        "",
        "Before you continue, please review Google's terms and data collection settings.",
        *[f"  Terms paragraph line {i}" for i in range(1, 14)],
        "",
        "> [x] Yes, I agree to help improve Antigravity CLI by allowing Google to collect",
        "      and use my Interactions data",
        "  [ ] No, do not collect my Interactions data",
        "",
        "  ↑/↓ Navigate · enter Toggle",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_COLOR_SCHEME_MODAL = "\n".join(
    [
        "Choose your color scheme:",
        "",
        "> Dark",
        "  Light",
        "  System",
        "",
        "  ↑/↓ Navigate · enter Confirm",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_SETTINGS_ERROR_MODAL = "\n".join(
    [
        "⚠ Settings Error",
        "",
        "Failed to parse ~/.gemini/antigravity-cli/settings.json",
        "",
        "> Continue with defaults",
        "  Exit",
        "",
        "  enter Confirm · esc Exit",
        "                                                        Gemini 4 Neon · high",
    ]
)

AGY_IDLE_COMPOSER = "\n".join(
    [
        _RULE,
        "> ",
        _RULE,
        "? for shortcuts                                         Gemini 4 Neon · high",
    ]
)

AGY_BANNER_ONLY = "\n".join(
    [
        "      ▄▀▀▄        Antigravity CLI 1.5.12",
        "     ▀▀▀▀▀▀       user@example.com (Google AI Ultra)",
        "    ▀▀▀▀▀▀▀▀      Gemini 4 Neon (High)",
        "   ▄▀▀    ▀▀▄     ~/source/project",
        "  ▄▀▀      ▀▀▄",
    ]
)

AGY_READY_PANE = "\n".join(
    [
        "user@host:~/project$ agy --model gemini-4-neon-high --dangerously-skip-permissions",
        "",
        AGY_BANNER_ONLY,
        "",
        AGY_IDLE_COMPOSER,
    ]
)


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class AgyTrustDetectionTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("shared.state.get_setting", side_effect=lambda *keys: 8)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agy_trust_dialog_is_never_auto_answered_but_always_asking(self):
        # Finding 6967: folder trust is a trust decision — Auto-Yes must NEVER answer it!
        for label, tail in (
            ("wide-yes", AGY_TRUST_DIALOG),
            ("40col-wrapped-yes", AGY_TRUST_WRAPPED_40COL),
        ):
            with self.subTest(case=label):
                self.assertIsNone(autoyes._detect_autoyes_prompt(tail, "gemini"))
                self.assertIsNone(autoyes.detect_answerable_prompt(tail, "gemini", "s:0.0"))
                popup = autoyes.prompt_popup_info(tail, "gemini", autoyes_armed=True)
                self.assertFalse(popup["autoyes"])

                attn = autoyes.observe_attention(tail, "gemini")
                self.assertIsNotNone(attn)
                self.assertEqual(attn["kind"], "menu")
                self.assertIsNone(attn["answer"])
                self.assertIn("Trust", attn["summary"])

    def test_agy_trust_dialog_not_selected_yes_is_not_autoyes_but_is_asking(self):
        self.assertIsNone(autoyes._detect_autoyes_prompt(AGY_TRUST_NO_SELECTED, "gemini"))
        attn = autoyes.observe_attention(AGY_TRUST_NO_SELECTED, "gemini")
        self.assertIsNotNone(attn)
        self.assertIsNone(attn["answer"])
        self.assertEqual(attn["kind"], "menu")

    def test_agy_startup_modals_and_24row_wizard_are_never_autoyes_and_always_asking(self):
        # Finding 6970: real 24-row Terms wizard, Color Scheme, Settings Error must be asking
        for label, tail, want_kind in (
            ("24-row-data-wizard", AGY_DATA_WIZARD, "question"),
            ("color-scheme", AGY_COLOR_SCHEME_MODAL, "menu"),
            ("settings-error", AGY_SETTINGS_ERROR_MODAL, "menu"),
        ):
            with self.subTest(case=label):
                self.assertIsNone(autoyes._detect_autoyes_prompt(tail, "gemini"))
                attn = autoyes.observe_attention(tail, "gemini")
                self.assertIsNotNone(attn, f"{label} must read as asking")
                self.assertIsNone(attn["answer"])
                self.assertEqual(attn["kind"], want_kind)

    def test_agy_trust_dialog_quoted_above_idle_composer_is_silent(self):
        for tail in (AGY_TRUST_DIALOG, AGY_DATA_WIZARD, AGY_COLOR_SCHEME_MODAL, AGY_SETTINGS_ERROR_MODAL):
            quoted = tail + "\n\n" + AGY_IDLE_COMPOSER
            self.assertIsNone(autoyes._detect_autoyes_prompt(quoted, "gemini"))
            self.assertIsNone(autoyes.observe_attention(quoted, "gemini"))

    def test_agy_trust_prose_or_wrong_agent_kind_is_silent(self):
        prose = "The tool asks: Do you trust the contents of this project?\n" + AGY_IDLE_COMPOSER
        self.assertIsNone(autoyes._detect_autoyes_prompt(prose, "gemini"))
        for kind in ("claude", "codex", "cursor", "opencode", "shell"):
            with self.subTest(agent_kind=kind):
                self.assertIsNone(autoyes._detect_autoyes_prompt(AGY_TRUST_DIALOG, kind))
                self.assertIsNone(autoyes.observe_attention(AGY_TRUST_DIALOG, kind))

    def test_assist_wait_exits_10_on_agy_trust_dialog_and_modals_never_idle_0(self):
        for label, tail, expected_prefix in (
            ("trust-yes", AGY_TRUST_DIALOG, "menu:"),
            ("trust-40col", AGY_TRUST_WRAPPED_40COL, "menu:"),
            ("trust-no", AGY_TRUST_NO_SELECTED, "menu:"),
            ("24-row-data-wizard", AGY_DATA_WIZARD, "question:"),
            ("color-scheme", AGY_COLOR_SCHEME_MODAL, "menu:"),
            ("settings-error", AGY_SETTINGS_ERROR_MODAL, "menu:"),
        ):
            with self.subTest(case=label):
                clock = _FakeClock()

                def fake_get(path, _tail=tail):
                    if path == "/poll":
                        return {
                            "states": {"del_agy:0.0": {"state": "idle", "idle_seconds": 45.0}},
                            "sessions": [{"target": "del_agy:0.0", "agent_kind": "gemini"}],
                        }
                    if path.startswith("/terminal/capture"):
                        return {"ok": True, "content": _tail}
                    if path.startswith("/api/resume"):
                        return {"resume": None}
                    raise AssertionError(f"unexpected GET {path}")

                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(wait_cli.http, "get", side_effect=fake_get), \
                        mock.patch("cli.session.http.get", side_effect=fake_get), \
                        mock.patch.object(wait_cli.time, "monotonic", side_effect=clock.monotonic), \
                        mock.patch.object(wait_cli.time, "sleep", side_effect=clock.sleep), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    code = wait_cli.run_wait("del_agy", "del_agy:0.0", 30.0, baseline_hash=None)

                self.assertEqual(code, 10, f"{label} must exit 10, got {code}; out={stdout.getvalue()!r}")
                self.assertIn(expected_prefix, stdout.getvalue())

    def test_refine_with_content_recognises_agy_pane(self):
        self.assertEqual(agent_identity.refine_with_content(None, AGY_TRUST_DIALOG), "gemini")
        self.assertEqual(agent_identity.refine_with_content(None, AGY_READY_PANE), "gemini")


class AgyModelBannerTests(unittest.TestCase):
    def test_agy_banner_parses_model_and_effort(self):
        cases = [
            (AGY_BANNER_ONLY, ("gemini 4 neon", "high")),
            (
                "Antigravity CLI 1.5.12\nGemini 4 Argon (Medium)\n~/project",
                ("gemini 4 argon", "medium"),
            ),
            (
                "Antigravity CLI 1.5.12\nGemini 4 Carbon (High)\n~/project",
                ("gemini 4 carbon", "high"),
            ),
            (
                "Antigravity CLI 1.5.12\nGemini 4 Hydrogen (XHigh)\n~/project",
                ("gemini 4 hydrogen", "xhigh"),
            ),
            (
                "Antigravity CLI 1.5.12\nGemini 3.8 Flash (Low)\n~/project",
                ("gemini 3.8 flash", "low"),
            ),
            (
                "Antigravity CLI 1.5.12\nClaude Opus 5.5 (High)\n~/project",
                ("claude opus 5.5", "high"),
            ),
            (
                "Antigravity CLI 1.5.12\nGPT-OSS 120B (Medium)\n~/project",
                ("gpt-oss 120b", "medium"),
            ),
        ]
        for tail, want in cases:
            with self.subTest(want=want):
                self.assertEqual(agent_model.resolve("gemini", tail), want)

    def test_agy_footer_parses_and_outranks_older_banner(self):
        tail = "\n".join(
            [
                "Antigravity CLI 1.5.12",
                "Gemini 4 Argon (Medium)",
                "switched model",
                _RULE,
                "> ",
                _RULE,
                "\x1b[2mesc to cancel\x1b[0m                                \x1b[36mGemini 4 Neon · high\x1b[0m",
            ]
        )
        self.assertEqual(agent_model.resolve("gemini", tail), ("gemini 4 neon", "high"))

    def test_shell_command_echo_alone_does_not_parse_as_agy_banner(self):
        echo = "user@host:~/project$ agy --model gemini-4-neon-high --dangerously-skip-permissions\n"
        self.assertEqual(agent_model.resolve("gemini", echo), (None, None))

    def test_claude_banner_and_footer_expose_effort(self):
        banner = (
            " ▐▛███▛█   Claude Code v2.1.310\n"
            "▝▜██████▀  Opus 5.5 with high effort · Claude Max\n"
            " ▝▝   ▝▝   /tmp/project\n"
        )
        self.assertEqual(agent_model.resolve("claude", banner), ("opus 5.5", "high"))

        footer = (
            "  ░░░░░░░░░░ 0.0% (0k/800k) │ 0m                    ● medium · /effort\n"
            "  project ⌀ │ ✎ 0 │ Sonnet 5.5\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        self.assertEqual(agent_model.resolve("claude", footer), ("sonnet 5.5", "medium"))

    def test_codex_case_insensitive_footer_and_full_model(self):
        footer = "  GPT-6.1-Sol medium · /tmp/x · GPT-6.1-Sol · medium · Context 97% left\n"
        self.assertEqual(agent_model.resolve("codex", footer), ("sol", "medium"))
        self.assertEqual(agent_model.resolve_full("codex", footer), ("gpt-6.1-sol", "medium"))

    def test_codex_wrapped_multi_segment_status_line_preserves_effort(self):
        # Finding 6972: wrapped status bar where · GPT-6.1-Sol · sits above medium · Context 100% left
        wrapped = (
            "› Ask Codex to do anything\n"
            "  GPT-6.1-Sol medium · /home/user/very/long/project/worktree/path\n"
            "  · GPT-6.1-Sol ·\n"
            "  medium · Context 100% left\n"
        )
        self.assertEqual(agent_model.resolve("codex", wrapped), ("sol", "medium"))
        self.assertEqual(agent_model.resolve_full("codex", wrapped), ("gpt-6.1-sol", "medium"))

    def test_claude_echo_with_model_id_does_not_parse_as_banner(self):
        echo = "user@host:~/project$ claude --model claude-opus-5-5 --effort high --dangerously-skip-permissions\n"
        self.assertEqual(agent_model.resolve("claude", echo), (None, None))


class AgyResumeLineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / "proc"
        self.home = self.root / "home"
        self.cwd = self.root / "my project"
        self.cwd.mkdir()
        self.r = resume_line.Resolver(self.proc, self.home)
        self.agents = []

    def agent(self, pid=10, kind="gemini", argv=None, start="123"):
        p = self.proc / str(pid)
        (p / "fd").mkdir(parents=True, exist_ok=True)
        (p / "stat").write_text(f"{pid} (agy) S " + " ".join(["0"] * 18 + [start]))
        full_argv = argv or ["agy", "--model", "gemini-4-neon-high", "--dangerously-skip-permissions"]
        (p / "cmdline").write_bytes(("\0".join(full_argv) + "\0").encode())
        if not (p / "cwd").exists():
            (p / "cwd").symlink_to(self.cwd)
        a = dict(kind=kind, pid=pid, start=start, argv=full_argv, cwd=str(self.cwd))
        self.agents.append(a)
        return a

    def conv_db(self, pid, ident=ID1, with_wal=True):
        base = self.home / ".gemini/antigravity-cli/conversations"
        base.mkdir(parents=True, exist_ok=True)
        db = base / f"{ident}.db"
        db.write_bytes(b"sqlite")
        fd_dir = self.proc / str(pid) / "fd"
        next_fd = lambda: str(len(list(fd_dir.iterdir())) + 3)
        (fd_dir / next_fd()).symlink_to(db)
        if with_wal:
            wal = base / f"{ident}.db-wal"
            wal.write_bytes(b"wal")
            (fd_dir / next_fd()).symlink_to(wal)
        return db

    def presence_lock(self, pid, ident=ID1):
        base = self.home / ".gemini/antigravity-cli/presence"
        base.mkdir(parents=True, exist_ok=True)
        lock = base / f"{ident}.lock"
        lock.write_text("1")
        fd_dir = self.proc / str(pid) / "fd"
        fd = fd_dir / str(len(list(fd_dir.iterdir())) + 3)
        fd.symlink_to(lock)
        return lock

    def observe(self, agents=None):
        agents = self.agents if agents is None else agents
        return ProcessObservation(True, [], agents[0] if agents else None, agents, False, "root1")

    def test_agy_fd_ownership_builds_verified_conversation_resume_line(self):
        self.agent(
            argv=[
                "agy",
                "--model",
                "gemini-4-neon-high",
                "--dangerously-skip-permissions",
                "--settings",
                "secret.json",
                "-p",
                "ignored prompt",
            ]
        )
        self.conv_db(10, ID1)
        record, reason = self.r.resolve("%1", "del_agy", self.observe(), now=100)
        self.assertIsNone(reason)
        self.assertIsNotNone(record)
        self.assertEqual(record["agent"], "gemini")
        self.assertEqual(record["id"], ID1)
        self.assertTrue(record["verified"])
        self.assertTrue(record["live"])
        self.assertEqual(record["source"], "fd")
        self.assertEqual(
            shlex.split(record["command"]),
            ["agy", "--conversation", ID1, "--model", "gemini-4-neon-high", "--dangerously-skip-permissions"],
        )
        self.assertIn(shlex.quote(str(self.cwd)), record["line"])

    def test_agy_no_fd_returns_no_session_yet_and_generation_change_unowned(self):
        self.agent()
        self.assertEqual(self.r.resolve("%1", "del_agy", self.observe(), now=100), (None, "no session yet"))
        self.conv_db(10, ID1)
        (self.proc / "10/stat").write_text("10 (agy) S " + " ".join(["0"] * 18 + ["999"]))
        self.assertEqual(
            self.r.resolve("%1", "del_agy", self.observe(), now=101, fresh=True),
            (None, "unowned"),
        )

    def test_agy_multiple_dbs_disambiguation_and_ambiguity(self):
        self.agent(argv=["agy", "--conversation", ID2, "--model", "gemini-4-neon-high"])
        self.conv_db(10, ID1)
        self.conv_db(10, ID2)
        record, reason = self.r.resolve("%1", "del_agy", self.observe(), now=100)
        self.assertIsNone(reason)
        self.assertEqual(record["id"], ID2)
        # --conversation must not be duplicated in the emitted command
        self.assertEqual(
            shlex.split(record["command"]),
            ["agy", "--conversation", ID2, "--model", "gemini-4-neon-high"],
        )

        # Without --conversation in argv, two dbs are ambiguous unless one presence lock narrows it
        (self.proc / "10/cmdline").write_bytes(b"agy\0--model\0gemini-4-neon-high\0")
        self.assertEqual(
            self.r.resolve("%1", "del_agy", self.observe(), now=101, fresh=True),
            (None, "ambiguous"),
        )
        self.presence_lock(10, ID2)
        record, reason = self.r.resolve("%1", "del_agy", self.observe(), now=102, fresh=True)
        self.assertIsNone(reason)
        self.assertEqual(record["id"], ID2)

    def test_agy_presence_lock_never_breaks_mixed_kind_or_cross_pid_ambiguity(self):
        # Finding 6973: presence_locks must not pick agy over claude, or cross PIDs
        self.agent(pid=10, kind="gemini", argv=["agy", "--model", "gemini-4-neon-high"])
        self.conv_db(10, ID1)
        self.presence_lock(10, ID1)

        # Case 1: mixed kind (claude + agy) -> must stay ambiguous
        self.agent(pid=11, kind="claude", argv=["claude", "--model", "opus"])
        csess = self.home / ".claude/sessions/11.json"
        csess.parent.mkdir(parents=True, exist_ok=True)
        csess.write_text(json.dumps(dict(procStart="123", tmux="%1", sessionId=ID2)))
        self.assertEqual(
            self.r.resolve("%1", "del_mix", self.observe(), now=100, fresh=True),
            (None, "ambiguous"),
        )

        # Case 2: two distinct agy PIDs, one holding a lock matching the other's db -> ambiguous
        self.agents.clear()
        self.agent(pid=20, kind="gemini", argv=["agy", "--model", "gemini-4-neon-high"])
        self.agent(pid=21, kind="gemini", argv=["agy", "--model", "gemini-4-neon-high"])
        self.conv_db(20, ID1)
        self.conv_db(21, ID2)
        self.presence_lock(21, ID1)
        self.assertEqual(
            self.r.resolve("%2", "del_two_agy", self.observe(), now=101, fresh=True),
            (None, "ambiguous"),
        )

    def test_agy_ignores_foreign_db_and_non_agy_gemini_is_unsupported(self):
        self.agent()
        foreign = self.root / f"{ID1}.db"
        foreign.write_bytes(b"evil")
        (self.proc / "10/fd/9").symlink_to(foreign)
        summary = self.home / ".gemini/antigravity-cli/conversation_summaries.db"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_bytes(b"summary")
        (self.proc / "10/fd/10").symlink_to(summary)
        self.assertEqual(self.r.resolve("%1", "del_agy", self.observe(), now=100), (None, "no session yet"))

        self.agents.clear()
        self.agent(pid=20, kind="gemini", argv=["gemini", "--model", "flash"])
        self.assertEqual(
            self.r.resolve("%2", "del_other", self.observe(), now=100),
            (None, "unsupported"),
        )

    def test_agy_farewell_is_unverified_resume_record(self):
        tail = f"Resume with -c (or command below):\nagy --conversation={ID1}\n"
        record, reason = self.r.resolve(
            "%1", "del_agy", self.observe(agents=[]), now=200, tail=tail
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(record)
        self.assertEqual(record["agent"], "gemini")
        self.assertEqual(record["id"], ID1)
        self.assertEqual(record["command"], f"agy --conversation {ID1}")
        self.assertFalse(record["verified"])
        self.assertFalse(record["live"])

    def test_poll_routes_gemini_to_resume_and_treats_agy_as_agent_command(self):
        source = (Path(__file__).resolve().parents[1] / "routes" / "poll.py").read_text(encoding="utf-8")
        self.assertIn('"agy"', source)
        self.assertNotIn('if kind in ("cursor", "opencode", "gemini")', source)


if __name__ == "__main__":
    unittest.main()
