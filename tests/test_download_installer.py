"""Run the standalone installer against real isolated Git sources."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from distribution.prepare_installer import render

ROOT = Path(__file__).resolve().parents[1]


class DownloadInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source with 'quotes' and @BRANCH@ spaces"
        self.source.mkdir()
        self.git(self.source, 'init', '-b', 'main')
        # Only the final installer is a fixture; Git downloads and pinning are real.
        (self.source / 'bin').mkdir()
        for name in ('serve.py', 'bin/assist', 'requirements.lock'):
            (self.source / name).write_text('# fixture\n')
        (self.source / '.gitignore').write_text('.bootstrap-ran\n.env\nauth_token\nsettings.json\ndrafts.json\n')
        (self.source / 'install.sh').write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$@" >> "$(dirname "$0")/.bootstrap-ran"\nexit "${ASSIST_TEST_INSTALL_EXIT:-0}"\n')
        self.commit('Version one')
        self.revision = self.git(self.source, 'rev-parse', 'HEAD')
        self.prefix = self.base / "installed '$source' with spaces"
        self.script = self.base / 'download-install.sh'
        self.script.write_text(render(self.revision, str(self.source)))
        self.user = self.base / 'isolated-home'; self.user.mkdir()
        self.env = {**os.environ, 'HOME': str(self.user), 'XDG_DATA_HOME': str(self.user / 'data'),
                    'XDG_CONFIG_HOME': str(self.user / 'config'), 'XDG_BIN_HOME': str(self.user / 'bin')}
        # Avoid invoking the user's Git hooks/templates or writing their Git config.
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(self, home, *args):
        result = subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, message):
        self.git(self.source, 'add', '.')
        self.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', message)

    def run_installer(self, *args):
        return subprocess.run(['bash', str(self.script), '--prefix', str(self.prefix), *args],
                              env=self.env, input='', capture_output=True, text=True, timeout=20)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_clean_staging(self):
        self.assertEqual(list(self.base.glob('.assist-download.*')), [])
        self.assertFalse((self.base / ('.' + self.prefix.name + '.assist-install-lock')).exists())

    def test_download_pins_reviewed_commit_and_retains_tracking_branch(self):
        (self.source / 'newer.txt').write_text('not in the reviewed version')
        self.commit('Unreviewed later version')
        self.assert_ok(self.run_installer('--non-interactive', '--no-integrations'))
        self.assertEqual(self.git(self.prefix, 'rev-parse', 'HEAD'), self.revision)
        self.assertEqual(self.git(self.prefix, 'symbolic-ref', '--short', 'HEAD'), 'main')
        self.assertEqual(self.git(self.prefix, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/main')
        self.assertFalse((self.prefix / 'newer.txt').exists())
        self.assertEqual((self.prefix / '.bootstrap-ran').read_text().splitlines(), ['--non-interactive', '--no-integrations'])
        self.assert_clean_staging()

    def test_rerun_preserves_local_source_and_runtime_without_pulling(self):
        self.assert_ok(self.run_installer())
        original = {'.env': 'test env', 'auth_token': 'fixture only', 'settings.json': '{}', 'drafts.json': '{"draft":"keep"}', 'serve.py': '# local edit'}
        for name, text in original.items(): (self.prefix / name).write_text(text)
        (self.source / 'newer.txt').write_text('new upstream')
        self.commit('Upstream changed')
        self.assert_ok(self.run_installer('--non-interactive'))
        self.assertEqual(self.git(self.prefix, 'rev-parse', 'HEAD'), self.revision)
        for name, text in original.items(): self.assertEqual((self.prefix / name).read_text(), text)
        self.assertFalse((self.prefix / 'newer.txt').exists())

    def test_existing_unrelated_directory_and_symlink_are_untouched(self):
        self.prefix.mkdir(); (self.prefix / 'keep.txt').write_text('keep')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.prefix / 'keep.txt').read_text(), 'keep')
        link = self.base / 'link'; link.symlink_to(self.prefix, target_is_directory=True)
        original = self.prefix; self.prefix = link
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0); self.assertIn('symlink', result.stderr)
        self.assertEqual((original / 'keep.txt').read_text(), 'keep')

    def test_wrong_revision_never_creates_destination(self):
        self.script.write_text(render('f' * 40, str(self.source)))
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.prefix.exists()); self.assert_clean_staging()

    def test_download_failure_never_creates_destination(self):
        self.script.write_text(render(self.revision, str(self.base / 'missing.git')))
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.prefix.exists()); self.assert_clean_staging()

    def test_install_failure_keeps_verified_source_for_rerun(self):
        self.env['ASSIST_TEST_INSTALL_EXIT'] = '7'
        self.assertEqual(self.run_installer().returncode, 7)
        self.assertEqual(self.git(self.prefix, 'rev-parse', 'HEAD'), self.revision)
        self.assert_clean_staging()
        self.env['ASSIST_TEST_INSTALL_EXIT'] = '0'
        self.assert_ok(self.run_installer('--dependencies-only'))

    def test_check_and_help_have_no_filesystem_side_effects(self):
        for option in ('--check', '--help'):
            self.assert_ok(self.run_installer(option))
            self.assertFalse(self.prefix.exists()); self.assert_clean_staging()

    def test_an_existing_install_lock_prevents_another_download(self):
        lock = self.base / ('.' + self.prefix.name + '.assist-install-lock'); lock.mkdir()
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.exists()); self.assertFalse(self.prefix.exists())

    def test_render_requires_full_revision_and_cli_refuses_artifact_overwrite(self):
        for bad in ('main', 'v0.1.0', '--help', '$(id)', 'a' * 39):
            with self.assertRaises(ValueError): render(bad)
        output = self.base / 'prepared.sh'
        command = [str(ROOT / '.venv/bin/python3'), str(ROOT / 'distribution/prepare_installer.py'),
                   '--revision', self.revision, '--output', str(output)]
        subprocess.run(command, check=True, capture_output=True, timeout=10)
        self.assertIn(self.revision, output.read_text())
        result = subprocess.run(command, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
