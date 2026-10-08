"""Installed-source updates shared by the CLI and authenticated web UI."""
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


UPSTREAM_REF = 'refs/drift-assist/upstream'


class UpdateError(Exception):
    pass


def source_version(home):
    try:
        match = re.search(r'^version\s*=\s*"([^"]+)"', (home / 'pyproject.toml').read_text(), re.M)
        return match[1] if match else 'unknown'
    except OSError:
        return 'unknown'


class Updater:
    def __init__(self, home, state_dir=None, running_commit=None):
        self.home = Path(home).resolve()
        base = Path(state_dir or Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')) / 'drift-assist')
        self.directory = base / ('updates-' + hashlib.sha256(str(self.home).encode()).hexdigest()[:16])
        self.running_commit = running_commit
        self.running_version = source_version(self.home)

    def _run(self, argv, purpose, timeout=30):
        environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'Never',
                       'GIT_SSH_COMMAND': (os.environ.get('GIT_SSH_COMMAND') or 'ssh') + ' -o BatchMode=yes -o ConnectTimeout=10'}
        try:
            result = subprocess.run(argv, cwd=self.home, env=environment, capture_output=True,
                                    text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(f'Could not {purpose}. Check the host terminal and try again.') from exc
        return result

    def _git(self, *args, required=True):
        result = self._run(['git', '-C', str(self.home), *args], 'read or update the configured Git source')
        if required and result.returncode:
            raise UpdateError('Git could not complete the request. Check the configured upstream and network on the host.')
        return result

    def commit(self):
        result = self._git('rev-parse', 'HEAD', required=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _read(self):
        try:
            value = json.loads((self.directory / 'state.json').read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def snapshot(self):
        state = self._read()
        state.setdefault('phase', 'unchecked')
        state.setdefault('message', 'Check for updates to this installation.')
        state.setdefault('version', self.running_version)
        state['observed_at'] = time.time()
        if self.running_commit is not None:
            state['running_commit'] = self.running_commit
            state['running_version'] = self.running_version
            state['restart_required'] = bool(state.get('installed_commit') and state['installed_commit'] != self.running_commit)
        state['busy'] = self.operation_busy()
        return {key: value for key, value in state.items() if key != 'prepared_directory'}

    def settle_restart(self):
        """Clear a persisted restart flag once a server runs the installed commit."""
        try:
            state = self._read()
            if (state.get('restart_required') and self.running_commit
                    and state.get('installed_commit') == self.running_commit
                    and state.get('phase') != 'repair_required'):
                with self._locked():
                    self._save(restart_required=False, message='This installation is up to date.')
        except (OSError, UpdateError):
            pass

    def operation_busy(self):
        try:
            fd = os.open(self.directory / 'operation.lock', os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return False
            except BlockingIOError:
                return True
        finally:
            os.close(fd)

    def _save(self, **changes):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        state = {**self._read(), **changes}
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix='.state-')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(state, stream)
            os.replace(temporary, self.directory / 'state.json')
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return self.snapshot()

    @contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / 'operation.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            for attempt in range(3):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if attempt == 2:
                        raise UpdateError('Another update operation is running. Check its status before trying again.') from exc
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def _local(self):
        root = self._git('rev-parse', '--show-toplevel', required=False)
        if root.returncode or Path(root.stdout.strip()).resolve() != self.home:
            raise UpdateError('This source directory has no Git update source. Download installation updates will be enabled when distribution is configured.')
        head = self.commit()
        branch = self._git('symbolic-ref', '--quiet', '--short', 'HEAD', required=False).stdout.strip()
        if not head or not branch:
            raise UpdateError('This checkout is not on a branch. Choose its update branch on the host first.')
        remote = self._git('config', '--get', f'branch.{branch}.remote', required=False).stdout.strip()
        ref = self._git('config', '--get', f'branch.{branch}.merge', required=False).stdout.strip()
        if (not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', remote) or not ref.startswith('refs/heads/')
                or self._git('check-ref-format', ref, required=False).returncode):
            raise UpdateError('No remote upstream is configured for this branch. Configure its tracking branch on the host first.')
        dirty = bool(self._git('status', '--porcelain', '--untracked-files=normal').stdout)
        return head, remote, ref, dirty

    def check(self):
        with self._locked():
            try:
                head, remote, ref, dirty = self._local()
                old = self._read()
                if old.get('phase') == 'repair_required':
                    return self._save(installed_commit=head, dirty=dirty, available=False,
                                      message='The update needs dependency repair before restart. Run assist update or choose Repair dependencies.')
                # A private ref: FETCH_HEAD is rewritten by any manual fetch in this checkout.
                self._git('fetch', '--no-tags', remote, f'+{ref}:{UPSTREAM_REF}')
                latest = self._git('rev-parse', UPSTREAM_REF + '^{commit}').stdout.strip()
                ahead = self._git('merge-base', '--is-ancestor', latest, head, required=False).returncode == 0
                forward = self._git('merge-base', '--is-ancestor', head, latest, required=False).returncode == 0
                available = latest != head and forward
                block = ('Local changes need review before updating.' if dirty else
                         'This branch has diverged from its upstream. Resolve it on the host.' if not forward and not ahead else None)
                message = ('An update is available.' if available else 'This installation is up to date.' if ahead else block)
                return self._save(phase='ready', installed_commit=head, latest_commit=latest,
                                  version=source_version(self.home), source=ref[len('refs/heads/'):],
                                  available=available, dirty=dirty, blocked_reason=block,
                                  checked_at=time.time(), message=message, error=None)
            except UpdateError as exc:
                return self._save(phase='repair_required' if self._read().get('phase') == 'repair_required' else 'check_failed', available=False, error=str(exc),
                                  message=str(exc), checked_at=time.time())

    def _dependencies(self, directory, install=False):
        python = self.home / '.venv/bin/python'
        if not python.is_file():
            raise UpdateError('The project virtual environment is missing. Repair it with bash install.sh --dependencies-only.')
        wheels = directory / 'wheels'; wheels.mkdir(exist_ok=True)
        args = [str(python), '-m', 'pip']
        if install:
            args += ['install', '--no-index', '--find-links', str(wheels)]
        else:
            args += ['download', '--only-binary=:all:', '--dest', str(wheels)]
        args += ['--require-hashes', '-r', str(directory / 'requirements.lock')]
        result = self._run(args, 'install pinned dependencies' if install else 'download verified update dependencies', timeout=180)
        if result.returncode:
            raise UpdateError('Dependency installation failed. Run assist update to repair before restarting.' if install else
                              'Could not download hash-verified dependencies. Installed code was left unchanged; check the network and package source on the host.')

    def apply(self, expected_commit):
        if not isinstance(expected_commit, str) or not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})', expected_commit):
            raise UpdateError('Check for updates and review the available version first.')
        with self._locked():
            state = self._read()
            head, _, _, dirty = self._local()
            if dirty:
                raise UpdateError('Local changes need review before updating. Nothing was changed.')
            repair = state.get('phase') == 'repair_required' and head == expected_commit == state.get('installed_commit')
            if not repair and (state.get('latest_commit') != expected_commit or state.get('installed_commit') != head
                               or not state.get('available')):
                raise UpdateError('The installed or available version changed. Check for updates again.')
            if not repair and self._git('merge-base', '--is-ancestor', head, expected_commit, required=False).returncode:
                raise UpdateError('This update cannot be applied as a fast-forward. Resolve the branch on the host.')
            directory = Path(tempfile.mkdtemp(dir=self.directory, prefix='prepared-'))
            try:
                project = self._git('show', f'{expected_commit}:pyproject.toml').stdout
                constraint = re.search(r'^requires-python\s*=\s*[\'"]([^\'"]+)[\'"]', project, re.M)
                if constraint:
                    floor = re.fullmatch(r'>=\s*(\d+)\.(\d+)(?:\.(\d+))?', constraint[1])
                    if not floor:
                        raise UpdateError('This Python requirement needs a manual upgrade. Review the new installer on the host.')
                    minimum = tuple(int(value or 0) for value in floor.groups())
                    if sys.version_info[:3] < minimum:
                        raise UpdateError(f'The update requires Python {constraint[1]}. Upgrade Python before installing; code was left unchanged.')
                lock = self._git('show', f'{expected_commit}:requirements.lock').stdout
                (directory / 'requirements.lock').write_text(lock)
                self._save(phase='downloading', message='Downloading and verifying dependencies…', error=None)
                self._dependencies(directory)
                # Preparation may take time; recheck before touching installed code.
                fresh_head, _, _, fresh_dirty = self._local()
                if fresh_head != head or fresh_dirty:
                    raise UpdateError('Local source changed while the update was prepared. Installed code was left unchanged.')
                if not repair:
                    self._git('merge', '--ff-only', expected_commit)
                # Persist repair-needed until installation finishes, so a process
                # exit at any point after the fast-forward can safely resume.
                self._save(phase='repair_required', installed_commit=expected_commit, prepared_directory=str(directory),
                           restart_required=True, message='Installing verified dependencies…')
                try:
                    self._dependencies(directory, install=True)
                except UpdateError as exc:
                    self._save(phase='repair_required', available=False, error=str(exc), message=str(exc))
                    raise
                return self._save(phase='installed', available=False, blocked_reason=None, dirty=False,
                                  version=source_version(self.home), error=None, prepared_directory=None,
                                  restart_required=True, message='Update installed. Restart to take effect.')
            except UpdateError as exc:
                if self._read().get('phase') != 'repair_required':
                    self._save(phase='apply_failed', error=str(exc), message=str(exc))
                raise
            finally:
                import shutil
                shutil.rmtree(directory)


class UpdateManager:
    """One background job; request and poll handlers never fetch or install."""
    def __init__(self, updater):
        self.updater = updater
        self.lock = threading.Lock()
        self.busy = False

    def snapshot(self):
        status = self.updater.snapshot()
        status['busy'] = self.busy or status.get('busy', False)
        return status

    def start(self, action='check', expected_commit=None):
        with self.lock:
            if self.busy or self.updater.operation_busy():
                raise UpdateError('An update operation is already running.')
            self.busy = True
        def work():
            try:
                if action == 'check': self.updater.check()
                else: self.updater.apply(expected_commit)
            except UpdateError as exc:
                if not self.updater.operation_busy(): self.updater._save(error=str(exc), message=str(exc))
            except Exception:
                if not self.updater.operation_busy():
                    self.updater._save(error='The update could not complete. Check disk space and file permissions on the host.',
                                       message='The update could not complete. Check its status before restarting.')
            finally:
                with self.lock: self.busy = False
        threading.Thread(target=work, daemon=True).start()

    def watch(self):
        time.sleep(15)
        while True:
            try:
                if not self.busy: self.start()
            except UpdateError:
                pass
            time.sleep(3600)


_manager = None
_manager_lock = threading.Lock()


def manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            from shared.state import DATA_DIR
            updater = Updater(DATA_DIR)
            updater.running_commit = updater.commit()
            updater.settle_restart()
            _manager = UpdateManager(updater)
        return _manager
