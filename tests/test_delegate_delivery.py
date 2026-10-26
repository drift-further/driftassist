"""Delegate browser delivery: authoritative state and per-browser alert keys."""
import unittest
from tests.test_prompt_popup import run_popup_js

SETUP = r"""
document.addEventListener = () => {};
vm.runInThisContext(fs.readFileSync('js/monitor.js', 'utf8'));
const app = fs.readFileSync('js/app.js', 'utf8');
vm.runInThisContext(app.slice(app.indexOf('function _applyStatesData('), app.indexOf('// Startup')));
globalThis.window = {isSecureContext: true};
globalThis._pageLoadedAt = 1000;
globalThis._notifPermission = 'granted';
globalThis.notifications = [];
globalThis.Notification = function(title, options) { notifications.push({title, options}); };
globalThis._termOpen = false; globalThis._termTarget = '';
globalThis._sessionStates = {}; globalThis._sessionPrompts = {};
globalThis._activityDecayTimers = {}; globalThis._notifSentFor = {}; globalThis._lastScanContent = {};
globalThis.CSS = {escape: s => s};
const tab = new FakeElement('button');
tab.querySelector = s => tab.find(s.slice(1));
tab.dataset = {};
document.querySelector = () => tab;
globalThis.detectionTail = s => s;
globalThis.detectSmartActions = () => question().detected;
globalThis.copyToClipboard = () => true;
const pane = {target: 'del_api:0.0', session: 'del_api', pane_id: '%1', tail: 'approve?',
 prompt: {fp: 'fp1', summary: 'Approve?', autoyes: true},
 delegate: {state: 'working', health: 'ok', alert: null}};
_sessionPanes = [pane];
function apply() {
 _applyStatesData({[pane.target]: {state: 'shell'}});
 _applyScanData([pane]);
 _applyDelegateAlerts(_sessionPanes);
}
function alert(state, key, at=2) {
 pane.delegate.state = state.toLowerCase();
 pane.delegate.alert = {key, state: state.toLowerCase(), at, reason: 'question', detail: 'Choose a path'};
}
"""

class DelegateDeliveryTests(unittest.TestCase):
    def run_js(self, body):
        return run_popup_js(SETUP + body)

    def test_owned_approval_suppresses_every_legacy_surface(self):
        self.assertEqual(self.run_js("""
_sessionStates[pane.target] = {state: 'running'};
apply(); console.log(JSON.stringify([notifications.length, _promptPopups.size,
 tab.classList.contains('has-prompt'), !!_sessionPrompts[pane.target]]));
"""), [0, 0, False, False])

    def test_waiting_is_once_per_browser(self):
        for _ in range(2):
            self.assertEqual(self.run_js("""
alert('WAITING', 'key1'); apply(); apply();
console.log(JSON.stringify([notifications.length, _promptPopups.size]));
"""), [1, 1])

    def test_new_finished_key_alerts_again_but_replayed_key_does_not(self):
        self.assertEqual(self.run_js("""
alert('FINISHED', 'one'); apply(); alert('FINISHED', 'two'); apply();
alert('FINISHED', 'one'); apply();
console.log(JSON.stringify(notifications.length));
"""), 2)

    def test_http_and_denied_notifications_still_have_in_app_popup(self):
        for gate in ["window.isSecureContext = false", "_notifPermission = 'denied'"]:
            self.assertEqual(self.run_js(gate + "; alert('WAITING', 'key'); apply(); console.log(JSON.stringify([notifications.length, _promptPopups.size]));"), [0, 1])

    def test_non_delegate_keeps_legacy_notification(self):
        self.assertEqual(self.run_js("pane.delegate = null; apply(); console.log(JSON.stringify([notifications.length, tab.classList.contains('has-prompt')]));"), [1, True])

    def test_dead_offers_open_and_copy_resume(self):
        self.assertEqual(self.run_js("""
pane.resume = {line: 'cd /tmp && codex resume abc'};
alert('DEAD', 'dead'); apply();
console.log(JSON.stringify([..._promptPopups.values()][0].el.labels()));
"""), ['Open tab', 'Copy resume'])

    def test_in_view_key_is_seen_without_system_notification(self):
        for view in ["_termOpen=true; _termTarget=pane.target", "globalThis.SplitView={readable:t=>t===pane.target}"]:
            self.assertEqual(self.run_js(view + """;
alert('WAITING','viewed'); apply();
_termOpen=false; globalThis.SplitView={readable:()=>false}; apply();
console.log(JSON.stringify([notifications.length,_promptPopups.size]));
"""), [0, 0])

    def test_old_alert_is_seen_and_only_new_key_pops_once(self):
        self.assertEqual(self.run_js("""
alert('FINISHED', 'old', 0.5); apply();
const old = [_promptPopups.size, _promptPopupSeen.delegate.has('delegate:old')];
alert('FINISHED', 'new', 2); apply();
const fresh = _promptPopups.size;
_closePromptPopup('delegate:new'); apply();
alert('FINISHED', 'old', 3); apply();
console.log(JSON.stringify([old, fresh, _promptPopups.size, notifications.length]));
"""), [[0, True], 1, 0, 1])
