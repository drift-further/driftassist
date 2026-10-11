"""Split views retain one explicit input owner and refuse replacement panes."""
import json
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.input import input_bp
from routes.terminal import terminal_bp
from shared.tmux import ExpectedTargetIdentity, DeliveryResult

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ExpectedTargetIdentity('/tmp/private/default', 1, 2, 3, '4', '$1', '@2', '%3', 5, '6')


class SplitViewRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(input_bp)
        self.app.register_blueprint(terminal_bp)
        self.client = self.app.test_client()

    def test_carried_identity_delivers_only_to_that_generation_in_both_directions(self):
        for target, identity in [('left:0.0', IDENTITY), ('right:1.1', replace(IDENTITY, pane_id='%4', pane_pid=7))]:
            with self.subTest(target=target), patch('routes.input.expected_target_identity', return_value=identity), patch('routes.input.generation_bound_delivery', return_value=DeliveryResult('delivered')) as delivery, patch('routes.input.state.touch_activity'):
                response = self.client.post('/key', json={'target':target,'keys':'Return','expected_target_identity':identity.as_dict()})
                self.assertEqual(response.status_code,200)
                self.assertEqual(delivery.call_args.args[0],identity)
                other = replace(identity,pane_pid=99)
                for url, payload in [('/key',{'keys':'Return'}),('/key',{'combo':{'ctrl':True,'alt':False,'shift':False,'key':'c'}}),('/type',{'text':'never send this','enter':True})]:
                    delivery.reset_mock()
                    refused = self.client.post(url,json={**payload,'target':target,'expected_target_identity':other.as_dict()})
                    self.assertEqual(refused.status_code,409)
                    delivery.assert_not_called()

    def test_identity_endpoint_missing_and_present(self):
        with patch('routes.terminal.expected_target_identity',return_value=IDENTITY):
            self.assertEqual(self.client.get('/terminal/identity?target=%3').json['identity'],IDENTITY.as_dict())
        with patch('routes.terminal.expected_target_identity',return_value=None):
            self.assertEqual(self.client.get('/terminal/identity?target=%3').status_code,409)

    def test_fit_uses_selected_window_and_never_resolves_missing_pane_to_session(self):
        for target,window in [('%3','@2'),('%4','@7')]:
            identity=replace(IDENTITY,pane_id=target,window_id=window)
            with patch('routes.terminal.expected_target_identity',return_value=identity), patch('routes.terminal.subprocess.run') as run:
                run.return_value.returncode=0
                response=self.client.post('/terminal/resize',json={'session':'work','target':target,'cols':80,'rows':60})
                self.assertEqual(response.status_code,200)
                command=run.call_args.args[0]
                self.assertEqual(command[command.index('-t')+1],window)
        with patch('routes.terminal.expected_target_identity',return_value=None), patch('routes.terminal.subprocess.run') as run:
            self.assertEqual(self.client.post('/terminal/resize',json={'session':'work','target':'%3','cols':80,'rows':60}).status_code,409)
            run.assert_not_called()


class SplitViewClientTests(unittest.TestCase):
    def test_late_draft_save_cannot_replace_recovered_send(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
let reply;
const context={_draftSaveTimer:null,_draftDirtyFor:null,_draftCache:{a:{text:'old'}},_draftSeq:{a:1},
 _draftRev:{},_draftMarks:new Set(),_draftTarget:()=> 'b',_renderDraftMarks(){},clearTimeout(){},
 fetch:()=>new Promise(resolve=>reply=resolve)};
vm.createContext(context);
const source=fs.readFileSync('js/drafts.js','utf8');
vm.runInContext(source.slice(source.indexOf('async function flushDraft('),source.indexOf('// `force` says')),context);
const pending=context.flushDraft('a');
context._draftCache.a={text:'recovered'};context._draftSeq.a++;
reply({json:async()=>({ok:true,draft:{text:'old'}})});
pending.then(()=>assert.equal(context._draftCache.a.text,'recovered')).catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_companion_transport_rejects_wrong_and_superseded_frames_and_preserves_scroll(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const nodes={};for(const key of ['pre','.split-latest','.split-more','.split-page','.split-dim'])nodes[key]={style:{},hidden:false,innerHTML:'',textContent:''};
const element={querySelector:k=>nodes[k],addEventListener(){},dataset:{},scrollHeight:500,scrollTop:0,clientHeight:100};
const sockets=[];
class WS {constructor(){this.readyState=1;sockets.push(this)} send(text){this.sent=text} close(){this.closed=true}}
const context={console,innerWidth:1280,Date,Map,JSON,Math,WebSocket:WS,location:{protocol:'http:',host:'private.test'},
 document:{hidden:false},window:{getSelection:()=>({toString:()=>''})},
 setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},requestAnimationFrame:fn=>fn(),
 _tuiOverride:{},_termFontSize:12,_paneInfo:{},_paneTui:{},_isTuiInfo:()=>false,ansiToHtml:text=>'escaped:'+text,
 _tuiSubscribeFrame:(target,lines)=>JSON.stringify({target,lines}),syncAutoYesState:async()=>{},_termWsConnected:true};
vm.createContext(context);vm.runInContext(fs.readFileSync('js/split-view.js','utf8').split('SplitView.init();')[0]+'\nglobalThis.view=SplitView; globalThis.Controller=PaneView;',context);
context.view.renderCountdowns=()=>{};
const pane=new context.Controller(element);
pane.watch({target:'a:0.0',pane_id:'%1',identity:{pane_pid:1}});const old=sockets[0];old.onopen();
old.onmessage({data:JSON.stringify({type:'full',target:'%2',content:'wrong'})});assert.equal(pane.content,'');
old.onmessage({data:JSON.stringify({type:'full',target:'%1',content:'first'})});assert.equal(nodes.pre.innerHTML,'escaped:first');
pane.paused=true;old.onmessage({data:JSON.stringify({type:'full',target:'%1',content:'later'})});assert.equal(nodes.pre.innerHTML,'escaped:first');assert.equal(pane.content,'later');
nodes['.split-latest'].onclick();assert.equal(nodes.pre.innerHTML,'escaped:later');
pane.watch({target:'b:1.0',pane_id:'%2',identity:{pane_pid:2}});const live=sockets[1];
old.onmessage({data:JSON.stringify({type:'full',target:'%1',content:'stale'})});old.onclose();assert.equal(pane.ws,live);assert.equal(pane.content,'');
pane.close();assert.equal(live.closed,true);assert.equal(pane.ws,null);
console.log('PASS independent view transport and scroll');
'''
        result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_owner_swap_readability_dead_pane_and_targeted_countdowns(self):
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const context={console,innerWidth:1280,Date,Map,JSON,Math,setTimeout,clearTimeout,setInterval,clearInterval,
 document:{hidden:false},window:{},_termTarget:'left:0.0',_attachments:[],_sessionPrompts:{},
 localStorage:{setItem(){},getItem(){return null}},showFlash(){},isAutoYes(){return false},
 fetch:async(url,opts)=>{context.requests.push({url,body:JSON.parse(opts.body)});return {ok:true,json:async()=>({ok:true,cancelled:true})}},requests:[]};
vm.createContext(context);
const source=fs.readFileSync('js/split-view.js','utf8').split('SplitView.init();')[0];
vm.runInContext(source+'\nglobalThis.view=SplitView; globalThis.PaneView=PaneView;',context);
const view=context.view;
view.update=()=>{};view.renderCountdowns=()=>{};
view.enabled=true;view.slots=[{target:'left:0.0',pane_id:'%1',identity:{pane_pid:1},dead:false},{target:'right:1.1',pane_id:'%2',identity:{pane_pid:2},dead:false}];
assert.equal(view.inputTarget(),'left:0.0');assert.equal(view.readable('right:1.1'),true);
context.innerWidth=390;assert.equal(view.readable('right:1.1'),false);
let selected=null;view.snapshot=()=>{};view.select=r=>{selected=r.target};view.other={watch(){},snapshot(){}};
view.activate(1);assert.equal(selected,'right:1.1');assert.equal(view.inputTarget(),'right:1.1');assert.equal(view.readable('left:0.0'),false);
view.swap();assert.equal(view.active,0);assert.equal(view.inputTarget(),'right:1.1');
view.slots[0].dead=true;assert.equal(view.inputTarget(),'%split-view-unavailable');
view.slots[0].dead=false;
view.event({target:'left:0.0',event:'countdown',remaining:5});view.event({target:'right:1.1',event:'countdown',remaining:7});
view.event({target:'left:0.0',event:'cancelled'});assert.equal(view.countdowns.has('right:1.1'),true);
view.event({target:'left:0.0',event:'countdown',remaining:4});
(async()=>{const cd=view.countdowns.get('left:0.0');await view.cancel('left:0.0',cd);assert.equal(view.inputTarget(),'right:1.1');assert.equal(view.countdowns.has('right:1.1'),true);assert.equal(context.requests[0].body.target,'left:0.0');
context.fetch=async()=>({ok:false,json:async()=>({ok:false})});view.event({target:'left:0.0',event:'countdown',remaining:4});await view.cancel('left:0.0',view.countdowns.get('left:0.0'));assert.equal(view.countdowns.has('left:0.0'),true);
console.log('PASS split owner, swap, phone visibility, dead target, simultaneous countdowns and refused STOP');})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result=subprocess.run(['node','-e',script],cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('PASS split owner',result.stdout)
