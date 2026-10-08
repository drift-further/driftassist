// studio.js — Studio attention inbox panel, connect sheet, ◇ badge, effort chip.
// Every Studio call is server-proxied through /studio/* so the API token
// never reaches the browser.

var _studioPanelOpen = false;
var _studioSheetOpen = false;
var _studioInbox = [];
var _studioState = 'unconfigured';
var _studioWebBase = '';

var _STUDIO_STATE_LABEL = {
    connected: '● connected',
    unreachable: '○ unreachable',
    unauthorized: '⚠ unauthorized',
    unconfigured: '○ not connected',
};

// Built with createElement + textContent throughout: every field below is
// Studio-authored text, and this panel must not be an HTML injection path.
function studioRecommendation(q) {
    var answer = q.answer_json, labels = q.options_json;
    if (q.status !== 'recommended' || !answer || typeof answer !== 'object' || Array.isArray(answer)) return null;
    var selections = answer.selections === undefined ? [] : answer.selections;
    if (!Array.isArray(selections) || selections.some(function (s) { return typeof s !== 'string' || !s.trim(); })) return null;
    if (selections.length && (!Array.isArray(labels) || labels.some(function (s) { return typeof s !== 'string'; }) || selections.some(function (s) { return !labels.includes(s); }))) return null;
    if (answer.text !== undefined && (typeof answer.text !== 'string' || answer.text.length > 10000)) return null;
    if (answer.rationale !== undefined && typeof answer.rationale !== 'string') return null;
    var text = typeof answer.text === 'string' ? answer.text.trim() : '';
    if (!selections.length && !text) return null;
    return {selections: selections.slice(), text: text, rationale: typeof answer.rationale === 'string' ? answer.rationale : ''};
}
function studioRecommendationText(rec) { return rec.selections.concat(rec.text ? [rec.text] : []).join('\n'); }
function _studioItemEl(q) {
    var rec = studioRecommendation(q), item = studioNode('article', 'studio-item' + (rec ? ' rec' : ''));
    item.dataset.qid = q.id;
    var actionLabel = q.kind === 'blocking' ? 'Your answer is needed · blocks the work' : q.kind === 'parked' ? 'Optional question' : 'Your input is requested';
    item.appendChild(studioNode('div', 'studio-action-label attention', actionLabel));
    item.appendChild(studioNode('h4', 'studio-prompt', q.prompt || 'Question'));
    item.appendChild(studioDate(q.created_at, 'Asked'));
    if (q.evidence) item.appendChild(studioNode('p', 'studio-evidence', q.evidence));
    if (rec) {
        var proposal = studioNode('div', 'studio-recommendation');
        proposal.appendChild(studioNode('div', 'studio-rec-label', 'Recommended answer'));
        proposal.appendChild(studioNode('strong', 'studio-rec-answer', studioRecommendationText(rec)));
        var reason = studioNode('p', 'studio-rec-reason'); reason.appendChild(studioNode('strong', '', 'Why: ')); reason.appendChild(document.createTextNode(rec.rationale || 'No reason was provided.'));
        proposal.appendChild(reason); item.appendChild(proposal);
        item.appendChild(studioButton('Use this recommendation', function () { studioAnswer(q.id, 'accept', null, null); }, 'studio-primary studio-rec-use'));
    } else item.appendChild(studioNode('p', 'studio-context-muted', q.status === 'recommended' ? 'This proposal has no usable answer here. Review it in Studio or choose your answer below.' : 'No recommendation has been provided. Choose the answer you prefer.'));
    var options = studioNode('div', 'studio-opts');
    if (rec) options.hidden = true;
    var labels = Array.isArray(q.options_json) && q.options_json.every(function (s) { return typeof s === 'string'; }) ? q.options_json : [];
    labels.forEach(function (label) {
        var b = studioButton(label, function () { studioAnswer(q.id, 'answer', label, null); }, 'studio-secondary studio-answer-option');
        if (rec && rec.selections.includes(label)) b.appendChild(studioNode('span', 'studio-option-recommended', 'Recommended'));
        options.appendChild(b);
    });
    var free = studioNode('textarea', 'studio-context-note studio-freetext'); free.placeholder = 'Write your answer…'; free.setAttribute('aria-label', 'Your answer'); free.maxLength = 10000;
    free.addEventListener('input', function () { _studioEditing = true; }); options.appendChild(free);
    options.appendChild(studioButton('Review my answer', function () { if (free.value.trim()) studioAnswer(q.id, 'answer', null, free.value.trim()); else free.focus(); }, 'studio-secondary'));
    item.appendChild(options);
    if (rec) item.insertBefore(studioButton('Choose another answer', function () { options.hidden = !options.hidden; _studioEditing = true; if (!options.hidden) options.querySelector('button,textarea').focus({preventScroll: true}); }, 'studio-secondary'), options);
    if (q.project_id && q.effort_id) studioLink(item, 'Open question in Studio ↗', q.studio_url || (_studioWebBase || '') + '/#/p/' + q.project_id + '/e/' + q.effort_id + '/questions/' + q.id);
    return item;
}

// ================================================================
// Connect sheet — the free -> connected doorway
// ================================================================
function openStudioConnect() {
    _studioSheetOpen = true;
    var sheet = document.getElementById('studio-sheet');
    sheet.classList.add('visible');
    // The markup ships aria-hidden="true"; leaving it set would keep the open
    // sheet invisible to assistive tech.
    sheet.setAttribute('aria-hidden', 'false');
    var s = (window.SETTINGS && SETTINGS.studio) || {};
    document.getElementById('studio-web-base').value = s.web_base || '';
    document.getElementById('studio-api-base').value = s.api_base || '';
    document.getElementById('studio-api-token').value = '';   // never seed the mask
    document.getElementById('studio-connect-result').textContent = '';
}

function closeStudioConnect() {
    _studioSheetOpen = false;
    var sheet = document.getElementById('studio-sheet');
    sheet.classList.remove('visible');
    sheet.setAttribute('aria-hidden', 'true');
}

async function studioConnectSave() {
    var out = document.getElementById('studio-connect-result');
    var patch = { studio: {
        web_base: document.getElementById('studio-web-base').value.trim(),
        api_base: document.getElementById('studio-api-base').value.trim(),
    } };
    var tok = document.getElementById('studio-api-token').value.trim();
    if (tok) patch.studio.api_token = tok;
    out.textContent = 'Testing…';
    try {
        var r = await fetch('/api/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch),
        });
        var d = await r.json();
        if (!d.ok) { out.textContent = d.error || 'Save failed'; return; }
        SETTINGS = d.settings;
        var t = await fetch('/studio/test', { method: 'POST' });
        var td = await t.json();
        _studioState = td.state;
        out.textContent = _STUDIO_STATE_LABEL[td.state] || td.state;
        if (td.state === 'connected') {
            showFlash('ok', 'Studio connected');
            closeStudioConnect();
            loadStudioInbox();
        }
    } catch (e) {
        out.textContent = 'Offline';
    }
}

async function studioTestConnection() {
    try {
        var r = await fetch('/studio/test', { method: 'POST' });
        var d = await r.json();
        _studioState = d.state;
        showFlash(d.state === 'connected' ? 'ok' : 'error',
            'Studio: ' + (_STUDIO_STATE_LABEL[d.state] || d.state));
        if (_studioPanelOpen) loadStudioInbox();
    } catch (e) {
        showFlash('error', 'Offline');
    }
}
