"""Snapshot options plus latched wake-ups; never writes terminal input."""
import json
import os
import subprocess

from shared.delegate_state import _new_memory
from shared.tmux import tmux_exact_target

PANE_OPTIONS = ('state', 'reason', 'since', 'health', 'observed', 'gen', 'agent_gen',
                'episode', 'key', 'rev', 'resume')
SESSION_OPTIONS = ('state', 'target', 'reason', 'since', 'health', 'observed', 'rev', 'resume')
SEED_OPTIONS = ('gen', 'agent_gen', 'episode', 'key', 'state', 'reason', 'since',
                'rev', 'health', 'observed', 'resume')


# tmux 3.7c accepted at most 16,364 serialized argv bytes in a private
# socket probe. Stay below half, counting encoded bytes and NUL terminators.
COMMAND_BYTE_BUDGET = 8000


def _command_size(command):
    return sum(len(os.fsencode(arg)) + 1 for arg in command)


def _command_batches(commands):
    """Pack whole commands; validate all sizes before any writes are attempted."""
    batches, argv, size = [], ['tmux'], 0
    for command in commands:
        command_size = _command_size(command)
        if command_size > COMMAND_BYTE_BUDGET:
            raise RuntimeError('delegate channel: individual command exceeds byte budget')
        separator_size = 2 if size else 0  # ';' and its NUL
        if size + separator_size + command_size > COMMAND_BYTE_BUDGET:
            batches.append(argv)
            argv, size = ['tmux'], 0
        if size:
            argv.append(';')
            size += 2
        argv.extend(command)
        size += command_size
    if size:
        batches.append(argv)
    return batches


def seed(row, generation):
    """Adopt only a matching pane generation; malformed options are not evidence."""
    try:
        if json.loads(row.get('assist_gen', '')) != list(generation):
            return None
        agent = json.loads(row.get('assist_agent_gen', '') or 'null')
        episode = int(row['assist_episode'])
        since = float(row['assist_since'])
        state = row['assist_state']
        if state not in ('working', 'waiting', 'finished', 'dead', 'unknown'):
            return None
        memory = _new_memory(generation)
        key = row.get('assist_key') or None
        memory.update(agent_gen=tuple(agent) if agent else None, episode=episode,
                      ever_present=bool(agent), idle_since_increment=state in ('waiting', 'finished', 'dead'))
        memory['published'] = dict(state=state, reason=row.get('assist_reason', ''),
                                   detail='', since=since, key=key)
        if key:
            memory['alerted'] = [[key, since]]
        return memory
    except (ValueError, TypeError, KeyError):
        return None


def _value(value):
    return '' if value is None else str(value)


class Channel:
    def __init__(self):
        self.panes = {}
        self.sessions = {}
        self.rev = 0
        self.active = False

    def restore(self, row, generation):
        """Seed publication equality and revision only from the same pane generation."""
        try:
            rev = int(row.get('assist_rev') or 0)
            self.rev = max(self.rev, rev)
            if row['pane_id'] in self.panes or seed(row, generation) is None:
                return
            values = {key: row.get('assist_' + key) for key in PANE_OPTIONS}
            for key in ('key', 'resume'):
                values[key] = values[key] or None
            values['rev'] = rev
            values['_session'] = row['session_id']
            values['_target'] = row['target']
            values['_agent'] = bool(json.loads(row.get('assist_agent_gen') or 'null'))
            self.panes[row['pane_id']] = values
        except (ValueError, TypeError):
            return

    def discover(self, rows):
        """Recover ownership for cleanup when restarted with the channel disabled."""
        if self.active or self.panes:
            return
        for row in rows:
            if row.get('assist_gen'):
                self.panes[row['pane_id']] = {'_session': row['session_id']}
                self.sessions[row['session_id']] = {}
        self.active = bool(self.panes)

    def publish(self, entries, memories, epoch, now, enabled=True, live_ids=None):
        """One command batch; commit the local snapshot only after tmux succeeds.

        live_ids=None means listing failed: retain known targets. A successful
        inventory permits cleanup of formerly watched, still-existing panes.
        """
        commands = []
        wakes = set()
        def option(scope, target, key, value):
            args = ['set-option'] + scope
            if target:
                args += ['-t', tmux_exact_target(target)]
            if value is None:
                args += ['-u']
            args += ['@assist_' + key]
            if value is not None:
                args += [_value(value)]
            commands.append(args)
        def wake(pane=None, session=None):
            wakes.add('assist-delegate')
            if pane:
                wakes.add('assist-delegate-p' + pane[1:])
            if session:
                wakes.add('assist-delegate-s' + session[1:])
        panes, sessions = {}, {}
        rev = self.rev
        if enabled:
            for entry in entries:
                pid = entry['pane_id']
                mem = memories.get(pid, {})
                values = {k: entry.get(k) for k in ('state', 'reason', 'since', 'health', 'episode', 'key')}
                values.update(observed=entry.get('observed_at'),
                              gen=json.dumps(mem.get('generation'), separators=(',', ':')),
                              agent_gen=json.dumps(mem.get('agent_gen'), separators=(',', ':')),
                              resume=(entry.get('resume') or {}).get('line'))
                old = self.panes.get(pid, {})
                changed = not old or any(values.get(k) != old.get(k) for k in
                                         ('state', 'reason', 'key', 'resume', 'health'))
                if changed:
                    rev += 1
                    wake(pid, entry['session_id'])
                values['rev'] = rev if changed else old['rev']
                values['_session'] = entry['session_id']
                values['_target'] = entry['target']
                values['_agent'] = bool(mem.get('agent_gen'))
                panes[pid] = values
                for k in PANE_OPTIONS:
                    option(['-p'], pid, k, _value(values.get(k)))
            for sid in {p['_session'] for p in panes.values()}:
                group = [(pid, p) for pid, p in panes.items() if p['_session'] == sid]
                priority = {'waiting': 0, 'dead': 1, 'finished': 2, 'working': 3, 'unknown': 4}
                def order(item):
                    p = item[1]
                    indexes = p['_target'].rsplit(':', 1)[-1].split('.')
                    return priority[p['state']], tuple(int(i) for i in indexes)
                pid, selected = min(group, key=order)
                values = {k: selected[k] for k in ('state', 'reason', 'since')}
                values.update(target=pid,
                              health=max((p['health'] for _, p in group), key={'ok': 0, 'unknown': 1, 'stale': 2}.get),
                              observed=min((p['observed'] or 0 for _, p in group)),
                              rev=max(p['rev'] for _, p in group))
                agents = [p for _, p in group if p['_agent']]
                values['resume'] = agents[0]['resume'] if len(agents) == 1 else None
                members_changed = {p for p, v in self.panes.items() if v['_session'] == sid} != {p for p, _ in group}
                if self.sessions.get(sid) and (members_changed or any(
                        values[k] != self.sessions[sid].get(k)
                        for k in ('target', 'state', 'reason', 'health', 'resume'))):
                    if values['rev'] <= self.sessions[sid]['rev']:
                        rev += 1
                        panes[pid]['rev'] = values['rev'] = rev
                        option(['-p'], pid, 'rev', rev)
                    wake(session=sid)
                elif self.sessions.get(sid):
                    values['rev'] = max(values['rev'], self.sessions[sid]['rev'])
                sessions[sid] = values
                for k in SESSION_OPTIONS:
                    option([], sid, k, values[k])
            option(['-g'], None, 'epoch', epoch)
            option(['-g'], None, 'heartbeat', now)
            if not self.active:
                wakes.add('assist-delegate')
        for pid, old in self.panes.items():
            if pid not in panes:
                if live_ids is None or pid in live_ids:
                    for k in PANE_OPTIONS:
                        option(['-p'], pid, k, None)
                wake(pid, old['_session'])
        live_sessions = {entry['session_id'] for entry in entries}
        for sid in self.sessions.keys() - sessions.keys():
            if live_ids is None or sid in live_ids or sid in live_sessions:
                for k in SESSION_OPTIONS:
                    option([], sid, k, None)
            wake(session=sid)
        if not enabled and self.active:
            for k in ('epoch', 'heartbeat'):
                option(['-g'], None, k, None)
            wakes.add('assist-delegate')
        signals = [['wait-for', '-S', name] for name in sorted(wakes)]
        all_commands = commands + signals
        total_size = sum(_command_size(c) for c in all_commands) + 2 * max(0, len(all_commands) - 1)
        if total_size <= COMMAND_BYTE_BUDGET:
            batches = _command_batches(all_commands)
        else:
            # Large snapshots finish EVERY option chunk before sending any
            # signal. Small snapshots retain their single-invocation fast path.
            batches = _command_batches(commands) + _command_batches(signals)
        for argv in batches:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=5)
            if result.returncode:
                raise RuntimeError('delegate channel: ' + result.stderr.strip())
        if batches:
            self.panes, self.sessions, self.rev, self.active = panes, sessions, rev, enabled
