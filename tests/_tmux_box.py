"""A private tmux server with `base-index 1` for the pane-identity tests.

$TMUX names the private socket, so every bare `tmux` in the code under test
goes there and never to the developer's real server; teardown passes the
socket with -S. The socket lives in a short /tmp directory because a unix
socket path must stay under 108 bytes.
"""

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from unittest import mock

BASE_INDEX_CONF = "set -g base-index 1\nsetw -g pane-base-index 1\n"

# A raw-mode reader that appends every byte the pane receives to a file.
READER = (
    "import os, sys, tty\n"
    "os.write(1, ('PANE-' + sys.argv[2] + '\\r\\n').encode())\n"
    "tty.setraw(0)\n"
    "out = open(sys.argv[1], 'ab', 0)\n"
    "out.write(b'ready')\n"
    "while True:\n"
    "    data = os.read(0, 1024)\n"
    "    if not data:\n"
    "        break\n"
    "    out.write(data)\n"
)


class TmuxBox:
    def __init__(self, case):
        self.root = Path(tempfile.mkdtemp(prefix="p7a-", dir="/tmp"))
        case.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.socket = self.root / "s"
        self.conf = self.root / "tmux.conf"
        self.conf.write_text(BASE_INDEX_CONF, encoding="utf-8")
        env = mock.patch.dict(os.environ, {"TMUX": f"{self.socket},0,0"})
        env.start()
        case.addCleanup(env.stop)
        os.environ.pop("TMUX_PANE", None)
        case.addCleanup(self.kill_server)

    def tmux(self, *args, check=True):
        return subprocess.run(
            ["tmux", "-S", str(self.socket), "-f", str(self.conf), *args],
            capture_output=True, text=True, check=check, timeout=5,
        ).stdout

    def kill_server(self, timeout=5):
        """Kill the server and wait until it has exited and its socket is gone.

        tmux 3.2 (the installer floor) refuses a new-session on the same socket
        while the old server is still shutting down, so a restart must not
        begin before the old one has finished.
        """
        pid = subprocess.run(["tmux", "-S", str(self.socket), "display-message", "-p", "#{pid}"],
                             capture_output=True, text=True, timeout=5, check=False).stdout.strip()
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                       capture_output=True, timeout=5, check=False)
        deadline = time.monotonic() + timeout
        alive = False
        while time.monotonic() < deadline:
            alive = pid.isdigit() and os.path.exists(f"/proc/{pid}")
            if pid.isdigit() and not os.path.exists("/proc"):
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except OSError:
                    alive = False
            if not alive:
                break
            time.sleep(0.02)
        # A socket left by a server that is gone is stale: remove it so the
        # next new-session starts cleanly rather than racing tmux's own check.
        if not alive and self.socket.exists():
            try:
                self.socket.unlink()
            except FileNotFoundError:
                pass

    def reader_command(self, name):
        path = self.root / f"recv-{name}"
        path.touch()
        script = self.root / "reader.py"
        if not script.exists():
            script.write_text(READER, encoding="utf-8")
        return path, f"python3 {script} {path} {name}"

    def new_session(self, name, command="sleep 600"):
        self.tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "10", command)
        return self.tmux("display-message", "-p", "-t", f"={name}:", "#{pane_id}").strip()

    def split(self, session, command="sleep 600"):
        return self.tmux("split-window", "-d", "-P", "-F", "#{pane_id}",
                         "-t", f"={session}:", command).strip()

    def display(self, pane_id, fmt):
        return self.tmux("display-message", "-p", "-t", pane_id, fmt).strip()

    def server_generation_inputs(self):
        pid, socket_path = self.tmux("display-message", "-p", "#{pid}\t#{socket_path}").strip().split("\t")
        return socket_path, int(pid)


def wait_for_bytes(path, expected, timeout=5):
    deadline = time.monotonic() + timeout
    data = b""
    while time.monotonic() < deadline:
        data = path.read_bytes()
        if data == expected:
            path.write_bytes(b"")
            return data
        time.sleep(0.02)
    raise AssertionError(f"{path.name} received {data!r}, wanted {expected!r}")
