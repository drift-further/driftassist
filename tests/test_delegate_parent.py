"""Parent ranking is pure; launch/adoption is checked on isolated tmux."""
import os
import io
import tempfile
from contextlib import redirect_stdout, redirect_stderr
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
from tests.test_exact_targets import _RouteHarness


class ParentTests(unittest.TestCase):
    def resolve(self, name, names, recorded=None):
        from shared.delegate_parent import resolve_parent
        return resolve_parent(name, names, recorded or {})

    def test_forward_boundaries_and_longest(self):
        # The supplied spec states 24 live names but does not enumerate them.
        # Exercise 24 representative combinations without inspecting live tmux.
        for parent in ('KAREN', 'KAREN-4', 'assist-dev', 'studio', 'apiBuilder', 'daic'):
            for suffix in ('sol-job', 'claude-123', 'astra-review-r1', 'job'):
                name = 'del_' + parent + '-' + suffix
                result = self.resolve(name, ['drift-further_' + parent, name])
                self.assertEqual(result['parent_session'], 'drift-further_' + parent)
                self.assertEqual(result['match'], 'attached')
        self.assertEqual(self.resolve('del_KAREN-4-sol-job', ['KAREN', 'KAREN-4'])['parent_session'], 'KAREN-4')
        self.assertEqual(self.resolve('del_studios-sol-job', ['studio'])['match'], 'unmatched')
        self.assertEqual(self.resolve('del_studio', ['studio'])['match'], 'unmatched')

    def test_to_casefold_and_reverse(self):
        r = self.resolve('del_studio-to-apibuilder-sol-job', ['studio'])
        self.assertEqual((r['parent_session'], r['target_repo']), ('studio', 'apibuilder'))
        self.assertEqual(self.resolve('del_KAREN-sol-job', ['KAREN', 'karen'])['match'], 'ambiguous')
        self.assertEqual(self.resolve('del_assist-sol-job', ['assist-dev'])['parent_session'], 'assist-dev')
        self.assertEqual(self.resolve('del_assist-sol-job', ['assist-dev', 'assist-tools'])['match'], 'ambiguous')

    def test_recorded_closed_and_cycles(self):
        name = 'del_studio-job'
        self.assertEqual(self.resolve(name, ['studio', 'other'], {name: 'other'})['parent_session'], 'other')
        self.assertEqual(self.resolve(name, ['studio'], {name: 'closed'})['match'], 'unmatched')
        for records in ({'del_A': 'del_B', 'del_B': 'del_A'}, {'del_A': 'del_A'}):
            self.assertEqual(self.resolve('del_A', list(records), records)['match'], 'cycle')
        r = self.resolve('del_A', ['del_A', 'del_B', 'studio'], {'del_A': 'del_B', 'del_B': 'studio'})
        self.assertEqual(r['parent_session'], 'del_B')
        self.assertEqual(r['ancestor_session'], 'studio')

    def test_child_stays_attached_to_open_orphan_parent(self):
        result = self.resolve('del_child', ['del_child', 'del_mid'],
                              {'del_child': 'del_mid', 'del_mid': 'closed'})
        self.assertEqual(result['match'], 'attached')
        self.assertEqual(result['parent_session'], 'del_mid')
        self.assertEqual(result['ancestor_session'], 'del_mid')
        self.assertEqual(self.resolve('del_mid', ['del_mid'], {'del_mid': 'closed'})['match'], 'unmatched')


class ParentLaunchTests(_RouteHarness):
    def test_create_records_parent_adoption_preserves_it(self):
        from routes import terminal
        import shared.state as state
        from shared import launch_provenance as provenance
        app = Flask(__name__)
        app.register_blueprint(terminal.terminal_bp)
        with tempfile.TemporaryDirectory() as d, patch.object(state, 'PROJECTS_DIR', Path(d)), \
             patch.object(provenance, 'STORE_DIR', Path(d) / 'provenance', create=True):
            client = app.test_client()
            payload = dict(project='del_parent', cwd=d, skip_init=True, parent='dev')
            result = client.post('/terminal/launch', json=payload)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertEqual(self.tmux('show', '-v', '-t', '=del_parent:', '@assist_parent'), 'dev')
            payload['parent'] = 'dev2'
            result = client.post('/terminal/launch', json=payload)
            self.assertEqual(result.status_code, 200, result.json)
            self.assertTrue(result.json['existed'])
            self.assertEqual(self.tmux('show', '-v', '-t', '=del_parent:', '@assist_parent'), 'dev')
            panes = client.get('/terminal/sessions').json['sessions']
            child = next(p for p in panes if p['session'] == 'del_parent')
            self.assertEqual(child['delegate']['parent_session'], 'dev')

    def test_cli_launch_uses_private_tmux_caller_and_adoption_keeps_parent(self):
        from cli import session
        target = self.pane_id('dev')
        with patch.dict(os.environ, {'TMUX': str(self.socket) + ',0,0', 'TMUX_PANE': target}), \
             patch.dict(os.environ, {'ASSIST_REPLY_TO': ''}), \
             patch.object(session.http, 'post', side_effect=lambda path, data: self.client.post(path, json=data).json), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            session.launch('del_cli_parent', cwd=str(self.projects))
            self.assertEqual(self.tmux('show', '-v', '-t', '=del_cli_parent:', '@assist_parent'), 'dev')
            os.environ['TMUX_PANE'] = self.dev2_pane
            session.launch('del_cli_parent', cwd=str(self.projects))
            self.assertEqual(self.tmux('show', '-v', '-t', '=del_cli_parent:', '@assist_parent'), 'dev')

    def test_control_characters_in_parent_rejected_before_creation(self):
        from routes import terminal
        for parent in ('dev\nevil', 'dev\tx', 'dev\r', 'dev\x00', 'dev\x7f', 'dev\x85'):
            with self.subTest(parent=parent), patch.object(terminal, 'create_tmux_session') as create:
                response = self.client.post('/terminal/launch', json={
                    'project': 'del_invalid', 'cwd': str(self.projects), 'parent': parent})
                self.assertEqual(response.status_code, 400, response.json)
                create.assert_not_called()
        self.assertNotIn('del_invalid', self.tmux('list-sessions', '-F', '#{session_name}'))
