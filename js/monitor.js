// monitor.js — Polling, health, session states, notifications, git actions

// -- Polling (WebSocket preferred, HTTP fallback) --
function startPolling() {
    stopPolling();
    if (_termTarget && _termOpen) {
        // Try WebSocket first — give it 2s to connect before falling back to HTTP
        connectTerminalWs();
        // Do one immediate HTTP capture for instant display
        captureTerminal();
        // Only start HTTP polling timer if WS fails to connect within 2s
        setTimeout(function() {
            if (!_termWsConnected && _termOpen && _termTarget && !_termPollTimer && !document.hidden) {
                _termPollTimer = setInterval(captureTerminal, SETTINGS ? SETTINGS.connection.http_fallback_poll_ms : 3000);
                updateConnIndicator();
            }
        }, 2000);
    }
    // Session tab refresh is now handled by consolidatedPoll() every 5s
}

function stopPolling() {
    disconnectTerminalWs();
    if (_termPollTimer) {
        clearInterval(_termPollTimer);
        _termPollTimer = null;
    }
    updateConnIndicator();
}

// -- tmux indicator --
function _currentPaneDisplayName() {
    if (!_termTarget) return '';
    const pane = (_sessionPanes || []).find(p => p.target === _termTarget);
    if (pane && pane.agent_name) return agentDisplayName(pane);
    return shortName(sessionOf(_termTarget));
}

function updateTmuxIndicator() {
    if (_termTarget) {
        const name = _currentPaneDisplayName();
        const suffix = _inputToSplit ? ' (split)' : '';
        tmuxInd.textContent = '\u2192 ' + name + suffix;
        tmuxInd.classList.add('active');
    } else {
        tmuxInd.textContent = '';
        tmuxInd.classList.remove('active');
    }
    updateRouteIndicator();
}

function updateRouteIndicator() {
    if (_termTarget) {
        const name = _currentPaneDisplayName();
        const session = sessionOf(_termTarget);
        routeDot.className = 'route-dot tmux';
        routeLabel.textContent = 'tmux \u2192 ' + name;
        routeLabel.style.color = 'var(--cyan)';
        const cmd = 'tmux attach -t ' + session;
        routeAttach.textContent = '\u2192 ' + cmd;
        routeAttach.dataset.cmd = cmd;
        routeAttach.title = 'Copy: ' + cmd;
    } else {
        routeDot.className = 'route-dot desktop';
        routeLabel.textContent = 'Desktop (focused window)';
        routeLabel.style.color = 'var(--text-dim)';
        routeAttach.textContent = '';
        delete routeAttach.dataset.cmd;
        routeAttach.removeAttribute('title');
    }
}

// Copies the bare command, not the displayed arrow prefix — it goes straight
// into a shell.
function copyAttachCommand() {
    const cmd = routeAttach.dataset.cmd;
    if (!cmd) return;
    if (copyToClipboard(cmd)) {
        showFlash('copied', 'Copied \u2014 ' + cmd);
        routeAttach.classList.add('copied');
        setTimeout(() => routeAttach.classList.remove('copied'), 1200);
    } else {
        showFlash('error', 'Copy failed');
    }
}

// ================================================================
// Health check + status time
// ================================================================
function updateStatusTime() {
    if (!lastAction) {
        statusTime.textContent = '';
        return;
    }
    const secs = Math.floor((Date.now() - lastAction) / 1000);
    if (secs < 5) statusTime.textContent = 'just now';
    else if (secs < 60) statusTime.textContent = secs + 's ago';
    else if (secs < 3600) statusTime.textContent = Math.floor(secs / 60) + 'm ago';
    else statusTime.textContent = Math.floor(secs / 3600) + 'h ago';
}

// ================================================================
// Completion notifications
// ================================================================

function sendDoneNotification(session) {
    if (_notifPermission !== 'granted') return;
    try {
        new Notification('Assist - ' + shortName(session), {
            body: 'Agent finished. Output ready.',
            tag: 'assist-done-' + session,
            renotify: true,
        });
    } catch(e) {}
}

// ================================================================
// Prompt notification scanner (all sessions)
// ================================================================
function requestNotifPermission() {
    if (typeof Notification === 'undefined') return;
    if (Notification.permission === 'default') {
        Notification.requestPermission().then(p => { _notifPermission = p; });
    }
}

function sendPromptNotification(session, detected) {
    if (_notifPermission !== 'granted') return;
    const shortSession = shortName(session);
    const body = detected.desc + ': ' + detected.actions.map(a => a.label).join(' / ');
    try {
        const n = new Notification('Assist \u2014 ' + shortSession, {
            body: body,
            icon: 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y="80" font-size="80">\u26A1</text></svg>',
            tag: 'assist-prompt-' + session,
            renotify: true,
        });
        n.onclick = function() {
            window.focus();
            n.close();
        };
    } catch(e) {}
}

// ================================================================
// Git Quick Actions (isolated — runs in temp tmux session)
// ================================================================
function toggleGitPanel() {
    const panel = document.getElementById('git-panel');
    const btn = document.getElementById('btn-git');
    const visible = panel.classList.contains('visible');
    panel.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

// target/expectDir: a confirmed Commit & push passes the pane and directory it
// showed the user, so dispatch never rereads a selection that may have moved.
async function gitRunOp(op, message, target, expectDir) {
    showFlash('sent', 'Git: running...');
    try {
        const body = { op, target: target !== undefined ? target : getInputTarget() };
        if (message !== undefined) body.message = message;
        if (expectDir !== undefined) body.expect_dir = expectDir;
        const resp = await fetch('/api/git/run', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', 'Git: done');
            lastAction = Date.now();
            updateStatusTime();
        } else {
            showFlash('error', data.reason || data.error || 'Git failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

function gitStatus() { gitRunOp('status'); }
function gitPush() { gitRunOp('push'); }

async function createVenv() {
    showFlash('sent', 'Creating .venv...');
    try {
        const resp = await fetch('/api/venv/create', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target: getInputTarget() }),
        });
        const data = await resp.json();
        if (data.ok) {
            showFlash('sent', '.venv created + activated');
        } else {
            showFlash('error', data.error || 'Failed');
        }
    } catch (e) {
        showFlash('error', 'Offline');
    }
}

// ================================================================
// Claude Info Bar
// ================================================================
function toggleClaudeInfo() {
    const bar = document.getElementById('claude-info-bar');
    const btn = document.getElementById('btn-info');
    const visible = bar.classList.contains('visible');
    bar.classList.toggle('visible', !visible);
    btn.classList.toggle('active', !visible);
}

function updateClaudeInfo(meta) {
    if (!meta || Object.keys(meta).length === 0) return;

    // Context usage
    const ctxEl = document.getElementById('ci-context');
    if (meta.used_percentage != null) {
        const pct = meta.used_percentage;
        const tokK = Math.floor((meta.tokens || 0) / 1000);
        const limK = Math.floor((meta.limit || 0) / 1000);
        const filled = Math.min(Math.max(Math.round(pct / 10), 0), 10);
        const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);
        ctxEl.textContent = bar + ' ' + pct.toFixed(1) + '% (' + tokK + 'k/' + limK + 'k)';
        ctxEl.className = 'ci-context' + (pct >= 80 ? ' crit' : pct >= 50 ? ' warn' : '');
    } else {
        ctxEl.textContent = '';
    }

    // Duration
    const durEl = document.getElementById('ci-duration');
    if (meta.duration_ms) {
        const secs = meta.duration_ms / 1000;
        const h = Math.floor(secs / 3600);
        const m = Math.floor((secs % 3600) / 60);
        durEl.textContent = h > 0 ? h + 'h' + m + 'm' : m + 'm';
    } else {
        durEl.textContent = '';
    }

    // Cost
    const costEl = document.getElementById('ci-cost');
    if (meta.cost_usd) {
        costEl.textContent = '$' + meta.cost_usd.toFixed(2);
    } else {
        costEl.textContent = '';
    }

    // Block timer
    const blockEl = document.getElementById('ci-block');
    if (meta.block_active) {
        const elH = Math.floor((meta.block_elapsed || 0) / 3600);
        const elM = Math.floor(((meta.block_elapsed || 0) % 3600) / 60);
        const remH = Math.floor((meta.block_remaining || 0) / 3600);
        const remM = Math.floor(((meta.block_remaining || 0) % 3600) / 60);
        const elStr = elH > 0 ? elH + 'h' + elM + 'm' : elM + 'm';
        const remStr = remH > 0 ? remH + 'h' + remM + 'm' : remM + 'm';
        const pct = Math.min(100, Math.round((meta.block_elapsed / (5 * 3600)) * 100));
        const filled = Math.min(Math.max(Math.round(pct / 10), 0), 10);
        const bar = '\u2588'.repeat(filled) + '\u2591'.repeat(10 - filled);
        blockEl.textContent = bar + ' ' + elStr + '/' + remStr;
        blockEl.className = 'ci-block' + (pct >= 85 ? ' crit' : pct >= 60 ? ' warn' : '');
    } else {
        blockEl.textContent = '';
    }

    // Task
    const taskEl = document.getElementById('ci-task');
    taskEl.textContent = 'Task: ' + (meta.task || 'None');

    // Edited files
    const filesEl = document.getElementById('ci-files');
    const edited = meta.edited_files || 0;
    const bits = ['\u270e ' + edited];
    if (meta.staged_files) bits.push('+' + meta.staged_files);
    if (meta.untracked_files) bits.push('?' + meta.untracked_files);
    if (meta.deleted_files) bits.push('-' + meta.deleted_files);
    filesEl.textContent = bits.join(' ');
    filesEl.title = 'Edited files' +
        (meta.staged_files ? ' · staged ' + meta.staged_files : '') +
        (meta.untracked_files ? ' · untracked ' + meta.untracked_files : '') +
        (meta.deleted_files ? ' · deleted ' + meta.deleted_files : '');

    // Open tasks
    const tasksEl = document.getElementById('ci-tasks-count');
    tasksEl.textContent = '[' + (meta.open_tasks || 0) + ']';

    // Branch
    const branchEl = document.getElementById('ci-branch');
    branchEl.textContent = meta.branch ? '\u238b ' + meta.branch : '';

    // Model
    const modelEl = document.getElementById('ci-model');
    modelEl.textContent = meta.model || '';
}

// Browser-local deduplication: a second browser receives the same snapshot.
const _delegateNotificationSeen = new Set();
function sendDelegateNotification(pane) {
    const alert = pane.delegate?.alert;
    if (!alert?.key || _delegateNotificationSeen.has(alert.key)) return;
    _delegateNotificationSeen.add(alert.key);
    if ((_termOpen && pane.target === _termTarget) ||
        (typeof SplitView !== 'undefined' && SplitView.readable(pane.target))) return;
    if (!window.isSecureContext || _notifPermission !== 'granted' ||
        !(Number(alert.at) * 1000 > _pageLoadedAt)) return;
    try {
        const n = new Notification('Assist — ' + shortName(pane.session), {
            body: alert.state + ': ' + (alert.detail || alert.reason || ''),
            tag: 'assist-delegate-' + pane.pane_id, renotify: true,
        });
        n.onclick = () => { window.focus(); selectTab(pane.target); n.close(); };
    } catch (e) {}
}
