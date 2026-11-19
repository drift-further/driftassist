"""Real browser functions under Node: a states tick must preserve reorder and idle data."""
import unittest
from tests.test_prompt_popup import run_popup_js

DOM = r"""
const assert = require('assert');
FakeClassList.prototype.add=function(...xs){xs.forEach(x=>this.names.add(x))};
FakeClassList.prototype.remove=function(...xs){xs.forEach(x=>this.names.delete(x))};
FakeClassList.prototype.toggle=function(x,on){if(on===undefined)on=!this.contains(x);on?this.add(x):this.remove(x);};
Object.defineProperty(FakeElement.prototype,'parentElement',{get(){return this.parentNode}});
Object.defineProperty(FakeElement.prototype,'firstChild',{get(){return this.children[0]}});
FakeElement.prototype.appendChild=function(c){c.remove();c.parentNode=this;this.children.push(c);return c};
FakeElement.prototype.insertBefore=function(c,b){c.remove();c.parentNode=this;const i=this.children.indexOf(b);this.children.splice(i<0?this.children.length:i,0,c);return c};
FakeElement.prototype.prepend=function(c){this.insertBefore(c,this.firstChild)};
FakeElement.prototype.after=function(c){const next=this.parentNode.children[this.parentNode.children.indexOf(this)+1];this.parentNode.insertBefore(c,next)};
FakeElement.prototype.closest=function(s){for(let e=this;e;e=e.parentNode)if(e.matches(s))return e;return null};
FakeElement.prototype.matches=function(s){return s[0]==='.'?s.slice(1).split('.').every(c=>this.classList.contains(c)):s[0]==='#'?this.id===s.slice(1):false};
FakeElement.prototype.querySelectorAll=function(selector){
 const out=[];for(let part of selector.split(',')){part=part.trim();const direct=part.startsWith(':scope > ');part=part.replace(':scope > ','');
 for(const e of direct?this.children:this.all().slice(1))if(e.matches(part)&&!out.includes(e))out.push(e)}return out};
FakeElement.prototype.querySelector=function(s){return this.querySelectorAll(s)[0]||null};
Object.defineProperty(FakeElement.prototype,'innerHTML',{set(s){for(const c of [...this.children])c.remove();if(s.includes('reorder-cancel')){const c=new FakeElement('span');c.className='reorder-cancel';this.appendChild(c)}}});
const create=document.createElement;document.createElement=t=>{const e=create(t);e.dataset={};return e};
document.addEventListener=()=>{};document.removeEventListener=()=>{};
document.querySelector=s=>document.body.querySelector(s);
document.querySelectorAll=s=>document.body.querySelectorAll(s);
const nodes={};for(const id of ['session-tabs','delegate-rail-host','stale-sheet-body','stale-sheet-count']){const e=document.createElement('div');e.id=id;nodes[id]=e;document.body.appendChild(e)};
document.getElementById=id=>nodes[id]||document.body.all().find(e=>e.id===id)||null;
globalThis.window={};globalThis.CSS={escape:s=>s};
globalThis._termTarget='elsewhere:0.0';globalThis._termOpen=false;
globalThis._tabState={pinned:[],snoozed:{},order:[]};
globalThis._sessionStates={};globalThis._sessionPrompts={};globalThis._activityDecayTimers={};globalThis._pageLoadedAt=0;
globalThis._reorderModeTab=null;globalThis._reorderCleanup=null;globalThis._staleSheetOpen=false;globalThis._lastStaleCount=0;
globalThis._getStaleThreshold=()=>120;globalThis._isPinned=()=>false;globalThis._isSnoozed=()=>false;
globalThis._autoTuckEnabled=()=>true;globalThis._revealActiveTab=()=>{};globalThis._syncTabScrollButtons=()=>{};
globalThis._formatIdleTime=()=>"6m";
globalThis._sessionOf=t=>t.split(":")[0];globalThis.sessionOf=globalThis._sessionOf;
globalThis._tabLabelText=()=>'';globalThis.escHtml=x=>x;
globalThis._buildTabRow=()=>{const row=document.createElement('div'),name=document.createElement('span');name.className='row-name';row.appendChild(name);return row};
const tabs=fs.readFileSync('js/tabs.js','utf8');
function loadFn(name){const start=tabs.indexOf('function '+name+'(');let end=tabs.indexOf('\nfunction ',start+1);vm.runInThisContext(tabs.slice(start,end<0?undefined:end));}
for(const name of ['_enterReorderMode','_exitReorderMode','_buildDropZones','_applyStaleGroup'])loadFn(name);
vm.runInThisContext(fs.readFileSync('js/delegate-nav.js','utf8'));
const app=fs.readFileSync('js/app.js','utf8');
vm.runInThisContext(app.slice(app.indexOf('function _applyStatesData('),app.indexOf('function _applyScanData(')));
const parent={session:'parent',target:'parent:0.0'}, child={session:'del_child',target:'del_child:0.0',delegate:{state:'finished',health:'ok',match:'attached',parent_session:'parent'}};
_sessionPanes=[parent,child];_delegateNavigation=buildDelegateNavigation(_sessionPanes,_tabState);
const wrap=document.createElement('div');wrap.className='navigation-unit';wrap.dataset.session='parent';nodes['session-tabs'].appendChild(wrap);
for(const pane of _sessionPanes){const tab=document.createElement('button');tab.className='session-tab';tab.dataset.target=pane.target;_delegateTabElements.set(pane.target,tab);wrap.appendChild(tab)}
// Only target selectors need attribute matching in this deliberately small DOM.
const query=document.querySelector;document.querySelector=s=>s.startsWith('.session-tab[data-target=')?_delegateTabElements.get(s.match(/data-target="([^"]+)/)[1]):query(s);
"""

class DelegateRailTickTests(unittest.TestCase):
    def test_child_reorder_survives_states_tick_and_cancel_is_inside_sheet(self):
        self.assertEqual(run_popup_js(DOM+r"""
const tab=_delegateTabElements.get(child.target);_enterReorderMode(tab);
const sheet=tab.parentElement;
_applyStatesData({[parent.target]:{state:'shell',idle_seconds:0},[child.target]:{state:'shell',idle_seconds:0}});
console.log(JSON.stringify([sheet.querySelectorAll('.session-tab').length,!!sheet.querySelector('.reorder-banner'),tab.parentElement===sheet]));
"""), [1, True, True])

    def test_finished_unit_tucks_but_waiting_and_working_children_keep_it_visible(self):
        for state, tucked in [('finished',True),('waiting',False),('working',False)]:
            self.assertEqual(run_popup_js(DOM+f"child.delegate.state='{state}';"+r"""
_applyStatesData({[parent.target]:{state:'shell',idle_seconds:400},[child.target]:{state:'shell',idle_seconds:400}});
console.log(JSON.stringify([_delegateTabElements.get(child.target).dataset.idleSeconds,wrap.classList.contains('stale-tucked'),nodes['stale-sheet-body'].children.length]));
"""), [400, tucked, 1 if tucked else 2])
