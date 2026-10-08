// Software updates use the same fixed-purpose engine as assist update.
var _softwareUpdate = null;
var _softwareUpdateSerial = 0;

function _applyUpdateData(status) {
    if (!status) return;
    if (_softwareUpdate && status.observed_at && _softwareUpdate.observed_at && status.observed_at < _softwareUpdate.observed_at) return;
    var changed = JSON.stringify(_softwareUpdate) !== JSON.stringify(status);
    _softwareUpdate = status;
    var attention = !!(status.available || status.restart_required || status.phase === 'repair_required');
    document.getElementById('btn-actions').classList.toggle('update-attention', attention);
    document.getElementById('btn-actions').setAttribute('aria-label', attention ? 'Actions, software update needs attention' : 'Actions');
    var label = document.getElementById('update-menu-label');
    if (label) label.textContent = status.phase === 'repair_required' ? 'Repair update' : status.restart_required ? 'Restart for update' : status.available ? 'Update available' : 'Software updates';
    if (changed && _settingsPanelOpen) renderSoftwareUpdate();
}

function openSoftwareUpdate() {
    closeActionsDeck();
    if (!_settingsPanelOpen) toggleSettingsPanel();
    loadSoftwareUpdate();
    document.getElementById('software-update')?.scrollIntoView({block: 'nearest'});
}

async function loadSoftwareUpdate() {
    var serial = ++_softwareUpdateSerial;
    try {
        var response = await fetch('/api/update');
        if (!response.ok) return;
        var status = await response.json();
        if (serial === _softwareUpdateSerial) _applyUpdateData(status);
    } catch (error) { /* Cached status stays visible; no update is implied. */ }
}

function renderSoftwareUpdate() {
    var section = document.getElementById('software-update');
    if (!section) return;
    var hadFocus = section.contains(document.activeElement);
    var focusedAction = hadFocus ? document.activeElement.dataset.updateAction : null;
    section.replaceChildren();
    function node(tag, text, cls) { var n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; }
    function button(label, action, callback, disabled) {
        var b = node('button', label, 'software-update-button'); b.type = 'button'; b.dataset.updateAction = action;
        b.disabled = !!disabled; b.addEventListener('click', callback); section.appendChild(b); return b;
    }
    var s = _softwareUpdate || {}, busy = !!s.busy || _softwareUpdateRequest;
    section.appendChild(node('div', 'Software', 'settings-section-hdr'));
    var version = s.running_version || s.version || 'Checking…';
    var commit = s.running_commit || s.installed_commit;
    section.appendChild(node('p', 'Running ' + version + (commit ? ' · ' + commit.slice(0, 8) : ''), 'software-update-version'));
    var message = s.busy ? s.message || 'Checking for updates…' : s.message || 'Check for updates to this installation.';
    if (s.restart_required && s.phase !== 'repair_required' && !s.busy) message = 'Update installed. Restart to take effect.';
    var status = node('p', message, 'software-update-status'); status.setAttribute('role', 'status'); section.appendChild(status);
    if (s.checked_at) section.appendChild(node('p', 'Last checked ' + new Date(s.checked_at * 1000).toLocaleString(), 'software-update-version'));
    if (s.blocked_reason) section.appendChild(node('p', s.blocked_reason, 'software-update-warning'));
    if (s.latest_commit && s.available) section.appendChild(node('p', 'Available revision ' + s.latest_commit.slice(0, 8), 'software-update-version'));
    button('Check for updates', 'check', function () { softwareUpdateAction('/api/update/check'); }, busy);
    if (s.phase === 'repair_required') button('Repair dependencies', 'repair', function () { confirmSoftwareUpdate(true); }, busy || s.dirty);
    else if (s.available) button('Install update', 'install', function () { confirmSoftwareUpdate(false); }, busy || !!s.blocked_reason);
    if (s.restart_required && s.phase !== 'repair_required') button('Restart to take effect', 'restart', function () { restartServer(); }, busy);
    if (focusedAction) section.querySelector('[data-update-action="' + focusedAction + '"]')?.focus({preventScroll:true});
}

function confirmSoftwareUpdate(repair) {
    var s = _softwareUpdate;
    if (!s || s.busy || _softwareUpdateRequest || s.blocked_reason) return;
    var expected = repair ? s.installed_commit : s.latest_commit;
    if (!expected) return;
    var prompt = repair ? 'Repair dependencies for the installed update? Restart remains a separate step.' :
        'Install update ' + expected.slice(0, 8) + '? Your tmux sessions keep running. Restart remains a separate step.';
    if (confirm(prompt)) softwareUpdateAction('/api/update', {expected_commit:expected});
}

var _softwareUpdateRequest = false;
async function softwareUpdateAction(path, payload) {
    if (_softwareUpdateRequest || _softwareUpdate?.busy) return;
    _softwareUpdateRequest = true; renderSoftwareUpdate();
    try {
        var response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload || {})});
        var result = await response.json();
        if (!response.ok || !result.ok) showFlash('error', result.error || 'Could not start the update operation');
        else _applyUpdateData(result);
    } catch (error) {
        showFlash('error', 'Could not confirm the update request. Check its status before trying again.');
    } finally {
        _softwareUpdateRequest = false; await loadSoftwareUpdate(); renderSoftwareUpdate();
    }
}
