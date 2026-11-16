import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from cli import proc
from routes import terminal


ROOT = Path(__file__).resolve().parents[1]


class PublicPortabilityTests(unittest.TestCase):
    def test_missing_projects_directory_is_an_empty_success(self):
        app = Flask("missing-projects-directory")
        app.register_blueprint(terminal.terminal_bp)
        client = app.test_client()

        with tempfile.TemporaryDirectory() as raw:
            missing_dir = Path(raw) / "projects-not-created-yet"
            with patch.object(terminal.state, "PROJECTS_DIR", missing_dir):
                response = client.get("/terminal/projects")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["projects"], [])
        self.assertIn(str(missing_dir), payload["note"])

    def test_logs_uses_resolved_log_file(self):
        with tempfile.TemporaryDirectory() as raw:
            log_file = Path(raw) / "configured.log"
            log_file.write_text("ready\n", encoding="utf-8")
            resolved = SimpleNamespace(log_file=log_file)
            with patch.object(proc.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertEqual(proc.logs(resolved, lines="17"), 0)
            run.assert_called_once_with(["tail", "-n", "17", str(log_file)])

    def test_assist_logs_honors_environment_log_path_end_to_end(self):
        with tempfile.TemporaryDirectory() as raw:
            scratch = Path(raw)
            log_file = scratch / "configured.log"
            log_file.write_text("first\nconfigured-last\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "ASSIST_HOME": str(ROOT),
                    "ASSIST_LOG_FILE": str(log_file),
                    "XDG_CONFIG_HOME": str(scratch / "xdg"),
                }
            )
            completed = subprocess.run(
                [sys.executable, str(ROOT / "bin" / "assist"), "logs", "1"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "configured-last\n")

    def test_studio_never_falls_back_to_an_estate_cli(self):
        stderr = io.StringIO()
        with patch.object(proc.shutil, "which", return_value=None) as which, \
                patch.object(proc.os, "execvp") as execute, \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(proc.studio(["status"]), 1)
        self.assertEqual([c.args for c in which.call_args_list], [("sto",), ("studio",)])
        execute.assert_not_called()
        self.assertIn("no Studio CLI on PATH", stderr.getvalue())

    def test_installer_prompts_before_adding_a_missing_statusline(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        branch = source.split('elif [[ -z "$sl_cmd" ]]', 1)[1].split(
            'elif [[ "$sl_cmd" == "$STATUSLINE_BIN" ]]', 1
        )[0]
        self.assertIn("prompt_replace_statusline", branch)
        self.assertLess(
            branch.index("prompt_replace_statusline"),
            branch.index("write_assist_statusline"),
        )

    def test_removed_redraw_helpers_stay_removed(self):
        streaming = (ROOT / "routes" / "streaming.py").read_text(encoding="utf-8")
        tmux = (ROOT / "shared" / "tmux.py").read_text(encoding="utf-8")
        park = (ROOT / "shared" / "execution_park.py").read_text(encoding="utf-8")
        for name in ("_force_redraw", "_maybe_redraw_async", "_stable_since"):
            self.assertNotIn(name, streaming)
        self.assertNotIn("pane_wants_ctrl_l_heal", tmux)
        self.assertNotIn("OPERATOR_TMUX_NON_CLAIM", park)


if __name__ == "__main__":
    unittest.main()
