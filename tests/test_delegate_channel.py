"""Channel contract on a private tmux server; no input delivery is permitted."""
import subprocess
import time
from unittest.mock import patch
from tests.test_delegate_watch import _WatchCase


class ChannelTests(_WatchCase):
    def setUp(self):
        super().setUp()
        from shared.delegate_channel import Channel
        self.channel = Channel()
        p = patch.object(self.d, '_CHANNEL', self.channel)
        p.start()
        self.addCleanup(p.stop)
        self.pane = self.delegate('del_channel')
        self.sid = self.tmux('display', '-p', '-t', self.pane, '#{session_id}')
        self.now = time.time()

    def option(self, target, name):
        return self.tmux('show-options', *(['-p'] if target.startswith('%') else []),
                         '-v', '-t', target, '@assist_' + name, check=False)

    def tick(self, delta=0):
        self.d.tick(self.now + delta)

    def drain(self, channel):
        subprocess.run(['tmux', '-S', str(self.socket), 'wait-for', channel], timeout=1, check=True)

    def blocked(self, channel):
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(['tmux', '-S', str(self.socket), 'wait-for', channel], timeout=.15)

    def test_finished_no_writes_and_one_batch(self):
        before = self.screen(self.pane)
        real = subprocess.run
        with patch('subprocess.run', wraps=real) as run:
            self.tick()
            self.tick(46)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertFalse(any('send-keys' in c or 'pipe-pane' in c for c in calls))
        self.assertEqual(len([c for c in calls if 'set-option' in c]), 2)
        self.assertEqual(len([c for c in calls if 'set-option' in c or 'wait-for' in c]), 2)
        self.assertEqual(self.screen(self.pane), before)
        self.assertEqual(self.option(self.pane, 'state'), 'finished')
        self.assertEqual(self.option(self.pane, 'health'), 'ok')

    def test_unchanged_and_question_keys_wake(self):
        self.tick()
        channel = 'assist-delegate-p' + self.pane[1:]
        self.drain(channel)
        rev = self.option(self.pane, 'rev')
        real_run = subprocess.run
        with patch('subprocess.run', wraps=real_run) as run:
            self.tick(5)
        self.assertFalse(any('wait-for' in c.args[0] for c in run.call_args_list))
        self.assertEqual(self.option(self.pane, 'rev'), rev)
        real = self.d._build_sample
        key = ['one']
        def question(*args):
            s = real(*args)
            s['attention'] = {'kind': 'question', 'key': key[0], 'summary': 'Choose'}
            return s
        with patch.object(self.d, '_build_sample', side_effect=question):
            self.tick(10)
            self.tick(20)
            self.drain(channel)
            rev = self.option(self.pane, 'rev')
            self.tick(25)
            key[0] = 'two'
            self.tick(30)
            self.tick(40)
            self.drain(channel)
            self.assertNotEqual(self.option(self.pane, 'rev'), rev)

    def test_summary_stale_recovery_rename_and_removal(self):
        other = self.tmux('split-window', '-d', '-P', '-F', '#{pane_id}', '-t', self.pane,
                          "bash -c 'exec -a claude sleep 600'")
        time.sleep(.1)
        real = self.d._build_sample
        fail = [False]
        def sample(row, *args):
            if fail[0] and row['pane_id'] == other:
                raise OSError('capture failed')
            s = real(row, *args)
            if row['pane_id'] == self.pane:
                s['attention'] = {'kind': 'question', 'key': 'one', 'summary': 'Choose'}
            return s
        with patch.object(self.d, '_build_sample', side_effect=sample):
            self.tick()
            self.drain("assist-delegate-s" + self.sid[1:])
            self.tick(10)
            self.assertEqual(self.option(self.sid, 'state'), 'waiting')
            self.assertEqual(self.option(self.sid, 'target'), self.pane)
            self.assertEqual(self.option(other, 'state'), 'working')
            rev = self.option(self.pane, 'rev')
            observed = self.option(other, 'observed')
            channel = 'assist-delegate-s' + self.sid[1:]
            self.drain(channel)
            self.tmux('rename-session', '-t', self.sid, 'del_renamed')
            fail[0] = True
            self.tick(15)
            self.drain(channel)
            self.assertEqual(self.option(self.sid, 'health'), 'stale')
            self.assertEqual(self.option(other, 'observed'), observed)
            self.assertEqual(self.option(self.pane, 'rev'), rev)
            fail[0] = False
            self.tick(20)
            self.drain(channel)
            self.assertEqual(self.option(self.sid, 'health'), 'ok')
        self.tmux('kill-pane', '-t', self.pane)
        self.tick(25)
        self.assertEqual(self.option(self.sid, 'target'), other)
        self.assertEqual(self.option(self.sid, 'rev'), self.option(other, 'rev'))

    def test_restart_seed_and_different_generation(self):
        self.tick()
        self.tick(46)
        key = self.option(self.pane, 'key')
        from shared.delegate_channel import Channel
        self.d._MEMORY.clear()
        self.d._CHANNEL = Channel()
        self.tick(50)
        self.assertEqual(self.option(self.pane, 'key'), key)
        self.assertEqual(self.d._MEMORY[self.pane]['episode'], 1)
        self.tmux('set', '-p', '-t', self.pane, '@assist_gen', '["wrong"]')
        self.tmux('set', '-p', '-t', self.pane, '@assist_episode', '88')
        self.d._MEMORY.clear()
        self.tick(55)
        self.assertEqual(self.d._MEMORY[self.pane]['episode'], 1)
        self.d._MEMORY.clear()
        real = self.d._build_sample
        def replacement(*args):
            sample = real(*args)
            sample['identity']['agent_pid'] += 1
            return sample
        with patch.object(self.d, '_build_sample', side_effect=replacement):
            self.tick(60)
        self.assertEqual(self.d._MEMORY[self.pane]['episode'], 2)

    def test_watch_off_unsets_owned_options_but_preserves_parent(self):
        self.tick()
        self.tmux('set', '-t', self.sid, '@assist_parent', 'dev')
        channel = 'assist-delegate-s' + self.sid[1:]
        self.drain(channel)
        self.settings['watch'] = 'off'
        self.tick(5)
        self.drain(channel)
        self.assertEqual(self.option(self.pane, 'state'), '')
        self.assertEqual(self.option(self.sid, 'state'), '')
        self.assertEqual(self.option(self.sid, 'parent'), 'dev')
        self.tick(10)
        self.blocked(channel)

    def test_dev_dev2_collision(self):
        self.settings['prefixes'] = ['dev']
        self.tick()
        self.tmux('kill-session', '-t', '=dev:')
        self.tick(5)
        self.assertEqual(self.option(self.dev2_pane, 'health'), 'ok')

    def test_restart_with_watch_off_cleans_existing_options(self):
        self.tick()
        from shared.delegate_channel import Channel
        self.d._CHANNEL = Channel()
        self.d._MEMORY.clear()
        self.settings['watch'] = 'off'
        self.tick(5)
        self.assertEqual(self.option(self.pane,'state'),'')
        self.assertEqual(self.option(self.sid,'state'),'')

    def test_resume_changes_wake_and_unset(self):
        record = {'line': 'assist restore one'}
        with patch('shared.resume_line.RESOLVER.resolve', side_effect=lambda *a, **k: (dict(record) if record else None,None)):
            self.tick()
            channel='assist-delegate-p'+self.pane[1:]
            self.drain(channel)
            before=self.option(self.pane,'rev')
            record['line']='assist restore two'
            self.tick(5)
            self.drain(channel)
            self.assertNotEqual(self.option(self.pane,'rev'),before)
            self.assertEqual(self.option(self.pane,'resume'),record['line'])
            record.clear()
            self.tick(10)
            self.drain(channel)
            self.assertEqual(self.option(self.pane,'resume'),'')

    def test_no_bytes_received_in_a_raw_pane(self):
        import shlex
        import sys
        from pathlib import Path
        received=Path(self.tmpdir)/'received'
        program="import os,tty,sys; tty.setraw(0); f=open(sys.argv[1],'wb',buffering=0); [f.write(os.read(0,4096)) for _ in iter(int,1)]"
        raw=self.delegate('del_raw',shlex.join([sys.executable,'-u','-c',program,str(received)]))
        for _ in range(50):
            if received.exists(): break
            time.sleep(.02)
        self.tick()
        self.tick(46)
        self.assertEqual(received.read_bytes(),b'')

    def test_incomplete_observation_keeps_state_and_marks_unknown(self):
        self.tick()
        real=self.d.observe_process_tree
        def incomplete(*args):
            return real(*args)._replace(complete=False, agent=None, agents=[])
        with patch.object(self.d,'observe_process_tree',side_effect=incomplete):
            self.tick(5)
        self.assertEqual(self.option(self.pane,'state'),'working')
        self.assertEqual(self.option(self.pane,'health'),'unknown')

    def test_vanished_pane_does_not_abort_other_publications(self):
        other = self.delegate('del_survivor')
        real = self.d._build_sample
        def vanish(row, *args):
            sample = real(row, *args)
            if row['pane_id'] == self.pane:
                self.tmux('kill-session', '-t', self.sid)
            return sample
        with patch.object(self.d, '_build_sample', side_effect=vanish):
            self.tick()
        self.assertEqual(self.option(other, 'state'), 'working')
        self.assertIsNone(self.d.snapshot()['watch']['error'])
        self.assertNotIn(self.pane, self.panes())

    def test_restart_keeps_revisions_and_only_wakes_host(self):
        from shared.delegate_channel import Channel
        self.delegate('del_other')
        self.tick()
        self.tick(46)
        rev = self.option(self.pane, 'rev')
        self.d._MEMORY.clear()
        self.d._CHANNEL = Channel()
        real = subprocess.run
        with patch('subprocess.run', wraps=real) as run:
            self.tick(50)
        self.assertEqual(self.option(self.pane, 'rev'), rev)
        signals = [args[i+2] for c in run.call_args_list for args in [c.args[0]]
                   for i, word in enumerate(args) if word == 'wait-for']
        self.assertEqual(signals, ['assist-delegate'])
        self.assertGreaterEqual(self.d._CHANNEL.rev, int(rev))

    def test_empty_pane_resume_does_not_inherit_session_resume(self):
        shell = self.tmux('split-window', '-d', '-P', '-F', '#{pane_id}', '-t', self.pane, 'sleep 600')
        time.sleep(.1)
        with patch('shared.resume_line.RESOLVER.resolve', side_effect=lambda pid, *a, **kw:
                   ({'line': 'restore agent'}, None) if pid == self.pane else (None, 'no agent')):
            self.tick()
        self.assertEqual(self.option(self.sid, 'resume'), 'restore agent')
        self.assertEqual(self.tmux('display', '-p', '-t', shell, '#{@assist_resume}'), '')

    def test_identical_question_key_has_no_revision_or_signal(self):
        real = self.d._build_sample
        def question(*args):
            sample = real(*args)
            sample['attention'] = {'kind': 'question', 'key': 'identical', 'summary': 'Choose'}
            return sample
        with patch.object(self.d, '_build_sample', side_effect=question):
            self.tick()
            self.tick(10)
            rev = self.option(self.pane, 'rev')
            with patch('subprocess.run', wraps=subprocess.run) as run:
                self.tick(15)
            self.assertEqual(self.option(self.pane, 'rev'), rev)
            self.assertFalse(any('wait-for' in call.args[0] for call in run.call_args_list))

    def test_stale_revision_single_wake_and_fresh_heartbeat(self):
        self.tick()
        rev = int(self.option(self.pane, 'rev'))
        with patch.object(self.d, '_capture', return_value=None):
            with patch('subprocess.run', wraps=subprocess.run) as run:
                self.tick(31)
            self.assertGreater(int(self.option(self.pane, 'rev')), rev)
            args = next(c.args[0] for c in run.call_args_list if 'set-option' in c.args[0])
            self.assertEqual(args.count('assist-delegate-p' + self.pane[1:]), 1)
            self.assertEqual(self.tmux('show', '-g', '-v', '@assist_heartbeat'), str(self.now + 31))
            with patch('subprocess.run', wraps=subprocess.run) as run:
                self.tick(36)
            self.assertFalse(any('wait-for' in c.args[0] for c in run.call_args_list))

    def test_dev_dev2_options_are_placed_on_exact_ids(self):
        self.settings['prefixes'] = ['dev']
        dev = self.pane_id('dev')
        real = self.d._build_sample
        def sample(row, *args):
            s = real(row, *args)
            if row['pane_id'] == dev:
                s['pane_dead'] = True
            return s
        with patch.object(self.d, '_build_sample', side_effect=sample):
            self.tick()
        self.assertEqual(self.option(dev, 'state'), 'dead')
        self.assertEqual(self.option(self.dev2_pane, 'state'), 'unknown')
        self.assertEqual(self.option('=dev:', 'target'), dev)
        self.assertEqual(self.option('=dev2:', 'target'), self.dev2_pane)

    def large_publication(self, count=60):
        entries, memories = [], {}
        for index in range(count):
            pid = self.tmux('new-window', '-d', '-P', '-F', '#{pane_id}', '-t', self.sid, 'sleep 600')
            target = self.tmux('display', '-p', '-t', pid, '#{session_name}:#{window_index}.#{pane_index}')
            entries.append(dict(pane_id=pid, session_id=self.sid, target=target, state='waiting',
                                reason='question', since=self.now, health='ok', observed_at=self.now,
                                episode=1, key=f'question-{index}',
                                resume={'line': 'codex resume ' + ('é' * 700) + str(index)}))
            memories[pid] = dict(generation=(pid, '123'), agent_gen=(index+100, '123'))
        return entries, memories

    def test_large_publication_chunks_all_writes_before_single_signals(self):
        entries, memories = self.large_publication()
        real = subprocess.run
        calls = []
        def record(args, *a, **kw):
            calls.append(list(args))
            if 'wait-for' in args and 'set-option' not in args:
                # Every option is already visible before the first signal invocation.
                for entry in entries:
                    value = real(['tmux', '-S', str(self.socket), 'show-options', '-p', '-v',
                                  '-t', entry['pane_id'], '@assist_resume'],
                                 capture_output=True, text=True, check=True).stdout.rstrip('\n')
                    self.assertEqual(value, entry['resume']['line'])
            return real(args, *a, **kw)
        with patch('subprocess.run', side_effect=record):
            self.channel.publish(entries, memories, 'test-epoch', self.now)
        from shared.delegate_channel import COMMAND_BYTE_BUDGET
        self.assertGreater(len(calls), 1)
        for args in calls:
            if 'wait-for' in args:
                self.assertNotIn('set-option', args)
            self.assertLessEqual(sum(len(a.encode()) + 1 for a in args[1:]), COMMAND_BYTE_BUDGET)
            self.assertFalse('send-keys' in args or 'pipe-pane' in args)
        signals = [args[i+2] for args in calls for i, word in enumerate(args) if word == 'wait-for']
        expected = ['assist-delegate', 'assist-delegate-s' + self.sid[1:]]
        expected += ['assist-delegate-p' + entry['pane_id'][1:] for entry in entries]
        self.assertCountEqual(signals, expected)
        for channel in expected:
            self.drain(channel)
        for entry in entries:
            self.assertEqual(self.option(entry['pane_id'], 'state'), 'waiting')
        self.assertEqual(len(self.channel.panes), len(entries))
        from shared.delegate_channel import PANE_OPTIONS
        format_string = '#{pane_id}\t' + '\t'.join('#{@assist_' + key + '}' for key in PANE_OPTIONS)
        rows = self.tmux('list-panes', '-a', '-F', format_string).splitlines()
        for row in rows:
            fields = row.split('\t')
            expected = self.channel.panes.get(fields[0])
            if expected:
                self.assertEqual(fields[1:], [str(expected[key]) if expected[key] is not None else ''
                                              for key in PANE_OPTIONS])
        self.assertEqual(self.option(self.sid, 'state'), 'waiting')
        self.assertEqual(self.tmux('show', '-g', '-v', '@assist_epoch'), 'test-epoch')

    def test_failed_middle_chunk_preserves_cache_and_sends_no_signals(self):
        import copy
        entries, memories = self.large_publication(12)
        self.channel.publish(entries, memories, 'test-epoch', self.now)
        before = copy.deepcopy((self.channel.panes, self.channel.sessions, self.channel.rev, self.channel.active))
        for entry in entries:
            entry['reason'] = 'approval'
        real = subprocess.run
        calls = []
        def fail_second(args, *a, **kw):
            calls.append(list(args))
            if len(calls) == 2:
                return subprocess.CompletedProcess(args, 1, '', 'injected chunk failure')
            return real(args, *a, **kw)
        with patch('subprocess.run', side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, 'injected chunk failure'):
                self.channel.publish(entries, memories, 'test-epoch', self.now+5)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any('wait-for' in args for args in calls))
        self.assertEqual((self.channel.panes, self.channel.sessions, self.channel.rev, self.channel.active), before)
        # An idempotent retry repairs all writes and commits the new snapshot.
        self.channel.publish(entries, memories, 'test-epoch', self.now+10)
        for entry in entries:
            self.assertEqual(self.option(entry['pane_id'], 'reason'), 'approval')
