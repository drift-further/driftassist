"""shared/agent_identity.py — process observation that reports its own completeness.

The legacy identity helpers swallow read errors: `_build_process_snapshot`
skips a PID whose `stat` cannot be read and `_snapshot_process_info` turns a
`comm`/`cmdline` failure into ("", []). An unreadable live agent therefore
looks exactly like no agent (the round-1 judge executed it with a denied
`comm`). `observe_process_tree` (spec 7185 §4.3) reports `complete` and the
errors it saw, so the delegate classifier can keep its state instead of
publishing DEAD on a read it could not make.

Every case runs on an in-memory fake `/proc` (no real process is read), in
both directions, plus a guard that the legacy callers return what they return
today on the same trees.

Run: .venv/bin/python3 -m unittest tests.test_process_observation
"""

import errno
import io
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from shared import agent_identity

ROOT = Path(__file__).resolve().parent.parent


def _stat_line(pid, comm, ppid, start):
    # fields after ") ": state, ppid, 17 fillers, then starttime at index 19.
    return f"{pid} ({comm}) S {ppid} " + " ".join(["0"] * 17) + f" {start} 0\n"


class FakeProc:
    """A fake /proc: pid -> {comm, argv, ppid, start, cwd}; a value may be an exception.

    `stat`, `comm`, `cmdline` and `cwd` may each be an exception instance (raised
    on read) or, for `stat`, a callable returning the line (so a test can change
    a start time between two reads).
    """

    def __init__(self, procs, present=True):
        self.procs = procs
        self.present = present
        self.reads = []

    def _entry(self, pid):
        if pid not in self.procs:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", f"/proc/{pid}")
        return self.procs[pid]

    def _value(self, pid, field):
        entry = self._entry(pid)
        if field == "stat" and "stat" not in entry:
            return _stat_line(pid, entry.get("comm", "x"), entry["ppid"], entry["start"])
        if field == "cmdline" and "cmdline" not in entry:
            return b"\0".join(part.encode() for part in entry.get("argv", [])) + b"\0"
        value = entry[field]
        if isinstance(value, BaseException):
            raise value
        return value() if callable(value) else value

    def open(self, path, mode="r", *args, **kwargs):
        match = re.fullmatch(r"/proc/(\d+)/(stat|comm|cmdline)", str(path))
        if not match or not self.present:
            return _REAL_OPEN(path, mode, *args, **kwargs)
        pid, field = int(match.group(1)), match.group(2)
        self.reads.append((pid, field))
        value = self._value(pid, field)
        if "b" in mode:
            return io.BytesIO(value if isinstance(value, bytes) else value.encode())
        if isinstance(value, bytes):
            value = value.decode()
        return io.StringIO(value if field != "comm" else value + "\n")

    def listdir(self, path):
        if str(path) != "/proc":
            return _REAL_LISTDIR(path)
        if not self.present:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", "/proc")
        return [str(pid) for pid in self.procs] + ["self", "sys", "meminfo"]

    def isdir(self, path):
        if str(path) != "/proc":
            return _REAL_ISDIR(path)
        return self.present

    def readlink(self, path, *args, **kwargs):
        match = re.fullmatch(r"/proc/(\d+)/cwd", str(path))
        if not match:
            return _REAL_READLINK(path, *args, **kwargs)
        pid = int(match.group(1))
        value = self._entry(pid).get("cwd", "/")
        if isinstance(value, BaseException):
            raise value
        return value

    def install(self, case):
        patches = (
            mock.patch.object(agent_identity, "open", self.open, create=True),
            mock.patch.object(agent_identity.os, "listdir", self.listdir),
            mock.patch.object(agent_identity.os.path, "isdir", self.isdir),
            mock.patch.object(agent_identity.os, "readlink", self.readlink),
            mock.patch.object(agent_identity, "_IS_MAC", False),
            mock.patch.object(agent_identity.tab_state, "get_agent_declaration", return_value=None),
        )
        for patcher in patches:
            patcher.start()
            case.addCleanup(patcher.stop)
        agent_identity._PROCESS_CACHE.clear()
        case.addCleanup(agent_identity._PROCESS_CACHE.clear)
        return self


_REAL_OPEN = open
_REAL_LISTDIR = os.listdir
_REAL_ISDIR = os.path.isdir
_REAL_READLINK = os.readlink

DENIED = PermissionError(errno.EACCES, "Permission denied")
GONE = FileNotFoundError(errno.ENOENT, "No such file or directory")


def _init():
    return {1: {"comm": "systemd", "argv": ["/sbin/init"], "ppid": 0, "start": "1"}}


def judge_denied_comm():
    """The round-1 judge's tree: readable bash 10, live child 20 whose comm is denied."""
    procs = _init()
    procs[10] = {"comm": "bash", "argv": ["-bash"], "ppid": 1, "start": "5000", "cwd": "/w"}
    procs[20] = {"comm": DENIED, "argv": ["claude", "--resume"], "ppid": 10, "start": "5100",
                 "cwd": "/w/proj"}
    return procs


def readable_claude():
    procs = judge_denied_comm()
    procs[20]["comm"] = "claude"
    return procs


def agent_free_shell():
    procs = _init()
    procs[10] = {"comm": "bash", "argv": ["-bash"], "ppid": 1, "start": "5000"}
    procs[30] = {"comm": "sleep", "argv": ["sleep", "60"], "ppid": 10, "start": "5200"}
    return procs


def wrapped_shell():
    procs = agent_free_shell()
    procs[40] = {"comm": "ssh", "argv": ["ssh", "host"], "ppid": 10, "start": "5300"}
    return procs


def unrelated_stat(exc):
    procs = agent_free_shell()
    procs[99] = {"comm": "other", "argv": ["other"], "ppid": 1, "start": "6000", "stat": exc}
    return procs


def codex_under_node():
    procs = _init()
    procs[10] = {"comm": "bash", "argv": ["-bash"], "ppid": 1, "start": "5000", "cwd": "/w"}
    procs[20] = {"comm": "node", "argv": ["node", "/usr/lib/node_modules/@openai/codex/bin/codex.js"],
                 "ppid": 10, "start": "5100", "cwd": "/w/a"}
    procs[21] = {"comm": "codex", "argv": ["/opt/codex/codex", "--yolo"], "ppid": 20,
                 "start": "5101", "cwd": "/w/a"}
    return procs


TREES = {
    "judge_denied_comm": judge_denied_comm,
    "readable_claude": readable_claude,
    "agent_free_shell": agent_free_shell,
    "wrapped_shell": wrapped_shell,
    "unrelated_stat_denied": lambda: unrelated_stat(DENIED),
    "unrelated_stat_gone": lambda: unrelated_stat(GONE),
    "codex_under_node": codex_under_node,
}


class CompletenessTests(unittest.TestCase):
    def test_agy_identity_paths_and_non_agent_twins(self):
        from shared.delegate_state import identity_of

        cases = [
            ("agy", ["agy", "--model", "gemini-4-argon-high"], True),
            ("agy", [], True),
            ("sleep", ["/opt/bin/agy", "600"], True),
            ("node", ["node", "/opt/bin/agy"], True),
            ("node", ["node", "--", "/opt/agy.js"], True),
            ("agyx", ["agyx"], False),
            ("bash", ["bash", "agy"], False),
            ("node", ["node", "/opt/agyx.js"], False),
            ("node", ["node", "/opt/agy/other.js", "agy"], False),
            ("node", ["node", "--eval", "agy"], False),
        ]
        for comm, argv, expected in cases:
            with self.subTest(comm=comm, argv=argv):
                procs = agent_free_shell()
                procs[30].update(comm=comm, argv=argv)
                obs = self.observe(procs)
                self.assertTrue(obs.complete)
                identity = identity_of(obs)
                self.assertEqual(identity["status"], "present" if expected else "absent")
                self.assertEqual(identity["kind"], "gemini" if expected else None)

    def observe(self, procs, pane_pid=10, present=True):
        FakeProc(procs, present=present).install(self)
        return agent_identity.observe_process_tree(pane_pid)

    def test_denied_comm_child_is_incomplete_and_names_the_read(self):
        obs = self.observe(judge_denied_comm())
        self.assertFalse(obs.complete)
        self.assertIn((20, "/proc/20/comm", "EACCES"), obs.errors)
        self.assertIsNone(obs.agent)

    def test_readable_twin_is_complete_with_the_agent(self):
        obs = self.observe(readable_claude())
        self.assertTrue(obs.complete)
        self.assertEqual(obs.errors, [])
        self.assertEqual(obs.agent, {"kind": "claude", "pid": 20, "start": "5100"})
        self.assertEqual(obs.agents, [{"kind": "claude", "pid": 20, "start": "5100",
                                       "argv": ["claude", "--resume"], "cwd": "/w/proj"}])
        self.assertFalse(obs.wrapper)
        self.assertEqual(obs.root_start, "5000")

    def test_readable_agent_free_shell_is_complete_and_absent(self):
        obs = self.observe(agent_free_shell())
        self.assertTrue(obs.complete)
        self.assertEqual(obs.errors, [])
        self.assertIsNone(obs.agent)
        self.assertEqual(obs.agents, [])
        self.assertFalse(obs.wrapper)

    def test_wrapper_in_tree_is_complete_without_an_agent(self):
        obs = self.observe(wrapped_shell())
        self.assertTrue(obs.complete)
        self.assertIsNone(obs.agent)
        self.assertTrue(obs.wrapper)

    def test_unrelated_stat_permission_error_is_incomplete(self):
        # An unreadable stat hides that PID's parent, so it could be in this tree.
        obs = self.observe(unrelated_stat(DENIED))
        self.assertFalse(obs.complete)
        self.assertIn((99, "/proc/99/stat", "EACCES"), obs.errors)

    def test_unrelated_stat_file_not_found_stays_complete(self):
        # The PID exited between listdir and the read: it cannot be in the tree.
        obs = self.observe(unrelated_stat(GONE))
        self.assertTrue(obs.complete)
        self.assertEqual(obs.errors, [])

    def test_cmdline_gone_inside_the_subtree_is_incomplete(self):
        # Inside the pane's subtree even FileNotFoundError counts: the child may
        # have been the agent.
        procs = agent_free_shell()
        procs[30]["cmdline"] = GONE
        obs = self.observe(procs)
        self.assertFalse(obs.complete)
        self.assertIn((30, "/proc/30/cmdline", "ENOENT"), obs.errors)

    def test_root_start_changing_during_the_walk_is_incomplete(self):
        procs = agent_free_shell()
        starts = iter(["5000", "7777", "7777", "7777"])
        procs[10]["stat"] = lambda: _stat_line(10, "bash", 1, next(starts))
        obs = self.observe(procs)
        self.assertFalse(obs.complete)

    def test_agent_found_despite_an_unrelated_error_is_present(self):
        procs = readable_claude()
        procs[99] = {"comm": "other", "argv": ["other"], "ppid": 1, "start": "6000", "stat": DENIED}
        obs = self.observe(procs)
        self.assertFalse(obs.complete)
        self.assertEqual(obs.agent, {"kind": "claude", "pid": 20, "start": "5100"})

    def test_every_agent_process_is_listed_with_argv_and_cwd(self):
        obs = self.observe(codex_under_node())
        self.assertTrue(obs.complete)
        self.assertEqual(obs.agent["pid"], 20)
        self.assertEqual([a["pid"] for a in obs.agents], [20, 21])
        self.assertEqual(obs.agents[1]["argv"], ["/opt/codex/codex", "--yolo"])
        self.assertEqual(obs.agents[1]["cwd"], "/w/a")

    def test_a_root_that_is_gone_is_incomplete_never_absent(self):
        # "absent" needs the root alive with a matching start time (§4.3).
        procs = agent_free_shell()
        del procs[10]
        obs = self.observe(procs)
        self.assertFalse(obs.complete)
        self.assertIsNone(obs.agent)

    def test_an_agent_whose_start_changed_during_the_walk_counts_as_gone(self):
        # PID reuse reads as exit, never as the old agent.
        procs = readable_claude()
        starts = iter(["5100", "9999", "9999"])
        procs[20]["stat"] = lambda: _stat_line(20, "claude", 10, next(starts))
        obs = self.observe(procs)
        self.assertIsNone(obs.agent)
        self.assertEqual(obs.agents, [])

    def _agent_recheck(self, second):
        """readable claude 20; its first stat read succeeds, the final recheck gives `second`."""
        procs = readable_claude()
        reads = iter(["5100", second])

        def stat():
            value = next(reads, second)
            if isinstance(value, BaseException):
                raise value
            return _stat_line(20, "claude", 10, value)

        procs[20]["stat"] = stat
        return self.observe(procs)

    def test_a_denied_final_agent_recheck_is_incomplete_never_absent(self):
        obs = self._agent_recheck(PermissionError(errno.EACCES, "Permission denied"))
        self.assertFalse(obs.complete)
        self.assertIn((20, "/proc/20/stat", "EACCES"), obs.errors)
        self.assertIsNone(obs.agent)

    def test_twin_a_gone_agent_on_recheck_is_an_exit(self):
        obs = self._agent_recheck(FileNotFoundError(errno.ENOENT, "No such file or directory"))
        self.assertTrue(obs.complete)
        self.assertEqual(obs.errors, [])
        self.assertIsNone(obs.agent)

    def test_twin_a_different_start_on_recheck_is_a_reused_pid(self):
        obs = self._agent_recheck("9999")
        self.assertTrue(obs.complete)
        self.assertIsNone(obs.agent)

    def test_twin_a_matching_start_on_recheck_is_present(self):
        obs = self._agent_recheck("5100")
        self.assertTrue(obs.complete)
        self.assertEqual(obs.agent["pid"], 20)

    def test_a_denied_final_root_recheck_is_incomplete_with_its_errno(self):
        procs = agent_free_shell()
        reads = iter(["5000"])

        def stat():
            value = next(reads, None)
            if value is None:
                raise PermissionError(errno.EACCES, "Permission denied")
            return _stat_line(10, "bash", 1, value)

        procs[10]["stat"] = stat
        obs = self.observe(procs)
        self.assertFalse(obs.complete)
        self.assertIn((10, "/proc/10/stat", "EACCES"), obs.errors)

    def test_a_cwd_read_failure_is_recorded_but_does_not_change_identity(self):
        # cwd feeds the resume line (§7.1), not whether an agent is present.
        procs = readable_claude()
        procs[20]["cwd"] = DENIED
        obs = self.observe(procs)
        self.assertTrue(obs.complete)
        self.assertEqual(obs.agent["pid"], 20)
        self.assertIsNone(obs.agents[0]["cwd"])
        self.assertIn((20, "/proc/20/cwd", "EACCES"), obs.errors)

    def test_no_proc_is_incomplete_and_unsupported(self):
        obs = self.observe(agent_free_shell(), present=False)
        self.assertFalse(obs.complete)
        self.assertEqual(obs.errors, [(None, "/proc", "unsupported")])
        self.assertIsNone(obs.agent)

    def test_macos_is_incomplete_and_unsupported(self):
        FakeProc(agent_free_shell()).install(self)
        with mock.patch.object(agent_identity, "_IS_MAC", True):
            obs = agent_identity.observe_process_tree(10)
        self.assertFalse(obs.complete)
        self.assertEqual(obs.errors, [(None, "/proc", "unsupported")])

    def test_observation_forces_a_fresh_snapshot(self):
        # A cached legacy snapshot (built without errors) must not answer an
        # observation: its swallowed failures are exactly what this reports.
        fake = FakeProc(judge_denied_comm()).install(self)
        agent_identity._get_process_snapshot()
        before = len(fake.reads)
        obs = agent_identity.observe_process_tree(10)
        self.assertGreater(len(fake.reads), before)
        self.assertFalse(obs.complete)


# What each legacy entry point returns on each tree with the code as it was
# before observe_process_tree existed. Captured on build-767 at e9e549f.
LEGACY = {
    # The swallowed comm read makes the live claude invisible: the bug §4.3
    # works around without changing it for these callers.
    "judge_denied_comm": {
        "scan": {"agent_kind": None, "agent_pid": None, "agent_start_time": None,
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "shell", "children": [20],
    },
    "readable_claude": {
        "scan": {"agent_kind": "claude", "agent_pid": 20, "agent_start_time": "5100",
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "claude", "children": [20],
    },
    "agent_free_shell": {
        "scan": {"agent_kind": None, "agent_pid": None, "agent_start_time": None,
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "shell", "children": [30],
    },
    "wrapped_shell": {
        "scan": {"agent_kind": None, "agent_pid": None, "agent_start_time": None,
                 "ambiguous": False, "has_wrapper": True, "root_start_time": "5000"},
        "resolve": None, "children": [30, 40],
    },
    "unrelated_stat_denied": {
        "scan": {"agent_kind": None, "agent_pid": None, "agent_start_time": None,
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "shell", "children": [30],
    },
    "unrelated_stat_gone": {
        "scan": {"agent_kind": None, "agent_pid": None, "agent_start_time": None,
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "shell", "children": [30],
    },
    "codex_under_node": {
        "scan": {"agent_kind": "codex", "agent_pid": 20, "agent_start_time": "5100",
                 "ambiguous": False, "has_wrapper": False, "root_start_time": "5000"},
        "resolve": "codex", "children": [20],
    },
}


class LegacyUnchangedTests(unittest.TestCase):
    """The existing callers see exactly today's answers on the same fake trees."""

    def test_scan_resolve_and_children_match_the_pinned_results(self):
        for name, build in TREES.items():
            with self.subTest(tree=name):
                FakeProc(build()).install(self)
                expected = LEGACY[name]
                self.assertEqual(agent_identity._scan_process_tree("t", 10), expected["scan"])
                agent_identity._PROCESS_CACHE.clear()
                self.assertEqual(agent_identity.resolve_process("t", 10, "bash"), expected["resolve"])
                agent_identity._PROCESS_CACHE.clear()
                self.assertEqual(agent_identity.child_pids(10), expected["children"])
                agent_identity._PROCESS_CACHE.clear()

    def test_snapshot_helpers_return_the_same_with_and_without_errors(self):
        for name, build in TREES.items():
            with self.subTest(tree=name):
                FakeProc(build()).install(self)
                plain = agent_identity._build_process_snapshot(1.0)
                errors = []
                observed = agent_identity._build_process_snapshot(1.0, errors=errors)
                self.assertEqual(plain, observed)
                for pid in sorted(plain["start_times"]):
                    self.assertEqual(
                        agent_identity._snapshot_process_info(plain, pid),
                        agent_identity._snapshot_process_info(observed, pid, errors=errors),
                    )

    def test_legacy_snapshot_still_swallows_errors(self):
        FakeProc(judge_denied_comm()).install(self)
        snapshot = agent_identity._build_process_snapshot(1.0)
        self.assertEqual(agent_identity._snapshot_process_info(snapshot, 20), ("", []))

    def test_no_existing_caller_passes_errors(self):
        call = re.compile(r"(?<!def )_(?:build_process_snapshot|snapshot_process_info)\([^)]*errors")
        offenders = []
        for base in ("routes", "shared", "cli"):
            for path in (ROOT / base).rglob("*.py"):
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if call.search(line):
                        offenders.append(f"{path.relative_to(ROOT)}:{number}")
        allowed = {p for p in offenders if p.startswith("shared/agent_identity.py:")}
        self.assertEqual(sorted(set(offenders) - allowed), [])
        source = (ROOT / "shared" / "agent_identity.py").read_text(encoding="utf-8")
        body = source.split("def observe_process_tree", 1)
        self.assertEqual(len(body), 2, "observe_process_tree is not defined")
        before = body[0]
        self.assertIsNone(call.search(before), "a legacy helper passes errors")


if __name__ == "__main__":
    unittest.main()
