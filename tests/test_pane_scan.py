"""shared/pane_scan.py — one capture per pane, published in order, with activity.

/poll and the delegate watcher (spec 7185 §5.1) both read panes through
`sample()`, which owns the capture cache AND the activity hash. The two used to
be written separately by /poll (routes/poll.py), one handler per browser, so a
late capture could land an older hash and time after a newer one, and a failed
`list-panes` wiped every pane's activity because the live set came back empty.

Each rule is pinned in both directions: the ordering guard rejects only the
stale writer, coalescing produces one capture for two callers, cleanup runs
only after a complete listing, and a new pane_id at an old target starts fresh.

Run: .venv/bin/python3 -m unittest tests.test_pane_scan
"""

import hashlib
import os
import threading
import time
import unittest
from unittest.mock import patch

MARKER = ("1000", "40", "0", "5", "120", "40", "0", "%7")


def _hash(tail):
    return hashlib.md5(tail.encode()).hexdigest()


class _Fresh(unittest.TestCase):
    """Every test runs against an empty cache and empty activity maps."""

    def setUp(self):
        from shared import pane_scan, state

        self.ps = pane_scan
        self.state = state
        for p in (
            patch.object(pane_scan, "_SCAN_CACHE", {}),
            patch.object(pane_scan, "_INFLIGHT", {}),
            patch.object(state, "pane_content_hash", {}),
            patch.object(state, "pane_last_activity", {}),
        ):
            p.start()
            self.addCleanup(p.stop)

    def sample(self, target="dev:1.1", pane_id="%7", marker=MARKER, now=None,
               max_age=30, capture=None, kind="shell"):
        return self.ps.sample(
            target, pane_id, marker, now if now is not None else time.time(), max_age,
            capture=capture or (lambda t: "hello"), kind_of=lambda tail: kind,
        )


class OrderedPublicationTests(_Fresh):
    def test_the_older_capture_published_second_changes_nothing(self):
        """Two writers, barrier-controlled: A draws its seq first and captures
        'old'; B draws its seq second and captures 'new'. B publishes first.
        A's publication must then be rejected: cache AND changed_at untouched."""
        target = "dev:1.1"
        a_in_capture = threading.Event()
        b_published = threading.Event()
        results = {}

        def capture_a(_t):
            a_in_capture.set()
            # Hold A inside its capture until B has published.
            self.assertTrue(b_published.wait(5), "B never published")
            return "old"

        def capture_b(_t):
            return "new"

        # Any marker difference means A's cached entry is not reusable for B,
        # and the in-flight flag must not make B wait on A forever either, so
        # B is given its own target-less path: run A, then B, both uncoalesced
        # by clearing the in-flight flag A set.
        def run_a():
            results["a"] = self.sample(target, capture=capture_a, now=1000.0)

        ta = threading.Thread(target=run_a)
        ta.start()
        self.assertTrue(a_in_capture.wait(5))
        # B is a later writer with a higher seq. Clear A's in-flight mark so B
        # captures itself instead of coalescing onto A (the interleaving under
        # test is two independent captures landing out of order).
        with self.ps._SCAN_LOCK:
            self.ps._INFLIGHT.pop((target, "%7"), None)
        results["b"] = self.sample(target, capture=capture_b, now=1001.0)
        b_published.set()
        ta.join(5)

        entry = self.ps._SCAN_CACHE[target]
        self.assertEqual(entry["raw"], "new")
        self.assertEqual(self.state.pane_content_hash[target], _hash("new"))
        self.assertEqual(self.state.pane_last_activity[target], 1001.0)
        self.assertEqual(results["b"].raw, "new")
        # A's return reflects what is published, not what it captured.
        self.assertEqual(results["a"].raw, "new")
        self.assertEqual(results["a"].changed_at, 1001.0)
        self.assertEqual(results["a"].captured_seq, results["b"].captured_seq)
        self.assertFalse(results["a"].changed)

    def test_a_newer_capture_published_second_does_land(self):
        self.sample(capture=lambda t: "first", now=1000.0)
        got = self.sample(capture=lambda t: "second", now=1001.0,
                          marker=MARKER[:1] + ("41",) + MARKER[2:])
        self.assertEqual(got.raw, "second")
        self.assertEqual(self.state.pane_content_hash["dev:1.1"], _hash("second"))
        self.assertEqual(got.changed_at, 1001.0)
        self.assertTrue(got.changed)

    def test_cache_and_activity_are_published_together(self):
        got = self.sample(capture=lambda t: "x", now=1000.0, kind="codex")
        entry = self.ps._SCAN_CACHE["dev:1.1"]
        expect = _hash(self.ps._activity_tail("x", "codex"))
        self.assertEqual(entry["content_hash"], expect)
        self.assertEqual(self.state.pane_content_hash["dev:1.1"], expect)
        self.assertEqual(entry["changed_at"], 1000.0)
        self.assertEqual(got.content_hash, expect)
        self.assertEqual(got.changed_at, self.state.pane_last_activity["dev:1.1"])

    def test_an_unchanged_tail_keeps_rev_and_changed_at(self):
        first = self.sample(capture=lambda t: "same", now=1000.0)
        second = self.sample(capture=lambda t: "same", now=1050.0, max_age=0)
        self.assertEqual(second.rev, first.rev)
        self.assertEqual(second.changed_at, 1000.0)
        self.assertFalse(second.changed)


class CoalescingTests(_Fresh):
    def test_two_concurrent_callers_produce_one_capture(self):
        target = "dev:1.1"
        captures = []
        release = threading.Event()
        started = threading.Event()

        def slow_capture(_t):
            captures.append(1)
            started.set()
            self.assertTrue(release.wait(5))
            return "shared"

        results = {}

        def first():
            results["first"] = self.sample(target, capture=slow_capture)

        def second():
            results["second"] = self.sample(target, capture=slow_capture)

        t1 = threading.Thread(target=first)
        t1.start()
        self.assertTrue(started.wait(5))
        t2 = threading.Thread(target=second)
        t2.start()
        time.sleep(0.2)  # the second caller is now waiting on the flag
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(len(captures), 1, "the second caller captured too")
        self.assertEqual(results["first"].raw, "shared")
        self.assertEqual(results["second"].raw, "shared")
        self.assertEqual(results["first"].captured_seq, results["second"].captured_seq)

    def test_the_wait_is_bounded(self):
        target = "dev:1.1"
        never = threading.Event()

        def stuck(_t):
            never.wait(10)
            return "late"

        threading.Thread(target=lambda: self.sample(target, capture=stuck), daemon=True).start()
        time.sleep(0.1)
        with patch.object(self.ps, "INFLIGHT_WAIT_SEC", 0.2):
            t0 = time.monotonic()
            got = self.sample(target, capture=lambda t: "own")
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(got.raw, "own", "a timed-out waiter must capture itself")
        never.set()

    def test_capture_runs_with_no_lock_held(self):
        def capture(_t):
            self.assertTrue(self.ps._SCAN_LOCK.acquire(timeout=1),
                            "capture-pane ran under the scan lock")
            self.ps._SCAN_LOCK.release()
            return "ok"

        self.assertEqual(self.sample(capture=capture).raw, "ok")

    def test_a_failed_capture_publishes_nothing_and_clears_the_flag(self):
        got = self.sample(capture=lambda t: None)
        self.assertIsNone(got)
        self.assertNotIn("dev:1.1", self.ps._SCAN_CACHE)
        self.assertNotIn("dev:1.1", self.state.pane_content_hash)
        self.assertNotIn(("dev:1.1", "%7"), self.ps._INFLIGHT)
        self.assertEqual(self.sample(capture=lambda t: "later").raw, "later")


class CleanupTests(_Fresh):
    def test_a_failed_listing_removes_nothing(self):
        self.sample("dev:1.1", "%7")
        self.sample("dev2:1.1", "%8")
        self.ps.forget_missing(set(), listing_ok=False)
        self.assertEqual(set(self.ps._SCAN_CACHE), {"dev:1.1", "dev2:1.1"})
        self.assertEqual(set(self.state.pane_content_hash), {"dev:1.1", "dev2:1.1"})
        self.assertEqual(set(self.state.pane_last_activity), {"dev:1.1", "dev2:1.1"})

    def test_a_complete_listing_removes_vanished_entries_only(self):
        self.sample("dev:1.1", "%7")
        self.sample("dev2:1.1", "%8")
        changed = self.ps.forget_missing({"dev:1.1"}, listing_ok=True)
        self.assertTrue(changed)
        self.assertEqual(set(self.ps._SCAN_CACHE), {"dev:1.1"})
        self.assertEqual(set(self.state.pane_content_hash), {"dev:1.1"})
        self.assertEqual(set(self.state.pane_last_activity), {"dev:1.1"})
        self.assertFalse(self.ps.forget_missing({"dev:1.1"}, listing_ok=True))


class PaneIdentityTests(_Fresh):
    def test_a_new_pane_id_at_an_old_target_starts_a_fresh_entry(self):
        first = self.sample("dev:1.1", "%7", capture=lambda t: "old pane", now=1000.0)
        # Same marker apart from the pane id: the old entry would be "reusable"
        # by marker alone; the pane_id check must discard it and recapture.
        captures = []

        def capture(_t):
            captures.append(1)
            return "old pane"

        second = self.sample("dev:1.1", "%9", marker=MARKER[:7] + ("%9",),
                             capture=capture, now=1001.0)
        self.assertEqual(captures, [1])
        self.assertEqual(self.ps._SCAN_CACHE["dev:1.1"]["pane_id"], "%9")
        # Same text, but a different pane: activity restarts at this sample.
        self.assertEqual(second.changed_at, 1001.0)
        self.assertNotEqual(first.captured_seq, second.captured_seq)

    def test_the_same_pane_id_reuses_the_entry(self):
        self.sample("dev:1.1", "%7", capture=lambda t: "text")
        captures = []
        got = self.sample("dev:1.1", "%7", capture=lambda t: captures.append(1) or "x")
        self.assertEqual(captures, [])
        self.assertEqual(got.raw, "text")


MARKER2 = MARKER[:1] + ("41",) + MARKER[2:]


class _ReleaseBarrierLock:
    """Wraps the scan lock: each of `parties` threads, on its FIRST release,
    waits for the others to reach theirs. Two callers released together from
    their first critical section is exactly the window where a lookup and a
    flight registration done in separate sections both see "no flight"."""

    def __init__(self, inner, parties):
        self.inner = inner
        self.barrier = threading.Barrier(parties)
        self.done = set()

    def acquire(self, *a, **kw):
        return self.inner.acquire(*a, **kw)

    def release(self):
        self.inner.release()
        me = threading.get_ident()
        if me not in self.done:
            self.done.add(me)
            try:
                self.barrier.wait(2)
            except threading.BrokenBarrierError:
                pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class SimultaneousArrivalTests(_Fresh):
    """Two callers that arrive together produce one capture (finding 4341)."""

    def race(self, marker):
        captures = []
        results = {}
        go = threading.Barrier(2)

        def capture(_t):
            captures.append(1)
            time.sleep(0.2)
            return "shared"

        def caller(name):
            go.wait(2)
            results[name] = self.sample(capture=capture, marker=marker, now=20.0)

        with patch.object(self.ps, "_SCAN_LOCK", _ReleaseBarrierLock(self.ps._SCAN_LOCK, 2)):
            threads = [threading.Thread(target=caller, args=(n,)) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        return captures, results

    def test_simultaneous_first_callers_capture_once(self):
        captures, results = self.race(MARKER)
        self.assertEqual(len(captures), 1, "both first callers captured")
        self.assertEqual(results["a"].raw, "shared")
        self.assertEqual(results["b"].raw, "shared")
        self.assertEqual(self.ps._INFLIGHT, {})

    def test_simultaneous_refreshes_of_a_cached_pane_capture_once(self):
        self.sample(capture=lambda t: "cached", now=10.0)
        captures, results = self.race(MARKER2)
        self.assertEqual(len(captures), 1, "both refreshing callers captured")
        self.assertEqual({results["a"].raw, results["b"].raw}, {"shared"})

    def test_twin_a_caller_after_the_flight_landed_reuses_it(self):
        self.sample(capture=lambda t: "first", now=10.0)
        captures = []
        got = self.sample(capture=lambda t: captures.append(1) or "x", now=11.0)
        self.assertEqual((captures, got.raw), ([], "first"))


class PollCaptureIdentityTests(unittest.TestCase):
    """/poll captures the listed pane by its id, so the bytes come from the
    pane the listing named wherever it sits now (finding 4333). A `%N` is
    unique only within one tmux server's life, so the id is then checked to
    still name the listed pane pid, failing closed (finding 4366)."""

    def run_capture(self, pane_id="%1", pane_pid="111", identity="111\n", rc=0):
        from types import SimpleNamespace
        from routes import poll

        calls = []

        def run(args, **_kw):
            calls.append(list(args))
            if "capture-pane" in args:
                return SimpleNamespace(returncode=0, stdout="pane text\n", stderr="")
            if "display-message" in args:
                return SimpleNamespace(returncode=rc, stdout=identity, stderr="")
            raise AssertionError(args)

        with patch.object(poll.subprocess, "run", side_effect=run):
            raw = poll._capture_tail("del_x:1.1", pane_id, pane_pid)
        return raw, calls

    def test_the_capture_addresses_the_listed_pane_id_and_checks_its_pid(self):
        raw, calls = self.run_capture()
        self.assertEqual(raw, "pane text\n")
        self.assertEqual([c[c.index("-t") + 1] for c in calls], ["%1", "%1"], "both by id, never by name")
        self.assertIn("#{pane_pid}", calls[1])

    def test_an_id_that_now_names_another_pid_is_no_capture(self):
        self.assertIsNone(self.run_capture(identity="222\n")[0])

    def test_an_id_that_disappeared_or_an_empty_answer_is_no_capture(self):
        self.assertIsNone(self.run_capture(identity="", rc=1)[0])
        self.assertIsNone(self.run_capture(identity="\n")[0])

    def test_an_unknown_listed_pid_is_no_capture(self):
        self.assertIsNone(self.run_capture(pane_pid=None)[0])

    def test_without_a_listed_pane_id_it_addresses_the_exact_name(self):
        raw, calls = self.run_capture(pane_id=None, pane_pid=None)
        self.assertEqual(calls[0][calls[0].index("-t") + 1], "=del_x:1.1")


class PollReplacementTests(unittest.TestCase):
    """Through the real /poll handler on an isolated tmux: the session is
    killed and recreated (a new pane at the same name) between /poll's listing
    and its capture."""

    def setUp(self):
        import shutil
        if not shutil.which("tmux"):
            self.skipTest("needs tmux")
        from flask import Flask
        from routes import poll
        from shared import pane_scan, state
        from tests.test_exact_targets import _IsolatedTmux

        class Box(_IsolatedTmux):
            def runTest(self):
                pass

        self.box = Box()
        self.box.setUp()
        self.addCleanup(self.box.doCleanups)
        self.poll, self.ps = poll, pane_scan
        cache = {}
        for p in (patch.object(pane_scan, "_SCAN_CACHE", cache), patch.object(pane_scan, "_INFLIGHT", {}),
                  patch.object(poll, "_SCAN_CACHE", cache),
                  patch.object(state, "pane_content_hash", {}), patch.object(state, "pane_last_activity", {}),
                  patch.object(state, "save_idle_state")):
            p.start()
            self.addCleanup(p.stop)
        self.box.tmux("new-session", "-d", "-s", "del_x", "-x", "80", "-y", "20", "printf 'old pane\\n'; sleep 600")
        self.old = self.box.pane_id("del_x")
        time.sleep(0.2)
        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        self.client = app.test_client()

    def get(self, replace):
        real = self.poll.subprocess.run
        box = self.box

        def run(args, *a, **kw):
            if replace and "capture-pane" in args and args[args.index("-t") + 1] in (self.old, "=del_x:1.1"):
                replace.pop()
                box.tmux("kill-session", "-t", "=del_x")
                box.tmux("new-session", "-d", "-s", "del_x", "-x", "80", "-y", "20",
                         "printf 'replacement\\n'; sleep 600")
                time.sleep(0.2)
            return real(args, *a, **kw)

        with patch.object(self.poll.subprocess, "run", side_effect=run):
            self.assertEqual(self.client.get("/poll").status_code, 200)
        return self.ps._SCAN_CACHE.get("del_x:1.1")

    def test_replacement_bytes_are_never_stamped_with_the_listed_pane(self):
        entry = self.get(replace=[True])
        new = self.box.pane_id("del_x")
        self.assertNotEqual(new, self.old)
        self.assertFalse(entry and entry["pane_id"] == self.old and "replacement" in entry["raw"],
                         "the replacement pane's bytes were stamped with the old pane id")
        self.assertIsNone(entry)
        # The next poll lists the new pane and captures it as itself.
        entry = self.get(replace=[])
        self.assertEqual(entry["pane_id"], new)
        self.assertIn("replacement", entry["raw"])

    def test_a_pane_swapped_away_and_back_still_gives_the_listed_panes_bytes(self):
        # The judge's real-tmux trace: B is swapped into the listed name just
        # before the capture and A swapped back right after it.
        self.box.tmux("new-session", "-d", "-s", "other", "-x", "80", "-y", "20",
                      "printf 'bytes of pane B\\n'; sleep 600")
        b = self.box.pane_id("other")
        time.sleep(0.2)
        real = self.poll.subprocess.run
        box, a, done = self.box, self.old, []

        def run(args, *rest, **kw):
            if "capture-pane" in args and not done and (
                    a in args or any(str(x).startswith("=del_x:") for x in args)):
                done.append(1)
                box.tmux("swap-pane", "-s", a, "-t", b)
                try:
                    return real(args, *rest, **kw)
                finally:
                    box.tmux("swap-pane", "-s", a, "-t", b)
            return real(args, *rest, **kw)

        with patch.object(self.poll.subprocess, "run", side_effect=run):
            self.assertEqual(self.client.get("/poll").status_code, 200)
        self.assertTrue(done, "the swap never ran")
        entry = self.ps._SCAN_CACHE.get("del_x:1.1")
        self.assertEqual(entry["pane_id"], a)
        self.assertNotIn("bytes of pane B", entry["raw"], "another pane's bytes cached under the listed id")
        self.assertIn("old pane", entry["raw"])

    def test_a_tmux_restart_reusing_the_listed_id_gives_no_bytes_from_the_new_pane(self):
        # The judge's trace: list del_x:1.1 as %2, then restart this private
        # server and recreate the same sessions before the capture, so the new
        # del_x:1.1 is again %2 with a different root.
        import subprocess as sp
        from pathlib import Path
        box, old, done = self.box, self.old, []
        conf = str(Path(box.tmpdir) / "tmux.conf")
        real = self.poll.subprocess.run

        def run(args, *rest, **kw):
            if "capture-pane" in args and not done and old in args:
                done.append(1)
                box.tmux("kill-server", check=False)
                sp.run(["tmux", "-S", str(box.socket), "-f", conf, "new-session", "-d", "-s", "dev2",
                        "-x", "80", "-y", "20"], check=True, capture_output=True, timeout=5)
                box.tmux("new-session", "-d", "-s", "dev", "-x", "80", "-y", "20")
                box.tmux("new-session", "-d", "-s", "del_x", "-x", "80", "-y", "20",
                         "printf 'RESTART NEW GENERATION\\n'; sleep 600")
                time.sleep(0.2)
            return real(args, *rest, **kw)

        with patch.object(self.poll.subprocess, "run", side_effect=run):
            self.assertEqual(self.client.get("/poll").status_code, 200)
        self.assertTrue(done, "the restart never ran")
        self.assertEqual(self.box.pane_id("del_x"), old, "the restarted server did not reuse the id")
        entry = self.ps._SCAN_CACHE.get("del_x:1.1")
        self.assertFalse(entry and "RESTART NEW GENERATION" in entry["raw"],
                         "the new generation's bytes were cached under the listed pane")

    def test_an_unknown_root_is_not_sampled_and_never_reads_as_idle(self):
        from shared import state
        from shared.agent_identity import ROOT_UNKNOWN
        state.pane_last_activity["del_x:1.1"] = time.time() - 10000  # another generation's time
        calls = []
        real = self.poll.subprocess.run

        def run(args, *a, **kw):
            calls.append(list(args))
            return real(args, *a, **kw)

        with patch.object(self.poll, "pane_root_start", return_value=ROOT_UNKNOWN), \
                patch.object(self.poll.subprocess, "run", side_effect=run):
            body = self.client.get("/poll").get_json()
        self.assertFalse([c for c in calls if "capture-pane" in c and self.old in c])
        self.assertNotIn("del_x:1.1", self.ps._SCAN_CACHE)
        self.assertLess(body["states"]["del_x:1.1"]["idle_seconds"], 5)

    def test_twin_an_unchanged_pane_is_captured_and_stamped(self):
        entry = self.get(replace=[])
        self.assertEqual(entry["pane_id"], self.old)
        self.assertIn("old pane", entry["raw"])


class FailedRefreshTests(_Fresh):
    """A waiter coalesced onto a refresh gets that refresh's result only when it
    succeeded for the same pane and listing; a failed or timed-out refresh never
    hands it the old cached sample as current (finding 4331)."""

    def _hold(self, result):
        started, release = threading.Event(), threading.Event()

        def capture(_t):
            started.set()
            self.assertTrue(release.wait(5))
            return result

        return capture, started, release

    def test_a_failed_refresh_does_not_hand_a_waiter_the_cached_sample(self):
        self.sample(capture=lambda t: "cached", now=10.0)
        capture, started, release = self._hold(None)
        results = {}
        owner = threading.Thread(target=lambda: results.setdefault(
            "owner", self.sample(capture=capture, marker=MARKER2, now=20.0)))
        owner.start()
        self.assertTrue(started.wait(5))
        waiter = threading.Thread(target=lambda: results.setdefault(
            "waiter", self.sample(capture=lambda t: "fresh", marker=MARKER2, now=21.0)))
        waiter.start()
        time.sleep(0.2)
        release.set()
        owner.join(5)
        waiter.join(5)
        self.assertIsNone(results["owner"])
        self.assertIsNotNone(results["waiter"])
        self.assertEqual(results["waiter"].raw, "fresh", "the waiter got the stale cached sample")

    def test_a_failed_refresh_and_a_failed_fallback_is_a_failure(self):
        self.sample(capture=lambda t: "cached", now=10.0)
        capture, started, release = self._hold(None)
        results = {}
        owner = threading.Thread(target=lambda: self.sample(capture=capture, marker=MARKER2, now=20.0))
        owner.start()
        self.assertTrue(started.wait(5))
        waiter = threading.Thread(target=lambda: results.setdefault(
            "waiter", self.sample(capture=lambda t: None, marker=MARKER2, now=21.0)))
        waiter.start()
        time.sleep(0.2)
        release.set()
        owner.join(5)
        waiter.join(5)
        self.assertIsNone(results["waiter"])
        self.assertEqual(self.ps._SCAN_CACHE["dev:1.1"]["raw"], "cached")

    def test_a_successful_refresh_is_shared_with_the_waiter(self):
        self.sample(capture=lambda t: "cached", now=10.0)
        capture, started, release = self._hold("refreshed")
        captures = []
        results = {}
        owner = threading.Thread(target=lambda: results.setdefault(
            "owner", self.sample(capture=capture, marker=MARKER2, now=20.0)))
        owner.start()
        self.assertTrue(started.wait(5))
        waiter = threading.Thread(target=lambda: results.setdefault(
            "waiter", self.sample(capture=lambda t: captures.append(1) or "own",
                                  marker=MARKER2, now=21.0)))
        waiter.start()
        time.sleep(0.2)
        release.set()
        owner.join(5)
        waiter.join(5)
        self.assertEqual(captures, [], "the waiter captured although the refresh succeeded")
        self.assertEqual(results["waiter"].raw, "refreshed")
        self.assertEqual(results["waiter"].captured_seq, results["owner"].captured_seq)

    def test_a_timed_out_wait_with_a_cache_captures_itself(self):
        self.sample(capture=lambda t: "cached", now=10.0)
        never = threading.Event()
        self.addCleanup(never.set)
        threading.Thread(target=lambda: self.sample(
            capture=lambda t: never.wait(10) and "late", marker=MARKER2, now=20.0), daemon=True).start()
        time.sleep(0.1)
        with patch.object(self.ps, "INFLIGHT_WAIT_SEC", 0.2):
            got = self.sample(capture=lambda t: "own", marker=MARKER2, now=21.0)
        self.assertEqual(got.raw, "own")


class ConcurrentReplacementTests(_Fresh):
    """A target reused by a new pane while samples are in flight: a listing
    older than the published pane cannot evict it, and an older pane's capture
    cannot overwrite it (finding 4333)."""

    def test_an_obsolete_listing_cannot_evict_the_newer_pane(self):
        target = "dev:1.1"
        m1, m2 = MARKER, MARKER[:7] + ("%2",)
        capture_a, a_started, a_release = FailedRefreshTests._hold(self, "current new-pane text")
        results = {}
        a = threading.Thread(target=lambda: results.setdefault(
            "a", self.sample(target, "%1", marker=m1, capture=capture_a, now=10.0)))
        a.start()
        self.assertTrue(a_started.wait(5))
        with patch.object(self.ps, "INFLIGHT_WAIT_SEC", 0.1):
            results["b"] = self.sample(target, "%2", marker=m2, capture=lambda t: "new pane", now=20.0)
            results["c"] = self.sample(target, "%1", marker=m1,
                                       capture=lambda t: "current new-pane text", now=11.0)
        a_release.set()
        a.join(5)
        entry = self.ps._SCAN_CACHE[target]
        self.assertEqual((entry["pane_id"], entry["raw"]), ("%2", "new pane"))
        self.assertEqual(self.state.pane_content_hash[target], _hash("new pane"))
        self.assertEqual(self.state.pane_last_activity[target], 20.0)
        self.assertEqual((results["b"].generation, results["b"].raw), ("%2", "new pane"))
        self.assertIsNone(results["c"], "an obsolete listing was sampled as the old pane")
        self.assertIsNone(results["a"], "the old pane's late capture was returned as current")

    def test_a_late_old_pane_capture_cannot_overwrite_the_newer_pane(self):
        # A draws its seq after B's but carries the older listing.
        target = "dev:1.1"
        self.sample(target, "%2", marker=MARKER[:7] + ("%2",), capture=lambda t: "new pane", now=20.0)
        got = self.sample(target, "%1", marker=MARKER, capture=lambda t: "anything", now=10.0)
        self.assertIsNone(got)
        self.assertEqual(self.ps._SCAN_CACHE[target]["pane_id"], "%2")
        self.assertEqual(self.state.pane_last_activity[target], 20.0)

    def test_a_newer_pane_replaces_an_old_pane_in_flight(self):
        # The ordinary transition, concurrently: the old pane's flight is still
        # out when the new pane's newer listing samples; the new pane wins.
        target = "dev:1.1"
        capture_a, a_started, a_release = FailedRefreshTests._hold(self, "old pane text")
        results = {}
        a = threading.Thread(target=lambda: results.setdefault(
            "a", self.sample(target, "%1", marker=MARKER, capture=capture_a, now=10.0)))
        a.start()
        self.assertTrue(a_started.wait(5))
        results["b"] = self.sample(target, "%2", marker=MARKER[:7] + ("%2",),
                                   capture=lambda t: "new pane", now=20.0)
        a_release.set()
        a.join(5)
        self.assertEqual(results["b"].raw, "new pane")
        self.assertEqual(self.ps._SCAN_CACHE[target]["pane_id"], "%2")
        self.assertEqual(self.state.pane_last_activity[target], 20.0)


class PurgePublicationTests(_Fresh):
    """Purge's capture is published through the same ordered protocol as every
    other sample: cache and activity together, rejected together when a newer
    sample landed first (finding 4332)."""

    LISTING = "dev\t$1\t999\t1000\t1\t1\t%7\t123\tbash\t\n"

    def _observe(self, capture_text, before_return=None):
        from types import SimpleNamespace
        from shared import purge

        def run(args, **_kwargs):
            if "list-panes" in args:
                return SimpleNamespace(returncode=0, stdout=self.LISTING, stderr="")
            if "capture-pane" in args:
                if before_return:
                    before_return()
                return SimpleNamespace(returncode=0, stdout=capture_text + "\n", stderr="")
            raise AssertionError(args)

        with patch.object(purge.subprocess, "run", run), \
                patch.object(purge, "resolve_process", return_value="shell"), \
                patch.object(purge, "_process_start_time", return_value="x"), \
                patch.object(purge, "pane_root_start", return_value=getattr(self, "root", None)), \
                patch.object(purge.state, "save_idle_state"), \
                patch.object(purge.state, "assist_owned_sessions", set()), \
                patch.object(purge, "time", SimpleNamespace(time=lambda: 2000.0)):
            return purge._observe()

    def test_a_late_purge_capture_does_not_overwrite_a_newer_sample(self):
        target = "dev:1.1"

        def newer_sample_lands():
            self.sample(target, "%7", capture=lambda t: "new", now=3000.0)

        self._observe("old", before_return=newer_sample_lands)
        entry = self.ps._SCAN_CACHE[target]
        self.assertEqual(entry["raw"], "new")
        self.assertEqual(self.state.pane_content_hash[target], _hash("new"))
        self.assertEqual(self.state.pane_last_activity[target], entry["changed_at"])
        self.assertEqual(entry["changed_at"], 3000.0)

    def test_a_newer_purge_capture_publishes_cache_and_activity_together(self):
        target = "dev:1.1"
        self.sample(target, "%7", capture=lambda t: "older", now=50.0)
        sessions = self._observe("purge text")
        entry = self.ps._SCAN_CACHE[target]
        self.assertEqual(entry["raw"], "purge text")
        self.assertEqual(self.state.pane_content_hash[target], _hash("purge text"))
        self.assertEqual(self.state.pane_last_activity[target], 2000.0)
        self.assertEqual(entry["changed_at"], 2000.0)
        self.assertEqual(sessions["dev"]["activity"][target], 2000.0)

    def test_purge_refuses_a_pane_whose_root_cannot_be_read(self):
        from shared.agent_identity import ROOT_UNKNOWN
        self.root = ROOT_UNKNOWN
        with self.assertRaises(RuntimeError):
            self._observe("text")

    def test_purge_still_floors_activity_at_the_session_birth(self):
        target = "dev:1.1"
        self.sample(target, "%7", capture=lambda t: "same", now=50.0)
        self.state.pane_last_activity[target] = 10.0  # a stale persisted time
        sessions = self._observe("same")
        self.assertEqual(sessions["dev"]["activity"][target], 1000.0)
        self.assertEqual(self.ps._SCAN_CACHE[target]["changed_at"], 1000.0)


class RootGenerationTests(_Fresh):
    """A pane is (pane_id, root start): a tmux restart reuses `%N`, and the new
    root must not inherit the old one's capture or activity (finding 4344)."""

    def test_a_new_root_with_identical_text_starts_fresh_activity(self):
        self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30, capture=lambda t: "same screen", root="1000")
        got = self.ps.sample("dev:1.1", "%0", MARKER, 200.0, 30, capture=lambda t: "same screen", root="2000")
        self.assertEqual(got.changed_at, 200.0, "the new root inherited the old root's quiet time")
        self.assertEqual(self.ps._SCAN_CACHE["dev:1.1"]["root"], "2000")

    def test_a_new_root_is_recaptured_even_when_the_marker_matches(self):
        self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30, capture=lambda t: "old root", root="1000")
        got = self.ps.sample("dev:1.1", "%0", MARKER, 101.0, 30, capture=lambda t: "new root", root="2000")
        self.assertEqual(got.raw, "new root")

    def test_twin_the_same_root_keeps_its_activity_and_reuses(self):
        self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30, capture=lambda t: "same screen", root="1000")
        captures = []
        got = self.ps.sample("dev:1.1", "%0", MARKER, 101.0, 30,
                             capture=lambda t: captures.append(1) or "x", root="1000")
        self.assertEqual((captures, got.raw, got.changed_at), ([], "same screen", 100.0))

    def test_an_old_root_flight_cannot_repopulate_the_new_root(self):
        target = "dev:1.1"
        started, release = threading.Event(), threading.Event()

        def old_capture(_t):
            started.set()
            self.assertTrue(release.wait(5))
            return "old root text"

        results = {}
        t = threading.Thread(target=lambda: results.setdefault("old", self.ps.sample(
            target, "%0", MARKER, 100.0, 30, capture=old_capture, root="1000")))
        t.start()
        self.assertTrue(started.wait(5))
        results["new"] = self.ps.sample(target, "%0", MARKER, 200.0, 30,
                                        capture=lambda t: "new root text", root="2000")
        release.set()
        t.join(5)
        entry = self.ps._SCAN_CACHE[target]
        self.assertEqual((entry["root"], entry["raw"]), ("2000", "new root text"))
        self.assertEqual(self.state.pane_last_activity[target], 200.0)
        self.assertIsNone(results["old"])

    def test_every_cache_user_passes_the_pane_root(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        import ast
        # Each caller's root source: /poll and purge read it with
        # pane_root_start; the watcher uses the root its observation validated.
        sources = {"routes/poll.py": "pane_root_start(", "shared/purge.py": "pane_root_start(",
                   "routes/delegates.py": "_validated_root("}
        for path in sources:
            with self.subTest(path=path):
                source = (root / path).read_text()
                calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "sample"
                         and isinstance(n.func.value, ast.Name) and n.func.value.id == "pane_scan"]
                self.assertTrue(calls)
                for call in calls:
                    self.assertIn("root", {k.arg for k in call.keywords},
                                  "pane_scan.sample() called without the pane root")
                self.assertIn(sources[path], source)


class UnknownRootTests(_Fresh):
    """A root that could not be read is ROOT_UNKNOWN, and an unknown root
    proves nothing: no capture is published under it and no entry is reused
    for it, so it can never borrow another generation's activity (finding
    4344). None stays "no /proc at all", where the pane id is the identity."""

    def test_an_unknown_root_publishes_nothing_and_takes_no_capture(self):
        from shared.agent_identity import ROOT_UNKNOWN
        self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30, capture=lambda t: "known", root="1000")
        captures = []
        got = self.ps.sample("dev:1.1", "%0", MARKER, 200.0, 30,
                             capture=lambda t: captures.append(1) or "x", root=ROOT_UNKNOWN)
        self.assertIsNone(got)
        self.assertEqual(captures, [])
        entry = self.ps._SCAN_CACHE["dev:1.1"]
        self.assertEqual((entry["root"], entry["raw"], entry["changed_at"]), ("1000", "known", 100.0))

    def test_two_unknown_roots_never_match(self):
        from shared.agent_identity import ROOT_UNKNOWN
        self.assertIsNone(self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30,
                                         capture=lambda t: "x", root=ROOT_UNKNOWN))
        self.assertNotIn("dev:1.1", self.ps._SCAN_CACHE)

    def test_twin_no_proc_compares_by_pane_id(self):
        self.ps.sample("dev:1.1", "%0", MARKER, 100.0, 30, capture=lambda t: "mac", root=None)
        got = self.ps.sample("dev:1.1", "%0", MARKER, 101.0, 30, capture=lambda t: "x", root=None)
        self.assertEqual((got.raw, got.changed_at), ("mac", 100.0))

    def test_pane_root_start_reports_unreadable_as_unknown(self):
        from unittest import mock
        from shared import agent_identity
        self.assertIs(agent_identity.pane_root_start(2 ** 22 + 12345), agent_identity.ROOT_UNKNOWN)
        self.assertIsInstance(agent_identity.pane_root_start(os.getpid()), str)
        with mock.patch.object(agent_identity, "_IS_MAC", True):
            self.assertIsNone(agent_identity.pane_root_start(os.getpid()))
        self.assertIs(agent_identity.pane_root_start(None), agent_identity.ROOT_UNKNOWN)


class NoOtherWriterTests(unittest.TestCase):
    def test_activity_maps_are_written_only_by_pane_scan(self):
        import re
        import subprocess
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        out = subprocess.run(
            ["grep", "-rn", "-E", r"pane_(last_activity|content_hash)(\[[^]]*\]\s*=|\.(setdefault|update|pop|clear)\()",
             "--include=*.py", "routes", "shared", "cli", "serve.py"],
            cwd=root, capture_output=True, text=True,
        ).stdout.splitlines()
        offenders = [
            line for line in out
            if not line.startswith("shared/pane_scan.py")
            # state.py owns persistence (load) and the user-input touch.
            and not re.match(r"shared/state\.py:\d+:\s+pane_(content_hash|last_activity)\.update\(", line)
            and not re.match(r"shared/state\.py:\d+:\s+pane_last_activity\[target\] = time\.time\(\)", line)
        ]
        self.assertEqual(offenders, [], "activity maps written outside shared/pane_scan.py")


if __name__ == "__main__":
    unittest.main()
