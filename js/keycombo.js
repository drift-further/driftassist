// js/keycombo.js — the "Any key" composer in the More Keys drawer, and the
// More keys sheet opened from the inline Keys panel.
//
// Ctrl/Alt/Shift are sticky toggles; one tap on a key (or one typed character)
// sends that combo to the active pane as POST /key {combo}, then clears them.
// Both surfaces share the one set of modifiers. The server owns the grammar
// (shared/key_combo.py) and refuses what tmux cannot name, so this file only
// collects the combo and shows what was sent.

const _comboMods = { ctrl: false, alt: false, shift: false };
const _comboNames = {
    Escape: 'Esc', BSpace: 'Backspace', Insert: 'Ins', Delete: 'Del',
    Left: '←', Up: '↑', Down: '↓', Right: '→',
};

function toggleKeyCombo() {
    const drawer = document.getElementById('drawer-left');
    const on = drawer.classList.toggle('combo-mode');
    const btn = document.getElementById('combo-toggle');
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.textContent = on ? 'All keys' : 'Any key';
    _clearComboMods();
    _setComboStatus('', false);
    drawer.scrollTop = 0;
}

function toggleComboMod(name) {
    _comboMods[name] = !_comboMods[name];
    _renderComboMods();
}

function _clearComboMods() {
    _comboMods.ctrl = _comboMods.alt = _comboMods.shift = false;
    _renderComboMods();
}

function _comboPrefix() {
    return (_comboMods.ctrl ? 'Ctrl+' : '') + (_comboMods.alt ? 'Alt+' : '')
        + (_comboMods.shift ? 'Shift+' : '');
}

function _renderComboMods() {
    document.querySelectorAll('.combo-mod').forEach(btn => {
        btn.setAttribute('aria-pressed', _comboMods[btn.dataset.mod] ? 'true' : 'false');
    });
    const prefix = _comboPrefix();
    if (prefix) _setComboStatus(prefix + '…', true);
}

function _setComboStatus(text, armed) {
    document.querySelectorAll('.combo-status').forEach(el => {
        el.textContent = text || 'Pick modifiers, then tap a key';
        el.classList.toggle('armed', !!armed);
    });
}

// Sends go one at a time. Two taps in quick succession are two requests, and
// two requests in flight can reach the pane in either order, so "ls" could
// arrive as "sl". The target is read at tap time, not when the send runs.
let _comboQueue = Promise.resolve();

function sendCombo(key) {
    const combo = { ctrl: _comboMods.ctrl, alt: _comboMods.alt, shift: _comboMods.shift, key: key };
    const label = _comboPrefix() + (_comboNames[key] || key);
    const route = {target: getInputTarget(), ...(typeof inputIdentity === 'function' ? inputIdentity() : {})};
    _clearComboMods();
    _comboQueue = _comboQueue.then(() => _postCombo(combo, label, route));
    return _comboQueue;
}

async function _postCombo(combo, label, route) {
    try {
        const resp = await fetch('/key', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({combo: combo, ...route}),
        });
        const data = await resp.json();
        if (data.ok) {
            lastAction = Date.now();
            updateStatusTime();
            _setComboStatus('Sent ' + label, false);
        } else {
            _setComboStatus(label + ': ' + (data.error || 'failed'), false);
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        _setComboStatus('', false);
        showFlash('error', 'Offline');
    }
}

// One typed character is one combo. The field empties itself so the next
// character is a fresh send; the keyboard stays up for it.
document.getElementById('combo-char').addEventListener('input', e => {
    const value = e.target.value;
    e.target.value = '';
    if (value) sendCombo(value.slice(-1));
});

// The More keys sheet. A string row is one key per character; an array row
// holds names from shared/key_combo.py's NAMED_KEYS. Between them and the base
// row (Esc, Tab, Space, Bksp, Enter) every key the grammar accepts is here:
// the 94 printable characters come from abc (letters, digits, ; , . /) and
// #+= (the other 28 symbols), uppercase from Shift + letter.
const _kbLayout = {
    abc: { cols: 10, rows: ['1234567890', 'qwertyuiop', 'asdfghjkl;', 'zxcvbnm,./'] },
    sym: { cols: 10, rows: ['`~!@#$%^&*', '()-_=+[]{}', '\\|\'":<>?'] },
    fn: { cols: 6, rows: [
        ['F1', 'F2', 'F3', 'F4', 'F5', 'F6'],
        ['F7', 'F8', 'F9', 'F10', 'F11', 'F12'],
        ['Insert', 'Delete', 'Home', 'End', 'PgUp', 'PgDn'],
        ['Left', 'Up', 'Down', 'Right'],
    ] },
};

function _renderKbLayers() {
    const host = document.getElementById('kb-layers');
    for (const [name, layout] of Object.entries(_kbLayout)) {
        const layer = document.createElement('div');
        layer.className = 'kb-layer';
        layer.dataset.layer = name;
        layer.hidden = name !== 'abc';
        for (const row of layout.rows) {
            const keys = typeof row === 'string' ? [...row] : row;
            const grid = document.createElement('div');
            grid.className = 'kb-row';
            grid.style.gridTemplateColumns = `repeat(${layout.cols}, 1fr)`;
            keys.forEach((key, i) => {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'hk-btn hk-dim kb-key';
                // Shift + a letter is its capital; Shift + any other character
                // is refused by the grammar, so those keys stand down for it.
                if (key.length === 1) btn.classList.add(/[a-z]/.test(key) ? 'kb-alpha' : 'kb-shiftless');
                btn.dataset.kb = key;
                // A short row sits centred, like the middle row of a phone keyboard.
                if (i === 0 && keys.length < layout.cols) {
                    btn.style.gridColumnStart = String(Math.floor((layout.cols - keys.length) / 2) + 1);
                }
                const label = document.createElement('span');
                label.className = 'hk-label';
                label.textContent = _comboNames[key] || key;
                btn.appendChild(label);
                grid.appendChild(btn);
            });
            layer.appendChild(grid);
        }
        host.appendChild(layer);
    }
}

function showKbLayer(name) {
    document.querySelectorAll('#kb-layers .kb-layer').forEach(layer => {
        layer.hidden = layer.dataset.layer !== name;
    });
    document.querySelectorAll('.kb-layer-btn').forEach(btn => {
        btn.setAttribute('aria-pressed', btn.dataset.layer === name ? 'true' : 'false');
    });
}

function toggleKeyboardSheet(force) {
    const sheet = document.getElementById('kb-sheet');
    const open = typeof force === 'boolean' ? force : !sheet.classList.contains('visible');
    sheet.classList.toggle('visible', open);
    const btn = document.getElementById('kb-open');
    btn.classList.toggle('active', open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    _clearComboMods();
    _setComboStatus('', false);
}

_renderKbLayers();
document.getElementById('kb-sheet').addEventListener('click', e => {
    const key = e.target.closest('[data-kb]');
    if (key) sendCombo(key.dataset.kb);
});

_setComboStatus('', false);
