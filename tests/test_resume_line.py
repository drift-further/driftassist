"""Resume ownership and lifecycle on fake proc/home trees, not newest-by-cwd."""
import io
import json
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from shared.agent_identity import ProcessObservation

ID1 = '11111111-1111-4111-8111-111111111111'
ID2 = '22222222-2222-4222-8222-222222222222'


class ResumeTests(unittest.TestCase):
    def setUp(self):
        from shared.resume_line import Resolver
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        self.home = self.root / 'home'
        self.cwd = self.root / "a dir's project"
        self.cwd.mkdir()
        self.r = Resolver(self.proc, self.home)
        self.agents = []

    def agent(self, pid=10, kind='codex', argv=None, start='123'):
        p = self.proc / str(pid)
        (p / 'fd').mkdir(parents=True, exist_ok=True)
        (p / 'stat').write_text(f'{pid} (agent) S ' + ' '.join(['0'] * 18 + [start]))
        (p / 'cmdline').write_bytes(('\0'.join(argv or [kind]) + '\0').encode())
        if not (p / 'cwd').exists():
            (p / 'cwd').symlink_to(self.cwd)
        a = dict(kind=kind, pid=pid, start=start, argv=argv or [kind], cwd=str(self.cwd))
        self.agents.append(a)
        return a

    def rollout(self, pid, ident=ID1, **payload):
        path = self.home / '.codex/sessions/2026/10/26' / f'rollout-now-{ident}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=ident, cwd=str(self.cwd), **payload))) + '\n')
        fd = self.proc / str(pid) / 'fd' / str(len(list((self.proc / str(pid) / 'fd').iterdir())) + 3)
        fd.symlink_to(path)
        return path

    def observe(self, complete=True, agents=None, root='root1'):
        agents = self.agents if agents is None else agents
        return ProcessObservation(complete, [], agents[0] if agents else None, agents, False, root)

    def resolve(self, now=100, **kw):
        return self.r.resolve('%1', 'del_test', self.observe(), now=now, **kw)

    def test_two_panes_same_cwd_and_node_shim(self):
        a = self.agent(10, argv=['node', '/bin/codex.js'])
        b = self.agent(11)
        c = self.agent(12)
        self.rollout(11)
        self.rollout(12, ID2)
        one, _ = self.r.resolve('%1', 'del_one', self.observe(agents=[a, b]), now=100)
        two, _ = self.r.resolve('%2', 'del_two', self.observe(agents=[c]), now=100)
        self.assertEqual((one['id'], two['id']), (ID1, ID2))

    def test_subagents_ambiguity_cwd_and_resume_argv(self):
        self.agent(argv=['codex', 'resume', ID2])
        sub = self.rollout(10, ID1, source={'subagent': {'spawn': {}}})
        self.rollout(10, ID2)
        record, reason = self.resolve()
        self.assertEqual(record['id'], ID2)
        sub.write_text(json.dumps({'type':'session_meta','payload':{'id':ID1,'cwd':str(self.cwd)}})+'\n')
        self.r = type(self.r)(self.proc, self.home)
        self.assertEqual(self.resolve()[0]['id'], ID2)
        (self.proc / '10/cmdline').write_bytes(b'codex\0')
        self.assertEqual(self.resolve(fresh=True), (None, 'ambiguous'))
        for p in (self.proc / '10/fd').iterdir(): p.unlink()
        self.rollout(10, ID1, parent_thread_id='parent')
        self.assertEqual(self.resolve(fresh=True), (None, 'unowned'))
        path = self.rollout(10, ID2)
        path.write_text(json.dumps({'type':'session_meta','payload':{'id':ID2,'cwd':'/foreign'}})+'\n')
        self.assertEqual(self.resolve(fresh=True), (None, 'unowned'))

    def test_no_fd_and_generation_recheck(self):
        self.agent()
        self.assertEqual(self.resolve(), (None, 'no session yet'))
        self.rollout(10)
        (self.proc / '10/stat').write_text('10 (agent) S ' + ' '.join(['0'] * 18 + ['999']))
        self.assertEqual(self.resolve(fresh=True), (None, 'unowned'))

    def test_claude_bindings_and_gate(self):
        self.r.claude_verified = False  # Explicit fallback remains available.
        self.agent(kind='claude')
        path = self.home / '.claude/sessions/10.json'
        path.parent.mkdir(parents=True)
        data = dict(procStart='123', tmux='%1', sessionId=ID1)
        path.write_text(json.dumps(data))
        self.assertEqual(self.resolve(), (None, 'unverifiable'))
        self.r.claude_verified = True
        self.assertEqual(self.resolve(fresh=True)[0]['id'], ID1)
        for field, value in (('procStart','999'), ('tmux','%2')):
            path.write_text(json.dumps({**data, field:value}))
            self.assertEqual(self.resolve(fresh=True), (None, 'unowned'))

    def test_claude_live_tmux_string_and_binding_twins(self):
        self.agent(kind='claude', argv=['claude', '--model', 'sonnet'])
        path = self.home / '.claude/sessions/10.json'
        path.parent.mkdir(parents=True)
        for binding, matches in (
            ('proj-dev:@355.%394', True),
            ('proj-dev:@355.%395', False),
            ('proj-dev:@355.394', False),
            ('proj-dev:@355.%394junk', False),
            ('%394', True),
            ({'pane_id': '%394'}, True),
            ({'pane': '%394'}, True),
            ({'pane_id': '%395'}, False),
        ):
            with self.subTest(binding=binding):
                path.write_text(json.dumps(dict(procStart='123', tmux=binding, sessionId=ID1)))
                record, reason = self.r.resolve('%394', 'proj-dev',
                                                self.observe(), fresh=True)
                if matches:
                    self.assertIsNone(reason)
                    self.assertEqual(shlex.split(record['command']),
                                     ['claude', '--resume', ID1, '--model', 'sonnet'])
                else:
                    self.assertEqual((record, reason), (None, 'unowned'))

    def test_verified_default_binds_generation_and_keeps_allowlisted_argv(self):
        self.agent(kind='claude', argv=['claude','--model','sonnet','--permission-mode','plan',
                                      '--add-dir','other dir','--system-prompt','private'])
        path = self.home / '.claude/sessions/10.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(procStart='123', tmux='%1', sessionId=ID1)))
        record, reason = self.resolve(fresh=True)
        self.assertIsNone(reason)
        self.assertEqual(shlex.split(record['command']), ['claude','--resume',ID1,'--model','sonnet',
                                                        '--permission-mode','plan','--add-dir','other dir'])
        path.write_text(json.dumps(dict(procStart='123', tmux='%1', sessionId=ID2)))
        refreshed, _ = self.resolve(fresh=True)
        self.assertEqual((refreshed['id'], refreshed['conv_rev']), (ID2, 2))
        path.write_text(json.dumps(dict(procStart='999', tmux='%1', sessionId=ID1)))
        self.assertEqual(self.resolve(fresh=True), (None,'unowned'))

    def test_lifecycle_cache_freezing_and_retention(self):
        self.agent()
        self.rollout(10)
        record, _ = self.resolve()
        self.assertEqual(record['conv_rev'], 1)
        for p in (self.proc / '10/fd').iterdir(): p.unlink()
        self.rollout(10, ID2)
        self.assertEqual(self.resolve(now=105, cache_seconds=30)[0]['id'], ID1)
        changed, _ = self.resolve(now=106, fresh=True)
        self.assertEqual((changed['id'], changed['conv_rev']), (ID2, 2))
        frozen, _ = self.r.resolve('%1','del_test',self.observe(False),now=110)
        self.assertEqual(frozen, changed)
        self.assertEqual(self.r.resolve('%1','del_test',self.observe(False),now=110,fresh=True), (None,'observation incomplete'))
        exited, _ = self.r.resolve('%1','del_test',self.observe(agents=[]),now=120)
        self.assertFalse(exited['live'])
        self.assertEqual(exited['verified_at'], 106)
        self.assertIsNone(self.r.resolve('%1','del_test',self.observe(agents=[]),now=90000)[0])
        self.agent(20)
        self.assertIsNone(self.r.resolve('%1','del_test',self.observe(agents=[self.agents[-1]]),now=130)[0])

    def test_unowned_unsets_and_farewell_is_unverified(self):
        self.agent()
        path = self.rollout(10)
        self.assertIsNotNone(self.resolve()[0])
        path.write_text('{}\n')
        self.assertIsNone(self.resolve(fresh=True)[0])
        self.r = type(self.r)(self.proc, self.home)
        r, _ = self.r.resolve('%1','del_test',self.observe(agents=[]),now=200,
                              tail='To continue this session, run codex resume ' + ID1)
        self.assertFalse(r['verified'])
        self.assertFalse(r['live'])

    def test_codex_0164_farewell_with_colon_and_ansi(self):
        fixtures = Path(__file__).parent / 'fixtures' / 'delegates'
        for name in ('codex-0164-farewell.txt', 'codex-0164-farewell-ansi.txt'):
            with self.subTest(name=name):
                self.r = type(self.r)(self.proc, self.home)
                record, reason = self.r.resolve('%1', 'del_test', self.observe(agents=[]),
                                                now=200, tail=(fixtures / name).read_text())
                self.assertIsNotNone(record, reason)
                self.assertEqual(record['id'], ID1)
                self.assertFalse(record['verified'])
                self.assertFalse(record['live'])

    def test_allowlist_and_shell_quoting(self):
        self.agent(argv=['codex','-m','model','-c','key=a b','--yolo','exec','rm everything','--secret','oops'])
        self.rollout(10)
        r, _ = self.resolve()
        self.assertEqual(shlex.split(r['command']), ['codex','resume',ID1,'-m','model','-c','key=a b','--yolo'])
        self.assertIn(shlex.quote(str(self.cwd)), r['line'])
        from shared.resume_line import allowed_args
        self.assertEqual(allowed_args('claude', ['claude','--continue','--model','m','--add-dir','a b','prompt']),
                         ['--model','m','--add-dir','a b'])


class ResumeCliTests(unittest.TestCase):
    def test_hints_silence_and_kill_order(self):
        from cli import session
        events = []
        r = dict(line='restore this', command='codex resume id', verified=True)
        def post(*args):
            events.append('kill')
            self.assertIn('resume: restore this', err.getvalue())
            return {'session':'x'}
        with patch.dict(os.environ, {'ASSIST_NO_HINTS':'0'}), patch.object(session.http,'get',return_value={'resume':r}), \
             patch.object(session.http,'post',side_effect=post), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            session.kill('x')
        self.assertEqual(events,['kill'])
        with patch.dict(os.environ, {'ASSIST_NO_HINTS':'1'}), patch.object(session.http,'get') as get, redirect_stderr(io.StringIO()) as err:
            session.print_resume_hint('x:0.0')
            get.assert_not_called()
            self.assertEqual(err.getvalue(),'')

    def test_json_merge_columns_and_explicit_resume(self):
        from cli import session
        pane=dict(session='s',target='s:0.0',state='idle',agent_kind='codex',idle_seconds=2,command_display='Codex',cwd='/tmp',delegate={'state':'waiting','reason':'question'})
        self.assertEqual(session._display_rows([pane],False)[0], ['s','idle','codex','-','-','-','-','2s','Codex','waiting:question'])
        self.assertEqual(session._display_rows([pane],True)[0][-2:], ['/tmp','waiting:question'])
        r=dict(line='restore',command='codex resume id')
        with patch.object(session.http,'get',return_value={'resume':r}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(session.resume('s',agent_only=True),0)
        self.assertEqual(out.getvalue(),'codex resume id\n')
        replies=[{'sessions':[{'target':'a','resume':{'id':'old'}},{'target':'b'}]},
                 {'states':{},'sessions':[{'target':'b','resume':{'id':'new'}},
                                         {'target':'a','resume':None,'resume_reason':'unowned'}]}]
        with patch.object(session.http,'get',side_effect=replies):
            merged=session._merged_sessions()['sessions']
        self.assertIsNone(merged[0]['resume'])
        self.assertEqual(merged[1]['resume']['id'],'new')



from tests.test_exact_targets import _RouteHarness


class ResumeHttpTests(_RouteHarness):
    """Real authenticated scratch HTTP and CLI, private tmux only."""
    def setUp(self):
        super().setUp()
        import threading
        import shared.auth as auth
        from shared import auth_gate, resume_line
        from routes import delegates, poll, terminal
        from flask import Flask
        from werkzeug.serving import make_server, WSGIRequestHandler
        app = Flask(__name__)
        auth_gate.register_auth_gate(app)
        app.register_blueprint(delegates.delegates_bp)
        app.register_blueprint(terminal.terminal_bp)
        app.register_blueprint(poll.poll_bp)
        p = patch.object(auth, '_token_cache', 'scratch-token')
        p.start(); self.addCleanup(p.stop)
        p = patch.object(resume_line, 'RESOLVER', resume_line.Resolver())
        p.start(); self.addCleanup(p.stop)
        class Quiet(WSGIRequestHandler):
            def log(self, *args, **kw): pass
        self.server = make_server('127.0.0.1', 0, app, request_handler=Quiet)
        self.assertGreaterEqual(self.server.server_port, 8190)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        from tests._paths import scratch_env
        root = Path(self.tmpdir) / 'cli'
        self.env = scratch_env(root, ASSIST_HOME=Path(__file__).resolve().parents[1],
                               ASSIST_PORT=self.server.server_port, ASSIST_CONFIG_DIR=root/'config',
                               ASSIST_DATA_DIR=root/'data', ASSIST_STATE_DIR=root/'state', ASSIST_NO_HINTS='0')
        (root/'config').mkdir(parents=True)
        (root/'config/auth_token').write_text('scratch-token')
        self.app = app

    def cli(self, *args, silent=False):
        import subprocess
        env = dict(self.env)
        if silent: env['ASSIST_NO_HINTS']='1'
        return subprocess.run([str(Path(__file__).resolve().parents[1]/'bin/assist'), *args],
                               env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15)

    def test_auth_fresh_resume_and_kill_order_on_scratch_http(self):
        from shared import resume_line
        from routes import delegates
        self.assertEqual(self.app.test_client().get('/api/resume?target=dev').status_code,401)
        # A shell farewell is explicitly unverified; it still provides a recovery line before killing.
        self.tmux('new-session','-d','-s','resume_probe', '-x','160','-y','20',
                  "printf 'To continue this session, run codex resume " + ID1 + "\\n'; sleep 600")
        import time
        time.sleep(.1)
        result = self.cli('resume','resume_probe','--pane','1.1','--agent-only')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertIn('codex resume ' + ID1,result.stdout)
        result = self.cli('kill','resume_probe','--pane','1.1')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertLess(result.stdout.index('resume (unverified):'),result.stdout.index('killed resume_probe'))
        self.assertNotIn('resume:',self.cli('view','dev','--pane','1.1',silent=True).stdout)
        result = self.cli('ls','--json')
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertNotIn('next:',result.stdout)
        body=json.loads(result.stdout)
        self.assertTrue(all('resume' in p for p in body['sessions']))
        result = self.cli('ls')
        import re
        first=next(line for line in result.stdout.splitlines() if not line.startswith('next:'))
        self.assertEqual(len(re.split(r' {2,}',first)),10,first)
        result=self.cli('ls','--cwd')
        first=next(line for line in result.stdout.splitlines() if not line.startswith('next:'))
        self.assertEqual(len(re.split(r' {2,}',first)),11,first)

    def test_verified_fresh_kill_sees_new_id_on_real_fd(self):
        import sys
        import time
        from shared import resume_line
        root=Path(self.tmpdir)/'owner'
        root.mkdir()
        rollout=root/'.codex/sessions/2026/10/26'/('rollout-now-'+ID1+'.jsonl')
        rollout.parent.mkdir(parents=True)
        rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':ID1,'cwd':str(root)}})+'\n')
        script=root/'agent.py'
        script.write_text("import time,sys,pathlib\nf=open(sys.argv[1])\nprint('ready',flush=True)\n"
                          "switch=pathlib.Path('switch')\nwhile True:\n"
                          " if switch.exists():\n  new=open(switch.read_text()); f.close(); f=new; switch.unlink(); print('swapped',flush=True)\n"
                          " time.sleep(.02)\n")
        command='exec -a codex '+shlex.join([sys.executable,str(script),str(rollout)])
        self.tmux('new-session','-d','-s','verified_probe','-c',str(root),'bash -c '+shlex.quote(command))
        pane=self.pane_id('verified_probe')
        for _ in range(50):
            if 'ready' in self.screen(pane): break
            time.sleep(.02)
        with patch.object(resume_line,'RESOLVER',resume_line.Resolver(home=root)):
            result=self.cli('resume','verified_probe','--pane','1.1','--agent-only')
            self.assertEqual(result.returncode,0,result.stdout)
            self.assertEqual(result.stdout.strip(),'codex resume '+ID1)
            newer=rollout.with_name('rollout-now-'+ID2+'.jsonl')
            newer.write_text(json.dumps({'type':'session_meta','payload':{'id':ID2,'cwd':str(root)}})+'\n')
            (root/'switch').write_text(str(newer))
            for _ in range(50):
                if 'swapped' in self.screen(pane): break
                time.sleep(.02)
            result=self.cli('kill','verified_probe','--pane','1.1')
            self.assertEqual(result.returncode,0,result.stdout)
            self.assertLess(result.stdout.index('resume:'),result.stdout.index('killed verified_probe'))
            self.assertIn(ID2,result.stdout)
            self.assertNotIn(ID1,result.stdout)

    def test_launch_from_private_pane_through_http_records_parent(self):
        import subprocess
        env = dict(self.env, TMUX=str(self.socket)+',0,0', TMUX_PANE=self.pane_id('dev'))
        cli=str(Path(__file__).resolve().parents[1]/'bin/assist')
        args=[cli,'launch','--session','del_http_parent','--cwd',self.tmpdir]
        first=subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(first.returncode,0,first.stderr)
        self.assertEqual(self.tmux('show','-v','-t','=del_http_parent:','@assist_parent'),'dev')
        env['TMUX_PANE']=self.dev2_pane
        second=subprocess.run(args,env=env,capture_output=True,text=True,timeout=15)
        self.assertEqual(second.returncode,0,second.stderr)
        self.assertEqual(self.tmux('show','-v','-t','=del_http_parent:','@assist_parent'),'dev')

class ResumeHelpTests(unittest.TestCase):
    def test_resume_help_describes_session_pane_and_agent_only(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, str(root/'bin/assist'), 'resume', '-h'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ('Session name', 'Pane index', 'Print only the agent command'):
            self.assertIn(text, result.stdout)
