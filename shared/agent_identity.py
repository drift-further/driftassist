"""Resolve the coding-agent identity of a tmux pane."""

import ctypes
import errno
import logging
import os
import platform
import re
import shlex
import subprocess
import threading
import time
from functools import lru_cache
from typing import NamedTuple

from shared import tab_state

log = logging.getLogger(__name__)

AGENT_KINDS = ("claude", "codex", "cursor", "opencode", "gemini")
SHELL_COMMANDS = ("bash", "zsh", "sh", "fish")
_VERSION_CMD_RE = re.compile(r"\d+(?:\.\d+){1,3}")

_WRAPPER_COMMS = ("docker", "podman", "lxc-attach", "kubectl", "ssh")
_PROCESS_CACHE = {}
_PROCESS_CACHE_LOCK = threading.Lock()
_PROCESS_CACHE_TTL = 0.25
_DECLARATION_STARTUP_GRACE = 10.0
_IS_MAC = platform.system() == "Darwin"
_LOGGED_FAILURES = set()

_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_])"
)
_CODEX_FINGERPRINT_RE = re.compile(r"·\s*Context \d+% (?:left|used)")
_CLAUDE_FINGERPRINT_RE = re.compile(
    r"⏵⏵\s+accept edits on(?:\s+\(shift\+tab to cycle\))?|\? for shortcuts"
)
_CLAUDE_DIALOG_FINGERPRINT_RE = re.compile(
    r"Allow once[\s\S]*Always allow[\s\S]*Deny"
    r"|\d+[\.)]\s+Chat about this[\s\S]*Enter to select\s*[·•]\s*Esc to cancel"
    r"|^\s*❯\s+Yes\s*$[\s\S]*^\s+No\s*$[\s\S]*"
    r"Enter to select\s*[·•]\s*Esc to cancel",
    re.MULTILINE,
)

_DIRECT_EXECUTABLE_KINDS = {
    "claude": "claude",
    "codex": "codex",
    "cursor-agent": "cursor",
    "opencode": "opencode",
    "gemini": "gemini",
    "antigravity": "gemini",
    "agy": "gemini",
}
_AGENT_PACKAGE_KINDS = {
    "@anthropic-ai/claude-code": "claude",
    "@openai/codex": "codex",
    "@google/gemini-cli": "gemini",
    "opencode-ai": "opencode",
}
_NODE_ENTRYPOINT_COMPONENTS = {
    ("@anthropic-ai", "claude-code"): "claude",
    ("@openai", "codex"): "codex",
    ("@google", "gemini-cli"): "gemini",
    ("opencode-ai",): "opencode",
}


def _log_failure_once(target, message):
    if target in _LOGGED_FAILURES:
        return
    _LOGGED_FAILURES.add(target)
    log.exception("agent identity: %s for %s", message, target)


class _DarwinProcBsdInfo(ctypes.Structure):
    # Public proc_pidinfo(PROC_PIDTBSDINFO) ABI, shared by Intel and Apple Silicon:
    # https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _darwin_pidinfo():
    # Load lazily so Linux never needs a macOS library. Cache the function, not
    # process observations: every bracket must read the current generation.
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    try:
        function = library.proc_pidinfo
    except AttributeError as exc:
        raise OSError("proc_pidinfo unavailable") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _darwin_stat_fields(pid):
    pid = int(pid)
    if not 0 < pid <= 0x7fffffff:
        raise ValueError("invalid process id")
    info = _DarwinProcBsdInfo()
    size = ctypes.sizeof(info)
    if _darwin_pidinfo()(pid, 3, 0, ctypes.byref(info), size) != size or info.pbi_pid != pid:
        raise OSError("process identity unavailable")
    # sys/proc.h: SIDL, SRUN, SSLEEP, SSTOP, SZOMB. Keep zombie rejection in
    # the existing callers, and never turn a short/failed native read into an id.
    states = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}
    if info.pbi_status not in states or not info.pbi_start_tvsec or info.pbi_start_tvusec >= 1000000:
        raise ValueError("invalid process identity")
    # ps lstart rounds to seconds. Keep the kernel's microseconds so a reused
    # pid within the same second still compares as a different generation.
    start = f"darwin:{info.pbi_start_tvsec}.{info.pbi_start_tvusec:06d}"
    return states[info.pbi_status], int(info.pbi_ppid), start


def _stat_fields(pid):
    """Return process state, parent pid and precise start identity on this OS."""
    if _IS_MAC:
        return _darwin_stat_fields(pid)
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    close = raw.rfind(")")
    if close < 0:
        raise ValueError("malformed /proc stat")
    fields = raw[close + 2 :].split()
    if len(fields) <= 19:
        raise ValueError("malformed /proc stat")
    return fields[0], int(fields[1]), fields[19]


class _RootUnknown:
    """A pane root that could not be read. It equals nothing, itself included,
    so an unknown root can never prove two pane generations the same."""

    def __eq__(self, other):
        return False

    def __hash__(self):
        return id(self)

    def __repr__(self):
        return "ROOT_UNKNOWN"


ROOT_UNKNOWN = _RootUnknown()


def pane_root_start(pane_pid):
    """The start identity of a pane's root process, read from /proc now: with
    the pane id it tells a pane generation apart when a restarted tmux server
    reuses `%N` (spec 7185 §4.1). One file read, no subprocess.

    None only where there is no /proc (macOS): the pane id alone is then the
    identity, as spec §5.1 has it. ROOT_UNKNOWN when /proc exists but the root
    cannot be read: callers publish nothing under it."""
    if _IS_MAC:
        return None
    try:
        return _stat_fields(int(pane_pid))[2]
    except (OSError, ValueError, IndexError, TypeError):
        return ROOT_UNKNOWN


def _proc_start_time(pid):
    try:
        if os.path.isdir("/proc"):
            _state, _ppid, start_time = _stat_fields(int(pid))
            return start_time
        proc = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, TypeError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def _kind_from_executable(executable):
    executable = os.path.basename(executable or "").lower()
    if _VERSION_CMD_RE.fullmatch(executable or ""):
        return "claude"
    return _DIRECT_EXECUTABLE_KINDS.get(executable)


def _kind_from_package_operand(token):
    token = (token or "").lower()
    for package, kind in _AGENT_PACKAGE_KINDS.items():
        version = token[len(package) + 1 :] if token.startswith(package + "@") else ""
        if token == package or version and "/" not in version:
            return kind
    return None


def _kind_from_node_entrypoint(token):
    components = tuple(
        component
        for component in (token or "").lower().replace("\\", "/").split("/")
        if component
    )
    # Match the script operand itself, never a directory or a later argument.
    if components and components[-1] in ("agy", "agy.js"):
        return "gemini"
    for marker, kind in _NODE_ENTRYPOINT_COMPONENTS.items():
        width = len(marker)
        if any(
            components[index : index + width] == marker
            for index in range(len(components) - width + 1)
        ):
            return kind
    return None


def _launcher_package_operand(argv, start):
    """Return a launcher's package operand, never an option value."""
    flag_only = {"-y", "--yes", "--quiet", "--no-install", "--ignore-existing"}
    for index in range(start, len(argv)):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token in flag_only or "=" in token and token.startswith("-"):
            continue
        if token.startswith("-"):
            # Unknown options may consume the next token. Failing closed avoids
            # interpreting that option value as a package to execute.
            return None
        return token
    return None


def _kind_from_process(comm, argv):
    executable = os.path.basename(argv[0]) if argv else comm
    direct = _kind_from_executable(executable)
    if direct:
        return direct
    if not argv:
        return _kind_from_executable(comm)

    launcher = os.path.basename(argv[0]).lower()
    if launcher in ("node", "nodejs"):
        # Standard Node agent shims put the executable JS file at argv[1]. If
        # Node options are present, abstain rather than inspect their values.
        if len(argv) > 1 and argv[1] == "--" and len(argv) > 2:
            return _kind_from_node_entrypoint(argv[2])
        if len(argv) > 1 and not argv[1].startswith("-"):
            return _kind_from_node_entrypoint(argv[1])
        return None
    if launcher == "npx":
        return _kind_from_package_operand(_launcher_package_operand(argv, 1))
    if launcher == "npm" and len(argv) > 1 and argv[1] in ("exec", "x"):
        return _kind_from_package_operand(_launcher_package_operand(argv, 2))
    if (
        launcher in ("pnpm", "yarn")
        and len(argv) > 1
        and argv[1] in ("dlx", "exec")
    ):
        return _kind_from_package_operand(_launcher_package_operand(argv, 2))
    return None


def _errno_name(exc):
    return errno.errorcode.get(getattr(exc, "errno", None) or 0, type(exc).__name__)


def _build_process_snapshot(now, errors=None):
    """Enumerate /proc once; pane roots share the resulting parent map.

    With an `errors` list (observe_process_tree only), a `stat` that could not
    be read is appended as (pid, path, errno name) instead of skipped silently;
    a PID that exited between listdir and the read (ENOENT, ESRCH) is not an
    error. Without it, behaviour is exactly the legacy one."""
    children = {}
    start_times = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            pid = int(entry)
            _state, ppid, start_time = _stat_fields(pid)
            children.setdefault(ppid, []).append(pid)
            start_times[pid] = start_time
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            if errors is not None:
                name = _errno_name(exc) if isinstance(exc, OSError) else "malformed"
                errors.append((int(entry), f"/proc/{entry}/stat", name))
            continue
    return {
        "created_at": now,
        "children": children,
        "start_times": start_times,
        "process_info": {},
    }


def _get_process_snapshot(force=False):
    now = time.time()
    with _PROCESS_CACHE_LOCK:
        snapshot = _PROCESS_CACHE.get("snapshot")
        if (
            not force
            and snapshot
            and now - snapshot["created_at"] < _PROCESS_CACHE_TTL
        ):
            return snapshot
        snapshot = _build_process_snapshot(now)
        _PROCESS_CACHE["snapshot"] = snapshot
        return snapshot


def child_pids(pid):
    """Direct children of `pid` from the shared /proc snapshot.

    None where there is no /proc to read (macOS), so the caller can fall back
    to asking pgrep. One snapshot walk serves every pane; a `pgrep -P` per
    shell pane scanned the whole process table each time.
    """
    if _IS_MAC or not os.path.isdir("/proc"):
        return None
    try:
        return list(_get_process_snapshot()["children"].get(int(pid), ()))
    except (OSError, ValueError):
        return None


def _snapshot_process_info(snapshot, pid, errors=None):
    """(comm, argv) for pid; ("", []) when either read fails. With an `errors`
    list, the failed read is appended as (pid, path, errno name) — any failure,
    ENOENT included, since the caller asks only about PIDs in a pane's tree."""
    cached = snapshot["process_info"].get(pid)
    if cached is not None:
        return cached
    path = f"/proc/{pid}/comm"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            comm = f.read().strip()
        path = f"/proc/{pid}/cmdline"
        with open(path, "rb") as f:
            argv = [
                part.decode("utf-8", errors="replace")
                for part in f.read().split(b"\0")
                if part
            ]
    except OSError as exc:
        if errors is not None:
            errors.append((pid, path, _errno_name(exc)))
        comm = ""
        argv = []
    snapshot["process_info"][pid] = (comm, argv)
    return comm, argv


def _scan_snapshot_tree(snapshot, pane_pid):
    children = snapshot["children"]
    start_times = snapshot["start_times"]

    agent_kind = None
    agent_pid = None
    agent_start_time = None
    has_wrapper = False
    queue = [pane_pid]
    seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        comm, argv = _snapshot_process_info(snapshot, pid)

        executable = os.path.basename(argv[0]) if argv else comm
        if comm in _WRAPPER_COMMS or executable in _WRAPPER_COMMS:
            has_wrapper = True
        if agent_kind is None:
            agent_kind = _kind_from_process(comm, argv)
            if agent_kind is not None:
                agent_pid = pid
                agent_start_time = start_times.get(pid) or _proc_start_time(pid)
        queue.extend(children.get(pid, []))

    return {
        "agent_kind": agent_kind,
        "agent_pid": agent_pid,
        "agent_start_time": agent_start_time,
        "has_wrapper": has_wrapper,
        "root_start_time": start_times.get(pane_pid) or _proc_start_time(pane_pid),
    }


class ProcessObservation(NamedTuple):
    """One process-tree read that says whether it can be trusted (spec 7185 §4.3)."""

    complete: bool      # every read that could change the answer succeeded
    errors: list        # [(pid | None, path, errno name)]
    agent: dict | None  # {kind, pid, start}: the first agent in the tree
    agents: list        # [{kind, pid, start, argv, cwd}]: every agent-kind process
    wrapper: bool       # ssh, docker, ... somewhere in the tree
    root_start: str | None


def _recheck_start(pid, errors):
    """(start, ok) for pid now. An exited pid (ENOENT, ESRCH) is (None, True):
    a true reading. Any other failure is (None, False) and is appended to
    `errors`: a denied read is no evidence that the process is gone."""
    try:
        return _stat_fields(pid)[2], True
    except (FileNotFoundError, ProcessLookupError):
        return None, True
    except (OSError, ValueError, IndexError) as exc:
        name = _errno_name(exc) if isinstance(exc, OSError) else "malformed"
        errors.append((pid, f"/proc/{pid}/stat", name))
        return None, False


def observe_process_tree(pane_pid):
    """Walk a pane's process tree from a fresh snapshot, reporting completeness.

    Unlike the legacy helpers, a read that fails is not mistaken for "no
    agent": `complete` is False when a stat outside ENOENT/ESRCH failed (it
    hides that PID's parent), when a comm/cmdline read in the pane's subtree
    failed, when the root is gone, or when its start time changed during the
    walk. An agent whose start time no longer matches counts as gone. Without
    /proc (macOS) nothing is complete. A cwd that cannot be read is recorded in
    `errors` but leaves `complete` alone: it does not change which agent runs.
    """
    if _IS_MAC or not os.path.isdir("/proc"):
        return ProcessObservation(False, [(None, "/proc", "unsupported")], None, [], False, None)
    errors = []
    try:
        pane_pid = int(pane_pid)
        snapshot = _build_process_snapshot(time.time(), errors=errors)
    except (TypeError, ValueError):
        return ProcessObservation(False, [(None, "pane_pid", "invalid")], None, [], False, None)
    except OSError as exc:
        return ProcessObservation(False, [(None, "/proc", _errno_name(exc))], None, [], False, None)
    children = snapshot["children"]
    start_times = snapshot["start_times"]
    root_start = start_times.get(pane_pid)
    complete = not errors
    if root_start is None:
        complete = False
        if not any(pid == pane_pid for pid, _path, _name in errors):
            errors.append((pane_pid, f"/proc/{pane_pid}/stat", "ENOENT"))

    agents = []
    has_wrapper = False
    queue = [pane_pid] if root_start is not None else []
    seen = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        before = len(errors)
        comm, argv = _snapshot_process_info(snapshot, pid, errors=errors)
        if len(errors) > before:
            complete = False
        executable = os.path.basename(argv[0]) if argv else comm
        if comm in _WRAPPER_COMMS or executable in _WRAPPER_COMMS:
            has_wrapper = True
        kind = _kind_from_process(comm, argv)
        if kind is not None:
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except OSError as exc:
                errors.append((pid, f"/proc/{pid}/cwd", _errno_name(exc)))
                cwd = None
            agents.append({"kind": kind, "pid": pid, "start": start_times.get(pid),
                           "argv": argv, "cwd": cwd})
        queue.extend(children.get(pid, []))

    if root_start is not None:
        start, ok = _recheck_start(pane_pid, errors)
        if not ok:
            complete = False
        elif start != root_start:
            complete = False
            errors.append((pane_pid, f"/proc/{pane_pid}/stat", "changed"))
    # PID reuse (a different start) and exit read as gone, never as the old
    # agent; a recheck that could not be read leaves the answer unknown.
    kept = []
    for a in agents:
        start, ok = _recheck_start(a["pid"], errors) if a["start"] else (None, True)
        if not ok:
            complete = False
        elif start is not None and start == a["start"]:
            kept.append(a)
    agents = kept
    agent = {k: agents[0][k] for k in ("kind", "pid", "start")} if agents else None
    return ProcessObservation(complete, errors, agent, agents, has_wrapper, root_start)


def _scan_process_tree(target, pane_pid, _force_snapshot=False):
    """Resolve one root from the shared short-lived /proc snapshot."""
    if pane_pid in (None, "") or _IS_MAC or not os.path.isdir("/proc"):
        return None
    pane_pid = int(pane_pid)
    snapshot = _get_process_snapshot(force=_force_snapshot)
    result = _scan_snapshot_tree(snapshot, pane_pid)

    root_alive = _proc_start_time(pane_pid) == result.get("root_start_time")
    agent_pid = result.get("agent_pid")
    agent_alive = agent_pid is None or (
        _proc_start_time(agent_pid) == result.get("agent_start_time")
    )
    if not _force_snapshot and (not root_alive or not agent_alive):
        # A child exit/switch or root PID reuse invalidates the snapshot now,
        # without waiting for its short TTL.
        return _scan_process_tree(target, pane_pid, _force_snapshot=True)
    return result


def _kind_from_command_name(command):
    command = os.path.basename((command or "").strip())
    return _kind_from_process(command, [command] if command else [])


def kind_from_agent_command(command):
    """Return the kind only when command is itself an agent launch."""
    try:
        tokens = shlex.split(command or "")
    except ValueError:
        return None
    if not tokens:
        return None

    while tokens and "=" in tokens[0] and not tokens[0].startswith(("/", "./")):
        name, _sep, _value = tokens[0].partition("=")
        if not name.replace("_", "").isalnum():
            break
        tokens.pop(0)
    while tokens and tokens[0] in ("command", "exec"):
        tokens.pop(0)
    if not tokens:
        return None

    return _kind_from_process(os.path.basename(tokens[0]), tokens)


def declare_agent_command(target, command):
    """Persist identity when Assist sends a recognizable agent launch."""
    kind = kind_from_agent_command(command)
    if not kind or not target:
        return None
    # Imported here: shared.tmux imports this module at load.
    from shared.tmux import tmux_exact_target

    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-t", tmux_exact_target(target), "-p", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        pane_pid = int(proc.stdout.strip()) if proc.returncode == 0 else 0
        start_time = _proc_start_time(pane_pid)
        if pane_pid <= 0 or not start_time:
            return None
        tab_state.set_agent_declaration(
            target,
            {
                "pane_pid": pane_pid,
                "proc_start_time": start_time,
                "kind": kind,
                "declared_at": time.time(),
            },
        )
        # A freshly typed launch must not be judged against the previous
        # process generation cached for this pane.
        with _PROCESS_CACHE_LOCK:
            _PROCESS_CACHE.clear()
        return kind
    except Exception:
        _log_failure_once(target, "could not persist declaration")
        return None


def resolve_process(target, pane_pid, command):
    """Resolve identity without pane content; return None when still opaque."""
    try:
        pane_pid = int(pane_pid) if pane_pid not in (None, "") else None
        scan = _scan_process_tree(target, pane_pid)
        inferred = scan.get("agent_kind") if scan else None
        if inferred is None:
            inferred = _kind_from_command_name(command)

        declaration = tab_state.get_agent_declaration(target)
        if declaration:
            current_start = (
                scan.get("root_start_time") if scan else _proc_start_time(pane_pid)
            )
            valid = (
                pane_pid is not None
                and declaration.get("pane_pid") == pane_pid
                and declaration.get("proc_start_time") == current_start
                and declaration.get("kind") in AGENT_KINDS
                and isinstance(declaration.get("declared_at"), (int, float))
            )
            contradictory = (
                inferred in AGENT_KINDS and inferred != declaration.get("kind")
            )
            bound_pid = declaration.get("agent_pid")
            bound_start = declaration.get("agent_start_time")
            bound_alive = (
                bound_pid is not None
                and bound_start
                and _proc_start_time(bound_pid) == bound_start
            )
            if bound_pid is not None and not bound_alive:
                # A cached tree may still contain the dead child. Do not let its
                # stale identity survive after invalidating the bound launch.
                if scan and scan.get("agent_pid") == bound_pid:
                    inferred = None
                tab_state.delete_agent_declaration(target)
            elif not valid or contradictory:
                tab_state.delete_agent_declaration(target)
            elif (
                inferred == declaration.get("kind")
                and scan
                and scan.get("agent_pid")
            ):
                inferred_pid = scan["agent_pid"]
                inferred_start = scan.get("agent_start_time")
                if bound_pid is not None:
                    if bound_pid == inferred_pid and bound_start == inferred_start:
                        return declaration["kind"]
                    # The declared process generation ended. A newly observed
                    # same-kind process stands on inference, not the old record.
                    tab_state.delete_agent_declaration(target)
                elif (
                    inferred_start
                    and _proc_start_time(inferred_pid) == inferred_start
                ):
                    tab_state.set_agent_declaration(
                        target,
                        {
                            **declaration,
                            "agent_pid": inferred_pid,
                            "agent_start_time": inferred_start,
                        },
                    )
                    return declaration["kind"]
                else:
                    tab_state.delete_agent_declaration(target)
            elif inferred is None and bound_pid is not None:
                tab_state.delete_agent_declaration(target)
            elif (
                inferred is None
                and 0
                <= time.time() - declaration["declared_at"]
                <= _DECLARATION_STARTUP_GRACE
            ):
                return declaration["kind"]
            else:
                tab_state.delete_agent_declaration(target)

        if inferred in AGENT_KINDS:
            return inferred
        # A transport can hide the real child process (Docker exec, SSH, etc.).
        # Abstain so callers with a capture can use the fingerprint tier.
        if scan is not None and scan.get("has_wrapper"):
            return None
        if scan is not None and (command or "") in SHELL_COMMANDS:
            return "shell"
        return None
    except Exception:
        _log_failure_once(target, "resolution failed")
        return None


def refine_with_content(kind_or_none, tail):
    """Use idle chrome only when process resolution abstained."""
    if kind_or_none in AGENT_KINDS or kind_or_none == "shell":
        return kind_or_none
    normalized = _ANSI_ESCAPE_RE.sub("", tail or "")
    nonempty = [line for line in normalized.splitlines() if line.strip()]
    fingerprint = "\n".join(nonempty[-5:])
    if _CODEX_FINGERPRINT_RE.search(fingerprint):
        return "codex"
    if _CLAUDE_FINGERPRINT_RE.search(fingerprint) or _CLAUDE_DIALOG_FINGERPRINT_RE.search(
        fingerprint
    ):
        return "claude"
    return "shell"


def resolve(target, pane_pid, command, tail=None):
    """Resolve a pane, optionally applying the content fingerprint tier."""
    process_kind = resolve_process(target, pane_pid, command)
    if tail is None:
        return process_kind or "unknown"
    return refine_with_content(process_kind, tail)


def _has_wrapper_descendant(target, pane_pid):
    """Return wrapper status from the same cached walk identity uses."""
    try:
        scan = _scan_process_tree(target, pane_pid)
        return bool(scan and scan.get("has_wrapper"))
    except Exception:
        _log_failure_once(target, "wrapper scan failed")
        return False
