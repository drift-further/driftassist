"""Code root versus runtime roots, and the layout that picks them (effort 621, Phase 3).

`APP_ROOT` is the checkout: code and static assets. The runtime roots
(`config_dir()` for secrets and settings, `data_dir()` for mutable state,
`state_dir()` for PID, log and the migration marker) default to the checkout
while the install is on the legacy layout, and to XDG directories once
`install.json` names this checkout. A second checkout refuses an XDG layout
owned by another one instead of mixing state with it.

Every entry path (serve.py, bin/assist, assist-ctl) holds a shared lock on
`state_dir()/migrate.lock` for as long as it can touch that layout, and refuses
to start while a migration marker says one is incomplete — before it imports
any application state.
"""

import ast
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402
from shared import paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python3") if (ROOT / ".venv" / "bin" / "python3").exists() else sys.executable
_OVERRIDES = ("ASSIST_CONFIG_DIR", "ASSIST_DATA_DIR", "ASSIST_STATE_DIR", "ASSIST_AUTH_TOKEN_PATH")


class _Scratch(unittest.TestCase):
    """A scratch HOME with XDG roots and no overrides, plus a fake checkout."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = _paths.scratch_env(self.root)
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in _OVERRIDES:
            os.environ.pop(key, None)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.xdg_config = self.root / "xdg-config" / "drift-assist"
        self.xdg_data = self.root / "xdg-data" / "drift-assist-data"
        self.xdg_state = self.root / "xdg-state" / "drift-assist"

    def install_json(self, checkout):
        self.xdg_config.mkdir(parents=True, exist_ok=True)
        (self.xdg_config / "install.json").write_text(
            json.dumps({"schema": 1, "checkout": str(checkout), "created": 1}), encoding="utf-8"
        )


class LayoutTests(_Scratch):
    def test_legacy_layout_selected_when_checkout_env_and_no_install_json(self):
        (self.checkout / ".env").write_text("ASSIST_PORT=8123\n")
        self.assertEqual(paths.layout(self.checkout), "legacy")
        self.assertEqual(paths.config_dir(self.checkout), self.checkout)
        self.assertEqual(paths.data_dir(self.checkout), self.checkout)
        self.assertEqual(paths.env_file(self.checkout), self.checkout / ".env")
        self.assertEqual(paths.token_path(self.checkout), self.checkout / "auth_token")
        self.assertEqual(paths.state_dir(), self.xdg_state)

    def test_xdg_layout_selected_by_install_json(self):
        (self.checkout / ".env").write_text("left behind\n")
        self.install_json(self.checkout)
        self.assertEqual(paths.layout(self.checkout), "xdg")
        self.assertEqual(paths.config_dir(self.checkout), self.xdg_config)
        self.assertEqual(paths.data_dir(self.checkout), self.xdg_data)
        self.assertEqual(paths.env_file(self.checkout), self.xdg_config / "server.env")
        self.assertEqual(paths.token_path(self.checkout), self.xdg_config / "auth_token")

    def test_env_overrides_win(self):
        self.install_json(self.checkout)
        cfg, data, st = self.root / "c", self.root / "d", self.root / "s"
        with mock.patch.dict(os.environ, {"ASSIST_CONFIG_DIR": str(cfg), "ASSIST_DATA_DIR": str(data),
                                          "ASSIST_STATE_DIR": str(st)}):
            # install.json is looked up in the overridden config dir: none there.
            self.assertEqual(paths.layout(self.checkout), "legacy")
            self.assertEqual(paths.config_dir(self.checkout), cfg)
            self.assertEqual(paths.data_dir(self.checkout), data)
            self.assertEqual(paths.state_dir(), st)

    def test_token_override_path_wins(self):
        explicit = self.root / "elsewhere" / "token"
        with mock.patch.dict(os.environ, {"ASSIST_AUTH_TOKEN_PATH": str(explicit)}):
            self.assertEqual(paths.token_path(self.checkout), explicit)
            self.install_json(self.checkout)
            self.assertEqual(paths.token_path(self.checkout), explicit)
            from cli import config

            (self.checkout / "bin").mkdir()
            resolved = config.resolve(self.checkout / "bin" / "assist")
            self.assertEqual(resolved.auth_token_path, explicit)

    def test_other_checkout_refused_by_install_json(self):
        """Review focus 4: a second checkout must not mix state with the owner."""
        other = self.root / "other-checkout"
        other.mkdir()
        self.install_json(other)
        with self.assertRaises(paths.LayoutError) as caught:
            paths.config_dir(self.checkout)
        self.assertEqual(caught.exception.code, "layout_owned_by_other_checkout")
        self.assertIn("ASSIST_CONFIG_DIR", str(caught.exception))
        # Must-not-fire: the owner is accepted, and so is a symlinked spelling of it.
        self.assertEqual(paths.layout(other), "xdg")
        link = self.root / "link"
        link.symlink_to(other)
        self.assertEqual(paths.layout(link), "xdg")
        # Overrides give a second checkout its own roots.
        with mock.patch.dict(os.environ, {"ASSIST_CONFIG_DIR": str(self.root / "c2"),
                                          "ASSIST_DATA_DIR": str(self.root / "d2")}):
            self.assertEqual(paths.config_dir(self.checkout), self.root / "c2")

    def test_invalid_install_json_refuses(self):
        self.xdg_config.mkdir(parents=True)
        (self.xdg_config / "install.json").write_text("{nope")
        with self.assertRaises(paths.LayoutError) as caught:
            paths.layout(self.checkout)
        self.assertEqual(caught.exception.code, "install_json_invalid")

    def test_write_install_json_and_ensure_dirs(self):
        written = paths.write_install_json(self.checkout)
        self.assertEqual(written, self.xdg_config / "install.json")
        self.assertEqual(json.loads(written.read_text())["checkout"], str(self.checkout.resolve()))
        paths.ensure_dirs(self.checkout)
        for d in (self.xdg_config, self.xdg_data):
            self.assertTrue(d.is_dir())
            self.assertEqual(d.stat().st_mode & 0o777, 0o700)

    def test_installer_keeps_legacy_layout_when_roots_overlap_checkout(self):
        """Finding 3871: a fresh install never selects XDG roots that alias the checkout."""
        with mock.patch.dict(os.environ, {"ASSIST_DATA_DIR": str(self.checkout)}):
            self.assertTrue(paths.overlap_problems(self.checkout))
            out = subprocess.run([sys.executable, "-I", str(ROOT / "shared" / "paths.py"), "select-xdg",
                                  "--checkout", str(self.checkout)], env=dict(os.environ),
                                 capture_output=True, text=True)
        self.assertEqual(out.returncode, 4, out.stderr)
        self.assertIn("overlaps the checkout", out.stderr)
        self.assertFalse((self.xdg_config / "install.json").exists())
        # Must-not-fire: the README checkout location with default roots is separate.
        readme = self.root / "xdg-data" / "drift-assist"
        readme.mkdir(parents=True)
        self.assertEqual(paths.overlap_problems(readme), [])

    def test_handoff_path_writes_nothing(self):
        self.install_json(self.checkout)
        before = sorted(str(p) for p in self.root.rglob("*"))
        lock = paths.enter_runtime(self.checkout, handoff=True)
        self.assertIsNone(lock)  # no lock file exists to share, and none is created
        after = sorted(str(p) for p in self.root.rglob("*"))
        self.assertEqual(before, after)
        # Must-fire: the ordinary path creates the lock and the XDG dirs.
        lock = paths.enter_runtime(self.checkout)
        self.addCleanup(os.close, lock)
        self.assertTrue((self.xdg_state / "migrate.lock").exists())
        self.assertTrue(self.xdg_data.is_dir())


class MarkerTests(_Scratch):
    def seed(self, payload):
        self.xdg_state.mkdir(parents=True, exist_ok=True)
        (self.xdg_state / "migration.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    def test_marker_states(self):
        self.assertIsNone(paths.migration_state())
        self.seed({"schema": 2, "phase": "done", "direction": "forward"})
        self.assertEqual(paths.migration_state()["phase"], "done")
        self.assertFalse(paths.migration_blocks(paths.migration_state()))
        self.seed({"schema": 2, "phase": "copying", "direction": "forward"})
        self.assertTrue(paths.migration_blocks(paths.migration_state()))
        self.seed("{torn")
        marker = paths.migration_state()
        self.assertFalse(marker["valid"])
        self.assertTrue(paths.migration_blocks(marker))
        # An unsupported schema or direction is not interpreted: it blocks too.
        for bad in ({"schema": 999, "phase": "done", "direction": "forward"},
                    {"schema": 2, "phase": "done", "direction": "sideways"},
                    {"phase": "done", "direction": "forward"}):
            self.seed(bad)
            self.assertFalse(paths.migration_state()["valid"], bad)
            self.assertTrue(paths.migration_blocks(paths.migration_state()), bad)

    def test_enter_runtime_refuses_mid_migration(self):
        self.seed({"schema": 2, "phase": "copying", "direction": "forward"})
        with self.assertRaises(paths.LayoutError) as caught:
            paths.enter_runtime(self.checkout)
        self.assertEqual(caught.exception.code, "migration_in_progress")
        self.assertIn("assist migrate-state --resume", str(caught.exception))
        self.assertIn("assist migrate-state --rollback", str(caught.exception))

    def test_exclusive_holder_makes_entry_busy(self):
        self.xdg_state.mkdir(parents=True)
        fd = os.open(self.xdg_state / "migrate.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaises(paths.LayoutError) as caught:
            paths.enter_runtime(self.checkout)
        self.assertEqual(caught.exception.code, "migration_in_progress")
        fcntl.flock(fd, fcntl.LOCK_UN)
        lock = paths.enter_runtime(self.checkout)
        self.addCleanup(os.close, lock)
        # While an entry holds the shared lock, an exclusive acquire is refused.
        with self.assertRaises(paths.LayoutError) as caught:
            paths.acquire_layout_lock(shared=False)
        self.assertEqual(caught.exception.code, "busy")


# ---------------------------------------------------------------------------
# Real entry points, in subprocesses
# ---------------------------------------------------------------------------

_PROBE = r"""
import runpy, sys
sys.argv = {argv!r}
sys.path.insert(0, {root!r})
code = 0
try:
    runpy.run_path({script!r}, run_name="__main__")
except SystemExit as exc:
    code = exc.code
print("STATE_IMPORTED=%s" % ("shared.state" in sys.modules))
print("EXIT=%s" % code)
"""


def _probe(script, argv, env, timeout=60):
    code = _PROBE.format(argv=argv, root=str(ROOT), script=str(script))
    return subprocess.run([PY, "-c", code], env=env, capture_output=True, text=True,
                          timeout=timeout, cwd=str(ROOT))


class EntryPointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "xdg-state" / "drift-assist"
        self.env = _paths.scratch_env(
            self.root,
            ASSIST_CONFIG_DIR=self.root / "cfg",
            ASSIST_DATA_DIR=self.root / "data",
            ASSIST_PORT=str(_free_port()),
            ASSIST_VENV_REEXEC="0",
        )

    def seed_copying(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "migration.json").write_text(json.dumps({"schema": 2, "phase": "copying", "direction": "forward"}))

    def test_every_entry_path_refuses_during_incomplete_migration_before_state_import(self):
        self.seed_copying()
        out = _probe(ROOT / "serve.py", ["serve.py", "--port", self.env["ASSIST_PORT"]], self.env)
        self.assertIn("migration_in_progress", out.stdout + out.stderr)
        self.assertIn("STATE_IMPORTED=False", out.stdout)
        self.assertNotIn("EXIT=0", out.stdout)

        for verb in (["ls"], ["send", "x", "hi"], ["devices"], ["start"]):
            out = _probe(ROOT / "bin" / "assist", ["assist", *verb], self.env)
            self.assertIn("migration_in_progress", out.stdout + out.stderr, verb)
            self.assertIn("STATE_IMPORTED=False", out.stdout, verb)
            self.assertNotIn("EXIT=0", out.stdout, verb)

        ctl = _ctl_copy(self.root)
        ran = self.root / "fake-serve-ran"
        env = dict(self.env, FAKE_SERVE_MARK=str(ran))
        out = subprocess.run([str(ctl), "start"], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("migration_in_progress", out.stdout + out.stderr)
        self.assertFalse(ran.exists())

        # Must-not-fire: a finished marker lets every entry through.
        (self.state / "migration.json").write_text(json.dumps({"schema": 2, "phase": "done", "direction": "forward"}))
        out = _probe(ROOT / "bin" / "assist", ["assist", "devices"], self.env)
        self.assertNotIn("migration_in_progress", out.stdout + out.stderr)

    def test_diagnostics_read_marker_without_lock_or_state_import(self):
        self.seed_copying()
        self.state.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.state / "migrate.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a migrator holds it
        for verb in ("config", "doctor"):
            out = _probe(ROOT / "bin" / "assist", ["assist", verb], self.env)
            self.assertIn("STATE_IMPORTED=False", out.stdout, verb)
            self.assertIn("copying", out.stdout, verb)
            self.assertNotIn("busy", out.stdout + out.stderr, verb)

    def test_entry_paths_hold_shared_lock_for_lifetime(self):
        # A long-running CLI verb: `assist logs -f` follows the log until killed.
        log = self.root / "assist.log"
        log.write_text("x\n")
        env = dict(self.env, ASSIST_LOG_FILE=str(log))
        # Its own process group: `logs -f` runs `tail -f`, which outlives a
        # terminated parent unless the whole group is signalled.
        proc = subprocess.Popen([PY, str(ROOT / "bin" / "assist"), "logs", "-f"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        try:
            self._wait_until_locked()
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(10)
        self._assert_exclusive_free()

        # A running server holds it for its life.
        tmux_dir = Path(tempfile.mkdtemp(prefix="t621", dir="/tmp"))
        self.addCleanup(shutil.rmtree, tmux_dir, True)
        env = dict(self.env, TMUX_TMPDIR=str(tmux_dir), ASSIST_AUTH_TOKEN_PATH=str(self.root / "tok"))
        env.pop("TMUX", None)
        server = subprocess.Popen([PY, str(ROOT / "serve.py"), "--port", env["ASSIST_PORT"]], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT))
        try:
            self._wait_until_locked(timeout=30)
        finally:
            server.terminate()
            server.wait(15)
        self._assert_exclusive_free()

    def _try_exclusive(self):
        lock = self.state / "migrate.lock"
        if not lock.exists():
            return True
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            return False
        finally:
            os.close(fd)

    def _wait_until_locked(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._try_exclusive():
                return
            time.sleep(0.2)
        self.fail("the entry path never held the shared layout lock")

    def _assert_exclusive_free(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._try_exclusive():
            time.sleep(0.2)
        self.assertTrue(self._try_exclusive(), "the lock outlived its holder")


def _free_port():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


_FAKE_SERVE = """import os, pathlib
mark = os.environ.get("FAKE_SERVE_MARK")
if mark:
    pathlib.Path(mark).write_text("ran")
"""


def _ctl_copy(root):
    """assist-ctl with shared/paths.py beside it and a fake serve.py."""
    home = root / "ctl-checkout"
    (home / "shared").mkdir(parents=True)
    shutil.copy2(ROOT / "assist-ctl", home / "assist-ctl")
    shutil.copy2(ROOT / "shared" / "paths.py", home / "shared" / "paths.py")
    (home / "serve.py").write_text(_FAKE_SERVE)
    return home / "assist-ctl"


class ConsumerTests(_Scratch):
    def test_paths_import_has_no_state_imports(self):
        tree = ast.parse((ROOT / "shared" / "paths.py").read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith(("shared", "routes", "cli", "flask")), node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(alias.name.startswith(("shared", "routes", "cli", "flask")), alias.name)
        out = subprocess.run(
            [PY, "-c", "import sys; sys.path.insert(0, %r); import shared.paths; "
                       "print(sorted(m for m in sys.modules if m.split('.')[0] in "
                       "('flask','routes','cli') or m in ('shared.state','shared.auth')))" % str(ROOT)],
            capture_output=True, text=True, env=self.env, check=True)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_app_root_serves_static_on_both_layouts(self):
        self.assertEqual(paths.APP_ROOT, ROOT)
        source = (ROOT / "routes" / "static.py").read_text()
        self.assertNotIn("DATA_DIR", source)
        from flask import Flask

        from routes.static import static_bp

        app = Flask(__name__)
        app.register_blueprint(static_bp)
        client = app.test_client()
        for path in ("/", "/js/app.js", "/css/base.css", "/login.js", "/sw.js"):
            self.assertEqual(client.get(path).status_code, 200, path)

    def test_updater_uses_app_root(self):
        from shared import updates

        captured = []

        class FakeUpdater:
            def __init__(self, root):
                captured.append(Path(root))
                self.running_commit = None

            def commit(self):
                return None

            def settle_restart(self):
                pass

        with mock.patch.object(updates, "Updater", FakeUpdater), \
                mock.patch.object(updates, "UpdateManager", lambda u: u), \
                mock.patch.object(updates, "_manager", None):
            updates.manager()
        self.assertEqual(captured, [ROOT])

    def test_restart_uses_app_root(self):
        source = (ROOT / "routes" / "settings.py").read_text()
        self.assertIn("restart_home = paths.APP_ROOT", source)
        self.assertNotIn("state.DATA_DIR", source)

    def test_state_files_follow_the_runtime_roots(self):
        from shared import state

        self.assertEqual(state.DATA_DIR, Path(os.environ.get("ASSIST_DATA_DIR") or state.DATA_DIR))
        self.assertEqual(state.SETTINGS_FILE.parent, state.CONFIG_DIR)
        self.assertEqual(state.HISTORY_FILE.parent, state.DATA_DIR)
        self.assertEqual(state.APP_ROOT, ROOT)

    def test_expose_edits_config_env_file(self):
        from cli import config, expose

        (self.checkout / "bin").mkdir()
        (self.checkout / ".env").write_text("UNTOUCHED=1\n")
        self.install_json(self.checkout)
        (self.xdg_config / "server.env").write_text("ASSIST_PORT=8123\n")
        resolved = config.resolve(self.checkout / "bin" / "assist")
        self.assertEqual(resolved.env_file, self.xdg_config / "server.env")
        self.assertEqual(resolved.layout, "xdg")
        with mock.patch.object(expose, "_server_running", return_value=False):
            expose.command(resolved, "10.0.0.5", False)
            self.assertIn("ASSIST_BIND=10.0.0.5", (self.xdg_config / "server.env").read_text())
            self.assertEqual((self.checkout / ".env").read_text(), "UNTOUCHED=1\n")
            expose.command(resolved, None, True)
        self.assertNotIn("ASSIST_BIND=", (self.xdg_config / "server.env").read_text())

    def test_assist_ctl_sources_config_env_file(self):
        ctl = _ctl_copy(self.root)
        checkout = ctl.parent
        (checkout / ".env").write_text("ASSIST_PORT=1111\n")
        env = dict(self.env)
        out = subprocess.run([str(ctl), "paths"], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("port=1111", out.stdout, out.stderr)
        self.install_json(checkout)
        (self.xdg_config / "server.env").write_text("ASSIST_PORT=2222\n")
        out = subprocess.run([str(ctl), "paths"], env=env, capture_output=True, text=True, timeout=30)
        self.assertIn("port=2222", out.stdout, out.stderr)
        self.assertIn("env_file=%s" % (self.xdg_config / "server.env"), out.stdout)
        # Another checkout's install.json: refused, not mixed.
        self.install_json(self.root / "someone-else")
        out = subprocess.run([str(ctl), "start"], env=env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("layout_owned_by_other_checkout", out.stdout + out.stderr)

    def test_config_prints_layout_and_roots(self):
        out = _probe(ROOT / "bin" / "assist", ["assist", "config"],
                     dict(self.env, ASSIST_VENV_REEXEC="0"))
        for label in ("Layout", "Config dir", "Data dir", "State dir", "Env file", "Migration"):
            self.assertIn(label, out.stdout)

    # The public export leaves the promote script out (its EXCLUDES), so this
    # consumer exists only in the private checkout.
    @unittest.skipUnless((ROOT / "distribution" / "promote_public.py").exists(),
                         "promote_public.py is not part of this tree")
    def test_promote_scan_union_and_fail_closed(self):
        sys.path.insert(0, str(ROOT / "distribution"))
        import promote_public

        install = self.checkout
        with mock.patch.object(promote_public, "install_root", return_value=install):
            # Nothing anywhere: refuses.
            with self.assertRaises(promote_public.PromoteError):
                promote_public.host_markers(install)
            # XDG layout: the env file and token live in the config dir.
            self.install_json(install)
            (self.xdg_config / "server.env").write_text("ASSIST_ALLOWED_ORIGINS=http://box.example.lan\n")
            (self.xdg_config / "auth_token").write_text("xdg-token-value\n")
            markers = promote_public.host_markers(install)
            self.assertIn("box.example.lan", markers)
            self.assertIn("xdg-token-value", markers)
            # An explicit token path is read too.
            explicit = self.root / "explicit-token"
            explicit.write_text("explicit-token-value\n")
            with mock.patch.dict(os.environ, {"ASSIST_AUTH_TOKEN_PATH": str(explicit)}):
                self.assertIn("explicit-token-value", promote_public.host_markers(install))
            # Env file present but no token anywhere: still refuses.
            (self.xdg_config / "auth_token").unlink()
            with self.assertRaisesRegex(promote_public.PromoteError, "auth_token"):
                promote_public.host_markers(install)


class BinAssistImportTests(unittest.TestCase):
    def test_bin_assist_lazy_imports_per_verb(self):
        tree = ast.parse((ROOT / "bin" / "assist").read_text())
        # The verb modules that load application state (session -> routes,
        # update -> shared.updates). Light ones (http, proc, pair, …) may load
        # at the top; the runtime probe below is the real guard.
        heavy = {"session", "update"}
        for node in tree.body:
            nodes = [node] + (list(ast.walk(node)) if isinstance(node, ast.Try) else [])
            for inner in nodes:
                if isinstance(inner, ast.ImportFrom) and inner.module == "cli":
                    self.assertFalse({a.name for a in inner.names} & heavy,
                                     "bin/assist imports a verb module at top level")
                if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith(("routes", "shared.state")):
                    self.fail("bin/assist imports application state at top level")
        with tempfile.TemporaryDirectory() as raw:
            env = _paths.scratch_env(raw, ASSIST_CONFIG_DIR=Path(raw) / "c", ASSIST_DATA_DIR=Path(raw) / "d",
                                     ASSIST_VENV_REEXEC="0")
            out = _probe(ROOT / "bin" / "assist", ["assist", "help"], env)
        self.assertIn("STATE_IMPORTED=False", out.stdout)
        self.assertIn("EXIT=0", out.stdout)


if __name__ == "__main__":
    unittest.main()
