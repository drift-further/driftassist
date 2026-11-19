"""Tests for Phase 3 part (b): structured roster, bans, quota groups, judge rules,
mutators (atomic write + timestamped .bak), doctor, and flat->structured migration.

Never touches ~/.config/drift-assist/roster.toml — any test using its content
operates exclusively on an isolated copy inside a TemporaryDirectory.
"""

import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()

from cli import roster, session  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "roster.example.toml"

# Self-contained flat v1 fixture mirroring a real multi-role roster with inline comments,
# policy bans in the top-level note, and Carbon signoff — never reads $HOME.
FLAT_V1_FIXTURE = """\
updated = "2026-11-17"
note = "Ask which family implements; the judge is always the competing family. No Fable panes after 2026-10-12. NEVER Luna, NEVER Haiku: Claude legs run on Opus."

[roles.plan]
claude = 'claude --model opus --effort high --dangerously-skip-permissions'   # was fable high; no more Fable panes
codex = 'codex -m gpt-6-astra -c model_reasoning_effort="high" --yolo'   # architecture / novel blockers
gemini = 'agy --model gemini-4-argon-medium --dangerously-skip-permissions'   # physics, writing

[roles.implement]
claude = 'claude --model sonnet --effort high --dangerously-skip-permissions'   # Sonnet is the worker
codex = 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" --yolo'   # daily coding: Sol medium/high
gemini = 'agy --model gemini-4-neon-high --dangerously-skip-permissions'   # Neon for coding

[roles.judge]
claude = 'claude --model opus --effort high --dangerously-skip-permissions'   # default phase judge
codex = 'codex -m gpt-6.1-sol -c model_reasoning_effort="high" --yolo'   # review: Sol high/xhigh
gemini = 'agy --model gemini-4-hydrogen-high --dangerously-skip-permissions'   # Hydrogen high reviews

[roles.signoff]
codex = 'codex -m gpt-6-astra -c model_reasoning_effort="xhigh" --yolo'   # final pre-merge sign-off
gemini = 'agy --model gemini-4-carbon-high --dangerously-skip-permissions'   # major-milestone reviews
"""


def _load_bin_assist():
    loader = importlib.machinery.SourceFileLoader("bin_assist_mod_p3b", str(ROOT / "bin" / "assist"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"ASSIST_VENV_REEXEC": str(os.getpid())}):
        loader.exec_module(mod)
    return mod


STRUCTURED_ROSTER = """\
schema_version = 2
updated = "2026-11-18"
note = "Structured test roster."

[judge_rules]
require_different_family = true
default_per_phase = "judge.claude"
milestone_role = "signoff"

[[judge_rules.extra_judges]]
author_family = "gemini"
judge = "signoff.gemini"
reason = "Carbon runs in parallel with Opus on Gemini-built major milestones."

[quota_groups.claude-max]
status = "ok"
check_cmd = "/usage"
notes = "Primary Claude bucket"
updated = "2026-11-18"
source = "test:2026-11-18"

[quota_groups.chatgpt-pro]
status = "low"
check_cmd = "/status"
notes = "Watch 5h window"
updated = "2026-11-18"
source = "test:2026-11-18"

[quota_groups.agy-gemini]
status = "ok"
check_cmd = "/quota"
notes = ""
updated = "2026-11-18"
source = "test:2026-11-18"

[quota_groups.agy-carbon]
status = "ok"
check_cmd = "/quota"
notes = "Separate carbon bucket"
updated = "2026-11-18"
source = "test:2026-11-18"

[[bans]]
pattern = "gpt-*-luna"
mode = "hard"
reason = "Never use Luna"
updated = "2026-11-03"
source = "policy"

[[bans]]
pattern = "*haiku*"
mode = "hard"
reason = "Never use Haiku"
updated = "2026-11-03"
source = "policy"

[[bans]]
pattern = "*fable*"
mode = "ask_daniel"
reason = "No Fable panes without explicit approval"
updated = "2026-10-12"
source = "policy"

[roles.implement.claude]
command = "claude --model sonnet --effort high --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "claude-max"
status = "default"
approval = "none"
notes = "Sonnet is the worker"
updated = "2026-11-18"
source = "policy"

[roles.implement.codex]
command = 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" --yolo'
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
status = "default"
approval = "none"
notes = "Daily coding"
updated = "2026-11-18"
source = "policy"

[roles.implement.gemini]
command = "agy --model gemini-4-neon-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-gemini"
status = "default"
approval = "none"
notes = "Neon for coding"
updated = "2026-11-18"
source = "policy"

[roles.judge.claude]
command = "claude --model opus --effort high --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "claude-max"
status = "default"
approval = "none"
notes = "Default phase judge"
updated = "2026-11-18"
source = "policy"

[roles.judge.codex]
command = 'codex -m gpt-6.1-sol -c model_reasoning_effort="high" --yolo'
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
status = "default"
approval = "none"
notes = "Sol high review"
updated = "2026-11-18"
source = "policy"

[roles.judge.gemini]
command = "agy --model gemini-4-hydrogen-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-gemini"
status = "option"
approval = "none"
notes = "Hydrogen sharp reviews"
updated = "2026-11-18"
source = "policy"

[roles.signoff.gemini]
command = "agy --model gemini-4-carbon-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-carbon"
status = "default"
approval = "none"
allows_judging_families = ["gemini"]
paired_with = ["judge.claude"]
notes = "Major milestones; parallel with Opus even on Gemini-built work."
updated = "2026-11-18"
source = "policy"

[roles.banned_test.hard_luna]
command = 'codex -m gpt-5.6-luna -c model_reasoning_effort="high" --yolo'
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
status = "emergency"
approval = "none"
notes = "Fixture for hard ban test"
updated = "2026-11-18"
source = "test"

[roles.banned_test.ask_fable]
command = "claude --model fable --effort high --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "claude-max"
status = "trial"
approval = "none"
notes = "Fixture for ask_daniel ban test"
updated = "2026-11-18"
source = "test"
"""


class StructuredRosterLoadAndQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster_path = self.root / "roster.toml"
        self.roster_path.write_text(STRUCTURED_ROSTER, encoding="utf-8")

    def test_load_structured_roster_and_roundtrip_toml(self):
        loaded = roster.load(self.roster_path)
        self.assertEqual(loaded.schema_version, 2)
        self.assertEqual(loaded.updated, "2026-11-18")
        self.assertEqual(
            loaded.roles["implement"]["gemini"],
            "agy --model gemini-4-neon-high --dangerously-skip-permissions",
        )
        entry = loaded.entries["signoff"]["gemini"]
        self.assertEqual(entry.family, "gemini")
        self.assertEqual(entry.transport, "agy")
        self.assertEqual(entry.quota_group, "agy-carbon")
        self.assertEqual(entry.status, "default")
        self.assertEqual(entry.allows_judging_families, ("gemini",))
        self.assertEqual(entry.paired_with, ("judge.claude",))
        self.assertEqual(loaded.quota_groups["chatgpt-pro"].status, "low")
        self.assertEqual(loaded.quota_groups["chatgpt-pro"].check_cmd, "/status")
        self.assertEqual(len(loaded.bans), 3)
        self.assertTrue(loaded.judge_rules.require_different_family)
        self.assertEqual(loaded.judge_rules.extra_judges[0].judge, "signoff.gemini")

        # Round-trip serialization reproduces identical structured data
        rendered = roster.render_toml(loaded)
        rt_path = self.root / "roundtrip.toml"
        rt_path.write_text(rendered, encoding="utf-8")
        reloaded = roster.load(rt_path)
        self.assertEqual(reloaded, loaded)

    def test_invalid_status_or_ban_mode_is_refused(self):
        bad = self.root / "bad.toml"
        bad.write_text(
            '[roles.implement.claude]\ncommand = "claude --dangerously-skip-permissions"\nstatus = "bogus"\n',
            encoding="utf-8",
        )
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            roster.load(bad)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("status", err.getvalue())

        bad_ban = self.root / "bad_ban.toml"
        bad_ban.write_text(
            '[[bans]]\npattern = "*luna*"\nmode = "soft"\nreason = "nope"\n',
            encoding="utf-8",
        )
        err2 = io.StringIO()
        with redirect_stderr(err2), self.assertRaises(SystemExit) as raised2:
            roster.load(bad_ban)
        self.assertEqual(raised2.exception.code, 2)
        self.assertIn("mode", err2.getvalue())

    def test_roster_role_family_json_includes_judge_rule_and_carbon_extra_judge(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = roster.cli_main(
                ["--role", "implement", "--family", "gemini", "--json"],
                self.roster_path,
            )
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["role"], "implement")
        self.assertEqual(data["entry"]["family"], "gemini")
        self.assertEqual(data["quota"]["id"], "agy-gemini")
        self.assertEqual(data["quota"]["check_cmd"], "/quota")

        jr = data["judge_rule"]
        self.assertEqual(jr["author_family"], "gemini")
        self.assertTrue(jr["require_different_family"])
        self.assertEqual(sorted(jr["eligible_judge_families"]), ["claude", "openai"])
        extra_refs = [ej["ref"] for ej in jr["extra_judges"]]
        self.assertIn("signoff.gemini", extra_refs)
        carbon = next(ej for ej in jr["extra_judges"] if ej["ref"] == "signoff.gemini")
        self.assertIn("gemini-4-carbon-high", carbon["command"])

        # For Claude author, eligible families are gemini + openai, and no Carbon extra judge
        out2 = io.StringIO()
        with redirect_stdout(out2):
            code2 = roster.cli_main(
                ["--role", "implement", "--family", "claude", "--json"],
                self.roster_path,
            )
        self.assertEqual(code2, 0)
        jr2 = json.loads(out2.getvalue())["judge_rule"]
        self.assertEqual(sorted(jr2["eligible_judge_families"]), ["gemini", "openai"])
        self.assertEqual(jr2["extra_judges"], [])

    def test_family_selector_accepts_both_key_codex_and_family_openai(self):
        loaded = roster.load(self.roster_path)
        by_key = roster.resolve_command(loaded, "implement", family="codex")
        by_family = roster.resolve_command(loaded, "implement", family="openai")
        self.assertEqual(by_key, by_family)
        self.assertIn("gpt-6.1-sol", by_key)

    def test_roster_human_output_shows_perms_notes_quotas_and_bans(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = roster.cli_main([], self.roster_path)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("PERMS", text)
        self.assertIn("FULL (--yolo)", text)
        self.assertIn("FULL (--dangerously-skip-permissions)", text)
        self.assertIn("chatgpt-pro", text)
        self.assertIn("low", text)
        self.assertIn("gpt-*-luna", text)
        self.assertIn("hard", text)
        self.assertIn("ask_daniel", text)
        self.assertIn("Sonnet is the worker", text)


class LaunchBanEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.roster_path = Path(self.tmp.name) / "roster.toml"
        self.roster_path.write_text(STRUCTURED_ROSTER, encoding="utf-8")

    def test_launch_hard_ban_always_refuses_even_with_approved_by_daniel(self):
        for approved in (False, True):
            with self.subTest(approved=approved):
                err = io.StringIO()
                with mock.patch.object(session.http, "post") as post, \
                        redirect_stderr(err), \
                        self.assertRaises(SystemExit) as raised:
                    session.launch(
                        "del_ban",
                        role="banned_test",
                        family="hard_luna",
                        roster_path=self.roster_path,
                        approved_by_daniel=approved,
                    )
                self.assertEqual(raised.exception.code, 2)
                post.assert_not_called()
                self.assertIn("hard", err.getvalue().lower())
                self.assertIn("gpt-*-luna", err.getvalue())

    def test_launch_ask_daniel_ban_refuses_without_flag_and_allows_with_flag(self):
        err = io.StringIO()
        with mock.patch.object(session.http, "post") as post, \
                redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            session.launch(
                "del_fable",
                role="banned_test",
                family="ask_fable",
                roster_path=self.roster_path,
                approved_by_daniel=False,
            )
        self.assertEqual(raised.exception.code, 2)
        post.assert_not_called()
        self.assertIn("--approved-by-daniel", err.getvalue())
        self.assertIn("*fable*", err.getvalue())

        # With --approved-by-daniel, launch proceeds
        launch_resp = {
            "session": "del_fable",
            "target": "del_fable:0.0",
            "venv": None,
            "venv_activated": False,
            "existed": False,
        }
        out = io.StringIO()
        err_ok = io.StringIO()
        with mock.patch.object(session.http, "post", return_value=launch_resp) as post, \
                mock.patch.object(session, "send", return_value=0) as send, \
                redirect_stdout(out), redirect_stderr(err_ok):
            code = session.launch(
                "del_fable",
                role="banned_test",
                family="ask_fable",
                roster_path=self.roster_path,
                approved_by_daniel=True,
            )
        self.assertEqual(code, 0)
        post.assert_called_once()
        send.assert_called_once()

    def test_launch_parser_accepts_approved_by_daniel(self):
        bin_assist = _load_bin_assist()
        parsed = bin_assist.build_parser().parse_args(
            ["launch", "--session", "del_x", "--role", "implement", "--family", "claude", "--approved-by-daniel"]
        )
        self.assertTrue(parsed.approved_by_daniel)

    def test_approved_by_daniel_without_role_is_refused(self):
        err = io.StringIO()
        with mock.patch.object(session.http, "post") as post, \
                redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            session.launch("del_x", approved_by_daniel=True)
        self.assertEqual(raised.exception.code, 2)
        post.assert_not_called()

    def test_ban_bypass_resistance_across_flags_suffixes_aliases_env_and_transports(self):
        """Findings 6977 & 6984: Luna, Haiku, Fable, Opus bans must catch all carrier variants."""
        loaded = roster.load(self.roster_path)
        must_block_luna = [
            'codex -c model="gpt-6.1-luna" --yolo',
            'codex --config model=gpt-6.1-luna --yolo',
            "codex -mgpt-6.1-luna --yolo",
            "codex -m gpt-6.1-luna-mini --yolo",
            "codex -m gpt-6.1-luna-2026-11-01 --yolo",
            "codex -m gpt-5.6-luna-preview --yolo",
            "codex -m luna --yolo",
            "codex -m GPT-6.1-LUNA --yolo",
            "codex -m gpt-6.1-sol -m=gpt-5.6-luna --yolo",
            "OPENAI_MODEL=gpt-5.6-luna codex --yolo",
            "env OPENAI_MODEL=gpt-5.6-luna codex --yolo",
            "/usr/bin/env -i OPENAI_MODEL=gpt-5.6-luna codex --yolo",
            "env -u FOO OPENAI_MODEL=gpt-5.6-luna -- codex --yolo",
            "codex -m gpt-5.6-luna-20261101 --yolo",
            "codex -m gpt-5.6-luna-v2 --yolo",
            "codex -m gpt-5.6-luna-v2.1-20261101 --yolo",
            "agy --model gpt-6.1-luna-high --dangerously-skip-permissions",
            "agy --model gpt-5.6-luna-ultra --dangerously-skip-permissions",
            "agy --model gpt-5.6-luna-minimal --dangerously-skip-permissions",
        ]
        for cmd in must_block_luna:
            with self.subTest(cmd=cmd):
                matched = roster.match_bans(loaded, cmd)
                self.assertTrue(
                    any(b.mode == "hard" for b in matched),
                    f"expected hard Luna ban to block: {cmd}",
                )

        # env wrapper also resolves underlying transport cleanly
        self.assertEqual(
            roster._infer_transport("env OPENAI_MODEL=gpt-6.1-sol codex --yolo"),
            "codex",
        )
        self.assertEqual(
            roster._infer_transport("/usr/bin/env -i ANTHROPIC_MODEL=opus claude --dangerously-skip-permissions"),
            "claude",
        )

        # Non-banned codex/claude commands must NOT false-positive
        for ok_cmd in (
            'codex -m gpt-6.1-sol -c model_reasoning_effort="medium" --yolo',
            "claude --model sonnet --effort high --dangerously-skip-permissions",
            "agy --model gemini-4-neon-high --dangerously-skip-permissions",
        ):
            with self.subTest(ok_cmd=ok_cmd):
                self.assertEqual(roster.match_bans(loaded, ok_cmd), [])

        # Bare 'opus' ban catches Opus-via-agy, claude-opus-4-6, opus[1m], and ANTHROPIC_MODEL=opus
        opus_roster = roster.Roster(
            updated="2026-11-19",
            note="",
            roles={},
            bans=(roster.BanRule(pattern="opus", mode="ask_daniel", reason="expensive"),),
            schema_version=2,
        )
        for cmd in (
            "agy --model claude-opus-4-6-high --dangerously-skip-permissions",
            "claude --model claude-opus-4-6 --dangerously-skip-permissions",
            "claude --model opus[1m] --dangerously-skip-permissions",
            "ANTHROPIC_MODEL=opus claude --dangerously-skip-permissions",
        ):
            with self.subTest(opus_cmd=cmd):
                self.assertTrue(roster.match_bans(opus_roster, cmd), f"missed opus ban: {cmd}")
        self.assertEqual(
            roster.match_bans(opus_roster, "claude --model sonnet --dangerously-skip-permissions"),
            [],
        )

        # Prefix ban 'claude-opus*' catches bare 'claude --model opus'
        prefix_roster = roster.Roster(
            updated="2026-11-19",
            note="",
            roles={},
            bans=(roster.BanRule(pattern="claude-opus*", mode="hard", reason="no opus"),),
            schema_version=2,
        )
        self.assertTrue(
            roster.match_bans(prefix_roster, "claude --model opus --effort high --dangerously-skip-permissions")
        )
        self.assertEqual(
            roster.match_bans(prefix_roster, "claude --model sonnet --effort high --dangerously-skip-permissions"),
            [],
        )

        # Repeated --model uses the LAST flag for family inference
        self.assertEqual(
            roster._infer_family(
                "gemini",
                "agy",
                "agy --model gemini-4-neon-high --model claude-opus-4-6-high --dangerously-skip-permissions",
            ),
            "claude",
        )

    def test_entry_level_approval_ask_daniel_and_ask_karen_enforced_without_bans(self):
        """Findings 6979, 6980, 6988: entry.approval gates launch even when no [[bans]] match."""
        approval_path = Path(self.tmp.name) / "approval.toml"
        approval_path.write_text(
            """\
schema_version = 2
updated = "2026-11-19"
note = ""

[roles.special.daniel_only]
command = "claude --model sonnet --effort high --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "claude-max"
status = "option"
approval = "ask_daniel"

[roles.special.karen_only]
command = 'codex -m gpt-6-astra -c model_reasoning_effort="xhigh" --yolo'
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
status = "option"
approval = "ask_karen"
""",
            encoding="utf-8",
        )
        # ask_daniel refused without flag, allowed with --approved-by-daniel
        err = io.StringIO()
        with mock.patch.object(session.http, "post") as post, \
                redirect_stderr(err), \
                self.assertRaises(SystemExit) as raised:
            session.launch("del_a", role="special", family="daniel_only", roster_path=approval_path)
        self.assertEqual(raised.exception.code, 2)
        post.assert_not_called()
        self.assertIn("--approved-by-daniel", err.getvalue())

        launch_resp = {"session": "del_a", "target": "del_a:0.0", "venv": None, "venv_activated": False, "existed": False}
        with mock.patch.object(session.http, "post", return_value=launch_resp), \
                mock.patch.object(session, "send", return_value=0), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                session.launch(
                    "del_a", role="special", family="daniel_only", roster_path=approval_path, approved_by_daniel=True
                ),
                0,
            )

        # ask_karen refused without --approved-by-karen, allowed with it
        err2 = io.StringIO()
        with mock.patch.object(session.http, "post") as post2, \
                redirect_stderr(err2), \
                self.assertRaises(SystemExit) as raised2:
            session.launch("del_k", role="special", family="karen_only", roster_path=approval_path)
        self.assertEqual(raised2.exception.code, 2)
        post2.assert_not_called()
        self.assertIn("--approved-by-karen", err2.getvalue())

        with mock.patch.object(session.http, "post", return_value=launch_resp), \
                mock.patch.object(session, "send", return_value=0), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(
                session.launch(
                    "del_k", role="special", family="karen_only", roster_path=approval_path, approved_by_karen=True
                ),
                0,
            )


class RosterMutatorsDoctorAndMigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.roster_path = self.root / "roster.toml"
        self.roster_path.write_text(STRUCTURED_ROSTER, encoding="utf-8")

    def _backups(self):
        return sorted(self.root.glob("roster.toml.*.bak"))

    def test_mutators_create_timestamped_backups_and_persist_atomically(self):
        original_bytes = self.roster_path.read_bytes()

        # 1. roster set
        out = io.StringIO()
        with redirect_stdout(out):
            code = roster.cli_main(
                [
                    "set",
                    "implement.opus_agy",
                    "--cmd",
                    "agy --model claude-opus-5-5-high --dangerously-skip-permissions",
                    "--family",
                    "claude",
                    "--transport",
                    "agy",
                    "--quota",
                    "agy-gemini",
                    "--status",
                    "emergency",
                    "--note",
                    "Opus via agy fallback",
                    "--source",
                    "test:2026-11-19",
                ],
                self.roster_path,
            )
        self.assertEqual(code, 0)
        baks = self._backups()
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_bytes(), original_bytes)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

        loaded = roster.load(self.roster_path)
        entry = loaded.entries["implement"]["opus_agy"]
        self.assertEqual(entry.family, "claude")
        self.assertEqual(entry.transport, "agy")
        self.assertEqual(entry.status, "emergency")
        self.assertEqual(entry.notes, "Opus via agy fallback")

        # 2. roster note (distinct backup even within same second)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            code2 = roster.cli_main(
                ["note", "implement.opus_agy", "Updated emergency note", "--source", "test:note"],
                self.roster_path,
            )
        self.assertEqual(code2, 0)
        self.assertEqual(len(self._backups()), 2)
        self.assertEqual(
            roster.load(self.roster_path).entries["implement"]["opus_agy"].notes,
            "Updated emergency note",
        )

        # 3. roster quota
        out3 = io.StringIO()
        with redirect_stdout(out3):
            code3 = roster.cli_main(
                [
                    "quota",
                    "agy-claude-gpt",
                    "--status",
                    "low",
                    "--check-cmd",
                    "/quota",
                    "--note",
                    "Running tight",
                    "--source",
                    "test:quota",
                ],
                self.roster_path,
            )
        self.assertEqual(code3, 0)
        self.assertEqual(len(self._backups()), 3)
        qg = roster.load(self.roster_path).quota_groups["agy-claude-gpt"]
        self.assertEqual(qg.status, "low")
        self.assertEqual(qg.check_cmd, "/quota")
        self.assertEqual(qg.notes, "Running tight")

        # 4. roster ban
        out4 = io.StringIO()
        with redirect_stdout(out4):
            code4 = roster.cli_main(
                ["ban", "*grok*", "--mode", "hard", "--reason", "Do not use grok", "--source", "test:ban"],
                self.roster_path,
            )
        self.assertEqual(code4, 0)
        self.assertEqual(len(self._backups()), 4)
        patterns = {b.pattern: b for b in roster.load(self.roster_path).bans}
        self.assertIn("*grok*", patterns)
        self.assertEqual(patterns["*grok*"].mode, "hard")

        # 5. roster set refuses hard-banned command and writes no backup
        err5 = io.StringIO()
        with redirect_stderr(err5), self.assertRaises(SystemExit) as raised5:
            roster.cli_main(
                ["set", "implement.bad", "--cmd", "claude --model haiku --dangerously-skip-permissions"],
                self.roster_path,
            )
        self.assertEqual(raised5.exception.code, 2)
        self.assertEqual(len(self._backups()), 4)

    def test_doctor_flags_missing_perms_hard_banned_entry_and_dangling_refs(self):
        # The STRUCTURED_ROSTER fixture has banned_test.hard_luna (hard ban -> error)
        out = io.StringIO()
        with redirect_stdout(out):
            code = roster.cli_main(["doctor", "--json"], self.roster_path)
        self.assertEqual(code, 1)
        report = json.loads(out.getvalue())
        self.assertFalse(report["ok"])
        self.assertTrue(any("gpt-*-luna" in e for e in report["errors"]))
        # Also warns on low quota group chatgpt-pro
        self.assertTrue(any("chatgpt-pro" in w for w in report["warnings"]))

        # Remove banned_test entries -> doctor should pass with 0 errors
        with redirect_stdout(io.StringIO()):
            roster.cli_main(["rm", "banned_test.hard_luna"], self.roster_path)
            roster.cli_main(["rm", "banned_test.ask_fable"], self.roster_path)

        out_ok = io.StringIO()
        with redirect_stdout(out_ok):
            code_ok = roster.cli_main(["doctor", "--json"], self.roster_path)
        self.assertEqual(code_ok, 0)
        report_ok = json.loads(out_ok.getvalue())
        self.assertTrue(report_ok["ok"], report_ok)
        self.assertEqual(report_ok["errors"], [])

        # Entry missing permission override is flagged by doctor
        with redirect_stdout(io.StringIO()):
            roster.cli_main(
                ["set", "implement.no_perms", "--cmd", "claude --model sonnet --effort high", "--quota", "claude-max"],
                self.roster_path,
            )
        out_bad = io.StringIO()
        with redirect_stdout(out_bad):
            code_bad = roster.cli_main(["doctor", "--json"], self.roster_path)
        self.assertEqual(code_bad, 1)
        report_bad = json.loads(out_bad.getvalue())
        self.assertTrue(any("implement.no_perms" in e for e in report_bad["errors"]))

    def test_migrate_dry_run_and_write_on_self_contained_flat_v1_fixture(self):
        """Finding 6988: deterministic, $HOME-independent migration test."""
        src = self.root / "flat_v1.toml"
        src.write_text(FLAT_V1_FIXTURE, encoding="utf-8")

        src_before = src.read_bytes()
        out_dry = io.StringIO()
        with redirect_stdout(out_dry):
            code_dry = roster.cli_main(["migrate", "--dry-run"], src)
        self.assertEqual(code_dry, 0)
        self.assertEqual(src.read_bytes(), src_before)
        self.assertIn("schema_version = 2", out_dry.getvalue())
        self.assertIn("[quota_groups.", out_dry.getvalue())
        self.assertIn("[judge_rules]", out_dry.getvalue())

        dest = self.root / "migrated.toml"
        out_write = io.StringIO()
        with redirect_stdout(out_write):
            code_write = roster.cli_main(["migrate", "--write", str(dest)], src)
        self.assertEqual(code_write, 0)
        self.assertEqual(src.read_bytes(), src_before)
        self.assertTrue(dest.is_file())

        flat = roster.load(src)
        migrated = roster.load(dest)
        self.assertEqual(migrated.schema_version, 2)
        self.assertEqual(migrated.roles, flat.roles)
        self.assertTrue(migrated.quota_groups)
        self.assertTrue(all(qg.check_cmd for qg in migrated.quota_groups.values()))
        self.assertIn("Sonnet is the worker", migrated.entries["implement"]["claude"].notes)
        ban_patterns = {b.pattern: b.mode for b in migrated.bans}
        self.assertEqual(ban_patterns.get("*luna*"), "hard")
        self.assertEqual(ban_patterns.get("*haiku*"), "hard")
        self.assertEqual(ban_patterns.get("*fable*"), "ask_daniel")
        self.assertTrue(any(ej.judge == "signoff.gemini" for ej in migrated.judge_rules.extra_judges))

        out_jr = io.StringIO()
        with redirect_stdout(out_jr):
            self.assertEqual(
                roster.cli_main(["--role", "implement", "--family", "gemini", "--json"], dest),
                0,
            )
        jr = json.loads(out_jr.getvalue())["judge_rule"]
        self.assertTrue(any(ej["ref"] == "signoff.gemini" for ej in jr["extra_judges"]))

        out_doc = io.StringIO()
        with redirect_stdout(out_doc):
            doc_code = roster.cli_main(["doctor", "--json"], dest)
        self.assertEqual(doc_code, 0, out_doc.getvalue())

    def test_mutators_on_flat_v1_preserve_comments_bans_and_quotas_and_reject_blank_inputs(self):
        """Findings 6978 & 6985: every mutator on flat v1 migrates cleanly; blank inputs refused; DEL safe."""
        for label, argv in (
            ("note", ["note", "judge.codex", "Updated judge note"]),
            ("ban", ["ban", "*grok*", "--mode", "hard", "--reason", "No grok"]),
            ("quota", ["quota", "chatgpt-pro", "--status", "low"]),
            ("rm", ["rm", "plan.gemini"]),
            ("set", ["set", "implement.extra", "--cmd", "claude --model sonnet --dangerously-skip-permissions"]),
        ):
            with self.subTest(mutator=label):
                p = self.root / f"flat_{label}.toml"
                p.write_text(FLAT_V1_FIXTURE, encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(roster.cli_main(argv, p), 0)
                loaded = roster.load(p)
                self.assertEqual(loaded.schema_version, 2)
                self.assertIn("Sonnet is the worker", loaded.entries["implement"]["claude"].notes)
                self.assertTrue(any(b.pattern == "*luna*" for b in loaded.bans))
                out_doc = io.StringIO()
                with redirect_stdout(out_doc):
                    self.assertEqual(roster.cli_main(["doctor", "--json"], p), 0, out_doc.getvalue())

        # Whitespace pattern / family refused without touching file
        before = self.roster_path.read_bytes()
        for bad_args in (
            ["ban", "   ", "--mode", "hard", "--reason", "x"],
            ["ban", "*foo*", "--mode", "hard", "--reason", "   "],
            ["set", "implement.claude", "--family", "   "],
            ["set", "implement.broken_q", "--cmd", 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium --yolo'],
        ):
            with self.subTest(bad_args=bad_args):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                    roster.cli_main(bad_args, self.roster_path)
                self.assertEqual(raised.exception.code, 2)
                self.assertEqual(self.roster_path.read_bytes(), before)

        # Control character \x7f in note is escaped in TOML and round-trips cleanly
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                roster.cli_main(["note", "implement.claude", "has\x7fdel"], self.roster_path),
                0,
            )
        self.assertEqual(roster.load(self.roster_path).entries["implement"]["claude"].notes, "has\x7fdel")

    def test_against_three_family_query_and_hard_ban_filtering_and_isolated_extra_judges(self):
        """Findings 6982, 6987, 6988: --role R --against F lists competitors; hard-banned extra judges skipped; each extra source tested alone."""
        # --role judge --against gemini --json returns both claude and codex competitors on 3-family roster
        out = io.StringIO()
        with redirect_stdout(out):
            code = roster.cli_main(["--role", "judge", "--against", "gemini", "--json"], self.roster_path)
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["against"], "gemini")
        self.assertEqual(sorted(c["key"] for c in data["competitors"]), ["claude", "codex"])

        # Works even when author family has no entry in the queried role (signoff has only gemini)
        out2 = io.StringIO()
        with redirect_stdout(out2):
            code2 = roster.cli_main(["--role", "signoff", "--against", "claude", "--json"], self.roster_path)
        self.assertEqual(code2, 0)
        self.assertEqual([c["key"] for c in json.loads(out2.getvalue())["competitors"]], ["gemini"])

        # Hard-banned judge excluded from eligible_judges AND extra_judges
        p = self.root / "hard_judge.toml"
        p.write_text(
            """\
schema_version = 2
updated = "2026-11-19"
note = ""

[judge_rules]
require_different_family = true

[[judge_rules.extra_judges]]
author_family = "gemini"
judge = "signoff.bad_extra"
reason = "banned extra"

[[bans]]
pattern = "*luna*"
mode = "hard"
reason = "no luna"

[roles.judge.claude]
command = "claude --model opus --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "claude-max"

[roles.judge.bad_codex]
command = "codex -m gpt-6.1-luna --yolo"
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"

[roles.signoff.bad_extra]
command = "codex -m gpt-6.1-luna --yolo"
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
allows_judging_families = ["gemini"]
""",
            encoding="utf-8",
        )
        jr = roster.judge_rule_for(roster.load(p), "gemini")
        self.assertEqual([j["key"] for j in jr["eligible_judges"]], ["claude"])
        self.assertEqual(jr["extra_judges"], [])

        # Test explicit extra_judges ALONE (no allows_judging_families) and allows_judging_families ALONE (no extra_judges)
        p_explicit = self.root / "extra_explicit.toml"
        p_explicit.write_text(
            """\
schema_version = 2
updated = "2026-11-19"
note = ""
[judge_rules]
require_different_family = true
[[judge_rules.extra_judges]]
author_family = "gemini"
judge = "signoff.carbon"
reason = "explicit rule"
[roles.signoff.carbon]
command = "agy --model gemini-4-carbon-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-carbon"
""",
            encoding="utf-8",
        )
        self.assertEqual(
            [e["ref"] for e in roster.judge_rule_for(roster.load(p_explicit), "gemini")["extra_judges"]],
            ["signoff.carbon"],
        )

        p_allows = self.root / "extra_allows.toml"
        p_allows.write_text(
            """\
schema_version = 2
updated = "2026-11-19"
note = ""
[judge_rules]
require_different_family = true
[roles.signoff.carbon]
command = "agy --model gemini-4-carbon-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-carbon"
allows_judging_families = ["gemini"]
""",
            encoding="utf-8",
        )
        self.assertEqual(
            [e["ref"] for e in roster.judge_rule_for(roster.load(p_allows), "gemini")["extra_judges"]],
            ["signoff.carbon"],
        )

    def test_doctor_catches_dangling_refs_empty_quota_broken_quotes_and_family_spoofing(self):
        """Findings 6987 & 6988: each doctor defect produces an explicit ERROR (exit 1)."""
        cases = [
            (
                "undefined_quota",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[roles.implement.claude]
command = "claude --model sonnet --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = "missing-bucket"
""",
                "missing-bucket",
            ),
            (
                "empty_quota_v2",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[roles.implement.claude]
command = "claude --model sonnet --dangerously-skip-permissions"
family = "claude"
transport = "claude"
quota_group = ""
""",
                "quota_group",
            ),
            (
                "dangling_paired_with",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[quota_groups.agy-carbon]
status = "ok"
check_cmd = "/quota"
[roles.signoff.gemini]
command = "agy --model gemini-4-carbon-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-carbon"
paired_with = ["judge.nonexistent"]
""",
                "judge.nonexistent",
            ),
            (
                "dangling_default_per_phase",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[judge_rules]
default_per_phase = "judge.missing"
""",
                "judge.missing",
            ),
            (
                "dangling_extra_judges",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[[judge_rules.extra_judges]]
author_family = "gemini"
judge = "signoff.missing"
reason = "x"
""",
                "signoff.missing",
            ),
            (
                "unclosed_quote",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[quota_groups.chatgpt-pro]
status = "ok"
check_cmd = "/status"
[roles.implement.codex]
command = 'codex -m gpt-6.1-sol -c model_reasoning_effort="medium --yolo'
family = "openai"
transport = "codex"
quota_group = "chatgpt-pro"
""",
                "quot",
            ),
            (
                "opus_via_agy_family_spoof",
                """\
schema_version = 2
updated = "2026-11-19"
note = ""
[quota_groups.agy-claude-gpt]
status = "ok"
check_cmd = "/quota"
[roles.implement.spoof]
command = "agy --model claude-opus-4-6-high --dangerously-skip-permissions"
family = "gemini"
transport = "agy"
quota_group = "agy-claude-gpt"
""",
                "contradicts",
            ),
        ]
        for label, text, expected_substr in cases:
            with self.subTest(case=label):
                p = self.root / f"doc_{label}.toml"
                p.write_text(text, encoding="utf-8")
                out = io.StringIO()
                with redirect_stdout(out):
                    code = roster.cli_main(["doctor", "--json"], p)
                self.assertEqual(code, 1, out.getvalue())
                report = json.loads(out.getvalue())
                self.assertTrue(
                    any(expected_substr in e.lower() for e in report["errors"]),
                    f"{label}: expected {expected_substr!r} in {report['errors']}",
                )

    def test_write_atomic_prevalidates_before_backup_and_uses_os_replace(self):
        """Finding 6979: write_atomic validates via load before writing .bak or calling os.replace."""
        p = self.root / "atomic.toml"
        p.write_text(STRUCTURED_ROSTER, encoding="utf-8")
        bad_roster = roster.Roster(
            updated="2026-11-19",
            note="",
            roles={},
            bans=(roster.BanRule(pattern="", mode="hard", reason="bad"),),
            schema_version=2,
        )
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            roster.write_atomic(p, bad_roster)
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(list(self.root.glob("atomic.toml.*.bak")), [])

        valid = roster.load(p)
        with mock.patch.object(roster.os, "replace", wraps=os.replace) as rep:
            roster.write_atomic(p, valid)
        rep.assert_called_once()

    def test_python310_fallback_handles_flat_comments_and_clear_v2_error(self):
        """Finding 6981: simulated tomllib=None parses flat comments and gives clear v2 error."""
        flat_p = self.root / "py310_flat.toml"
        flat_p.write_text(FLAT_V1_FIXTURE, encoding="utf-8")
        with mock.patch.object(roster, "tomllib", None):
            loaded = roster.load(flat_p)
            self.assertEqual(
                loaded.roles["implement"]["claude"],
                "claude --model sonnet --effort high --dangerously-skip-permissions",
            )
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                roster.load(self.roster_path)
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("3.11", err.getvalue())

            err_w = io.StringIO()
            with redirect_stderr(err_w), self.assertRaises(SystemExit) as raised_w:
                roster.write_atomic(flat_p, loaded)
            self.assertEqual(raised_w.exception.code, 2)
            self.assertIn("3.11", err_w.getvalue())

    def test_bin_assist_has_no_dead_roster_shadow_parser(self):
        """Finding 6989: build_parser() must not retain the dead pre-Phase-3 roster_parser."""
        bin_assist = _load_bin_assist()
        parser = bin_assist.build_parser()
        subparsers_action = next(a for a in parser._actions if isinstance(a, bin_assist.argparse._SubParsersAction))
        self.assertNotIn("roster", subparsers_action.choices)


if __name__ == "__main__":
    unittest.main()
