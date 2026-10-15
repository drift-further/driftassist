"""The More keys sheet: it offers every key /key's combo grammar accepts, and
sends taps one at a time in the order they were made.

js/keycombo.js renders the sheet's layers from `_kbLayout`; index.html holds
the base row (Esc, Tab, Space, Bksp, Enter). Together they are meant to be the
whole grammar in shared/key_combo.py: every printable character plus every
named key. The coverage test holds both directions, so a key added to the
grammar without a button, or a button the grammar refuses, fails here.

The order test runs the real js/keycombo.js under Node with a fake DOM and a
fetch that answers only when told to. Two taps used to be two requests in
flight at once, which tmux can deliver either way round ("ls" as "sl").

Run: .venv/bin/python3 -m unittest tests.test_more_keys
"""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from shared.key_combo import NAMED_KEYS, ComboError, combo_to_tmux

ROOT = Path(__file__).resolve().parent.parent

_HARNESS = r"""
function el(tag) {
    const classes = new Set();
    return {
        tagName: tag, children: [], dataset: {}, style: {}, attrs: {}, listeners: {},
        hidden: false, textContent: '', type: '',
        set className(v) { classes.clear(); v.split(/\s+/).filter(Boolean).forEach(c => classes.add(c)); },
        get className() { return [...classes].join(' '); },
        classList: {
            add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
            toggle(c, on) { on = on === undefined ? !classes.has(c) : on; on ? classes.add(c) : classes.delete(c); return on; },
        },
        appendChild(c) { this.children.push(c); return c; },
        addEventListener(type, fn) { this.listeners[type] = fn; },
        setAttribute(k, v) { this.attrs[k] = String(v); },
        getAttribute(k) { return this.attrs[k]; },
    };
}
const byId = {};
global.document = {
    getElementById: id => byId[id] || (byId[id] = el('div')),
    createElement: el,
    querySelectorAll: () => [],
};
let target = 'work:1.0';
global.getInputTarget = () => target;
var lastAction = 0;
function updateStatusTime() {}
function showFlash() {}

const calls = [];
const pending = [];
global.fetch = (url, opts) => {
    calls.push({url, body: JSON.parse(opts.body)});
    return new Promise(resolve => pending.push(() => resolve({json: async () => ({ok: true})})));
};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

__KEYCOMBO__

async function scenario(name) {
    if (name === 'layout') {
        const layers = byId['kb-layers'].children.map(layer => ({
            name: layer.dataset.layer,
            hidden: layer.hidden,
            keys: layer.children.flatMap(row => row.children.map(btn => ({
                key: btn.dataset.kb,
                classes: btn.className.split(' '),
                label: btn.children[0].textContent,
            }))),
        }));
        return {layers};
    }
    if (name === 'order') {
        const seen = [];
        toggleComboMod('ctrl');
        sendCombo('b');
        target = 'other:1.0';              // the tab moves while the first send is out
        byId['kb-sheet'].listeners.click({target: {closest: () => ({dataset: {kb: 'l'}})}});
        sendCombo('s');
        await settle();
        seen.push(calls.length);           // only the first request has gone
        pending.shift()();
        await settle(); await settle();
        seen.push(calls.length);
        pending.shift()();
        await settle(); await settle();
        seen.push(calls.length);
        pending.shift()();
        await settle();
        return {seen, bodies: calls.map(c => c.body)};
    }
    throw new Error('unknown scenario ' + name);
}
scenario(process.argv[2]).then(r => console.log(JSON.stringify(r)));
"""


def _run(name):
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    source = (ROOT / "js" / "keycombo.js").read_text()
    with tempfile.TemporaryDirectory() as raw:
        script = Path(raw) / "harness.js"
        script.write_text(_HARNESS.replace("__KEYCOMBO__", source))
        out = subprocess.run(
            ["node", str(script), name],
            capture_output=True, text=True, timeout=20, check=True,
        )
    return json.loads(out.stdout)


def _base_row_keys():
    html = (ROOT / "index.html").read_text()
    match = re.search(r'<div class="kb-base">(.*?)</div>', html, re.S)
    if match is None:
        raise AssertionError("index.html has no .kb-base row")
    return re.findall(r'data-kb="([^"]+)"', match.group(1))


class SheetCoversTheGrammarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layers = _run("layout")["layers"]
        cls.layer_keys = [k for layer in cls.layers for k in layer["keys"]]
        cls.offered = [k["key"] for k in cls.layer_keys] + _base_row_keys()

    def test_every_key_the_grammar_accepts_has_one_button(self):
        printable = {chr(c) for c in range(0x21, 0x7f) if not chr(c).isupper()}
        expected = printable | set(NAMED_KEYS)
        self.assertEqual(len(self.offered), len(set(self.offered)), "a key has two buttons")
        self.assertEqual(set(self.offered) - expected, set(), "buttons the grammar does not name")
        self.assertEqual(expected - set(self.offered), set(), "grammar keys with no button")

    def test_every_button_is_accepted_bare(self):
        for key in self.offered:
            with self.subTest(key=key):
                combo_to_tmux({"key": key})

    def test_shift_marking_matches_what_the_grammar_does_with_shift(self):
        # Letters show as capitals under Shift; the other printable keys stand
        # down, because the grammar refuses Shift with them.
        for entry in self.layer_keys:
            key, classes = entry["key"], entry["classes"]
            if len(key) != 1:
                self.assertNotIn("kb-shiftless", classes, key)
                continue
            with self.subTest(key=key):
                try:
                    combo_to_tmux({"key": key, "shift": True})
                    refused = False
                except ComboError:
                    refused = True
                self.assertEqual("kb-shiftless" in classes, refused)
                self.assertEqual("kb-alpha" in classes, not refused)

    def test_letters_open_first(self):
        shown = [layer["name"] for layer in self.layers if not layer["hidden"]]
        self.assertEqual(shown, ["abc"])


class SendOrderTests(unittest.TestCase):
    def test_taps_are_sent_one_at_a_time_in_tap_order(self):
        result = _run("order")
        self.assertEqual(result["seen"], [1, 2, 3])
        self.assertEqual([b["combo"] for b in result["bodies"]], [
            {"ctrl": True, "alt": False, "shift": False, "key": "b"},
            {"ctrl": False, "alt": False, "shift": False, "key": "l"},
            {"ctrl": False, "alt": False, "shift": False, "key": "s"},
        ])

    def test_each_send_goes_to_the_pane_in_view_when_it_was_tapped(self):
        result = _run("order")
        self.assertEqual([b["target"] for b in result["bodies"]],
                         ["work:1.0", "other:1.0", "other:1.0"])


if __name__ == "__main__":
    unittest.main()
