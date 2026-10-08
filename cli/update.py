"""Install a checked upstream update; restarting remains an explicit action."""
import json
import sys
from pathlib import Path
from shared.updates import Updater, UpdateError


def command(config, check_only=False, json_output=False):
    updater = Updater(config.home)
    try:
        status = updater.check()
    except UpdateError as exc:
        print(json.dumps({'error':str(exc), 'message':str(exc)}) if json_output else str(exc))
        return 1
    code = 0
    if not check_only and (not status.get('error') or status.get('phase') == 'repair_required'):
        try:
            if status.get('phase') == 'repair_required':
                status = updater.apply(status['installed_commit'])
            elif status.get('available'):
                status = updater.apply(status['latest_commit'])
        except UpdateError as exc:
            status = {**updater.snapshot(), 'error': str(exc), 'message': str(exc)}
            code = 1
    if status.get('error') or (not check_only and status.get('blocked_reason')):
        code = 1
    status['busy'] = updater.operation_busy()
    if json_output:
        print(json.dumps(status))
    else:
        print(status.get('message', 'No update status available.'))
        if status.get('blocked_reason'): print(status['blocked_reason'])
        if status.get('restart_required') and status.get('phase') != 'repair_required':
            print('Restart when ready: assist restart (tmux sessions keep running).')
    return code


def restart_installed():
    """Restart this physical checkout through its canonical service/controller.

    Validate the install selector before reading another checkout's credentials
    or signalling any process. This entry point is used by the built-in UI
    restart aliases, independent of the caller's PATH and legacy wrappers.
    """
    from cli.config import ConfigError, resolve
    from cli.proc import restart

    home = Path(__file__).resolve().parent.parent
    try:
        config = resolve(script_path=home / 'bin' / 'assist', activation_expected_home=home)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return restart(config)


if __name__ == '__main__':
    raise SystemExit(restart_installed() if sys.argv[1:] == ['--restart-installed'] else 2)
