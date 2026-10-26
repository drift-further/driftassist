"""Generation-bound conversation ownership. Commands are returned, never executed.

Claude sessionId followed /clear in the controlled 2026-10-26 probe (task 5981).
Session-file ownership still requires the matching process start. No newest-by-directory fallback is permitted.
"""
import copy
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path

from shared.agent_identity import _ANSI_ESCAPE_RE

UUID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'
_ROLLOUT = re.compile(r'rollout-.*-(' + UUID + r')\.jsonl$')
_FAREWELL = re.compile(r'^\s*(?:To continue this session, run:? )?(codex resume|claude --resume) (' + UUID + r')\s*$')
_VALUES = {'codex': {'-m','--model','-c','-p','--profile','-s','--sandbox','-a','--ask-for-approval'},
           'claude': {'--model','--permission-mode','--add-dir'}}
_FLAGS = {'codex': {'--yolo','--search'}, 'claude': {'--dangerously-skip-permissions'}}
# These flags consume a value even though their value is not retained.
_DROP_VALUES = {'--resume','--session-id','--print','--output-format','--input-format',
                '--settings','--system-prompt','--append-system-prompt'}


def allowed_args(kind, argv):
    kept = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        key, sep, value = arg.partition('=')
        if key in _VALUES.get(kind, ()):
            if sep:
                kept.append(arg)
            elif i + 1 < len(argv):
                kept.extend([arg, argv[i + 1]])
                i += 1
        elif arg in _FLAGS.get(kind, ()):
            kept.append(arg)
        elif key in _DROP_VALUES or (kind == 'claude' and key == '-p'):
            if not sep:
                i += 1
        elif arg == '--':
            break
        i += 1
    return kept


def _line(session, cwd, command):
    launch = ['assist','launch','--session',session]
    if cwd:
        launch += ['--cwd',cwd]
    launch.append('--wait')
    return shlex.join(launch) + ' && ' + shlex.join(['assist','send',session,command,'--enter'])


class Resolver:
    def __init__(self, proc_root='/proc', home=None, claude_verified=True):
        self.proc = Path(proc_root)
        self.home = Path(home or Path.home())
        self.claude_verified = claude_verified
        self.memory = {}
        self.meta = {}
        self.lock = threading.RLock()

    def _start(self, pid):
        text = (self.proc / str(pid) / 'stat').read_text()
        return text[text.rfind(')') + 2:].split()[19]

    def _process(self, agent):
        """Re-read argv and cwd bounded by the observed generation, not stale snapshot argv."""
        pid = agent['pid']
        if self._start(pid) != str(agent['start']):
            raise ValueError('generation changed')
        path = self.proc / str(pid)
        argv = [x.decode('utf-8', errors='replace') for x in (path / 'cmdline').read_bytes().split(b'\0') if x]
        cwd = os.readlink(path / 'cwd')
        return argv, cwd

    def _metadata(self, path, fresh):
        # Read at most the header. Never load a whole conversation transcript.
        if fresh or path not in self.meta:
            with path.open(encoding='utf-8') as f:
                self.meta[path] = json.loads(f.readline(1024 * 1024))
        return self.meta[path]

    def _owned(self, pane_id, observation, fresh):
        survivors = {}
        preferred = set()
        any_rollout = False
        errors = False
        claude_accepted = 0
        kinds = {a['kind'] for a in observation.agents}
        if kinds == {'claude'} and not self.claude_verified:
            return None, 'unverifiable'
        if not kinds.intersection({'codex','claude'}):
            return None, 'unsupported' if kinds else 'no agent'
        for agent in observation.agents:
            kind, pid = agent['kind'], agent['pid']
            if kind not in ('codex','claude'):
                continue
            if kind == 'claude' and not self.claude_verified:
                continue
            try:
                argv, cwd = self._process(agent)
                found = []
                if kind == 'codex':
                    root = (self.home / '.codex/sessions').resolve()
                    for fd in (self.proc / str(pid) / 'fd').iterdir():
                        try:
                            link = Path(os.readlink(fd))
                        except FileNotFoundError:
                            continue
                        match = _ROLLOUT.fullmatch(link.name)
                        if not match or not link.is_relative_to(root):
                            continue
                        any_rollout = True
                        meta = self._metadata(link, fresh)
                        if not isinstance(meta, dict):
                            continue
                        payload = meta.get('payload') or {}
                        if not isinstance(payload, dict):
                            continue
                        source = payload.get('source')
                        if (meta.get('type') != 'session_meta'
                                or (isinstance(source, dict) and 'subagent' in source)
                                or payload.get('parent_thread_id') or payload.get('forked_from_id')
                                or payload.get('cwd') != cwd or payload.get('id') != match[1]):
                            continue
                        found.append((match[1], 'fd'))
                    for index, arg in enumerate(argv[:-1]):
                        if arg == 'resume' and re.fullmatch(UUID, argv[index + 1]):
                            preferred.add((kind, argv[index + 1]))
                else:
                    path = self.home / '.claude/sessions' / f'{pid}.json'
                    data = json.loads(path.read_text())
                    if not isinstance(data, dict):
                        continue
                    tmux = data.get('tmux')
                    if isinstance(tmux, dict):
                        tmux = tmux.get('pane_id') or tmux.get('pane')
                    elif isinstance(tmux, str):
                        binding = re.fullmatch(r'.+:@\d+\.(%\d+)', tmux)
                        if binding:
                            tmux = binding[1]
                    if (str(data.get('procStart')) == str(agent['start'])
                            and ('tmux' not in data or tmux == pane_id)
                            and re.fullmatch(UUID, str(data.get('sessionId', '')))):
                        found.append((data['sessionId'], 'session file'))
                        claude_accepted += 1
                if self._start(pid) != str(agent['start']):
                    errors = True
                    continue
                for ident, source in found:
                    survivors[(kind, ident)] = dict(agent=kind,id=ident,cwd=cwd,source=source,
                                                     argv=argv,agent_gen=(pid,agent['start']))
            except (OSError, ValueError, IndexError, TypeError):
                errors = True
        if errors:
            return None, 'unowned'
        if claude_accepted > 1:
            return None, 'ambiguous'
        chosen = set(survivors).intersection(preferred)
        if len(chosen) == 1:
            return survivors[next(iter(chosen))], None
        if len(survivors) == 1:
            return next(iter(survivors.values())), None
        if len(survivors) > 1:
            return None, 'ambiguous'
        return None, 'no session yet' if kinds == {'codex'} and not any_rollout else 'unowned'

    def resolve(self, pane_id, session, observation, now=None, fresh=False, cache_seconds=0, tail='', pane_dead=False):
        now = time.time() if now is None else now
        with self.lock:
            if pane_dead:
                old = self.memory.get(pane_id)
                root = old['generation'][1] if old else observation.root_start
                observation = observation._replace(complete=True, agents=[], agent=None, root_start=root)
            return self._resolve(pane_id, session, observation, now, fresh, cache_seconds, tail)

    def _resolve(self, pane_id, session, obs, now, fresh, cache_seconds, tail):
        old = self.memory.get(pane_id)
        if not obs.complete:
            if fresh:
                return None, 'observation incomplete'
            return (copy.deepcopy(old['record']), old['reason']) if old else (None, 'observation incomplete')
        generation = (pane_id, obs.root_start)
        if old and old['generation'] != generation:
            old = None
        current_agents = {(a['pid'],a['start']) for a in obs.agents}
        if old and not fresh and cache_seconds and now - old['checked_at'] < cache_seconds:
            if old['agents'] == current_agents and old['session'] == session:
                return copy.deepcopy(old['record']), old['reason']
        previous = old['record'] if old else None
        owned, reason = self._owned(pane_id, obs, fresh)
        record = None
        rev = old['rev'] if old else 0
        if owned:
            if not previous or (previous['agent'],previous['id']) != (owned['agent'],owned['id']):
                rev += 1
            kind, ident = owned['agent'], owned['id']
            command = shlex.join([kind, 'resume' if kind == 'codex' else '--resume', ident]
                                 + allowed_args(kind, owned['argv']))
            record = {k:owned[k] for k in ('agent','id','cwd','source')}
            record.update(command=command,line=_line(session,owned['cwd'],command),live=True,
                          verified=True,conv_rev=rev,verified_at=now)
            owner = owned['agent_gen']
        elif not obs.agents and previous and now - previous['verified_at'] <= 86400:
            record = dict(previous, live=False)
            record['line'] = _line(session, record['cwd'], record['command'])
            reason = None
            owner = old['owner']
        else:
            owner = None
            if previous:
                rev += 1
            if not obs.agents and not obs.wrapper and old is None:
                for row in _ANSI_ESCAPE_RE.sub('', tail).splitlines()[-20:]:
                    match = _FAREWELL.fullmatch(row)
                    if match:
                        command = match[1] + ' ' + match[2]
                        record = dict(agent=match[1].split()[0],id=match[2],cwd=None,command=command,
                                      line=_line(session,None,command),source='farewell',live=False,
                                      verified=False,conv_rev=rev,verified_at=now)
                        reason = None
        self.memory[pane_id] = dict(generation=generation,agents=current_agents,session=session,
                                    record=record,reason=reason,rev=rev,owner=owner,checked_at=now)
        return copy.deepcopy(record), reason

    def cached(self, pane_id, session, root, now):
        with self.lock:
            old = self.memory.get(pane_id)
            if (old and old['generation'] == (pane_id, root) and old['session'] == session
                    and now - old['checked_at'] < 30):
                return copy.deepcopy(old['record']), old['reason']
        return None

    def forget_missing(self, live_ids):
        with self.lock:
            for pid in self.memory.keys() - set(live_ids):
                self.memory.pop(pid, None)
            # Header cache is only an optimization; cap memory for long-running watchers.
            if len(self.meta) > 1024:
                self.meta.clear()


RESOLVER = Resolver()
