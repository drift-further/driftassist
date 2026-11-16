// outbox.js — the files tray: files an agent offered with `assist send-file`.
//
// /poll's `outbox` lists every pending offer. The top bar shows "⬇ N" while
// any wait; tapping it opens a sheet with Download and Dismiss for each. The
// popup (prompt-popup.js, kind `file`) is the arrival notice; this tray is
// what stays when the popup was closed, until the file is taken or expires.
//
// Download is a plain navigation, so the phone's own download handling and
// "open in…" take over. The server serves each offer once; the status check
// first turns a lost race into a readable message instead of a failed download.

let _outboxOffers = [];
let _outboxSheetOpen = false;

function outboxSize(size) {
    let value = Number(size) || 0;
    for (const unit of ['B', 'KB', 'MB', 'GB']) {
        if (value < 1024 || unit === 'GB') return unit === 'B' ? value + ' B' : value.toFixed(1) + ' ' + unit;
        value /= 1024;
    }
    return value + ' B';
}

function _outboxMinutesLeft(expiresAt) {
    return Math.max(0, Math.ceil((Number(expiresAt) * 1000 - Date.now()) / 60000));
}

function renderOutboxTray(offers) {
    _outboxOffers = (offers || []).filter(o => o && o.id);
    const badge = document.getElementById('outbox-badge');
    if (badge) {
        badge.hidden = !_outboxOffers.length;
        badge.textContent = '⬇ ' + _outboxOffers.length;
        badge.title = _outboxOffers.length === 1 ? '1 file waiting' : _outboxOffers.length + ' files waiting';
    }
    if (!_outboxSheetOpen) return;
    if (_outboxOffers.length) _renderOutboxSheet();
    else closeOutboxSheet();
}

function openOutboxSheet() {
    if (!_outboxOffers.length) return;
    _outboxSheetOpen = true;
    _renderOutboxSheet();
}

function closeOutboxSheet() {
    _outboxSheetOpen = false;
    document.getElementById('outbox-sheet')?.remove();
}

function _outboxButton(label, action, id, className) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className;
    btn.textContent = label;
    btn.dataset.action = action;
    if (id !== undefined) btn.dataset.arg = JSON.stringify(id);
    return btn;
}

function _renderOutboxSheet() {
    document.getElementById('outbox-sheet')?.remove();
    const sheet = document.createElement('section');
    sheet.id = 'outbox-sheet';
    sheet.className = 'outbox-sheet';
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-label', 'Files waiting');

    const head = document.createElement('div');
    head.className = 'outbox-sheet-head';
    const title = document.createElement('span');
    title.textContent = 'Files waiting';
    head.appendChild(title);
    head.appendChild(_outboxButton('×', 'closeOutboxSheet', undefined, 'outbox-sheet-close'));
    sheet.appendChild(head);

    for (const offer of _outboxOffers) {
        const row = document.createElement('div');
        row.className = 'outbox-row';
        const text = document.createElement('div');
        text.className = 'outbox-row-text';
        const name = document.createElement('div');
        name.className = 'outbox-row-name';
        name.textContent = offer.name;
        const meta = document.createElement('div');
        meta.className = 'outbox-row-meta';
        meta.textContent = outboxSize(offer.size) + ' · ' + (offer.sender || 'an agent')
            + ' · expires ' + _outboxMinutesLeft(offer.expires_at) + 'm';
        text.appendChild(name);
        text.appendChild(meta);
        if (offer.secret_hint) {
            const note = document.createElement('div');
            note.className = 'outbox-row-note';
            note.textContent = 'looks like a private key';
            text.appendChild(note);
        }
        row.appendChild(text);
        row.appendChild(_outboxButton('Dismiss', 'outboxDismiss', offer.id, 'outbox-dismiss'));
        row.appendChild(_outboxButton('Download', 'outboxDownload', offer.id, 'outbox-download'));
        sheet.appendChild(row);
    }

    const foot = document.createElement('div');
    foot.className = 'outbox-sheet-foot';
    foot.textContent = 'Each link works once. Dismiss removes it from every device.';
    sheet.appendChild(foot);
    document.body.appendChild(sheet);
}

function outboxDownloadUrl(id) {
    return '/api/outbox/' + encodeURIComponent(id) + '/download';
}

// Forget an offer here at once; the next /poll confirms it everywhere.
function _outboxForget(id) {
    if (typeof _closePromptPopup === 'function') _closePromptPopup('file:' + id);
    renderOutboxTray(_outboxOffers.filter(o => o.id !== id));
}

function _outboxEndedText(state) {
    return {
        picked_up: 'Already picked up on another device',
        dismissed: 'Dismissed on another device',
        expired: 'Expired',
    }[state] || 'No longer available';
}

// Only a confirmed end (404 unknown, 410 ended, or a status that is no longer
// pending) forgets an offer here. Any other failure — a 500, a 403 from an
// unlisted origin — leaves it in place: hiding a key file the server still
// holds would tell the owner it was gone when it was not.
function _outboxConfirmedEnd(status) {
    return status === 404 || status === 410;
}

async function outboxDownload(id) {
    try {
        const resp = await fetch('/api/outbox/' + encodeURIComponent(id));
        const data = await resp.json().catch(() => ({}));
        const ended = resp.ok ? !data.offer || data.offer.state !== 'pending' : _outboxConfirmedEnd(resp.status);
        if (ended) {
            showFlash('error', _outboxEndedText(data.offer ? data.offer.state : data.error));
            _outboxForget(id);
            return;
        }
        if (!resp.ok) {
            showFlash('error', 'Could not check the file; try again');
            return;
        }
    } catch (e) {
        showFlash('error', 'Offline');
        return;
    }
    const link = document.createElement('a');
    link.href = outboxDownloadUrl(id);
    link.download = '';
    link.rel = 'noopener';
    document.body.appendChild(link);
    link.click();
    link.remove();
    _outboxForget(id);
}

async function outboxDismiss(id) {
    try {
        const resp = await fetch('/api/outbox/' + encodeURIComponent(id) + '/dismiss', {method: 'POST'});
        if (!resp.ok) {
            const data = await resp.json().catch(() => ({}));
            if (!_outboxConfirmedEnd(resp.status)) {
                showFlash('error', 'Could not dismiss the file; try again');
                return;
            }
            showFlash('error', _outboxEndedText(data.error));
        }
    } catch (e) {
        showFlash('error', 'Offline');
        return;
    }
    _outboxForget(id);
}
