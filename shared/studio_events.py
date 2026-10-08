"""Owner-only, bounded Unix event intake. No terminal commands are executed."""
import atexit
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import stat
import struct
import threading
import uuid

from shared.studio_context import ingest, connection_generation
from shared.tmux import _socket_path

MAX_EVENT_BYTES = 8192
EVENTS = queue.Queue(maxsize=256)
INSTALLATION = uuid.uuid4().hex
GAPS = 0


def runtime_directory():
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "drift-assist")
    return Path(base) / "drift-assist-observers"


def _owned_private(path):
    info = path.lstat()
    return info.st_uid == os.getuid() and not info.st_mode & 0o077 and not stat.S_ISLNK(info.st_mode)


def peer_uid(connection):
    if hasattr(socket, "SO_PEERCRED"):
        return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    if hasattr(connection, "getpeereid"):
        return connection.getpeereid()[0]
    if os.uname().sysname == "Darwin":
        import ctypes
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        library = ctypes.CDLL(None)
        if library.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0:
            return uid.value
    return None


def receive(connection):
    global GAPS
    connection.settimeout(.08)
    uid = peer_uid(connection)
    if uid != os.getuid():
        return
    raw = bytearray()
    while len(raw) <= MAX_EVENT_BYTES:
        chunk = connection.recv(min(4096, MAX_EVENT_BYTES + 1 - len(raw)))
        if not chunk:
            break
        raw.extend(chunk)
    if len(raw) > MAX_EVENT_BYTES:
        GAPS += 1
        return
    event = json.loads(raw)
    if not isinstance(event, dict) or event.get("installation") != INSTALLATION:
        return
    try:
        EVENTS.put_nowait((event, uid))
    except queue.Full:
        GAPS += 1


def studio_event_receiver():
    """One accepting thread; queue consumer verifies tmux away from intake."""
    directory = runtime_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not _owned_private(directory):
        return
    name = hashlib.sha256(str(Path(__file__).resolve().parents[1]).encode()).hexdigest()[:12]
    registration = directory / (name + ".json")
    sockpath = directory / (name + ".sock")
    # Do not steal an observer belonging to another running server.
    if registration.exists():
        try:
            previous = json.loads(registration.read_text())
            os.kill(int(previous["pid"]), 0)
            return
        except (OSError, ValueError, KeyError):
            pass
    if sockpath.exists():
        if not _owned_private(sockpath):
            return
        sockpath.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sockpath))
    sockpath.chmod(0o600)
    listener.listen(16)
    temporary = directory / (name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"version": 1, "pid": os.getpid(), "installation": INSTALLATION,
                   "connection_generation": connection_generation(), "socket": str(sockpath), "tmux_socket": _socket_path()}, output)
    temporary.replace(registration)

    def cleanup():
        for path in (registration, sockpath):
            try:
                path.unlink()
            except OSError:
                pass
    atexit.register(cleanup)
    listener.settimeout(1)
    generation = connection_generation()
    while True:
        current = connection_generation()
        if current != generation:
            generation = current
            record = json.loads(registration.read_text())
            record["connection_generation"] = current
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as output:
                json.dump(record, output)
            temporary.replace(registration)
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            try:
                receive(connection)
            except (OSError, ValueError, TypeError):
                pass


def studio_event_consumer():
    while True:
        event, uid = EVENTS.get()
        try:
            ingest(event, uid)
        except (ValueError, TypeError, KeyError, OSError):
            pass
        finally:
            EVENTS.task_done()
