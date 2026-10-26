"""The scanner's records, read back: state.autoyes_disposition and the countdown's pane_id.

Delegate classification (spec 7185 §4.5) decides whether Auto-Yes OWNS a prompt
from the scanner's live records for the SAME request — never from how long the
prompt has been visible. So every tick that sees a present prompt publishes a
disposition: countdown, fired, withheld, probe-failed, cancelled, vetoed,
recent, or the delivery failure string, with the consecutive failure count for
that request. Reporting only: no branch of the tick changes, and every
test_autoyes_* module passes unmodified.

Real scanner ticks through the test_autoyes_failsafe harness, with a pane_id in
the list-panes reply.

Run: .venv/bin/python3 -m unittest tests.test_autoyes_disposition
"""

import unittest
from unittest.mock import patch

import shared.state as state
from routes import autoyes
from shared.tmux import DeliveryResult
from tests.test_autoyes_failsafe import (
    CLAUDE_YNA, CODEX, CODEX_THEN_DOWNGRADE, IDLE, PANE_ID, _IDENTITY, _UNSET, _ScannerCase,
    _completed,
)

# The approval with its header scrolled out of view: the scanner withholds.
CLIPPED_CODEX = CODEX.split("\n", 1)[1]


class _DispositionCase(_ScannerCase):
    def setUp(self):
        super().setUp()
        p = patch.dict(state.autoyes_disposition, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def tick(self, tail, now, *, kind="claude", identity=_IDENTITY, recapture=_UNSET,
             delivery="delivered", pane_id=PANE_ID):
        """The harness tick, with #{pane_id} as the sixth list-panes field."""
        again = tail if recapture is _UNSET else recapture

        def run(argv, **_kwargs):
            if "list-panes" in argv:
                return _completed(f"{self.SESSION}\t0\t0\t123\t{kind}\t{pane_id}\n")
            if "capture-pane" in argv and pane_id in argv:
                if again is None:
                    return _completed("", returncode=1)
                return _completed(again + "\n")
            if "capture-pane" in argv:
                return _completed(tail + "\n")
            raise AssertionError(argv)

        with patch("routes.autoyes.subprocess.run", run), patch(
            "routes.autoyes.time.time", return_value=now
        ), patch("routes.autoyes.resolve_process", return_value=kind), patch(
            "routes.autoyes.refine_with_content", return_value=kind
        ), patch(
            "routes.autoyes.state.autoyes_enabled_for", return_value=(True, self.source)
        ), patch("routes.autoyes.state.get_setting", side_effect=self.settings), patch(
            "routes.autoyes.state.get_project_setting",
            side_effect=lambda _s, _sec, key: self.project[key],
        ), patch(
            "routes.autoyes.expected_target_identity", return_value=identity
        ), patch(
            "routes.autoyes.broadcast_autoyes_event",
            side_effect=lambda *e: self.events.append(e),
        ), patch(
            "routes.autoyes.generation_bound_delivery",
            return_value=DeliveryResult(delivery),
        ) as deliver:
            autoyes._autoyes_scan_tick()
        return deliver

    def disposition(self):
        return state.autoyes_disposition.get(self.TARGET)


class DispositionTests(_DispositionCase):
    def test_an_ordinary_approval_gets_a_countdown_with_the_pane_id(self):
        self.tick(CLAUDE_YNA, now=100.0)
        cd = self.countdown()
        self.assertEqual(cd["pane_id"], PANE_ID)
        d = self.disposition()
        self.assertEqual(d["status"], "countdown")
        self.assertEqual(d["pane_id"], PANE_ID)
        self.assertEqual(d["request_id"], cd["prompt_hash"])
        self.assertEqual(d["at"], 100.0)
        self.assertEqual(d["failed_deliveries"], 0)
        self.assertIsNone(d["probe_failing_since"])
        # Fires as today.
        self.tick(CLAUDE_YNA, now=105.0).assert_called_once()
        self.assertEqual(self.disposition()["status"], "fired")
        self.assertEqual(self.disposition()["at"], 105.0)

    def test_a_clipped_header_is_withheld(self):
        self.tick(CLIPPED_CODEX, now=100.0, kind="codex")
        self.assertIsNone(self.countdown())
        d = self.disposition()
        self.assertEqual(d["status"], "withheld")
        self.assertIsNone(d["request_id"])
        self.assertEqual(d["pane_id"], PANE_ID)

    def test_a_failed_probe_records_when_it_started_failing_and_keeps_the_countdown(self):
        self.tick(CLAUDE_YNA, now=100.0)
        request = self.countdown()["prompt_hash"]
        self.tick(CLAUDE_YNA, now=101.0, identity=None)
        d = self.disposition()
        self.assertEqual(d["status"], "probe-failed")
        self.assertEqual(d["request_id"], request)
        self.assertEqual(d["probe_failing_since"], 101.0)
        self.assertIsNotNone(self.countdown(), "the countdown is left exactly as it was")
        self.tick(CLAUDE_YNA, now=102.0, identity=None)
        self.assertEqual(self.disposition()["probe_failing_since"], 101.0)
        self.assertEqual(self.disposition()["at"], 102.0)
        # The probe recovers: back to the countdown, the failing-since gone.
        self.tick(CLAUDE_YNA, now=103.0)
        self.assertEqual(self.disposition()["status"], "countdown")
        self.assertIsNone(self.disposition()["probe_failing_since"])

    def test_a_cancel_is_recorded(self):
        self.tick(CLAUDE_YNA, now=100.0)
        autoyes.cancel_countdown(self.TARGET)
        self.tick(CLAUDE_YNA, now=101.0).assert_not_called()
        self.assertEqual(self.disposition()["status"], "cancelled")
        self.assertEqual(self.disposition()["at"], 101.0)

    def test_luna_or_a_downgrade_menu_is_vetoed(self):
        self.tick(CODEX_THEN_DOWNGRADE, now=100.0, kind="codex")
        self.assertIsNone(self.countdown())
        self.assertEqual(self.disposition()["status"], "vetoed")

    def test_a_just_answered_prompt_is_recent(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0).assert_called_once()
        self.tick(CLAUDE_YNA, now=106.0).assert_not_called()
        self.assertEqual(self.disposition()["status"], "recent")

    def test_no_prompt_leaves_the_last_disposition_and_a_dead_pane_prunes_it(self):
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(IDLE, now=101.0)
        self.assertEqual(self.disposition()["status"], "countdown")
        state.autoyes_disposition["gone:0.0"] = {"status": "countdown"}
        self.tick(IDLE, now=102.0)
        self.assertNotIn("gone:0.0", state.autoyes_disposition)

    def test_the_judges_timeline_three_failures_then_success(self):
        """Delay 30, delivery failing three times: countdowns at 0, 31, 62 and
        93, success at 123; failed_deliveries 1 → 2 → 3 → 0 (§4.5 table)."""
        self.project["delay"] = 30
        self.tick(CLAUDE_YNA, now=0.0)
        self.assertEqual(self.countdown()["deadline"], 30.0)
        request = self.countdown()["prompt_hash"]
        for fail_at, count, next_deadline in ((30.0, 1, 61.0), (61.0, 2, 92.0), (92.0, 3, 123.0)):
            self.tick(CLAUDE_YNA, now=fail_at, delivery="pane_gone").assert_called_once()
            d = self.disposition()
            self.assertEqual((d["status"], d["failed_deliveries"], d["at"]), ("pane_gone", count, fail_at))
            self.assertEqual(d["request_id"], request)
            self.assertIsNone(self.countdown(), "a failed delivery drops the countdown as today")
            self.tick(CLAUDE_YNA, now=fail_at + 1)
            self.assertEqual(self.countdown()["deadline"], next_deadline)
            self.assertEqual(self.countdown()["pane_id"], PANE_ID)
            d = self.disposition()
            self.assertEqual(d["status"], "countdown")
            self.assertEqual(d["failed_deliveries"], count, "the count survives into the new countdown")
        self.tick(CLAUDE_YNA, now=100.0).assert_not_called()
        self.assertEqual(self.disposition()["status"], "countdown")
        self.tick(CLAUDE_YNA, now=123.0).assert_called_once()
        d = self.disposition()
        self.assertEqual((d["status"], d["failed_deliveries"]), ("fired", 0))
        self.assertEqual(state.autoyes_answered[self.TARGET][0], request)

    def test_a_new_request_resets_the_failure_count(self):
        self.tick(CLAUDE_YNA, now=0.0)
        self.tick(CLAUDE_YNA, now=5.0, delivery="pane_gone")
        self.assertEqual(self.disposition()["failed_deliveries"], 1)
        self.tick(IDLE, now=6.0)
        self.tick(IDLE, now=7.0)
        from tests.test_autoyes_failsafe import OTHER_YNA
        self.tick(OTHER_YNA, now=8.0)
        self.assertEqual(self.disposition()["failed_deliveries"], 0)


class ReplacementPaneTests(_DispositionCase):
    """A new pane at the same target starts its own history, even with the
    same prompt text (same request_id): counters and timers carry over only
    when BOTH the pane_id and the request_id match (finding 4335)."""

    def test_a_replacement_pane_starts_its_own_probe_failure_timer(self):
        self.tick(CLAUDE_YNA, now=100.0, identity=None)
        self.assertEqual(self.disposition()["probe_failing_since"], 100.0)
        self.tick(CLAUDE_YNA, now=200.0, identity=None, pane_id="%999")
        d = self.disposition()
        self.assertEqual((d["pane_id"], d["status"]), ("%999", "probe-failed"))
        self.assertEqual(d["probe_failing_since"], 200.0)

    def test_the_same_pane_keeps_its_probe_failure_timer(self):
        self.tick(CLAUDE_YNA, now=100.0, identity=None)
        self.tick(CLAUDE_YNA, now=110.0, identity=None)
        self.assertEqual(self.disposition()["probe_failing_since"], 100.0)

    def test_a_replacement_pane_starts_its_own_failure_count(self):
        self.tick(CLAUDE_YNA, now=0.0)
        self.tick(CLAUDE_YNA, now=5.0, delivery="pane_gone").assert_called_once()
        self.assertEqual(self.disposition()["failed_deliveries"], 1)
        self.tick(CLAUDE_YNA, now=6.0, pane_id="%999")
        d = self.disposition()
        self.assertEqual(d["pane_id"], "%999")
        self.assertEqual(d["failed_deliveries"], 0)

    def test_a_replacement_pane_failing_counts_from_one(self):
        self.tick(CLAUDE_YNA, now=0.0)
        self.tick(CLAUDE_YNA, now=5.0, delivery="pane_gone")
        self.tick(CLAUDE_YNA, now=6.0)
        self.tick(CLAUDE_YNA, now=11.0, delivery="pane_gone")
        self.assertEqual(self.disposition()["failed_deliveries"], 2)
        self.tick(CLAUDE_YNA, now=12.0, pane_id="%999")
        self.tick(CLAUDE_YNA, now=17.0, pane_id="%999", delivery="pane_gone")
        d = self.disposition()
        self.assertEqual((d["pane_id"], d["failed_deliveries"]), ("%999", 1))

    def test_the_same_pane_keeps_its_failure_count(self):
        self.tick(CLAUDE_YNA, now=0.0)
        self.tick(CLAUDE_YNA, now=5.0, delivery="pane_gone")
        self.tick(CLAUDE_YNA, now=6.0)
        self.assertEqual(self.disposition()["failed_deliveries"], 1)


class ReportingOnlyTests(unittest.TestCase):
    def test_the_disposition_map_is_declared_and_pruned_with_the_others(self):
        self.assertIsInstance(state.autoyes_disposition, dict)

    def test_a_rename_carries_the_disposition(self):
        with patch.dict(state.autoyes_disposition, clear=True), \
                patch.dict(state.autoyes_countdowns, clear=True), \
                patch.dict(state.autoyes_sessions, clear=True):
            state.autoyes_disposition["work:0.0"] = {"status": "countdown"}
            state.autoyes_disposition["work2:0.0"] = {"status": "fired"}
            autoyes.rename_autoyes_session("work", "done")
            self.assertEqual(state.autoyes_disposition["done:0.0"]["status"], "countdown")
            self.assertEqual(state.autoyes_disposition["work2:0.0"]["status"], "fired")
            self.assertNotIn("work:0.0", state.autoyes_disposition)


if __name__ == "__main__":
    unittest.main()
