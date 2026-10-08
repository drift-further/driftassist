"""Execute the Bash installer with isolated state and a dependency-install fixture."""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class InstallWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.home = self.base / "Assist's $local source"
        self.home.mkdir()
        for name in ('install.sh', 'serve.py', 'pyproject.toml', 'requirements.lock', 'env.example',
                     'roster.example.toml', 'bin/assist', 'bin/statusline.sh', 'assist-ctl',
                     'cli/__init__.py', 'shared/state.py', 'routes/__init__.py', 'index.html'):
            destination = self.home / name; destination.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        pip = self.home / '.venv/bin/pip'; pip.parent.mkdir(parents=True)
        pip.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$ASSIST_TEST_PIP_LOG"\n')
        pip.chmod(0o700)
        self.user = self.base / 'user'; self.user.mkdir()
        self.config = self.base / 'config/drift-assist'; self.config.mkdir(parents=True)
        self.bin = self.base / 'commands'; self.bin.mkdir()
        self.log = self.base / 'pip.log'
        self.env = {**os.environ, 'HOME': str(self.user), 'XDG_CONFIG_HOME': str(self.config.parent),
                    'XDG_BIN_HOME': str(self.bin), 'ASSIST_TEST_PIP_LOG': str(self.log),
                    'ASSIST_INSTALL_SERVICE': '1', 'ASSIST_REPLACE_STATUSLINE': '1'}

    def run_install(self, *args):
        return subprocess.run(['bash', str(self.home / 'install.sh'), *args], env=self.env,
                              input='', text=True, capture_output=True, timeout=20)

    def test_check_and_help_do_not_write(self):
        for option in ('--check', '--help'):
            result = self.run_install(option)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(self.log.exists()); self.assertFalse((self.home / '.env').exists())
            self.assertFalse((self.config / 'config.env').exists())

    def test_rerun_preserves_configuration_and_runtime_files(self):
        marker = self.config / 'config.env'; marker.write_text('ASSIST_PORT=9191\nASSIST_HOME=/old\n# keep me\n')
        settings = self.user / '.claude/settings.json'; settings.parent.mkdir()
        settings.write_text('{"statusLine":{"command":"keep-this"},"custom":true}')
        originals = {'.env': 'ASSIST_PORT=9191\n', 'auth_token': 'test-only-token\n',
                     'settings.json': '{"custom":true}', 'drafts.json': '{"one":"draft"}'}
        for name, content in originals.items(): (self.home / name).write_text(content)
        roster = self.config / 'roster.toml'; roster.write_text('# custom roster\n')
        for _ in range(2):
            result = self.run_install('--non-interactive', '--no-integrations')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('ASSIST_PORT=9191', marker.read_text()); self.assertIn('# keep me', marker.read_text())
        self.assertEqual(marker.read_text().count('ASSIST_HOME='), 1)
        # The recorded quoted path works for Bash and the CLI assignment parser.
        result = subprocess.run(['bash', '-c', 'source "$1"; printf "%s" "$ASSIST_HOME"', '_', str(marker)],
                                env=self.env, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout, str(self.home))
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(settings.read_text(), '{"statusLine":{"command":"keep-this"},"custom":true}')
        self.assertFalse((settings.parent / 'state').exists())
        for name, content in originals.items(): self.assertEqual((self.home / name).read_text(), content)
        self.assertEqual(roster.read_text(), '# custom roster\n')
        self.assertEqual((self.bin / 'assist').resolve(), self.home / 'bin/assist')
        self.assertIn('--require-hashes', self.log.read_text())

    def test_fresh_install_creates_private_env(self):
        result = self.run_install('--non-interactive', '--no-integrations')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(stat.S_IMODE((self.home / '.env').stat().st_mode), 0o600)

    def test_foreign_command_needs_explicit_replacement_before_any_writes(self):
        target = self.bin / 'assist'; target.write_text('foreign command')
        result = self.run_install('--non-interactive', '--no-integrations')
        self.assertNotEqual(result.returncode, 0); self.assertIn('--replace-command', result.stderr)
        self.assertEqual(target.read_text(), 'foreign command'); self.assertFalse(self.log.exists())
        result = self.run_install('--non-interactive', '--no-integrations', '--replace-command')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(target.is_symlink())

    def test_dependency_repair_does_not_touch_install_config_or_foreign_command(self):
        target = self.bin / 'assist'; target.write_text('foreign command')
        result = self.run_install('--dependencies-only')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.log.exists()); self.assertFalse((self.config / 'config.env').exists())
        self.assertFalse((self.home / '.env').exists()); self.assertEqual(target.read_text(), 'foreign command')

    def test_unknown_option_fails_without_writing(self):
        self.assertEqual(self.run_install('--typo').returncode, 2)
        self.assertFalse(self.log.exists())
