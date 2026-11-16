"""Where Drift Assist keeps its code, configuration and runtime state.

Standard library only, and no import of application state: serve.py,
bin/assist and assist-ctl resolve paths, take the layout lock and check the
migration marker through this module BEFORE anything loads settings or reads
a credential.

Two kinds of root:

    APP_ROOT      the checkout: code and static assets. Never written at runtime.
    config_dir()  secrets and settings (auth_token, settings.json, the env file)
    data_dir()    mutable runtime state (history, drafts, devices, provenance…)
    state_dir()   PID, log, the migration marker and its lock

Layout. An install is on the *legacy* layout — config and data dirs are the
checkout itself, as before — until `install.json` exists in the XDG config dir
naming this checkout; then it is on the *xdg* layout:

    config  ${XDG_CONFIG_HOME:-~/.config}/drift-assist           ASSIST_CONFIG_DIR
    data    ${XDG_DATA_HOME:-~/.local/share}/drift-assist-data   ASSIST_DATA_DIR
    state   ${XDG_STATE_HOME:-~/.local/state}/drift-assist   ASSIST_STATE_DIR

An override variable always wins for its root. `install.json` names exactly
one checkout: any other checkout refuses those directories
(`layout_owned_by_other_checkout`) instead of mixing its state into them, and
gets its own roots through the overrides.

Locking. Every entry path holds a SHARED flock on `state_dir()/migrate.lock`
for as long as it can read or write the layout (the server for its life, a CLI
verb for its run); the migration tool takes it EXCLUSIVE. While a migration
marker (`state_dir()/migration.json`) says a transaction is incomplete, or
cannot be read, every entry refuses with `migration_in_progress`.

Run as a script, it answers assist-ctl (bash) the same questions:
    python3 -I shared/paths.py env-file --checkout DIR
    python3 -I shared/paths.py enter --checkout DIR --fd N
and the installer:
    python3 -I shared/paths.py layout | select-xdg --checkout DIR
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
NAME = "drift-assist"
DATA_NAME = "drift-assist-data"
INSTALL_JSON = "install.json"
MARKER = "migration.json"
MARKER_SCHEMA = 2
MIGRATION_PHASES = ("preflight", "copying", "validating", "publishing", "committed", "cleaning", "done")
LOCK = "migrate.lock"
# The env file's name in the XDG config dir; on the legacy layout it is the
# checkout's `.env`, as it always was.
XDG_ENV_NAME = "server.env"
RECOVERY_HINT = (
    "Run: assist migrate-state --resume   (finish it)\n"
    "  or: assist migrate-state --rollback (undo it)"
)

# The process-lifetime lock descriptor: closing it would release the lock.
_held_lock = None


class LayoutError(RuntimeError):
    """A layout or migration condition an entry path must refuse on."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _home():
    return Path(os.environ.get("HOME") or Path.home())


def _xdg(var, *default):
    value = os.environ.get(var)
    return Path(value) if value else _home().joinpath(*default)


def xdg_config_default():
    return _xdg("XDG_CONFIG_HOME", ".config") / NAME


def xdg_data_default():
    # Not .../drift-assist: that is where the README and the download
    # installer put the checkout, and a data root that IS the checkout makes a
    # migration publish each item onto its own source and then delete it
    # (finding 3871). See overlap_problems().
    return _xdg("XDG_DATA_HOME", ".local", "share") / DATA_NAME


def state_dir():
    override = os.environ.get("ASSIST_STATE_DIR")
    return Path(override) if override else _xdg("XDG_STATE_HOME", ".local", "state") / NAME


def install_json_path():
    override = os.environ.get("ASSIST_CONFIG_DIR")
    return (Path(override) if override else xdg_config_default()) / INSTALL_JSON


def _same_checkout(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def read_install_json(path=None):
    """The install record, or None when absent. The ONE contract: the runtime
    layout and the migration tool (passing its journal's path) both use it.
    Anything but a schema-1 object naming a checkout is install_json_invalid."""
    path = Path(path) if path is not None else install_json_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LayoutError("install_json_invalid", f"{path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise LayoutError("install_json_invalid", f"{path} does not parse") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("checkout"), str):
        raise LayoutError("install_json_invalid", f"{path} is not a schema-1 install record")
    return data


def layout(checkout=APP_ROOT):
    """"legacy" or "xdg" for `checkout`; raises when another checkout owns it."""
    record = read_install_json()
    if record is None:
        return "legacy"
    if not _same_checkout(record["checkout"], checkout):
        raise LayoutError(
            "layout_owned_by_other_checkout",
            f"{install_json_path()} belongs to {record['checkout']}, not {checkout}. "
            "Give this checkout its own roots with ASSIST_CONFIG_DIR and ASSIST_DATA_DIR.",
        )
    return "xdg"


def config_dir(checkout=APP_ROOT):
    override = os.environ.get("ASSIST_CONFIG_DIR")
    if override:
        layout(checkout)  # an install.json there must still be ours
        return Path(override)
    return xdg_config_default() if layout(checkout) == "xdg" else Path(checkout)


def data_dir(checkout=APP_ROOT):
    override = os.environ.get("ASSIST_DATA_DIR")
    if override:
        layout(checkout)
        return Path(override)
    return xdg_data_default() if layout(checkout) == "xdg" else Path(checkout)


def env_file(checkout=APP_ROOT):
    """The one env file the server, CLI, installer and assist-ctl all read."""
    name = XDG_ENV_NAME if layout(checkout) == "xdg" else ".env"
    return config_dir(checkout) / name


def token_path(checkout=APP_ROOT):
    explicit = os.environ.get("ASSIST_AUTH_TOKEN_PATH")
    return Path(explicit) if explicit else config_dir(checkout) / "auth_token"


# Every piece of runtime state, as (name in the checkout, root, name in that
# root on the XDG layout). The legacy layout keeps all of them in the checkout
# under their first name. `assist doctor` and the migration tool read this.
# container_config.json, extensions.json and automate_state.json belong to the
# retired Automate and Container features: nothing reads them, but an old
# install's copies are the user's files, so a migration still carries them
# (and a journal written before the retirement still validates).
STATE_ITEMS = (
    (".env", "config", XDG_ENV_NAME),
    ("auth_token", "config", "auth_token"),
    ("settings.json", "config", "settings.json"),
    ("project_settings.json", "config", "project_settings.json"),
    ("history.json", "data", "history.json"),
    ("favorites.json", "data", "favorites.json"),
    ("drafts.json", "data", "drafts.json"),
    ("tab_state.json", "data", "tab_state.json"),
    ("idle_state.json", "data", "idle_state.json"),
    ("devices.json", "data", "devices.json"),
    ("container_config.json", "data", "container_config.json"),
    ("extensions.json", "data", "extensions.json"),
    ("automate_state.json", "data", "automate_state.json"),
    ("claude_mode.txt", "data", "claude_mode.txt"),
    ("kill-audit.log", "data", "kill-audit.log"),
    (".assist-launch-provenance-v1", "data", ".assist-launch-provenance-v1"),
    (".assist-launch-provenance-v1-initialization.json", "data",
     ".assist-launch-provenance-v1-initialization.json"),
)


def xdg_roots():
    """The config and data roots the XDG layout uses, overrides applied."""
    config = os.environ.get("ASSIST_CONFIG_DIR")
    data = os.environ.get("ASSIST_DATA_DIR")
    return (Path(config) if config else xdg_config_default(),
            Path(data) if data else xdg_data_default())


def _resolved(path):
    return Path(os.path.realpath(path))


def overlap_problems(checkout=APP_ROOT, roots=None):
    """The code/data root-overlap policy: [] when the XDG roots are separate.

    A config or data root that equals the checkout, lies inside it, or contains
    it (symlinks resolved) is refused wherever a layout is selected or state is
    moved: the checkout is code, and `rm -rf` of it or a migration between the
    two must never reach live state (finding 3871).
    """
    code = _resolved(checkout)
    problems = []
    for label, root in zip(("config", "data"), roots or xdg_roots()):
        real = _resolved(root)
        if real == code or code in real.parents or real in code.parents:
            problems.append(
                f"the {label} dir {root} overlaps the checkout {checkout}"
                + (f" (resolves to {real})" if str(real) != str(root) else "")
            )
    return problems


def item_locations(checkout=APP_ROOT):
    """[(label, legacy path, xdg path)] for every STATE_ITEMS entry."""
    roots = {"config": xdg_config_default(), "data": xdg_data_default()}
    return [
        (legacy, Path(checkout) / legacy, roots[root] / name)
        for legacy, root, name in STATE_ITEMS
    ]


def ensure_dirs(checkout=APP_ROOT):
    """Create the config and data dirs (0700) when they are not the checkout."""
    for directory in (config_dir(checkout), data_dir(checkout)):
        if _same_checkout(directory, checkout):
            continue
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)


def _write_durable(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, indent=2) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_install_json(checkout=APP_ROOT, path=None):
    """Select the XDG layout for `checkout` (the installer and the migration
    tool, which passes the path its journal recorded)."""
    path = Path(path) if path is not None else install_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _write_durable(path, {"schema": 1, "checkout": str(Path(checkout).resolve()), "created": int(time.time())})
    return path


# ---------------------------------------------------------------------------
# Migration marker and lock
# ---------------------------------------------------------------------------


def migration_marker_path():
    return state_dir() / MARKER


def migration_state():
    """None when there is no marker; the marker dict; or {"valid": False, …}.

    Read-only and lock-free, so diagnostics can report it while a migrator
    holds the lock.
    """
    path = migration_marker_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"valid": False, "error": str(exc), "path": str(path)}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"valid": False, "error": "does not parse", "path": str(path)}
    if (
        not isinstance(data, dict)
        or data.get("schema") != MARKER_SCHEMA
        or data.get("direction") not in ("forward", "reverse")
        or data.get("phase") not in MIGRATION_PHASES
    ):
        # An unknown schema (a newer tool, a hand edit) is not something this
        # code may interpret: recovery by hand, not a guess (finding 3873).
        return {"valid": False, "error": "unsupported or malformed migration journal", "path": str(path)}
    data.setdefault("valid", True)
    return data


def migration_blocks(marker):
    """True when an entry path must refuse: incomplete or unreadable."""
    if marker is None:
        return False
    return not marker.get("valid", True) or marker.get("phase") != "done"


def acquire_layout_lock(shared=True, create=True):
    """Take the layout lock without blocking, for the life of the process.

    Returns the descriptor (keep it open), or None when `create` is False and
    no lock file exists. Contention raises LayoutError: `migration_in_progress`
    for a shared request (only the migrator holds it exclusively), `busy` for
    an exclusive one.
    """
    global _held_lock
    path = state_dir() / LOCK
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    else:
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return None
    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        if shared:
            raise LayoutError("migration_in_progress", "a state migration holds the layout lock.\n" + RECOVERY_HINT)
        raise LayoutError("busy", "an Assist server or CLI command is using this install; stop it first")
    _held_lock = fd
    return fd


def check_marker():
    marker = migration_state()
    if migration_blocks(marker):
        if not marker.get("valid", True):
            detail = f"the migration marker {migration_marker_path()} cannot be read ({marker['error']})."
        else:
            detail = (f"a state migration stopped in phase {marker['phase']!r} "
                      f"({marker.get('direction', 'forward')}).")
        raise LayoutError("migration_in_progress", detail + "\n" + RECOVERY_HINT)
    return marker


def enter_runtime(checkout=APP_ROOT, handoff=False):
    """The bootstrap every entry path runs before importing application state.

    Checks layout ownership, takes the shared lock (held until exit), refuses
    an incomplete migration, and creates the runtime dirs. The park-activation
    handoff writes nothing: it opens an existing lock file only, and creates
    no directory.
    """
    layout(checkout)
    fd = acquire_layout_lock(shared=True, create=not handoff)
    check_marker()
    if not handoff:
        ensure_dirs(checkout)
    return fd


def describe(checkout=APP_ROOT):
    """Everything `assist config` and `assist doctor` print about the layout."""
    out = {"app_root": str(APP_ROOT), "state_dir": str(state_dir())}
    try:
        out.update({
            "layout": layout(checkout),
            "config_dir": str(config_dir(checkout)),
            "data_dir": str(data_dir(checkout)),
            "env_file": str(env_file(checkout)),
        })
    except LayoutError as exc:
        out.update({"layout": "refused", "error": str(exc)})
    out["migration"] = migration_state()
    return out


def _main(argv):
    import argparse

    parser = argparse.ArgumentParser(prog="shared/paths.py")
    parser.add_argument("command", choices=("env-file", "enter", "describe", "layout", "select-xdg"))
    parser.add_argument("--checkout", default=str(APP_ROOT))
    parser.add_argument("--fd", type=int, help="enter: flock this inherited descriptor (shared)")
    args = parser.parse_args(argv)
    try:
        if args.command == "env-file":
            print(env_file(args.checkout))
        elif args.command == "layout":
            print(layout(args.checkout))
        elif args.command == "select-xdg":
            # The installer, for a fresh install only: a checkout that already
            # holds any runtime state stays where it is until migrate-state.
            overlap = overlap_problems(args.checkout)
            if overlap:
                print("assist: " + "; ".join(overlap) + "; staying on the legacy layout. Set "
                      "ASSIST_CONFIG_DIR / ASSIST_DATA_DIR outside the checkout to separate them.",
                      file=sys.stderr)
                return 4
            present = [legacy for legacy, path, _ in item_locations(args.checkout) if path.exists()]
            if present:
                print(f"assist: legacy state present ({', '.join(present)}); staying on the legacy layout",
                      file=sys.stderr)
                return 4
            write_install_json(args.checkout)
            ensure_dirs(args.checkout)
            print(layout(args.checkout))
        elif args.command == "describe":
            print(json.dumps(describe(args.checkout)))
        else:
            layout(args.checkout)
            if args.fd is not None:
                # The lock belongs to the open file description bash holds, so
                # it outlives this process for as long as bash keeps the fd.
                try:
                    fcntl.flock(args.fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise LayoutError("migration_in_progress",
                                      "a state migration holds the layout lock.\n" + RECOVERY_HINT)
            check_marker()
            ensure_dirs(args.checkout)
    except LayoutError as exc:
        print(f"assist: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
