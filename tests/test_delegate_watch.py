"""routes/delegates.py — the 5 s delegate watcher, its health, and the wire fields.

Spec 7185 §5.2–§5.4, §6.1 wire fields. The watcher runs whether or not a
browser is open and whether or not Auto-Yes is armed; it samples delegate
panes only, by pane id; a failed listing keeps every published state and says
so in `watch.error`; one pane's exception marks only that pane stale. "No
delegates" (`panes: []`, `error: null`) and "cannot tell" stay distinguishable.

Real isolated tmux (tests/test_exact_targets._IsolatedTmux); the clock is
passed to `tick` so a 45 s quiet needs no sleeping.

Run: .venv/bin/python3 -m unittest tests.test_delegate_watch
"""

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import shared.auth as auth
import shared.state as state
from shared import auth_gate, pane_scan
from tests.test_exact_targets import _IsolatedTmux

ROOT = Path(__file__).resolve().parent.parent
# A pane whose root process is a "claude" agent (argv[0]) that prints once and idles.
FAKE_AGENT = "bash -c \"printf 'hello from agent\\n'; exec -a claude sleep 600\""


class _WatchCase(_IsolatedTmux):
    def setUp(self):
        super().setUp()
        from routes import delegates

        self.d = delegates
        sessions = Path(tempfile.mkdtemp(prefix="assist-claude-sessions-"))
        self.addCleanup(shutil.rmtree, sessions, ignore_errors=True)
        self.settings = dict(state.DEFAULT_SETTINGS["delegates"])
        for p in (
            patch.dict(state.delegate_states, clear=True),
            patch.object(state, "delegate_watch", None),
            patch.dict(delegates._MEMORY, clear=True),
            patch.object(delegates, "CLAUDE_SESSIONS_DIR", sessions),
            patch.object(delegates, "_settings", side_effect=lambda: dict(self.settings)),
            patch.object(pane_scan, "_SCAN_CACHE", {}),
            patch.object(pane_scan, "_INFLIGHT", {}),
            patch.object(state, "pane_content_hash", {}),
            patch.object(state, "pane_last_activity", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def delegate(self, name, command=FAKE_AGENT):
        self.tmux("new-session", "-d", "-s", name, "-x", "80", "-y", "20", command)
        pane = self.pane_id(name)
        # Let the agent exec and print.
        for _ in range(50):
            if "hello from agent" in self.tmux("capture-pane", "-p", "-t", pane) or command != FAKE_AGENT:
                break
            time.sleep(0.05)
        time.sleep(0.1)
        return pane

    def panes(self):
        return {p["pane_id"]: p for p in self.d.snapshot()["panes"]}


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class WatcherTests(_WatchCase):
    def test_changing_non_agent_pane_keeps_one_dead_key_across_30_ticks(self):
        pane = self.delegate("del_churn", "sleep 600")
        t0 = time.time() + 130
        records = []
        with patch.object(self.d, "_capture", side_effect=[f"animation {i}" for i in range(30)]), \
                patch.object(self.d, "CAPTURE_MAX_AGE_SEC", 0):
            for i in range(30):
                self.d.tick(now=t0 + i * 5)
                records.append(self.panes()[pane])
        dead = [r for r in records if r["state"] == "dead"]
        self.assertEqual(len(dead), 29)
        self.assertEqual(len({r["episode"] for r in records}), 1)
        self.assertEqual(len({r["key"] for r in dead}), 1)
        self.assertEqual(len({r["since"] for r in dead}), 1)

    def test_agy_is_present_and_each_working_turn_gets_one_finished_key(self):
        pane = self.delegate("del_agy", "bash -c 'exec -a agy sleep 600'")
        t0 = time.time()
        keys = []
        with patch.object(self.d, "CAPTURE_MAX_AGE_SEC", 0):
            for turn in range(3):
                for i in range(6):
                    with patch.object(self.d, "_capture", return_value=f"turn {turn} output {i}"):
                        self.d.tick(now=t0 + turn * 100 + i * 5)
                with patch.object(self.d, "_capture", return_value=f"turn {turn} output 5"):
                    self.d.tick(now=t0 + turn * 100 + 75)
                    done = self.panes()[pane]
                    self.assertEqual(done["state"], "finished")
                    self.assertEqual(done["episode"], turn + 1)
                    keys.append(done["key"])
                    self.d.tick(now=t0 + turn * 100 + 80)
                    self.assertEqual(self.panes()[pane]["key"], keys[-1])
        self.assertEqual(len(set(keys)), 3)

    def test_a_delegate_pane_reaches_finished_with_no_browser_and_autoyes_off(self):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        self.d.tick(now=t0)
        first = self.panes()[pane]
        self.assertEqual(first["state"], "working")
        self.assertEqual(first["health"], "ok")
        self.d.tick(now=t0 + 46)
        done = self.panes()[pane]
        self.assertEqual((done["state"], done["reason"]), ("finished", "quiet"))
        self.assertEqual(done["observed_at"], t0 + 46)
        self.assertEqual(done["session"], "del_alpha")
        self.assertIsNotNone(done["alert"])
        # Only delegate panes are watched.
        self.assertEqual(set(self.panes()), {pane})
        watch = self.d.snapshot()["watch"]
        self.assertIsNone(watch["error"])
        self.assertEqual(watch["last_ok_at"], t0 + 46)

    def test_the_watcher_captures_by_pane_id(self):
        pane = self.delegate("del_alpha")
        calls = []
        real = subprocess.run

        def record(args, *a, **kw):
            calls.append(list(args))
            return real(args, *a, **kw)

        with patch.object(self.d.subprocess, "run", side_effect=record):
            self.d.tick(now=time.time())
        captures = [c for c in calls if "capture-pane" in c]
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0][captures[0].index("-t") + 1], pane)
        self.assertFalse(any("send-keys" in c for c in calls), "the watcher never types")

    def test_a_failed_listing_keeps_states_and_sets_watch_error(self):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        self.d.tick(now=t0)
        before = self.panes()[pane]
        broken = SimpleNamespace(returncode=1, stdout="", stderr="server exploded")
        with patch.object(self.d.subprocess, "run", return_value=broken):
            self.d.tick(now=t0 + 5)
        watch = self.d.snapshot()["watch"]
        self.assertIn("server exploded", watch["error"])
        self.assertEqual(watch["last_ok_at"], t0)
        self.assertEqual(watch["last_tick_at"], t0 + 5)
        self.assertEqual(self.panes()[pane]["state"], before["state"])
        # Recovery clears the error.
        self.d.tick(now=t0 + 10)
        self.assertIsNone(self.d.snapshot()["watch"]["error"])

    def test_no_tmux_server_is_empty_not_an_error(self):
        self.delegate("del_alpha")
        self.d.tick(now=time.time())
        self.tmux("kill-server")
        self.d.tick(now=time.time())
        snap = self.d.snapshot()
        self.assertEqual(snap["panes"], [])
        self.assertIsNone(snap["watch"]["error"])

    def test_a_pane_exception_marks_only_that_pane_stale(self):
        good = self.delegate("del_good")
        bad = self.delegate("del_bad")
        t0 = time.time()
        self.d.tick(now=t0)
        real = self.d._build_sample

        def boom(row, *a, **kw):
            if row["pane_id"] == bad:
                raise RuntimeError("probe blew up")
            return real(row, *a, **kw)

        with patch.object(self.d, "_build_sample", side_effect=boom):
            self.d.tick(now=t0 + 5)
        panes = self.panes()
        self.assertEqual(panes[bad]["health"], "stale")
        self.assertEqual(panes[bad]["state"], "working", "the published state is kept")
        self.assertEqual(panes[bad]["observed_at"], t0)
        self.assertEqual(panes[good]["health"], "ok")
        self.assertEqual(panes[good]["observed_at"], t0 + 5)
        self.assertIsNone(self.d.snapshot()["watch"]["error"])

    def test_a_pane_that_closes_leaves_the_inventory(self):
        pane = self.delegate("del_alpha")
        self.d.tick(now=time.time())
        self.tmux("kill-session", "-t", "=del_alpha")
        self.d.tick(now=time.time())
        self.assertNotIn(pane, self.panes())
        self.assertNotIn(pane, self.d._MEMORY)

    def test_the_prefixes_setting_decides_which_sessions_are_delegates(self):
        bench = self.delegate("bench_one")
        self.d.tick(now=time.time())
        self.assertNotIn(bench, self.panes())
        self.settings["prefixes"] = ["del_", "bench_"]
        self.d.tick(now=time.time())
        self.assertIn(bench, self.panes())

    def test_an_alert_setting_off_publishes_the_state_without_an_alert(self):
        pane = self.delegate("del_alpha")
        self.settings["alert_finished"] = "off"
        t0 = time.time()
        self.d.tick(now=t0)
        self.d.tick(now=t0 + 46)
        done = self.panes()[pane]
        self.assertEqual(done["state"], "finished")
        self.assertIsNone(done["alert"])

    def test_watch_off_publishes_nothing(self):
        self.delegate("del_alpha")
        self.d.tick(now=time.time())
        self.settings["watch"] = "off"
        self.d.tick(now=time.time())
        self.assertEqual(self.d.snapshot(), {"watch": None, "panes": []})

    def test_the_sample_carries_the_pane_facts(self):
        pane = self.delegate("del_alpha")
        self.tmux("select-pane", "-t", pane, "-T", "⠋ working title")
        captured = {}
        real = self.d._build_sample

        def keep(row, *a, **kw):
            s = real(row, *a, **kw)
            captured[row["pane_id"]] = s
            return s

        with patch.object(self.d, "_build_sample", side_effect=keep):
            self.d.tick(now=time.time())
        s = captured[pane]
        self.assertEqual(s["pane_width"], 80)
        self.assertEqual(s["pane_title"], "⠋ working title")
        self.assertFalse(s["pane_dead"])
        self.assertTrue(s["chrome"]["title_busy"])
        self.assertEqual(s["identity"]["status"], "present")
        self.assertEqual(s["identity"]["kind"], "claude")
        self.assertIsNone(s["claude_status"])
        self.assertTrue(s["capture_ok"])
        self.assertIn("hello from agent", s["tail"])
        self.assertEqual(s["generation"][0], pane)
        self.assertIn("armed", s["autoyes"])


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class WatcherEvidenceTests(_WatchCase):
    """A tick that cannot sample a pane (its exception, or a failed listing)
    breaks that pane's consecutive evidence like a failed capture does: two
    absences around it are not two consecutive absences (finding 4343)."""

    def scripted(self, pane, script):
        from tests.test_delegate_state import ABSENT, sample as data_sample

        steps = iter(script)

        def build(row, now, settings):
            step = next(steps)
            if step == "raise":
                raise RuntimeError("probe blew up")
            identity = ABSENT if step == "absent" else data_sample(0)["identity"]
            return data_sample(now, pane_id=pane, generation=(pane, "R"), identity=identity,
                               changed_at=now - 100)
        return build

    def run_ticks(self, pane, script, listing_fails_at=None):
        t0 = time.time()
        broken = SimpleNamespace(returncode=1, stdout="", stderr="server exploded")
        with patch.object(self.d, "_build_sample", side_effect=self.scripted(pane, script)):
            for i in range(len(script) + (1 if listing_fails_at is not None else 0)):
                if i == listing_fails_at:
                    with patch.object(self.d.subprocess, "run", return_value=broken):
                        self.d.tick(now=t0 + 5 * i)
                else:
                    self.d.tick(now=t0 + 5 * i)
        return self.panes()[pane]

    def test_an_exception_tick_between_two_absences_breaks_the_run(self):
        pane = self.delegate("del_alpha")
        last = self.run_ticks(pane, ["present", "absent", "raise", "absent"])
        self.assertNotEqual(last["state"], "dead")

    def test_a_failed_listing_between_two_absences_breaks_the_run(self):
        pane = self.delegate("del_alpha")
        last = self.run_ticks(pane, ["present", "absent", "absent"], listing_fails_at=2)
        self.assertNotEqual(last["state"], "dead")
        self.assertIsNotNone(self.d.snapshot()["watch"]["last_ok_at"])

    def test_twin_two_consecutive_absent_ticks_are_dead(self):
        pane = self.delegate("del_alpha")
        last = self.run_ticks(pane, ["present", "absent", "absent"])
        self.assertEqual((last["state"], last["reason"]), ("dead", "exited"))


def _observation(pane_pid, root, denied_root=False):
    """A present claude agent under `root`; with denied_root the final root
    recheck failed, so the observer could not validate the root."""
    from shared.agent_identity import ProcessObservation
    errors = [(int(pane_pid), f"/proc/{pane_pid}/stat", "EACCES")] if denied_root else []
    agent = {"kind": "claude", "pid": int(pane_pid), "start": root}
    return ProcessObservation(not denied_root, errors, agent, [dict(agent, argv=["claude"], cwd="/")],
                              False, root)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class RootGenerationWatchTests(_WatchCase):
    """The pane generation is the observer's validated root: a reused `%N`
    with a new root starts fresh, and a root that could not be validated is
    uncertain, never a match for the previous generation (finding 4344)."""

    def two_ticks(self, first, second):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        for now, (root, denied) in ((t0, first), (t0 + 100, second)):
            with patch.object(self.d, "observe_process_tree",
                              side_effect=lambda pid, r=root, d=denied: _observation(pid, r, d)):
                self.d.tick(now=now)
        return self.panes()[pane], t0

    def test_a_readable_root_change_with_the_same_text_starts_fresh(self):
        entry, t0 = self.two_ticks(("1000", False), ("2000", False))
        self.assertEqual((entry["state"], entry["health"]), ("working", "ok"))
        self.assertIsNone(entry["alert"])
        self.assertEqual(pane_scan._SCAN_CACHE[entry["target"]]["changed_at"], t0 + 100)

    def test_a_root_that_cannot_be_validated_is_uncertain_not_finished(self):
        # The judge's trace: the new generation's root read is denied. It must
        # not borrow the previous generation's quiet time and alert FINISHED.
        entry, t0 = self.two_ticks(("1000", False), ("2000", True))
        self.assertEqual((entry["state"], entry["health"]), ("working", "unknown"))
        self.assertIsNone(entry["alert"])
        cached = pane_scan._SCAN_CACHE[entry["target"]]
        self.assertEqual((cached["root"], cached["changed_at"]), ("1000", t0), "published under an unknown root")

    def test_twin_the_same_root_100s_later_is_finished(self):
        entry, _t0 = self.two_ticks(("1000", False), ("1000", False))
        self.assertEqual((entry["state"], entry["health"]), ("finished", "ok"))

    def test_twin_recovery_after_an_unvalidated_root(self):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        for now, root, denied in ((t0, "1000", False), (t0 + 100, "2000", True), (t0 + 105, "2000", False)):
            with patch.object(self.d, "observe_process_tree",
                              side_effect=lambda pid, r=root, d=denied: _observation(pid, r, d)):
                self.d.tick(now=now)
        entry = self.panes()[pane]
        self.assertEqual((entry["state"], entry["health"]), ("working", "ok"))
        self.assertEqual(pane_scan._SCAN_CACHE[entry["target"]]["root"], "2000")


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class RetainedDeadPaneTests(_WatchCase):
    """A pane whose root has exited is gone from /proc, so its root is
    unknown; tmux's own pane_dead flag still says DEAD (finding 4367)."""

    def dead_pane(self):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        self.d.tick(now=t0)
        self.assertEqual(self.panes()[pane]["state"], "working")
        self.tmux("set-option", "-w", "-t", pane, "remain-on-exit", "on")
        self.tmux("send-keys", "-t", pane, "C-c")
        for _ in range(50):
            if self.tmux("display-message", "-p", "-t", pane, "#{pane_dead}") == "1":
                break
            time.sleep(0.05)
        self.assertEqual(self.tmux("display-message", "-p", "-t", pane, "#{pane_dead}"), "1")
        return pane, t0

    def test_a_retained_dead_pane_is_dead_and_alerts(self):
        pane, t0 = self.dead_pane()
        for now in (t0 + 5, t0 + 10, t0 + 100):
            self.d.tick(now=now)
            entry = self.panes()[pane]
            self.assertEqual((entry["state"], entry["reason"], entry["health"]),
                             ("dead", "pane-dead", "ok"), now - t0)
        self.assertEqual(entry["alert"]["state"], "dead")
        self.assertEqual(entry["alert"]["at"], t0 + 5)

    def test_a_retained_dead_pane_with_alert_dead_off_is_dead_without_an_alert(self):
        self.settings["alert_dead"] = "off"
        pane, t0 = self.dead_pane()
        self.d.tick(now=t0 + 5)
        entry = self.panes()[pane]
        self.assertEqual((entry["state"], entry["alert"]), ("dead", None))

    def test_control_a_live_pane_with_an_unreadable_root_stays_uncertain(self):
        pane = self.delegate("del_alpha")
        t0 = time.time()
        self.d.tick(now=t0)
        with patch.object(self.d, "observe_process_tree",
                          side_effect=lambda pid: _observation(pid, "2000", denied_root=True)):
            for now in (t0 + 5, t0 + 100):
                self.d.tick(now=now)
        entry = self.panes()[pane]
        self.assertEqual((entry["state"], entry["health"], entry["alert"]), ("working", "unknown", None))


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class FailurePathTests(_WatchCase):
    """Every failure path (a listing that fails or raises, a pane's
    exception, the watcher loop's own handler) reaches each pane's memory as a
    missing sample and publishes through the healthy path's alert function
    (findings 4357 and 4345)."""

    def setUp(self):
        super().setUp()
        self.pane = self.delegate("del_alpha")
        self.t0 = time.time()

    def scripted(self, script):
        from tests.test_delegate_state import ABSENT, sample as data_sample
        steps = iter(script)
        pane = self.pane

        def build(row, now, settings):
            step = next(steps)
            if step == "raise":
                raise RuntimeError("probe blew up")
            identity = ABSENT if step == "absent" else data_sample(0)["identity"]
            return data_sample(now, pane_id=pane, generation=(pane, "R"), identity=identity,
                               changed_at=now - 100)
        return patch.object(self.d, "_build_sample", side_effect=build)

    def run_loop_once(self, run):
        class Stop(Exception):
            pass

        with patch.object(self.d.subprocess, "run", side_effect=run), \
                patch.object(self.d.time, "sleep", side_effect=Stop):
            with self.assertRaises(Stop):
                self.d.delegate_watcher()

    def test_a_listing_timeout_in_the_watcher_loop_breaks_the_absent_run(self):
        with self.scripted(["present", "absent", "absent", "absent"]):
            self.d.tick(now=self.t0)
            self.d.tick(now=self.t0 + 5)
            self.run_loop_once(subprocess.TimeoutExpired(["tmux", "list-panes"], 5))
            self.assertIn("list-panes", self.d.snapshot()["watch"]["error"])
            after = self.d.tick(now=self.t0 + 15) or self.panes()[self.pane]
            self.assertNotEqual(after["state"], "dead", "a tick that observed nothing kept the absent run")
            # Recovery: the next consecutive absence is DEAD.
            self.d.tick(now=self.t0 + 20)
            self.assertEqual(self.panes()[self.pane]["state"], "dead")

    def test_twin_a_nonzero_listing_in_the_watcher_loop_breaks_it_the_same_way(self):
        broken = SimpleNamespace(returncode=1, stdout="", stderr="server exploded")
        with self.scripted(["present", "absent", "absent"]):
            self.d.tick(now=self.t0)
            self.d.tick(now=self.t0 + 5)
            self.run_loop_once(lambda *a, **k: broken)
            self.d.tick(now=self.t0 + 15)
        self.assertNotEqual(self.panes()[self.pane]["state"], "dead")

    def test_any_tick_exception_in_the_loop_breaks_every_pane_and_keeps_the_state(self):
        with self.scripted(["present", "absent", "absent"]):
            self.d.tick(now=self.t0)
            self.d.tick(now=self.t0 + 5)
            with patch.object(self.d, "_parse", side_effect=ValueError("bad listing")):
                self.run_loop_once(subprocess.run)
            entry = self.panes()[self.pane]
            self.assertEqual((entry["state"], entry["health"]), ("finished", "stale"))
            self.d.tick(now=self.t0 + 15)
        self.assertNotEqual(self.panes()[self.pane]["state"], "dead")

    def finished_then(self, failure, setting_before, setting_after):
        self.settings["alert_finished"] = setting_before
        with self.scripted(["present", "raise"] if failure == "raise" else ["present"]):
            self.d.tick(now=self.t0)
            first = self.panes()[self.pane]
            self.assertEqual(first["state"], "finished")
            self.settings["alert_finished"] = setting_after
            if failure == "listing":
                broken = SimpleNamespace(returncode=1, stdout="", stderr="list failed")
                with patch.object(self.d.subprocess, "run", return_value=broken):
                    self.d.tick(now=self.t0 + 5)
            elif failure == "timeout":
                with patch.object(self.d.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(["tmux"], 5)):
                    self.d.tick(now=self.t0 + 5)
            else:
                self.d.tick(now=self.t0 + 5)
        entry = self.panes()[self.pane]
        self.assertEqual((entry["state"], entry["health"]), ("finished", "stale"))
        return first, entry

    def test_a_listing_failure_after_the_setting_goes_off_clears_the_alert(self):
        for failure in ("listing", "timeout"):
            with self.subTest(failure=failure):
                state.delegate_states.clear()
                self.d._MEMORY.clear()
                first, entry = self.finished_then(failure, "on", "off")
                self.assertIsNotNone(first["alert"])
                self.assertIsNone(entry["alert"])

    def test_a_pane_exception_after_the_setting_comes_on_shows_the_alert_at_its_first_time(self):
        first, entry = self.finished_then("raise", "off", "on")
        self.assertIsNone(first["alert"])
        self.assertEqual(entry["alert"]["key"], entry["key"])
        self.assertEqual(entry["alert"]["at"], self.t0)

    def test_twin_a_listing_failure_with_the_setting_unchanged_keeps_the_same_alert(self):
        first, entry = self.finished_then("listing", "on", "on")
        self.assertEqual(entry["alert"], first["alert"])


class WireAlertTests(unittest.TestCase):
    """The wire `alert` is the current snapshot, not a retained event: derived
    each tick from the published state and key plus the current settings, with
    the key's ORIGINAL publication time (finding 4345). Production classify."""

    ROW = {"pane_id": "%7", "target": "del_x:1.1", "session": "del_x", "session_id": "$1"}
    WORKING = {"working": True, "turn_done": False, "title_busy": False}

    def setUp(self):
        from routes import delegates
        from shared.delegate_state import classify
        from tests.test_delegate_state import sample

        self.d, self.classify, self.sample = delegates, classify, sample
        self.memory = None
        self.settings = dict(state.DEFAULT_SETTINGS["delegates"])

    def tick(self, now, **kw):
        result, self.memory = self.classify(self.sample(now, **kw), self.memory)
        return self.d._entry(self.ROW, result, self.settings)

    def finished(self, now):
        return self.tick(now, changed_at=now - 100)

    def test_turning_the_setting_off_clears_the_alert(self):
        on = self.finished(0)
        self.assertEqual((on["state"], on["alert"]["at"]), ("finished", 0))
        self.settings["alert_finished"] = "off"
        self.assertIsNone(self.finished(5)["alert"])

    def test_turning_it_back_on_shows_the_current_alert_with_its_first_time(self):
        self.settings["alert_finished"] = "off"
        self.assertIsNone(self.finished(0)["alert"])
        self.settings["alert_finished"] = "on"
        entry = self.finished(5)
        self.assertEqual(entry["alert"]["key"], entry["key"])
        self.assertEqual(entry["alert"]["at"], 0, "the publication time, not a fresh event time")

    def test_finished_brief_working_same_finished_key_keeps_the_alert_and_its_time(self):
        first = self.finished(0)
        working = self.tick(10, chrome=self.WORKING)
        self.assertEqual(working["state"], "working")
        self.assertIsNone(working["alert"], "no alert while WORKING")
        again = self.finished(60)
        self.assertEqual(again["key"], first["key"])
        self.assertEqual(again["alert"], first["alert"])
        self.assertEqual(again["alert"]["at"], 0)

    def test_twin_an_unchanged_finished_state_keeps_the_same_alert(self):
        first = self.finished(0)
        for now in (5, 10, 15):
            self.assertEqual(self.finished(now)["alert"], first["alert"])

    def test_twin_the_setting_unchanged_off_never_shows_one(self):
        self.settings["alert_finished"] = "off"
        for now in (0, 5, 10):
            self.assertIsNone(self.finished(now)["alert"])

    def test_a_working_pane_never_carries_an_alert(self):
        self.assertIsNone(self.tick(0, chrome=self.WORKING)["alert"])


class ClaudeStatusTests(unittest.TestCase):
    def setUp(self):
        from routes import delegates

        self.d = delegates
        self.dir = Path(tempfile.mkdtemp(prefix="assist-claude-sessions-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, **record):
        (self.dir / f"{name}.json").write_text(json.dumps(record))

    def test_status_is_read_only_for_the_same_process_start(self):
        self.write(42, pid=42, procStart="5100", status="busy")
        self.assertEqual(self.d._claude_status(42, "5100", self.dir), "busy")

    def test_twin_a_different_start_is_not_this_process(self):
        self.write(42, pid=42, procStart="9999", status="busy")
        self.assertIsNone(self.d._claude_status(42, "5100", self.dir))

    def test_a_missing_or_corrupt_file_is_none(self):
        self.assertIsNone(self.d._claude_status(43, "5100", self.dir))
        (self.dir / "44.json").write_text("{")
        self.assertIsNone(self.d._claude_status(44, "5100", self.dir))
        self.assertIsNone(self.d._claude_status(None, None, self.dir))


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class ApiTests(_WatchCase):
    TOKEN = "test-token-not-the-real-one"

    def setUp(self):
        super().setUp()
        saved = auth._token_cache
        auth._token_cache = self.TOKEN
        self.addCleanup(setattr, auth, "_token_cache", saved)
        app = Flask(__name__)
        auth_gate.register_auth_gate(app)
        app.register_blueprint(self.d.delegates_bp)
        self.client = app.test_client()

    def get(self, headers=None):
        return self.client.get("/api/delegates", headers=headers if headers is not None
                               else {auth.HEADER_NAME: self.TOKEN})

    def test_the_endpoint_needs_the_token(self):
        self.assertEqual(self.get(headers={}).status_code, 401)
        self.assertEqual(self.get().status_code, 200)

    def test_an_empty_scratch_tmux_is_no_delegates_and_no_error(self):
        self.d.tick(now=time.time())
        body = self.get().get_json()
        self.assertEqual(body["panes"], [])
        self.assertIsNone(body["watch"]["error"])
        self.assertIsNotNone(body["watch"]["epoch"])

    def test_a_broken_tmux_sets_watch_error(self):
        broken = SimpleNamespace(returncode=1, stdout="", stderr="error connecting to sock (Permission denied)")
        with patch.object(self.d.subprocess, "run", return_value=broken):
            self.d.tick(now=time.time())
        body = self.get().get_json()
        self.assertIn("Permission denied", body["watch"]["error"])

    def test_no_socket_or_a_refused_connection_is_no_server_not_an_error(self):
        # Seen on a scratch server: with no tmux server running, tmux reports a
        # missing socket rather than "no server running".
        for stderr in ("no server running on /tmp/x/default",
                       "error connecting to /tmp/x/default (No such file or directory)",
                       "error connecting to /tmp/x/default (Connection refused)"):
            with self.subTest(stderr=stderr):
                empty = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
                with patch.object(self.d.subprocess, "run", return_value=empty):
                    self.d.tick(now=time.time())
                body = self.get().get_json()
                self.assertEqual(body["panes"], [])
                self.assertIsNone(body["watch"]["error"])

    def test_before_the_first_tick_the_watch_is_off_or_unknown(self):
        body = self.get().get_json()
        self.assertEqual(body, {"watch": None, "panes": []})

    def test_a_delegate_pane_is_listed_with_its_wire_fields(self):
        pane = self.delegate("del_alpha")
        self.d.tick(now=time.time())
        (entry,) = self.get().get_json()["panes"]
        self.assertEqual(entry["pane_id"], pane)
        for field in ("state", "reason", "detail", "since", "episode", "health", "observed_at",
                      "alert", "target", "session"):
            self.assertIn(field, entry)


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class PollWireTests(_WatchCase):
    def setUp(self):
        super().setUp()
        from routes import poll

        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        self.client = app.test_client()

    def test_poll_carries_delegate_and_watch_and_leaves_states_alone(self):
        pane = self.delegate("del_alpha")
        before = self.client.get("/poll").get_json()
        self.d.tick(now=time.time())
        after = self.client.get("/poll").get_json()
        self.assertIn("watch", after)
        self.assertIsNone(after["watch"]["error"])
        (mine,) = [s for s in after["sessions"] if s.get("pane_id") == pane]
        self.assertEqual(mine["delegate"]["state"], "working")
        for field in ("state", "reason", "detail", "since", "episode", "health", "observed_at", "alert"):
            self.assertIn(field, mine["delegate"])
        others = [s for s in after["sessions"] if s.get("pane_id") != pane]
        self.assertTrue(others)
        self.assertFalse(any("delegate" in s for s in others))
        self.assertEqual(set(before["states"]), set(after["states"]))
        for key, entry in after["states"].items():
            self.assertEqual(set(entry), set(before["states"][key]), key)


class WiringTests(unittest.TestCase):
    def test_serve_starts_the_watcher_beside_the_scanner_and_registers_the_blueprint(self):
        source = (ROOT / "serve.py").read_text()
        self.assertIn("threading.Thread(target=delegate_watcher, daemon=True).start()", source)
        self.assertIn("app.register_blueprint(delegates_bp)", source)

    def test_state_declares_the_watcher_maps(self):
        self.assertIsInstance(state.delegate_states, dict)
        self.assertTrue(hasattr(state, "delegate_lock"))


if __name__ == "__main__":
    unittest.main()

class TerminalDelegateSnapshotTests(_WatchCase):
    def test_sessions_preserves_watch_state_and_removes_it_when_watch_is_off(self):
        from routes.terminal import terminal_bp
        app = Flask(__name__)
        app.register_blueprint(terminal_bp)
        client = app.test_client()
        pane = self.delegate('del_sessions')
        self.d.tick(now=time.time())
        records = client.get('/terminal/sessions').get_json()['sessions']
        record = next(p for p in records if p['pane_id'] == pane)
        self.assertEqual(record['delegate']['state'], 'working')
        self.assertIn('resume', record)
        self.settings['watch'] = 'off'
        self.d.tick(now=time.time())
        records = client.get('/terminal/sessions').get_json()['sessions']
        record = next(p for p in records if p['pane_id'] == pane)
        self.assertNotIn('state', record['delegate'])
