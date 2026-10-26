// devices.js — Settings → Access → Devices, and the "name this device" step.
//
// Every signed-in browser is a record in shared/devices.py with its own
// cookie. This sheet lists them and revokes one at a time. Names are labels
// someone typed and IPs and User-Agents arrive in headers, so every row is
// built with DOM text nodes — never parsed as markup.

let _devicesSheetOpen = false;
let _deviceThis = null;        // {id, name, named} from /poll, or null (token)
let _devnamePrompted = false;  // once per page load

function openDevicesSheet() {
    _setDevicesSheet(true);
    loadDevices();
}

function _setDevicesSheet(open) {
    const sheet = document.getElementById('devices-sheet');
    if (!sheet) return;
    _devicesSheetOpen = open;
    sheet.classList.toggle('visible', open);
    sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function _devAgo(ts) {
    if (!ts) return '—';
    const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (sec < 90) return 'now';
    if (sec < 3600) return Math.floor(sec / 60) + ' min ago';
    if (sec < 86400) return Math.floor(sec / 3600) + ' h ago';
    if (sec < 7 * 86400) return Math.floor(sec / 86400) + ' d ago';
    return _devDate(ts);
}

function _devDate(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

const _DEV_SOURCE_VERB = {
    login: 'signed in', window: 'admitted', pair: 'paired', approval: 'approved'
};

function _el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

async function _devicesPost(path, body) {
    const resp = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {})
    });
    let data = null;
    try { data = await resp.json(); } catch (e) { /* not JSON */ }
    return { status: resp.status, data: data || {} };
}

function _deviceRow(d, thisId) {
    const me = d.id === thisId;
    const row = _el('div', 'dev-row' + (me ? ' me' : '') + (d.revoked_at ? ' revoked' : ''));

    const name = _el('div', 'dev-name', d.name || '—');
    if (me) name.appendChild(_el('span', 'dev-tag me', 'this device'));
    if (d.revoked_at) name.appendChild(_el('span', 'dev-tag revoked', 'revoked ' + _devDate(d.revoked_at)));
    row.appendChild(name);

    const verb = _DEV_SOURCE_VERB[d.source] || 'signed in';
    const meta = verb + ' ' + _devDate(d.created) + ' · last seen ' + _devAgo(d.last_seen) +
                 (d.ip ? ' · ' + d.ip : '');
    row.appendChild(_el('div', 'dev-meta', meta));

    const acts = _el('div', 'dev-acts');
    if (d.revoked_at) {
        const forget = _el('button', 'dev-btn', 'Forget');
        forget.addEventListener('click', () => _deviceAction('/access/devices/forget', { id: d.id }));
        acts.appendChild(forget);
    } else {
        const rename = _el('button', 'dev-btn', 'Rename');
        rename.addEventListener('click', () => {
            const next = prompt('Device name', d.name || '');
            if (next === null) return;
            _deviceAction('/access/devices/rename', { id: d.id, name: next });
        });
        const revoke = _el('button', 'dev-btn red', 'Revoke');
        revoke.addEventListener('click', () => {
            const msg = me
                ? 'Revoke THIS browser? It is signed out immediately and must sign in again.'
                : 'Revoke "' + (d.name || d.id) + '"? That browser is signed out immediately.';
            if (!confirm(msg)) return;
            _deviceAction('/access/devices/revoke', { id: d.id, confirm_self: me });
        });
        acts.appendChild(rename);
        acts.appendChild(revoke);
    }
    row.appendChild(acts);
    return row;
}

async function _deviceAction(path, body) {
    try {
        const res = await _devicesPost(path, body);
        if (res.status === 401) { location.href = '/login'; return; }
        if (!res.data.ok) {
            showToast('Not done: ' + (res.data.error || res.status), 'error');
        }
    } catch (e) {
        showToast('Could not reach the server', 'error');
    }
    loadDevices();
}

// Only the newest request may paint: an older response that arrives after a
// newer one (a reload racing a revoke's reload) is discarded, not shown.
let _devicesLoadSeq = 0;

function _devicesFailed(notice, message) {
    // Keep the rows already shown: a failed read is not an empty registry.
    if (!notice) return;
    notice.hidden = false;
    notice.textContent = 'Could not load devices' + (message ? ': ' + message : '.') +
        ' The list below may be out of date.';
}

async function loadDevices() {
    const list = document.getElementById('devices-list');
    const notice = document.getElementById('devices-notice');
    if (!list) return;
    const seq = ++_devicesLoadSeq;
    let data;
    try {
        const resp = await fetch('/access/devices');
        if (resp.status === 401) { location.href = '/login'; return; }
        try { data = await resp.json(); } catch (e) { data = null; }
        if (seq !== _devicesLoadSeq) return;
        if (!resp.ok || !data || data.ok !== true || !Array.isArray(data.devices)) {
            _devicesFailed(notice, (data && data.error) || ('HTTP ' + resp.status));
            return;
        }
    } catch (e) {
        if (seq === _devicesLoadSeq) _devicesFailed(notice, '');
        return;
    }
    if (notice) {
        notice.hidden = !data.recovery_required;
        notice.textContent = data.recovery_required ? data.message : '';
    }
    list.replaceChildren();
    const rows = data.devices.slice().sort((a, b) =>
        (!!a.revoked_at - !!b.revoked_at) || ((b.last_seen || 0) - (a.last_seen || 0)));
    if (!rows.length) list.appendChild(_el('div', 'dev-empty', 'No browsers signed in.'));
    for (const d of rows) list.appendChild(_deviceRow(d, data.this_device));
}

// --- Name step ---------------------------------------------------------------

// Called on every /poll. While the sheet is open it follows the registry, so a
// revoke or rename from another browser shows up without reopening it; and a
// name chosen in another tab closes this tab's prompt.
function applyDeviceData(device) {
    _deviceThis = device;
    if (device && !device.named && !_devnamePrompted) {
        _devnamePrompted = true;
        _setDevnameSheet(true, device.name || '');
    } else if (device && device.named) {
        const sheet = document.getElementById('devname-sheet');
        if (sheet && sheet.classList.contains('visible')) _setDevnameSheet(false);
    }
    if (_devicesSheetOpen) loadDevices();
}

function _setDevnameSheet(open, suggestion) {
    const sheet = document.getElementById('devname-sheet');
    const backdrop = document.getElementById('devname-backdrop');
    if (!sheet) return;
    sheet.classList.toggle('visible', open);
    sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
    if (backdrop) {
        backdrop.classList.toggle('visible', open);
        backdrop.setAttribute('aria-hidden', open ? 'false' : 'true');
    }
    if (open) {
        const input = document.getElementById('devname-input');
        const hint = document.getElementById('devname-suggest');
        if (input) input.value = suggestion;
        if (hint) hint.textContent = suggestion;
    }
}

async function _devnameSave(skip) {
    const input = document.getElementById('devname-input');
    const name = skip ? '' : (input ? input.value.trim() : '');
    _setDevnameSheet(false);
    const hint = document.getElementById('devname-suggest');
    const chosen = name || (hint ? hint.textContent : '');
    if (!chosen) return;
    try {
        const res = await _devicesPost('/access/devices/name', { name: chosen });
        if (!res.data.ok) showToast('Name not saved: ' + (res.data.error || res.status), 'error');
    } catch (e) {
        showToast('Name not saved', 'error');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const close = document.getElementById('devices-close');
    if (close) close.addEventListener('click', () => _setDevicesSheet(false));
    const save = document.getElementById('devname-save');
    if (save) save.addEventListener('click', () => _devnameSave(false));
    const skip = document.getElementById('devname-skip');
    if (skip) skip.addEventListener('click', () => _devnameSave(true));
    const input = document.getElementById('devname-input');
    if (input) input.addEventListener('keydown', ev => {
        if (ev.key === 'Enter') { ev.preventDefault(); _devnameSave(false); }
    });
});
