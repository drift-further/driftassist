// Per-pane Studio context. All text is rendered as text; credentials stay server-side.
var _studioContext = null;
var _studioContextSerial = 0;
var _studioContextRendered = '';
var _studioEditing = false;
var _studioPending = false;
var _studioUncertainStatus = null;
var _studioNoteDrafts = {};

function studioNode(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}
function studioButton(label, callback, cls) {
    var node = studioNode('button', cls || 'studio-opt', label);
    node.type = 'button'; node.addEventListener('click', callback); return node;
}
function studioFrozen(context) {
    var c = context || _studioContext || {};
    return {target: c.target, pane_generation: c.pane_generation,
        binding_generation: c.binding_generation, connection_generation: c.connection_generation};
}
function studioRequestId() {
    return window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
}
function studioStatusUncertain(task, context) {
    return !!(_studioUncertainStatus && task.id === _studioUncertainStatus.task_id &&
        JSON.stringify(studioFrozen(context)) === JSON.stringify(studioFrozen(_studioUncertainStatus)));
}
function studioLink(parent, label, href) {
    var link = studioNode('a', 'studio-open-link', label);
    // URLs are assembled by our server from the configured Studio origin.
    if (!/^https?:\/\//i.test(href || '')) return;
    link.href = href; link.target = '_blank'; link.rel = 'noopener'; parent.appendChild(link);
}
function studioSection(parent, title) {
    var section = studioNode('section', 'studio-context-section');
    section.appendChild(studioNode('h3', '', title)); parent.appendChild(section); return section;
}
function studioTarget() { return typeof _termTarget !== 'undefined' ? _termTarget || '' : ''; }

function studioOnTargetChange(target) {
    if (_studioContext && _studioContext.target === target) return;
    _studioEditing = false; _studioContextRendered = ''; _studioContext = null;
    ++_studioContextSerial;
    studioRenderTrigger(null);
    if (_studioPanelOpen) document.getElementById('studio-body').replaceChildren(studioNode('p', 'studio-context-muted', 'Loading this tab’s Studio context…'));
    loadStudioInbox();
}

function openStudio() {
    if (_studioState === 'unconfigured') { openStudioConnect(); return; }
    if (!_studioPanelOpen) toggleStudioPanel();
}
function studioSyncPanelMode() {
    var panel = document.getElementById('studio-panel');
    var modal = _studioPanelOpen && window.matchMedia('(max-width: 900px)').matches;
    panel.setAttribute('role', modal ? 'dialog' : 'complementary');
    if (modal) panel.setAttribute('aria-modal', 'true');
    else panel.removeAttribute('aria-modal');
    document.body.classList.toggle('studio-mobile-open', modal);
    document.querySelector('.top-chrome').inert = modal;
}
window.matchMedia('(max-width: 900px)').addEventListener('change', studioSyncPanelMode);
function toggleStudioPanel() {
    _studioPanelOpen = !_studioPanelOpen;
    var panel = document.getElementById('studio-panel');
    panel.classList.toggle('visible', _studioPanelOpen);
    panel.setAttribute('aria-hidden', String(!_studioPanelOpen));
    document.getElementById('term-panel').classList.toggle('studio-context-open', _studioPanelOpen);
    document.getElementById('studio-backdrop').classList.toggle('visible', _studioPanelOpen);
    var trigger = document.getElementById('btn-studio');
    if (trigger) trigger.setAttribute('aria-expanded', String(_studioPanelOpen));
    studioSyncPanelMode();
    if (!_studioPanelOpen) {
        _studioEditing = false; _studioContextRendered = '';
        if (trigger) trigger.focus({preventScroll: true});
    } else {
        loadStudioInbox();
        panel.querySelector('.studio-panel-close').focus({preventScroll: true});
    }
}
document.addEventListener('keydown', function (event) {
    if (!_studioPanelOpen || _studioSheetOpen) return;
    if (event.key === 'Escape') { toggleStudioPanel(); event.preventDefault(); return; }
    if (event.key !== 'Tab' || !window.matchMedia('(max-width: 900px)').matches) return;
    var panel = document.getElementById('studio-panel');
    var controls = Array.from(panel.querySelectorAll('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]'))
        .filter(function (node) { return node.getClientRects().length; });
    if (!controls.length) return;
    var first = controls[0], last = controls[controls.length - 1];
    if (!panel.contains(document.activeElement) || event.shiftKey && document.activeElement === first) {
        (event.shiftKey ? last : first).focus(); event.preventDefault();
    } else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
});

async function loadStudioInbox() {
    var target = studioTarget(), serial = ++_studioContextSerial;
    try {
        var response = await fetch('/studio/context?target=' + encodeURIComponent(target));
        var context = await response.json();
        if (!response.ok || serial !== _studioContextSerial || target !== studioTarget()) return;
        var changed = !_studioContext || JSON.stringify(studioFrozen(context)) !== JSON.stringify(studioFrozen(_studioContext));
        if (changed) { _studioEditing = false; _studioContextRendered = ''; _studioUncertainStatus = null; }
        _studioContext = context; _studioInbox = context.questions || [];
        _studioState = context.state; _studioWebBase = context.web_base || '';
        if (_studioUncertainStatus && !_studioPending && studioWritable(context)) {
            var confirmedTask = [context.current_task].concat(context.tasks || []).find(function (task) {
                return task && studioStatusUncertain(task, context) && task.effort_id === _studioUncertainStatus.effort_id && task.status === _studioUncertainStatus.status;
            });
            if (confirmedTask) {
                _studioUncertainStatus = null; _studioEditing = false; _studioContextRendered = '';
                showFlash('ok', confirmedTask.status === 'done' ? 'Studio confirms this task is complete' : 'Studio confirms the task status');
            }
        }
        studioRenderTrigger(context);
        if (_studioPanelOpen && !_studioEditing && !_studioPending) renderStudioInbox();
        if (_studioPanelOpen && _studioEditing && (context.stale || changed)) {
            document.querySelectorAll('#studio-body .studio-submit').forEach(function (b) { b.disabled = true; });
        }
    } catch (error) {
        if (_studioContext && _studioContext.target === target) {
            _studioContext.stale = true; studioRenderTrigger(_studioContext);
            document.querySelectorAll('#studio-body button').forEach(function (b) { b.disabled = true; });
        }
    }
}

function studioRenderTrigger(c) {
    var trigger = document.getElementById('btn-studio');
    if (!trigger) return;
    var state = c ? c.state : _studioState;
    var offline = !!(c && (c.stale || c.conflict) || state === 'unreachable' || state === 'unauthorized');
    var count = c && !offline ? c.attention_count || 0 : 0;
    var target = c && c.target || studioTarget();
    trigger.classList.toggle('offline', offline);
    trigger.classList.toggle('unlinked', !!c && !c.effort);
    trigger.classList.toggle('unconfigured', state === 'unconfigured');
    var label = 'Studio' + (target ? ' for ' + target : '') + (offline ? ', connection unavailable' : count ? ', ' + count + ' questions pending' : state === 'unconfigured' ? ', connect your account' : c && !c.effort ? ', link an effort' : ' context');
    trigger.setAttribute('aria-label', label);
    trigger.title = label;
    var session = document.getElementById('studio-panel-session');
    if (session) session.textContent = target ? '/ ' + target.split(':')[0] : '';
    var badge = document.getElementById('studio-badge');
    if (badge) {
        badge.textContent = count > 9 ? '9+' : String(count);
        badge.classList.toggle('hidden', !(count > 0));
    }
    var warning = document.getElementById('studio-lock');
    if (warning) warning.classList.toggle('hidden', !offline);
}

function _applyStudioData(block) {
    if (!block) return;
    _studioState = block.state || 'unconfigured';
    if (block.web_base) _studioWebBase = block.web_base;
    updateStudioChip(block);
}

function updateStudioChip(block) {
    var chip = document.getElementById('ci-effort');
    if (chip) chip.classList.add('hidden');
    // The poll's active target is shared across browsers. Detailed context must
    // always address THIS browser's pane. An unrelated or older poll cannot
    // invalidate an editor or overwrite a freshly confirmed binding.
    loadStudioInbox();
    if (block.tabs) document.querySelectorAll('.session-tab[data-target]').forEach(function (tab) {
        var summary = block.tabs[tab.dataset.target], existing = tab.querySelector('.studio-tab-context');
        if (!summary || !summary.effort_title) { if (existing) existing.remove(); return; }
        if (!existing) { existing = studioNode('span', 'studio-tab-context'); tab.appendChild(existing); }
        existing.textContent = summary.task_status || summary.phase || 'linked';
        existing.title = summary.effort_title + (summary.task_title ? ' · ' + summary.task_title : '');
    });
}

var _studioFoldStates = {};

function studioDate(value, prefix) {
    var date = new Date(typeof value === 'number' ? value * 1000 : value || NaN);
    var node = studioNode('time', 'studio-dated');
    if (!Number.isFinite(date.getTime())) { node.textContent = 'Date unavailable'; return node; }
    node.dateTime = date.toISOString();
    node.title = date.toLocaleString(undefined, {year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short'});
    node.setAttribute('aria-label', (prefix ? prefix + ' ' : '') + node.title);
    node.textContent = (prefix ? prefix + ' · ' : '') + date.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
    return node;
}
function studioFoldout(parent, title, key, opened, count) {
    var details = studioNode('details', 'studio-foldout'), summary = studioNode('summary', '', title);
    var c = _studioContext || {};
    key = c.connection_generation + ':' + c.pane_generation + ':' + (c.effort || {}).id + ':' + key;
    details.open = Object.prototype.hasOwnProperty.call(_studioFoldStates, key) ? _studioFoldStates[key] : !!opened;
    details.addEventListener('toggle', function () {
        // Ignore queued events from a replaced render.
        if (details.isConnected) _studioFoldStates[key] = details.open;
    });
    if (count !== undefined) summary.appendChild(studioNode('span', 'studio-fold-count', count));
    details.appendChild(summary); parent.appendChild(details); return details;
}
function studioWritable(c) { return !!c && c.state === 'connected' && !c.stale && !c.conflict; }
function studioTaskReady(task) {
    var gates = task.gates || [], findings = task.findings || [];
    return task.status === 'review' && gates.length > 0 && gates.every(function (g) { return ['pass', 'waived'].includes(g.verdict); }) &&
        !findings.some(function (f) { return f.status === 'open' && f.severity === 'blocking'; });
}
function studioReviewInstructions(task) {
    // Only an explicit instruction section is authoritative, never a raw spec snapshot.
    var supplied = [task.note, task.description].find(function (text) { return typeof text === 'string' && /^Review instructions:\s*\S/im.test(text); });
    var match = supplied && supplied.match(/^Review instructions:\s*([^\n]+(?:\n(?!\s*$|#)[^\n]+)*)/im);
    return match ? match[1].slice(0, 600) : 'Review the result against the linked design. If you’re happy, confirm complete; otherwise send feedback.';
}
function renderStudioInbox() {
    var c = _studioContext;
    if (!c || c.target !== studioTarget()) return;
    var signature = JSON.stringify(c);
    if (signature === _studioContextRendered) return;
    _studioContextRendered = signature;
    var body = document.getElementById('studio-body'), oldScroll = body.scrollTop;
    body.replaceChildren();
    var writable = studioWritable(c), stateEl = document.getElementById('studio-state');
    stateEl.textContent = writable ? '● connected' : 'saved context';
    stateEl.className = 'studio-state ' + (writable ? 'connected' : c.state);
    body.appendChild(studioNode('div', 'studio-context-breadcrumb', (c.project_name || 'Studio') + (c.effort ? '\n' + c.effort.title : '\nLink this tab to your work')));
    if (!writable) {
        var warn = studioNode('div', 'studio-context-warning', c.conflict || (c.state === 'unauthorized' ? 'Studio rejected this credential. Review the connection. Human QA actions require an authorized human credential.' : 'Studio is unavailable. This is saved context; completion and answers are paused.'));
        if (c.checked_at) warn.appendChild(studioDate(c.checked_at, 'Last synced'));
        warn.appendChild(studioButton('Retry connection', studioTestConnection)); body.appendChild(warn);
    }
    if ((c.questions || []).length) {
        var attention = studioSection(body, 'Needs your answer');
        c.questions.forEach(function (q) {
            var item = _studioItemEl(q);
            if (!writable) item.querySelectorAll('button, input, textarea').forEach(function (control) { control.disabled = true; });
            attention.appendChild(item);
        });
    }
    if (c.effort) studioRenderTasks(body, c);
    else {
        body.appendChild(studioNode('p', 'studio-context-muted', 'Choose an effort to see the work and its next steps.'));
        var choose = studioButton('Choose effort', studioChooseEffort); choose.disabled = c.state !== 'connected'; body.appendChild(choose);
    }
    studioRenderDocuments(body, c);
    var activity = studioFoldout(body, 'This tab’s activity', 'activity', false, (c.activity || []).length ? String(c.activity.length) : 'No recent commands');
    if (c.observation_gaps) activity.appendChild(studioNode('p', 'studio-context-warning', 'Some activity is unavailable. Current work status comes from Studio.'));
    (c.activity || []).slice(0, 12).forEach(function (event) {
        var details = studioNode('details', 'studio-activity');
        var label = event.kind === 'finish' ? event.exit_code === 0 ? 'Command finished' : 'Command failed' : event.confirmed ? event.kind === 'write' ? 'Studio updated' : 'Studio record read' : 'Studio refused or failed';
        if (event.effort_id && c.effort && event.effort_id !== c.effort.id) label += ' · another effort';
        details.appendChild(studioNode('summary', '', label));
        details.appendChild(studioDate(event.at));
        details.appendChild(studioNode('p', 'studio-context-muted', event.summary + (event.status ? ' · HTTP ' + event.status : ''))); activity.appendChild(details);
    });
    if ((c.related_tabs || []).length) {
        var related = studioFoldout(body, 'Other tabs in this effort', 'related', false, String(c.related_tabs.length));
        c.related_tabs.forEach(function (tab) { related.appendChild(studioButton(tab.target, function () { selectTab(tab.target); loadStudioInbox(); })); });
    }
    var connection = studioFoldout(body, 'Connection & linked effort', 'connection', false);
    var workspace = (c.account || {}).active_workspace;
    connection.appendChild(studioNode('p', 'studio-context-muted', workspace ? (workspace.name || workspace.slug || 'Workspace ' + workspace.id) + ' · ' + (c.account.principal_kind || 'connected') : 'Local Studio · ' + ((c.account || {}).principal_kind || 'connection')));
    if (c.effort) {
        connection.appendChild(studioNode('p', 'studio-context-muted', c.effort.title + ' · ' + (c.effort.phase || 'effort')));
        var change = studioButton('Change link', studioChooseEffort); change.disabled = !writable; connection.appendChild(change);
    }
    studioLink(connection, 'Open effort in Studio ↗', c.url);
    connection.appendChild(studioButton('Connection settings', openStudioConnect, 'studio-text-button'));
    if ((c.suggestions || []).length) {
        var suggestions = studioFoldout(connection, 'Suggested links', 'suggestions', false, String(c.suggestions.length));
        c.suggestions.forEach(function (s) {
            var effort = (c.efforts || []).find(function (e) { return e.id === s.effort_id; });
            var button = studioButton(effort ? effort.title : 'Review suggested effort', function () { studioChooseEffort(s.effort_id); }); button.disabled = !writable; suggestions.appendChild(button);
        });
    }
    body.scrollTop = oldScroll;
}
function studioRenderDocuments(body, c) {
    var rows = (c.documents || []).slice().sort(function (a, b) {
        var at = Date.parse(a.version_created_at), bt = Date.parse(b.version_created_at);
        return (Number.isFinite(bt) ? bt : -Infinity) - (Number.isFinite(at) ? at : -Infinity);
    });
    var handoffs = rows.filter(function (d) { return (d.kind || d.type) === 'handoff'; });
    function update(parent, doc) {
        var entry = studioNode('article', 'studio-update');
        entry.appendChild(studioNode('h4', '', doc.presentation_heading || (doc.title && !/^handoff note$/i.test(doc.title) ? doc.title : 'Progress update')));
        entry.appendChild(studioDate(doc.version_created_at, 'Updated'));
        if (doc.presentation_summary) entry.appendChild(studioNode('p', 'studio-context-muted', doc.presentation_summary));
        studioLink(entry, 'Read update ↗', doc.studio_url); parent.appendChild(entry);
    }
    if (handoffs.length) {
        var latest = studioFoldout(body, 'Latest update', 'latest-update', true);
        update(latest, handoffs[0]);
        if (handoffs.length > 1) {
            var history = studioFoldout(latest, 'Earlier updates', 'history', false, String(handoffs.length - 1));
            handoffs.slice(1).forEach(function (d) { update(history, d); });
        }
    }
    var docs = rows.filter(function (d) { return (d.kind || d.type) !== 'handoff'; });
    if (docs.length) {
        var references = studioFoldout(body, 'Design & references', 'references', false, String(docs.length));
        docs.forEach(function (doc) {
            var entry = studioNode('div', 'studio-document');
            studioLink(entry, doc.title || 'Reference document', doc.studio_url);
            entry.appendChild(studioDate(doc.version_created_at, 'Updated')); references.appendChild(entry);
        });
    }
}
function studioRenderTasks(body, c) {
    var section = studioSection(body, c.current_task ? 'Current task' : 'Choose a task');
    var tasks = c.current_task ? [c.current_task] : (c.tasks || []).filter(function (t) { return ['doing', 'review', 'blocked'].includes(t.status); });
    if (!tasks.length) tasks = (c.tasks || []).slice(0, 5);
    if (!tasks.length) section.appendChild(studioNode('p', 'studio-context-muted', 'No task is linked yet.'));
    tasks.forEach(function (task) {
        var ready = studioTaskReady(task), writable = studioWritable(c), card = studioNode('article', 'studio-task-card'); card.dataset.taskId = task.id;
        var labels = {todo: 'Not started · no action needed', doing: 'Agent is working · no action needed', blocked: 'Work is blocked · review details', done: 'Confirmed complete · no action needed'};
        var label = task.status === 'review' ? ready ? 'Ready for your review' : 'Agent needs to finish the checks' : labels[task.status] || 'Review task details';
        card.appendChild(studioNode('div', 'studio-action-label' + (task.status === 'done' ? ' success' : ''), label));
        card.appendChild(studioNode('h4', '', task.title));
        if (task.status === 'review') card.appendChild(studioNode('p', 'studio-next-step', ready ? 'Your next step: ' + studioReviewInstructions(task) : 'This task is not ready for completion. All required checks must pass and blocking findings must be resolved.'));
        else if (task.status === 'doing') card.appendChild(studioNode('p', 'studio-context-muted', 'Keep using the terminal. A review request will appear here when the result is ready.'));
        else if (task.status === 'done') card.appendChild(studioNode('p', 'studio-context-muted', 'There is nothing else to accept for this task.'));
        else if (task.status === 'blocked') card.appendChild(studioNode('p', 'studio-context-muted', 'Review the linked questions or task details to see what is blocking progress.'));
        card.appendChild(studioDate(task.status_changed_at || task.created_at, task.status === 'review' ? 'Ready for review' : 'Updated'));
        var gates = task.gates || [], open = (task.findings || []).filter(function (f) { return f.status === 'open'; });
        if (gates.length) {
            var gateRow = studioNode('div', 'studio-context-gates');
            gates.forEach(function (g) { gateRow.appendChild(studioNode('span', 'studio-gate gate-' + g.verdict, (['pass', 'waived'].includes(g.verdict) ? '✓ ' : '○ ') + (g.name || g.gate_name || 'Check') + (g.verdict === 'waived' ? ' · waived' : g.verdict === 'fail' ? ' · failed' : !g.verdict || g.verdict === 'pending' ? ' · pending' : ''))); }); card.appendChild(gateRow);
        }
        open.forEach(function (f) { card.appendChild(studioNode('p', 'studio-context-warning', (f.severity === 'blocking' ? 'Blocking: ' : 'Note: ') + (f.title || 'Review finding in Studio'))); });
        if (ready) {
            var complete = studioButton('Confirm task complete', function () { studioConfirmComplete(task, card); }, 'studio-primary');
            complete.disabled = !writable || !task.assist_revision || studioStatusUncertain(task, c); card.appendChild(complete);
            card.appendChild(studioNode('p', 'studio-action-boundary', 'Records completion in Studio. Does not commit code.'));
        }
        if (task.status !== 'done') {
            var feedback = studioButton('Send feedback', function () { studioFeedbackEditor(task, card); }, 'studio-secondary'); feedback.disabled = !writable; card.appendChild(feedback);
        }
        if (studioStatusUncertain(task, c)) {
            card.appendChild(studioNode('p', 'studio-action-error', 'Waiting for Studio to confirm the task status. Your update will not be submitted again.'));
            card.appendChild(studioButton('Check status', loadStudioInbox, 'studio-secondary'));
        }
        if (!c.current_task || c.current_task.id !== task.id) {
            var use = studioButton('Use this task', function () { studioBind(c.effort.id, task.id); }); use.disabled = !writable; card.appendChild(use);
        }
        var more = studioFoldout(card, 'Task details & evidence', 'task-' + task.id, false);
        gates.forEach(function (g) { more.appendChild(studioNode('p', 'studio-context-muted', (g.name || g.gate_name || 'Check') + ': ' + (g.verdict || 'pending') + (g.reason ? '\n' + g.reason : ''))); });
        var edit = studioButton('Edit task status or add note', function () { studioTaskEditor(task); }); edit.disabled = !writable || studioStatusUncertain(task, c); more.appendChild(edit);
        studioLink(more, 'View task in Studio ↗', task.studio_url);
        section.appendChild(card);
    });
}
function studioConfirmComplete(task, card) {
    if (_studioPending || !studioWritable(_studioContext) || !studioTaskReady(task) || studioStatusUncertain(task, _studioContext)) return;
    _studioEditing = true;
    card.querySelector('.studio-confirm')?.remove();
    var box = studioNode('div', 'studio-confirm'); box.setAttribute('role', 'group'); box.setAttribute('aria-label', 'Confirm task completion');
    box.appendChild(studioNode('strong', '', 'Confirm this task is complete?'));
    box.appendChild(studioNode('p', '', studioTarget().split(':')[0] + ' · ' + task.title));
    box.appendChild(studioNode('p', '', 'You have reviewed the result. Studio will record the task as done. No code will be committed.'));
    var payload = Object.assign({}, studioFrozen(), {task_id: task.id, expected_status: task.status, task_revision: task.assist_revision, status: 'done', request_id: studioRequestId()});
    var yes = studioButton('Yes, confirm complete', function () { studioAction('/studio/task-status', payload, box); }, 'studio-primary studio-submit'); box.appendChild(yes);
    box.appendChild(studioButton('Cancel', studioCancelEdit, 'studio-secondary')); card.appendChild(box); box.scrollIntoView({block: 'nearest'}); yes.focus({preventScroll: true});
}
function studioFeedbackEditor(task, card) {
    if (_studioPending || !studioWritable(_studioContext)) return;
    _studioEditing = true; card.querySelector('.studio-feedback')?.remove();
    var frozen = studioFrozen(), section = studioNode('div', 'studio-feedback');
    section.appendChild(studioNode('p', 'studio-context-muted', 'What needs changing? Saves a note on this task and leaves it open.'));
    var note = studioNode('textarea', 'studio-context-note'); note.setAttribute('aria-label', 'Feedback for this task'); note.placeholder = 'Describe what you’d like changed…'; note.maxLength = 10000;
    var key = frozen.connection_generation + ':' + frozen.pane_generation + ':' + task.id;
    note.value = _studioNoteDrafts[key] || ''; note.addEventListener('input', function () { _studioNoteDrafts[key] = note.value; }); section.appendChild(note);
    section.appendChild(studioButton('Review feedback', function () {
        if (!note.value.trim()) { note.focus(); return; }
        section.querySelector('.studio-confirm')?.remove();
        var box = studioNode('div', 'studio-confirm');
        box.appendChild(studioNode('strong', '', 'Save this feedback?'));
        box.appendChild(studioNode('p', '', frozen.target.split(':')[0] + ' · ' + task.title));
        box.appendChild(studioNode('p', '', note.value.trim()));
        box.appendChild(studioNode('p', 'studio-context-muted', 'Adds a Studio task note. The task stays open. This does not send a message to the agent.'));
        var payload = Object.assign({}, frozen, {task_id: task.id, expected_status: task.status, task_revision: task.assist_revision, note: note.value.trim(), request_id: studioRequestId()});
        var save = studioButton('Confirm feedback', function () { studioAction('/studio/task-note', payload, box, function () { delete _studioNoteDrafts[key]; }); }, 'studio-primary studio-submit'); box.appendChild(save);
        box.appendChild(studioButton('Cancel', function () { box.remove(); note.focus(); }, 'studio-secondary'));
        section.appendChild(box); box.scrollIntoView({block: 'nearest'}); save.focus({preventScroll: true});
    }, 'studio-secondary'));
    section.appendChild(studioButton('Close feedback', studioCancelEdit, 'studio-text-button')); card.appendChild(section); section.scrollIntoView({block: 'nearest'}); note.focus({preventScroll: true});
}

function studioChooseEffort(selected) {
    if (!_studioContext || _studioPending) return;
    _studioEditing = true;
    var body = document.getElementById('studio-body'), section = studioSection(body, 'Choose this tab’s effort');
    var select = studioNode('select', 'studio-context-select'); select.setAttribute('aria-label', 'Effort');
    select.appendChild(studioNode('option', '', 'Choose an effort…')); select.options[0].value = '';
    (_studioContext.efforts || []).forEach(function (effort) {
        var option = studioNode('option', '', effort.title + ' · ' + effort.phase); option.value = effort.id; select.appendChild(option);
    });
    if (selected) select.value = String(selected);
    section.appendChild(select);
    var frozen = studioFrozen();
    section.appendChild(studioButton('Confirm link', function () { if (select.value) studioBind(Number(select.value), null, frozen); }, 'studio-accept studio-submit'));
    if (_studioContext.effort && _studioContext.source !== 'launch') section.appendChild(studioButton('Unlink this tab', function () { studioBind(null, null, frozen); }));
    section.appendChild(studioButton('Cancel', studioCancelEdit)); section.scrollIntoView({block: 'nearest'});
}
function studioCancelEdit() { _studioEditing = false; _studioContextRendered = ''; renderStudioInbox(); }

async function studioBind(eid, tid, frozen) {
    if (_studioPending) return;
    if ((frozen || studioFrozen()).target !== studioTarget()) { studioOnTargetChange(studioTarget()); return; }
    _studioPending = true;
    try {
        var response = await fetch('/studio/context-binding', {method: eid ? 'PUT' : 'DELETE', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(Object.assign({}, frozen || studioFrozen(), {effort_id: eid, task_id: tid}))});
        var result = await response.json();
        if (!result.ok) { showFlash('error', result.error || 'Could not link effort'); return; }
        _studioEditing = false; _studioContextRendered = ''; showFlash('ok', eid ? 'Tab linked to Studio effort' : 'Tab unlinked');
    } catch (error) { showFlash('error', 'Could not reach Assist'); }
    finally { _studioPending = false; loadStudioInbox(); }
}

function studioTaskEditor(task) {
    if (_studioPending) return;
    _studioEditing = true;
    var frozen = studioFrozen(), body = document.getElementById('studio-body'), section = studioSection(body, task.title);
    var select = studioNode('select', 'studio-context-select'); select.setAttribute('aria-label', 'Task status');
    ['todo', 'doing', 'review', 'done', 'blocked'].forEach(function (status) {
        var option = studioNode('option', '', status); option.value = status; select.appendChild(option);
    }); select.value = task.status; section.appendChild(select);
    section.appendChild(studioNode('p', 'studio-context-muted', 'Studio checks gates, findings and account permissions before allowing completion.'));
    var statusRequest = Object.assign({}, frozen, {task_id: task.id, expected_status: task.status, task_revision: task.assist_revision, request_id: studioRequestId()});
    section.appendChild(studioButton('Confirm status update', function () {
        studioAction('/studio/task-status', Object.assign({}, statusRequest, {status: select.value, request_id: studioRequestId()}), section);
    }, 'studio-accept studio-submit'));
    var note = studioNode('textarea', 'studio-context-note'); note.setAttribute('aria-label', 'Progress note'); note.placeholder = 'Add a progress note…'; note.maxLength = 10000;
    var draftKey = frozen.connection_generation + ':' + frozen.pane_generation + ':' + task.id; note.value = _studioNoteDrafts[draftKey] || '';
    note.addEventListener('input', function () { _studioNoteDrafts[draftKey] = note.value; }); section.appendChild(note);
    var noteRequest = Object.assign({}, frozen, {task_id: task.id, expected_status: task.status, task_revision: task.assist_revision, request_id: studioRequestId()});
    section.appendChild(studioButton('Save progress note', function () {
        studioAction('/studio/task-note', Object.assign({}, noteRequest, {note: note.value, request_id: studioRequestId()}), section, function () { delete _studioNoteDrafts[draftKey]; });
    }, 'studio-opt studio-submit'));
    section.appendChild(studioButton('Cancel', studioCancelEdit)); section.scrollIntoView({block: 'nearest'});
}

function studioAnswer(qid, action, select, text) {
    if (_studioPending || !studioWritable(_studioContext)) return;
    var question = (_studioContext.questions || []).find(function (q) { return q.id === qid; });
    if (!question) return;
    var item = document.querySelector('.studio-item[data-qid="' + qid + '"]'); if (!item) return;
    _studioEditing = true;
    var existing = item.querySelector('.studio-confirm'); if (existing) existing.remove();
    var confirmation = studioNode('div', 'studio-confirm');
    var recommendation = action === 'accept' ? studioRecommendation(question) : null;
    if (action === 'accept' && !recommendation) return;
    confirmation.appendChild(studioNode('strong', '', 'Send this answer?'));
    confirmation.appendChild(studioNode('p', '', studioTarget().split(':')[0] + ' · ' + question.prompt));
    confirmation.appendChild(studioNode('p', 'studio-confirm-answer', recommendation ? studioRecommendationText(recommendation) : select || text));
    var consequence = recommendation ? recommendation.selections.find(function (s) { return /^(waive|wontfix)$/i.test(s); }) : select;
    if (/^(waive|wontfix)$/i.test(consequence || '')) confirmation.appendChild(studioNode('p', 'studio-context-warning', /^waive$/i.test(consequence) ? 'Studio will waive linked gates and close current blocking findings.' : 'Studio will close current blocking findings without waiving gates.'));
    var payload = Object.assign({}, studioFrozen(), {request_id: studioRequestId(), question_id: qid, question_revision: question.assist_revision, action: action});
    if (select) payload.select = select; if (text) payload.text = text;
    confirmation.appendChild(studioButton('Confirm answer', function () { studioAction('/studio/answer', payload, confirmation); }, 'studio-accept studio-submit'));
    confirmation.appendChild(studioButton('Cancel', function () { confirmation.remove(); _studioEditing = false; }));
    item.appendChild(confirmation); confirmation.scrollIntoView({block: 'nearest'}); confirmation.querySelector('button').focus({preventScroll: true});
}

async function studioAction(path, payload, section, onSuccess) {
    if (_studioPending) return;
    if (path === '/studio/task-status' && studioStatusUncertain({id: payload.task_id}, _studioContext)) {
        showFlash('error', 'Check the recorded task status before submitting another update.'); return;
    }
    if (payload.target !== studioTarget() || JSON.stringify(studioFrozen(payload)) !== JSON.stringify(studioFrozen())) {
        showFlash('error', 'Tab or Studio connection changed. Review the current context.');
        studioOnTargetChange(studioTarget()); loadStudioInbox(); return;
    }
    _studioPending = true;
    var buttons = section.querySelectorAll('button'); buttons.forEach(function (b) { b.disabled = true; });
    var errorEl = section.querySelector('.studio-action-error');
    if (!errorEl) { errorEl = studioNode('p', 'studio-action-error'); errorEl.setAttribute('role', 'status'); section.appendChild(errorEl); }
    errorEl.textContent = 'Saving…';
    var uncertain = false;
    try {
        var response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        var result = await response.json();
        if (!result.ok) { uncertain = !!result.uncertain; errorEl.textContent = result.error || 'Studio refused this update'; return; }
        if (onSuccess) onSuccess();
        _studioEditing = false; _studioContextRendered = ''; showFlash('ok', 'Saved in Studio');
    } catch (error) { uncertain = true; errorEl.textContent = 'Outcome unknown. Check the record in Studio before submitting again.'; }
    finally {
        _studioPending = false;
        if (uncertain && path === '/studio/task-status' && JSON.stringify(studioFrozen(payload)) === JSON.stringify(studioFrozen())) {
            _studioUncertainStatus = Object.assign({}, payload, {effort_id: _studioContext.effort.id});
            errorEl.textContent = 'Checking Studio for the recorded task status. Your update will not be submitted again.';
            buttons.forEach(function (b) { if (!b.classList.contains('studio-submit')) b.disabled = false; });
        }
        if (!uncertain) buttons.forEach(function (b) { b.disabled = false; });
        loadStudioInbox();
    }
}
