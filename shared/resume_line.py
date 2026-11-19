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
_AGY_CONV = re.compile(r'^(' + UUID + r')\.db$')
_AGY_LOCK = re.compile(r'^(' + UUID + r')\.lock$')
_FAREWELL = re.compile(
    r'^\s*(?:To continue this session, run:? )?'
    r'(?:(codex resume|claude --resume) |(?:(agy --conversation)(?:=| )))(' + UUID + r')\s*$'
)
_VALUES = {'codex': {'-m','--model','-c','-p','--profile','-s','--sandbox','-a','--ask-for-approval'},
           'claude': {'--model','--permission-mode','--add-dir'},
           'gemini': {'--model','--effort','--mode','--agent','--project','--add-dir'}}
_FLAGS = {'codex': {'--yolo','--search'},
          'claude': {'--dangerously-skip-permissions'},
          'gemini': {'--dangerously-skip-permissions','--sandbox','--remote-agent','--remote-control'}}
# These flags consume a value even though their value is not retained.
_DROP_VALUES = {'--resume','--session-id','--conversation','--print','--prompt',
                '--prompt-interactive','--output-format','--input-format',
                '--settings','--system-prompt','--append-system-prompt',
                '--log-file','--json-schema','--print-timeout'}


def _is_agy_argv(argv):
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    if exe in ('agy', 'antigravity'):
        return True
    if exe in ('node', 'nodejs') and len(argv) > 1:
        script = argv[2] if argv[1] == '--' and len(argv) > 2 else argv[1]
        return os.path.basename(script).lower() in ('agy', 'agy.js')
    return False


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
        elif key in _DROP_VALUES or (kind == 'claude' and key == '-p') or (kind == 'gemini' and key in ('-p', '-i')):
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
        pid_locked = set()
        any_rollout = False
        any_agy_conv = False
        agy_seen = False
        errors = False
        claude_accepted = 0
        kinds = {a['kind'] for a in observation.agents}
        if kinds == {'claude'} and not self.claude_verified:
            return None, 'unverifiable'
        if not kinds.intersection({'codex','claude','gemini'}):
            return None, 'unsupported' if kinds else 'no agent'
        for agent in observation.agents:
            kind, pid = agent['kind'], agent['pid']
            if kind not in ('codex','claude','gemini'):
                continue
            if kind == 'claude' and not self.claude_verified:
                continue
            try:
                argv, cwd = self._process(agent)
                found = []
                pid_locks = set()
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
                elif kind == 'gemini':
                    if not _is_agy_argv(argv):
                        continue
                    agy_seen = True
                    conv_root = (self.home / '.gemini/antigravity-cli/conversations').resolve()
                    lock_root = (self.home / '.gemini/antigravity-cli/presence').resolve()
                    for fd in (self.proc / str(pid) / 'fd').iterdir():
                        try:
                            raw_link = os.readlink(fd)
                        except FileNotFoundError:
                            continue
                        if not raw_link.startswith('/'):
                            continue
                        link = Path(os.path.realpath(raw_link))
                        conv_match = _AGY_CONV.fullmatch(link.name)
                        if conv_match and link.is_relative_to(conv_root):
                            any_agy_conv = True
                            found.append((conv_match[1], 'fd'))
                            continue
                        lock_match = _AGY_LOCK.fullmatch(link.name)
                        if lock_match and link.is_relative_to(lock_root):
                            pid_locks.add(lock_match[1])
                    for index, arg in enumerate(argv):
                        if arg == '--conversation' and index + 1 < len(argv) and re.fullmatch(UUID, argv[index + 1]):
                            preferred.add((kind, argv[index + 1]))
                        elif arg.startswith('--conversation='):
                            cid = arg.split('=', 1)[1]
                            if re.fullmatch(UUID, cid):
                                preferred.add((kind, cid))
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
                    if ident in pid_locks:
                        pid_locked.add((kind, ident))
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
            # Presence-lock tie-breaking only disambiguates multiple open DBs
            # owned by ONE agy process, never mixed kinds or distinct PIDs.
            if (
                {k for k, _ in survivors} == {'gemini'}
                and len({v['agent_gen'] for v in survivors.values()}) == 1
            ):
                locked = set(survivors).intersection(pid_locked)
                if len(locked) == 1:
                    return survivors[next(iter(locked))], None
            return None, 'ambiguous'
        if kinds == {'gemini'} and not agy_seen:
            return None, 'unsupported'
        if (kinds == {'codex'} and not any_rollout) or (kinds == {'gemini'} and agy_seen and not any_agy_conv):
            return None, 'no session yet'
        return None, 'unowned'

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
            if kind == 'codex':
                base_cmd = ['codex', 'resume', ident]
            elif kind == 'gemini':
                base_cmd = ['agy', '--conversation', ident]
            else:
                base_cmd = ['claude', '--resume', ident]
            command = shlex.join(base_cmd + allowed_args(kind, owned['argv']))
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
                        prefix = match[1] or match[2]
                        ident = match[3]
                        command = prefix + ' ' + ident
                        agent = 'gemini' if prefix.startswith('agy') else prefix.split()[0]
                        record = dict(agent=agent,id=ident,cwd=None,command=command,
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
