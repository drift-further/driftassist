"""shared/delegate_state.py — classify(sample, memory), pure (spec 7185 §4.5–§4.9, §10.1).

Every §10.1 row is a case here with its twin. Samples are data: `classify`
runs with no tmux and no server. Rows that are about what the screen shows
build the sample's derived fields with shared/delegate_signals.py (chrome,
report line, stall counter) and routes.autoyes.observe_attention, the same
helpers the phase 6 adapter uses; rows about process identity go through the
phase 4 fake /proc trees and `identity_of`.

Run: .venv/bin/python3 -m unittest tests.test_delegate_state
"""

import ast
import unittest
from pathlib import Path
from unittest import mock

from routes.autoyes import observe_attention
from shared import agent_identity, delegate_signals
from shared.delegate_state import AGENT_KINDS, classify, identity_of
from tests.test_process_observation import (
    DENIED, GONE, FakeProc, agent_free_shell, judge_denied_comm, readable_claude, unrelated_stat,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "delegates"

GEN = ("%7", "5000")
PRESENT = {"status": "present", "kind": "claude", "agent_pid": 20, "agent_start": "5100", "errors": []}
ABSENT = {"status": "absent", "kind": None, "agent_pid": None, "agent_start": None, "errors": []}
OPAQUE = {"status": "opaque", "kind": None, "agent_pid": None, "agent_start": None, "errors": []}
INCOMPLETE = {"status": "incomplete", "kind": None, "agent_pid": None, "agent_start": None,
              "errors": [(20, "/proc/20/comm", "EACCES")]}
ANSWERABLE = {"kind": "answerable", "answer": ("numbered-yes", "1", True, "Run curl?"),
              "summary": "Run curl?", "key": "answerable:aaaa"}
CLAUDE_COMPOSER = (
    "─" * 76 + "\n❯ \n" + "─" * 76 + "\n  ░░░░░░░░░░ 0.0% (0k/800k) │ 0m   ● high · /effort"
)
CODEX_COMPOSER = "› Ask Codex to do anything\n  GPT-6-Astra medium · /tmp/x · Context 97% left"


def fixture(name):
    return (FIXTURES / f"{name}.txt").read_text().rstrip("\n")


def present(kind="claude", pid=20, start="5100"):
    return {**PRESENT, "kind": kind, "agent_pid": pid, "agent_start": start}


def sample(now, **kw):
    s = {
        "now": now, "pane_id": "%7", "session_id": "$1", "target": "del_x:1.1",
        "generation": GEN, "generation_born": -1000.0, "pane_dead": False,
        "pane_title": "", "pane_width": 120, "tail": "", "content_rev": 1,
        "changed_at": -1000.0, "identity": present(), "content_kind": "claude",
        "attention": None, "request_id": None,
        "autoyes": {"armed": False, "countdown": None, "answered": None, "disposition": None},
        "chrome": {"working": False, "turn_done": False, "title_busy": False},
        "claude_status": None, "report_line": None, "stall": None,
        "capture_ok": True, "listing_ok": True,
    }
    s.update(kw)
    return s


def screen(now, text, kind="codex", title="", **kw):
    """A sample whose derived fields come from the screen text, as the adapter builds them."""
    att = observe_attention(text, kind, 120)
    return sample(
        now, tail=text, content_kind=kind, identity=kw.pop("identity", present(kind)),
        attention=att, chrome=delegate_signals.chrome(text, title),
        report_line=delegate_signals.report_line(text), stall=delegate_signals.stall_counter(text),
        pane_title=title, **kw,
    )


def armed(countdown=None, answered=None, disposition=None):
    return {"armed": True, "countdown": countdown, "answered": answered, "disposition": disposition}


def countdown(request="R", pane="%7", deadline=30.0):
    return {"prompt_hash": request, "pane_id": pane, "deadline": deadline}


def disposition(status, at, request="R", pane="%7", failed=0, since=None):
    return {"pane_id": pane, "request_id": request, "status": status, "at": at,
            "failed_deliveries": failed, "probe_failing_since": since}


class Run:
    """Feed samples in order, keep the memory, remember every result."""

    def __init__(self, memory=None):
        self.memory = memory
        self.results = []

    def __call__(self, s):
        result, self.memory = classify(s, self.memory)
        self.results.append(result)
        return result

    def alerts(self):
        return [r["alert"] for r in self.results if r["alert"]]

    def published(self):
        return [(r["state"], r["reason"]) for r in self.results]


def over(start, stop, make, step=1):
    t = start
    while t <= stop:
        yield make(t)
        t += step


class AttentionRows(unittest.TestCase):
    def test_waiting_question_for_a_live_ask_user_question_with_autoyes_on(self):
        run = Run()
        for s in over(0, 8, lambda t: screen(t, fixture("claude-ask"), "claude",
                                             autoyes=armed(), request_id=None)):
            r = run(s)
        self.assertEqual((r["state"], r["reason"]), ("waiting", "question"))

    def test_twin_the_same_menu_quoted_above_an_idle_composer_is_not_waiting(self):
        quoted = fixture("claude-ask") + "\n\nsome transcript\n" + CLAUDE_COMPOSER
        run = Run()
        for s in over(0, 60, lambda t: screen(t, quoted, "claude", autoyes=armed()), step=5):
            r = run(s)
            self.assertNotEqual(r["state"], "waiting")

    def test_waiting_menu_for_the_live_faster_model_menu(self):
        run = Run()
        for s in over(0, 8, lambda t: screen(t, fixture("codex-faster-model"), "codex")):
            r = run(s)
        self.assertEqual((r["state"], r["reason"]), ("waiting", "menu"))

    def test_twin_the_faster_model_text_printed_above_the_codex_composer(self):
        quoted = fixture("codex-faster-model") + "\n" + CODEX_COMPOSER
        run = Run()
        for s in over(0, 60, lambda t: screen(t, quoted, "codex"), step=5):
            self.assertNotEqual(run(s)["state"], "waiting")

    def test_working_under_a_numbered_prose_list_gotcha_528(self):
        text = ("Plan:\n  1. Read the code\n  2. Write the test\n  3. Fix it\n"
                "• Working (12m 03s • esc to interrupt)")
        r = Run()(screen(0, text, "codex"))
        self.assertEqual(r["state"], "working")

    def test_twin_the_same_list_above_a_live_footer_is_a_menu(self):
        text = ("Pick one:\n  1. Read the code\n  2. Write the test\n"
                "Press enter to confirm or esc to cancel")
        run = Run()
        for s in over(0, 8, lambda t: screen(t, text, "codex")):
            r = run(s)
        self.assertEqual((r["state"], r["reason"]), ("waiting", "menu"))


class OwnershipRows(unittest.TestCase):
    def waiting_after_hold(self, make, hold_from=0):
        run = Run()
        for s in over(0, hold_from + 7, make):
            self.assertNotEqual(run(s)["state"], "waiting", s["now"])
        r = run(make(hold_from + 8))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "approval"))
        return r

    def never_waiting(self, make, until=60):
        run = Run()
        for s in over(0, until, make):
            r = run(s)
            self.assertEqual((r["state"], r["reason"]), ("working", "answering"), s["now"])

    def test_waiting_for_a_clipped_header_with_no_request_id(self):
        self.waiting_after_hold(lambda t: sample(
            t, attention=ANSWERABLE, request_id=None,
            autoyes=armed(disposition=disposition("withheld", t, request=None))))

    def test_twin_a_live_countdown_for_the_request_owns_it_at_both_delays(self):
        for delay in (0.1, 30):
            with self.subTest(delay=delay):
                self.never_waiting(lambda t: sample(
                    t, attention=ANSWERABLE, request_id="R",
                    autoyes=armed(countdown=countdown(deadline=t + delay))))

    def test_waiting_when_the_countdown_is_for_another_request_or_pane(self):
        for cd in (countdown(request="OTHER", deadline=1e9), countdown(pane="%9", deadline=1e9)):
            with self.subTest(countdown=cd):
                self.waiting_after_hold(lambda t: sample(
                    t, attention=ANSWERABLE, request_id="R", autoyes=armed(countdown=cd)))

    def test_twin_a_countdown_matching_both_owns_it(self):
        self.never_waiting(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R", autoyes=armed(countdown=countdown(deadline=1e9))))

    def test_a_countdown_owns_it_for_three_scanner_ticks_past_its_deadline(self):
        for late, owned in ((3, True), (4, False)):
            with self.subTest(late=late):
                r = Run()(sample(30 + late, attention=ANSWERABLE, request_id="R",
                                 autoyes=armed(countdown=countdown(deadline=30.0))))
                self.assertEqual(r["candidate"], None if owned else "waiting")

    def timeline_autoyes(self, t):
        """The scanner's records at t, the round-3 judge's real run (delay 30)."""
        windows = ((0, 30, 30.0), (31, 61, 61.0), (62, 92, 92.0), (93, 123, 123.0))
        for start, end, deadline in windows:
            if start <= t < end:
                failed = {30.0: 0, 61.0: 1, 92.0: 2, 123.0: 3}[deadline]
                return armed(countdown=countdown(deadline=deadline),
                             disposition=disposition("countdown", t, failed=failed))
        if t in (30, 61, 92):
            return armed(disposition=disposition("pane_gone", t, failed={30: 1, 61: 2, 92: 3}[t]))
        return armed(answered=("R", 123.0), disposition=disposition("fired", 123.0))

    def test_the_ownership_timeline_t0_to_123(self):
        run = Run()
        for t in range(0, 124):
            r = run(sample(t, attention=ANSWERABLE, request_id="R", autoyes=self.timeline_autoyes(t)))
            self.assertEqual((r["state"], r["reason"]), ("working", "answering"), t)
            if t == 92:
                self.assertEqual(r["candidate"], "waiting", "the gap after the 3rd failure is unowned")
            if t == 93:
                self.assertIsNone(r["candidate"], "the live countdown resets the candidate")
        self.assertEqual(run.alerts(), [])

    def test_twin_no_scanner_record_is_unowned_at_5_and_waiting_at_13(self):
        run = Run()
        for t in range(0, 13):
            r = run(sample(t, attention=ANSWERABLE, request_id="R", autoyes=armed()))
            self.assertEqual(r["state"], "working", t)
            self.assertEqual(r["candidate"], "waiting" if t >= 5 else None, t)
        r = run(sample(13, attention=ANSWERABLE, request_id="R", autoyes=armed()))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "approval"))
        self.assertIn("no Auto-Yes countdown", r["detail"])
        self.assertEqual(len(run.alerts()), 1)

    def test_twin_a_countdown_written_at_4s_keeps_it_working(self):
        self.never_waiting(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R",
            autoyes=armed(countdown=countdown(deadline=1e9) if t >= 4 else None)))

    def test_waiting_when_the_probe_has_failed_for_16s(self):
        self.waiting_after_hold(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R",
            autoyes=armed(disposition=disposition("probe-failed", t, since=0.0))), hold_from=16)

    def test_twin_probe_failing_10s_then_a_countdown_never_publishes_waiting(self):
        self.never_waiting(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R",
            autoyes=armed(countdown=countdown(deadline=1e9) if t > 10 else None,
                          disposition=disposition("probe-failed", t, since=0.0) if t <= 10 else None)))

    def test_waiting_for_a_human_cancelled_countdown(self):
        r = self.waiting_after_hold(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R",
            autoyes=armed(disposition=disposition("cancelled", t))))
        self.assertIn("cancelled", r["detail"])

    def test_twin_uncancelled_armed_is_working(self):
        self.never_waiting(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R",
            autoyes=armed(countdown=countdown(deadline=1e9), disposition=disposition("countdown", t))))

    def test_waiting_for_a_vetoed_approval_luna_on_screen(self):
        r = self.waiting_after_hold(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R", content_kind="codex", identity=present("codex"),
            autoyes=armed(disposition=disposition("vetoed", t, request=None))))
        self.assertIn("luna", r["detail"])

    def test_twin_no_luna_and_owned_is_working(self):
        self.never_waiting(lambda t: sample(
            t, attention=ANSWERABLE, request_id="R", content_kind="codex", identity=present("codex"),
            autoyes=armed(countdown=countdown(deadline=1e9))))

    def test_a_failed_delivery_gap_is_owned_until_the_count_reaches_3(self):
        for count, owned in ((1, True), (2, True), (3, False)):
            with self.subTest(failed=count):
                r = Run()(sample(30, attention=ANSWERABLE, request_id="R",
                                 autoyes=armed(disposition=disposition("pane_gone", 29.0, failed=count))))
                self.assertEqual(r["candidate"], None if owned else "waiting")

    def test_unarmed_is_unowned(self):
        run = Run()
        for s in over(0, 8, lambda t: sample(t, attention=ANSWERABLE, request_id="R")):
            r = run(s)
        self.assertEqual((r["state"], r["reason"]), ("waiting", "approval"))


class StallRows(unittest.TestCase):
    RECONNECT = "• Reconnecting... {}/5\n› Ask Codex to do anything"

    def test_waiting_stalled_when_the_counter_is_unchanged_for_stall_sec(self):
        run = Run()
        for t in range(0, 308, 4):
            self.assertNotEqual(run(screen(t, self.RECONNECT.format(2), changed_at=0.0))["state"], "waiting", t)
        r = run(screen(308, self.RECONNECT.format(2), changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "stalled"))

    def test_twin_an_advancing_counter_is_not_stalled(self):
        run = Run()
        for t in range(0, 900, 5):
            n = 2 + t // 200
            self.assertNotEqual(run(screen(t, self.RECONNECT.format(n), changed_at=0.0))["reason"], "stalled")

    def test_twin_the_reconnect_line_gone_and_working_back_resets_it(self):
        run = Run()
        for t in range(0, 250, 5):
            run(screen(t, self.RECONNECT.format(2), changed_at=0.0))
        for t in range(250, 600, 5):
            r = run(screen(t, "• Working (1m 02s • esc to interrupt)", changed_at=0.0))
            self.assertEqual(r["state"], "working")
        r = run(screen(600, self.RECONNECT.format(2), changed_at=0.0))
        self.assertNotEqual(r["state"], "waiting")


class FinishedRows(unittest.TestCase):
    CODEX_DONE = "Done.\n─ Worked for 1h 19m ─\n" + CODEX_COMPOSER
    CLAUDE_DONE = "All done.\n\n✻ Crunched for 11m\n\n" + CLAUDE_COMPOSER

    def test_finished_turn_done_for_codex_after_45s_quiet(self):
        r = Run()(screen(45, self.CODEX_DONE, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("finished", "turn done"))

    def test_twin_44s_quiet_is_still_working(self):
        r = Run()(screen(44, self.CODEX_DONE, changed_at=0.0))
        self.assertEqual(r["state"], "working")

    def test_finished_turn_done_for_claude(self):
        r = Run()(screen(45, self.CLAUDE_DONE, "claude", changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("finished", "turn done"))

    def test_twin_claude_session_file_busy_is_working(self):
        r = Run()(screen(45, self.CLAUDE_DONE, "claude", changed_at=0.0, claude_status="busy"))
        self.assertEqual(r["state"], "working")

    def test_plain_quiet_is_finished_quiet(self):
        r = Run()(screen(45, "some output\n" + CODEX_COMPOSER, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("finished", "quiet"))

    def test_a_genuine_final_report_sets_only_the_detail(self):
        text = "BLOCKED — need the path\n─ Worked for 2m ─\n" + CODEX_COMPOSER
        r = Run()(screen(45, text, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("finished", "turn done"))
        self.assertEqual(r["detail"], "last report line: BLOCKED — need the path")

    def test_twin_the_same_row_as_tool_output_mid_turn_is_still_finished_never_waiting(self):
        run = Run()
        mid = "$ cat brief.md\nBLOCKED — need the path\n• Working (0m 12s • esc to interrupt)"
        for t in range(0, 30, 5):
            self.assertEqual(run(screen(t, mid, changed_at=float(t)))["state"], "working")
        end = "$ cat brief.md\nBLOCKED — need the path\nHere is the answer.\n─ Worked for 2m ─\n" + CODEX_COMPOSER
        for t in range(30, 80, 5):
            r = run(screen(t, end, changed_at=30.0))
            self.assertNotEqual(r["state"], "waiting")
        self.assertEqual(r["state"], "finished")

    def test_no_report_row_leaves_the_detail_empty(self):
        r = Run()(screen(45, self.CODEX_DONE, changed_at=0.0))
        self.assertEqual(r["detail"], "")

    def test_twin_a_brief_reply_template_is_not_a_report_row(self):
        text = "Reply: DONE — <one line> | BLOCKED — <what>\n─ Worked for 2m ─\n" + CODEX_COMPOSER
        r = Run()(screen(45, text, changed_at=0.0))
        self.assertEqual(r["state"], "finished")
        self.assertEqual(r["detail"], "")


class DeadRows(unittest.TestCase):
    def test_dead_exited_after_two_complete_absent_samples_5s_apart(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        kept = run(sample(5, identity=ABSENT, changed_at=0.0))
        self.assertEqual(kept["state"], "working", "one absent sample keeps the published state")
        r = run(sample(10, identity=ABSENT, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("dead", "exited"))
        self.assertEqual(len(run.alerts()), 1)

    def test_twin_absent_on_one_sample_only_keeps_the_state(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        run(sample(5, identity=ABSENT, changed_at=0.0))
        r = run(sample(10, changed_at=0.0))
        self.assertEqual(r["state"], "working")

    def test_two_absent_samples_closer_than_5s_are_not_yet_dead(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        run(sample(5, identity=ABSENT, changed_at=0.0))
        self.assertNotEqual(run(sample(7, identity=ABSENT, changed_at=0.0))["state"], "dead")
        self.assertEqual(run(sample(10, identity=ABSENT, changed_at=0.0))["state"], "dead")

    def test_a_reused_pid_reads_as_absent_then_dead(self):
        # Phase 4 drops an agent whose start time changed, so the identity is absent.
        procs = readable_claude()
        FakeProc(procs).install(self)
        run = Run()
        run(sample(0, identity=identity_of(agent_identity.observe_process_tree(10)), changed_at=0.0))
        # The agent exits and its pid is reused by another process.
        procs[20] = {"comm": "vim", "argv": ["vim"], "ppid": 10, "start": "9999"}
        run(sample(5, identity=identity_of(agent_identity.observe_process_tree(10)), changed_at=0.0))
        r = run(sample(10, identity=identity_of(agent_identity.observe_process_tree(10)), changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("dead", "exited"))

    def test_twin_the_same_pid_with_a_matching_start_is_present(self):
        FakeProc(readable_claude()).install(self)
        identity = identity_of(agent_identity.observe_process_tree(10))
        self.assertEqual(identity["status"], "present")
        r = Run()(sample(0, identity=identity, changed_at=0.0))
        self.assertEqual(r["state"], "working")

    def test_dead_pane_dead(self):
        r = Run()(sample(0, pane_dead=True))
        self.assertEqual((r["state"], r["reason"]), ("dead", "pane-dead"))

    def test_twin_a_live_pane_with_the_same_tail_is_not_dead(self):
        r = Run()(sample(0, pane_dead=False))
        self.assertNotEqual(r["state"], "dead")

    def test_dead_no_agent_when_absent_twice_and_121s_old(self):
        run = Run()
        run(sample(121, identity=ABSENT, generation_born=0.0))
        r = run(sample(126, identity=ABSENT, generation_born=0.0))
        self.assertEqual((r["state"], r["reason"]), ("dead", "no-agent"))

    def test_twin_30s_old_is_starting_shown_as_working_and_never_alerted(self):
        run = Run()
        run(sample(25, identity=ABSENT, generation_born=0.0))
        r = run(sample(30, identity=ABSENT, generation_born=0.0))
        self.assertEqual((r["state"], r["reason"]), ("working", "starting"))
        self.assertEqual(run.alerts(), [])

    def test_a_readable_agent_free_shell_under_an_old_pane_is_dead(self):
        FakeProc(agent_free_shell()).install(self)
        run = Run()
        for t in (200, 205):
            r = run(sample(t, identity=identity_of(agent_identity.observe_process_tree(10)),
                           generation_born=0.0))
        self.assertEqual((r["state"], r["reason"]), ("dead", "no-agent"))

    def test_twin_the_judges_denied_comm_tree_is_incomplete_kept_and_never_dead(self):
        FakeProc(judge_denied_comm()).install(self)
        run = Run()
        run(sample(0, changed_at=0.0))
        for t in (100, 105, 110):
            r = run(sample(t, identity=identity_of(agent_identity.observe_process_tree(10)),
                           generation_born=0.0, changed_at=0.0))
            self.assertEqual(r["state"], "working", "the previous state is kept")
            self.assertEqual(r["health"], "unknown")
        self.assertEqual(run.alerts(), [])

    def test_an_unrelated_stat_permission_error_keeps_the_state(self):
        FakeProc(unrelated_stat(DENIED)).install(self)
        identity = identity_of(agent_identity.observe_process_tree(10))
        self.assertEqual(identity["status"], "incomplete")
        run = Run()
        run(sample(0, changed_at=0.0))
        r = run(sample(100, identity=identity, generation_born=0.0))
        self.assertEqual((r["state"], r["health"]), ("working", "unknown"))

    def test_twin_an_unrelated_stat_file_not_found_is_complete(self):
        FakeProc(unrelated_stat(GONE)).install(self)
        self.assertEqual(identity_of(agent_identity.observe_process_tree(10))["status"], "absent")

    def test_an_absent_run_broken_by_an_incomplete_sample_starts_again(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        run(sample(5, identity=ABSENT, changed_at=0.0))
        run(sample(10, identity=INCOMPLETE, changed_at=0.0))
        self.assertNotEqual(run(sample(15, identity=ABSENT, changed_at=0.0))["state"], "dead")


class PlatformAndHealthRows(unittest.TestCase):
    def test_macos_every_observation_incomplete_unknown_no_alert_never_dead(self):
        FakeProc(readable_claude()).install(self)
        with mock.patch.object(agent_identity, "_IS_MAC", True):
            identity = identity_of(agent_identity.observe_process_tree(10))
        self.assertEqual(identity["status"], "incomplete")
        run = Run()
        for t in range(0, 300, 5):
            r = run(screen(t, fixture("codex-approval"), "codex", identity=identity,
                           autoyes=armed(), generation_born=0.0))
            self.assertEqual((r["state"], r["health"]), ("unknown", "unknown"))
        self.assertEqual(run.alerts(), [])

    def test_twin_the_same_capture_on_linux_complete_is_waiting(self):
        run = Run()
        for t in range(0, 9):
            r = run(screen(t, fixture("codex-approval"), "codex", autoyes=armed()))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "approval"))

    def test_capture_failure_keeps_the_state_and_marks_it_stale(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        r = run(sample(5, capture_ok=False, attention=ANSWERABLE))
        self.assertEqual((r["state"], r["health"], r["observed_at"]), ("working", "stale", 0))

    def test_twin_a_capture_that_works_reclassifies_with_health_ok(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        r = run(sample(50, changed_at=0.0))
        self.assertEqual((r["state"], r["health"], r["observed_at"]), ("finished", "ok", 50))

    def test_a_failed_listing_is_also_stale(self):
        run = Run()
        run(sample(0, changed_at=0.0))
        self.assertEqual(run(sample(5, listing_ok=False))["health"], "stale")


class OpaqueRows(unittest.TestCase):
    def test_ssh_wrapped_codex_with_codex_chrome_is_working_then_finished_never_dead(self):
        run = Run()
        r = run(screen(0, "• Working (0m 05s • esc to interrupt)", identity=OPAQUE, changed_at=0.0))
        self.assertEqual(r["state"], "working")
        for t in range(5, 300, 5):
            r = run(screen(t, "Done.\n" + CODEX_COMPOSER, identity=OPAQUE, changed_at=0.0))
            self.assertNotEqual(r["state"], "dead")
        self.assertEqual(r["state"], "finished")

    def test_twin_an_opaque_pane_naming_no_agent_is_unknown_and_never_alerts(self):
        run = Run()
        for t in range(0, 100, 5):
            r = run(sample(t, identity=OPAQUE, content_kind="shell"))
            self.assertEqual(r["state"], "unknown")
        self.assertEqual(run.alerts(), [])


class EpisodeRows(unittest.TestCase):
    WORKING = {"working": True, "turn_done": False, "title_busy": False}
    IDLE = {"working": False, "turn_done": True, "title_busy": False}

    def test_dead_content_churn_keeps_one_publication_and_key(self):
        for identity, pane_dead in ((ABSENT, False), (present("gemini"), True)):
            with self.subTest(identity=identity, pane_dead=pane_dead):
                run = Run()
                results = [run(sample(t * 5, identity=identity, pane_dead=pane_dead,
                                      content_kind="gemini", content_rev=t,
                                      changed_at=t * 5, chrome=self.WORKING))
                           for t in range(30)]
                dead = [r for r in results if r["state"] == "dead"]
                self.assertTrue(dead)
                self.assertEqual(len({r["episode"] for r in results}), 1)
                self.assertEqual(len({r["key"] for r in dead}), 1)
                self.assertEqual(len({r["since"] for r in dead}), 1)
                self.assertEqual(len(run.alerts()), 1)

    def test_live_unsignalled_agent_counts_each_turn_once(self):
        run = Run()
        kw = dict(identity=present("gemini"), content_kind="gemini")
        first = run(sample(0, **kw))
        for turn in range(2):
            start = 100 + turn * 100
            for step in range(1, 7):
                result = run(sample(start + step * 5, content_rev=turn * 6 + step + 1,
                                    changed_at=start + step * 5, **kw))
                self.assertEqual(result["episode"], first["episode"] + turn + (step >= 3))
                if step == 3:
                    self.assertEqual(run.memory["changes"], 0)
            done = run(sample(start + 80, content_rev=turn * 6 + 7,
                              changed_at=start + 30, **kw))
            self.assertEqual(done["state"], "finished")
            self.assertIsNotNone(done["alert"])
        self.assertEqual(len(run.alerts()), 3)

    def finish(self, run, t, **kw):
        return run(sample(t, chrome=self.IDLE, changed_at=t - 60.0, **kw))

    def test_a_exits_b_starts_is_a_new_episode_and_a_new_finished_key(self):
        run = Run()
        run(sample(0, chrome=self.WORKING, identity=present(pid=20, start="A")))
        a_done = self.finish(run, 10, identity=present(pid=20, start="A"))
        self.assertEqual(a_done["state"], "finished")
        run(sample(15, identity=ABSENT, changed_at=-50.0))
        dead = run(sample(20, identity=ABSENT, changed_at=-50.0))
        self.assertEqual(dead["state"], "dead")
        self.assertEqual(dead["episode"], a_done["episode"])
        b = run(sample(25, chrome=self.WORKING, identity=present(pid=30, start="B")))
        self.assertEqual(b["episode"], a_done["episode"] + 1)
        b_done = self.finish(run, 35, identity=present(pid=30, start="B"))
        self.assertEqual(b_done["state"], "finished")
        self.assertNotEqual(b_done["alert"]["key"], a_done["alert"]["key"])
        self.assertEqual(len(run.alerts()), 3)  # A finished, dead, B finished

    def test_twin_one_process_two_turns_with_a_streak_of_2_is_a_new_episode(self):
        run = Run()
        run(sample(0, chrome=self.WORKING))
        first = self.finish(run, 10)
        run(sample(15, chrome=self.WORKING, content_rev=2))
        second_turn = run(sample(20, chrome=self.WORKING, content_rev=3))
        self.assertEqual(second_turn["episode"], first["episode"] + 1)
        done = self.finish(run, 30)
        self.assertIsNotNone(done["alert"])
        self.assertNotEqual(done["alert"]["key"], first["alert"]["key"])

    def test_the_streak_is_independent_of_publication(self):
        run = Run()
        run(sample(0, chrome=self.WORKING))
        first = self.finish(run, 10)
        s1 = run(sample(15, chrome=self.WORKING))
        self.assertEqual(s1["state"], "working", "WORKING is published at once")
        self.assertEqual(s1["episode"], first["episode"])
        s2 = run(sample(20, chrome=self.WORKING))
        self.assertEqual(s2["episode"], first["episode"] + 1)

    def test_twin_composer_text_typed_and_left_is_the_same_episode(self):
        run = Run()
        run(sample(0, chrome=self.WORKING))
        first = self.finish(run, 10)
        for t, rev in ((15, 2), (20, 3), (25, 4)):
            run(sample(t, chrome={**self.IDLE, "turn_done": True}, content_rev=rev, changed_at=float(t)))
        again = self.finish(run, 100)
        self.assertEqual(again["episode"], first["episode"])
        self.assertIsNone(again["alert"], "no second FINISHED alert")

    def test_a_short_turn_on_one_sample_does_not_alert_again(self):
        run = Run()
        run(sample(0, chrome=self.WORKING))
        first = self.finish(run, 10)
        run(sample(15, chrome=self.WORKING))
        again = self.finish(run, 80)
        self.assertEqual(again["episode"], first["episode"])
        self.assertIsNone(again["alert"])

    def test_kinds_without_working_signals_count_three_content_changes(self):
        cursor = present("cursor")
        run = Run()
        run(sample(0, identity=cursor, content_kind="cursor", content_rev=1))
        first = run(sample(50, identity=cursor, content_kind="cursor", content_rev=1, changed_at=0.0))
        self.assertEqual(first["state"], "finished")
        for t, rev in ((55, 2), (60, 3)):
            r = run(sample(t, identity=cursor, content_kind="cursor", content_rev=rev, changed_at=float(t)))
            self.assertEqual(r["episode"], first["episode"])
        r = run(sample(65, identity=cursor, content_kind="cursor", content_rev=4, changed_at=65.0))
        self.assertEqual(r["episode"], first["episode"] + 1)

    def test_a_new_pane_generation_starts_over_at_episode_0(self):
        run = Run()
        run(sample(0, chrome=self.WORKING))
        r = run(sample(5, generation=("%7", "6000"), chrome=self.WORKING, identity=present(start="X")))
        self.assertEqual(r["episode"], 1, "episode 0, then the first agent generation")


class HoldRows(unittest.TestCase):
    def test_waiting_is_published_after_the_8s_hold(self):
        run = Run()
        for t in range(0, 8):
            r = run(sample(t, attention=ANSWERABLE, request_id="R"))
            # Nothing is published before the hold: a new pane reads unknown (§6.2).
            self.assertEqual((r["state"], r["candidate"]), ("unknown", "waiting"))
        self.assertEqual(run(sample(8, attention=ANSWERABLE, request_id="R"))["state"], "waiting")

    def test_twin_a_candidate_for_7s_then_gone_publishes_nothing(self):
        run = Run()
        for t in range(0, 8):
            run(sample(t, attention=ANSWERABLE, request_id="R"))
        r = run(sample(8, changed_at=8.0))
        self.assertEqual(r["state"], "working")
        self.assertEqual(run.alerts(), [])

    def test_a_changed_key_restarts_the_hold(self):
        run = Run()
        for t in range(0, 6):
            run(sample(t, attention=ANSWERABLE))
        other = {**ANSWERABLE, "key": "answerable:bbbb"}
        for t in range(6, 14):
            self.assertNotEqual(run(sample(t, attention=other))["state"], "waiting", t)
        self.assertEqual(run(sample(14, attention=other))["state"], "waiting")

    def test_a_second_dialog_with_a_new_key_alerts_again(self):
        run = Run()
        for t in range(0, 9):
            run(sample(t, attention=ANSWERABLE))
        other = {**ANSWERABLE, "key": "answerable:bbbb"}
        for t in range(9, 18):
            run(sample(t, attention=other))
        self.assertEqual(len(run.alerts()), 2)
        self.assertEqual(run.results[-1]["state"], "waiting")

    def test_the_same_key_held_on_does_not_alert_twice(self):
        run = Run()
        for t in range(0, 40):
            run(sample(t, attention=ANSWERABLE))
        self.assertEqual(len(run.alerts()), 1)

    def test_the_hold_follows_the_configured_seconds(self):
        run = Run()
        for t in range(0, 20):
            r = run(sample(t, attention=ANSWERABLE, config={"alert_hold_sec": 20}))
            self.assertNotEqual(r["state"], "waiting")
        self.assertEqual(run(sample(20, attention=ANSWERABLE, config={"alert_hold_sec": 20}))["state"],
                         "waiting")


class InterruptedEvidenceRows(unittest.TestCase):
    """Consecutive evidence is consecutive: a sample that is missing or
    incomplete, or one where a higher rule wins, still breaks or updates it
    (finding 4343, the judge's three traces, each with its uninterrupted twin)."""

    WORKING = {"working": True, "turn_done": False, "title_busy": False}
    IDLE = {"working": False, "turn_done": True, "title_busy": False}
    RECONNECT = "• Reconnecting... 2/5\n› Ask Codex to do anything"

    def test_a_capture_failure_between_two_absences_breaks_the_run(self):
        run = Run()
        self.assertEqual(run(sample(0, changed_at=-100.0))["state"], "finished")
        run(sample(5, identity=ABSENT, changed_at=-100.0))
        run(sample(10, capture_ok=False))
        r = run(sample(15, identity=ABSENT, changed_at=-100.0))
        self.assertEqual(r["state"], "finished", "absent, missing, absent is not two consecutive absences")
        r = run(sample(20, identity=ABSENT, changed_at=-100.0))
        self.assertEqual((r["state"], r["reason"]), ("dead", "exited"))

    def test_a_listing_failure_between_two_absences_breaks_the_run(self):
        run = Run()
        run(sample(0, changed_at=-100.0))
        run(sample(5, identity=ABSENT, changed_at=-100.0))
        run(sample(10, listing_ok=False))
        self.assertNotEqual(run(sample(15, identity=ABSENT, changed_at=-100.0))["state"], "dead")

    def test_twin_two_consecutive_absences_are_dead(self):
        run = Run()
        run(sample(0, changed_at=-100.0))
        run(sample(5, identity=ABSENT, changed_at=-100.0))
        self.assertEqual(run(sample(10, identity=ABSENT, changed_at=-100.0))["state"], "dead")

    def test_an_incomplete_sample_without_working_evidence_breaks_the_streak(self):
        run = Run()
        first = run(sample(0, chrome=self.IDLE, changed_at=-100.0))
        self.assertEqual(first["state"], "finished")
        run(sample(5, chrome=self.WORKING))
        run(sample(10, identity=INCOMPLETE, chrome=self.IDLE))
        r = run(sample(15, chrome=self.WORKING))
        self.assertEqual(r["episode"], first["episode"], "working, incomplete-idle, working is no streak of 2")
        done = run(sample(60, chrome=self.IDLE, changed_at=0.0))
        self.assertEqual(done["state"], "finished")
        self.assertIsNone(done["alert"], "the same episode's FINISHED alerted again")

    def test_a_capture_failure_breaks_the_streak(self):
        run = Run()
        first = run(sample(0, chrome=self.IDLE, changed_at=-100.0))
        run(sample(5, chrome=self.WORKING))
        run(sample(10, capture_ok=False))
        self.assertEqual(run(sample(15, chrome=self.WORKING))["episode"], first["episode"])

    def test_twin_two_consecutive_working_samples_are_a_new_episode(self):
        run = Run()
        first = run(sample(0, chrome=self.IDLE, changed_at=-100.0))
        run(sample(5, chrome=self.WORKING))
        self.assertEqual(run(sample(10, chrome=self.WORKING))["episode"], first["episode"] + 1)

    def test_the_reconnect_line_gone_under_a_dialog_resets_the_stall(self):
        run = Run()
        run(screen(0, self.RECONNECT, changed_at=0.0))
        for t in (100, 105):
            r = run(sample(t, attention=ANSWERABLE, content_kind="codex", identity=present("codex")))
        for t in range(300, 608, 4):
            r = run(screen(t, self.RECONNECT, changed_at=0.0))
            self.assertNotEqual(r["reason"], "stalled", t)
        r = run(screen(608, self.RECONNECT, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "stalled"))

    def test_twin_a_dialog_above_a_live_reconnect_line_keeps_counting(self):
        run = Run()
        run(screen(0, self.RECONNECT, changed_at=0.0))
        for t in (100, 105):
            run(sample(t, attention=ANSWERABLE, stall="2/5", content_kind="codex", identity=present("codex")))
        for t in range(110, 308, 5):
            run(screen(t, self.RECONNECT, changed_at=0.0))
        r = run(screen(308, self.RECONNECT, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("waiting", "stalled"))

    def test_a_capture_failure_resets_the_stall(self):
        run = Run()
        run(screen(0, self.RECONNECT, changed_at=0.0))
        run(sample(150, capture_ok=False))
        for t in range(155, 320, 5):
            r = run(screen(t, self.RECONNECT, changed_at=0.0))
            self.assertNotEqual(r["reason"], "stalled", t)


class PresentationRows(unittest.TestCase):
    """STARTING and UNKNOWN for each identity state (present, absent, opaque, incomplete)."""

    def test_present_is_classified_never_starting_or_unknown(self):
        r = Run()(sample(0, changed_at=0.0))
        self.assertEqual((r["state"], r["reason"]), ("working", "working"))

    def test_absent_young_is_starting_and_old_is_dead(self):
        run = Run()
        run(sample(10, identity=ABSENT, generation_born=0.0))
        self.assertEqual(run(sample(15, identity=ABSENT, generation_born=0.0))["reason"], "starting")

    def test_absent_once_with_nothing_published_is_unknown(self):
        r = Run()(sample(10, identity=ABSENT, generation_born=0.0))
        self.assertEqual((r["state"], r["health"]), ("unknown", "ok"))

    def test_opaque_without_an_agent_fingerprint_is_unknown(self):
        r = Run()(sample(0, identity=OPAQUE, content_kind="shell"))
        self.assertEqual((r["state"], r["reason"]), ("unknown", "opaque"))

    def test_opaque_with_an_agent_fingerprint_is_classified(self):
        r = Run()(sample(0, identity=OPAQUE, content_kind="codex", changed_at=0.0))
        self.assertEqual(r["state"], "working")

    def test_incomplete_with_nothing_published_is_unknown(self):
        r = Run()(sample(0, identity=INCOMPLETE))
        self.assertEqual((r["state"], r["reason"], r["health"]), ("unknown", "incomplete", "unknown"))

    def test_identity_of_maps_each_observation(self):
        cases = ((readable_claude(), "present"), (agent_free_shell(), "absent"),
                 (judge_denied_comm(), "incomplete"))
        for procs, status in cases:
            with self.subTest(status=status):
                FakeProc(procs).install(self)
                self.assertEqual(identity_of(agent_identity.observe_process_tree(10))["status"], status)
        from tests.test_process_observation import wrapped_shell
        FakeProc(wrapped_shell()).install(self)
        self.assertEqual(identity_of(agent_identity.observe_process_tree(10))["status"], "opaque")


class PurityTests(unittest.TestCase):
    SOURCE = (ROOT / "shared" / "delegate_state.py").read_text(encoding="utf-8")

    def test_the_classifier_holds_no_prompt_or_report_regex(self):
        tree = ast.parse(self.SOURCE)
        names = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                 for alias in node.names}
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertNotIn("re", names)
        for word in ("DONE", "BLOCKED", "Working (", "esc to interrupt", "Reconnecting"):
            self.assertNotIn(word, self.SOURCE)

    def test_the_classifier_imports_nothing_stateful(self):
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertLessEqual(imported, {"__future__", "copy"})

    def test_the_agent_kinds_match_agent_identity(self):
        self.assertEqual(AGENT_KINDS, agent_identity.AGENT_KINDS)

    def test_classify_does_not_mutate_its_inputs(self):
        s = sample(0, attention=ANSWERABLE, autoyes=armed())
        frozen = repr(s)
        _r, memory = classify(s, None)
        frozen_memory = repr(memory)
        classify(sample(9, attention=ANSWERABLE, autoyes=armed()), memory)
        self.assertEqual(repr(s), frozen)
        self.assertEqual(repr(memory), frozen_memory)


class SignalTests(unittest.TestCase):
    """shared/delegate_signals.py: the screen-derived fields, both directions."""

    def test_working_chrome(self):
        self.assertTrue(delegate_signals.chrome("x\n• Working (1m 02s • esc to interrupt)", "")["working"])
        self.assertTrue(delegate_signals.chrome("x\n  esc to interrupt", "")["working"])
        self.assertFalse(delegate_signals.chrome("I was Working (hard) yesterday\n" + "\n" * 13 + "x", "")["working"])

    def test_turn_done_chrome(self):
        self.assertTrue(delegate_signals.chrome("─ Worked for 1h 19m ─", "")["turn_done"])
        self.assertTrue(delegate_signals.chrome("✻ Crunched for 11m", "")["turn_done"])
        self.assertFalse(delegate_signals.chrome("I worked for 3 days on it", "")["turn_done"])

    def test_title_busy_is_a_braille_spinner(self):
        self.assertTrue(delegate_signals.chrome("", "⠋ codex")["title_busy"])
        self.assertFalse(delegate_signals.chrome("", "✳ Curl example")["title_busy"])

    def test_report_line(self):
        self.assertEqual(delegate_signals.report_line("x\n● DONE — all green\n"), "● DONE — all green")
        self.assertEqual(delegate_signals.report_line("BUILD767 BLOCKED — need it"), "BUILD767 BLOCKED — need it")
        self.assertIsNone(delegate_signals.report_line("Reply: DONE — <one line> | BLOCKED — <what>"))
        self.assertIsNone(delegate_signals.report_line("BLOCKED — early\n" + "line\n" * 25))
        self.assertEqual(len(delegate_signals.report_line("DONE — " + "x" * 300)), 120)

    def test_stall_counter(self):
        self.assertEqual(delegate_signals.stall_counter("• Reconnecting... 2/5"), "2/5")
        self.assertIsNone(delegate_signals.stall_counter("• Reconnecting... 2/5\n" + "x\n" * 13))
        self.assertIsNone(delegate_signals.stall_counter("all fine"))


if __name__ == "__main__":
    unittest.main()
