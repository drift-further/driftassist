"""Every entry module imports FIRST in a fresh interpreter.

shared.agent_identity imports shared.tab_state, and shared.tmux imports
shared.agent_identity. A top-level `from shared import tmux` in tab_state
(621 Phase 7 step B) closed that into a cycle that only some import orders hit:
`import routes.autoyes` died with "cannot import name '_ANSI_ESCAPE_RE' from
partially initialized module", seven test modules failed to load, and serve.py
could have failed to start, while a suite run that imported tab_state some other
way first stayed green. One subprocess per module, so no earlier import in this
process can hide a cycle.

Run: .venv/bin/python3 -m unittest tests.test_import_order
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._paths import scratch_env

ROOT = Path(__file__).resolve().parent.parent

FIRST_IMPORTS = (
    "shared.tmux",
    "shared.tab_state",
    "shared.drafts",
    "shared.agent_identity",
    "shared.utils",
    "shared.opencode",
    "routes.autoyes",
    "routes.poll",
    "routes.terminal",
    "routes.streaming",
    "routes.input",
    "routes.drafts",
    "routes.tabstate",
    "serve",
)


class ImportOrderTests(unittest.TestCase):
    def test_each_entry_module_imports_first_in_a_fresh_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="p7b-import-") as root:
            env = scratch_env(
                root,
                ASSIST_CONFIG_DIR=Path(root) / "config",
                ASSIST_DATA_DIR=Path(root) / "data",
                ASSIST_STATE_DIR=Path(root) / "state",
                # No tmux server is reachable: importing must not need one.
                TMUX_TMPDIR=Path(root) / "tmux",
            )
            for name in ("config", "data", "state", "tmux"):
                (Path(root) / name).mkdir()
            for module in FIRST_IMPORTS:
                with self.subTest(module=module):
                    result = subprocess.run(
                        [sys.executable, "-c", f"import {module}"],
                        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
