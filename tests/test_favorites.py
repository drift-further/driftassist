"""Starring a history entry must create a favorite, and a second star must remove it.

The star on a Prompts/Cmds row POSTs /favorite with the entry's text. The add
branch stamps `ts` via time.strftime; when the execution-park refactor (86707e3)
dropped `import time` from routes/input.py, every star tap 500'd while unstar —
which never reaches that line — kept working. A one-directional break with no
visible error on the phone, which is exactly the kind this suite exists to pin.

Run: .venv/bin/python3 -m unittest tests.test_favorites
"""

import json
import inspect
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import shared.state as state
import shared.utils as utils
from routes import input as input_routes


class FavoriteStarTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.logger.disabled = True
        app.register_blueprint(input_routes.input_bp)
        self.app = app
        self.client = app.test_client()

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.fav_file = Path(tmp.name) / "favorites.json"
        self.hist_file = Path(tmp.name) / "history.json"
        for name, path in (("FAVORITES_FILE", self.fav_file), ("HISTORY_FILE", self.hist_file)):
            patcher = patch.object(state, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _favorites_on_disk(self):
        return json.loads(self.fav_file.read_text())

    def test_star_adds_a_favorite(self):
        response = self.client.post("/favorite", json={"text": "run the tests"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "action": "added"})
        favs = self._favorites_on_disk()
        self.assertEqual([f["text"] for f in favs], ["run the tests"])
        self.assertTrue(favs[0]["id"].startswith("f_"))
        self.assertRegex(favs[0]["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_new_favorite_lands_first_and_shows_in_history(self):
        self.client.post("/favorite", json={"text": "older"})
        self.client.post("/favorite", json={"text": "newer"})

        listed = self.client.get("/history").get_json()["favorites"]
        self.assertEqual([f["text"] for f in listed], ["newer", "older"])

    def test_second_star_removes_it(self):
        self.client.post("/favorite", json={"text": "run the tests"})
        response = self.client.post("/favorite", json={"text": "run the tests"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "action": "removed"})
        self.assertEqual(self._favorites_on_disk(), [])

    def test_bare_star_keeps_a_named_segment(self):
        self.fav_file.write_text(json.dumps(
            [{"id": "f_seg", "handle": "realign", "text": "standing brief"}]
        ))

        response = self.client.post("/favorite", json={"text": "standing brief"})

        self.assertEqual(response.get_json()["action"], "kept")
        self.assertEqual(response.get_json()["id"], "f_seg")
        self.assertEqual(len(self._favorites_on_disk()), 1)

    def test_empty_text_is_rejected(self):
        self.assertEqual(self.client.post("/favorite", json={"text": "  "}).status_code, 400)

    def test_concurrent_stars_do_not_lose_either_favorite(self):
        real_load = input_routes.load_json
        first_read = threading.Event()
        second_read = threading.Event()
        order_lock = threading.Lock()
        reads = 0

        def coordinated_load(path, default=None):
            nonlocal reads
            value = real_load(path, default=default)
            if Path(path) != self.fav_file:
                return value
            with order_lock:
                order = reads
                reads += 1
            if order == 0:
                first_read.set()
                second_read.wait(0.2)
            elif order == 1:
                second_read.set()
            return value

        statuses = []

        def star(text):
            with self.app.test_client() as client:
                statuses.append(client.post("/favorite", json={"text": text}).status_code)

        with patch("routes.input.load_json", side_effect=coordinated_load):
            first = threading.Thread(target=star, args=("first",))
            second = threading.Thread(target=star, args=("second",))
            first.start()
            self.assertTrue(first_read.wait(1))
            second.start()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sorted(statuses), [200, 200])
        self.assertEqual(
            {favorite["text"] for favorite in self._favorites_on_disk()},
            {"first", "second"},
        )

    def test_all_favorite_mutations_hold_the_shared_lock(self):
        for function in (
            input_routes.favorite,
            input_routes.update_favorite,
            input_routes.delete_favorite,
        ):
            with self.subTest(function=function.__name__):
                self.assertIn("with _favorites_lock", inspect.getsource(function))

    def test_clear_history_uses_the_history_mutation_lock(self):
        self.hist_file.write_text('[{"text":"keep until locked"}]', encoding="utf-8")
        started = threading.Event()
        finished = threading.Event()
        statuses = []

        def clear():
            started.set()
            with self.app.test_client() as client:
                statuses.append(client.delete("/history").status_code)
            finished.set()

        utils._history_lock.acquire()
        try:
            worker = threading.Thread(target=clear)
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.1))
        finally:
            utils._history_lock.release()
        worker.join(2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(statuses, [200])
        self.assertEqual(json.loads(self.hist_file.read_text(encoding="utf-8")), [])


@unittest.skipUnless(shutil.which("node"), "needs node")
class FavoriteEditClientTests(unittest.TestCase):
    def test_edit_closes_the_drawer_and_other_clicks_keep_their_behavior(self):
        # Load the actual delegated handler, drawer controls and segment editor.
        script = r"""
const fs = require('fs'), vm = require('vm');
function element() {
    const classes = new Set();
    return {dataset: {}, style: {}, value: '', innerHTML: '', listeners: {},
        classList: {add: (...xs) => xs.forEach(x => classes.add(x)),
            remove: (...xs) => xs.forEach(x => classes.delete(x)),
            contains: x => classes.has(x)},
        addEventListener: function(name, fn) { this.listeners[name] = fn; },
        contains: () => true, focus: () => {}};
}
const els = {};
for (const id of ['drawer-left', 'drawer-bottom', 'drawer-overlay', 'seg-sheet',
                  'hist-filter-clear']) els[id] = element();
global.document = {getElementById: id => els[id] || null,
    querySelector: () => null, querySelectorAll: () => []};
global.listArea = element();
global.input = element();
global._favorites = [{id: 'f_one', text: 'the body', handle: 'brief'}];
global._history = [];
global._filterText = '';
global.toggleFavorite = () => {};
for (const file of ['js/ui.js', 'js/segments.js'])
    vm.runInThisContext(fs.readFileSync(file, 'utf8'), {filename: file});
// HTML escaping isn't the subject of this DOM stub.
escHtml = text => text;
switchHistTab('favs');
const row = {dataset: {id: 'f_one', text: 'the body', handle: 'brief'},
    closest: () => row};
function click(kind, item = row) {
    els['drawer-bottom'].classList.add('open', 'half');
    els['drawer-overlay'].classList.add('visible');
    els['seg-sheet'].classList.remove('visible');
    let prevented = false, stopped = false;
    const button = {closest: () => item};
    const target = {closest: selector =>
        selector === '.list-item-edit' && kind === 'edit' ? button :
        selector === '.list-item-star' && kind === 'star' ? button :
        selector.includes('data-list-action') && kind === 'load' ? row : null};
    listArea.listeners.click({target, preventDefault: () => {prevented = true;},
        stopPropagation: () => {stopped = true;}});
    return {open: els['drawer-bottom'].classList.contains('open'),
        half: els['drawer-bottom'].classList.contains('half'),
        overlay: els['drawer-overlay'].classList.contains('visible'),
        editor: els['seg-sheet'].classList.contains('visible'), prevented, stopped};
}
const edited = click('edit');
const bodyPresent = els['seg-sheet'].innerHTML.includes('the body');
const composerAfterEdit = input.value;
const star = click('star');
const background = click('background');
const invalidEdit = click('edit', {dataset: {}});
const loaded = click('load');
console.log(JSON.stringify({edited, bodyPresent, composerAfterEdit, star, background, invalidEdit,
    loaded, composer: input.value}));
"""
        result = subprocess.run(["node", "-e", script],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["edited"], dict(open=False, half=False, overlay=False,
                                             editor=True, prevented=True, stopped=True))
        self.assertTrue(data["bodyPresent"])
        self.assertEqual(data["composerAfterEdit"], "")
        for name in ("star", "background", "invalidEdit"):
            with self.subTest(click=name):
                self.assertTrue(data[name]["open"])
                self.assertFalse(data[name]["editor"])
        self.assertFalse(data["loaded"]["open"])
        self.assertEqual(data["composer"], "[brief] ")


if __name__ == "__main__":
    unittest.main()
