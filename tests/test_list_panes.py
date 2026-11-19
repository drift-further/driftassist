"""One tmux pane listing, `shared/tmux.py:list_panes`, for /poll, /terminal/sessions
and the Auto-Yes scanner (effort 621 Phase 7 step A).

Each of the three used to carry its own `list-panes -a -F` format and its own
positional parser, so a field added to one was missing from the others. One
listing now returns `PaneRef` rows: the pane id (`%N`), the tmux server
generation (socket path, server pid and start time, hashed), the session name
and the display target `session:w.p`, plus the fields /poll needs. The durable
key `<generation>:<pane_id>` rides on the same single tmux call, so /poll stays
one listing per request however many panes there are.

Run: .venv/bin/python3 -m unittest tests.test_list_panes
"""

import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask

from shared import tmux
from tests._tmux_box import TmuxBox

ROOT = Path(__file__).resolve().parent.parent
KEY_RE = re.compile(r"[0-9a-f]{16}:%\d+")


def _completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _full_row(session="dev", window="1", pane="2", pane_id="%7", pid="4242",
              socket_path="/tmp/x/s"):
    # The order of shared/tmux.py:LIST_PANES_FIELDS.
    fields = [session, window, pane, "999", "bash", pane_id, "80", "24",
              "1700000000", "1690000000", "/dev/pts/3", "1700000001", "12",
              "0", "5", "0", "parent", "0", pid, socket_path]
    return "\t".join(fields) + "\n"


class ParseTests(unittest.TestCase):
    def listing(self, stdout, start="777", returncode=0, stderr=""):
        with patch.object(tmux.subprocess, "run", return_value=_completed(stdout, returncode, stderr)), \
                patch.object(tmux, "_process_start_time", return_value=start):
            return tmux.list_panes()

    def test_a_full_row_carries_identity_fields_and_a_durable_key(self):
        [ref] = self.listing(_full_row())
        self.assertEqual((ref.session, ref.window, ref.pane, ref.pane_id), ("dev", 1, 2, "%7"))
        self.assertEqual(ref.display_target, "dev:1.2")
        self.assertEqual((ref.pane_pid, ref.command, ref.width, ref.height), ("999", "bash", 80, 24))
        self.assertEqual((ref.session_created, ref.pane_tty, ref.assist_parent, ref.pane_dead),
                         (1690000000, "/dev/pts/3", "parent", "0"))
        generation = tmux.generation_of("/tmp/x/s", 4242, "777")
        self.assertRegex(generation, r"^[0-9a-f]{16}$")
        self.assertEqual(ref.generation, generation)
        self.assertEqual(ref.key, f"{generation}:%7")
        # The change marker in shared/tmux.py:capture_pane_if_changed's order.
        self.assertEqual(ref.marker, ("1700000001", "12", "0", "5", "80", "24", "0", "%7"))

    def test_generation_depends_on_socket_pid_and_start_time(self):
        base = tmux.generation_of("/tmp/x/s", 4242, "777")
        self.assertEqual(base, tmux.generation_of("/tmp/x/s", 4242, "777"))
        self.assertNotEqual(base, tmux.generation_of("/tmp/y/s", 4242, "777"))
        self.assertNotEqual(base, tmux.generation_of("/tmp/x/s", 4243, "777"))
        self.assertNotEqual(base, tmux.generation_of("/tmp/x/s", 4242, "778"))

    def test_an_unreadable_server_start_gives_no_key(self):
        [ref] = self.listing(_full_row(), start=None)
        self.assertIsNone(ref.generation)
        self.assertIsNone(ref.key)
        self.assertEqual(ref.display_target, "dev:1.2")

    def test_a_short_row_still_parses_without_a_key(self):
        # The Auto-Yes scanner's old five- and six-field order is the listing's
        # prefix, so its fixtures keep meaning what they meant.
        refs = self.listing("s\t0\t0\t123\tclaude\nt\t1\t2\t124\tcodex\t%3\n")
        self.assertEqual([r.display_target for r in refs], ["s:0.0", "t:1.2"])
        self.assertEqual([r.pane_id for r in refs], ["", "%3"])
        self.assertEqual([r.pane_pid for r in refs], ["123", "124"])
        self.assertEqual([r.command for r in refs], ["claude", "codex"])
        self.assertEqual([r.key for r in refs], [None, None])
        self.assertEqual([r.marker for r in refs], [None, None])

    def test_rows_too_short_or_with_bad_indexes_are_skipped(self):
        self.assertEqual(self.listing("s\t0\t0\t123\n\nx\ta\t0\t1\tbash\n"), [])

    def test_a_session_name_with_a_colon_keeps_its_session(self):
        [ref] = self.listing(_full_row(session="c:d"))
        self.assertEqual((ref.session, ref.display_target), ("c:d", "c:d:1.2"))

    def test_an_empty_listing_is_not_a_failure(self):
        self.assertEqual(self.listing(""), [])

    def test_a_failed_listing_raises_and_says_whether_the_server_is_gone(self):
        with self.assertRaises(tmux.TmuxListError) as gone:
            self.listing("", returncode=1, stderr="no server running on /tmp/x/s")
        self.assertTrue(gone.exception.no_server)
        with self.assertRaises(tmux.TmuxListError) as other:
            self.listing("", returncode=1, stderr="lost server")
        self.assertFalse(other.exception.no_server)

    def test_the_socket_argument_scopes_the_call(self):
        with patch.object(tmux.subprocess, "run", return_value=_completed("")) as run:
            tmux.list_panes(socket="/tmp/x/s")
            tmux.list_panes()
        self.assertEqual(run.call_args_list[0].args[0][:3], ["tmux", "-S", "/tmp/x/s"])
        self.assertEqual(run.call_args_list[1].args[0][:2], ["tmux", "list-panes"])


class SourceGuardTests(unittest.TestCase):
    def test_the_three_consumers_have_no_parser_of_their_own(self):
        for name in ("routes/poll.py", "routes/terminal.py", "routes/autoyes.py"):
            with self.subTest(module=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn('"list-panes"', source)
                self.assertIn("list_panes(", source)


def _patched_poll(case):
    """/poll's own writes patched out, as tests/test_poll_perf.py does."""
    import routes.poll as poll
    import shared.state as state

    patches = [
        patch.object(state, "tmux_target", None),
        patch.object(state, "save_idle_state", MagicMock()),
        patch.object(state, "pane_content_hash", {}),
        patch.object(state, "pane_last_activity", {}),
        patch("routes.poll.tab_state.sweep_agent_declarations"),
        patch("routes.poll.tab_state.sweep_wakes"),
        patch("routes.poll.tab_state.apply_order", side_effect=lambda panes: panes),
        patch("routes.poll.tab_state.get_tab_state", return_value={}),
        patch("routes.poll.drafts.poll_block", return_value=None),
        patch("routes.studio.studio_poll_block", return_value=None),
        patch.object(poll, "_SCAN_CACHE", {}),
    ]
    for p in patches:
        p.start()
        case.addCleanup(p.stop)


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class RealTmuxListingTests(unittest.TestCase):
    def setUp(self):
        self.box = TmuxBox(self)
        self.dev1 = self.box.new_session("dev")
        self.dev2 = self.box.split("dev")
        self.other = self.box.new_session("dev2")
        time.sleep(0.2)

    def test_list_panes_single_parser_matches_poll_fields(self):
        refs = {r.pane_id: r for r in tmux.list_panes()}
        self.assertEqual(
            {pid: r.display_target for pid, r in refs.items()},
            {self.dev1: "dev:1.1", self.dev2: "dev:1.2", self.other: "dev2:1.1"},
        )
        socket_path, server_pid = self.box.server_generation_inputs()
        generation = tmux.generation_of(socket_path, server_pid, tmux._process_start_time(server_pid))
        for ref in refs.values():
            self.assertEqual(ref.generation, generation)
            self.assertEqual(ref.key, f"{generation}:{ref.pane_id}")
            self.assertEqual(ref.pane_pid, self.box.display(ref.pane_id, "#{pane_pid}"))

        import routes.poll as poll
        import routes.terminal as terminal
        _patched_poll(self)
        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        app.register_blueprint(terminal.terminal_bp)
        client = app.test_client()
        fields = ("target", "session", "window", "pane", "pane_id", "pane_pid",
                  "command", "width", "height", "created", "key")
        for path in ("/poll", "/terminal/sessions"):
            with self.subTest(path=path):
                listed = client.get(path).get_json()["sessions"]
                self.assertEqual(len(listed), 3)
                for pane in listed:
                    ref = refs[pane["pane_id"]]
                    expected = (ref.display_target, ref.session, ref.window, ref.pane, ref.pane_id,
                                ref.pane_pid, ref.command, ref.width, ref.height,
                                ref.session_created, ref.key)
                    self.assertEqual(tuple(pane[f] for f in fields), expected)

    def test_listing_is_one_tmux_call_whatever_the_pane_count(self):
        real = subprocess.run
        calls = []

        def recording(args, *a, **k):
            calls.append(list(args))
            return real(args, *a, **k)

        with patch.object(tmux.subprocess, "run", side_effect=recording):
            refs = tmux.list_panes()
        self.assertEqual(len(refs), 3)
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(r.key for r in refs))

    def test_poll_adds_key_without_another_tmux_call(self):
        import routes.poll as poll
        _patched_poll(self)
        app = Flask(__name__)
        app.register_blueprint(poll.poll_bp)
        real = subprocess.run
        calls = []

        def recording(args, *a, **k):
            calls.append(list(args))
            return real(args, *a, **k)

        with patch.object(poll.subprocess, "run", side_effect=recording):
            body = app.test_client().get("/poll").get_json()
        self.assertTrue(all(KEY_RE.fullmatch(p["key"]) for p in body["sessions"]))
        tmux_calls = [c for c in calls if c and c[0] == "tmux"]
        self.assertEqual(sum("list-panes" in c for c in tmux_calls), 1)
        # Nothing per pane asks tmux for the server's identity: the key rides on
        # the one listing.
        asks_identity = [c for c in tmux_calls if "list-panes" not in c
                         and any("#{pid}" in str(a) or "#{socket_path}" in str(a) for a in c)]
        self.assertEqual(asks_identity, [])

    def test_key_is_stable_on_one_server_and_changes_when_the_server_restarts(self):
        first = {r.pane_id: r.key for r in tmux.list_panes()}
        self.assertEqual(first, {r.pane_id: r.key for r in tmux.list_panes()})
        self.box.kill_server()
        restarted = self.box.new_session("dev")
        self.box.split("dev")
        # A fresh server hands out the same ids again: %0 is back.
        self.assertIn(restarted, first)
        second = {r.pane_id: r.key for r in tmux.list_panes()}
        self.assertNotEqual(second[restarted], first[restarted])

    def test_session_of_and_canonical_key_resolve_every_form(self):
        refs = {r.pane_id: r for r in tmux.list_panes()}
        dev2 = refs[self.dev2]
        for form in (dev2.key, self.dev2, "dev:1.2"):
            with self.subTest(form=form):
                self.assertEqual(tmux.canonical_key(form), dev2.key)
                self.assertEqual(tmux.session_of(form), "dev")
        # Given the listing, neither asks tmux again.
        panes = list(refs.values())
        with patch.object(tmux.subprocess, "run", side_effect=AssertionError("no tmux call")):
            self.assertEqual(tmux.canonical_key("dev2:1.1", panes), refs[self.other].key)
            self.assertEqual(tmux.session_of(dev2.key, panes), "dev")
            # A display target names its session without any listing at all.
            self.assertEqual(tmux.session_of("gone:3.4"), "gone")
            self.assertEqual(tmux.session_of("c:d:1.2"), "c:d")

    def test_session_of_and_canonical_key_refuse_what_is_not_live(self):
        refs = {r.pane_id: r for r in tmux.list_panes()}
        stale = f"{'0' * 16}:{self.dev2}"
        for form in (stale, "gone:1.1", "%999", "dev:1.9"):
            with self.subTest(form=form):
                self.assertIsNone(tmux.canonical_key(form))
        self.assertIsNone(tmux.session_of(stale))
        self.assertIsNone(tmux.session_of("%999"))
        self.assertIsNone(tmux.canonical_key(""))
        self.assertIsNone(tmux.session_of(""))
        self.assertEqual(tmux.canonical_key(refs[self.dev1].key), refs[self.dev1].key)


if __name__ == "__main__":
    unittest.main()
