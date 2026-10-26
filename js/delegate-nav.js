// Derived navigation only: persisted pin/order/snooze keys remain unchanged.
let _delegateNavigation = [];
let expandedDelegateParent = null;
let _delegateSheetOpen = false;
const _delegateTabElements = new Map();
function delegateState(pane) { return String(pane.delegate?.state || '').toUpperCase(); }

function buildDelegateNavigation(panes, tabState, nest = true) {
    const sessions = new Map();
    for (const pane of panes) {
        if (!sessions.has(pane.session)) sessions.set(pane.session, []);
        sessions.get(pane.session).push(pane);
    }
    const owner = session => {
        const d = sessions.get(session)[0].delegate;
        const parent = d?.ancestor_session || d?.parent_session;
        return nest && d?.match === 'attached' && parent !== session && sessions.has(parent) ? parent : session;
    };
    const roots = new Map();
    for (const session of sessions.keys()) {
        let root = owner(session), seen = new Set([session]);
        while (!seen.has(root) && owner(root) !== root) { seen.add(root); root = owner(root); }
        if (seen.has(root) && root !== session) root = session;
        roots.set(session, root);
    }
    const units = new Map();
    for (const [session, root] of roots) {
        if (!units.has(root)) {
            const parents = sessions.get(root);
            units.set(root, {session: root, parents, children: [], pinned: false,
                orphan: !!parents[0].delegate && (parents[0].delegate.match !== 'attached' ||
                    !sessions.has(parents[0].delegate.ancestor_session || parents[0].delegate.parent_session))});
        }
        const unit = units.get(root);
        if (root !== session) unit.children.push(...sessions.get(session));
        if ((tabState.pinned || []).includes(session)) unit.pinned = true;
    }
    // Server order is already manual/name/created within each tier. A child's
    // pin promotes the unit; the parent's position supplies the unit order.
    const position = new Map([...sessions.keys()].map((s,i)=>[s,i]));
    return [...units.values()].sort((a,b)=>Number(b.pinned)-Number(a.pinned) ||
        Number(a.orphan)-Number(b.orphan) || position.get(a.session)-position.get(b.session));
}

function navigationSessionOrder(units) {
    return [...new Set(units.flatMap(u=>[...u.parents,...u.children].map(p=>p.session)))];
}
function delegateGroupNeedsAttention(unit) {
    return unit.children.some(p=>delegateState(p) === 'WAITING');
}
function reorderDelegateNavigation(units, source, anchor, after=false) {
    const result = units.map(u=>({...u, children:u.children.slice()}));
    const from = result.find(u=>u.session===source || u.children.some(p=>p.session===source));
    const to = result.find(u=>u.session===anchor || u.children.some(p=>p.session===anchor));
    if (!from || source===anchor) return navigationSessionOrder(result);
    if (from.session===source && (!to || to.session===anchor)) {
        result.splice(result.indexOf(from),1);
        let at=to?result.indexOf(to)+(after?1:0):result.length;
        result.splice(at,0,from);
    } else if (from.session!==source && (!to || (from===to && to.session!==anchor))) {
        const moving=from.children.filter(p=>p.session===source);
        from.children=from.children.filter(p=>p.session!==source);
        let at=anchor?from.children.findIndex(p=>p.session===anchor):from.children.length;
        if (after) while (at<from.children.length && from.children[at].session===anchor) at++;
        from.children.splice(at,0,...moving);
    }
    return navigationSessionOrder(result);
}
function delegateRailRows(children, active, snoozed = {}) {
    const rank=p=>p.target===active?0:delegateState(p)==='WAITING'?1:snoozed[p.target]?4:delegateState(p)==='WORKING'?2:3;
    const preferred=children.filter(p=>rank(p)<3);
    const chosen=new Set((preferred.length?preferred:children.filter(p=>rank(p)<4)).slice().sort((a,b)=>rank(a)-rank(b)).slice(0,3));
    return {visible:children.filter(p=>chosen.has(p)),hidden:children.filter(p=>!chosen.has(p))};
}
function _delegateUnit(target) {
    return _delegateNavigation.find(u=>[...u.parents,...u.children].some(p=>p.target===target));
}
function _allNavigationTabs() { return [..._delegateTabElements.values()]; }

function _makeSessionTab(p, current, teamSessions) {
    const isAgent=!!p.agent_name, isSubpane=!isAgent && !!p.is_subpane;
    const tab=document.createElement('button');
    tab.className='session-tab';
    if (isAgent) tab.classList.add('agent-tab');
    if (isSubpane) tab.classList.add('agent-tab','subpane-tab');
    if (!isAgent && teamSessions.has(p.session)) tab.classList.add('team-lead-tab');
    tab.dataset.target=p.target;
    if (isAgent) tab.dataset.agentName=p.agent_name;
    tab.textContent=agentDisplayName(p) || (isSubpane?shortName(p.session)+'·'+p.pane:shortName(p.session));
    const color=_agentColors[p.agent_color];
    if (isAgent && color) { tab.style.borderLeftWidth='3px';tab.style.borderLeftColor=color; }
    else if (p.session.endsWith('-auto')) { tab.style.borderLeftWidth='3px';tab.style.borderLeftColor='var(--amber)'; }
    const dot=document.createElement('span');dot.className='tab-dot';tab.appendChild(dot);
    applyTabModel(tab,p); applyDelegateMark(tab,p);
    if (!p.delegate?.state && _sessionPrompts[p.target]) tab.classList.add('has-prompt');
    if (p.target===current) tab.classList.add('active');
    tab.dataset.action='selectTab';tab.dataset.arg=JSON.stringify(p.target);
    tab.title=p.session+' · '+p.pane;
    if (p.delegate?.target_repo) {
        const suffix=document.createElement('span');suffix.className='delegate-target';
        suffix.textContent=' → '+p.delegate.target_repo;tab.appendChild(suffix);
    }
    _delegateTabElements.set(p.target,tab);
    return tab;
}
function renderDelegateNavigation(panes,current) {
    const container=document.getElementById('session-tabs');
    const host=document.getElementById('delegate-rail-host');
    if (!host) return;
    _delegateNavigation=buildDelegateNavigation(panes, _tabState, SETTINGS?.delegates?.nest_tabs!=='off');
    _delegateTabElements.clear();container.innerHTML='';host.innerHTML='';
    const teamSessions=new Set(panes.filter(p=>p.agent_name).map(p=>p.session));
    const collapsed=_delegateCollapsed();
    for (const unit of _delegateNavigation) {
        const wrap=document.createElement('div');wrap.className='navigation-unit';wrap.dataset.session=unit.session;
        wrap.classList.toggle('pinned',unit.pinned);
        for (const pane of unit.parents) {
            const tab=_makeSessionTab(pane,current,teamSessions);
            if (unit.orphan) {
                tab.classList.add('delegate-orphan');
                const label=document.createElement('span');label.className='delegate-orphan-label';
                label.textContent='parent not open';tab.appendChild(label);
                tab.title+=' · '+(pane.delegate?.match || 'unmatched')+' · parent not open';
            }
            wrap.appendChild(tab);
        }
        if (unit.children.length && collapsed.has(unit.session)) {
            const bar=document.createElement('button');bar.className='delegate-disclosure collapsed';
            bar.dataset.action='showDelegateChip';bar.dataset.arg=JSON.stringify(unit.session);
            const count=new Set(unit.children.map(p=>p.session)).size;
            bar.title=count+' '+shortName(unit.session)+' delegates hidden · click to show';
            bar.setAttribute('aria-label',bar.title);
            wrap.appendChild(bar);
        } else if (unit.children.length) {
            const button=document.createElement('button');button.className='delegate-disclosure';
            button.dataset.action='toggleDelegateRail';button.dataset.arg=JSON.stringify(unit.session);
            button.setAttribute('aria-controls','delegate-rail-host');
            const count=new Set(unit.children.map(p=>p.session)).size;
            button.textContent='▸ '+count;
            const states=new Set();
            for (const child of unit.children) {
                const key=child.delegate?.health==='ok'?delegateState(child):'UNKNOWN';
                if (states.has(key)) continue;
                states.add(key);
                const holder=document.createElement('span');
                applyDelegateMark(holder,child);
                const mark=holder.querySelector('.delegate-mark');
                if (mark) button.appendChild(mark);
            }
            button.setAttribute('aria-label',count+' '+shortName(unit.session)+' delegates: '+[...new Set(unit.children.map(p=>p.delegate?.state||'unknown'))].join(', '));
            wrap.appendChild(button);
        }
        container.appendChild(wrap);
        for (const pane of unit.children) {
            const tab=_makeSessionTab(pane,current,teamSessions);
            const age=document.createElement('span');age.className='delegate-age';
            if (pane.created) age.textContent=Math.floor(Math.max(0,Date.now()/1000-pane.created)/60)+'m old';
            age.title='Session age';tab.appendChild(age);
            const reason=document.createElement('span');reason.className='delegate-reason';
            reason.textContent=[pane.delegate?.reason,pane.delegate?.detail].filter(Boolean).join(' · ');
            reason.title=reason.textContent;tab.appendChild(reason);
            const arrow=document.createElement('span');arrow.className='delegate-focus';arrow.textContent='↗';arrow.setAttribute('aria-hidden','true');tab.appendChild(arrow);
        }
    }
    refreshDelegateRail();
}
// Parents whose delegate chip is collapsed to a bar. Per browser, by session name.
const _DELEGATE_COLLAPSED_KEY='assist.delegateCollapsed';
function _delegateCollapsed() {
    try { return new Set(JSON.parse(localStorage.getItem(_DELEGATE_COLLAPSED_KEY)||'[]')); }
    catch (e) { return new Set(); }
}
function setDelegateChipCollapsed(session, collapse) {
    const set=_delegateCollapsed();
    if (collapse) set.add(session); else set.delete(session);
    try { localStorage.setItem(_DELEGATE_COLLAPSED_KEY,JSON.stringify([...set])); } catch (e) { /* this page only */ }
    if (collapse && expandedDelegateParent===session) expandedDelegateParent=null;
    _applySessionsData(_sessionPanes||[],_termTarget);
}
// Right-click / long-press on a chip or its bar: one item, Collapse or Show.
function _createDelegateChipMenu(chip, x, y) {
    _removeContextMenu();
    const session=JSON.parse(chip.dataset.arg);
    const collapse=!chip.classList.contains('collapsed');
    const menu=document.createElement('div');menu.className='tab-context-menu';
    menu.style.left=x+'px';menu.style.top=y+'px';
    const item=document.createElement('button');item.className='tab-ctx-item';
    item.textContent=collapse?'Collapse delegates':'Show delegates';
    item.onclick=function(e) { e.stopPropagation();_removeContextMenu();setDelegateChipCollapsed(session,collapse); };
    menu.appendChild(item);document.body.appendChild(menu);
    requestAnimationFrame(()=>{
        const rect=menu.getBoundingClientRect();
        if (rect.right>window.innerWidth) menu.style.left=(window.innerWidth-rect.width-8)+'px';
        if (rect.bottom>window.innerHeight) menu.style.top=(y-rect.height)+'px';
    });
    setTimeout(()=>{
        document.addEventListener('touchstart',_onOutsideTap,{once:true});
        document.addEventListener('click',_onOutsideTap,{once:true});
    },50);
}
function toggleDelegateRail(session) {
    expandedDelegateParent=expandedDelegateParent===session?null:session;
    _delegateSheetOpen=false;refreshDelegateRail();
    _enforceDelegateFloor(true);
}
function delegateSelectTarget(target) {
    const unit=_delegateUnit(target);
    if (unit?.session!==expandedDelegateParent) expandedDelegateParent=null;
    _delegateSheetOpen=false;refreshDelegateRail();
}
function openDelegateSheet() { _delegateSheetOpen=true;refreshDelegateRail(); }
function closeDelegateSheet() { _delegateSheetOpen=false;refreshDelegateRail(); }
function refreshDelegateRail() {
    if (typeof _reorderModeTab !== 'undefined' && _reorderModeTab?.closest('.delegate-sheet')) return;
    const host=document.getElementById('delegate-rail-host');
    if (!host) return;
    host.innerHTML='';
    document.getElementById('delegate-sheet')?.remove();
    const unit=_delegateNavigation.find(u=>u.session===expandedDelegateParent && u.children.length);
    if (!unit) { expandedDelegateParent=null;_delegateSheetOpen=false; }
    document.querySelectorAll('.delegate-disclosure').forEach(b=>{
        const session=JSON.parse(b.dataset.arg);
        b.setAttribute('aria-expanded',String(session===expandedDelegateParent));
        b.classList.toggle('contains-active-child',_delegateNavigation.find(u=>u.session===session)?.children.some(p=>p.target===_termTarget));
    });
    const bank=document.createElement('div');bank.hidden=true;host.appendChild(bank);
    for (const u of _delegateNavigation) for (const p of u.children) bank.appendChild(_delegateTabElements.get(p.target));
    host.classList.toggle('is-open',!!unit);
    if (!unit) return;
    const label=document.createElement('div');label.className='delegate-rail-label';label.textContent=shortName(unit.session)+' delegates';host.appendChild(label);
    const rows=delegateRailRows(unit.children,_termTarget,_tabState.snoozed);
    for (const p of rows.visible) {
        const tab=_delegateTabElements.get(p.target);tab.classList.add('delegate-child-row');tab.classList.remove('stale-tucked');
        tab.classList.toggle('active',p.target===_termTarget);
        host.appendChild(tab);
    }
    if (rows.hidden.length) {
        const more=document.createElement('button');more.className='delegate-more';more.dataset.action='openDelegateSheet';
        const finished=rows.hidden.every(p=>!['WORKING','WAITING','DEAD'].includes(delegateState(p)));
        more.textContent='+'+rows.hidden.length+(finished?' finished / idle':' more');host.appendChild(more);
    }
    let sheet=document.getElementById('delegate-sheet');
    if (sheet) sheet.remove();
    if (_delegateSheetOpen) {
        sheet=document.createElement('section');sheet.id='delegate-sheet';sheet.className='delegate-sheet';
        sheet.setAttribute('role','dialog');sheet.setAttribute('aria-label',shortName(unit.session)+' delegates');
        const close=document.createElement('button');close.textContent='Close delegates';close.dataset.action='closeDelegateSheet';sheet.appendChild(close);
        for (const p of rows.hidden) { const tab=_delegateTabElements.get(p.target);tab.classList.add('delegate-child-row');tab.classList.remove('stale-tucked');sheet.appendChild(tab); }
        document.body.appendChild(sheet);
    }
    requestAnimationFrame(()=>_enforceDelegateFloor(false));
}
function _enforceDelegateFloor(showMessage) {
    if (!expandedDelegateParent) return;
    const display=document.getElementById('term-display');
    if (display && display.clientHeight<320) {
        expandedDelegateParent=null;refreshDelegateRail();
        if (showMessage) showFlash('error','Close a panel to show delegates');
    }
}
if (typeof document!=='undefined') document.addEventListener('DOMContentLoaded',()=>{
    const display=document.getElementById('term-display');
    if (display && typeof ResizeObserver!=='undefined') new ResizeObserver(()=>_enforceDelegateFloor(false)).observe(display);
    window.visualViewport?.addEventListener('resize',()=>_enforceDelegateFloor(false));
});
