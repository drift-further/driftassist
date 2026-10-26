"""Group navigation preserves all pane targets and server tab-state keys."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert');
vm.runInThisContext(fs.readFileSync('js/delegate-nav.js','utf8'));
const p=(session, parent=null, state='WORKING', pane='0.0')=>({session,target:session+':'+pane,pane,
 delegate:parent===null?null:{parent_session:parent,ancestor_session:parent,match:parent?'attached':'unmatched',state:state.toLowerCase(),health:'ok'}});
const panes=[p('a'),p('b'),p('del_a','a'),p('del_b','a','FINISHED'),p('del_a','a','WAITING','0.1'),p('orphan','')];
const ts={pinned:[],order:[],snoozed:{},sort:'manual'};
"""

class DelegateNavigationTests(unittest.TestCase):
    def run_js(self, body):
        r=subprocess.run(['node','-e',SETUP+body],cwd=ROOT,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_every_pane_once_and_orphan_last_in_tier(self):
        self.run_js("""
const nav=buildDelegateNavigation(panes,ts);
assert.deepEqual(nav.map(u=>u.session),['a','b','orphan']);
assert.equal(nav.flatMap(u=>[...u.parents,...u.children]).length,panes.length);
assert.deepEqual(navigationSessionOrder(nav),['a','del_a','del_b','b','orphan']);
""")

    def test_child_pin_pins_unit_and_snooze_does_not_erase_attention(self):
        self.run_js("""
ts.pinned=['del_a']; ts.snoozed={'a:0.0':{at:1}};
const nav=buildDelegateNavigation(panes,ts);
assert.equal(nav[0].pinned,true);
assert.equal(delegateGroupNeedsAttention(nav[0]),true);
assert.ok(ts.snoozed['a:0.0']);
""")

    def test_parent_reorder_moves_unit_child_reorder_stays_among_siblings(self):
        self.run_js("""
const nav=buildDelegateNavigation(panes,ts);
assert.deepEqual(reorderDelegateNavigation(nav,'a','b',true),['b','a','del_a','del_b','orphan']);
assert.deepEqual(reorderDelegateNavigation(nav,'del_a','del_b',true),['a','del_b','del_a','b','orphan']);
assert.deepEqual(reorderDelegateNavigation(nav,'del_a','b',true),navigationSessionOrder(nav));
""")

    def test_closed_or_renamed_parent_keeps_selected_child_as_orphan(self):
        self.run_js("""
const nav=buildDelegateNavigation(panes.filter(p=>p.session!=='a'),ts);
assert.ok(nav.find(u=>u.session==='del_a').orphan);
assert.equal(nav.flatMap(u=>[...u.parents,...u.children]).filter(p=>p.target==='del_a:0.1').length,1);
""")

    def test_finished_fold_first_active_target_remains_visible(self):
        self.run_js("""
const children=[p('w','a'),p('q','a','WAITING'),p('f1','a','FINISHED'),p('f2','a','FINISHED'),p('f3','a','FINISHED')];
let rows=delegateRailRows(children,'');
assert.deepEqual(rows.visible.map(p=>p.session),['w','q']); assert.equal(rows.hidden.length,3);
rows=delegateRailRows(children,'f3:0.0');
assert.ok(rows.visible.some(p=>p.session==='f3'));assert.ok(rows.visible.length<=3);
""")

    def test_nesting_disabled_preserves_flat_inventory(self):
        self.run_js("""
const nav=buildDelegateNavigation(panes,ts,false);
assert.equal(nav.length,5);
assert.equal(nav.find(u=>u.session==='del_a').orphan,false);
assert.equal(nav.find(u=>u.session==='orphan').orphan,true);
assert.ok(nav.every(u=>u.children.length===0));
""")

    def test_shared_renderer_holds_missing_nested_target_then_accepts_fresh_close(self):
        from tests.test_prompt_popup import run_popup_js
        result = run_popup_js(r"""
vm.runInThisContext(fs.readFileSync('js/target-hold.js','utf8'));
const app=fs.readFileSync('js/app.js','utf8');
vm.runInThisContext(app.slice(app.indexOf('function _applySessionsData('),app.indexOf('function _formatIdleTime(')));
globalThis._termTarget='del_child:0.1'; globalThis._termShowProjects=false;
globalThis._agentColors={}; globalThis._sessionPrompts={};
globalThis.renderDelegateNavigation=()=>{};
globalThis.markActiveTab=()=>{};globalThis.updateTmuxIndicator=()=>{};
globalThis._modelSeenSweep=()=>{};
const container=new FakeElement('div');document.getElementById=()=>container;
let now=1000;Date.now=()=>now;
noteTargetChosen(_termTarget,true); now=1200;
const parent={session:'parent',target:'parent:0.0'};
_applySessionsData([parent],'',900);
const held=_termTarget;
noteTargetListed(_termTarget);
_applySessionsData([parent],'',1300);
console.log(JSON.stringify([held,selectCalls]));
""")
        self.assertEqual(result, ['del_child:0.1', ['parent:0.0']])

    def test_snoozed_child_folds_until_active_or_waiting_without_mutating_record(self):
        self.run_js("""
const children=[p('work','a'),p('question','a','WAITING')];
const snoozed={'work:0.0':{at:1},'question:0.0':{at:1}};
assert.deepEqual(delegateRailRows(children,'',snoozed).visible.map(p=>p.session),['question']);
assert.equal(delegateRailRows(children,'work:0.0',snoozed).visible.length,2);
assert.equal(Object.keys(snoozed).length,2);
""")

    def test_orphan_pin_tier_and_multi_level_ancestor(self):
        self.run_js("""
ts.pinned=['orphan'];
assert.equal(buildDelegateNavigation(panes,ts)[0].session,'orphan');
const extra=p('grandchild','del_a');extra.delegate.ancestor_session='a';
const nav=buildDelegateNavigation([...panes,extra],ts);
assert.equal(nav.find(u=>u.session==='a').children.length,4);
""")

    def test_disclosure_and_selection_registry_unpacks_one_argument(self):
        self.run_js("""
globalThis.document={addEventListener(){}};
vm.runInThisContext(fs.readFileSync('js/actions-registry.js','utf8'));
let selected=null;globalThis.selectTab=t=>{selected=t};
_runAction('selectTab',['del_a:0.1']);assert.equal(selected,'del_a:0.1');
let opened=null;toggleDelegateRail=s=>{opened=s};
_runAction('toggleDelegateRail',['a']);assert.equal(opened,'a');
""")
