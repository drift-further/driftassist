"""Narrow tests for the role/model-family roster CLI surface."""

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from cli import roster, session


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "roster.example.toml"


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.loaded = roster.load(EXAMPLE)

    def assert_roster_error(self, function, *args):
        error = io.StringIO()
        with redirect_stderr(error), self.assertRaises(SystemExit) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, 2)
        return error.getvalue()

    def test_parse_preserves_launch_command(self):
        self.assertEqual(self.loaded.updated, "2026-09-29")
        self.assertIn("competing family", self.loaded.note)
        self.assertEqual(
            self.loaded.roles["plan"]["codex"],
            'codex -m gpt-6-astra -c model_reasoning_effort="medium" --yolo',
        )

    def test_parse_accepts_trailing_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roster.toml"
            path.write_text(
                "[roles.implement]\n"
                "claude = 'claude --model opus'  # the implementer\n",
                encoding="utf-8",
            )
            loaded = roster.load(path)
        self.assertEqual(
            loaded.roles["implement"]["claude"], "claude --model opus"
        )

    def test_unknown_role_and_family_list_valid_values(self):
        error = self.assert_roster_error(
            roster.resolve_command, self.loaded, "missing"
        )
        self.assertIn("valid roles: implement, judge, oneshot, plan, ticket", error)
        error = self.assert_roster_error(
            roster.resolve_command, self.loaded, "plan", "cursor"
        )
        self.assertIn("valid families: claude, codex", error)

    def test_missing_file_names_destination_and_example(self):
        path = Path("/tmp/roster-test-does-not-exist/roster.toml")
        error = self.assert_roster_error(roster.load, path)
        self.assertIn(str(path), error)
        self.assertIn("roster.example.toml", error)

    def test_family_selects_command(self):
        self.assertEqual(
            roster.resolve_command(self.loaded, "implement", family="claude"),
            "claude --model opus --effort medium",
        )

    def test_against_selects_the_one_competitor(self):
        self.assertEqual(
            roster.resolve_command(self.loaded, "judge", against="claude"),
            'codex -m gpt-6-astra -c model_reasoning_effort="high" --yolo',
        )

    def test_against_refuses_two_competitors(self):
        loaded = roster.Roster(
            updated="today",
            note="test",
            roles={"judge": {"claude": "c", "codex": "x", "cursor": "u"}},
        )
        error = self.assert_roster_error(
            roster.resolve_command, loaded, "judge", None, "claude"
        )
        self.assertIn("codex, cursor", error)

    def test_multi_family_without_flag_refuses_with_required_message(self):
        error = self.assert_roster_error(
            roster.resolve_command, self.loaded, "plan"
        )
        self.assertIn(
            "ask the user which model family to use (--family), or use --against "
            "<author family> for a judge",
            error,
        )

    def test_single_family_role_needs_no_flag(self):
        self.assertEqual(
            roster.resolve_command(self.loaded, "ticket"),
            'codex -m gpt-5.6-luna -c model_reasoning_effort="max" --yolo',
        )

    def test_roster_role_prints_only_the_command(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = roster.command(EXAMPLE, "oneshot", "codex")
        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue(),
            'codex -m gpt-6-astra -c model_reasoning_effort="low" --yolo\n',
        )

    def test_launch_builds_the_same_enter_autoyes_wait_send(self):
        launch_response = {
            "session": "judge-run",
            "target": "judge-run:2.3",
            "venv": None,
            "venv_activated": False,
            "existed": False,
        }
        output = io.StringIO()
        with mock.patch.object(session.http, "post", return_value=launch_response), \
                mock.patch.object(session, "send", return_value=0) as send, \
                redirect_stdout(output):
            code = session.launch(
                "judge-run",
                role="judge",
                against="claude",
                roster_path=EXAMPLE,
            )
        self.assertEqual(code, 0)
        send.assert_called_once_with(
            "judge-run",
            text='codex -m gpt-6-astra -c model_reasoning_effort="high" --yolo',
            enter=True,
            pane="2.3",
            wait_for_completion=True,
            timeout=110,
            autoyes=True,
            show_next_hint=False,
        )
        self.assertIn(
            'command: codex -m gpt-6-astra -c model_reasoning_effort="high" --yolo',
            output.getvalue(),
        )

    def test_role_launch_refuses_existing_session_without_sending(self):
        launch_response = {
            "session": "judge-run",
            "target": "judge-run:2.3",
            "venv": None,
            "venv_activated": False,
            "existed": True,
        }
        error = io.StringIO()
        output = io.StringIO()
        with mock.patch.object(session.http, "post", return_value=launch_response), \
                mock.patch.object(session, "send") as send, \
                redirect_stderr(error), redirect_stdout(output):
            code = session.launch(
                "judge-run",
                role="judge",
                against="claude",
                roster_path=EXAMPLE,
            )
        self.assertEqual(code, 2)
        send.assert_not_called()
        self.assertIn(
            "session judge-run already exists (target judge-run:2.3); "
            "role command not sent — use assist send",
            error.getvalue(),
        )

    def test_role_launch_wait_non_idle_explains_skip_and_does_not_send(self):
        launch_response = {
            "session": "judge-run",
            "target": "judge-run:2.3",
            "venv": None,
            "venv_activated": False,
            "existed": False,
        }
        for wait_exit in (10, 75):
            with self.subTest(wait_exit=wait_exit):
                error = io.StringIO()
                output = io.StringIO()
                with mock.patch.object(
                    session.http, "post", return_value=launch_response
                ), mock.patch.object(
                    session.wait_cli, "run_wait", return_value=wait_exit
                ), mock.patch.object(session, "send") as send, redirect_stderr(
                    error
                ), redirect_stdout(output):
                    code = session.launch(
                        "judge-run",
                        wait_for_completion=True,
                        role="judge",
                        against="claude",
                        roster_path=EXAMPLE,
                    )
                self.assertEqual(code, wait_exit)
                send.assert_not_called()
                self.assertIn(
                    "role command not sent (pane not idle after launch)",
                    error.getvalue(),
                )

    def test_installer_seeds_roster_without_overwriting(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ ! -f "$ROSTER_FILE" ]]; then', source)
        self.assertIn('cp "$SCRIPT_DIR/roster.example.toml" "$ROSTER_FILE"', source)
        self.assertNotIn('cp -f "$SCRIPT_DIR/roster.example.toml"', source)


if __name__ == "__main__":
    unittest.main()
