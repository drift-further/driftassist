"""Real isolated Git sources exercise update ordering, preservation and recovery."""
import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout
from types import SimpleNamespace

from flask import Flask
from shared.updates import Updater, UpdateError, UpdateManager
from routes import updates
from cli import update as update_cli
from cli import config as cli_config, proc
from routes import settings


class InstalledRestartTests(unittest.TestCase):
    def test_builtin_restart_aliases_use_this_checkout_and_custom_command_is_preserved(self):
        app = Flask(__name__)
        home = Path(settings.state.DATA_DIR).resolve()
        with app.app_context(), patch.object(settings.subprocess, 'Popen') as spawn:
            for command in ('assist restart', 'karen assist restart'):
                response = settings._restart_server_effect(command)
                self.assertTrue(response.get_json()['ok'])
                self.assertEqual(spawn.call_args.args[0], [
                    str(home / '.venv' / 'bin' / 'python3'),
                    '-m', 'cli.update', '--restart-installed',
                ])
                self.assertEqual(spawn.call_args.kwargs['cwd'], home)
            settings._restart_server_effect('/usr/bin/custom-restart --configured')
            self.assertEqual(spawn.call_args.args[0], ['/usr/bin/custom-restart', '--configured'])
            self.assertNotIn('cwd', spawn.call_args.kwargs)

    def test_installed_restart_checks_home_before_controller_or_service(self):
        home = Path(update_cli.__file__).resolve().parent.parent
        config = SimpleNamespace(home=home)
        with patch.object(cli_config, 'resolve', return_value=config) as resolve, patch.object(proc, 'restart', return_value=0) as restart:
            self.assertEqual(update_cli.restart_installed(), 0)
            resolve.assert_called_once_with(script_path=home / 'bin' / 'assist', activation_expected_home=home)
            restart.assert_called_once_with(config)
        with patch.object(cli_config, 'resolve', side_effect=cli_config.ConfigError('home mismatch')), patch.object(proc, 'restart') as restart, patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(update_cli.restart_installed(), 1)
            restart.assert_not_called()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / 'origin.git'; self.source = self.base / 'source'; self.home = self.base / 'installed with spaces'
        self.git(self.base, 'init', '--bare', str(self.origin))
        self.git(self.base, 'init', '-b', 'main', str(self.source))
        (self.source / 'pyproject.toml').write_text('[project]\nversion = "0.1.0"\n')
        (self.source / 'requirements.lock').write_text('# Empty fixture dependency set\n')
        (self.source / 'app.txt').write_text('version one')
        (self.source / '.gitignore').write_text('.venv/\n.env\nauth_token\nsettings.json\ndrafts.json\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Initial fixture')
        self.git(self.source, 'remote', 'add', 'origin', str(self.origin))
        self.git(self.source, 'push', '-u', 'origin', 'main')
        self.git(self.base, 'clone', '-b', 'main', str(self.origin), str(self.home))
        self.before = self.git(self.home, 'rev-parse', 'HEAD')
        self.updater = Updater(self.home, self.base / 'state', running_commit=self.before)

    def git(self, home, *args):
        result = subprocess.run(['git', '-C', str(home), *args], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def upstream_change(self):
        (self.source / 'app.txt').write_text('version two')
        (self.source / 'pyproject.toml').write_text('[project]\nversion = "0.2.0"\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Update fixture')
        self.git(self.source, 'push', 'origin', 'main')
        return self.git(self.source, 'rev-parse', 'HEAD')

    def test_real_git_and_pip_update_preserves_runtime_and_requires_restart(self):
        expected = self.upstream_change()
        subprocess.run([sys.executable, '-m', 'venv', str(self.home / '.venv')], check=True, timeout=30)
        originals = {'.env': 'ASSIST_PORT=9191', 'auth_token': 'test-secret-only', 'settings.json': '{"a":1}', 'drafts.json': '{"a":"draft"}'}
        for name, content in originals.items(): (self.home / name).write_text(content)
        status = self.updater.check()
        self.assertTrue(status['available']); self.assertEqual(status['latest_commit'], expected)
        result = self.updater.apply(expected)
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), expected)
        self.assertEqual(result['phase'], 'installed'); self.assertTrue(result['restart_required'])
        self.assertEqual(result['running_version'], '0.1.0'); self.assertEqual(result['version'], '0.2.0')
        for name, content in originals.items(): self.assertEqual((self.home / name).read_text(), content)
        restarted = Updater(self.home, self.base / 'state', running_commit=expected)
        self.assertFalse(restarted.snapshot()['restart_required'])

    def test_manual_fetch_during_check_cannot_redirect_the_update(self):
        self.git(self.source, 'checkout', '-b', 'feature')
        (self.source / 'feature.txt').write_text('not the upstream')
        self.git(self.source, 'add', '.')
        self.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Feature')
        self.git(self.source, 'push', 'origin', 'feature')
        self.git(self.source, 'checkout', 'main')
        self.upstream_change()
        main_tip = self.git(self.source, 'rev-parse', 'main')
        feature_tip = self.git(self.source, 'rev-parse', 'feature')
        original = self.updater._git
        def git_with_manual_fetch(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[0] == 'fetch':
                self.git(self.home, 'fetch', 'origin', 'feature')  # rewrites FETCH_HEAD mid-check
            return result
        with patch.object(self.updater, '_git', side_effect=git_with_manual_fetch):
            status = self.updater.check()
        self.assertEqual(status['latest_commit'], main_tip)
        self.assertNotEqual(status['latest_commit'], feature_tip)
        self.assertTrue(status['available'])

    def test_restart_flag_clears_only_when_a_server_runs_the_installed_commit(self):
        installed = 'a' * 40
        self.updater._save(phase='installed', installed_commit=installed, restart_required=True)
        cli_view = Updater(self.home, self.base / 'state')
        self.assertTrue(cli_view.snapshot()['restart_required'])
        old_server = Updater(self.home, self.base / 'state', running_commit=self.before)
        old_server.settle_restart()
        self.assertTrue(cli_view.snapshot()['restart_required'])
        self.updater._save(phase='repair_required')
        Updater(self.home, self.base / 'state', running_commit=installed).settle_restart()
        self.assertTrue(cli_view.snapshot()['restart_required'])
        self.updater._save(phase='installed')
        Updater(self.home, self.base / 'state', running_commit=installed).settle_restart()
        self.assertFalse(cli_view.snapshot()['restart_required'])

    def test_snapshot_and_ui_reads_never_fetch(self):
        self.updater.check()
        with patch.object(self.updater, '_git', side_effect=AssertionError('Git on cached read')):
            self.assertEqual(self.updater.snapshot()['phase'], 'ready')

    def test_dirty_checkout_blocks_apply_and_keeps_changes(self):
        expected = self.upstream_change(); (self.home / 'app.txt').write_text('my unfinished edits')
        status = self.updater.check()
        self.assertTrue(status['available']); self.assertIn('Local changes', status['blocked_reason'])
        with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual((self.home / 'app.txt').read_text(), 'my unfinished edits')

    def test_untracked_files_are_preserved_and_block_apply(self):
        expected = self.upstream_change(); (self.home / 'personal.txt').write_text('keep me')
        self.updater.check()
        with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual((self.home / 'personal.txt').read_text(), 'keep me')

    def test_download_failure_leaves_installed_commit_unchanged(self):
        expected = self.upstream_change(); self.updater.check()
        with patch.object(self.updater, '_dependencies', side_effect=UpdateError('Download failed')):
            with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual(self.updater.snapshot()['phase'], 'apply_failed')

    def test_dependency_failure_has_repair_action_and_no_second_merge(self):
        expected = self.upstream_change(); self.updater.check()
        with patch.object(self.updater, '_dependencies', side_effect=[None, UpdateError('Install failed')]):
            with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), expected)
        self.assertEqual(self.updater.snapshot()['phase'], 'repair_required')
        self.assertEqual(self.updater.check()['phase'], 'repair_required')
        with patch.object(self.updater, '_dependencies'):
            self.assertEqual(self.updater.apply(expected)['phase'], 'installed')
        self.assertEqual(self.git(self.home, 'rev-list', '--count', 'HEAD'), '2')

    def test_edits_during_download_stop_apply(self):
        expected = self.upstream_change(); self.updater.check()
        def edit(*args, **kwargs): (self.home / 'app.txt').write_text('new edits while preparing')
        with patch.object(self.updater, '_dependencies', side_effect=edit):
            with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), self.before)

    def test_stale_or_unchecked_expected_version_is_rejected(self):
        expected = self.upstream_change()
        with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.updater.check()
        with self.assertRaises(UpdateError): self.updater.apply(self.before)
        with self.assertRaises(UpdateError): self.updater.apply('--not-a-commit')
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), self.before)

    def test_divergent_branch_never_auto_merges(self):
        expected = self.upstream_change(); (self.home / 'local.txt').write_text('local work')
        self.git(self.home, 'add', '.')
        self.git(self.home, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'Local fixture work')
        status = self.updater.check()
        self.assertFalse(status['available']); self.assertIn('diverged', status['blocked_reason'])
        with self.assertRaises(UpdateError): self.updater.apply(expected)
        self.assertEqual((self.home / 'local.txt').read_text(), 'local work')

    def test_archive_and_nested_directory_have_no_implicit_source(self):
        archive = self.base / 'archive'; archive.mkdir()
        self.assertIn('no Git update source', Updater(archive, self.base / 'state').check()['error'])
        nested = self.home / 'nested'; nested.mkdir()
        self.assertIn('no Git update source', Updater(nested, self.base / 'state').check()['error'])

    def test_operation_lock_covers_two_instances(self):
        second = Updater(self.home, self.base / 'state')
        with self.updater._locked():
            with self.assertRaises(UpdateError): second.check()

    def test_web_apply_requires_the_reviewed_version(self):
        self.upstream_change(); self.updater.check()
        manager = UpdateManager(self.updater)
        app = Flask(__name__); app.register_blueprint(updates.updates_bp)
        with patch.object(updates, 'manager', return_value=manager), patch.object(manager, 'start') as start:
            client = app.test_client()
            self.assertEqual(client.post('/api/update', json={'expected_commit':self.before}).status_code, 409)
            start.assert_not_called()
            expected = self.updater.snapshot()['latest_commit']
            self.assertEqual(client.post('/api/update', json={'expected_commit':expected}).status_code, 202)
            start.assert_called_once_with('apply', expected)

    def test_concurrent_manager_job_is_rejected(self):
        manager = UpdateManager(self.updater); manager.busy = True
        with self.assertRaises(UpdateError): manager.start()

    def test_cli_rerun_repairs_even_when_previous_install_recorded_an_error(self):
        expected = self.upstream_change(); self.updater.check()
        with patch.object(self.updater, '_dependencies', side_effect=[None, UpdateError('Install failed')]):
            with self.assertRaises(UpdateError): self.updater.apply(expected)
        output = io.StringIO()
        with patch.object(update_cli, 'Updater', return_value=self.updater), patch.object(self.updater, '_dependencies'), redirect_stdout(output):
            code = update_cli.command(SimpleNamespace(home=self.home), json_output=True)
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0); self.assertEqual(result['phase'], 'installed')
        self.assertFalse(result['busy']); self.assertIsNone(result['error'])

    def test_cli_reports_another_process_lock_without_a_traceback(self):
        output = io.StringIO()
        with self.updater._locked(), patch.object(update_cli, 'Updater', return_value=self.updater), redirect_stdout(output):
            code = update_cli.command(SimpleNamespace(home=self.home), check_only=True, json_output=True)
        self.assertEqual(code, 1); self.assertIn('Another update', json.loads(output.getvalue())['error'])

    def test_incompatible_python_is_rejected_before_dependency_download_or_code_change(self):
        (self.source / 'pyproject.toml').write_text('[project]\nversion = "0.2.0"\nrequires-python = ">=99.0"\n')
        self.git(self.source, 'add', '.')
        self.git(self.source, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'New Python fixture')
        self.git(self.source, 'push', 'origin', 'main')
        status = self.updater.check()
        with patch.object(self.updater, '_dependencies') as dependencies:
            with self.assertRaisesRegex(UpdateError, 'requires Python'): self.updater.apply(status['latest_commit'])
        dependencies.assert_not_called()
        self.assertEqual(self.git(self.home, 'rev-parse', 'HEAD'), self.before)
