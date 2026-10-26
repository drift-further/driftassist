"""The Devices sheet (js/devices.js) under Node, against a small DOM stub.

Findings 3874 (A1), 3875 (A2) and 3866: a failed response must read as a
failure, never as "No browsers signed in"; an older list response must not
overwrite a newer one; and the sheet and the name step must follow what
/poll says while they are open.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_HARNESS = r"""
function el(id) {
  return { id, hidden: false, textContent: '', className: '', value: '', children: [],
    attrs: {}, classes: new Set(), listeners: {},
    classList: { toggle(c, on) { on ? this._o.classes.add(c) : this._o.classes.delete(c); },
                 contains(c) { return this._o.classes.has(c); } },
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    replaceChildren() { this.children = []; },
    addEventListener(t, f) { this.listeners[t] = f; } };
}
const nodes = {};
for (const id of ['devices-sheet', 'devices-list', 'devices-notice', 'devname-sheet', 'devname-backdrop',
                  'devname-input', 'devname-suggest', 'devices-close', 'devname-save', 'devname-skip'])
  { nodes[id] = el(id); nodes[id].classList._o = nodes[id]; }
global.document = {
  getElementById: id => nodes[id] || null,
  createElement: tag => { const e = el(tag); e.classList._o = e; return e; },
  addEventListener() {},
};
global.location = { href: '/' };
global.showToast = () => {};
global.confirm = () => true;
global.prompt = () => null;
const queue = [];
let fetches = 0;
global.fetch = (url, opts) => { fetches++; return new Promise((resolve, reject) => queue.push({ url, resolve, reject })); };
function reply(entry, status, body) {
  entry.resolve({ status, ok: status >= 200 && status < 300,
                  json: async () => { if (body === 'MALFORMED') throw new SyntaxError('bad'); return body; } });
}
__DEVICES__
const settle = () => new Promise(r => setTimeout(r, 0));
const text = () => {
  const out = [];
  const walk = n => { if (n.textContent) out.push(n.textContent); (n.children || []).forEach(walk); };
  walk(nodes['devices-list']);
  return out.join(' | ');
};
const ok = devices => ({ ok: true, devices, this_device: null, recovery_required: false, message: '' });
const active = { id: 'd_b', name: 'B', source: 'login', created: 1, last_seen: 1, revoked_at: null };
const revoked = Object.assign({}, active, { revoked_at: 1761000000 });

async function scenario(name) {
  if (name === 'errors') {
    const out = {};
    for (const [label, status, body] of [['json500', 500, { ok: false, error: 'disk unavailable' }],
                                         ['json403', 403, { ok: false, error: 'Origin not allowed' }],
                                         ['malformed', 200, 'MALFORMED'],
                                         ['noshape', 200, { ok: true }],
                                         ['empty', 200, ok([])]]) {
      nodes['devices-list'].children = [revokedRow()];
      const p = loadDevices(); await settle(); reply(queue.shift(), status, body); await p; await settle();
      out[label] = { list: text(), notice: nodes['devices-notice'].hidden ? '' : nodes['devices-notice'].textContent };
    }
    nodes['devices-list'].children = [revokedRow()];
    const p = loadDevices(); await settle(); queue.shift().reject(new Error('offline')); await p;
    out.network = { list: text(), notice: nodes['devices-notice'].hidden ? '' : nodes['devices-notice'].textContent };
    return out;
  }
  if (name === 'order') {
    const first = loadDevices(); await settle();
    const second = loadDevices(); await settle();
    const [older, newer] = [queue.shift(), queue.shift()];
    reply(newer, 200, ok([revoked])); await second; await settle();
    reply(older, 200, ok([active])); await first; await settle();
    const afterStale = text();
    const third = loadDevices(); await settle(); reply(queue.shift(), 200, ok([active])); await third; await settle();
    return { afterStale, afterNewer: text() };
  }
  if (name === 'poll') {
    openDevicesSheet(); await settle(); reply(queue.shift(), 200, ok([active])); await settle();
    const before = fetches;
    applyDeviceData({ id: 'd_a', name: 'A', named: true }); await settle();
    const refetched = fetches - before;
    if (queue.length) { reply(queue.shift(), 200, ok([revoked])); await settle(); }
    const listAfterPoll = text();
    nodes['devices-sheet'].classes.delete('visible');
    _setDevicesSheet(false);
    const closedBefore = fetches;
    applyDeviceData({ id: 'd_a', name: 'A', named: true }); await settle();
    // The name step: open in this tab, then another tab names the device.
    applyDeviceData({ id: 'd_c', name: 'C', named: false });
    const nameOpen = nodes['devname-sheet'].classes.has('visible');
    applyDeviceData({ id: 'd_c', name: 'Phone', named: true });
    const nameClosed = !nodes['devname-sheet'].classes.has('visible');
    return { refetched, listAfterPoll, closedFetches: fetches - closedBefore, nameOpen, nameClosed };
  }
}
function revokedRow() { const r = el('div'); r.classList._o = r; r.textContent = 'previous row'; return r; }
scenario(process.argv[2]).then(r => console.log(JSON.stringify(r)));
"""


def _run(name):
    if not shutil.which("node"):
        raise unittest.SkipTest("node not installed")
    source = _HARNESS.replace("__DEVICES__", (ROOT / "js" / "devices.js").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as raw:
        script = Path(raw) / "harness.js"
        script.write_text(source)
        out = subprocess.run(["node", str(script), name], capture_output=True, text=True, timeout=20)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class DevicesSheetTests(unittest.TestCase):
    def test_failed_responses_are_errors_not_an_empty_list(self):
        """A1 (3874): JSON 500/403, malformed JSON and a missing list are failures."""
        out = _run("errors")
        for label in ("json500", "json403", "malformed", "noshape", "network"):
            with self.subTest(label):
                self.assertNotIn("No browsers signed in", out[label]["list"])
                self.assertTrue(out[label]["notice"], out[label])
                self.assertIn("previous row", out[label]["list"])  # the prior list is kept
        self.assertIn("disk unavailable", out["json500"]["notice"])
        # Must-not-fire: a genuine empty list says so, with no error.
        self.assertIn("No browsers signed in", out["empty"]["list"])
        self.assertEqual(out["empty"]["notice"], "")

    def test_older_response_cannot_overwrite_a_newer_one(self):
        """A2 (3875): a held, superseded response is discarded; a newer one still wins."""
        out = _run("order")
        self.assertIn("revoked", out["afterStale"])
        self.assertNotIn("revoked", out["afterNewer"])

    def test_sheet_and_name_step_follow_poll_while_open(self):
        """3866: an open sheet refreshes on /poll; a name chosen elsewhere closes the prompt."""
        out = _run("poll")
        self.assertEqual(out["refetched"], 1)
        self.assertIn("revoked", out["listAfterPoll"])
        self.assertEqual(out["closedFetches"], 0)
        self.assertTrue(out["nameOpen"])
        self.assertTrue(out["nameClosed"])


if __name__ == "__main__":
    unittest.main()
