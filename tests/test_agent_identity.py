"""shared/agent_identity.py — which agent owns a pane when its tree holds more than one.

Task 2304. A pane root can have two live agent descendants: a Claude that runs
codex from its Bash tool, a shell with a backgrounded agent beside the
foreground one, an npx launcher over the Node entrypoint it starts. The rule:

  * a process nested under any agent belongs to that agent (a launcher chain,
    a shim, a nested `claude -p`, two `codex exec` from Claude's Bash tool),
    so only the outermost agent of each branch is a candidate;
  * of the candidates, the nearest descendant of the pane root wins when their
    kinds differ;
  * two candidates of the same kind leave the pane with no identity, and the
    Auto-Yes scanner refuses it: the prompt is left to the human, and the
    question popup and delegate alerts are not suppressed for it.

Every tree is an in-memory fake /proc (tests/test_process_observation.FakeProc);
no real process is read. The /poll surface (a refused pane keeps its question
popup; one /poll walks each tree once) is in tests/test_poll_perf.py, where the
real /poll runs against an isolated tmux.

Run: .venv/bin/python3 -m unittest tests.test_agent_identity
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from routes import autoyes
from shared import agent_identity
from tests.test_autoyes_failsafe import CLAUDE_YNA, _ScannerCase
from tests.test_process_observation import FakeProc

TARGET = "fs:0.0"
PANE_PID = 123  # what the scanner harness's list-panes reports


def _proc(ppid, argv, start):
    return {"comm": Path(argv[0]).name[:15], "argv": argv, "ppid": ppid, "start": start}


def _bash(ppid=1, start=1000):
    return _proc(ppid, ["bash"], start)


CLAUDE_NPM = "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
CODEX_NPM = "/usr/lib/node_modules/@openai/codex/bin/codex.js"

# Name -> (tree, expected kind or None for refused, refused?)
TREES = {
    # --- kinds differ: the nearest candidate wins --------------------------
    # Listed deepest-first so neither pid order nor listing order can be what
    # picks the winner.
    "claude nearer than a codex in a sibling branch": ({
        PANE_PID: _bash(),
        150: _proc(PANE_PID, ["sh"], 1001),
        160: _proc(150, ["codex"], 1002),
        400: _proc(PANE_PID, ["claude"], 1003),
    }, "claude", False),
    "codex nearer than a claude in a sibling branch": ({
        PANE_PID: _bash(),
        150: _proc(PANE_PID, ["sh"], 1001),
        160: _proc(150, ["claude"], 1002),
        400: _proc(PANE_PID, ["codex"], 1003),
    }, "codex", False),
    "claude running codex from its Bash tool": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        210: _proc(200, ["bash", "-c", "codex exec 'review'"], 1002),
        220: _proc(210, ["codex", "exec", "review"], 1003),
    }, "claude", False),
    # The children of an agent's tool runner have no access to the pane TTY,
    # so however many there are, of whatever kind, they cannot own the dialog.
    "claude running two codex exec in parallel": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        210: _proc(200, ["bash", "-c", "codex exec 'a'"], 1002),
        220: _proc(210, ["codex", "exec", "a"], 1003),
        230: _proc(200, ["bash", "-c", "codex exec 'b'"], 1004),
        240: _proc(230, ["codex", "exec", "b"], 1005),
    }, "claude", False),
    "codex running two claude -p in parallel": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["codex"], 1001),
        210: _proc(200, ["bash", "-c", "claude -p 'a'"], 1002),
        220: _proc(210, ["claude", "-p", "a"], 1003),
        230: _proc(200, ["bash", "-c", "claude -p 'b'"], 1004),
        240: _proc(230, ["claude", "-p", "b"], 1005),
    }, "codex", False),
    # --- one agent, several processes of its kind: not ambiguous -----------
    "npx launcher over the claude entrypoint": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["npx", "-y", "@anthropic-ai/claude-code"], 1001),
        300: _proc(200, ["node", CLAUDE_NPM], 1002),
    }, "claude", False),
    "codex npm shim over the native binary": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["node", CODEX_NPM], 1001),
        300: _proc(200, ["codex"], 1002),
    }, "codex", False),
    "claude whose Bash tool runs claude -p": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        210: _proc(200, ["bash", "-c", "claude -p 'summarise'"], 1002),
        220: _proc(210, ["claude", "-p", "summarise"], 1003),
    }, "claude", False),
    # --- two same-kind candidates: refused ----------------------------------
    "two claude siblings": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        300: _proc(PANE_PID, ["claude"], 1002),
    }, None, True),
    "two claudes at different depths, neither inside the other": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        250: _proc(PANE_PID, ["sh"], 1002),
        350: _proc(250, ["claude"], 1003),
    }, None, True),
    "two codex siblings beside a nearer claude": ({
        PANE_PID: _bash(),
        200: _proc(PANE_PID, ["claude"], 1001),
        250: _proc(PANE_PID, ["sh"], 1002),
        260: _proc(250, ["codex"], 1003),
        270: _proc(250, ["codex"], 1004),
    }, None, True),
}


class MultiDescendantIdentityTests(unittest.TestCase):
    def resolve(self, procs, command="bash"):
        FakeProc(procs).install(self)
        return (agent_identity.resolve_process(TARGET, PANE_PID, command),
                agent_identity.identity_refused(TARGET, PANE_PID))

    def test_every_tree_resolves_as_labelled(self):
        for name, (procs, kind, refused) in TREES.items():
            with self.subTest(tree=name):
                agent_identity._PROCESS_CACHE.clear()
                self.assertEqual(self.resolve(procs), (kind, refused))

    def test_a_refusal_does_not_fall_back_to_the_pane_command(self):
        # tmux reports the foreground command; with two claudes it says
        # "claude", and that used to be the answer whenever the walk had none.
        procs, _kind, _refused = TREES["two claude siblings"]
        self.assertEqual(self.resolve(procs, command="claude"), (None, True))

    def test_the_corpus_covers_both_directions(self):
        self.assertEqual({refused for _p, _k, refused in TREES.values()}, {True, False})

    def test_no_tree_means_no_refusal(self):
        FakeProc({}).install(self)
        self.assertFalse(agent_identity.identity_refused(TARGET, PANE_PID))
        self.assertFalse(agent_identity.identity_refused(TARGET, None))


class ScannerRefusesAmbiguousPanesTests(_ScannerCase):
    """Real scanner ticks; the pane's tree is the fake /proc. The harness pins the
    resolved kind to claude (as the content tier would read the dialog), so only
    the tree decides."""

    def test_two_same_kind_candidates_leave_the_prompt_to_the_human(self):
        FakeProc(TREES["two claude siblings"][0]).install(self)
        self.tick(CLAUDE_YNA, now=100.0)
        self.assertIsNone(self.countdown())
        self.tick(CLAUDE_YNA, now=105.0).assert_not_called()

    def test_a_countdown_pending_when_the_pane_turns_ambiguous_is_dropped(self):
        FakeProc(TREES["npx launcher over the claude entrypoint"][0]).install(self)
        self.tick(CLAUDE_YNA, now=100.0)
        self.assertIsNotNone(self.countdown())
        agent_identity._PROCESS_CACHE.clear()
        FakeProc(TREES["two claude siblings"][0]).install(self)
        self.tick(CLAUDE_YNA, now=105.0).assert_not_called()
        self.assertIsNone(self.countdown())

    def test_one_candidate_is_answered(self):
        FakeProc(TREES["npx launcher over the claude entrypoint"][0]).install(self)
        self.tick(CLAUDE_YNA, now=100.0)
        self.assertEqual(self.countdown()["prompt_type"], "permission-yna")
        self.tick(CLAUDE_YNA, now=105.0).assert_called_once()

    def test_the_nearest_of_two_kinds_is_answered(self):
        FakeProc(TREES["claude running codex from its Bash tool"][0]).install(self)
        self.tick(CLAUDE_YNA, now=100.0)
        self.tick(CLAUDE_YNA, now=105.0).assert_called_once()

    def test_a_refused_pane_is_not_armed_for_the_popup_or_alerts(self):
        with patch("routes.autoyes.state.get_project_setting", return_value=False):
            self.assertFalse(autoyes.autoyes_will_consider(
                TARGET, True, "explicit", "claude", "claude", refused=True))
            self.assertTrue(autoyes.autoyes_will_consider(
                TARGET, True, "explicit", "claude", "claude", refused=False))

    def test_the_detail_resolver_reports_the_refusal_from_one_walk(self):
        for name, (procs, kind, refused) in TREES.items():
            with self.subTest(tree=name):
                agent_identity._PROCESS_CACHE.clear()
                FakeProc(procs).install(self)
                with patch.object(agent_identity, "_scan_process_tree",
                                  wraps=agent_identity._scan_process_tree) as walk:
                    got = agent_identity.resolve_process_detail(TARGET, PANE_PID, "bash")
                self.assertEqual(got, (kind, refused))
                self.assertEqual(walk.call_count, 1)

    def test_a_refused_delegate_pane_is_not_counted_as_auto_yes_owned(self):
        # The delegates surface: an armed del_* pane showing a Yes dialog. With
        # one claude its prompt is Auto-Yes-owned (armed); with two the scanner
        # refuses it, so the delegate must read unarmed and alert the human.
        from routes import delegates
        from shared import pane_scan
        dialog = "\n".join(["─" * 40, " Do you want to proceed?", " ❯ 1. Yes", "   2. No",
                            " Enter to select · Esc to cancel"])
        row = {"pane_id": "%77", "target": "del_x:0.0", "pane_pid": str(PANE_PID),
               "session": "del_x", "session_id": "$7", "pane_dead": "0", "pane_title": "",
               "window_activity": "1", "history_size": "0", "cursor_x": "0", "cursor_y": "0",
               "pane_width": "80", "pane_height": "24", "alternate_on": "0"}
        for tree, armed in (("claude whose Bash tool runs claude -p", True),
                            ("two claude siblings", False)):
            with self.subTest(tree=tree), \
                 patch.object(delegates, "_capture", return_value=dialog), \
                 patch.object(delegates.resume_line.RESOLVER, "resolve", return_value=(None, "x")), \
                 patch.object(delegates, "_claude_status", return_value=None), \
                 patch.object(delegates.state, "autoyes_enabled_for", return_value=(True, "explicit")), \
                 patch.dict(pane_scan._SCAN_CACHE, clear=True):
                agent_identity._PROCESS_CACHE.clear()
                FakeProc(TREES[tree][0]).install(self)
                sample = delegates._build_sample(dict(row), 100.0, {
                    "alert_hold_sec": 0, "finish_quiet_sec": 0, "stall_sec": 0})
                self.assertEqual(sample["autoyes"]["armed"], armed)


if __name__ == "__main__":
    unittest.main()
