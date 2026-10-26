"""Scratch roots for tests: never a real install's config, data or state.

`isolate_process()` points ASSIST_CONFIG_DIR, ASSIST_DATA_DIR and ASSIST_STATE_DIR at a fresh
temporary directory for the whole test process, unless the caller already set
them. tests/test__isolation.py calls it at import; `unittest discover` imports
modules in sorted order, and `test__` sorts before every other test module, so
it runs before anything imports shared.state.

`scratch_env(root)` builds a complete environment for a subprocess (HOME and
every XDG and ASSIST root under `root`), for tests that start a real entry
point (serve.py, bin/assist, assist-ctl).
"""

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_ISOLATED = None


def isolate_process():
    global _ISOLATED
    if _ISOLATED is None:
        _ISOLATED = Path(tempfile.mkdtemp(prefix="assist-tests-"))
        os.environ.setdefault("ASSIST_CONFIG_DIR", str(_ISOLATED / "config"))
        os.environ.setdefault("ASSIST_DATA_DIR", str(_ISOLATED / "data"))
        # The state dir too: an entry path run by a test (bin/assist, serve.py)
        # takes its layout lock there, and must never create one in the real
        # ~/.local/state/drift-assist.
        os.environ.setdefault("ASSIST_STATE_DIR", str(_ISOLATED / "state"))
        for key in ("ASSIST_CONFIG_DIR", "ASSIST_DATA_DIR", "ASSIST_STATE_DIR"):
            Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    return _ISOLATED


def scratch_env(root, **extra):
    """Environment for a subprocess whose every root is under `root`."""
    root = Path(root)
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "ASSIST_LEGACY_PID_FILE": str(root / "legacy.pid"),
        "ASSIST_NO_HINTS": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    env.update({k: str(v) for k, v in extra.items()})
    return env
