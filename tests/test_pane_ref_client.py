"""The browser addresses panes by durable key (effort 621 Phase 7 step B, client).

js/pane-ref.js holds the selection as a PaneRef: `_termTarget` (display) and
`_termKey` (the durable key from /poll), written only by setTermTarget().
Per-pane requests send the key with `proto: 2` and fall back to the display
target when the server sends no key. The selection follows its key through a
renumber or rename, and a stranger that takes the old address is not it.
Split view keeps its own `%N` + identity addressing, unchanged.

Run: .venv/bin/python3 -m unittest tests.test_pane_ref_client
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
K1 = "aaaaaaaaaaaaaaaa:%1"
K2 = "aaaaaaaaaaaaaaaa:%2"


def run_js(files, body, prelude=""):
    harness = r"""
const fs = require('fs');
const vm = require('vm');
PRELUDE
for (const f of FILES) vm.runInThisContext(fs.readFileSync(f, 'utf8'), {filename: f});
(async () => {
BODY
})().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
""".replace("FILES", json.dumps(files)).replace("PRELUDE", prelude).replace("BODY", body)
    result = subprocess.run(["node", "-e", harness], cwd=ROOT, text=True,
                            capture_output=True, check=False)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout.strip().splitlines()[-1])


PANE_PRELUDE = r"""
globalThis._termTarget = null;
globalThis._termKey = null;
globalThis.defaultTermLines = () => 2000;  // js/state.js, not loaded here
globalThis.maxTermLines = () => 20000;
globalThis._sessionPanes = [];
globalThis.getInputTarget = () => _termTarget;
"""

DRAFTS_PRELUDE = PANE_PRELUDE + r"""
globalThis.sent = [];
globalThis.fetch = async (url, opts) => {
    sent.push({url, body: opts && opts.body ? JSON.parse(opts.body) : null});
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    return {json: async () => ({ok: true, target: body.target,
                               draft: {text: body.text || '', attachments: [], enter_armed: true, updated_at: 5}})};
};
globalThis.input = {value: '', addEventListener() {}, setAttribute() {}, blur() {}, focus() {}};
globalThis.document = {addEventListener() {}, querySelectorAll() { return []; },
                       getElementById() { return null; }, activeElement: null};
globalThis._attachments = [];
globalThis.renderAttachments = () => {};
globalThis.showFlash = () => {};
"""


SELECT_PRELUDE = DRAFTS_PRELUDE + r"""
globalThis.stubElement = {classList: {add(){}, remove(){}, toggle(){}}, dataset: {}, setAttribute(){}};
document.getElementById = () => stubElement;
globalThis.localStorage = {setItem(){}};
globalThis._lastTabTapTime = 0;
globalThis._DOUBLE_TAP_MS = 500;
globalThis._termWs = null;
globalThis._termWsConnected = false;
globalThis._renderTimer = null;
globalThis._pendingRender = null;
globalThis._termShowProjects = false;
globalThis._getSmartState = () => ({});
for (const name of ['noteTargetChosen','noteTargetListed','markActiveTab','updateTmuxIndicator',
                    'captureTerminal','disconnectTerminalWs','connectTerminalWs',
                    'onTabSwitchCommands','onTabSwitchSkills','renderDelegateNavigation','_modelSeenSweep']) {
    globalThis[name] = () => {};
}
globalThis.targetMissingIsStale = () => false;
globalThis.sessionOf = t => t.replace(/:\d+\.\d+$/, '');
globalThis.tick = () => new Promise(r => setImmediate(r));
"""

# The real selectTab (js/terminal.js) and _applySessionsData (js/app.js).
SELECT_LOAD = r"""
const termSrc = fs.readFileSync('js/terminal.js', 'utf8');
vm.runInThisContext(termSrc.slice(termSrc.indexOf('function selectTab('), termSrc.indexOf('async function _clearActivePane(')));
const appSrc = fs.readFileSync('js/app.js', 'utf8');
vm.runInThisContext(appSrc.slice(appSrc.indexOf('function _applySessionsData('), appSrc.indexOf('function _formatIdleTime(')));
"""


def _js(name):
    return (ROOT / name).read_text(encoding="utf-8")


class PaneRefTests(unittest.TestCase):
    def test_setTermTarget_is_only_writer(self):
        writes = re.compile(r"(?<![\w.])_termTarget\s*=(?!=)|(?<![\w.])_termKey\s*=(?!=)")
        for path in sorted((ROOT / "js").glob("*.js")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not writes.search(line):
                    continue
                allowed = (path.name == "state.js" and line.lstrip().startswith("let ")) or \
                    (path.name == "pane-ref.js" and line.strip().startswith(("_termTarget = ", "_termKey = ")))
                self.assertTrue(allowed, f"js/{path.name}:{number}: {line.strip()}")
        self.assertIn("let _termKey = null;", _js("js/state.js"))
        # Loaded right after state.js, before anything that selects a pane.
        index = _js("index.html")
        order = [m.group(1) for m in re.finditer(r'<script src="/js/([\w-]+)\.js', index)]
        self.assertEqual(order[order.index("state") + 1], "pane-ref")
        self.assertIn("'/js/pane-ref.js?v=1'", _js("sw.js"))

    def test_client_sends_key_with_proto_2_and_falls_back_to_target(self):
        result = run_js(["js/pane-ref.js"], r"""
_sessionPanes = [{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%1'}, {target: 'dev:1.2', key: null}];
setTermTarget('dev:1.1');
const keyed = wireTarget('dev:1.1');
const noKey = wireTarget('dev:1.2');
const unknown = wireTarget('gone:1.1');
const route = inputRoute();
_sessionPanes = [{target: 'dev:1.1'}];          // a server without keys
setTermTarget('dev:1.1');
const oldServer = {wire: wireTarget('dev:1.1'), id: currentPaneId(), route: inputRoute()};
console.log(JSON.stringify({keyed, noKey, unknown, route, oldServer}));
""", PANE_PRELUDE)
        self.assertEqual(result["keyed"], {"target": K1, "proto": 2})
        self.assertEqual(result["noKey"], {"target": "dev:1.2"})
        self.assertEqual(result["unknown"], {"target": "gone:1.1"})
        self.assertEqual(result["route"], {"target": K1, "proto": 2})
        self.assertEqual(result["oldServer"], {"wire": {"target": "dev:1.1"}, "id": "dev:1.1",
                                               "route": {"target": "dev:1.1"}})

        # Drafts: saved and read under the key with proto 2, or the display
        # target against an older server.
        result = run_js(["js/pane-ref.js", "js/drafts.js"], r"""
_sessionPanes = [{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%1'}];
setTermTarget('dev:1.1');
input.value = 'hello';
saveDraftSoon();
await flushDraft(currentPaneId());
await loadDraft(currentPaneId(), true);
_sessionPanes = [{target: 'dev:1.1'}];
setTermTarget('dev:1.1');
saveDraftSoon();
await flushDraft(currentPaneId());
await loadDraft(currentPaneId(), true);
console.log(JSON.stringify(sent));
""", DRAFTS_PRELUDE)
        put_new, get_new, put_old, get_old = result
        self.assertEqual((put_new["body"]["target"], put_new["body"]["proto"]), (K1, 2))
        self.assertEqual(put_new["body"]["text"], "hello")
        self.assertEqual(get_new["url"], "/api/draft?target=aaaaaaaaaaaaaaaa%3A%251&proto=2")
        self.assertEqual(put_old["body"]["target"], "dev:1.1")
        self.assertNotIn("proto", put_old["body"])
        self.assertEqual(get_old["url"], "/api/draft?target=dev%3A1.1")

        # Every other per-pane request goes through the same mapping.
        terminal = _js("js/terminal.js")
        self.assertIn("Object.assign(msg, wireTarget(target))", terminal)          # subscribe
        self.assertIn("new URLSearchParams(wireTarget(_termTarget))", terminal)     # capture
        self.assertIn("...wireTarget(target), on: !!on", _js("js/tabs.js"))         # snooze
        self.assertIn("proto=2", _js("js/app.js"))                                  # /poll
        for name in ("js/input.js", "js/keycombo.js"):
            self.assertIn("inputRoute()", _js(name), name)
            self.assertNotIn("target: getInputTarget(), ...(typeof inputIdentity", _js(name), name)
        # The vault sends only to the tab on screen (it checks that first).
        self.assertIn("...wireRoute(_termTarget,", _js("js/vault.js"))
        for name in ("js/actions.js", "js/terminal.js"):
            self.assertIn("wireRoute(", _js(name), name)

    def test_selection_follows_its_key_and_drops_a_stranger(self):
        result = run_js(["js/pane-ref.js"], r"""
_sessionPanes = [{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%0'}, {target: 'dev:1.2', key: 'aaaaaaaaaaaaaaaa:%1'}];
setTermTarget('dev:1.2');
const bound = _termKey;
// dev:1.1 is killed: the selected pane renumbers to dev:1.1.
const moved = followTermPane([{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%1'}]);
const afterMove = [_termTarget, _termKey];
// A tmux restart: a new pane sits at dev:1.1 with the same %N, another key.
const gone = followTermPane([{target: 'dev:1.1', key: 'bbbbbbbbbbbbbbbb:%1'}]);
const afterGone = [_termTarget, _termKey];
// A pane not listed yet (just launched): bound by display once listed.
setTermTarget('new:1.1', null);
const unlisted = followTermPane([]);
const listed = followTermPane([{target: 'new:1.1', key: 'aaaaaaaaaaaaaaaa:%5'}]);
const frame = displayOf('aaaaaaaaaaaaaaaa:%5');
console.log(JSON.stringify({bound, moved, afterMove, gone, afterGone, unlisted, listed,
                            key: _termKey, frame, id: currentPaneId()}));
""", PANE_PRELUDE)
        self.assertEqual(result["bound"], K1)
        self.assertEqual(result["moved"], "moved")
        self.assertEqual(result["afterMove"], ["dev:1.1", K1])
        self.assertEqual(result["gone"], "gone")
        self.assertEqual(result["afterGone"], ["dev:1.1", K1], "a stranger at the address is not adopted")
        self.assertEqual(result["unlisted"], "gone")
        self.assertEqual(result["listed"], "same")
        self.assertEqual(result["key"], "aaaaaaaaaaaaaaaa:%5")
        self.assertEqual(result["frame"], "new:1.1")
        self.assertEqual(result["id"], "aaaaaaaaaaaaaaaa:%5")
        app = _js("js/app.js")
        self.assertIn("followTermPane(panes)", app)
        self.assertIn("displayOf(data.target)", _js("js/terminal.js"))

    def test_a_keyless_listing_does_not_move_the_selection(self):
        """A transient listing without keys (the tmux server's start time not
        readable) must not read as the selected pane being gone (R-P7B F2)."""
        result = run_js(["js/pane-ref.js"], r"""
_sessionPanes = [{target: 'dev:1.2', key: 'aaaaaaaaaaaaaaaa:%1'}];
setTermTarget('dev:1.2');
const keyless = followTermPane([{target: 'dev:1.2', key: null}]);
const kept = _termKey;
const gone = followTermPane([{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%0'}]);
console.log(JSON.stringify({keyless, kept, gone}));
""", PANE_PRELUDE)
        self.assertEqual(result["keyless"], "same")
        self.assertEqual(result["kept"], K1)
        self.assertEqual(result["gone"], "gone")

    def _same_address_run(self, how):
        return run_js(["js/pane-ref.js", "js/drafts.js"], SELECT_LOAD + r"""
const dead = 'aaaaaaaaaaaaaaaa:%0', fresh = 'bbbbbbbbbbbbbbbb:%0';
// A tiny draft server: GET and PUT by target.
const store = {[dead]: 'OLD PANE DRAFT', [fresh]: 'FRESH PANE OWN DRAFT'};
globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    sent.push({url, method: (opts && opts.method) || 'GET', body});
    if (url.startsWith('/api/draft?')) {
        const t = new URLSearchParams(url.split('?')[1]).get('target');
        return {json: async () => ({ok: true, target: t, draft: {text: store[t] || '', attachments: [], enter_armed: true, updated_at: 7}})};
    }
    if (body && opts.method === 'PUT') { store[body.target] = body.text; }
    return {json: async () => ({ok: true, target: body && body.target, draft: {text: (body && body.text) || '', attachments: [], enter_armed: true, updated_at: 9}})};
};
_sessionPanes = [{session: 'dev', target: 'dev:1.1', key: dead}];
setTermTarget('dev:1.1');
await loadDraft(dead, true);
const startText = input.value;
// tmux restarts; a new pane sits at the very same address with its own draft.
const panes = [{session: 'dev', target: 'dev:1.1', key: fresh}];
let routeBefore = null;
if (HOW === 'auto') {
    _applySessionsData(panes, 'dev:1.1', Date.now());
} else {
    _sessionPanes = panes;                      // the list the user tapped from
    routeBefore = inputRoute();                 // input before reconciliation
    _lastTabTapTime = 0;
    selectTab('dev:1.1', false);
}
for (let i = 0; i < 4; i++) await tick();
console.log(JSON.stringify({startText, routeBefore, key: _termKey, route: inputRoute(), text: input.value, store,
    gets: sent.filter(s => s.method === 'GET').map(s => s.url),
    puts: sent.filter(s => s.method === 'PUT').map(s => [s.body.target, s.body.text])}));
""".replace("HOW", json.dumps(how)), SELECT_PRELUDE)

    def test_a_replacement_at_the_same_address_is_selected_with_its_own_key_and_draft(self):
        """Sign-off M3 F3: after a tmux restart a new pane at the exact old
        display address is a different pane. The fallback and a deliberate tap
        take its key from the listed PaneRef, flush the old composer under the
        old key and load the new pane's own draft."""
        dead, fresh = "aaaaaaaaaaaaaaaa:%0", "bbbbbbbbbbbbbbbb:%0"
        for how in ("auto", "tap"):
            with self.subTest(how=how):
                got = self._same_address_run(how)
                self.assertEqual(got["startText"], "OLD PANE DRAFT")
                self.assertEqual(got["key"], fresh)
                self.assertEqual(got["route"], {"target": fresh, "proto": 2})
                self.assertEqual(got["text"], "FRESH PANE OWN DRAFT")
                self.assertIn(f"/api/draft?target={fresh.replace(':', '%3A').replace('%0', '%250')}&proto=2", got["gets"])
                self.assertEqual(got["store"][fresh], "FRESH PANE OWN DRAFT")
                self.assertEqual(got["store"][dead], "OLD PANE DRAFT")
                self.assertNotIn([fresh, "OLD PANE DRAFT"], got["puts"])
        # Input routing before the selection moves keeps the current binding
        # (paneKeyOf's guard): typing is never re-aimed by a list alone.
        self.assertEqual(self._same_address_run("tap")["routeBefore"], {"target": dead, "proto": 2})

    def _copy_run(self, schedule):
        """A small draft server that copies an orphan the way /api/drafts/copy
        does (the orphan stays). The copy's answer is held until the schedule
        releases it; everything else answers at once."""
        return run_js(["js/pane-ref.js", "js/drafts.js"], SELECT_LOAD + r"""
const ka = 'aaaaaaaaaaaaaaaa:%1', kb = 'aaaaaaaaaaaaaaaa:%2';
const store = {[kb]: 'PRECIOUS PANE B DRAFT', 'lost:1.1': 'RECOVER ME'};
let releaseCopy = null;
globalThis.openOrphanDrafts = () => {};
globalThis.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : null;
    sent.push({url, method: (opts && opts.method) || 'GET', body});
    const draft = t => ({text: store[t] || '', attachments: [], enter_armed: true, updated_at: 5});
    if (url === '/api/drafts/copy') {
        // The server commits at once; only the answer is held.
        store[body.target] = store[body.from];
        const committed = draft(body.target);
        await new Promise(r => { releaseCopy = r; });
        return {json: async () => ({ok: true, target: body.target, draft: committed})};
    }
    if (url.startsWith('/api/draft?')) {
        const t = new URLSearchParams(url.split('?')[1]).get('target');
        return {json: async () => ({ok: true, target: t, draft: draft(t)})};
    }
    if (opts && opts.method === 'PUT') {
        if (body.text || (body.attachments || []).length) store[body.target] = body.text; else delete store[body.target];
        return {json: async () => ({ok: true, target: body.target, draft: {...draft(body.target), updated_at: 9}})};
    }
    return {json: async () => ({ok: false})};
};
_sessionPanes = [{session: 'dev', target: 'dev:1.1', key: ka}, {session: 'dev', target: 'dev:1.2', key: kb}];
setTermTarget('dev:1.1');
const copying = copyOrphanDraft('lost:1.1');
await tick();
if (SCHEDULE === 'switch') { _lastTabTapTime = 0; selectTab('dev:1.2', false); }
if (SCHEDULE === 'type') { input.value = 'TYPED WHILE COPYING'; saveDraftSoon(); }
for (let i = 0; i < 4; i++) await tick();
releaseCopy();
await copying;
for (let i = 0; i < 4; i++) await tick();
const afterResponse = {key: _termKey, composer: input.value};
if (SCHEDULE === 'switch') {
    input.value += '!'; saveDraftSoon(); await flushDraft(kb);
    for (let i = 0; i < 4; i++) await tick();
}
if (SCHEDULE === 'type') { await flushDraft(ka); for (let i = 0; i < 4; i++) await tick(); }
console.log(JSON.stringify({afterResponse, store,
    puts: sent.filter(s => s.method === 'PUT').map(s => [s.body.target, s.body.text]),
    bases: sent.filter(s => s.method === 'PUT' && 'base' in s.body).length}));
""".replace("SCHEDULE", json.dumps(schedule)), SELECT_PRELUDE)

    def test_an_orphan_copy_keeps_the_orphan_whatever_the_composer_does(self):
        ka, kb = "aaaaaaaaaaaaaaaa:%1", "aaaaaaaaaaaaaaaa:%2"
        # Ordinary copy: the composer shows the copied draft; the orphan stays.
        got = self._copy_run("plain")
        self.assertEqual(got["afterResponse"]["composer"], "RECOVER ME")
        self.assertEqual(got["store"][ka], "RECOVER ME")
        self.assertEqual(got["store"]["lost:1.1"], "RECOVER ME")
        # Switch to B while the answer is in flight: the late answer does not
        # paint B, and B's next edit saves B's own text.
        got = self._copy_run("switch")
        self.assertEqual(got["afterResponse"], {"key": kb, "composer": "PRECIOUS PANE B DRAFT"})
        self.assertEqual(got["store"][kb], "PRECIOUS PANE B DRAFT!")
        self.assertEqual(got["store"]["lost:1.1"], "RECOVER ME")
        # Type on A while the copy is in flight: typing wins on screen and is
        # saved; the copied text is still the orphan's.
        got = self._copy_run("type")
        self.assertEqual(got["afterResponse"]["composer"], "TYPED WHILE COPYING")
        self.assertEqual(got["store"][ka], "TYPED WHILE COPYING")
        self.assertEqual(got["store"]["lost:1.1"], "RECOVER ME")
        # No save carries a revision precondition any more.
        self.assertEqual(got["bases"], 0)

    def test_split_view_identity_checks_unchanged(self):
        """Split view addresses its slots by `%N` with a carried identity and
        revalidates that identity; none of it goes through the key mapping.
        Asserted directly, so the test needs no git history."""
        split = _js("js/split-view.js")
        for line in (
            "same(a,b) { return !!a && !!b && a.pane_id === b.pane_id && a.pane_pid === String(b.pane_pid) && a.created === b.created; },",
            "body:JSON.stringify({target:this.record.pane_id, keys:'Page_Up', expected_target_identity:this.record.identity})});",
            "const resp = await fetch('/terminal/identity?target='+encodeURIComponent(record.pane_id));",
            "&& (!expected || JSON.stringify(data.identity) === JSON.stringify(expected))) { record.identity = data.identity; return true; }",
            "record.dead = true; record.identity = null; return false;",
            "body:JSON.stringify({session:r.session,target:r.pane_id,cols,rows,expected_target_identity:r.identity})});",
            "return r && !r.dead && r.identity ? r.target : '%split-view-unavailable';",
            "if (msg.target !== this.record.pane_id) return;",
        ):
            self.assertIn(line, split)
        self.assertNotIn("wireTarget(", split)
        self.assertNotIn("wireRoute(", split)
        # Its subscribe goes through the shared frame builder with a `%N`, which
        # the key mapping leaves as it is; and inputRoute passes a split pane's
        # target and identity through untouched.
        result = run_js(["js/pane-ref.js"], r"""
_sessionPanes = [{target: 'dev:1.1', key: 'aaaaaaaaaaaaaaaa:%1', pane_id: '%1'}];
setTermTarget('dev:1.1');
const percent = wireTarget('%1');
globalThis.SplitView = {enabled: true};
globalThis.getInputTarget = () => 'dev:1.1';
globalThis.inputIdentity = () => ({expected_target_identity: {pane_id: '%1'}});
const split = inputRoute();
console.log(JSON.stringify({percent, split}));
""", PANE_PRELUDE)
        self.assertEqual(result["percent"], {"target": "%1"})
        self.assertEqual(result["split"], {"target": "dev:1.1",
                                           "expected_target_identity": {"pane_id": "%1"}})

    def test_a_gone_panes_composer_stays_with_its_key_and_does_not_follow(self):
        """The selected pane is gone (a tmux restart): its composer is flushed
        under its dead key, where the server keeps it as an orphan, and the pane
        the app falls back to shows its own draft, not the old pane's text."""
        result = run_js(["js/pane-ref.js", "js/drafts.js"], r"""
const dead = 'aaaaaaaaaaaaaaaa:%1', fresh = 'bbbbbbbbbbbbbbbb:%0';
_sessionPanes = [{target: 'dev:1.2', key: dead}];
setTermTarget('dev:1.2');
input.value = 'the old pane draft';
saveDraftSoon();
await flushDraft(dead);
// The new tmux server: what selectTab(fallback, true, true) does for a key.
_sessionPanes = [{target: 'dev:1.1', key: fresh}];
onTabSwitchDraft(dead, fresh, true);
setTermTarget('dev:1.1', fresh);
await new Promise(r => setTimeout(r, 0));
await new Promise(r => setTimeout(r, 0));
console.log(JSON.stringify({composer: input.value, puts: sent.filter(s => s.body).map(s => [s.body.target, s.body.text])}));
""", DRAFTS_PRELUDE)
        self.assertEqual(result["composer"], "")
        self.assertIn(["aaaaaaaaaaaaaaaa:%1", "the old pane draft"], result["puts"])
        self.assertNotIn(["bbbbbbbbbbbbbbbb:%0", "the old pane draft"], result["puts"])
        terminal = _js("js/terminal.js")
        self.assertIn("const replace = !auto || (paneGone && isPaneKey(leaving));", terminal)
        self.assertIn("selectTab(fallback.target, true, true);", _js("js/app.js"))

if __name__ == "__main__":
    unittest.main()
