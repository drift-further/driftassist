"""Purge previews are dry; only a reviewed, still-idle generation may be killed."""

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from cli import purge as purge_cli
from routes import terminal
from shared import purge, state
from tests.test_exact_targets import _IsolatedTmux
from tests.test_install_surface import _load_cli
from tests.test_state_integrity import _TempState

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('tmux'), 'needs tmux')
class PurgeServerTests(_IsolatedTmux):
    def setUp(self):
        super().setUp()
        self.audit = Path(self.tmpdir) / 'kill-audit.log'
        for p in (
            patch.object(terminal, 'KILL_AUDIT_FILE', self.audit),
            patch.object(state, 'pane_content_hash', {}),
            patch.object(state, 'pane_last_activity', {}),
            patch.object(state, 'assist_owned_sessions', set()),
            patch.object(state, 'save_idle_state'),
            patch.object(state, 'tmux_target', 'dev2:1.1'),
            patch.object(purge, '_PREVIEWS', {}),
            # Advance only purge's clock: real tmux sessions must have existed
            # long enough to qualify, rather than inventing activity before birth.
            patch.object(purge, 'time', SimpleNamespace(
                time=lambda: time.time() + 4 * 86400, monotonic=time.monotonic)),
            patch('shared.purge.tab_state.get_tab_state', return_value={'pinned': []}),
        ):
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.register_blueprint(terminal.terminal_bp)
        self.client = app.test_client()
        # Let shell startup settle, then seed exactly the hashes /poll uses.
        time.sleep(.2)
        self.age_all()

    def age_all(self, age=90000):
        purge._observe()
        for target in state.pane_last_activity:
            state.pane_last_activity[target] = purge.time.time() - age

    def preview(self, idle=86400, target=''):
        response = self.client.get('/terminal/purge', query_string={'idle': idle, 'target': target})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def execute(self, preview, names):
        return self.client.post('/terminal/purge', json={'preview': preview['preview'], 'sessions': names})

    def names(self):
        return {name for name in self.tmux('list-sessions', '-F', '#{session_name}').splitlines()}

    def test_preview_is_dry_and_only_idle_sessions_are_offered(self):
        state.pane_last_activity['dev2:1.1'] = purge.time.time()
        data = self.preview()
        self.assertEqual([r['session'] for r in data['sessions']], ['dev'])
        self.assertTrue(data['sessions'][0]['checked'])
        self.assertEqual(self.names(), {'dev', 'dev2'})
        self.assertFalse(self.audit.exists())

    def test_session_uses_its_most_recent_pane(self):
        self.tmux('split-window', '-d', '-t', '=dev:', "printf 'purge-split-ready\\n'; sleep 600")
        split_id = self.tmux('list-panes', '-s', '-t', '=dev:', '-F', '#{pane_id}').splitlines()[-1]
        self.assertTrue(self.wait_for(split_id, 'purge-split-ready'))
        self.age_all()
        target = next(t for t in state.pane_last_activity if t.startswith('dev:') and t != 'dev:1.1')
        state.pane_last_activity[target] = purge.time.time()
        self.assertNotIn('dev', [r['session'] for r in self.preview()['sessions']])
        self.age_all()
        self.assertIn('dev', [r['session'] for r in self.preview()['sessions']])

    def test_exceptions_are_unchecked_with_reasons_and_can_be_selected(self):
        with patch('shared.purge.tab_state.get_tab_state', return_value={'pinned': ['dev']}):
            data = self.preview(target='dev:1.1')
        rows = {r['session']: r for r in data['sessions']}
        self.assertFalse(rows['dev']['checked'])
        self.assertEqual(rows['dev']['reasons'], ['pinned', 'in view'])
        self.assertTrue(rows['dev2']['checked'])
        response = self.execute(data, ['dev'])
        self.assertEqual(response.get_json()['killed'], ['dev'])
        self.assertEqual(self.names(), {'dev2'})

    def test_prompt_session_is_unchecked_but_plain_output_is_checked(self):
        pane_id = self.pane_id('dev')
        self.tmux('respawn-pane', '-k', '-t', pane_id,
                  "printf 'Continue? (y/n)\\n'; sleep 600")
        self.assertTrue(self.wait_for(pane_id, 'Continue?'))
        self.age_all()
        rows = {r['session']: r for r in self.preview()['sessions']}
        self.assertFalse(rows['dev']['checked'])
        self.assertEqual(rows['dev']['reasons'], ['waiting at a prompt'])
        self.assertTrue(rows['dev2']['checked'])

    def test_server_owned_and_own_tmux_sessions_are_never_offered(self):
        # Explicit server mark, tmux mark, then the server's own inherited pane.
        for protection in ('registry', 'option', 'environment'):
            with self.subTest(protection=protection):
                if protection == 'registry':
                    state.assist_owned_sessions.add('dev')
                elif protection == 'option':
                    self.tmux('set-option', '-t', '=dev:', '@assist_owned', '1')
                with patch.dict(os.environ, {'TMUX': f'{self.socket},0,0', 'TMUX_PANE': self.pane_id('dev')}
                                if protection == 'environment' else {}):
                    data = self.preview()
                self.assertEqual([r['session'] for r in data['sessions']], ['dev2'])
                response = self.execute(data, ['dev'])
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.names(), {'dev', 'dev2'})
                state.assist_owned_sessions.clear()
                self.tmux('set-option', '-u', '-t', '=dev:', '@assist_owned')
        self.assertEqual({r['session'] for r in self.preview()['sessions']}, {'dev', 'dev2'})

    def test_new_server_owned_mark_is_rechecked_at_kill_time(self):
        data = self.preview()
        state.assist_owned_sessions.add('dev')
        result = self.execute(data, ['dev', 'dev2']).get_json()
        self.assertEqual(result['killed'], ['dev2'])
        self.assertEqual(result['skipped'], [{'session': 'dev', 'reason': 'Assist server session'}])
        self.assertEqual(self.names(), {'dev'})

    def test_kill_uses_exact_generation_and_keeps_prefix_sibling_and_audit(self):
        data = self.preview()
        result = self.execute(data, ['dev']).get_json()
        self.assertEqual(result['killed'], ['dev'])
        self.assertEqual(self.names(), {'dev2'})
        audit = [json.loads(line) for line in self.audit.read_text().splitlines()]
        self.assertEqual([(r['session'], r['outcome']) for r in audit], [('dev', 'success')])
        # Existing manual kill path also shares the same audit and exactness.
        response = self.client.post('/terminal/kill', json={'session': 'dev'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.names(), {'dev2'})
        self.assertEqual(json.loads(self.audit.read_text().splitlines()[-1])['outcome'], 'failure')

    def test_activity_moved_since_preview_is_skipped_and_unchanged_sibling_is_killed(self):
        data = self.preview()
        state.touch_activity('dev:1.1')
        result = self.execute(data, ['dev', 'dev2']).get_json()
        self.assertEqual(result['killed'], ['dev2'])
        self.assertEqual(result['skipped'], [{'session': 'dev', 'reason': 'activity changed'}])
        self.assertEqual(self.names(), {'dev'})

    def test_new_terminal_output_is_rechecked_even_without_a_poll(self):
        data = self.preview()
        pane_id = self.pane_id('dev')
        self.tmux('send-keys', '-t', pane_id, 'echo awake-now', 'Enter')
        self.assertTrue(self.wait_for(pane_id, 'awake-now'))
        result = self.execute(data, ['dev']).get_json()
        self.assertEqual(result['killed'], [])
        self.assertEqual(result['skipped'], [{'session': 'dev', 'reason': 'activity changed'}])
        self.assertEqual(self.names(), {'dev', 'dev2'})

    def test_recreated_name_is_skipped_even_with_identical_tail_and_old_activity(self):
        data = self.preview()
        self.tmux('kill-session', '-t', '=dev')
        self.tmux('new-session', '-d', '-s', 'dev', '-x', '80', '-y', '20')
        time.sleep(.2)
        self.age_all()
        result = self.execute(data, ['dev']).get_json()
        self.assertEqual(result['killed'], [])
        self.assertIn('recreated', result['skipped'][0]['reason'])
        self.assertEqual(self.names(), {'dev', 'dev2'})

    def test_recreated_same_name_identical_content_cannot_inherit_dead_session_idle(self):
        command = "printf 'stable-shell\\n'; sleep 600"
        self.tmux('new-session', '-d', '-s', 'scratch', '-x', '80', '-y', '20', command)
        self.assertTrue(self.wait_for(self.pane_id('scratch'), 'stable-shell'))
        with patch.object(purge, 'time', time):
            original = purge._observe('scratch')['scratch']
            target = original['panes'][0][0]
            old_hash = state.pane_content_hash[target]
            stale_activity = time.time() - 3 * 86400
            state.pane_last_activity[target] = stale_activity
            self.tmux('kill-session', '-t', '=scratch')
            self.tmux('new-session', '-d', '-s', 'scratch', '-x', '80', '-y', '20', command)
            self.assertTrue(self.wait_for(self.pane_id('scratch'), 'stable-shell'))
            fresh = purge._observe('scratch')['scratch']
            self.assertEqual(state.pane_content_hash[target], old_hash, 'screens must match')
            self.assertNotEqual(fresh['session_id'], original['session_id'])
            self.assertLess(fresh['idle_seconds'], 3)
            self.assertGreaterEqual(state.pane_last_activity[target], int(fresh['generation'][3]))
            self.assertNotIn('scratch', [row['session'] for row in self.preview()['sessions']])
        self.assertIn('scratch', self.names())
        self.assertFalse(self.audit.exists())

    def test_replacement_between_recheck_and_kill_is_not_hit_by_name(self):
        data = self.preview()
        real_kill = terminal._kill_terminal_session

        def replace_then_kill(name, session_id):
            self.tmux('kill-session', '-t', '=dev')
            self.tmux('new-session', '-d', '-s', 'dev', '-x', '80', '-y', '20')
            return real_kill(name, session_id)

        with patch.object(terminal, '_kill_terminal_session', side_effect=replace_then_kill):
            result = self.execute(data, ['dev']).get_json()
        self.assertEqual(result['killed'], [])
        self.assertIn('kill-session failed', result['skipped'][0]['reason'])
        self.assertEqual(self.names(), {'dev', 'dev2'})

    def test_quota_only_change_between_preview_and_kill_stays_idle(self):
        real_run = subprocess.run
        pane_id = self.pane_id('dev')
        tail = 'Waiting\n gpt-6-sol · Context 62% left · weekly 48% left'

        def codex_capture(args, **kwargs):
            result = real_run(args, **kwargs)
            if 'capture-pane' in args and args[args.index('-t') + 1] == pane_id:
                result.stdout = tail
            return result

        with patch('shared.purge.subprocess.run', side_effect=codex_capture), \
                patch('shared.purge.resolve_process', side_effect=lambda target, pid, cmd:
                      'codex' if target.startswith('dev:') else 'shell'):
            self.age_all()
            data = self.preview()
            self.assertIn('dev', [r['session'] for r in data['sessions']])
            tail = tail.replace('48%', '47%')
            result = self.execute(data, ['dev']).get_json()
        self.assertEqual(result['killed'], ['dev'])
        self.assertEqual(result['skipped'], [])
        self.assertEqual(self.names(), {'dev2'})

    def test_empty_selection_replay_and_expired_preview_kill_nothing(self):
        data = self.preview()
        result = self.execute(data, []).get_json()
        self.assertEqual(result, {'ok': True, 'killed': [], 'skipped': []})
        self.assertEqual(self.execute(data, ['dev']).status_code, 400)
        data = self.preview()
        with patch.object(purge, 'PREVIEW_TTL', -1):
            self.assertEqual(self.execute(data, ['dev']).status_code, 400)
        self.assertEqual(self.names(), {'dev', 'dev2'})
        self.assertFalse(self.audit.exists())

    def test_capture_failure_refuses_preview_instead_of_offering_unknown_sessions(self):
        real_run = subprocess.run

        def fail_capture(args, **kwargs):
            if 'capture-pane' in args:
                return subprocess.CompletedProcess(args, 1, '', 'pane gone')
            return real_run(args, **kwargs)

        with patch('shared.purge.subprocess.run', side_effect=fail_capture):
            response = self.client.get('/terminal/purge')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.audit.exists())

    def test_cli_uses_the_same_dry_preview_and_audited_kill_path(self):
        def get(path):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            return response.get_json()

        def post(path, payload):
            response = self.client.post(path, json=payload)
            self.assertEqual(response.status_code, 200)
            return response.get_json()

        with patch('cli.purge.http.get', side_effect=get), patch('cli.purge.http.post', side_effect=post):
            with redirect_stdout(io.StringIO()) as out:
                purge_cli.command('24h', json_output=True)
            data = json.loads(out.getvalue())
            self.assertEqual([(r['session'], r['checked']) for r in data['sessions']], [('dev', True), ('dev2', False)])
            self.assertFalse(self.audit.exists())
            with redirect_stdout(io.StringIO()) as out:
                purge_cli.command('24h', yes=True, json_output=True)
            self.assertEqual(json.loads(out.getvalue())['killed'], ['dev'])
            self.assertEqual(self.names(), {'dev2'})
            self.assertEqual(json.loads(self.audit.read_text())['outcome'], 'success')


class PurgeSettingTests(_TempState):
    def test_patch_accepts_choices_rejects_bad_types_and_remembers_threshold(self):
        from routes.settings import settings_bp
        state.load_settings()
        app = Flask(__name__)
        app.register_blueprint(settings_bp)
        client = app.test_client()
        self.assertEqual(state.get_setting('ui', 'purge_idle_threshold_sec'), 86400)
        for value in ('24h', None, True, 0, 4000, 3600.5, 999999):
            with self.subTest(value=value):
                self.assertEqual(client.patch('/api/settings', json={'ui': {'purge_idle_threshold_sec': value}}).status_code, 400)
        for value in state.PURGE_IDLE_THRESHOLDS:
            self.assertEqual(client.patch('/api/settings', json={'ui': {'purge_idle_threshold_sec': value}}).status_code, 200)
            state.load_settings()
            self.assertEqual(state.get_setting('ui', 'purge_idle_threshold_sec'), value)

    def test_bad_saved_threshold_falls_back_per_key(self):
        for value in (None, '24h', 1, 4000, 999999):
            state.SETTINGS_FILE.write_text(json.dumps({'ui': {'purge_idle_threshold_sec': value, 'popup_seconds': 9}}))
            with self.assertLogs('shared.state', 'WARNING'):
                state.load_settings()
            self.assertEqual(state.get_setting('ui', 'purge_idle_threshold_sec'), 86400)
            self.assertEqual(state.get_setting('ui', 'popup_seconds'), 9)


class PurgeCliTests(unittest.TestCase):
    def preview(self):
        return {'ok': True, 'preview': 'token', 'idle': 86400, 'sessions': [
            {'session': 'old', 'idle_seconds': 90000, 'checked': True, 'reasons': []},
            {'session': 'keep', 'idle_seconds': 90000, 'checked': True, 'reasons': []},
            {'session': 'pinned', 'idle_seconds': 90000, 'checked': False, 'reasons': ['pinned']},
        ]}

    def test_dry_run_has_no_post_and_yes_kills_only_checked_non_kept_rows(self):
        for yes in (False, True):
            with self.subTest(yes=yes), patch('cli.purge.http.get', return_value=self.preview()) as get, \
                    patch('cli.purge.http.post', return_value={'ok': True, 'killed': ['old'], 'skipped': []}) as post:
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(purge_cli.command('24h', yes, ['keep'], True), 0)
                data = json.loads(out.getvalue())
                self.assertEqual(data['dry_run'], not yes)
                self.assertEqual(err.getvalue(), '')
                self.assertEqual(get.call_args.args[0], '/terminal/purge?idle=86400')
                if yes:
                    post.assert_called_once_with('/terminal/purge', {'preview': 'token', 'sessions': ['old']})
                else:
                    post.assert_not_called()
                    self.assertEqual(data['killed'], [])

    def test_hints_human_reasons_and_suppression(self):
        for silent in ('0', '1'):
            out, err = io.StringIO(), io.StringIO()
            with patch.dict(os.environ, {'ASSIST_NO_HINTS': silent}), \
                    patch('cli.purge.http.get', return_value=self.preview()), \
                    redirect_stdout(out), redirect_stderr(err):
                purge_cli.command('24h', keep=['keep'])
            self.assertIn('[ ] pinned', out.getvalue())
            self.assertIn('kept by --keep', out.getvalue())
            self.assertIn('nothing killed', out.getvalue())
            if silent == '1':
                self.assertEqual(err.getvalue(), '')
            else:
                self.assertIn('next: assist purge --idle 24h --keep keep --yes', err.getvalue())

    def test_unknown_keep_is_an_error_and_never_posts(self):
        for yes in (False, True):
            for json_output in (False, True):
                with self.subTest(yes=yes, json=json_output), \
                        patch('cli.purge.http.get', return_value=self.preview()), \
                        patch('cli.purge.http.post') as post:
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = purge_cli.command('24h', yes, ['keep', 'keeep'], json_output)
                    self.assertNotEqual(code, 0)
                    post.assert_not_called()
                    if json_output:
                        data = json.loads(out.getvalue())
                        self.assertFalse(data['ok'])
                        self.assertIn('keeep', data['error'])
                        self.assertEqual(data['killed'], [])
                    else:
                        self.assertIn('keeep', err.getvalue())
                    self.assertNotIn('next:', err.getvalue())

    def test_parser_dispatch_help_and_repeatable_keep(self):
        cli = _load_cli()
        args = ['purge', '--idle', '12h', '--yes', '--keep', 'one', '--keep', 'two', '--json']
        parsed = cli.build_parser().parse_args(args)
        self.assertEqual(parsed.keep, ['one', 'two'])
        with patch.object(cli, 'resolve'), patch.object(cli.purge, 'command', return_value=0) as command:
            self.assertEqual(cli.main(args), 0)
            command.assert_called_once_with('12h', True, ['one', 'two'], True)
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()) as help_out:
            cli.build_parser().parse_args(['purge', '--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn('--keep', help_out.getvalue())
        self.assertIn('Purge'.lower(), cli.help_text().lower())

    def test_changed_asset_urls_match_worker_cache_and_sheet_control_is_present(self):
        import re
        html = (ROOT / 'index.html').read_text()
        worker = (ROOT / 'sw.js').read_text()
        for path in ('js/tabs.js', 'js/ui.js', 'css/terminal.css'):
            url = re.search(r'/' + re.escape(path) + r'\?v=\d+', html).group()
            self.assertIn("'" + url + "'", worker)
        self.assertIn('onclick="openPurgeIdle()"', html)
        self.assertIn('onclick="killPurgeSelected()" disabled', html)


@unittest.skipUnless(shutil.which('node'), 'needs Node')
class PurgeSheetTests(unittest.TestCase):
    def run_js(self, body):
        source = (ROOT / 'js/tabs.js').read_text()
        # Execute the actual sheet controls and purge functions; other tab-strip
        # initialization requires the full page and is outside this surface.
        functions = source[source.index('let _staleSheetOpen'):source.index('function _staleRowDotClass')]
        harness = r"""
const vm = require('vm');
function element() {
    const classes = new Set();
    return {children: [], hidden: false, disabled: false, value: '', dataset: {},
        textContent: '', listeners: {},
        classList: {add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x)},
        set innerHTML(value) { this.children = []; },
        appendChild(child) { this.children.push(child); },
        addEventListener(name, fn) { this.listeners[name] = fn; },
        setAttribute() {},
        querySelectorAll() { return this.children.flatMap(row => row.children.filter(c => c.type === 'checkbox')); }};
}
const els = {};
for (const id of ['stale-sheet', 'drawer-overlay', 'purge-panel', 'stale-sheet-body',
                  'purge-idle', 'purge-kill', 'purge-status', 'purge-rows']) els[id] = element();
els['purge-panel'].hidden = true;
global.document = {getElementById: id => els[id] || null, createElement: element};
global.SETTINGS = {ui: {purge_idle_threshold_sec: 86400}};
global._termTarget = 'view:1.1';
const requests = [], saves = [];
global._saveSetting = async (section, key, value) => {
    saves.push([section, key, value]); SETTINGS[section][key] = value;
};
const preview = {ok: true, preview: 'one', idle: 86400, sessions: [
    {session: 'old', idle_seconds: 90000, checked: true, reasons: []},
    {session: 'pin', idle_seconds: 90000, checked: false, reasons: ['pinned']},
    {session: 'prompt', idle_seconds: 90000, checked: false, reasons: ['waiting at a prompt']},
    {session: 'view', idle_seconds: 90000, checked: false, reasons: ['in view']},
]};
let respond = async (path, options) => ({ok: true, json: async () => options && options.method === 'POST'
    ? {ok: true, killed: ['pin'], skipped: [{session: 'old', reason: 'activity changed'}]}
    : {...preview, idle: Number(new URLSearchParams(path.split('?')[1]).get('idle')) || 86400}});
global.fetch = async (path, options) => { requests.push({path, options}); return respond(path, options); };
vm.runInThisContext(FUNCTIONS);
(async () => { BODY })().catch(e => { console.error(e.stack); process.exitCode = 1; });
""".replace('FUNCTIONS', json.dumps(functions)).replace('BODY', body)
        result = subprocess.run(['node', '-e', harness], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_preview_is_dry_exceptions_editable_and_only_tap_posts_selected_set(self):
        data = self.run_js(r"""
openStaleSheet();
await openPurgeIdle();
const dryPosts = requests.filter(r => r.options).length;
const boxes = els['purge-rows'].querySelectorAll('input');
const initial = boxes.map(b => b.checked);
const reasons = els['purge-rows'].children.map(row => row.children[1].textContent);
const firstButton = els['purge-kill'].textContent;
boxes[0].checked = false;
boxes[1].checked = true;
boxes[1].listeners.change();
await killPurgeSelected();
const payload = JSON.parse(requests.find(r => r.options).options.body);
const disabled = els['purge-kill'].disabled;
await killPurgeSelected(); // consumed preview cannot execute again
console.log(JSON.stringify({dryPosts, initial, reasons, firstButton, payload, disabled,
    posts: requests.filter(r => r.options).length, status: els['purge-status'].textContent}));
""")
        self.assertEqual(data['dryPosts'], 0)
        self.assertEqual(data['initial'], [True, False, False, False])
        self.assertEqual(data['firstButton'], 'Kill 1 session')
        self.assertIn('pinned', data['reasons'][1])
        self.assertIn('waiting at a prompt', data['reasons'][2])
        self.assertIn('in view', data['reasons'][3])
        self.assertEqual(data['payload'], {'preview': 'one', 'sessions': ['pin']})
        self.assertTrue(data['disabled'])
        self.assertEqual(data['posts'], 1)
        self.assertIn('activity changed', data['status'])

    def test_unchecking_all_disables_kill_and_threshold_saves_then_fetches_new_preview(self):
        data = self.run_js(r"""
await openPurgeIdle();
const boxes = els['purge-rows'].querySelectorAll('input');
boxes.forEach(b => { b.checked = false; });
_updatePurgeCount();
const emptyDisabled = els['purge-kill'].disabled;
await killPurgeSelected();
els['purge-idle'].value = '14400';
await changePurgeThreshold();
closePurgeIdle();
await openPurgeIdle();
console.log(JSON.stringify({emptyDisabled, saves, queries: requests.map(r => r.path),
    posts: requests.filter(r => r.options).length, choice: els['purge-idle'].value}));
""")
        self.assertTrue(data['emptyDisabled'])
        self.assertEqual(data['posts'], 0)
        self.assertEqual(data['saves'], [['ui', 'purge_idle_threshold_sec', 14400]])
        self.assertIn('idle=14400', data['queries'][-1])
        self.assertEqual(data['choice'], '14400')

    def test_late_preview_after_close_and_failed_preview_cannot_enable_kill(self):
        data = self.run_js(r"""
let release;
respond = () => new Promise(resolve => { release = resolve; });
openStaleSheet();
const pending = openPurgeIdle();
closeStaleSheet();
release({ok: true, json: async () => preview});
await pending;
const afterClose = els['purge-rows'].children.length;
respond = async () => ({ok: false, json: async () => ({ok: false, error: 'capture failed'})});
openStaleSheet();
await openPurgeIdle();
await killPurgeSelected();
console.log(JSON.stringify({afterClose, disabled: els['purge-kill'].disabled,
    posts: requests.filter(r => r.options).length, status: els['purge-status'].textContent}));
""")
        self.assertEqual(data['afterClose'], 0)
        self.assertTrue(data['disabled'])
        self.assertEqual(data['posts'], 0)
        self.assertEqual(data['status'], 'capture failed')

    def test_failed_setting_save_leaves_old_selection_unusable(self):
        data = self.run_js(r"""
await openPurgeIdle();
_saveSetting = async () => {}; // settings helper reports failures without throwing
els['purge-idle'].value = '14400';
await changePurgeThreshold();
await killPurgeSelected();
console.log(JSON.stringify({disabled: els['purge-kill'].disabled,
    gets: requests.length, posts: requests.filter(r => r.options).length,
    status: els['purge-status'].textContent}));
""")
        self.assertTrue(data['disabled'])
        self.assertEqual(data['gets'], 1)
        self.assertEqual(data['posts'], 0)
        self.assertEqual(data['status'], 'Could not save idle threshold.')


if __name__ == '__main__':
    unittest.main()
