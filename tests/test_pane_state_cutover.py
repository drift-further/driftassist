"""Stored per-pane state moves to durable pane keys (effort 621 Phase 7 step B).

Drafts and snoozes were keyed by `session:w.p`, which names a POSITION: kill a
pane and its neighbour takes the name, restart tmux and a new pane takes the
`%N`. Step B keys them by the durable key `<generation>:%N`
(shared/tmux.py:PaneRef.key). A pre-upgrade file is converted once, on load,
before the first canonical read or write:

- a legacy key naming exactly one live pane becomes that pane's key, every
  field kept, `legacy_from` recorded;
- a legacy key naming no live pane stays under its legacy key, `orphan: true`,
  and is never assigned to a later pane by name;
- a collision keeps both: the canonical entry stays, the legacy one is an orphan;
- the pre-upgrade bytes are copied next to the file first, and a `schema: 2`
  marker makes the conversion run once.

Old clients still send `session:w.p`; the server canonicalises every read and
write and projects /poll's draft marks and revisions per protocol (`proto=2`:
durable keys; otherwise display targets).

Run: .venv/bin/python3 -m unittest tests.test_pane_state_cutover
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
from shared import drafts, tab_state, tmux
from shared.tmux import PaneRef
from tests._tmux_box import TmuxBox

ROOT = Path(__file__).resolve().parent.parent
GEN = "a" * 16
GEN2 = "b" * 16
NOW = 1_800_000_000.0
KEY_RE = re.compile(r"[0-9a-f]{16}:%\d+")


def ref(display, pane_id, generation=GEN):
    session = display.rsplit(":", 1)[0]
    return PaneRef(pane_id=pane_id, generation=generation, session=session,
                   display_target=display)


def entry(text, updated_at, enter_armed=True, attachments=()):
    return {"text": text, "attachments": list(attachments), "enter_armed": enter_armed,
            "updated_at": updated_at}


# A real upload (the step B conversion gives v2 its own copy of each file the
# legacy store references, sign-off M3 R2-F2), unique to this process.
ATT_TAG = f"p7batt{os.getpid()}"
ATT_BYTES = b"twelve bytes"
ATT = {"id": "u1", "name": "a.png", "size": 12, "path": f"/tmp/assist_u1_{ATT_TAG}.png"}


def _app(*blueprints):
    app = Flask(__name__)
    for bp in blueprints:
        app.register_blueprint(bp)
    return app.test_client()


class _StoreCase(unittest.TestCase):
    """Scratch drafts.json and tab_state.json, and a fake live pane list."""

    panes = []

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="p7b-state-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.drafts_file = self.dir / "drafts.json"
        self.tab_file = self.dir / "tab_state.json"
        self.drafts_v2 = self.dir / "drafts.v2.json"
        self.tab_v2 = self.dir / "tab_state.v2.json"
        self.live = list(self.panes)
        for p in (patch.object(drafts, "DRAFTS_FILE", self.drafts_file),
                  patch.object(tab_state, "TAB_STATE_FILE", self.tab_file),
                  patch.object(drafts, "DRAFTS_V2_FILE", self.drafts_v2),
                  patch.object(tab_state, "TAB_STATE_V2_FILE", self.tab_v2),
                  patch.object(drafts, "_list_panes", side_effect=lambda: list(self.live)),
                  patch.object(tab_state, "_list_panes", side_effect=lambda: list(self.live)),
                  patch.object(state, "pane_last_activity", {})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._reload_defaults)
        Path(ATT["path"]).write_bytes(ATT_BYTES)
        self.addCleanup(lambda: [p.unlink(missing_ok=True)
                                 for p in Path("/tmp").glob(f"assist_*{ATT_TAG}.png")])

    def owned(self, e):
        """`e` with v2's own copy of ATT's upload shown as ATT, after checking
        it is one: a different file name holding the same bytes."""
        atts = []
        for a in e.get("attachments", []):
            if a.get("id") == ATT["id"]:
                self.assertNotEqual(a["path"], ATT["path"])
                self.assertEqual(Path(a["path"]).read_bytes(), ATT_BYTES)
                a = {**a, "path": ATT["path"]}
            atts.append(a)
        return {**e, "attachments": atts}

    def _reload_defaults(self):
        # Leave the module state empty for the next test module.
        drafts._drafts = {}
        drafts._converted = True
        tab_state._tab_state = tab_state._normalize({})
        tab_state._converted = True

    def seed(self, drafts_doc=None, tabs_doc=None):
        """Seed pre-upgrade files; a doc carrying `schema: 2` seeds the v2 file
        instead (a store 7B already converted)."""
        if drafts_doc is not None:
            if drafts_doc.get("schema") == 2:
                self.drafts_v2.write_text(json.dumps({
                    "schema": 2, "source": {"sha256": None, "entries": {}},
                    "drafts": {k: v for k, v in drafts_doc.items() if k != "schema"}}), encoding="utf-8")
            else:
                self.drafts_file.write_text(json.dumps(drafts_doc), encoding="utf-8")
        if tabs_doc is not None:
            target = self.tab_v2 if tabs_doc.get("schema") == 2 else self.tab_file
            target.write_text(json.dumps(tabs_doc), encoding="utf-8")
        self.restart()

    def disk_drafts(self):
        """The drafts store on disk, flat: v2 once written, else the legacy file."""
        if self.drafts_v2.exists():
            doc = json.loads(self.drafts_v2.read_text(encoding="utf-8"))
            return {"schema": doc["schema"], **doc["drafts"]}
        return json.loads(self.drafts_file.read_text(encoding="utf-8"))

    def disk_tabs(self):
        if self.tab_v2.exists():
            return json.loads(self.tab_v2.read_text(encoding="utf-8"))
        return json.loads(self.tab_file.read_text(encoding="utf-8"))

    def restart(self):
        """What an Assist restart does to these modules: load from disk."""
        drafts.load_drafts()
        tab_state.load_tab_state()

    def disk(self, path):
        return json.loads(path.read_text(encoding="utf-8"))


class ConverterTests(_StoreCase):
    panes = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1"), ref("dev2:1.1", "%2")]

    def test_converter_seeded_legacy_files_matched_entries_become_canonical_with_all_fields_and_revision_order(self):
        seeded = {
            "dev:1.2": entry("second", 300.0, enter_armed=False, attachments=[ATT]),
            "dev:1.1": entry("first", 100.0),
            "dev2:1.1": entry("", 200.0, enter_armed=False),
        }
        raw = json.dumps(seeded, indent=2).encode()
        self.drafts_file.write_bytes(raw)
        self.restart()
        drafts.ensure_converted()

        on_disk = self.disk_drafts()
        self.assertEqual(on_disk.pop("schema"), 2)
        self.assertEqual(list(on_disk), [f"{GEN}:%1", f"{GEN}:%0", f"{GEN}:%2"])
        for legacy, key in (("dev:1.2", f"{GEN}:%1"), ("dev:1.1", f"{GEN}:%0"),
                            ("dev2:1.1", f"{GEN}:%2")):
            self.assertEqual(self.owned(on_disk[key]), {**seeded[legacy], "legacy_from": legacy})
        # Revision order: every updated_at kept, so the newest draft is still newest.
        revs = {k: v["updated_at"] for k, v in on_disk.items()}
        self.assertEqual(sorted(revs, key=revs.get), [f"{GEN}:%0", f"{GEN}:%2", f"{GEN}:%1"])
        # The converted store is its own file; drafts.json is left byte for
        # byte as the pre-7B code wrote it (sign-off M3 F5).
        self.assertEqual(self.drafts_file.read_bytes(), raw)
        self.assertEqual(json.loads(self.drafts_v2.read_text())["source"]["entries"],
                         {k: v["updated_at"] for k, v in seeded.items()})
        self.assertEqual(self.drafts_v2.stat().st_mode & 0o777, 0o600)

        # Snoozes too, beside session-keyed pin and order that are left alone.
        tabs = {"pinned": ["dev"], "order": ["dev2", "dev"], "sort": "manual",
                "snoozed": {"dev:1.2": {"at": 50.0, "was_busy": True}},
                "agent_declarations": {}}
        tab_raw = json.dumps(tabs).encode()
        self.tab_file.write_bytes(tab_raw)
        tab_state.load_tab_state()
        tab_state.ensure_converted()
        tabs_disk = self.disk_tabs()
        self.assertEqual(tabs_disk["schema"], 2)
        self.assertEqual(tabs_disk["pinned"], ["dev"])
        self.assertEqual(tabs_disk["order"], ["dev2", "dev"])
        self.assertEqual(tabs_disk["snoozed"], {
            f"{GEN}:%1": {"at": 50.0, "was_busy": True, "legacy_from": "dev:1.2"}})
        self.assertEqual(self.tab_file.read_bytes(), tab_raw)

    def test_converter_unmatched_entries_retained_as_orphans_never_assigned_by_name(self):
        self.seed({"gone:1.1": entry("a gone pane's words", 50.0),
                   "dev:1.1": entry("mine", 60.0)},
                  {"snoozed": {"gone:1.1": {"at": 5.0, "was_busy": False}}})
        with patch("time.time", return_value=NOW):
            drafts.ensure_converted()
            tab_state.ensure_converted()
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk["gone:1.1"], {**entry("a gone pane's words", 50.0),
                                               "orphan": True, "orphaned_at": NOW})
        self.assertEqual(self.disk_tabs()["snoozed"]["gone:1.1"],
                         {"at": 5.0, "was_busy": False, "orphan": True, "orphaned_at": NOW})

        # A pane named gone:1.1 appears later: the orphan is NOT its draft, by
        # either protocol, and a save there does not touch the orphan.
        self.live.append(ref("gone:1.1", "%7"))
        self.assertEqual(drafts.get_draft("gone:1.1")["text"], "")
        self.assertEqual(drafts.get_draft(f"{GEN}:%7")["text"], "")
        drafts.set_draft("gone:1.1", text="new pane")
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk["gone:1.1"]["text"], "a gone pane's words")
        self.assertTrue(on_disk["gone:1.1"]["orphan"])
        self.assertEqual(on_disk[f"{GEN}:%7"]["text"], "new pane")
        block = drafts.poll_block(self.live, proto=1)
        self.assertEqual(block["marks"], ["dev:1.1", "gone:1.1"])
        self.assertNotIn("a gone pane's words", json.dumps(block))
        # The snooze orphan does not snooze the new pane either.
        stamped = tab_state.apply_order([{"session": "gone", "target": "gone:1.1",
                                          "key": f"{GEN}:%7"}])
        self.assertFalse(stamped[0]["snoozed"])

    def test_converter_collision_keeps_both_never_overwrites(self):
        self.seed({f"{GEN}:%1": entry("canonical words", 500.0),
                   "dev:1.2": entry("legacy words", 400.0, attachments=[ATT]),
                   "%1": entry("pane-id words", 300.0)},
                  {"snoozed": {f"{GEN}:%1": {"at": 9.0, "was_busy": True},
                               "dev:1.2": {"at": 8.0, "was_busy": False}}})
        with patch("time.time", return_value=NOW):
            drafts.ensure_converted()
            tab_state.ensure_converted()
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk[f"{GEN}:%1"], entry("canonical words", 500.0))
        self.assertEqual(self.owned(on_disk["dev:1.2"]), {**entry("legacy words", 400.0, attachments=[ATT]),
                                              "orphan": True, "orphaned_at": NOW})
        self.assertEqual(on_disk["%1"]["text"], "pane-id words")
        self.assertTrue(on_disk["%1"]["orphan"])
        self.assertEqual(drafts.get_draft("dev:1.2")["text"], "canonical words")
        snoozed = self.disk_tabs()["snoozed"]
        self.assertEqual(snoozed[f"{GEN}:%1"], {"at": 9.0, "was_busy": True})
        self.assertTrue(snoozed["dev:1.2"]["orphan"])

    def test_converter_idempotent_across_restart(self):
        self.seed({"dev:1.1": entry("one", 10.0), "gone:1.1": entry("two", 20.0)},
                  {"snoozed": {"dev:1.1": {"at": 1.0, "was_busy": False}}})
        drafts.ensure_converted()
        tab_state.ensure_converted()
        first = self.drafts_v2.read_bytes(), self.tab_v2.read_bytes()
        legacy = self.drafts_file.read_bytes(), self.tab_file.read_bytes()

        # The panes renumber and a stranger takes dev:1.1 before the restart.
        self.live[:] = [ref("dev:1.1", "%9"), ref("dev:1.2", "%0")]
        for _ in range(2):
            self.restart()
            drafts.ensure_converted()
            tab_state.ensure_converted()
        self.assertEqual((self.drafts_v2.read_bytes(), self.tab_v2.read_bytes()), first)
        self.assertEqual((self.drafts_file.read_bytes(), self.tab_file.read_bytes()), legacy)
        # The draft followed its pane, not the name.
        self.assertEqual(drafts.get_draft("dev:1.2")["text"], "one")
        self.assertEqual(drafts.get_draft("dev:1.1")["text"], "")

    def test_the_legacy_files_are_never_written(self):
        """drafts.json and tab_state.json stay exactly as the pre-7B code left
        them through conversion, saves, snoozes, a rename, a sweep, a copy, a
        dismiss and a restart: rolling back is a pure code rollback (sign-off M3 F5)."""
        self.seed({"dev:1.1": entry("one", 10.0), "gone:1.1": entry("gone", 5.0)},
                  {"pinned": ["dev"], "order": [], "snoozed": {"dev:1.1": {"at": 1.0, "was_busy": False}}})
        legacy = self.drafts_file.read_bytes(), self.tab_file.read_bytes()
        drafts.ensure_converted()
        tab_state.ensure_converted()
        drafts.set_draft("dev:1.2", text="two")
        tab_state.set_snooze("dev:1.2", True)
        tab_state.set_lists(pinned=["dev2"])
        self.live[:] = [ref("work:1.1", "%0"), ref("work:1.2", "%1"), ref("dev2:1.1", "%2")]
        drafts.rename_session("dev", "work")
        tab_state.rename_session("dev", "work")
        drafts.sweep_expired(now=time.time())
        tab_state.sweep_wakes(self.live, time.time())
        drafts.copy_orphan("gone:1.1", "dev2:1.1")
        drafts.dismiss_orphan("gone:1.1")
        self.restart()
        self.assertEqual((self.drafts_file.read_bytes(), self.tab_file.read_bytes()), legacy)
        self.assertTrue(self.drafts_v2.exists() and self.tab_v2.exists())

    def test_unparseable_legacy_file_is_left_as_is_and_a_corrupt_v2_is_rebuilt(self):
        self.drafts_file.write_text("{nope")
        with self.assertLogs("shared.drafts", "ERROR"):
            self.restart()
        self.assertTrue(drafts.ensure_converted())
        self.assertEqual(self.drafts_file.read_text(), "{nope")   # not moved, not written
        self.assertFalse(any(".corrupt-" in p.name for p in self.dir.iterdir()))
        self.assertEqual(self.disk_drafts(), {"schema": 2})
        # A v2 file that does not parse goes through the usual quarantine and
        # is rebuilt from the (untouched) legacy file.
        self.drafts_file.write_text(json.dumps({"dev:1.1": entry("one", 10.0)}))
        self.drafts_v2.write_text("{also nope")
        with self.assertLogs("shared.state", "ERROR"):
            self.restart()
        corrupt = [p for p in self.dir.iterdir() if p.name.startswith("drafts.v2.json.corrupt-")]
        self.assertEqual([p.read_text() for p in corrupt], ["{also nope"])
        self.assertTrue(drafts.ensure_converted())
        self.assertEqual(self.disk_drafts()[f"{GEN}:%0"]["text"], "one")

    def test_no_live_server_orphans_everything_and_loses_nothing(self):
        self.live.clear()
        with patch.object(drafts, "_list_panes",
                          side_effect=tmux.TmuxListError("no server running on /tmp/x")):
            self.seed({"dev:1.1": entry("one", 10.0)})
            drafts.ensure_converted()
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk["dev:1.1"]["text"], "one")
        self.assertTrue(on_disk["dev:1.1"]["orphan"])

    def test_conversion_waits_for_a_listing_that_failed_for_another_reason(self):
        self.seed({"dev:1.1": entry("one", 10.0)})
        with patch.object(drafts, "_list_panes", side_effect=tmux.TmuxListError("timeout")):
            self.assertFalse(drafts.ensure_converted())
            # Nothing is served from an unconverted store, and nothing written.
            self.assertIsNone(drafts.set_draft("dev:1.1", text="x"))
        self.assertNotIn("schema", self.disk_drafts())
        self.assertTrue(drafts.ensure_converted())
        self.assertEqual(drafts.get_draft("dev:1.1")["text"], "one")


class ClientProtocolTests(_StoreCase):
    panes = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1"), ref("dev2:1.1", "%2")]

    def setUp(self):
        super().setUp()
        import routes.drafts as drafts_routes
        import routes.tabstate as tabstate
        self.client = _app(drafts_routes.drafts_bp, tabstate.tabstate_bp)

    def test_old_and_new_client_read_seeded_drafts_before_any_save(self):
        seeded = {"dev:1.2": entry("seeded", 300.0, enter_armed=False, attachments=[ATT]),
                  "gone:1.1": entry("orphan words", 100.0)}
        self.seed(seeded)
        old = self.client.get("/api/draft?target=dev:1.2").get_json()
        new = self.client.get(f"/api/draft?target={GEN}:%251&proto=2").get_json()
        for body in (old, new):
            self.assertTrue(body["ok"])
            self.assertEqual({k: self.owned(body["draft"])[k] for k in seeded["dev:1.2"]}, seeded["dev:1.2"])
        self.assertEqual(old["target"], "dev:1.2")
        self.assertEqual(new["target"], f"{GEN}:%1")
        # The orphan is readable only through the orphan list, never by name.
        self.assertEqual(self.client.get("/api/draft?target=gone:1.1").get_json()["draft"]["text"], "")
        orphans = self.client.get("/api/drafts/orphans").get_json()["orphans"]
        self.assertEqual([(o["key"], o["text"]) for o in orphans], [("gone:1.1", "orphan words")])

    def test_old_client_save_to_a_dead_target_is_refused_not_stored(self):
        self.seed({"gone:1.1": entry("orphan words", 100.0)})
        response = self.client.put("/api/draft", json={"target": "gone:1.1", "text": "x"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "target_absent")
        self.assertEqual(self.disk_drafts()["gone:1.1"]["text"], "orphan words")
        # A delete for a dead target removes nothing either.
        self.client.delete("/api/draft", json={"target": "gone:1.1"})
        self.assertEqual(self.disk_drafts()["gone:1.1"]["text"], "orphan words")

    def test_poll_marks_and_revisions_projected_per_protocol(self):
        self.seed({"dev:1.2": entry("two", 300.0), "dev2:1.1": entry("", 200.0, enter_armed=False),
                   "gone:1.1": entry("orphan", 100.0)})
        drafts.set_draft(f"{GEN2}:%5", text="from an old generation")
        old = drafts.poll_block(self.live, proto=1)
        new = drafts.poll_block(self.live, proto=2)
        self.assertEqual(old["marks"], ["dev:1.2"])
        self.assertEqual(old["rev"], {"dev:1.2": 300.0, "dev2:1.1": 200.0})
        self.assertEqual(new["marks"], [f"{GEN}:%1"])
        self.assertEqual(new["rev"], {f"{GEN}:%1": 300.0, f"{GEN}:%2": 200.0})
        self.assertEqual(new["orphans"], 2)
        self.assertNotIn("orphans", old)
        # A listing that failed projects nothing rather than "every draft deleted".
        self.assertIsNone(drafts.poll_block(None, proto=1))

        import routes.poll as poll
        client = _app(poll.poll_bp)
        with patch("routes.poll.list_panes", return_value=self.live), \
                patch("routes.poll.drafts.poll_block", wraps=drafts.poll_block) as block, \
                patch("routes.studio.studio_poll_block", return_value=None), \
                patch.object(state, "save_idle_state"):
            client.get("/poll")
            client.get("/poll?proto=2")
        self.assertEqual([c.kwargs.get("proto") for c in block.call_args_list], [1, 2])

    def test_snooze_by_either_form_lands_on_the_key_and_projects_to_display(self):
        self.seed({}, {"snoozed": {}})
        old = self.client.post("/api/tab-state/snooze", json={"target": "dev:1.2", "on": True})
        self.assertEqual(old.get_json()["tab_state"]["snoozed"].keys(), {"dev:1.2"})
        new = self.client.post("/api/tab-state/snooze",
                               json={"target": f"{GEN}:%2", "on": True, "proto": 2})
        self.assertEqual(new.get_json()["tab_state"]["snoozed"].keys(), {"dev:1.2", "dev2:1.1"})
        self.assertEqual(self.disk_tabs()["snoozed"].keys(), {f"{GEN}:%1", f"{GEN}:%2"})
        # A wake by the display name reaches the key.
        self.client.post("/api/tab-state/snooze", json={"target": "dev:1.2", "on": False})
        self.assertEqual(self.disk_tabs()["snoozed"].keys(), {f"{GEN}:%2"})
        refused = self.client.post("/api/tab-state/snooze", json={"target": "gone:1.1", "on": True})
        self.assertEqual(refused.status_code, 409)

    def test_old_and_new_client_concurrent_drafts_snooze_pin_rename_commands_opencode(self):
        from routes import commands
        self.seed({"dev:1.1": entry("seed", 10.0)}, {"pinned": [], "order": [], "snoozed": {}})
        # Old and new browser write the same pane's draft in turn: one entry.
        self.client.put("/api/draft", json={"target": "dev:1.1", "text": "old says"})
        self.client.put("/api/draft", json={"target": f"{GEN}:%0", "text": "new says", "proto": 2})
        self.assertEqual(self.client.get("/api/draft?target=dev:1.1").get_json()["draft"]["text"],
                         "new says")
        self.client.post("/api/tab-state/snooze", json={"target": "dev:1.2", "on": True})
        self.client.patch("/api/tab-state", json={"pinned": ["dev"]})
        commands_split = patch.dict(commands._command_panes, {"dev": "%1"}, clear=True)
        commands_split.start()
        self.addCleanup(commands_split.stop)
        # The session is renamed in tmux: the panes keep their keys.
        self.live[:] = [ref("work:1.1", "%0"), ref("work:1.2", "%1"), ref("dev2:1.1", "%2")]
        from routes.terminal import on_session_renamed
        with patch("routes.terminal.rename_autoyes_session"), \
                patch("routes.terminal.state.rename_project_settings"):
            on_session_renamed("dev", "work")
        for target in ("work:1.1", f"{GEN}:%0"):
            self.assertEqual(drafts.get_draft(target)["text"], "new says")
        doc = self.client.get("/api/tab-state").get_json()["tab_state"]
        self.assertEqual(doc["pinned"], ["work"])
        self.assertEqual(list(doc["snoozed"]), ["work:1.2"])
        self.assertEqual(commands._command_panes, {"work": "%1"})
        # OpenCode takes a durable key as well as `session:w.p`.
        from shared import opencode
        # (Bound to its pane through the lookup: tests/test_opencode_output.py
        # KeyBoundContextTests, real tmux.)
        bound = tmux.PaneKeyBinding(key=f"{GEN}:%1", pane_id="%1", generation=GEN, socket_path="/s",
                                    socket_identity=(1, 2), server_pid=1, server_start_time="1",
                                    display_target="work:1.2")
        with patch.object(opencode, "bind_pane_key_status",
                          return_value=(bound, "bound")) as resolved, \
                patch.object(opencode.subprocess, "run",
                             side_effect=opencode.OpenCodeError("pane_gone", "x", 409)):
            with self.assertRaises(opencode.OpenCodeError) as caught:
                opencode.pane_context(f"{GEN}:%1")
        resolved.assert_called_once_with(f"{GEN}:%1")
        self.assertEqual(caught.exception.code, "pane_gone")
        with patch.object(opencode, "bind_pane_key_status", return_value=(None, "gone")):
            with self.assertRaises(opencode.OpenCodeError) as caught:
                opencode.pane_context(f"{GEN2}:%1")
        self.assertEqual(caught.exception.code, "pane_gone")
        with self.assertRaises(opencode.OpenCodeError) as caught:
            opencode.pane_context("not a target")
        self.assertEqual(caught.exception.code, "invalid_target")


class SessionScopedTests(_StoreCase):
    panes = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1"), ref("dev2:1.1", "%2")]

    def test_rename_before_first_draft_read_keeps_draft(self):
        self.seed({"dev:1.2": entry("typed before the upgrade", 30.0)},
                  {"snoozed": {"dev:1.2": {"at": 3.0, "was_busy": False}}})
        # Nothing has read the stores yet; tmux has already renamed the session
        # when the hook runs.
        self.assertFalse(drafts._converted)
        self.live[:] = [ref("work:1.1", "%0"), ref("work:1.2", "%1"), ref("dev2:1.1", "%2")]
        drafts.rename_session("dev", "work")
        tab_state.rename_session("dev", "work")
        self.assertEqual(drafts.get_draft("work:1.2")["text"], "typed before the upgrade")
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk[f"{GEN}:%1"]["text"], "typed before the upgrade")
        self.assertEqual(json.loads(self.drafts_file.read_text()),
                         {"dev:1.2": entry("typed before the upgrade", 30.0)})
        self.assertIn(f"{GEN}:%1", self.disk_tabs()["snoozed"])

    def test_pin_and_order_stay_session_scoped_across_rename(self):
        self.seed({}, {"pinned": ["dev"], "order": ["dev2", "dev"], "snoozed": {}})
        tab_state.ensure_converted()
        self.live[:] = [ref("work:1.1", "%0"), ref("work:1.2", "%1"), ref("dev2:1.1", "%2")]
        tab_state.rename_session("dev", "work")
        doc = self.disk_tabs()
        self.assertEqual(doc["pinned"], ["work"])
        self.assertEqual(doc["order"], ["dev2", "work"])
        self.assertFalse(any(KEY_RE.search(s) for s in doc["pinned"] + doc["order"]))
        ordered = tab_state.apply_order([
            {"session": "dev2", "target": "dev2:1.1", "key": f"{GEN}:%2"},
            {"session": "work", "target": "work:1.1", "key": f"{GEN}:%0"},
            {"session": "work", "target": "work:1.2", "key": f"{GEN}:%1"},
        ])
        self.assertEqual([p["target"] for p in ordered], ["work:1.1", "work:1.2", "dev2:1.1"])
        self.assertEqual([p["pinned"] for p in ordered], [True, True, False])

    def test_background_consumers_translate_keys(self):
        self.seed({}, {"snoozed": {}})
        with patch("time.time", return_value=NOW):
            # The activity map stays display-keyed; _is_busy reads it by key.
            state.pane_last_activity["dev:1.2"] = NOW - 5
            self.assertTrue(tab_state._is_busy(f"{GEN}:%1", NOW, panes=self.live))
            self.assertFalse(tab_state._is_busy(f"{GEN}:%0", NOW, panes=self.live))
            tab_state.set_snooze("dev:1.2", True)
        self.assertEqual(self.disk_tabs()["snoozed"][f"{GEN}:%1"]["was_busy"], True)
        # Display stamping goes through the key: the pane renumbered to dev:1.1
        # is still snoozed, the stranger now at dev:1.2 is not.
        moved = [ref("dev:1.1", "%1"), ref("dev:1.2", "%9")]
        stamped = tab_state.apply_order([
            {"session": "dev", "target": "dev:1.1", "key": f"{GEN}:%1"},
            {"session": "dev", "target": "dev:1.2", "key": f"{GEN}:%9"},
        ])
        self.assertEqual([p["snoozed"] for p in stamped], [True, False])
        self.assertEqual(list(tab_state.get_tab_state(moved)["snoozed"]), ["dev:1.1"])
        # sweep_wakes: no flip yet keeps it; the busy->quiet flip wakes it, read
        # through the key at its new display target.
        state.pane_last_activity.clear()
        state.pane_last_activity["dev:1.1"] = NOW + 100
        tab_state.sweep_wakes(moved, NOW + 110)
        self.assertIn(f"{GEN}:%1", self.disk_tabs()["snoozed"])
        tab_state.sweep_wakes(moved, NOW + 200)
        self.assertNotIn(f"{GEN}:%1", self.disk_tabs()["snoozed"])

    def test_real_poll_and_snooze_sweep_keep_unmatched_entries(self):
        self.seed({"gone:1.1": entry("orphan words", 10.0), "dev:1.1": entry("mine", 20.0)},
                  {"snoozed": {"gone:1.1": {"at": 1.0, "was_busy": False},
                               "dev:1.1": {"at": 1.0, "was_busy": False}}})
        import routes.poll as poll
        client = _app(poll.poll_bp)
        with patch("routes.poll.list_panes", return_value=self.live), \
                patch("routes.studio.studio_poll_block", return_value=None), \
                patch.object(state, "save_idle_state"):
            for path in ("/poll", "/poll?proto=2"):
                self.assertEqual(client.get(path).status_code, 200)
            drafts.sweep_expired(now=time.time())
        self.assertEqual(self.disk_drafts()["gone:1.1"]["text"], "orphan words")
        self.assertTrue(self.disk_tabs()["snoozed"]["gone:1.1"]["orphan"])
        # A failed listing sweeps no snooze at all.
        with patch("routes.poll.list_panes", side_effect=tmux.TmuxListError("boom")), \
                patch("routes.studio.studio_poll_block", return_value=None), \
                patch.object(state, "save_idle_state"):
            client.get("/poll")
        self.assertIn(f"{GEN}:%0", self.disk_tabs()["snoozed"])
        # Orphans are pruned 30 days after they were orphaned, not before.
        orphaned_at = self.disk_drafts()["gone:1.1"]["orphaned_at"]
        drafts.sweep_expired(now=orphaned_at + drafts.EXPIRY_SEC - 60)
        self.assertIn("gone:1.1", self.disk_drafts())
        tab_state.sweep_wakes(self.live, orphaned_at + drafts.EXPIRY_SEC - 60)
        self.assertIn("gone:1.1", self.disk_tabs()["snoozed"])
        drafts.sweep_expired(now=orphaned_at + drafts.EXPIRY_SEC + 60)
        self.assertNotIn("gone:1.1", self.disk_drafts())
        tab_state.sweep_wakes(self.live, orphaned_at + drafts.EXPIRY_SEC + 60)
        self.assertNotIn("gone:1.1", self.disk_tabs()["snoozed"])

    def test_orphan_copy_is_explicit_only_and_keeps_the_orphan(self):
        import routes.drafts as drafts_routes
        client = _app(drafts_routes.drafts_bp)
        self.seed({"gone:1.1": entry("orphan words", 10.0, attachments=[ATT]),
                   "dev:1.2": entry("busy here", 20.0)})
        # Nothing copies it implicitly: reads, writes and a rename leave it alone.
        drafts.get_draft("dev:1.1")
        drafts.set_draft("dev:1.1", enter_armed=False)
        drafts.rename_session("gone", "dev")
        orphan_before = self.disk_drafts()["gone:1.1"]
        self.assertTrue(orphan_before["orphan"])
        # A copy onto a pane that holds a draft is refused: nothing overwritten.
        refused = client.post("/api/drafts/copy", json={"from": "gone:1.1", "target": "dev:1.2"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()["error"], "destination_has_draft")
        # A live pane's own draft is not an orphan and cannot be copied.
        refused = client.post("/api/drafts/copy", json={"from": f"{GEN}:%1", "target": "dev:1.1"})
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()["error"], "not_orphan")
        # The explicit copy: onto a pane with only a disarmed lock.
        with patch("time.time", return_value=NOW):
            copied = client.post("/api/drafts/copy",
                                 json={"from": "gone:1.1", "target": f"{GEN}:%0", "proto": 2})
        self.assertEqual(copied.status_code, 200, copied.get_json())
        on_disk = self.disk_drafts()
        dest = on_disk[f"{GEN}:%0"]
        self.assertEqual(self.owned(dest), {**entry("orphan words", NOW, enter_armed=False,
                                                    attachments=[ATT]), "copied_from": "gone:1.1"})
        # The orphan stays: same content, same expiry clock, its own file.
        orphan = on_disk["gone:1.1"]
        self.assertEqual({k: orphan[k] for k in orphan_before}, orphan_before)
        self.assertEqual((orphan["copied_to"], orphan["copied_at"]), (f"{GEN}:%0", NOW))
        self.assertNotEqual(orphan["attachments"][0]["path"], dest["attachments"][0]["path"])
        listed = client.get("/api/drafts/orphans").get_json()["orphans"]
        self.assertEqual([(o["key"], o["text"], o["copied_at"]) for o in listed],
                         [("gone:1.1", "orphan words", NOW)])
        # Survives a restart, both of them.
        self.restart()
        self.assertEqual(drafts.get_draft(f"{GEN}:%0")["text"], "orphan words")
        self.assertEqual([o["key"] for o in drafts.list_orphans(self.live)], ["gone:1.1"])


class KeylessPaneTests(_StoreCase):
    """A listed pane with no durable key (its tmux server's start time could not
    be read, PaneRef.key None) never makes its display target a storage key:
    such a listing is treated like an unreadable one (R-P7B F2)."""

    keyed = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1")]
    keyless = [ref("dev:1.1", "%0", None), ref("dev:1.2", "%1", None)]
    panes = keyed

    def setUp(self):
        super().setUp()
        import routes.drafts as drafts_routes
        import routes.tabstate as tabstate
        self.client = _app(drafts_routes.drafts_bp, tabstate.tabstate_bp)

    def test_a_keyless_listing_defers_the_conversion(self):
        self.seed({"dev:1.1": entry("precious", 10.0)},
                  {"snoozed": {"dev:1.1": {"at": 1.0, "was_busy": False}}})
        self.live[:] = self.keyless
        self.assertFalse(drafts.ensure_converted())
        self.assertFalse(tab_state.ensure_converted())
        self.assertNotIn("schema", self.disk_drafts())
        self.assertNotIn("schema", self.disk_tabs())
        # Must-not-fire: the key comes back and the same file converts normally.
        self.live[:] = self.keyed
        self.assertTrue(drafts.ensure_converted())
        self.assertTrue(tab_state.ensure_converted())
        self.assertEqual(self.disk_drafts()[f"{GEN}:%0"]["text"], "precious")
        self.assertIn(f"{GEN}:%0", self.disk_tabs()["snoozed"])

    def test_convert_entries_flags_a_keyless_match_as_an_orphan(self):
        out = drafts.convert_entries({"dev:1.1": {"text": "x"}, "dev:1.2": {"text": "y"}},
                                     [ref("dev:1.1", "%0", None), ref("dev:1.2", "%1")], NOW)
        self.assertEqual(out["dev:1.1"], {"text": "x", "orphan": True, "orphaned_at": NOW})
        self.assertEqual(out[f"{GEN}:%1"], {"text": "y", "legacy_from": "dev:1.2"})

    def test_a_keyless_pane_is_unavailable_not_keyed_by_name(self):
        self.seed({"dev:1.1": entry("precious", 10.0)}, {"snoozed": {}})
        drafts.ensure_converted()
        tab_state.ensure_converted()
        before = self.drafts_v2.read_bytes(), self.tab_v2.read_bytes()
        self.live[:] = self.keyless
        response = self.client.put("/api/draft", json={"target": "dev:1.1", "text": "typed"})
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(self.client.get("/api/draft?target=dev:1.1").get_json()["draft"]["text"], "")
        self.assertIsNone(drafts.poll_block(self.live, proto=1))
        self.assertIsNone(drafts.poll_block(self.live, proto=2))
        self.assertEqual(self.client.get("/api/drafts/orphans").status_code, 503)
        self.assertEqual(self.client.post("/api/drafts/copy", json={"from": "x", "target": "dev:1.2"}).status_code, 503)
        self.assertEqual(self.client.post("/api/drafts/dismiss", json={"key": "x"}).status_code, 503)
        response = self.client.post("/api/tab-state/snooze", json={"target": "dev:1.2", "on": True})
        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual((self.drafts_v2.read_bytes(), self.tab_v2.read_bytes()), before)
        # Must-not-fire: with keys the same requests work, by key.
        self.live[:] = self.keyed
        self.assertEqual(self.client.put("/api/draft", json={"target": "dev:1.1", "text": "typed"}).status_code, 200)
        self.assertEqual(self.client.post("/api/tab-state/snooze", json={"target": "dev:1.2", "on": True}).status_code, 200)
        self.assertEqual(self.disk_drafts()[f"{GEN}:%0"]["text"], "typed")
        self.assertNotIn("dev:1.1", self.disk_drafts())

    def test_a_display_keyed_entry_is_never_hidden(self):
        # A schema 2 store holding a display-keyed entry with no orphan flag (what
        # a key-less pane left behind before this fix): listed, never by name.
        self.seed({"schema": 2, "dev:1.1": entry("precious", 10.0), f"{GEN}:%1": entry("live", 20.0)})
        orphans = drafts.list_orphans(self.keyed)
        self.assertEqual([(o["key"], o["reason"]) for o in orphans], [("dev:1.1", "legacy")])
        self.assertEqual(drafts.poll_block(self.keyed, proto=2)["orphans"], 1)
        self.assertEqual(drafts.get_draft("dev:1.1")["text"], "")
        # Must-not-fire: the live keyed draft is not an orphan.
        self.assertEqual(drafts.poll_block(self.keyed, proto=2)["marks"], [f"{GEN}:%1"])

    def test_snooze_sweep_and_stamping_with_a_keyless_listing(self):
        self.seed({}, {"snoozed": {}})
        tab_state.set_snooze("dev:1.2", True)
        # A key-less listing sweeps nothing (its pane would read as gone) ...
        tab_state.sweep_wakes(self.keyless, time.time() + 3600)
        self.assertIn(f"{GEN}:%1", self.disk_tabs()["snoozed"])
        # ... and stamps nothing by display target.
        stamped = tab_state.apply_order([{"session": "dev", "target": "dev:1.2", "key": None}])
        self.assertFalse(stamped[0]["snoozed"])
        self.assertEqual(tab_state.get_tab_state(self.keyless)["snoozed"], {})
        # Must-fire control: a keyed listing without the pane drops it, as before.
        tab_state.sweep_wakes([ref("dev:1.1", "%0")], time.time() + 3600)
        self.assertNotIn(f"{GEN}:%1", self.disk_tabs()["snoozed"])


class OrphaningTimeTests(_StoreCase):
    """A durable-key draft whose pane dies gets the full 30-day recovery window
    from when its death was first confirmed by a complete, successful listing,
    not from its last edit (sign-off M3 F6)."""

    panes = [ref("dev:1.1", "%0")]
    DAY = 24 * 60 * 60

    def setUp(self):
        super().setUp()
        self.dead = f"{GEN}:%1"
        self.seed({"schema": 2, self.dead: entry("KEEP AFTER DEATH", NOW - 29 * self.DAY),
                   f"{GEN}:%0": entry("live", NOW)})

    def test_first_confirmed_death_starts_the_window_and_is_never_renewed(self):
        with patch("time.time", return_value=NOW):
            block = drafts.poll_block(self.live, proto=2)
        self.assertEqual(block["orphans"], 1)
        self.assertEqual(self.disk_drafts()[self.dead]["orphaned_at"], NOW)
        # An Assist restart and later listings keep the first time.
        self.restart()
        with patch("time.time", return_value=NOW + 5 * self.DAY):
            drafts.list_orphans(self.live)
            drafts.poll_block(self.live, proto=1)
        self.assertEqual(self.disk_drafts()[self.dead]["orphaned_at"], NOW)
        # Must-not-fire: the live pane's draft is never stamped.
        self.assertNotIn("orphaned_at", self.disk_drafts()[f"{GEN}:%0"])
        # One day after death: kept (it was last edited 30 days ago).
        drafts.sweep_expired(now=NOW + self.DAY)
        self.assertIn(self.dead, self.disk_drafts())
        drafts.sweep_expired(now=NOW + drafts.EXPIRY_SEC - 60)
        self.assertIn(self.dead, self.disk_drafts())
        drafts.sweep_expired(now=NOW + drafts.EXPIRY_SEC + 60)
        self.assertNotIn(self.dead, self.disk_drafts())

    def test_a_failed_or_keyless_listing_confirms_nothing(self):
        failures = (tmux.TmuxListError("timeout"), subprocess.TimeoutExpired(["tmux"], 5))
        for failure in failures:
            with patch.object(drafts, "_list_panes", side_effect=failure):
                self.assertIsNone(drafts.poll_block(None, proto=2))
                self.assertIsNone(drafts.list_orphans())
                # The sweep cannot tell a newly dead key from a live one: it
                # leaves an unstamped durable key alone, however old.
                drafts.sweep_expired(now=NOW + 2 * self.DAY)
        self.assertIsNone(drafts.poll_block([ref("dev:1.1", "%0", None)], proto=2))
        on_disk = self.disk_drafts()
        self.assertIn(self.dead, on_disk)
        self.assertNotIn("orphaned_at", on_disk[self.dead])

    def test_the_sweep_itself_confirms_with_a_complete_listing(self):
        # No poll ran since the pane died: the sweep stamps it rather than
        # expiring it by its last edit.
        drafts.sweep_expired(now=NOW + self.DAY)
        on_disk = self.disk_drafts()
        self.assertEqual(on_disk[self.dead]["orphaned_at"], NOW + self.DAY)
        # A live pane's draft untouched for 30 days still expires as before.
        self.seed({"schema": 2, f"{GEN}:%0": entry("old live", NOW - 31 * self.DAY)})
        drafts.sweep_expired(now=NOW)
        self.assertNotIn(f"{GEN}:%0", self.disk_drafts())


class CopyTests(_StoreCase):
    """Sign-off M3 R3, the owner's decision (2026-11-16): "Move to this pane" is a
    copy. The orphan stays until it is dismissed or expires, so no later save,
    stale read, late response or clock step can lose its text, and there is no
    revision fence on saves at all."""

    panes = [ref("dev:1.1", "%1"), ref("dev:1.2", "%2")]
    KA, KB = f"{GEN}:%1", f"{GEN}:%2"

    def setUp(self):
        super().setUp()
        import routes.drafts as drafts_routes
        self.client = _app(drafts_routes.drafts_bp)
        self.seed({"lost:1.1": entry("RECOVER ME", 10.0), "dev:1.2": entry("B WORDS", 20.0)})
        self.assertTrue(drafts.ensure_converted())

    def copy(self, target=None):
        r = self.client.post("/api/drafts/copy", json={"from": "lost:1.1", "target": target or self.KA,
                                                       "proto": 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["draft"]

    def put(self, text, target=None, **extra):
        return self.client.put("/api/draft", json={"target": target or self.KA, "proto": 2,
                                                   "text": text, **extra})

    def orphan_text(self):
        return {o["key"]: o["text"] for o in drafts.list_orphans(self.live)}.get("lost:1.1")

    def test_an_empty_save_after_a_copy_clears_the_pane_and_keeps_the_orphan(self):
        self.copy()
        for text in ("", "typed elsewhere"):
            self.assertEqual(self.put(text).status_code, 200, text)
            self.assertEqual(drafts.get_draft(self.KA)["text"], text)
            self.assertEqual(self.orphan_text(), "RECOVER ME")
        self.restart()
        self.assertEqual(self.orphan_text(), "RECOVER ME")

    def test_a_clock_step_back_before_the_copy_then_a_delayed_empty_save(self):
        """R3-F3's schedule: the old fence let this delete the recovery. The
        delayed empty save now simply clears the pane; the orphan keeps it."""
        with patch.object(drafts.time, "time", return_value=1000):
            self.assertEqual(self.client.put("/api/draft", json={
                "target": self.KA, "proto": 2, "text": "", "attachments": [],
                "enter_armed": False}).status_code, 200)
        with patch.object(drafts.time, "time", return_value=999):
            self.copy()
            late = self.client.put("/api/draft", json={"target": self.KA, "proto": 2, "text": "",
                                                       "attachments": [], "enter_armed": False,
                                                       "base": 1000})
        self.assertEqual(late.status_code, 200)
        self.restart()
        self.assertEqual(drafts.get_draft(self.KA)["text"], "")
        self.assertEqual(self.orphan_text(), "RECOVER ME")

    def test_saves_are_last_writer_wins_and_a_stray_base_is_ignored(self):
        self.copy()
        # A page from before this change still sends `base`: no fence, no 409.
        for base in (0, 1, "x"):
            r = self.put(f"saved with base {base!r}", base=base)
            self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(drafts.get_draft(self.KA)["text"], "saved with base 'x'")
        self.assertNotIn("moved_at", drafts.get_draft(self.KA))

    def test_a_store_carrying_moved_at_loads_and_the_field_is_dropped(self):
        self.seed({"schema": 2, self.KA: {**entry("moved before", 50.0), "moved_from": "x:1.1",
                                          "moved_at": 50.0}})
        got = drafts.get_draft(self.KA)
        self.assertEqual((got["text"], got["moved_from"]), ("moved before", "x:1.1"))
        self.assertNotIn("moved_at", got)
        with patch.object(drafts.time, "time", return_value=10.0):   # earlier than moved_at
            self.assertEqual(self.put("", base=0).status_code, 200)
        self.assertNotIn(self.KA, self.disk_drafts())

    def test_copy_preconditions_and_a_second_copy(self):
        self.assertEqual(self.client.post("/api/drafts/copy", json={"from": "nope", "target": self.KA}).status_code, 404)
        r = self.client.post("/api/drafts/copy", json={"from": "lost:1.1", "target": "gone:9.9"})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "target_absent"))
        r = self.client.post("/api/drafts/copy", json={"from": "lost:1.1", "target": self.KB})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "destination_has_draft"))
        self.assertEqual(drafts.get_draft(self.KB)["text"], "B WORDS")
        self.copy()
        # Copying onto a pane that now holds the copy is refused like any draft;
        # once that pane is cleared the orphan can be copied again.
        r = self.client.post("/api/drafts/copy", json={"from": "lost:1.1", "target": self.KA})
        self.assertEqual(r.get_json()["error"], "destination_has_draft")
        self.put("")
        self.assertEqual(self.copy()["text"], "RECOVER ME")

    def test_dismiss_removes_only_the_orphan(self):
        self.copy()
        r = self.client.post("/api/drafts/dismiss", json={"key": self.KA})
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "not_orphan"))
        self.assertEqual(self.client.post("/api/drafts/dismiss", json={"key": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/api/drafts/dismiss", json={}).status_code, 400)
        r = self.client.post("/api/drafts/dismiss", json={"key": "lost:1.1"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(self.orphan_text())
        self.assertEqual(drafts.get_draft(self.KA)["text"], "RECOVER ME")
        self.restart()
        self.assertNotIn("lost:1.1", self.disk_drafts())
        self.assertEqual(drafts.get_draft(self.KA)["text"], "RECOVER ME")


class CopyAttachmentTests(_StoreCase):
    """A copy gives the destination its own upload file (a hard link, else a
    copy), made before the destination's metadata names it, so expiring or
    dismissing either draft never unlinks the other's file."""

    panes = [ref("dev:1.1", "%1")]
    KA = f"{GEN}:%1"

    def setUp(self):
        super().setUp()
        import uuid
        self.tag = "p7bcopy" + uuid.uuid4().hex[:10]
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in Path("/tmp").glob(f"assist_*{self.tag}*")])
        self.data = b"orphan attachment bytes"
        path = Path(f"/tmp/assist_{uuid.uuid4().hex[:8]}_{self.tag}_o.txt")
        path.write_bytes(self.data)
        self.att = {"id": "o", "name": "o.txt", "size": len(self.data), "path": str(path)}
        # A store 7B already owns: the orphan names its own v2 file.
        self.seed({"schema": 2, "lost:1.1": {**entry("with a file", 10.0, attachments=[self.att]),
                                             "orphan": True, "orphaned_at": time.time()}})

    def test_dismissing_the_orphan_leaves_the_destinations_file(self):
        copied, error = drafts.copy_orphan("lost:1.1", self.KA)
        self.assertIsNone(error)
        dest = copied["attachments"][0]["path"]
        self.assertNotEqual(dest, self.att["path"])
        self.assertTrue(drafts._is_sweepable_upload(dest))
        self.assertEqual(Path(dest).read_bytes(), self.data)
        self.assertEqual(drafts.dismiss_orphan("lost:1.1"), ([self.att["path"]], None))
        self.assertFalse(Path(self.att["path"]).exists())
        self.restart()
        self.assertEqual(Path(drafts.get_draft(self.KA)["attachments"][0]["path"]).read_bytes(), self.data)

    def test_expiring_the_destination_leaves_the_orphans_file(self):
        copied, _ = drafts.copy_orphan("lost:1.1", self.KA)
        dest = copied["attachments"][0]["path"]
        with drafts._lock:
            drafts._drafts[self.KA]["updated_at"] = 1.0
        dropped, removed = drafts.sweep_expired(now=time.time())
        self.assertEqual((dropped, removed), ([self.KA], [dest]))
        self.assertEqual(Path(drafts.list_orphans(self.live)[0]["attachments"][0]["path"]).read_bytes(), self.data)

    def test_a_vanished_orphan_file_is_left_out_of_the_copy(self):
        Path(self.att["path"]).unlink()
        copied, error = drafts.copy_orphan("lost:1.1", self.KA)
        self.assertIsNone(error)
        self.assertEqual((copied["text"], copied["attachments"]), ("with a file", []))


# The real js/drafts.js against the real draft handlers over HTTP (a werkzeug
# server on a free loopback port, backed by the scratch store). Only network
# delivery is controlled by the schedules; every copy, PUT, GET and the poll
# block are real.
_REAL_NET = r"""
const real = (url, opts = {}) => nativeFetch(BASE + url, opts);
globalThis.fetch = real;
const read = async k => (await (await real('/api/draft?' + new URLSearchParams({target: k, proto: 2}))).json()).draft.text;
const orphans = async () => Object.fromEntries((await (await real('/api/drafts/orphans')).json()).orphans.map(o => [o.key, o.text]));
const settle = () => new Promise(r => setTimeout(r, 80));
const poll = async () => { _applyDraftsData((await (await real('/test/poll')).json()).drafts); await settle(); await settle(); };
const copyOn = async k => (await real('/api/drafts/copy', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({from: 'lost:1.1', target: k, proto: 2})})).json();
const ka = KA, kb = KB;
globalThis.openOrphanDrafts = async () => {};
globalThis.confirm = () => true;
_sessionPanes = [{session: 'dev', target: 'dev:1.1', key: ka}, {session: 'dev', target: 'dev:1.2', key: kb}];
setTermTarget('dev:1.1');
_draftCache[ka] = {..._DRAFT_EMPTY};
"""


class _RealHandlersCase(_StoreCase):
    """The drafts blueprint served over real HTTP, for the production client."""

    panes = [ref("dev:1.1", "%1"), ref("dev:1.2", "%2")]
    seed_doc = {"schema": 2}

    def extra_routes(self, app):
        pass

    def setUp(self):
        super().setUp()
        import threading
        from flask import jsonify
        from werkzeug.serving import make_server
        import routes.drafts as drafts_routes
        app = Flask(__name__)
        app.register_blueprint(drafts_routes.drafts_bp)
        app.add_url_rule("/test/poll", "test_poll",
                         lambda: jsonify({"drafts": drafts.poll_block(list(self.live), proto=2)}))
        self.extra_routes(app)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.seed(self.seed_doc)
        self.assertTrue(drafts.ensure_converted())

class CopyOverRealHandlersTests(_RealHandlersCase):
    """The real client and server. After each of sign-off M3 R3's schedules
    the orphan still holds its text, whatever happened to the destination,
    and the client never replays an old answer over a newer edit."""

    seed_doc = {"lost:1.1": entry("RECOVER ME", 10.0), "dev:1.2": entry("PRECIOUS PANE B DRAFT", 20.0)}

    def run_client(self, body):
        from tests.test_pane_ref_client import SELECT_LOAD, SELECT_PRELUDE, run_js
        net = (_REAL_NET.replace("BASE", json.dumps(self.base))
               .replace("KA", json.dumps(f"{GEN}:%1")).replace("KB", json.dumps(f"{GEN}:%2")))
        return run_js(["js/pane-ref.js", "js/drafts.js"], SELECT_LOAD + net + body,
                      "globalThis.nativeFetch = globalThis.fetch;\n" + SELECT_PRELUDE)

    def test_an_ordinary_copy_shows_the_text_keeps_the_orphan_and_typing_saves(self):
        got = self.run_client(r"""
await copyOrphanDraft('lost:1.1'); await settle();
const composer = input.value;
input.value += ' EDITED'; saveDraftSoon(); await flushDraft(ka); await settle();
console.log(JSON.stringify({composer, server: await read(ka), orphans: await orphans(),
    sent: true}));
""")
        self.assertEqual(got["composer"], "RECOVER ME")
        self.assertEqual(got["server"], "RECOVER ME EDITED")
        self.assertEqual(got["orphans"], {"lost:1.1": "RECOVER ME"})
        self.restart()
        self.assertEqual(drafts.get_draft(f"{GEN}:%1")["text"], "RECOVER ME EDITED")
        self.assertEqual([o["text"] for o in drafts.list_orphans(self.live)], ["RECOVER ME"])

    def test_r3_f1_two_devices_read_then_save(self):
        """Another device copies onto A while this page types before its
        first read; the read keeps the typing, the save writes it, and the
        recovery is still the orphan's."""
        got = self.run_client(r"""
input.value = 'TYPED ON THIS DEVICE'; saveDraftSoon();
const copied = await copyOn(ka);
await poll();
await flushDraft(ka); await settle();
console.log(JSON.stringify({ok: copied.ok, composer: input.value, final: await read(ka), orphans: await orphans()}));
""")
        self.assertTrue(got["ok"])
        self.assertEqual(got["composer"], "TYPED ON THIS DEVICE")
        self.assertEqual(got["final"], "TYPED ON THIS DEVICE")
        self.assertEqual(got["orphans"], {"lost:1.1": "RECOVER ME"})

    def test_r3_f4_a_late_copy_answer_after_an_edit_or_a_clear_replays_nothing(self):
        for schedule, expected in (("edit", "RECOVER"), ("clear", "")):
            with self.subTest(schedule):
                if schedule == "clear":
                    drafts.set_draft(f"{GEN}:%1", text="")
                got = self.run_client(r"""
let releaseCopy;
globalThis.fetch = async (url, opts = {}) => {
    if (url === '/api/drafts/copy') {
        const data = await (await real(url, opts)).json();      // committed
        await new Promise(r => { releaseCopy = r; });           // answer held
        return {json: async () => data};
    }
    return real(url, opts);
};
const copying = copyOrphanDraft('lost:1.1'); await settle();
await poll();                                    // the poll reveals the copy
const seen = input.value;
if (SCHEDULE === 'edit') { input.value = 'RECOVER'; saveDraftSoon(); await flushDraft(ka); }
else { input.value = ''; _attachments = []; await clearDraftAfterSend(ka); }
await settle();
releaseCopy(); await copying; await settle(); await settle();
const after = {server: await read(ka), composer: input.value};
await flushDraft(ka); await settle();
console.log(JSON.stringify({seen, after, final: await read(ka), orphans: await orphans()}));
""".replace("SCHEDULE", json.dumps(schedule)))
                self.assertEqual(got["seen"], "RECOVER ME")
                self.assertEqual(got["after"], {"server": expected, "composer": expected})
                self.assertEqual(got["final"], expected)
                self.assertEqual(got["orphans"], {"lost:1.1": "RECOVER ME"})

    def test_a_save_dispatched_before_the_copy_cannot_lose_the_text(self):
        """R2-F1 schedule A, which the fence used to cover: the empty save
        lands after the copy and clears the pane; the orphan still has it."""
        got = self.run_client(r"""
let releasePut = null, holdNextPut = true;
globalThis.fetch = async (url, opts = {}) => {
    if (url === '/api/draft' && opts.method === 'PUT' && holdNextPut) {
        holdNextPut = false;
        await new Promise(r => { releasePut = r; });
    }
    return real(url, opts);
};
const saving = flushDraft(ka);
await tick();
await copyOrphanDraft('lost:1.1');
releasePut(); await saving; await settle();
await poll();
console.log(JSON.stringify({final: await read(ka), composer: input.value, orphans: await orphans()}));
""")
        self.assertEqual(got["final"], "")
        self.assertEqual(got["orphans"], {"lost:1.1": "RECOVER ME"})

    def test_dismiss_from_the_sheet(self):
        got = self.run_client(r"""
await copyOrphanDraft('lost:1.1'); await settle();
let asked = 0; globalThis.confirm = () => { asked++; return false; };
await dismissOrphanDraft('lost:1.1');
const kept = await orphans();
globalThis.confirm = () => { asked++; return true; };
await dismissOrphanDraft('lost:1.1');
console.log(JSON.stringify({asked, kept, after: await orphans(), pane: await read(ka)}));
""")
        self.assertEqual(got, {"asked": 2, "kept": {"lost:1.1": "RECOVER ME"}, "after": {},
                               "pane": "RECOVER ME"})


class DeadKeyOrphanTests(_StoreCase):
    """Sign-off M3 R4-F1: a `reason: gone` orphan keeps its dead pane's
    durable key, which an ordinary PUT or DELETE can still address. Only
    Dismiss and expiry remove an orphan; an ordinary write never shrinks,
    replaces or empties it. A late flush carrying different text is kept
    beside it (its own orphan), so neither text is lost."""

    panes = [ref("dev:1.1", "%1"), ref("dev:1.2", "%2")]
    KA, KB = f"{GEN}:%1", f"{GEN}:%2"

    def setUp(self):
        super().setUp()
        import routes.drafts as drafts_routes
        self.client = _app(drafts_routes.drafts_bp)
        self.seed({"schema": 2})
        self.assertTrue(drafts.ensure_converted())

    def put(self, target, **body):
        return self.client.put("/api/draft", json={"target": target, "proto": 2, **body})

    def kill_a(self, text="RECOVER THIS", attachments=()):
        """Another device saves to A, then A's pane closes."""
        r = self.put(self.KA, text=text, attachments=list(attachments))
        self.assertEqual(r.status_code, 200, r.get_json())
        self.live = [ref("dev:1.2", "%2")]
        self.assertEqual(self.orphans(), {self.KA: text})

    def orphans(self):
        return {o["key"]: o["text"] for o in drafts.list_orphans(self.live)}

    def rows(self):
        return {o["key"]: o for o in drafts.list_orphans(self.live)}

    def assert_kept(self, expected):
        self.assertEqual(self.orphans(), expected)
        self.restart()
        self.assertEqual(self.orphans(), expected)

    def test_writes_that_add_nothing_leave_the_orphan_exactly_as_it_was(self):
        self.kill_a(attachments=[ATT])
        before = self.disk_drafts()[self.KA]
        schedules = {
            "empty": {"text": "", "attachments": [], "enter_armed": True},
            "whitespace": {"text": "  \n\t", "attachments": []},
            "enter_armed_only": {"enter_armed": False},
            "same_text_fewer_files": {"text": "RECOVER THIS", "attachments": []},
            "same_text_same_files": {"text": "RECOVER THIS", "attachments": [ATT]},
            "text_only_partial": {"text": "RECOVER THIS"},
        }
        for name, body in schedules.items():
            with self.subTest(name):
                r = self.put(self.KA, **body)
                self.assertEqual((r.status_code, r.get_json()["error"]), (409, "target_absent"))
                self.assertEqual(self.disk_drafts()[self.KA], before)
        r = self.client.delete("/api/draft", json={"target": self.KA, "proto": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.disk_drafts()[self.KA], before)
        drafts.delete_draft(self.KA)
        self.assert_kept({self.KA: "RECOVER THIS"})
        self.assertEqual(self.disk_drafts()[self.KA], before)

    def test_a_differing_late_flush_is_kept_beside_the_orphan(self):
        self.kill_a()
        r = self.put(self.KA, text="RECOVER THIS AND MORE")
        self.assertEqual((r.status_code, r.get_json()["error"]), (409, "target_absent"))
        got = self.rows()
        self.assertEqual(sorted(o["text"] for o in got.values()),
                         ["RECOVER THIS", "RECOVER THIS AND MORE"])
        late = next(o for o in got.values() if o["text"] == "RECOVER THIS AND MORE")
        self.assertNotEqual(late["key"], self.KA)
        self.assertIsNone(tmux.parse_pane_key(late["key"]))   # no PUT can address it
        self.assertEqual(late["reason"], "late-save")
        self.assertIsNotNone(late["orphaned_at"])
        # The same flush again, and the orphan's own text, add nothing.
        for text in ("RECOVER THIS AND MORE", "RECOVER THIS"):
            self.assertEqual(self.put(self.KA, text=text).status_code, 409)
            self.assertEqual(len(self.orphans()), 2)
        # A late save is itself never shrunk by a later empty write.
        self.assertEqual(self.put(self.KA, text="").status_code, 409)
        # A third, different text is a third entry: never only the newer one.
        self.put(self.KA, text="third")
        self.assert_kept({self.KA: "RECOVER THIS", late["key"]: "RECOVER THIS AND MORE",
                          **{k: "third" for k, v in self.orphans().items() if v == "third"}})
        self.assertEqual(len(self.orphans()), 3)

    def test_a_late_save_does_not_inherit_the_orphans_copied_mark(self):
        """Re-judge F1: copy the orphan, then a differing late flush; the late
        row was never copied and must not say so."""
        self.kill_a()
        r = self.client.post("/api/drafts/copy", json={"from": self.KA, "target": self.KB, "proto": 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNotNone(self.rows()[self.KA]["copied_at"])
        self.put(self.KA, text="LATE FLUSH B2")
        late = next(o for o in self.rows().values() if o["text"] == "LATE FLUSH B2")
        self.assertIsNone(late.get("copied_at"))
        stored = next(v for v in self.disk_drafts().values() if isinstance(v, dict) and v.get("saved_for") == self.KA)
        for name in ("copied_to", "copied_at", "copied_from", "legacy_from", "moved_from"):
            self.assertNotIn(name, stored)

    def test_a_write_decided_live_before_the_pane_was_stamped_gone_cannot_erase_it(self):
        """Re-judge F2: a PUT/DELETE that listed A as live, then waited while A
        died and was copied, takes the lock as a dead-key write."""
        self.kill_a()
        self.rows()                                  # a complete listing stamps A gone
        self.client.post("/api/drafts/copy", json={"from": self.KA, "target": self.KB, "proto": 2})
        with mock.patch.object(drafts, "_resolve_write", return_value=(self.KA, True)):
            self.assertEqual(self.put(self.KA, text="").status_code, 409)
            drafts.delete_draft(self.KA)
        self.put(self.KB, text="")                   # the destination may be cleared
        self.assert_kept({self.KA: "RECOVER THIS"})

    def test_late_saves_are_dismissed_and_copied_like_any_orphan(self):
        self.kill_a()
        self.put(self.KA, text="LATE")
        late = next(k for k, v in self.orphans().items() if v == "LATE")
        r = self.client.post("/api/drafts/copy", json={"from": late, "target": self.KB, "proto": 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(drafts.get_draft(self.KB)["text"], "LATE")
        r = self.client.post("/api/drafts/dismiss", json={"key": late})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_kept({self.KA: "RECOVER THIS"})

    def test_a_late_flush_with_new_or_shared_files_gets_its_own(self):
        self.kill_a(attachments=[ATT])
        own_a = self.rows()[self.KA]["attachments"][0]["path"]
        self.assertEqual(own_a, ATT["path"])
        new = {"id": "u2", "name": "b.png", "size": 3, "path": f"/tmp/assist_u2_{ATT_TAG}.png"}
        Path(new["path"]).write_bytes(b"new")
        r = self.put(self.KA, text="LATE", attachments=[ATT, new])
        self.assertEqual(r.status_code, 409)
        late = next(o for o in self.rows().values() if o["text"] == "LATE")
        paths = {a["id"]: a["path"] for a in late["attachments"]}
        self.assertNotEqual(paths["u1"], own_a)               # shared → its own file
        self.assertEqual(Path(paths["u1"]).read_bytes(), ATT_BYTES)
        self.assertEqual(paths["u2"], new["path"])            # only it names this one
        # Dismissing the late save leaves the orphan's file usable, and back.
        self.client.post("/api/drafts/dismiss", json={"key": late["key"]})
        self.assertEqual(Path(own_a).read_bytes(), ATT_BYTES)
        self.assertFalse(Path(paths["u1"]).exists())

    def test_a_late_flush_whose_file_cannot_be_made_writes_nothing(self):
        self.kill_a(attachments=[ATT])
        before = self.disk_drafts()
        with patch.object(drafts, "_owned_copy",
                          side_effect=drafts.AttachmentCopyFailed(ATT["path"], OSError(28, "full"))):
            r = self.put(self.KA, text="LATE", attachments=[ATT])
        self.assertEqual((r.status_code, r.get_json()["error"]), (507, "storage_full"))
        self.assertEqual(self.disk_drafts(), before)

    def test_a_final_flush_to_a_pane_that_died_with_nothing_stored_is_kept(self):
        self.live = [ref("dev:1.2", "%2")]
        self.assertEqual(self.put(self.KA, text="").status_code, 409)
        self.assertEqual(self.orphans(), {})
        self.assertEqual(self.put(self.KA, text="FINAL WORDS").status_code, 409)
        self.assertEqual(self.put(self.KA, text="").status_code, 409)
        self.assertEqual(self.put(self.KA, text="FINAL WORDS").status_code, 409)
        self.assert_kept({self.KA: "FINAL WORDS"})
        self.assertIsNotNone(self.rows()[self.KA]["orphaned_at"])

    def test_a_key_of_another_tmux_server_generation_is_dead(self):
        old = f"{GEN2}:%2"   # same %N, restarted server
        self.put(self.KB, text="B")
        self.assertEqual(self.put(old, text="").status_code, 409)
        self.assertEqual(self.put(old, text="x").status_code, 409)
        self.assertEqual(drafts.get_draft(self.KB)["text"], "B")
        self.assertEqual(self.orphans(), {old: "x"})

    def test_a_live_panes_saves_and_deletes_are_unchanged(self):
        r = self.put(self.KB, text="hello")
        self.assertEqual((r.status_code, r.get_json()["draft"]["text"]), (200, "hello"))
        r = self.put(self.KB, text="")
        self.assertEqual((r.status_code, r.get_json()["draft"]["updated_at"]), (200, 0.0))
        self.assertNotIn(self.KB, self.disk_drafts())
        self.put(self.KB, text="again")
        r = self.client.delete("/api/draft", json={"target": self.KB, "proto": 2})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(self.KB, self.disk_drafts())
        self.put("dev:1.2", text="old client")                # display target, live
        self.assertEqual(drafts.get_draft(self.KB)["text"], "old client")
        self.assertEqual(self.orphans(), {})

    def test_an_unreadable_pane_list_answers_503_and_writes_nothing(self):
        self.kill_a()
        self.put(self.KB, text="B")
        before = self.drafts_v2.read_bytes()
        for exc in (OSError("tmux"), subprocess.TimeoutExpired("tmux", 5),
                    tmux.TmuxListError("boom") if hasattr(tmux, "TmuxListError") else OSError()):
            with self.subTest(type(exc).__name__), \
                    patch.object(drafts, "_list_panes", side_effect=exc):
                for key in (self.KA, self.KB):
                    r = self.put(key, text="")
                    self.assertEqual((r.status_code, r.get_json()["error"]), (503, "drafts_unavailable"))
                    r = self.client.delete("/api/draft", json={"target": key, "proto": 2})
                    self.assertEqual(r.status_code, 503)
        # A key-less listing is unreadable too (R-P7B F2), never "dead".
        keyless = PaneRef(pane_id="%2", generation=None, session="dev", display_target="dev:1.2")
        with patch.object(drafts, "_list_panes", return_value=[keyless]):
            self.assertEqual(self.put(self.KB, text="").status_code, 503)
            self.assertEqual(self.put(self.KA, text="x").status_code, 503)
        self.assertEqual(self.drafts_v2.read_bytes(), before)

    def test_r4_f1_copy_then_delayed_empty_saves_to_both_panes(self):
        """Astra's schedule over the real handlers: copy A's orphan onto B,
        then the delayed empty saves land on B (cleared, last writer wins)
        and on A's dead key (refused: the orphan stays), across a restart."""
        self.kill_a()
        r = self.client.post("/api/drafts/copy", json={"from": self.KA, "target": self.KB, "proto": 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        empty = {"text": "", "attachments": [], "enter_armed": True}
        self.assertEqual(self.put(self.KB, **empty).status_code, 200)
        self.assertEqual(drafts.get_draft(self.KB)["text"], "")
        self.assertEqual(self.put(self.KA, **empty).status_code, 409)
        self.assert_kept({self.KA: "RECOVER THIS"})

    def test_an_import_never_stores_an_orphan_under_a_durable_key(self):
        """A new page saved a durable key to an older Assist (advisory 6684's
        window); its import must not land where the live pane's own saves
        go, or the next ordinary save would rewrite the orphan."""
        self.drafts_file.write_text(json.dumps({self.KB: entry("WRITTEN DURING ROLLBACK", 99.0)}))
        self.restart()
        rows = self.rows()
        self.assertEqual([o["text"] for o in rows.values()], ["WRITTEN DURING ROLLBACK"])
        key = next(iter(rows))
        self.assertNotEqual(key, self.KB)
        self.assertIsNone(tmux.parse_pane_key(key))
        self.put(self.KB, text="live pane typing")
        self.put(self.KB, text="")
        self.assert_kept({key: "WRITTEN DURING ROLLBACK"})

    def test_a_stored_orphan_under_a_durable_key_is_moved_off_it_at_load(self):
        self.seed({"schema": 2, self.KB: {**entry("FLAGGED", 5.0), "orphan": True,
                                          "orphaned_at": 5.0, "orphan_reason": "legacy-after-rollback",
                                          "legacy_from": self.KB}})
        rows = self.rows()
        self.assertEqual([o["text"] for o in rows.values()], ["FLAGGED"])
        key = next(iter(rows))
        self.assertNotEqual(key, self.KB)
        self.assertEqual(rows[key]["orphaned_at"], 5.0)
        self.assertEqual(drafts.get_draft(self.KB)["text"], "")
        self.put(self.KB, text="mine")
        self.put(self.KB, text="")
        self.assert_kept({key: "FLAGGED"})


class DeadKeyOverRealHandlersTests(_RealHandlersCase):
    """R4-F1 with the production client (js/pane-ref.js + js/drafts.js) and
    real handlers over HTTP: two empty flushes are held, another device saves
    to A, A closes, this page copies A's orphan onto B, then both held
    flushes land."""

    def test_held_empty_flushes_to_b_and_to_as_dead_key(self):
        got = self.run_client(r"""
_draftCache[kb] = {..._DRAFT_EMPTY};
const held = [];
globalThis.fetch = async (url, opts = {}) => {
    if (url === '/api/draft' && opts.method === 'PUT' && held.length < 2) {
        await new Promise(r => held.push(r));
    }
    return real(url, opts);
};
const fa = flushDraft(ka), fb = flushDraft(kb);
await tick(); await tick();
await real('/test/other-device-save', {method: 'POST'});           // text to A, A closes
_sessionPanes = [{session: 'dev', target: 'dev:1.2', key: kb}];
setTermTarget('dev:1.2');
input.value = ''; _attachments = [];
await real('/api/draft', {method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({target: kb, proto: 2, text: ''})});          // B starts empty
await copyOrphanDraft(ka); await settle();
const composer = input.value;
held[1](); await fb; await settle();
const afterB = await orphans();
held[0](); await fa; await settle();
console.log(JSON.stringify({composer, afterB, final: await orphans(), b: await read(kb)}));
""")
        self.assertEqual(got["composer"], "SO3R4 RECOVER THIS TEXT")
        self.assertEqual(got["afterB"][f"{GEN}:%1"], "SO3R4 RECOVER THIS TEXT")
        self.assertEqual(got["final"][f"{GEN}:%1"], "SO3R4 RECOVER THIS TEXT")
        self.assertEqual(got["b"], "")
        self.restart()
        self.assertEqual({o["key"]: o["text"] for o in drafts.list_orphans(self.live)}[f"{GEN}:%1"],
                         "SO3R4 RECOVER THIS TEXT")

    run_client = CopyOverRealHandlersTests.run_client

    def extra_routes(self, app):
        def other_device():
            # Another device saves to A, then A's pane closes.
            drafts.set_draft(f"{GEN}:%1", text="SO3R4 RECOVER THIS TEXT")
            self.live = [ref("dev:1.2", "%2")]
            return "", 204
        app.add_url_rule("/test/other-device-save", "other_device", other_device, methods=["POST"])


def _pre7b(case, name):
    """The real pre-7B module (6089e0b, tests/fixtures), loading its own file
    from the case's scratch dir, as it did at start."""
    import importlib.util
    path = ROOT / "tests" / "fixtures" / f"pre7b_{name}.py"
    spec = importlib.util.spec_from_file_location(f"pre7b_{name}", path)
    module = importlib.util.module_from_spec(spec)
    with patch.object(state, "DATA_DIR", case.dir):
        spec.loader.exec_module(module)
    return module


class _Crash(BaseException):
    """The process dies here."""


class _UploadCase(_StoreCase):
    """Real uploads in /tmp (the only place either sweeper unlinks), each
    named with a tag this case removes."""

    panes = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1")]
    K0 = f"{GEN}:%0"

    def setUp(self):
        super().setUp()
        import uuid
        self.tag = "p7bown" + uuid.uuid4().hex[:10]
        self.addCleanup(self._remove_tagged)

    def _remove_tagged(self):
        for p in Path("/tmp").glob(f"assist_*{self.tag}*"):
            p.unlink(missing_ok=True)

    def upload(self, label, data=b"user attachment bytes"):
        """A real upload the way routes/input.py names one."""
        import uuid
        path = Path(f"/tmp/assist_{uuid.uuid4().hex[:8]}_{self.tag}_{label}.txt")
        path.write_bytes(data)
        return {"id": label, "name": f"{label}.txt", "size": len(data), "path": str(path)}

    def assert_owned(self, path, original, data=b"user attachment bytes"):
        self.assertNotEqual(path, original["path"])
        self.assertTrue(drafts._is_sweepable_upload(path), path)
        self.assertEqual(Path(path).read_bytes(), data)


class AttachmentOwnershipTests(_UploadCase):
    """Sign-off M3 R2-F2: drafts.json and drafts.v2.json never share an upload
    file. v2 gets its own name for every upload the legacy store references (a
    hard link, else a copy), created before any v2 metadata names it, so the
    unmodified pre-7B sweeper unlinking its own expired reference leaves the v2
    draft's file usable, and the new sweeper never unlinks a path drafts.json
    still references. Real files in /tmp."""

    def legacy_paths(self):
        doc = json.loads(self.drafts_file.read_text(encoding="utf-8"))
        return {a["path"] for e in doc.values() for a in e["attachments"]}

    def v2_paths(self):
        doc = json.loads(self.drafts_v2.read_text(encoding="utf-8"))
        return {a["path"] for e in doc["drafts"].values() for a in e["attachments"]}

    def test_conversion_gives_v2_its_own_file_and_leaves_drafts_json_alone(self):
        att = self.upload("conv")
        self.seed({"dev:1.1": entry("with a file", 10.0, attachments=[att]),
                   "gone:1.1": entry("orphan with the same file", 9.0, attachments=[att])})
        legacy_raw = self.drafts_file.read_bytes()
        self.assertTrue(drafts.ensure_converted())
        v2 = self.disk_drafts()
        own = v2[self.K0]["attachments"][0]
        self.assert_owned(own["path"], att)
        self.assertEqual({k: own[k] for k in ("id", "name", "size")},
                         {k: att[k] for k in ("id", "name", "size")})
        # The same file attached twice stays one v2 file, as it was one file.
        self.assertEqual(v2["gone:1.1"]["attachments"][0]["path"], own["path"])
        self.assertFalse(self.v2_paths() & self.legacy_paths())
        self.assertTrue(Path(att["path"]).exists())
        self.assertEqual(self.drafts_file.read_bytes(), legacy_raw)

    def test_the_old_sweeper_expiring_its_reference_leaves_the_v2_file(self):
        now = time.time()
        att = self.upload("rollback")
        self.seed({"dev:1.1": entry("legacy draft", now - 29 * 86400, attachments=[att])})
        self.assertTrue(drafts.ensure_converted())
        drafts.set_draft(self.K0, text="recently edited v2 draft")
        # The code rollback: the pre-7B sweeper expires its 31-day-old entry
        # and unlinks the file only it references now.
        old = _pre7b(self, "drafts")
        self.assertEqual(old.get_draft("dev:1.1")["text"], "legacy draft")
        dropped, removed = old.sweep_expired(now=now + 2 * 86400)
        self.assertEqual((dropped, removed), (["dev:1.1"], [att["path"]]))
        self.assertFalse(Path(att["path"]).exists())
        # The re-upgrade: the fresh v2 draft's attachment is still there.
        self.restart()
        saved = drafts.get_draft(self.K0)
        self.assertEqual(saved["text"], "recently edited v2 draft")
        self.assert_owned(saved["attachments"][0]["path"], att)

    def test_the_new_sweeper_never_unlinks_a_path_drafts_json_references(self):
        now = time.time()
        att = self.upload("reverse")
        self.seed({"dev:1.1": entry("legacy draft", now, attachments=[att])})
        self.assertTrue(drafts.ensure_converted())
        own = self.disk_drafts()[self.K0]["attachments"][0]["path"]
        # The v2 entry expires: its own file goes, the legacy file stays and
        # the pre-7B code still reads its draft with the file there.
        dropped, removed = drafts.sweep_expired(max_age_sec=-1, now=now)
        self.assertEqual((dropped, removed), ([self.K0], [own]))
        self.assertTrue(Path(att["path"]).exists())
        self.assertEqual(_pre7b(self, "drafts").get_draft("dev:1.1")["attachments"][0]["path"], att["path"])
        # Even a v2 entry naming the legacy path itself (put there by hand)
        # never makes the new sweeper unlink a file drafts.json references.
        with drafts._lock:
            drafts._drafts["gone:9.9"] = {**entry("by hand", 1.0, attachments=[att]),
                                          "orphan": True, "orphaned_at": 1.0}
        dropped, removed = drafts.sweep_expired(now=now)
        self.assertEqual((dropped, removed), (["gone:9.9"], []))
        self.assertTrue(Path(att["path"]).exists())

    def test_drafts_an_older_assist_wrote_are_imported_with_their_own_files(self):
        now = time.time()
        self.seed({"dev:1.1": entry("before", now)})
        self.assertTrue(drafts.ensure_converted())
        att = self.upload("during")
        old = _pre7b(self, "drafts")
        old.set_draft("new:1.1", text="written during the rollback", attachments=[att])
        self.restart()
        imported = self.disk_drafts()["new:1.1"]
        self.assertEqual(imported["orphan_reason"], "legacy-after-rollback")
        self.assert_owned(imported["attachments"][0]["path"], att)
        self.assertFalse(self.v2_paths() & self.legacy_paths())
        # A second rollback's sweep of that legacy entry leaves the orphan's file.
        old = _pre7b(self, "drafts")
        old.sweep_expired(max_age_sec=-1, now=now)
        self.assertFalse(Path(att["path"]).exists())
        self.restart()
        self.assert_owned(drafts.list_orphans(self.live)[0]["attachments"][0]["path"], att)

    def test_a_store_already_sharing_a_path_and_an_old_client_save_get_their_own_files(self):
        now = time.time()
        att, sent = self.upload("shared"), self.upload("sent")
        # A v2 store written before this fix shares the legacy path.
        self.drafts_file.write_text(json.dumps({"dev:1.1": entry("legacy", now, attachments=[att]),
                                                "dev:1.2": entry("other", now, attachments=[sent])}))
        digest = __import__("hashlib").sha256(self.drafts_file.read_bytes()).hexdigest()
        self.drafts_v2.write_text(json.dumps({
            "schema": 2, "source": {"sha256": digest, "entries": {"dev:1.1": now, "dev:1.2": now}},
            "drafts": {self.K0: {**entry("legacy", now, attachments=[att]), "legacy_from": "dev:1.1"}}}))
        self.restart()
        self.assert_owned(self.disk_drafts()[self.K0]["attachments"][0]["path"], att)
        # An old cached page re-sends a tray holding a legacy path.
        saved = drafts.set_draft("dev:1.2", text="old page", attachments=[sent])
        self.assert_owned(saved["attachments"][0]["path"], sent)
        self.assertFalse(self.v2_paths() & self.legacy_paths())

    def test_a_crash_before_the_metadata_leaves_only_an_unreferenced_file(self):
        att = self.upload("crash")
        self.seed({"dev:1.1": entry("legacy draft", time.time(), attachments=[att])})
        real_write = state.atomic_write_json

        def checked(path, data, **kw):
            # Whenever v2 metadata is published, every file it names exists.
            if path == self.drafts_v2:
                for e in data["drafts"].values():
                    for a in e["attachments"]:
                        self.assertTrue(Path(a["path"]).exists(), a["path"])
            return real_write(path, data, **kw)

        def crash(path, data, **kw):
            raise _Crash()

        with patch.object(state, "atomic_write_json", side_effect=crash):
            with self.assertRaises(_Crash):
                drafts.ensure_converted()
        self.assertFalse(self.drafts_v2.exists())
        strays = [p for p in Path("/tmp").glob(f"assist_*{self.tag}_crash.txt") if str(p) != att["path"]]
        self.assertEqual(len(strays), 1)      # at most an unreferenced file
        # The next start converts again; the published file exists.
        self.restart()
        with patch.object(state, "atomic_write_json", side_effect=checked):
            self.assertTrue(drafts.ensure_converted())
        own = self.disk_drafts()[self.K0]["attachments"][0]["path"]
        self.assert_owned(own, att)
        self.assertNotIn(str(strays[0]), self.v2_paths() | self.legacy_paths())

    def test_a_missing_upload_is_dropped_and_a_foreign_path_is_never_copied(self):
        gone = {"id": "g", "name": "g.txt", "size": 1, "path": f"/tmp/assist_00000000_{self.tag}_gone.txt"}
        foreign = {"id": "f", "name": "f.txt", "size": 1, "path": str(self.dir / "not-an-upload.txt")}
        (self.dir / "not-an-upload.txt").write_bytes(b"x")
        self.seed({"dev:1.1": entry("files", time.time(), attachments=[gone, foreign])})
        with self.assertLogs("shared.drafts", "WARNING"):
            self.assertTrue(drafts.ensure_converted())
        # Gone: nothing to own, so no v2 reference to a missing file. Foreign:
        # neither sweeper can unlink it, and copying it would read any path.
        self.assertEqual(self.disk_drafts()[self.K0]["attachments"], [foreign])
        self.assertEqual(list(Path("/tmp").glob(f"assist_*{self.tag}*")), [])


class _UploadFault:
    """ENOSPC (or another errno) where an upload file is made, and nowhere
    else: os.link, and os.open / the copy's writes for a /tmp/assist_* path.
    Metadata writes (drafts.v2.json, in the case's scratch dir) stay real."""

    def __init__(self, mode="link+open", err=None, named=False):
        import errno as _errno
        self.mode = mode
        self.err = _errno.ENOSPC if err is None else err
        self.named = named          # name the path, as a real OSError does

    def _fault(self, path=None):
        if self.named and path is not None:
            return OSError(self.err, os.strerror(self.err), str(path))
        return OSError(self.err, os.strerror(self.err) + " (injected)")

    def __enter__(self):
        import errno as _errno
        real_open, real_fdopen = os.open, os.fdopen
        fault = self._fault

        def is_upload(path):
            return str(path).startswith("/tmp/assist_")

        def link(src, dst, **kw):
            raise fault(dst) if self.mode == "link+open" else OSError(_errno.EXDEV, "cross-device (injected)")

        def open_(path, *a, **kw):
            if self.mode == "link+open" and is_upload(path):
                raise fault(path)
            return real_open(path, *a, **kw)

        class _Full:
            def __init__(self, f):
                self.f = f
            def write(self, data):
                raise fault()
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                self.f.close()

        def fdopen(fd, *a, **kw):
            f = real_fdopen(fd, *a, **kw)
            if self.mode == "exdev+write" and is_upload(os.readlink(f"/proc/self/fd/{fd}")):
                return _Full(f)
            return f

        self.patches = [patch.object(os, "link", side_effect=link),
                        patch.object(os, "open", side_effect=open_),
                        patch.object(os, "fdopen", side_effect=fdopen)]
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class DismissKeepsSharedFilesTests(_UploadCase):
    """Review N2: Dismiss unlinks only files no other draft names and
    drafts.json does not reference."""

    def test_a_file_a_live_draft_also_names_survives_dismiss(self):
        att = self.upload("shared")
        # One upload attached twice: conversion makes it one v2 file, named
        # by the live pane's draft and by the orphan.
        self.seed({"dev:1.1": entry("live", 10.0, attachments=[att]),
                   "gone:1.1": entry("orphan", 9.0, attachments=[att])})
        self.assertTrue(drafts.ensure_converted())
        own = self.disk_drafts()[self.K0]["attachments"][0]["path"]
        self.assertEqual(self.disk_drafts()["gone:1.1"]["attachments"][0]["path"], own)
        self.assertEqual(drafts.dismiss_orphan("gone:1.1", self.live), ([], None))
        self.assertTrue(Path(own).exists())
        self.assertEqual(drafts.get_draft(self.K0)["attachments"][0]["path"], own)

    def test_a_file_drafts_json_references_survives_dismiss(self):
        att = self.upload("legacy")
        self.seed({"schema": 2, "lost:1.1": {**entry("orphan", 10.0, attachments=[att]),
                                             "orphan": True, "orphaned_at": time.time()}})
        self.restart()
        self.assertTrue(drafts.ensure_converted())
        # A load or re-import relinks a path both stores name, so this guard is
        # defence in depth: drafts.json, read at dismiss time, names the
        # orphan's own file (no import runs in between).
        legacy = {"old:1.1": entry("rollback-era legacy draft", 9.0, attachments=[att])}
        with mock.patch.object(drafts, "_read_legacy_quiet", return_value=legacy):
            removed, error = drafts.dismiss_orphan("lost:1.1", self.live)
        self.assertIsNone(error)
        self.assertNotIn(att["path"], removed)
        self.assertTrue(Path(att["path"]).exists())


class IOErrorKeepsAttachmentTests(_UploadCase):
    """Sign-off M3 R3-F2: only confirmed absence (ENOENT/ENOTDIR on the
    source, or not a regular file) drops an attachment. Any other error while
    linking or copying is retryable: nothing is published, the legacy file and
    the import snapshot are left as they were, a save or a copy answers an
    error with the previous state intact, and the retry after the fault is
    gone gives v2 its own file with identical bytes."""

    MODES = ("link+open", "exdev+write")

    def strays(self, *known):
        return sorted(str(p) for p in Path("/tmp").glob(f"assist_*{self.tag}*") if str(p) not in known)

    def test_conversion_publishes_nothing_until_the_files_can_be_made(self):
        import routes.drafts as drafts_routes
        client = _app(drafts_routes.drafts_bp)
        for mode in self.MODES:
            with self.subTest(mode):
                self.drafts_v2.unlink(missing_ok=True)
                att = self.upload("conv-" + mode.replace("+", ""))
                self.seed({"dev:1.1": entry("with a file", 10.0, attachments=[att])})
                legacy = self.drafts_file.read_bytes()
                existing = self.strays()
                with _UploadFault(mode):
                    with self.assertLogs("shared.drafts", "ERROR") as logs:
                        self.assertFalse(drafts.ensure_converted())
                    with self.assertNoLogs("shared.drafts", "ERROR"):   # logged once
                        self.assertFalse(drafts.ensure_converted())
                    r = client.get(f"/api/draft?target={self.K0}&proto=2")
                    self.assertEqual((r.status_code, r.get_json()["error"]), (503, "drafts_unavailable"))
                    self.assertIsNone(drafts.poll_block(self.live, proto=2))
                self.assertIn("No space", "\n".join(logs.output))
                self.assertFalse(self.drafts_v2.exists())
                self.assertEqual(self.drafts_file.read_bytes(), legacy)
                self.assertEqual(self.strays(*existing), [])        # a half-made copy is removed
                self.restart()                                       # and a restart is no different
                self.assertTrue(drafts.ensure_converted())
                self.assert_owned(self.disk_drafts()[self.K0]["attachments"][0]["path"], att)
                self.assertEqual(self.drafts_file.read_bytes(), legacy)

    def test_a_retry_logs_once_though_each_error_names_a_fresh_destination(self):
        """Review N1/N3: a real OSError names the random destination it tried,
        so the log dedupes on the source path and errno, and the 503 says
        which upload holds the store back."""
        import routes.drafts as drafts_routes
        client = _app(drafts_routes.drafts_bp)
        att = self.upload("named")
        self.seed({"dev:1.1": entry("with a file", 10.0, attachments=[att])})
        with _UploadFault(named=True):
            with self.assertLogs("shared.drafts", "ERROR"):
                self.assertFalse(drafts.ensure_converted())
            with self.assertNoLogs("shared.drafts", "ERROR"):
                for _ in range(3):
                    self.assertFalse(drafts.ensure_converted())
            body = client.get(f"/api/draft?target={self.K0}&proto=2").get_json()
            self.assertEqual((body["error"], body["reason"], body["path"], body["errno"]),
                             ("drafts_unavailable", "attachment_copy_failed", att["path"], "ENOSPC"))
        self.assertTrue(drafts.ensure_converted())
        self.assertIsNone(drafts.retry_reason())

    def test_a_rollback_era_import_stays_pending_until_its_files_can_be_made(self):
        now = time.time()
        self.seed({"dev:1.1": entry("before", now)})
        self.assertTrue(drafts.ensure_converted())
        att = self.upload("during")
        _pre7b(self, "drafts").set_draft("new:1.1", text="written during the rollback", attachments=[att])
        v2_before = self.drafts_v2.read_bytes()
        with _UploadFault():
            with self.assertLogs("shared.drafts", "ERROR"):
                self.restart()
            # The store still serves what it had; nothing new is published.
            self.assertTrue(drafts.ensure_converted())
            self.assertEqual(drafts.get_draft(self.K0)["text"], "before")
            self.assertEqual(drafts.list_orphans(self.live), [])
            self.assertEqual(self.drafts_v2.read_bytes(), v2_before)
            drafts._retry_at = 0.0                      # the next retry is due, and fails too
            with self.assertNoLogs("shared.drafts", "ERROR"):
                drafts.ensure_converted()
            self.assertEqual(self.drafts_v2.read_bytes(), v2_before)
        self.assertEqual(self.strays(att["path"]), [])
        # Without a restart: the next due retry imports it, with its own file.
        drafts._retry_at = 0.0
        self.assertTrue(drafts.ensure_converted())
        imported = self.disk_drafts()["new:1.1"]
        self.assertEqual(imported["orphan_reason"], "legacy-after-rollback")
        self.assert_owned(imported["attachments"][0]["path"], att)
        # Consumed once: a restart imports nothing twice.
        after = self.drafts_v2.read_bytes()
        self.restart()
        self.assertEqual(self.drafts_v2.read_bytes(), after)

    def test_an_old_page_resend_is_refused_and_keeps_the_owned_file(self):
        import errno
        import routes.drafts as drafts_routes
        client = _app(drafts_routes.drafts_bp)
        att = self.upload("resend")
        self.seed({"dev:1.1": entry("draft", time.time(), attachments=[att])})
        self.assertTrue(drafts.ensure_converted())
        before, disk_before = drafts.get_draft(self.K0), self.drafts_v2.read_bytes()
        owned = before["attachments"][0]["path"]
        for mode, err, status, code in (("link+open", errno.ENOSPC, 507, "storage_full"),
                                        ("exdev+write", errno.EIO, 500, "attachment_copy_failed")):
            with self.subTest(mode), _UploadFault(mode, err):
                r = client.put("/api/draft", json={"target": "dev:1.1", "text": "edited", "attachments": [att]})
                self.assertEqual((r.status_code, r.get_json()["error"]), (status, code))
            self.assertEqual(drafts.get_draft(self.K0), before)
            self.assertEqual(self.drafts_v2.read_bytes(), disk_before)
            self.assertEqual(self.strays(att["path"], owned), [])
        self.restart()
        self.assertEqual(drafts.get_draft(self.K0)["attachments"], before["attachments"])
        # The retry succeeds, with the same bytes.
        r = client.put("/api/draft", json={"target": "dev:1.1", "text": "edited", "attachments": [att]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_owned(r.get_json()["draft"]["attachments"][0]["path"], att)

    def test_a_copy_is_refused_and_writes_nothing(self):
        import errno
        import routes.drafts as drafts_routes
        client = _app(drafts_routes.drafts_bp)
        att = self.upload("orphan")
        self.seed({"gone:1.1": entry("orphan words", time.time(), attachments=[att])})
        self.assertTrue(drafts.ensure_converted())
        disk_before = self.drafts_v2.read_bytes()
        orphan_file = self.disk_drafts()["gone:1.1"]["attachments"][0]["path"]
        for mode, err, status, code in (("link+open", errno.ENOSPC, 507, "storage_full"),
                                        ("exdev+write", errno.EACCES, 500, "attachment_copy_failed")):
            with self.subTest(mode), _UploadFault(mode, err):
                r = client.post("/api/drafts/copy", json={"from": "gone:1.1", "target": self.K0, "proto": 2})
                self.assertEqual((r.status_code, r.get_json()["error"]), (status, code))
            self.assertEqual(self.drafts_v2.read_bytes(), disk_before)
            self.assertEqual(self.strays(att["path"], orphan_file), [])
        r = client.post("/api/drafts/copy", json={"from": "gone:1.1", "target": self.K0, "proto": 2})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_owned(r.get_json()["draft"]["attachments"][0]["path"], att)
        self.assertNotEqual(r.get_json()["draft"]["attachments"][0]["path"], orphan_file)

    def test_a_stat_error_is_retryable_not_absence(self):
        import errno
        att = self.upload("stat")
        real_lstat = os.lstat

        def lstat(path, *a, **kw):
            if str(path) == att["path"]:
                raise PermissionError(errno.EACCES, "denied (injected)")
            return real_lstat(path, *a, **kw)

        with patch.object(os, "lstat", side_effect=lstat):
            with self.assertRaises(drafts.AttachmentCopyFailed) as caught:
                drafts._owned_copy(att["path"])
        self.assertEqual((caught.exception.error, caught.exception.status), ("attachment_copy_failed", 500))

    def test_a_copy_failing_on_its_second_file_removes_the_first(self):
        import errno
        one, two = self.upload("one"), self.upload("two")
        self.seed({"schema": 2, "gone:1.1": {**entry("two files", time.time(), attachments=[one, two]),
                                             "orphan": True, "orphaned_at": time.time()}})
        disk_before = self.drafts_v2.read_bytes()
        real_copy, made = drafts._owned_copy, []

        def second_fails(path):
            if path == two["path"]:
                raise drafts.AttachmentCopyFailed(path, OSError(errno.ENOSPC, "full (injected)"))
            made.append(real_copy(path))
            return made[-1]

        with patch.object(drafts, "_owned_copy", side_effect=second_fails):
            self.assertEqual(drafts.copy_orphan("gone:1.1", self.K0), (None, "storage_full"))
        self.assertEqual(len(made), 1)
        self.assertFalse(Path(made[0]).exists())
        self.assertEqual(self.drafts_v2.read_bytes(), disk_before)
        self.assertEqual(self.strays(one["path"], two["path"]), [])

    def test_confirmed_absence_is_still_dropped(self):
        """Must-fire control: a source that is genuinely gone, or not a regular
        file, is dropped from the save as before, and the save succeeds."""
        att, linked = self.upload("gone"), self.upload("symlinked")
        self.seed({"dev:1.1": entry("draft", time.time())})
        self.assertTrue(drafts.ensure_converted())
        drafts._legacy_paths = {att["path"], linked["path"]}
        Path(att["path"]).unlink()
        Path(linked["path"]).unlink()
        Path(linked["path"]).symlink_to(self.dir / "elsewhere")
        with self.assertLogs("shared.drafts", "WARNING"):
            saved = drafts.set_draft(self.K0, text="kept", attachments=[att, linked])
        self.assertEqual((saved["text"], saved["attachments"]), ("kept", []))
        self.assertEqual(drafts._owned_copy(f"/tmp/assist_00000000_{self.tag}_nodir/x"), None)


class RollbackLifecycleTests(_StoreCase):
    """Upgrade, a code rollback to the pre-7B Assist, and a re-upgrade (sign-off
    M3 F5, the owner's decision on question 7039). The pre-7B code is the real
    shared/drafts.py and shared/tab_state.py from 6089e0b, checked in under
    tests/fixtures so this needs no git history."""

    panes = [ref("dev:1.1", "%0"), ref("dev:1.2", "%1")]

    def old_code(self, name):
        import importlib.util
        path = ROOT / "tests" / "fixtures" / f"pre7b_{name}.py"
        spec = importlib.util.spec_from_file_location(f"pre7b_{name}", path)
        module = importlib.util.module_from_spec(spec)
        with patch.object(state, "DATA_DIR", self.dir):
            spec.loader.exec_module(module)  # loads its own file, as it did at start
        return module

    def test_upgrade_rollback_reupgrade_keeps_everything_and_assigns_nothing_by_name(self):
        k0, k1 = f"{GEN}:%0", f"{GEN}:%1"
        self.seed({"dev:1.1": entry("BEFORE UPGRADE", 10.0), "gone:1.1": entry("GONE BEFORE", 5.0)},
                  {"pinned": ["dev"], "order": [], "sort": "manual",
                   "snoozed": {"dev:1.2": {"at": 1.0, "was_busy": False}}, "agent_declarations": {}})
        # 1. The upgrade: v2 files made, the legacy files untouched.
        legacy_before = self.drafts_file.read_bytes(), self.tab_file.read_bytes()
        self.assertTrue(drafts.ensure_converted())
        self.assertTrue(tab_state.ensure_converted())
        drafts.set_draft("dev:1.2", text="POST UPGRADE")
        tab_state.set_snooze("dev:1.1", True)
        self.assertEqual((self.drafts_file.read_bytes(), self.tab_file.read_bytes()), legacy_before)

        # 2. A code rollback: the pre-7B Assist reads, writes, polls and sweeps
        # its own files, which it finds exactly as it left them.
        old_drafts, old_tabs = self.old_code("drafts"), self.old_code("tab_state")
        self.assertEqual(old_drafts.get_draft("dev:1.1")["text"], "BEFORE UPGRADE")
        self.assertEqual(old_drafts.get_draft("gone:1.1")["text"], "GONE BEFORE")
        old_drafts.set_draft("dev:1.1", text="TYPED DURING ROLLBACK")
        old_drafts.set_draft("gone:1.1", text="GONE CHANGED")
        old_drafts.set_draft("new:1.1", text="NEW DURING ROLLBACK")
        old_drafts.poll_block()
        old_drafts.sweep_expired()
        old_tabs.set_snooze("new:1.1", True)
        old_tabs.set_lists(pinned=["new"])
        old_tabs.sweep_wakes({"dev:1.1", "new:1.1"})
        legacy_after_rollback = self.drafts_file.read_bytes(), self.tab_file.read_bytes()

        # 3. The re-upgrade, after a tmux restart: new panes at the old names.
        self.live[:] = [ref("dev:1.1", "%0", GEN2), ref("new:1.1", "%1", GEN2)]
        self.restart()
        v2 = self.disk_drafts()
        # Post-upgrade v2 drafts intact (their panes are gone now: orphans).
        self.assertEqual(v2[k0]["text"], "BEFORE UPGRADE")
        self.assertEqual(v2[k1]["text"], "POST UPGRADE")
        # What the old code wrote after the upgrade: orphans, by legacy name,
        # never over a v2 entry (gone:1.1 already was an orphan).
        self.assertEqual(v2["gone:1.1"]["text"], "GONE BEFORE")
        for key, text in (("dev:1.1", "TYPED DURING ROLLBACK"),
                          ("gone:1.1 (after rollback)", "GONE CHANGED"),
                          ("new:1.1", "NEW DURING ROLLBACK")):
            self.assertEqual(v2[key]["text"], text, key)
            self.assertTrue(v2[key]["orphan"], key)
            self.assertEqual(v2[key]["orphan_reason"], "legacy-after-rollback")
        # Nothing is assigned by name to the new panes.
        for target in ("dev:1.1", "new:1.1", f"{GEN2}:%0", f"{GEN2}:%1"):
            self.assertEqual(drafts.get_draft(target)["text"], "", target)
        self.assertEqual(drafts.poll_block(self.live, proto=1)["marks"], [])
        reasons = {o["key"]: o["reason"] for o in drafts.list_orphans()}
        self.assertEqual(reasons, {k0: "gone", k1: "gone", "gone:1.1": "legacy",
                                   "dev:1.1": "legacy-after-rollback",
                                   "gone:1.1 (after rollback)": "legacy-after-rollback",
                                   "new:1.1": "legacy-after-rollback"})
        # Snoozes and pins: the v2 set, the old window's ignored.
        tabs = self.disk_tabs()
        self.assertEqual(tabs["pinned"], ["dev"])
        self.assertIn(k0, tabs["snoozed"])
        self.assertFalse(any(not KEY_RE.fullmatch(k) and not v.get("orphan") for k, v in tabs["snoozed"].items()))
        self.assertFalse(tab_state.apply_order([{"session": "new", "target": "new:1.1",
                                                 "key": f"{GEN2}:%1"}])[0]["snoozed"])
        # The new code still never writes the legacy files, and a second
        # restart imports nothing twice.
        self.assertEqual((self.drafts_file.read_bytes(), self.tab_file.read_bytes()), legacy_after_rollback)
        before = self.drafts_v2.read_bytes()
        self.restart()
        self.assertEqual(self.drafts_v2.read_bytes(), before)

    def test_rollback_without_old_writes_imports_nothing(self):
        self.seed({"dev:1.1": entry("one", 10.0)})
        drafts.ensure_converted()
        old_drafts = self.old_code("drafts")
        old_drafts.get_draft("dev:1.1")
        old_drafts.poll_block()
        self.restart()
        self.assertEqual(drafts.list_orphans(self.live), [])


class NoServerClassifierTests(unittest.TestCase):
    def test_absent_or_refused_socket_is_no_server_and_nothing_else_is(self):
        for stderr in ("no server running on /tmp/tmux-1000/default\n",
                       "error connecting to /tmp/x/s (No such file or directory)\n",
                       "error connecting to /tmp/x/s (Connection refused)\n"):
            self.assertTrue(tmux.TmuxListError(stderr).no_server, stderr)
        for stderr in ("error connecting to /tmp/x/s (Permission denied)\n",
                       "server exited unexpectedly\n", "", "lost server\n"):
            self.assertFalse(tmux.TmuxListError(stderr).no_server, stderr)


class SourceGuardTests(unittest.TestCase):
    def test_pre_schema2_backups_are_gitignored(self):
        """On the legacy layout the data dir is the checkout: the conversion's
        backups hold every pre-upgrade draft body, and an untracked file there
        also makes the updater read the checkout as dirty (R-P7B F1)."""
        names = ("drafts.v2.json", "tab_state.v2.json",
                 "drafts.json.pre-schema2", "tab_state.json.pre-schema2",
                 "drafts.json.pre-schema2-20261115T134505", "tab_state.json.pre-schema2-20261115T134505.1")
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("*.pre-schema2", ignore)
        self.assertIn("*.pre-schema2-*", ignore)
        if shutil.which("git") is None or subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT,
                capture_output=True, text=True).stdout.strip() != "true":
            self.skipTest("not a git checkout")
        result = subprocess.run(["git", "check-ignore", "--no-index", *names], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(sorted(result.stdout.split()), sorted(names), result.stderr)
        # The patterns are not so wide that real state files are hidden by them.
        result = subprocess.run(["git", "check-ignore", "--no-index", "-v", "pre-schema2.py"], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertNotIn("pre-schema2", result.stdout.split(":")[2] if result.stdout else "")

    def test_session_of_helper_replaces_split_parsing(self):
        for name in ("js/actions.js", "js/commands.js", "js/prompt-popup.js",
                     "js/studio-context.js", "js/terminal.js", "js/tabs.js", "js/app.js",
                     "js/state.js"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotRegex(text, r"\.split\(['\"]:['\"]\)\[0\]", name)
        self.assertIn("function sessionOf(", (ROOT / "js/state.js").read_text(encoding="utf-8"))
        py = (ROOT / "shared/opencode.py").read_text(encoding="utf-8")
        self.assertIn("bind_pane_key_status", py)
        for name in ("shared/tab_state.py", "shared/drafts.py"):
            self.assertNotIn('.split(":")[0]', (ROOT / name).read_text(encoding="utf-8"), name)


@unittest.skipUnless(shutil.which("tmux"), "needs a tmux binary")
class RealTmuxLifetimeTests(_StoreCase):
    """A real private tmux server: an Assist restart keeps the generation, a
    tmux server restart that reuses `%N` does not."""

    def setUp(self):
        super().setUp()
        self.box = TmuxBox(self)
        # The real lister, on the private server.
        for p in (patch.object(drafts, "_list_panes", side_effect=tmux.list_panes),
                  patch.object(tab_state, "_list_panes", side_effect=tmux.list_panes)):
            p.start()
            self.addCleanup(p.stop)

    def _build(self):
        self.box.new_session("dev")
        self.box.split("dev")
        return {r.display_target: r for r in tmux.list_panes()}

    def test_state_survives_assist_restart_same_server(self):
        panes = self._build()
        self.seed({"dev:1.2": entry("before restart", 10.0)})
        drafts.ensure_converted()
        key = panes["dev:1.2"].key
        self.restart()
        self.assertEqual(drafts.get_draft(key)["text"], "before restart")
        self.assertEqual(drafts.get_draft("dev:1.2")["text"], "before restart")
        self.assertEqual(drafts.poll_block(tmux.list_panes(), proto=2)["marks"], [key])

    def test_absent_socket_converts_to_orphans_before_a_new_server_reuses_the_name(self):
        """No tmux server and no socket at all (after a reboot, before tmux
        starts): tmux says `error connecting to <socket> (No such file or
        directory)`. That is no server, so the conversion runs at once and the
        old entries become orphans; a pane later created at the same name does
        not inherit them (sign-off M3 F1)."""
        self.assertFalse(self.box.socket.exists())
        with self.assertRaises(tmux.TmuxListError) as caught:
            tmux.list_panes()
        self.assertIn("error connecting to", caught.exception.stderr)
        self.assertTrue(caught.exception.no_server, caught.exception.stderr)
        self.seed({"dev:1.1": entry("OLD SERVER DRAFT", 10.0)},
                  {"snoozed": {"dev:1.1": {"at": 1.0, "was_busy": False}}})
        self.assertTrue(drafts.ensure_converted())
        self.assertTrue(tab_state.ensure_converted())
        self.assertTrue(self.disk_drafts()["dev:1.1"]["orphan"])
        self.assertTrue(self.disk_tabs()["snoozed"]["dev:1.1"]["orphan"])
        # A new server, a new pane at the same name.
        self.box.new_session("dev")
        fresh = tmux.list_panes()[0]
        self.assertEqual(fresh.display_target, "dev:1.1")
        self.assertEqual(drafts.get_draft(fresh.key)["text"], "")
        self.assertEqual(drafts.get_draft("dev:1.1")["text"], "")
        self.assertEqual([(o["key"], o["text"]) for o in drafts.list_orphans()],
                         [("dev:1.1", "OLD SERVER DRAFT")])
        self.assertFalse(tab_state.apply_order([{"session": "dev", "target": "dev:1.1",
                                                 "key": fresh.key}])[0]["snoozed"])

    def test_a_timeout_or_a_permission_error_still_defers(self):
        self.seed({"dev:1.1": entry("OLD", 10.0)}, {"snoozed": {"dev:1.1": {"at": 1.0, "was_busy": False}}})
        failures = (subprocess.TimeoutExpired(["tmux"], 5),
                    tmux.TmuxListError("error connecting to /tmp/x/s (Permission denied)\n"),
                    tmux.TmuxListError("server exited unexpectedly\n"))
        for failure in failures:
            with self.subTest(failure=repr(failure)), \
                    patch.object(drafts, "_list_panes", side_effect=failure), \
                    patch.object(tab_state, "_list_panes", side_effect=failure):
                self.assertFalse(drafts.ensure_converted())
                self.assertFalse(tab_state.ensure_converted())
        self.assertNotIn("schema", self.disk_drafts())
        self.assertNotIn("schema", self.disk_tabs())

    def test_orphan_after_tmux_server_restart_reusing_pane_id(self):
        panes = self._build()
        old = panes["dev:1.2"]
        self.seed({"dev:1.2": entry("the old pane's draft", 10.0)},
                  {"snoozed": {"dev:1.2": {"at": 1.0, "was_busy": False}}})
        drafts.ensure_converted()
        tab_state.ensure_converted()
        self.box.kill_server()
        new = self._build()["dev:1.2"]
        self.assertEqual(new.pane_id, old.pane_id)
        self.assertNotEqual(new.key, old.key)
        self.restart()
        # Not on the new pane by either form, and listed as an orphan.
        self.assertEqual(drafts.get_draft("dev:1.2")["text"], "")
        self.assertEqual(drafts.get_draft(new.key)["text"], "")
        live = tmux.list_panes()
        self.assertEqual(drafts.poll_block(live, proto=1)["marks"], [])
        self.assertEqual(drafts.poll_block(live, proto=2)["marks"], [])
        orphans = drafts.list_orphans(live)
        self.assertEqual([(o["key"], o["text"], o["reason"]) for o in orphans],
                         [(old.key, "the old pane's draft", "gone")])
        self.assertFalse(tab_state.apply_order([{"session": "dev", "target": "dev:1.2",
                                                 "key": new.key}])[0]["snoozed"])
        self.assertEqual(self.disk_drafts()[old.key]["text"], "the old pane's draft")


if __name__ == "__main__":
    unittest.main()
