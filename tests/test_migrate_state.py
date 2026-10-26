"""`assist migrate-state`: the legacy-to-XDG state move as a transaction (effort 621, Phase 4).

The tool runs once on a live install, so every claim here is made against a
scratch legacy install driven through the real `bin/assist` wrapper in a
subprocess, with crashes injected by SIGKILL at every named point — after
each marker write AND between each filesystem effect and the next marker
write — in both directions, then resumed and compared with an uninterrupted
run: hashed tree, selected layout, token bytes and provenance identity.

Rules under test: sources are untouched until the layout is committed;
resume compares hashes, never existence; an unreadable marker means recovery
by hand, never a guess; rollback restores the current files (post-migration
changes kept) and stops on a conflicting legacy file with a report — there is
no --force; the server, a live old CLI process from the checkout, and an
existing install.json are each refused before anything is written.
"""

import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _paths  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python3") if (ROOT / ".venv" / "bin" / "python3").exists() else sys.executable
RECEIPT = ".assist-launch-provenance-v1-initialization.json"
STORE = ".assist-launch-provenance-v1"
CONFIG_ITEMS = {".env": "server.env", "auth_token": "auth_token", "settings.json": "settings.json",
                "project_settings.json": "project_settings.json"}
DATA_ITEMS = ["history.json", "drafts.json", "tab_state.json", "devices.json", "kill-audit.log", STORE, RECEIPT]


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _hash(path):
    path = Path(path)
    if path.is_dir():
        h = hashlib.sha256()
        for child in sorted(path.rglob("*")):
            rel = str(child.relative_to(path)).encode()
            h.update(rel + b"\0" + oct(child.stat().st_mode & 0o777).encode())
            if child.is_file():
                h.update(child.read_bytes())
        return h.hexdigest()
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Install:
    """A scratch legacy install: a checkout holding every state item."""

    def __init__(self, root, *, code=False, readme_default=False):
        self.root = Path(root)
        self.readme_default = readme_default
        # The README's install: the checkout at ~/.local/share/drift-assist,
        # no XDG_* variables and no ASSIST_*_DIR overrides.
        self.checkout = (self.root / "home" / ".local" / "share" / "drift-assist"
                         if readme_default else self.root / "checkout")
        if code:
            _copy_tracked_tree(self.checkout)
        else:
            self.checkout.mkdir(parents=True)
        self.port = _free_port()
        # The port comes from the environment, so .env is byte-identical across
        # installs and end states compare.
        self.env = _paths.scratch_env(self.root, ASSIST_HOME=self.checkout, ASSIST_VENV_REEXEC="0",
                                      ASSIST_PORT=str(self.port))
        if readme_default:
            for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
                self.env.pop(key)
            home = self.root / "home"
            self.config = home / ".config" / "drift-assist"
            self.data = home / ".local" / "share" / "drift-assist-data"
            self.state = home / ".local" / "state" / "drift-assist"
        else:
            self.config = self.root / "xdg-config" / "drift-assist"
            self.data = self.root / "xdg-data" / "drift-assist-data"
            self.state = self.root / "xdg-state" / "drift-assist"
        self.seed()

    def seed(self):
        c = self.checkout
        (c / ".env").write_text("ASSIST_SESSION_INIT_CMD=\n")
        token = c / "auth_token"
        token.write_text("scratch-token-value\n")
        token.chmod(0o600)
        (c / "settings.json").write_text(json.dumps({"ui": {"theme": "x"}}))
        (c / "project_settings.json").write_text("{}")
        (c / "history.json").write_text(json.dumps([{"text": "hello"}]))
        (c / "drafts.json").write_text(json.dumps({"s:1.1": {"text": "draft"}}))
        (c / "tab_state.json").write_text("{}")
        (c / "devices.json").write_text(json.dumps({"schema": 1, "devices": []}))
        (c / "kill-audit.log").write_text("{}\n")
        code = (
            "import sys; sys.path.insert(0, %r); import os; os.environ.pop('ASSIST_LAUNCH_PROVENANCE_ROOT', None);"
            "from shared import launch_provenance as p;"
            "p.initialize_epoch(assist_home=%r, receipt_path=%r, expect_empty=True)"
        ) % (str(ROOT), str(c), str(c / RECEIPT))
        subprocess.run([PY, "-c", code], env=self.env, check=True, capture_output=True)

    def assist(self, *args, crash=None, pause=None, trace=None, timeout=60, script=None):
        env = dict(self.env)
        if crash:
            env["ASSIST_MIGRATE_TEST_CRASH"] = crash
        if pause:
            env["ASSIST_MIGRATE_TEST_PAUSE"] = pause
        if trace:
            env["ASSIST_MIGRATE_TEST_TRACE"] = str(trace)
        return subprocess.run([PY, str(script or ROOT / "bin" / "assist"), *args], env=env,
                              capture_output=True, text=True, timeout=timeout)

    def migrate(self, *args, **kw):
        return self.assist("migrate-state", *args, **kw)

    def marker(self):
        path = self.state / "migration.json"
        return json.loads(path.read_text()) if path.exists() else None

    def finish(self):
        """Resume from a valid marker, or start over when none was written."""
        marker = self.marker()
        if marker is None:
            return self.migrate()
        if marker.get("phase") == "done":
            return None
        return self.migrate("--resume")

    def sources(self):
        out = {}
        for name in list(CONFIG_ITEMS) + DATA_ITEMS:
            path = self.checkout / name
            if path.exists():
                out[name] = _hash(path)
        return out

    def end_state(self):
        """What must match between an uninterrupted run and a resumed one."""
        tree = {}
        for base, label in ((self.config, "config"), (self.data, "data"), (self.checkout, "checkout")):
            if not base.exists():
                continue
            for name in sorted(os.listdir(base)):
                if name in ("install.json", ".assist-state-moved"):
                    continue
                path = base / name
                tree[f"{label}/{name}"] = _hash(path)
        layout = "xdg" if (self.config / "install.json").exists() else "legacy"
        token_file = self.config / "auth_token" if layout == "xdg" else self.checkout / "auth_token"
        store = (self.data if layout == "xdg" else self.checkout) / STORE
        receipt = (self.data if layout == "xdg" else self.checkout) / RECEIPT
        return {
            "tree": tree,
            "layout": layout,
            "token": token_file.read_bytes(),
            "epoch": (store / "epoch.json").read_bytes(),
            "receipt_root": json.loads(receipt.read_text())["provenance_root"],
            "marker": (self.marker() or {}).get("phase"),
            "parts": sorted(str(p) for p in self.root.rglob("*.part")),
        }


def _copy_tracked_tree(dest):
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, check=True).stdout
    for rel in filter(None, files.decode().split("\0")):
        src = ROOT / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    (dest / ".venv").symlink_to(ROOT / ".venv")


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def install(self, **kw):
        self._count = getattr(self, "_count", 0) + 1
        return Install(self.tmp / f"i{self._count}", **kw)


# ---------------------------------------------------------------------------
# Preflight refusals: nothing written
# ---------------------------------------------------------------------------


class PreflightTests(_Base):
    def assert_refused_untouched(self, inst, out, needle):
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn(needle, out.stdout + out.stderr)
        self.assertIsNone(inst.marker())
        self.assertFalse((inst.config / "install.json").exists())
        self.assertFalse(inst.data.exists() and any(inst.data.iterdir()))

    def test_refuses_while_server_running(self):
        inst = self.install()
        server = subprocess.Popen([PY, "-m", "http.server", str(inst.port), "--bind", "127.0.0.1"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(server.wait)
        self.addCleanup(server.terminate)
        for _ in range(50):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", inst.port)) == 0:
                    break
            time.sleep(0.1)
        self.assert_refused_untouched(inst, inst.migrate(), "server")

    def test_refuses_while_checkout_cli_process_alive(self):
        inst = self.install()
        old_cli = subprocess.Popen([PY, "-c", "import time; time.sleep(60)",
                                    str(inst.checkout / "bin" / "assist"), "wait", "x"])
        self.addCleanup(old_cli.wait)
        self.addCleanup(old_cli.kill)
        out = inst.migrate()
        self.assert_refused_untouched(inst, out, str(old_cli.pid))
        old_cli.kill()
        old_cli.wait()
        # Must-not-fire: with it gone the run goes ahead.
        self.assertEqual(inst.migrate().returncode, 0)

    def test_refuses_foreign_install_json(self):
        inst = self.install()
        inst.config.mkdir(parents=True)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": "/elsewhere", "created": 1}))
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("layout_owned_by_other_checkout", out.stdout + out.stderr)
        self.assertIsNone(inst.marker())
        # Its own install.json: no take-over either.
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": str(inst.checkout), "created": 1}))
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("install.json", out.stdout + out.stderr)
        self.assertIsNone(inst.marker())

    def test_refuses_differing_existing_destination(self):
        inst = self.install()
        inst.config.mkdir(parents=True)
        (inst.config / "settings.json").write_text('{"other": true}')
        before = inst.sources()
        self.assert_refused_untouched_dest(inst)
        self.assertEqual(inst.sources(), before)
        # An identical destination is not a conflict.
        shutil.copy2(inst.checkout / "settings.json", inst.config / "settings.json")
        self.assertEqual(inst.migrate().returncode, 0)

    def assert_refused_untouched_dest(self, inst):
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("settings.json", out.stdout + out.stderr)
        self.assertIsNone(inst.marker())
        self.assertEqual((inst.config / "settings.json").read_text(), '{"other": true}')

    def test_preflight_inventory_includes_provenance_store_and_both_receipt_items(self):
        inst = self.install()
        out = inst.migrate(crash="marker:copying")
        self.assertEqual(out.returncode, -signal.SIGKILL)
        labels = {item["label"] for item in inst.marker()["items"]}
        self.assertTrue({STORE, "receipt.orig", "receipt.new", "auth_token", ".env", "devices.json"} <= labels)
        for item in inst.marker()["items"]:
            self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")


def _all_hashes(root):
    """Every file's hash, except the empty flock file taking the lock creates."""
    return {str(p): _hash(p) for p in Path(root).rglob("*") if p.is_file() and p.name != "migrate.lock"}


class RootOverlapTests(_Base):
    """Finding 3871 (B1): a data or config root that aliases the checkout."""

    def test_readme_default_install_migrates_without_losing_data(self):
        inst = self.install(readme_default=True)
        before = inst.sources()
        out = inst.migrate()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertNotEqual(inst.data.resolve(), inst.checkout.resolve())
        for name in ("history.json", "drafts.json", "devices.json", "tab_state.json", "kill-audit.log", RECEIPT):
            self.assertTrue((inst.data / name).is_file(), name)
        self.assertEqual(_hash(inst.data / STORE), before[STORE])
        self.assertEqual(_hash(inst.data / "history.json"), before["history.json"])
        self.assertEqual(inst.sources(), {})
        out = inst.migrate("--rollback")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(inst.sources(), before)

    def test_readme_default_install_crash_points_resume(self):
        reference = self.install(readme_default=True)
        trace = self.tmp / "default.trace"
        self.assertEqual(reference.migrate(trace=trace).returncode, 0)
        expected = _normalise(reference.end_state(), reference)
        for point in trace.read_text().split():
            with self.subTest(point=point):
                inst = self.install(readme_default=True)
                self.assertEqual(inst.migrate(crash=point).returncode, -signal.SIGKILL)
                out = inst.finish()
                self.assertTrue(out is None or out.returncode == 0, out and out.stdout + out.stderr)
                self.assertEqual(_normalise(inst.end_state(), inst), expected)
                shutil.rmtree(inst.root)

    def test_overlapping_roots_are_refused_before_anything_is_written(self):
        for label, variant in (
            ("data equals checkout", {"ASSIST_DATA_DIR": "{checkout}"}),
            ("data inside checkout", {"ASSIST_DATA_DIR": "{checkout}/data"}),
            ("config equals checkout", {"ASSIST_CONFIG_DIR": "{checkout}"}),
            ("checkout inside config", {"ASSIST_CONFIG_DIR": "{root}"}),
        ):
            with self.subTest(label):
                inst = self.install()
                for key, value in variant.items():
                    inst.env[key] = value.format(checkout=inst.checkout, root=inst.root)
                before = _all_hashes(inst.root)
                out = inst.migrate()
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("overlaps the checkout", out.stdout + out.stderr)
                self.assertIsNone(inst.marker())
                self.assertEqual(_all_hashes(inst.root), before)

    def test_symlinked_data_root_aliasing_the_checkout_is_refused(self):
        inst = self.install()
        link = inst.root / "data-link"
        link.symlink_to(inst.checkout)
        inst.env["ASSIST_DATA_DIR"] = str(link)
        before = _all_hashes(inst.checkout)
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("overlaps the checkout", out.stdout + out.stderr)
        self.assertEqual(_all_hashes(inst.checkout), before)

    def test_rollback_refuses_overlap_too(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        inst.env["ASSIST_DATA_DIR"] = str(inst.checkout)
        out = inst.migrate("--rollback")
        self.assertNotEqual(out.returncode, 0)
        self.assertIsNotNone(inst.marker())
        self.assertEqual(inst.marker()["direction"], "forward")


class TokenOverrideTests(_Base):
    """Finding 3872 (B2): ASSIST_AUTH_TOKEN_PATH naming the token the migration moves."""

    def test_exported_override_naming_the_checkout_token_is_refused(self):
        inst = self.install()
        inst.env["ASSIST_AUTH_TOKEN_PATH"] = str(inst.checkout / "auth_token")
        before = _all_hashes(inst.root)
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ASSIST_AUTH_TOKEN_PATH", out.stdout + out.stderr)
        self.assertIsNone(inst.marker())
        self.assertEqual(_all_hashes(inst.root), before)

    def test_env_file_override_naming_the_checkout_token_is_refused(self):
        inst = self.install()
        with open(inst.checkout / ".env", "a") as env:
            env.write(f"ASSIST_AUTH_TOKEN_PATH={inst.checkout / 'auth_token'}\n")
        before = _all_hashes(inst.root)
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ASSIST_AUTH_TOKEN_PATH", out.stdout + out.stderr)
        self.assertEqual(_all_hashes(inst.root), before)

    def test_external_override_is_preserved_both_ways(self):
        inst = self.install()
        external = inst.root / "external-token"
        external.write_text("external-token-value\n")
        external.chmod(0o600)
        inst.env["ASSIST_AUTH_TOKEN_PATH"] = str(external)
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(external.read_text(), "external-token-value\n")
        self.assertIn(f"Token file         {external}", inst.assist("config").stdout)
        self.assertEqual(inst.migrate("--rollback").returncode, 0)
        self.assertEqual(external.read_text(), "external-token-value\n")

    def test_rollback_refuses_override_naming_the_xdg_token(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        inst.env["ASSIST_AUTH_TOKEN_PATH"] = str(inst.config / "auth_token")
        before = _all_hashes(inst.root)
        out = inst.migrate("--rollback")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ASSIST_AUTH_TOKEN_PATH", out.stdout + out.stderr)
        self.assertEqual(_all_hashes(inst.root), before)


class JournalIdentityTests(_Base):
    """Finding 3873 (B3): recovery is bound to the validated journal."""

    def refused_unchanged(self, inst, *args, needle):
        before = _all_hashes(inst.root)
        marker = (inst.state / "migration.json").read_bytes()
        out = inst.migrate(*args)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn(needle, out.stdout + out.stderr)
        self.assertEqual(_all_hashes(inst.root), before)
        self.assertEqual((inst.state / "migration.json").read_bytes(), marker)
        return out

    def test_resume_refuses_config_root_drift(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:committed").returncode, -signal.SIGKILL)
        inst.env["ASSIST_CONFIG_DIR"] = str(inst.root / "different-config")
        self.refused_unchanged(inst, "--resume", needle="config_dir")
        self.assertFalse((inst.root / "different-config").exists())
        # Back in the original environment, resume finishes.
        del inst.env["ASSIST_CONFIG_DIR"]
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "xdg")

    def test_resume_refuses_data_root_drift_in_rollback(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(inst.migrate("--rollback", crash="marker:committed").returncode, -signal.SIGKILL)
        inst.env["ASSIST_DATA_DIR"] = str(inst.root / "different-data")
        self.refused_unchanged(inst, "--resume", needle="data_dir")
        del inst.env["ASSIST_DATA_DIR"]
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "legacy")

    def test_rollback_after_done_refuses_root_drift(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        inst.env["ASSIST_DATA_DIR"] = str(inst.root / "different-data")
        self.refused_unchanged(inst, "--rollback", needle="data_dir")

    def test_resume_refuses_token_path_drift(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:publishing").returncode, -signal.SIGKILL)
        external = inst.root / "external-token"
        external.write_text("x\n")
        inst.env["ASSIST_AUTH_TOKEN_PATH"] = str(external)
        self.refused_unchanged(inst, "--resume", needle="token")

    def test_resume_refuses_foreign_owner(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:committed").returncode, -signal.SIGKILL)
        inst.config.mkdir(parents=True, exist_ok=True)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": "/other-checkout"}))
        out = self.refused_unchanged(inst, "--resume", needle="/other-checkout")
        self.assertTrue((inst.checkout / "auth_token").exists())

    def test_unsupported_or_malformed_journal_is_recovery_required(self):
        def corrupt_schema(m):
            m["schema"] = 999

        def corrupt_direction(m):
            m["direction"] = "sideways"

        def corrupt_item_path(m):
            m["items"][0]["dst"] = "/etc/assist-takeover"

        def corrupt_item_shape(m):
            del m["items"][0]["sha256"]

        def corrupt_roots(m):
            m["data_dir"] = "relative/path"

        for corrupt in (corrupt_schema, corrupt_direction, corrupt_item_path, corrupt_item_shape, corrupt_roots):
            with self.subTest(corrupt.__name__):
                inst = self.install()
                self.assertEqual(inst.migrate(crash="marker:copying").returncode, -signal.SIGKILL)
                marker = inst.marker()
                corrupt(marker)
                (inst.state / "migration.json").write_text(json.dumps(marker))
                for args in (("--resume",), ("--rollback",)):
                    self.refused_unchanged(inst, *args, needle="recovery required")
                out = inst.assist("ls")
                self.assertIn("migration_in_progress", out.stdout + out.stderr)


class ProcessScanTests(unittest.TestCase):
    """Finding 3867: a process listing that cannot be produced is not 'nothing running'."""

    def test_scan_fails_closed_when_it_cannot_see_itself(self):
        from unittest import mock

        sys.path.insert(0, str(ROOT))
        from cli import migrate_state as ms

        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.object(ms, "PROC_ROOT", Path(raw)):  # a /proc without us in it
                with self.assertRaises(ms.MigrationError) as caught:
                    ms._process_argvs()
            self.assertIn("cannot list processes", str(caught.exception))
            failed = subprocess.CompletedProcess([], 1, stdout="", stderr="ps: not allowed")
            with mock.patch.object(ms, "PROC_ROOT", Path(raw) / "absent"), \
                    mock.patch.object(ms.subprocess, "run", return_value=failed):
                with self.assertRaises(ms.MigrationError):
                    ms._process_argvs()
            listing = subprocess.CompletedProcess([], 0, stdout="  1 /sbin/init\n", stderr="")
            with mock.patch.object(ms, "PROC_ROOT", Path(raw) / "absent"), \
                    mock.patch.object(ms.subprocess, "run", return_value=listing):
                with self.assertRaises(ms.MigrationError):  # our own pid is missing
                    ms._process_argvs()
        # Must-not-fire: the real /proc lists this process.
        self.assertIn(os.getpid(), ms._process_argvs())
        ours = subprocess.CompletedProcess([], 0, stdout=f"  1 /sbin/init\n {os.getpid()} python x\n", stderr="")
        with mock.patch.object(ms, "PROC_ROOT", Path("/nonexistent-proc")), \
                mock.patch.object(ms.subprocess, "run", return_value=ours):
            self.assertIn(os.getpid(), ms._process_argvs())


class RecoveryInvariantTests(_Base):
    """Round 2, finding 3905 (R2-B1): cleanup only on a valid, phase-consistent selected layout."""

    def refused_unchanged(self, inst, *args, needle):
        before = _all_hashes(inst.root)
        out = inst.migrate(*args)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn(needle, out.stdout + out.stderr)
        self.assertEqual(_all_hashes(inst.root), before)
        return out

    def test_unsupported_record_naming_this_checkout_is_refused(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:committed").returncode, -signal.SIGKILL)
        before = inst.sources()
        (inst.config / "install.json").write_text(json.dumps({"schema": 999, "checkout": str(inst.checkout)}))
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        self.assertEqual(inst.sources(), before)
        self.follow_record_step(inst, self.first_step(out))
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "xdg")

    def test_missing_record_at_forward_cleaning_is_refused_with_steps(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:cleaning").returncode, -signal.SIGKILL)
        before = inst.sources()
        (inst.config / "install.json").unlink()
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        self.assertIn("To recover", out.stderr)
        self.assertEqual(inst.sources(), before)
        self.follow_record_step(inst, self.first_step(out))
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "xdg")

    def test_unexpected_record_at_reverse_cleaning_is_refused(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(inst.migrate("--rollback", crash="marker:cleaning").returncode, -signal.SIGKILL)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": str(inst.checkout)}))
        xdg_token = (inst.config / "auth_token").read_bytes()
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        self.assertIn("To recover", out.stderr)
        self.assertEqual((inst.config / "auth_token").read_bytes(), xdg_token)
        self.follow_record_step(inst, self.first_step(out))
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "legacy")

    def test_record_present_before_commit_is_refused(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:publishing").returncode, -signal.SIGKILL)
        inst.config.mkdir(parents=True, exist_ok=True)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": str(inst.checkout)}))
        self.refused_unchanged(inst, "--resume", needle="install.json")

    def test_a_destination_that_no_longer_verifies_stops_cleanup(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:cleaning").returncode, -signal.SIGKILL)
        before = inst.sources()
        (inst.data / "history.json").write_text('["damaged"]')
        self.refused_unchanged(inst, "--resume", needle="history.json")
        self.assertEqual(inst.sources(), before)

    # Round 3, finding 3925 (R3-A1): the printed repair, followed literally, lets --resume finish.

    def first_step(self, out):
        """The first recovery bullet, with its continuation lines."""
        lines = out.stderr.split("To recover by hand", 1)[1].splitlines()[1:]
        step = [lines[0]]
        for line in lines[1:]:
            if line.startswith("  - "):
                break
            step.append(line)
        return "\n".join(step)

    def follow_record_step(self, inst, step):
        record = inst.config / "install.json"
        if "contain exactly" in step:
            record.write_text(step.split("contain exactly", 1)[1].split("\n")[1].strip())
        else:
            self.assertIn(f"remove {record}", step)
            record.unlink()

    def test_record_planted_before_commit_step_removes_it(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:copying").returncode, -signal.SIGKILL)
        inst.config.mkdir(parents=True, exist_ok=True)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": str(inst.checkout)}))
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        step = self.first_step(out)
        self.assertNotIn("contain exactly", step)
        self.follow_record_step(inst, step)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(inst.end_state()["layout"], "xdg")

    def test_record_removed_before_reverse_commit_step_restores_it(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(inst.migrate("--rollback", crash="marker:copying").returncode, -signal.SIGKILL)
        (inst.config / "install.json").unlink()
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        step = self.first_step(out)
        self.assertIn("contain exactly", step)
        self.follow_record_step(inst, step)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(inst.end_state()["layout"], "legacy")

    def test_damaged_destination_step_restores_that_destination(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:cleaning").returncode, -signal.SIGKILL)
        (inst.data / "history.json").write_text('["damaged"]')
        out = self.refused_unchanged(inst, "--resume", needle="history.json")
        step = self.first_step(out)
        self.assertNotIn("install.json", step)
        src, dst = inst.checkout / "history.json", inst.data / "history.json"
        self.assertIn(f"copy {src} over {dst}", step)
        shutil.copy2(src, dst)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(src.exists())
        self.assertEqual(json.loads(dst.read_text()), [{"text": "hello"}])

    def test_abandon_step_requires_checking_every_listed_source(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:copying").returncode, -signal.SIGKILL)
        inst.config.mkdir(parents=True, exist_ok=True)
        (inst.config / "install.json").write_text(json.dumps({"schema": 1, "checkout": str(inst.checkout)}))
        out = self.refused_unchanged(inst, "--resume", needle="install.json")
        self.assertIn("src_sha256", out.stderr)
        self.assertIn(str(inst.state / "migration.json"), out.stderr)

    def test_positive_controls_still_resume_both_ways(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="install-json").returncode, -signal.SIGKILL)
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "xdg")
        self.assertEqual(inst.migrate("--rollback", crash="install-json").returncode, -signal.SIGKILL)
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertEqual(inst.end_state()["layout"], "legacy")


class CanonicalIdentityTests(_Base):
    """Round 2, finding 3906 (R2-B2): the journal binds physical paths, recorded absolute."""

    def linked_install(self):
        inst = self.install()
        old, new, link = (inst.root / n for n in ("data-original", "data-new", "data-link"))
        old.mkdir()
        new.mkdir()
        link.symlink_to(old, target_is_directory=True)
        inst.env["ASSIST_DATA_DIR"] = str(link)
        return inst, old, new, link

    def test_retargeted_symlink_is_refused_on_resume(self):
        inst, old, new, link = self.linked_install()
        self.assertEqual(inst.migrate(crash="marker:committed").returncode, -signal.SIGKILL)
        self.assertEqual(inst.marker()["data_dir"], str(old.resolve()))
        link.unlink()
        link.symlink_to(new, target_is_directory=True)
        before = inst.sources()
        out = inst.migrate("--resume")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("data_dir", out.stderr)
        self.assertEqual(inst.sources(), before)
        self.assertEqual(list(new.iterdir()), [])

    def test_retargeted_symlink_is_refused_on_rollback(self):
        inst, old, new, link = self.linked_install()
        self.assertEqual(inst.migrate().returncode, 0)
        link.unlink()
        link.symlink_to(new, target_is_directory=True)
        out = inst.migrate("--rollback")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("data_dir", out.stderr)

    def test_unchanged_symlink_resumes_and_rolls_back(self):
        inst, old, new, link = self.linked_install()
        before = inst.sources()
        self.assertEqual(inst.migrate(crash="marker:committed").returncode, -signal.SIGKILL)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue((old / "history.json").exists())
        out = inst.migrate("--rollback")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(inst.sources(), before)

    def test_relative_token_override_resumes_and_rolls_back(self):
        inst = self.install()
        token = inst.root / "external-token"
        token.write_text("scratch-retained-token\n")
        inst.env["ASSIST_AUTH_TOKEN_PATH"] = os.path.relpath(token, ROOT)  # the wrapper's cwd is ROOT
        self.assertEqual(inst.migrate(crash="marker:copying").returncode, -signal.SIGKILL)
        self.assertEqual(inst.marker()["token_path"], str(token.resolve()))
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = inst.migrate("--rollback")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(token.read_text(), "scratch-retained-token\n")

    def test_relative_root_override_is_recorded_absolute(self):
        inst = self.install()
        inst.env["ASSIST_DATA_DIR"] = os.path.relpath(inst.root / "rel-data", ROOT)
        self.assertEqual(inst.migrate(crash="marker:copying").returncode, -signal.SIGKILL)
        self.assertEqual(inst.marker()["data_dir"], str((inst.root / "rel-data").resolve()))
        self.assertEqual(inst.migrate("--resume").returncode, 0)


class ProvenanceReceiptRootTests(_Base):
    """Round 3, finding 3924 (R3-B1): the moved receipt verifies through the owning checkout's own module."""

    def verify(self, inst, receipt):
        env = dict(inst.env)
        env.pop("ASSIST_LAUNCH_PROVENANCE_ROOT", None)
        code = ("import sys; sys.path.insert(0, %r); from shared import launch_provenance as p;"
                "raise SystemExit(p.main(sys.argv[1:]))") % str(inst.checkout)
        return subprocess.run([PY, "-c", code, "verify-initialization", "--assist-home", str(inst.checkout),
                               "--receipt", str(receipt), "--require-empty",
                               "--require-coverage", "post_epoch_created_only"],
                              env=env, capture_output=True, text=True, timeout=60)

    def round_trip(self, inst, data_spelling):
        """Verify before migration, after an interrupted then resumed run, and after rollback."""
        out = self.verify(inst, inst.checkout / RECEIPT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(inst.migrate(crash="marker:cleaning").returncode, -signal.SIGKILL)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.verify(inst, data_spelling / RECEIPT)
        self.assertEqual(out.returncode, 0, out.stderr)
        out = inst.migrate("--rollback")
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self.verify(inst, inst.checkout / RECEIPT)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_unchanged_symlinked_data_home_keeps_a_valid_receipt(self):
        inst = self.install(code=True)
        link = inst.root / "xdg-data-link"
        link.symlink_to(inst.root / "xdg-data", target_is_directory=True)
        (inst.root / "xdg-data").mkdir()
        inst.env["XDG_DATA_HOME"] = str(link)
        self.round_trip(inst, link / "drift-assist-data")

    def test_plain_data_home_control(self):
        inst = self.install(code=True)
        self.round_trip(inst, inst.data)

    def test_a_receipt_naming_a_different_root_is_still_refused(self):
        inst = self.install(code=True)
        receipt = inst.checkout / RECEIPT
        other = inst.root / "elsewhere"
        shutil.copytree(inst.checkout / STORE, other / STORE)
        value = json.loads(receipt.read_text())
        value["provenance_root"] = str(other / STORE)
        receipt.write_text(json.dumps(value))
        out = self.verify(inst, receipt)
        self.assertEqual(out.returncode, 2)
        self.assertIn("provenance_initialization_root_mismatch", out.stderr)


# ---------------------------------------------------------------------------
# The transaction
# ---------------------------------------------------------------------------


class TransactionTests(_Base):
    def test_forward_end_state(self):
        inst = self.install()
        before = inst.sources()
        out = inst.migrate()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        end = inst.end_state()
        self.assertEqual(end["layout"], "xdg")
        self.assertEqual(end["token"], b"scratch-token-value\n")
        self.assertEqual(end["receipt_root"], str(inst.data / STORE))
        self.assertEqual(end["marker"], "done")
        self.assertEqual(end["parts"], [])
        self.assertEqual(inst.sources(), {})  # every source removed after commit
        self.assertEqual(_hash(inst.config / "server.env"), before[".env"])
        self.assertEqual(_hash(inst.data / STORE), before[STORE])
        pointer = (inst.checkout / ".assist-state-moved").read_text()
        self.assertIn(str(inst.config), pointer)
        self.assertEqual((inst.config / "auth_token").stat().st_mode & 0o777, 0o600)

    def test_sources_untouched_until_committed(self):
        inst = self.install()
        before = inst.sources()
        for point in ("marker:copying", "marker:validating", "marker:publishing", "marker:committed"):
            out = inst.migrate(crash=point) if inst.marker() is None else inst.migrate("--resume", crash=point)
            self.assertEqual(out.returncode, -signal.SIGKILL, point)
            self.assertEqual(inst.sources(), before, point)
            self.assertFalse((inst.config / "install.json").exists(), point)

    def test_invalid_marker_stops_with_recovery_required_and_no_action(self):
        inst = self.install()
        inst.migrate(crash="marker:publishing")
        (inst.state / "migration.json").write_text("{torn")
        before = {p: _hash(p) for p in inst.root.rglob("*") if p.is_file()}
        for args in (("--resume",), ("--rollback",), ()):
            out = inst.migrate(*args)
            self.assertNotEqual(out.returncode, 0, args)
            self.assertIn("recovery required", out.stdout + out.stderr, args)
            self.assertIn(str(inst.state / "migration.json"), out.stdout + out.stderr)
        after = {p: _hash(p) for p in inst.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((inst.config / "auth_token").exists())

    def test_resume_compares_hashes_never_existence(self):
        inst = self.install()
        out = inst.migrate(crash="published:settings.json")
        self.assertEqual(out.returncode, -signal.SIGKILL)
        (inst.config / "settings.json").write_text('{"tampered": true}')
        out = inst.migrate("--resume")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("settings.json", out.stdout + out.stderr)
        self.assertIn("hash", out.stdout + out.stderr)
        self.assertFalse((inst.config / "install.json").exists())

    def test_crash_at_each_boundary_resumes_to_same_end_state(self):
        reference = self.install()
        trace = self.tmp / "forward.trace"
        self.assertEqual(reference.migrate(trace=trace).returncode, 0)
        forward_end = reference.end_state()
        forward_points = trace.read_text().split()
        self.assertGreater(len(forward_points), 20)
        rtrace = self.tmp / "reverse.trace"
        self.assertEqual(reference.migrate("--rollback", trace=rtrace).returncode, 0)
        reverse_end = reference.end_state()
        reverse_points = rtrace.read_text().split()
        self.assertEqual(reverse_end["layout"], "legacy")

        for point in forward_points:
            with self.subTest(direction="forward", point=point):
                inst = self.install()
                before = inst.sources()
                out = inst.migrate(crash=point)
                self.assertEqual(out.returncode, -signal.SIGKILL, out.stderr)
                if "install-json" not in point and not point.startswith(("marker:cleaning", "cleaned:", "marker:done", "pointer")):
                    self.assertEqual(inst.sources(), before)
                out = inst.finish()
                self.assertTrue(out is None or out.returncode == 0, out and out.stdout + out.stderr)
                self.assertEqual(_normalise(inst.end_state(), inst), _normalise(forward_end, reference))
                shutil.rmtree(inst.root)

        for point in reverse_points:
            with self.subTest(direction="reverse", point=point):
                inst = self.install()
                self.assertEqual(inst.migrate().returncode, 0)
                out = inst.migrate("--rollback", crash=point)
                self.assertEqual(out.returncode, -signal.SIGKILL, out.stderr)
                out = inst.finish()
                self.assertTrue(out is None or out.returncode == 0, out and out.stdout + out.stderr)
                self.assertEqual(_normalise(inst.end_state(), inst), _normalise(reverse_end, reference))
                shutil.rmtree(inst.root)

    def test_receipt_transform_validates_and_inverse_round_trips_bytes(self):
        inst = self.install()
        original = (inst.checkout / RECEIPT).read_bytes()
        sys.path.insert(0, str(ROOT))
        from shared import launch_provenance as p

        moved = p.transform_receipt(original, inst.data / STORE)
        self.assertEqual(json.loads(moved)["provenance_root"], str(inst.data / STORE))
        self.assertEqual(p.transform_receipt(moved, inst.checkout / STORE), original)
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(inst.migrate("--rollback").returncode, 0)
        self.assertEqual((inst.checkout / RECEIPT).read_bytes(), original)
        self.assertFalse((inst.state / "migration-receipt.orig.json").exists())

    def test_verify_staged_validates_without_selecting_layout_or_state_imports(self):
        inst = self.install()
        staged = self.tmp / "staged"
        shutil.copytree(inst.checkout / STORE, staged / STORE)
        receipt = (inst.checkout / RECEIPT).read_bytes()
        probe = r"""
import json, sys
sys.path.insert(0, %(root)r)
from pathlib import Path
from shared import launch_provenance as p
store = Path(%(staged)r)
moved = p.transform_receipt(%(receipt)r, Path(%(final)r))
p.verify_staged(store, moved, Path(%(final)r), assist_home=%(home)r)
errors = []
for bad_root, bad_receipt in ((Path("/elsewhere"), moved), (Path(%(final)r), %(receipt)r)):
    try:
        p.verify_staged(store, bad_receipt, bad_root, assist_home=%(home)r)
    except p.ProvenanceError as exc:
        errors.append(exc.code)
(store / "epoch.json").chmod(0o600)
data = json.loads((store / "epoch.json").read_text()); data["epoch"] = "0" * 32
(store / "epoch.json").write_text(json.dumps(data))
try:
    p.verify_staged(store, moved, Path(%(final)r), assist_home=%(home)r)
except p.ProvenanceError as exc:
    errors.append(exc.code)
print(json.dumps({"errors": errors, "state": "shared.state" in sys.modules,
                  "tmux": "shared.tmux" in sys.modules}))
""" % {"root": str(ROOT), "staged": str(staged / STORE), "receipt": receipt,
       "final": str(inst.data / STORE), "home": str(inst.checkout)}
        out = subprocess.run([PY, "-c", probe], env=inst.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(len(result["errors"]), 3, result)
        self.assertFalse(result["state"])
        self.assertFalse(result["tmux"])
        self.assertFalse((inst.config / "install.json").exists())
        self.assertFalse(inst.data.exists())


def _normalise(end, inst):
    """Make two installs' end states comparable: their roots differ."""
    def rel(value):
        return value.replace(str(inst.root), "<root>") if isinstance(value, str) else value
    out = dict(end)
    out["receipt_root"] = rel(end["receipt_root"])
    out["epoch"] = None  # each scratch install has its own epoch; compared per install below
    out["tree"] = {k: v for k, v in end["tree"].items() if not k.endswith((STORE, RECEIPT))}
    return out


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


class RollbackTests(_Base):
    def test_rollback_preserves_post_migration_change(self):
        inst = self.install()
        epoch = (inst.checkout / STORE / "epoch.json").read_bytes()
        self.assertEqual(inst.migrate().returncode, 0)
        (inst.config / "settings.json").write_text('{"changed": "after migration"}')
        out = inst.migrate("--rollback")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual((inst.checkout / "settings.json").read_text(), '{"changed": "after migration"}')
        self.assertFalse((inst.config / "install.json").exists())
        self.assertFalse((inst.config / "settings.json").exists())
        self.assertEqual((inst.checkout / STORE / "epoch.json").read_bytes(), epoch)
        self.assertEqual((inst.checkout / "auth_token").read_text(), "scratch-token-value\n")

    def test_rollback_conflict_reports_and_stops_no_force_flag(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        planted = inst.checkout / "settings.json"
        planted.write_text('{"planted": true}')
        out = inst.migrate("--rollback")
        self.assertNotEqual(out.returncode, 0)
        text = out.stdout + out.stderr
        self.assertIn(str(planted), text)
        self.assertIn(_hash(planted), text)
        self.assertIn(_hash(inst.config / "settings.json"), text)
        self.assertEqual(inst.marker()["phase"], "validating")
        self.assertEqual(inst.marker()["direction"], "reverse")
        self.assertTrue((inst.config / "install.json").exists())  # still the XDG layout
        self.assertNotEqual(inst.migrate("--rollback", "--force").returncode, 0)
        self.assertIn("unrecognized arguments", inst.migrate("--force").stderr + inst.migrate("--force").stdout)
        planted.rename(inst.root / "set-aside.json")
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(json.loads((inst.checkout / "settings.json").read_text()), {"ui": {"theme": "x"}})

    def test_completed_rollback_reported_by_doctor_and_pointer(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertIn("completed", inst.assist("doctor").stdout)
        self.assertEqual(inst.migrate("--rollback").returncode, 0)
        doctor = inst.assist("doctor").stdout
        self.assertIn("rolled back", doctor)
        self.assertNotIn("INCOMPLETE", doctor)
        self.assertIn("rolled back", (inst.checkout / ".assist-state-moved").read_text())
        marker = inst.marker()
        self.assertEqual((marker["phase"], marker["direction"]), ("done", "reverse"))

    def test_interrupted_rollback_resumes(self):
        inst = self.install()
        self.assertEqual(inst.migrate().returncode, 0)
        self.assertEqual(inst.migrate("--rollback", crash="marker:publishing").returncode, -signal.SIGKILL)
        self.assertNotEqual(inst.assist("ls").returncode, 0)
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(inst.end_state()["layout"], "legacy")

    def test_doctor_reports_part_leftovers_and_missing_install_json(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="copied:history.json").returncode, -signal.SIGKILL)
        doctor = inst.assist("doctor").stdout
        self.assertIn(".part leftover", doctor)
        self.assertIn("history.json.part", doctor)
        self.assertEqual(inst.migrate("--resume").returncode, 0)
        self.assertNotIn(".part leftover", inst.assist("doctor").stdout)
        (inst.config / "install.json").unlink()
        doctor = inst.assist("doctor").stdout
        self.assertIn("install.json is missing", doctor)

    def test_cleaning_leaves_differing_source_and_doctor_reports_it(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:cleaning").returncode, -signal.SIGKILL)
        (inst.checkout / "history.json").write_text('["edited after commit"]')
        out = inst.migrate("--resume")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("history.json", out.stdout)
        self.assertTrue((inst.checkout / "history.json").exists())
        self.assertFalse((inst.checkout / "drafts.json").exists())
        doctor = inst.assist("doctor").stdout
        self.assertIn("history.json present in both", doctor)
        self.assertIn("DIFFERENT", doctor)


# ---------------------------------------------------------------------------
# Locking and the other verbs
# ---------------------------------------------------------------------------


class ConcurrencyTests(_Base):
    def test_server_start_racing_migrator_is_refused_by_lock(self):
        inst = self.install()
        release = self.tmp / "release"
        env = dict(inst.env, ASSIST_MIGRATE_TEST_PAUSE="marker:copying", ASSIST_MIGRATE_TEST_RELEASE=str(release))
        migrator = subprocess.Popen([PY, str(ROOT / "bin" / "assist"), "migrate-state"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(100):
                if (inst.marker() or {}).get("phase") == "copying":
                    break
                time.sleep(0.1)
            self.assertEqual(inst.marker()["phase"], "copying")
            # The exclusive lock is held: a server start is refused by it.
            out = subprocess.run([PY, str(ROOT / "serve.py"), "--port", str(_free_port())], env=inst.env,
                                 capture_output=True, text=True, timeout=30)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("migration_in_progress", out.stdout + out.stderr)
            lock = inst.state / "migrate.lock"
            fd = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            release.write_text("go")
            stdout, stderr = migrator.communicate(timeout=60)
        self.assertEqual(migrator.returncode, 0, stdout + stderr)

    def test_other_verbs_refuse_mid_transaction(self):
        inst = self.install()
        self.assertEqual(inst.migrate(crash="marker:validating").returncode, -signal.SIGKILL)
        for verb in (["ls"], ["devices"], ["start"], ["send", "x", "hi"], ["pair"]):
            out = inst.assist(*verb)
            self.assertNotEqual(out.returncode, 0, verb)
            self.assertIn("migration_in_progress", out.stdout + out.stderr, verb)
        doctor = inst.assist("doctor")
        self.assertIn("INCOMPLETE", doctor.stdout)
        self.assertIn("validating", doctor.stdout)
        # A new run without --resume is refused too: one transaction at a time.
        out = inst.migrate()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--resume", out.stdout + out.stderr)


# ---------------------------------------------------------------------------
# End to end through the real wrapper and a real server
# ---------------------------------------------------------------------------


class WrapperTests(_Base):
    def test_wrapper_session_verbs_across_migration_and_rollback(self):
        inst = self.install(code=True)
        tmux_dir = Path(tempfile.mkdtemp(prefix="t621m", dir="/tmp"))
        self.addCleanup(shutil.rmtree, tmux_dir, True)
        token = self.tmp / "explicit-token"
        token.write_text("explicit-token-value\n")
        token.chmod(0o600)
        port = _free_port()
        env = dict(inst.env, TMUX_TMPDIR=str(tmux_dir), ASSIST_PORT=str(port),
                   ASSIST_AUTH_TOKEN_PATH=str(token), ASSIST_PID_FILE=str(self.tmp / "assist.pid"),
                   ASSIST_LOG_FILE=str(self.tmp / "assist.log"))
        env.pop("ASSIST_HOME")
        env.pop("ASSIST_VENV_REEXEC")
        (inst.checkout / ".env").write_text("# port and token come from the environment\n")
        wrapper = inst.checkout / "bin" / "assist"
        tmux = ["tmux", "-S", str(tmux_dir / "sock")]

        def run(*args, ok=True):
            out = subprocess.run([str(inst.checkout / ".venv" / "bin" / "python3"), str(wrapper), *args],
                                 env=env, capture_output=True, text=True, timeout=90)
            if ok:
                self.assertEqual(out.returncode, 0, f"{args}: {out.stdout}{out.stderr}")
            return out

        env["TMUX_TMPDIR"] = str(tmux_dir)
        subprocess.run(["tmux", "new-session", "-d", "-s", "mig", "-x", "100", "-y", "20"],
                       env={**env, "TMUX_TMPDIR": str(tmux_dir)}, check=True)
        self.addCleanup(subprocess.run, ["tmux", "kill-server"], env={**env, "TMUX_TMPDIR": str(tmux_dir)})
        tmux_pid = subprocess.run(["tmux", "display-message", "-p", "#{pid}"], env=env,
                                  capture_output=True, text=True).stdout.strip()

        def session_round(word):
            run("start")
            listing = json.loads(run("ls", "--json").stdout)
            self.assertIn("mig", json.dumps(listing))
            run("send", "mig", f"echo {word}", "--enter")
            for _ in range(30):
                if word in run("view", "mig").stdout:
                    break
                time.sleep(0.3)
            self.assertIn(word, run("view", "mig").stdout)
            run("stop")
            # `assist stop` signals the server and returns; the server holds the
            # shared layout lock until it has actually exited.
            lock = inst.state / "migrate.lock"
            for _ in range(100):
                fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.1)
                finally:
                    os.close(fd)

        epoch = (inst.checkout / STORE / "epoch.json").read_bytes()
        session_round("before-migration")

        out = run("migrate-state", ok=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)  # its own invocation is not refused
        self.assertEqual(json.loads((inst.config / "install.json").read_text())["checkout"],
                         str(inst.checkout.resolve()))
        session_round("after-migration")
        self.assertEqual((inst.data / STORE / "epoch.json").read_bytes(), epoch)

        out = run("migrate-state", "--rollback", ok=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        session_round("after-rollback")
        self.assertEqual((inst.checkout / STORE / "epoch.json").read_bytes(), epoch)
        self.assertEqual(subprocess.run(["tmux", "display-message", "-p", "#{pid}"], env=env,
                                        capture_output=True, text=True).stdout.strip(), tmux_pid)
        self.assertEqual(token.read_text(), "explicit-token-value\n")


if __name__ == "__main__":
    unittest.main()
