"""The suite never reads or writes a real install's config or data.

Imported first by `unittest discover` (sorted order; `test__` sorts before
`test_a…`), so the roots are scratch before any module imports shared.state.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

_paths.isolate_process()


class IsolationTests(unittest.TestCase):
    def test_runtime_roots_are_scratch(self):
        home = Path.home()
        for key in ("ASSIST_CONFIG_DIR", "ASSIST_DATA_DIR", "ASSIST_STATE_DIR"):
            value = Path(os.environ[key]).resolve()
            self.assertNotEqual(value, (home / ".config" / "drift-assist").resolve())
            self.assertNotEqual(value, (home / ".local" / "share" / "drift-assist").resolve())
            self.assertNotEqual(value, (home / ".local" / "share" / "drift-assist-data").resolve())
            self.assertNotEqual(value, _paths.ROOT)
            self.assertNotEqual(value, (home / ".local" / "state" / "drift-assist").resolve())


if __name__ == "__main__":
    unittest.main()
