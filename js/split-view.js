// Two browser views of existing tmux panes. The selected view keeps the existing
// terminal/composer controller; PaneView owns the companion's transport/render state.
// Closing a view only closes its socket. Command execution has a separate lifetime.
class PaneView {
    constructor(element) {
        this.element = element;
        this.pre = element.querySelector('pre');
        this.latest = element.querySelector('.split-latest');
        this.record = null;
        this.content = '';
        this.lines = 2000;
        this.paused = false;
        this.programmatic = false;
        this.generation = 0;
        this.delay = 1000;
        this.latest.onclick = () => { this.paused = false; this.paint(); };
        element.addEventListener('scroll', () => {
            if (!this.programmatic) this.paused = element.scrollHeight - element.scrollTop - element.clientHeight > 50;
        });
        element.addEventListener('click', e => {
            if (e.target.closest('button,a') || window.getSelection().toString()) return;
            SplitView.activate(1 - SplitView.active);
        });
        element.querySelector('.split-more').onclick = () => {
            this.lines = Math.min(20000, this.lines + 2000);
            this.subscribe();
            this.capture(this.generation);
        };
        element.querySelector('.split-page').onclick = () => {
            if (this.record) fetch('/key', {method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({target:this.record.pane_id, keys:'Page_Up', expected_target_identity:this.record.identity})});
        };
    }
    watch(record) {
        this.snapshot(); this.close();
        this.record = record;
        this.content = record.content || '';
        this.paused = !!record.paused;
        this.lines = record.lines || 2000;
        this.pre.innerHTML = ansiToHtml((record.paused ? record.displayContent : record.content) || '');
        this.displayContent = record.displayContent || '';
        this.element.scrollTop = record.top || 0; this.element.scrollLeft = record.left || 0;
        this.paint();
        if (!record.dead && record.identity && !document.hidden) this.connect(this.generation);
    }
    subscribe() {
        if (this.ws && this.ws.readyState === 1) {
            const mode = _tuiSubscribeFrame(this.record.pane_id, this.lines);
            // Overrides are stored by canonical tab target.
            const msg = JSON.parse(mode);
            const override = _tuiOverride[this.record.target];
            if (override !== undefined) msg.tui = override === 'tui';
            this.ws.send(JSON.stringify(msg));
        }
    }
    connect(gen) {
        if (gen !== this.generation || !this.record || this.record.dead || document.hidden) return;
        const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/terminal/stream`);
        this.ws = ws;
        const owns = () => gen === this.generation && this.ws === ws;
        const heartbeat = () => {
            clearTimeout(this.inactive);
            this.inactive = setTimeout(() => { if (owns()) ws.close(); }, 8000);
        };
        ws.onopen = () => {
            if (!owns()) return;
            this.delay = 1000;
            clearInterval(this.fallback);
            this.fallback = null;
            this.subscribe(); heartbeat();
            this.ping = setInterval(() => { if (owns() && ws.readyState === 1) ws.send('{"type":"ping"}'); },10000);
            syncAutoYesState().then(() => SplitView.renderCountdowns());
        };
        ws.onmessage = e => {
            if (!owns()) return;
            heartbeat();
            try {
                const msg = JSON.parse(e.data);
                // The main stream consumes broadcasts. If it is offline the
                // companion takes over; polling repairs any event gap.
                if (msg.type === 'autoyes') {
                    if (!_termWsConnected) handleAutoYesWsMessage(msg);
                    return;
                }
                if (msg.target !== this.record.pane_id) return;
                if (msg.type === 'full') this.frame(msg.content || '', msg.info);
                if (msg.type === 'error') this.element.dataset.connection = 'Target unavailable';
            } catch (e) { /* malformed frame is not a target change */ }
        };
        ws.onclose = () => {
            if (!owns()) return;
            this.ws = null;
            clearTimeout(this.inactive); clearInterval(this.ping);
            this.element.dataset.connection = 'Reconnecting';
            this.capture(gen);
            if (!this.fallback) this.fallback = setInterval(() => this.capture(gen),3000);
            this.retry = setTimeout(() => this.connect(gen),this.delay);
            this.delay = Math.min(this.delay * 2, _getWsReconnectMax());
        };
        ws.onerror = () => {};
    }
    async capture(gen) {
        const record = this.record;
        if (gen !== this.generation || !record || record.dead || document.hidden) return;
        try {
            const resp = await fetch(`/terminal/capture?target=${encodeURIComponent(record.pane_id)}&lines=${this.lines}${_tuiQuery(record.target)}`);
            if (authLost(resp)) return;
            const data = await resp.json();
            if (gen !== this.generation || this.record !== record) return;
            if (data.ok) this.frame(data.content || '',data.info);
        } catch (e) { /* keep the last transcript */ }
    }
    frame(content, info) {
        this.content = content;
        this.record.content = content;
        if (info) {
            _paneInfo[this.record.target] = info;
            _paneTui[this.record.target] = _isTuiInfo(info,this.record.target);
            this.element.querySelector('.split-dim').textContent = `${info.width || '?'}×${info.height || '?'}`;
        }
        this.element.dataset.connection = 'Connected';
        this.paint();
    }
    paint() {
        if (!this.record) return;
        const tui = !!_paneTui[this.record.target];
        this.element.querySelector('.split-page').hidden = !tui;
        this.element.querySelector('.split-more').hidden = tui || this.content.split('\n').length < this.lines - 5;
        if (this.paused) { this.latest.hidden = false; return; }
        this.latest.hidden = true;
        this.pre.innerHTML = ansiToHtml(this.content); this.displayContent = this.content;
        this.pre.style.fontSize = _termFontSize + 'px';
        this.programmatic = true;
        this.element.scrollTop = this.element.scrollHeight;
        requestAnimationFrame(() => { this.programmatic = false; });
    }
    snapshot() {
        if (!this.record) return;
        Object.assign(this.record,{content:this.content,displayContent:this.displayContent,paused:this.paused,
            lines:this.lines,top:this.element.scrollTop,left:this.element.scrollLeft});
    }
    close() {
        this.generation++;
        clearTimeout(this.retry); clearTimeout(this.inactive);
        clearInterval(this.fallback); clearInterval(this.ping);
        this.fallback = null;
        if (this.ws) { const ws = this.ws; this.ws = null; ws.close(); }
    }
}

const SplitView = {
    enabled:false, active:0, slots:[], switching:false, chosenAt:0, restored:false,
    countdowns:new Map(), trays:new Map(),
    init() {
        const display = document.getElementById('term-display');
        this.grid = document.createElement('div');
        this.grid.className = 'split-views';
        display.before(this.grid);
        this.main = document.createElement('div'); this.main.className = 'split-main';
        this.grid.appendChild(this.main); this.main.appendChild(display);
        this.main.appendChild(document.getElementById('opencode-output'));
        const other = document.createElement('div'); other.className = 'term-display split-companion'; other.hidden = true;
        other.innerHTML = '<div class="term-info"><button class="split-identity" type="button"></button><span class="split-dim term-info-dim"></span><button class="split-change" type="button">Change</button><button class="split-fit" type="button">Fit</button><button class="split-page" type="button" hidden>↑</button></div><button class="split-more" type="button" hidden>Load more ↑</button><pre class="term-content"></pre><button class="split-latest" type="button" hidden>New output ↓</button>';
        this.grid.appendChild(other); this.other = new PaneView(other);
        this.selector = document.createElement('div'); this.selector.className = 'split-selector'; this.selector.hidden = true;
        this.grid.before(this.selector);
        // Each tab sits on its own pane's box. The label selects the side (or, on the
        // side already selected, opens the menu); ▾ always opens the pane menu.
        const tab = slot => `<div class="split-tab" data-slot="${slot}"><button type="button" class="split-tab-label"></button><button type="button" class="split-tab-menu" aria-haspopup="menu" aria-expanded="false" aria-label="Change pane ${slot ? 'B':'A'}">▾</button></div>`;
        this.selector.innerHTML = tab(0) + '<div class="split-ctl"><button type="button" data-swap aria-label="Swap pane positions" title="Swap sides">⇄</button><button type="button" data-single>Single</button></div>' + tab(1);
        this.selector.querySelectorAll('.split-tab').forEach(t => {
            const slot = Number(t.dataset.slot);
            t.querySelector('.split-tab-label').onclick = () => {
                if (slot === this.active) this.toggleMenu(slot);
                else { this.closeMenu(); this.activate(slot); }
            };
            t.querySelector('.split-tab-menu').onclick = () => this.toggleMenu(slot);
        });
        this.selector.querySelector('[data-swap]').onclick = () => { this.closeMenu(); this.swap(); };
        this.selector.querySelector('[data-single]').onclick = () => { this.closeMenu(); this.exit(); };
        this.menu = document.createElement('div'); this.menu.className = 'split-menu'; this.menu.setAttribute('role','menu'); this.menu.hidden = true;
        document.body.appendChild(this.menu);
        this.menu.addEventListener('keydown', e => {
            if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); this.closeMenu(true); return; }
            if (!['ArrowDown','ArrowUp'].includes(e.key)) return;
            e.preventDefault();
            const items = [...this.menu.querySelectorAll('button:not(:disabled)')], i = items.indexOf(document.activeElement);
            if (items.length) items[(i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length].focus();
        });
        document.addEventListener('pointerdown', e => {
            if (!this.menu.hidden && !e.target.closest('.split-menu, .split-tab')) this.closeMenu();
        });
        this.identity = document.createElement('button'); this.identity.type = 'button'; this.identity.className = 'split-identity'; this.identity.hidden = true;
        document.getElementById('term-info').prepend(this.identity);
        this.change = document.createElement('button'); this.change.textContent = 'Change'; this.change.type = 'button'; this.change.hidden = true;
        document.getElementById('term-info').appendChild(this.change); this.change.onclick = () => this.openPicker(this.active);
        this.identity.onclick = () => this.activate(this.active);
        other.querySelector('.split-identity').onclick = () => this.activate(1-this.active);
        other.querySelector('.split-change').onclick = () => this.openPicker(1-this.active);
        other.querySelector('.split-fit').onclick = () => this.fit(1-this.active);
        this.destination = document.createElement('div'); this.destination.className = 'split-destination'; this.destination.hidden = true;
        this.destination.setAttribute('aria-live','polite'); document.querySelector('.input-wrap').before(this.destination);
        this.stopStrip = document.createElement('div'); this.stopStrip.className = 'split-stops'; this.stopStrip.hidden = true;
        document.getElementById('autoyes-countdown').before(this.stopStrip);
        display.addEventListener('click', e => {
            if (!this.enabled || e.target.closest('button,a') || window.getSelection().toString()) return;
            this.activate(this.active);
        });
        document.addEventListener('keydown', e => {
            if (this.enabled && !e.isComposing && e.altKey && e.shiftKey && ['ArrowLeft','ArrowRight'].includes(e.key) && !this.dialog.open) {
                e.preventDefault(); this.activate(e.key === 'ArrowLeft' ? 0 : 1);
            }
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) this.other.close();
            else if (this.enabled) { this.other.watch(this.slots[1-this.active]); syncAutoYesState(); }
        });
        window.addEventListener('resize', () => { this.closeMenu(); if (this.enabled) this.update(); });
        this.dialog = document.createElement('dialog'); this.dialog.className = 'split-picker';
        this.dialog.innerHTML = '<form method="dialog"><strong>Choose pane</strong><button aria-label="Close pane picker">×</button></form><input placeholder="Find a session or pane…" aria-label="Filter panes"><div class="split-choices"></div><p>Existing panes only. Nothing is created or stopped.</p>';
        document.body.appendChild(this.dialog);
        this.dialog.querySelector('input').oninput = () => this.choices();
    },
    record(pane) {
        return {target:pane.target, pane_id:pane.pane_id, pane_pid:String(pane.pane_pid), created:pane.created,
            window:pane.window, session:pane.session, command:pane.agent_kind || pane.command_display || pane.command,
            dead:false, identity:null, content:'', paused:false, lines:2000};
    },
    same(a,b) { return !!a && !!b && a.pane_id === b.pane_id && a.pane_pid === String(b.pane_pid) && a.created === b.created; },
    async identify(record, expected = null) {
        try {
            const resp = await fetch('/terminal/identity?target='+encodeURIComponent(record.pane_id));
            if (authLost(resp)) return;
            const data = await resp.json();
            if (data.ok && String(data.identity.pane_pid) === record.pane_pid
                && (!expected || JSON.stringify(data.identity) === JSON.stringify(expected))) record.identity = data.identity;
            else { record.dead = true; record.identity = null; }
        } catch (e) { /* no identity means no input */ }
    },
    openPicker(slot=1) {
        if (!_termTarget) return;
        if (!this.enabled) {
            const pane = _sessionPanes.find(p => p.target === _termTarget);
            if (!pane || !pane.pane_id) return;
            this.slots = [this.record(pane)]; this.active = 0;
        }
        this.pickerSlot = slot;
        this.dialog.querySelector('strong').textContent = `Choose pane ${slot === 0 ? 'A' : 'B'}`;
        this.dialog.querySelector('input').value = ''; this.choices(); this.dialog.showModal();
        this.dialog.querySelector('input').focus();
    },
    choices() {
        const list = this.dialog.querySelector('.split-choices'); list.replaceChildren();
        this.fillChoices(list,this.dialog.querySelector('input').value.toLowerCase(),this.pickerSlot,false);
    },
    fillChoices(list, query, slot, menu) {
        for (const pane of _sessionPanes) {
            if (!(pane.target+' '+pane.command).toLowerCase().includes(query)) continue;
            const b = document.createElement('button'); b.type = 'button';
            const occupied = this.slots.findIndex(r => r.pane_id === pane.pane_id && !r.dead);
            const name = document.createElement('span'); name.className = 'split-choice-target'; name.textContent = pane.target;
            const kind = document.createElement('span'); kind.className = 'split-choice-kind';
            kind.textContent = ` · ${pane.agent_kind || pane.command}${occupied < 0 ? '' : occupied === slot ? ' · Showing here' : ' · Already in '+(occupied ? 'B':'A')}`;
            b.append(name,kind);
            b.disabled = occupied >= 0;
            if (menu) { b.setAttribute('role','menuitem'); b.onclick = () => { this.closeMenu(); this.choose(pane,slot); }; }
            else b.onclick = () => this.choose(pane,slot);
            list.appendChild(b);
        }
    },
    toggleMenu(slot) {
        if (!this.menu.hidden && this.menuSlot === slot) { this.closeMenu(); return; }
        this.closeMenu();
        this.menuSlot = slot;
        const tab = this.selector.querySelector(`.split-tab[data-slot="${slot}"]`);
        const head = document.createElement('div'); head.className = 'split-menu-head'; head.textContent = `Show in ${slot ? 'B':'A'}`;
        this.menu.replaceChildren(head);
        this.fillChoices(this.menu,'',slot,true);
        if (!_sessionPanes.length) { const empty = document.createElement('p'); empty.textContent = 'No panes'; this.menu.appendChild(empty); }
        this.menu.hidden = false;
        const r = tab.getBoundingClientRect(), width = Math.min(innerWidth - 16, Math.max(r.width, 340));
        this.menu.style.width = width + 'px';
        this.menu.style.left = Math.max(8, Math.min(slot ? r.right - width : r.left, innerWidth - width - 8)) + 'px';
        this.menu.style.top = (r.bottom + 4) + 'px';
        this.menu.style.maxHeight = Math.max(160, innerHeight - r.bottom - 16) + 'px';
        tab.querySelector('.split-tab-menu').setAttribute('aria-expanded','true');
        const first = this.menu.querySelector('button:not(:disabled)');
        if (first) first.focus({preventScroll:true});
    },
    closeMenu(refocus=false) {
        if (!this.menu || this.menu.hidden) return;
        this.menu.hidden = true;
        const button = this.selector.querySelector(`.split-tab[data-slot="${this.menuSlot}"] .split-tab-menu`);
        button.setAttribute('aria-expanded','false');
        if (refocus) button.focus();
    },
    async choose(pane,slot) {
        if (this.slots.some(r => !r.dead && r.pane_id === pane.pane_id)) return;
        const record = this.record(pane); await this.identify(record);
        if (!record.identity) { showFlash('error','Pane unavailable; choose again'); return; }
        const replacingActive = this.enabled && this.active === slot;
        if (!this.enabled) await this.identify(this.slots[0]);
        this.dialog.close();
        if (replacingActive) this.snapshot();
        this.slots[slot] = record; this.enabled = true; this.chosenAt = Date.now();
        _inputToSplit = false;
        // Suspend a command overlay without invoking its destructive close path.
        for (const session of Object.keys(_splitPanes)) disconnectSplitWs(session);
        document.getElementById('cmd-output').classList.remove('visible');
        if (!replacingActive && !this.slots[this.active].content) this.snapshot();
        this.other.watch(this.slots[1-this.active]);
        if (replacingActive) this.select(record); else this.update();
    },
    snapshot() {
        const r = this.slots[this.active]; if (!r) return;
        r.content = _termLatestContent; r.displayContent = _termLastContent; r.paused = _termPaused; r.lines = _termLines;
        r.top = document.getElementById('term-display').scrollTop;
        r.left = document.getElementById('term-display').scrollLeft;
        this.trays.set(r.pane_id,_attachments);
    },
    activate(slot) {
        if (!this.enabled || slot === this.active) return;
        this.snapshot(); this.other.snapshot(); this.active = slot; this.switching = true;
        this.select(this.slots[slot]); this.switching = false;
        this.other.watch(this.slots[1-slot]); this.update();
    },
    select(record) {
        this.switching = true; selectTab(record.target); this.switching = false;
        _termLatestContent = record.content || ''; _termLastContent = '';
        _termPaused = false; _termLines = record.lines || 2000;
        _doRender((record.paused ? record.displayContent : record.content) || '',_paneInfo[record.target],record.target);
        _termLatestContent = record.content || '';
        _termPaused = !!record.paused;
        if (record.paused) document.getElementById('term-display').scrollTop = record.top || 0;
        document.getElementById('term-display').scrollLeft = record.left || 0;
        if (this.trays.has(record.pane_id)) {
            const pending = this.trays.get(record.pane_id).filter(a => a.uploading);
            _attachments.push(...pending.filter(a => !_attachments.some(b => b.id === a.id)));
            renderAttachments();
        }
        this.update();
    },
    intercept(target) {
        if (!this.enabled || this.switching) return false;
        const slot = this.slots.findIndex(r => r.target === target && !r.dead);
        if (slot >= 0) this.activate(slot);
        else {
            const pane = _sessionPanes.find(p => p.target === target);
            if (pane) this.choose(pane,this.active);
        }
        return true;
    },
    swap() { if (!this.enabled) return; this.slots.reverse(); this.active = 1-this.active; this.update(); },
    exit() {
        if (this.slots[this.active].dead) { showFlash('error','Select a surviving pane before leaving split view'); return; }
        this.snapshot(); this.enabled = false; this.other.close(); this.closeMenu();
        this.update(); this.persist();
        onTabSwitchCommands((_termTarget || '').split(':')[0]);
        syncAutoYesState().then(() => _renderAutoYesCountdown());
    },
    inputTarget() {
        const r = this.slots[this.active];
        return r && !r.dead && r.identity ? r.target : '%split-view-unavailable';
    },
    readable(target) { return this.enabled && this.slots.some((r,i) => !r.dead && r.target === target && (innerWidth >= 800 || i === this.active)); },
    update() {
        document.body.classList.toggle('split-enabled',this.enabled);
        this.selector.hidden = this.destination.hidden = this.identity.hidden = this.change.hidden = !this.enabled;
        this.other.element.hidden = !this.enabled;
        this.main.style.order = this.active; this.other.element.style.order = 1-this.active;
        if (!this.enabled) { this.main.style.order = ''; this.stopStrip.hidden = true; document.querySelector('.btn-paste').textContent = 'Send'; this.setInputDisabled(false); return; }
        const current = this.slots[this.active], other = this.slots[1-this.active];
        this.identity.textContent = `${this.active ? 'B':'A'} · INPUT HERE`;
        markActiveTab(current.target);
        this.other.element.querySelector('.split-identity').textContent = `${this.active ? 'A':'B'} · ${other.target}${other.dead ? ' · Pane ended':' · View only'}`;
        this.selector.querySelectorAll('.split-tab').forEach((t,i) => {
            const r = this.slots[i], b = t.querySelector('.split-tab-label');
            b.textContent = `${i ? 'B':'A'} · ${r.target}${r.dead ? ' · Ended' : _sessionPrompts[r.target] ? ' · ?' : ''}`;
            t.classList.toggle('selected',i === this.active); b.setAttribute('aria-pressed',String(i === this.active)); b.title = r.target;
        });
        this.grid.classList.toggle('split-b-active',this.active === 1);
        this.setInputDisabled(current.dead || !current.identity);
        this.destination.textContent = `${current.dead ? 'Pane ended — choose a replacement' : 'Typing into '+(this.active ? 'B':'A')+' · '+current.target}`;
        document.querySelector('.btn-paste').textContent = `Send → ${this.active ? 'B':'A'}`;
        document.querySelectorAll('.session-tab').forEach(tab => {
            const slot = this.slots.findIndex(r => r.target === tab.dataset.target);
            tab.classList.toggle('split-visible',slot >= 0 && slot !== this.active);
            if (slot >= 0) tab.dataset.splitSlot = slot ? 'B':'A'; else delete tab.dataset.splitSlot;
        });
        this.renderCountdowns(); this.persist();
    },
    setInputDisabled(disabled) {
        document.querySelectorAll('.btn-paste, .btn-keys, .btn-vault, .hk-btn, .sa-btn').forEach(b => { b.disabled = disabled; });
    },
    promptFingerprint(content) {
        const lines = detectionTail(content).split('\n');
        const footer = lines.findLastIndex(line => /Esc to cancel|Press enter to confirm|Enter to select|Yes \(y\).*No \(n\)/i.test(line));
        return (footer >= 0 ? lines.slice(0,footer+1) : lines).join('\n').trim();
    },
    renderCountdowns() {
        if (!this.stopStrip) return;
        this.stopStrip.hidden = !this.enabled;
        this.stopStrip.replaceChildren();
        if (!this.enabled) return;
        for (let i=0;i<2;i++) {
            const r = this.slots[i], cd = this.countdowns.get(r.target);
            const row = document.createElement('div'); row.className = 'split-stop-row';
            const label = document.createElement('span');
            label.textContent = `${i ? 'B':'A'} · ${r.target} · ${cd ? 'Auto-Yes '+Math.max(0,Math.ceil((cd.deadline-Date.now())/1000))+'s' : isAutoYes(r.session) ? 'Auto-Yes armed':'Auto-Yes off'}`;
            row.appendChild(label);
            if (cd) { const b=document.createElement('button'); b.textContent='STOP'; b.type='button'; b.setAttribute('aria-label','Stop Auto-Yes for '+r.target); b.onclick=()=>this.cancel(r.target,cd); row.appendChild(b); }
            this.stopStrip.appendChild(row);
        }
    },
    async cancel(target,cd) {
        try {
            const resp = await fetch('/autoyes/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target})});
            const data = await resp.json();
            if (!resp.ok || !data.ok) { showFlash('error','Stop failed — answer by hand'); return; }
            if (this.countdowns.get(target) === cd) this.countdowns.delete(target);
            this.renderCountdowns(); showFlash(data.cancelled ? 'sent':'error',data.cancelled ? 'Cancelled':'Too late — already answered');
        } catch (e) { showFlash('error','Stop failed — answer by hand'); }
    },
    event(msg) {
        if (msg.event === 'countdown') this.countdowns.set(msg.target,{...msg,deadline:Date.now()+msg.remaining*1000});
        else this.countdowns.delete(msg.target);
        this.renderCountdowns();
    },
    syncCountdowns(data) {
        this.countdowns.clear();
        for (const [target,cd] of Object.entries(data.countdowns || {})) this.countdowns.set(target,{...cd,deadline:Date.now()+cd.remaining*1000});
        this.renderCountdowns();
    },
    reconcile(panes,requestedAt) {
        if (!this.restored) {
            this.restored = true;
            try {
                const saved = JSON.parse(localStorage.getItem('assist.splitView.v1'));
                if (saved && saved.enabled && saved.slots.length === 2 && saved.slots[0].pane_id !== saved.slots[1].pane_id) {
                    this.slots = saved.slots.map(r => ({...r,dead:!r.identity || !panes.some(p=>this.same(r,p)),expected:r.identity,identity:null,content:''}));
                    if (this.slots.every(r=>r.dead)) { localStorage.removeItem('assist.splitView.v1'); return; }
                    this.active = saved.active === 1 ? 1:0; this.enabled = true;
                    for (let i=0;i<2;i++) {
                        const r=this.slots[i];
                        if (!r.dead) this.identify(r,saved.slots[i].identity).then(() => {
                            if (!this.enabled) return;
                            if (i !== this.active && !r.dead) this.other.watch(r);
                            this.update();
                        });
                    }
                    this.select(this.slots[this.active]); this.other.watch(this.slots[1-this.active]);
                }
            } catch (e) { /* malformed local preference never changes routing */ }
        }
        if (!this.enabled || requestedAt <= this.chosenAt) return;
        for (let i=0;i<2;i++) {
            const r = this.slots[i], pane = panes.find(p=>p.target===r.target && this.same(r,p)) || panes.find(p=>this.same(r,p));
            if (!pane) {
                if (!r.dead) { r.dead=true; r.identity=null; if (i!==this.active) this.other.close(); }
            } else if (!r.dead && pane.target !== r.target) {
                const old = r.target; r.target=pane.target; r.session=pane.session; r.window=pane.window;
                if (_draftCache[old]) { _draftCache[r.target]=_draftCache[old]; delete _draftCache[old]; }
                if (i===this.active) { _termTarget=r.target; this.select(r); }
            }
        }
        this.update();
    },
    persist() {
        try { localStorage.setItem('assist.splitView.v1',JSON.stringify({enabled:this.enabled,active:this.active,
            slots:this.slots.map(({target,pane_id,pane_pid,created,session,window,identity,expected})=>({target,pane_id,pane_pid,created,session,window,identity:identity || expected || null}))})); } catch (e) {}
    },
    async fit(slot, requestedCols=null, requestedRows=null, opts={}) {
        const r=this.slots[slot]; if (!r || r.dead) return;
        const el=slot===this.active ? document.getElementById('term-display'):this.other.element;
        if (!el.clientWidth) { showFlash('error','Show this pane before fitting'); return; }
        if (this.slots.some((p,i)=>i!==slot && p.identity && r.identity && p.identity.window_id===r.identity.window_id)) {
            if (!confirm('Fit this tmux window? It affects both displayed panes.')) return;
        }
        const pre=el.querySelector('pre'), probe=document.createElement('span'); probe.textContent='MMMMMMMMMM'; probe.style.font='inherit'; pre.appendChild(probe);
        const charWidth=probe.getBoundingClientRect().width/10 || _termFontSize*.6; probe.remove();
        const cols=requestedCols == null ? Math.max(40,Math.floor((el.clientWidth-16)/charWidth)) : Math.max(40,Math.min(400,parseInt(requestedCols)));
        const rows=requestedRows == null ? Math.max(_paneTui[r.target] ? 10:60,Math.floor((el.clientHeight-44)/(_termFontSize*1.35))) : Math.max(10,Math.min(200,parseInt(requestedRows)));
        const resp=await fetch('/terminal/resize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session:r.session,target:r.pane_id,cols,rows,expected_target_identity:r.identity})});
        const data=await resp.json(); if (!data.ok || !opts.quiet) showFlash(data.ok?'ok':'error',data.ok?'Window fitted':data.error || 'Fit failed');
    }
};
SplitView.init();
setInterval(() => { if (SplitView.enabled) SplitView.renderCountdowns(); },500);
