// state.js — Global variables, constants, and DOM refs
// Loaded first; all other modules depend on these globals.

// Settings — fetched from /api/settings on load, updated on save.
// The one place the launch-mode → command mapping is written. It mirrors
// CLAUDE_COMMANDS in shared/state.py; keep the two in step if a mode is added.
const CLAUDE_COMMANDS = { npx: 'npx @anthropic-ai/claude-code', claude: 'claude' };

function claudeCmdForMode(mode) {
    return CLAUDE_COMMANDS[mode] || CLAUDE_COMMANDS.npx;
}

let SETTINGS = null;  // populated by loadSettings()
let CLAUDE_CMD = CLAUDE_COMMANDS.npx;  // default until settings load
let _serverPid = null;
let _serverUptime = 0;

async function loadSettings() {
    try {
        const r = await fetch('/api/settings');
        if (r.ok) {
            const d = await r.json();
            SETTINGS = d.settings;
            _serverPid = d.pid;
            _serverUptime = d.uptime;
            CLAUDE_CMD = claudeCmdForMode(SETTINGS.server.claude_mode);
        }
    } catch(e) {}
}

// Load on startup
loadSettings();

const input = document.getElementById('text-input');
const flash = document.getElementById('flash');
const dot = document.getElementById('status-dot');
const statusTime = document.getElementById('status-time');
const listArea = document.getElementById('list-area');
const tmuxInd = document.getElementById('tmux-ind');
const routeDot = document.getElementById('route-dot');
const routeLabel = document.getElementById('route-label');
const routeAttach = document.getElementById('route-attach');

let lastAction = null;
let _history = [];
let _favorites = [];
let _filterText = '';

// Terminal state
let _termOpen = true;           // always open in drawer layout
let _termTarget = null;         // active tmux target
let _termKey = null;            // its durable key (js/pane-ref.js); setTermTarget() writes both
let _termExpectedIdentity = null; // server-selected identity from bare launch
let _termPaused = false;        // scroll-freeze
let _termLines = 2000;          // scrollback depth; defaultTermLines() once settings load

/** The server's capture cap (limits.max_capture_lines). */
function maxTermLines() {
    const v = SETTINGS && SETTINGS.limits && SETTINGS.limits.max_capture_lines;
    return v > 0 ? v : 20000;
}

/** Scrollback a tab opens with: terminal.capture_lines, where 0 means all
 *  (the server cap). */
function defaultTermLines() {
    const v = SETTINGS && SETTINGS.terminal ? SETTINGS.terminal.capture_lines : undefined;
    if (v === 0) return maxTermLines();
    return v > 0 ? Math.min(v, maxTermLines()) : 2000;
}
let _termPollTimer = null;
let _termLatestContent = '';    // stored when paused
let _termHasNew = false;
let _termLastContent = '';      // last displayed content
let _termProjects = [];
let _termShowProjects = false;

// WebSocket state
let _termWs = null;             // WebSocket instance
let _termWsConnected = false;   // true when WS is active
let _termWsReconnectTimer = null;

// Smart actions state — per-target so background-pane scans can't reset
// the active pane's dismissal, and vice versa.
let _smartState = {};           // target -> { key, dismissedContent (ANSI-stripped) }
let _smartActionTarget = null;  // which pane the smart action came from (main or split)

function _getSmartState(target) {
    const t = target || _termTarget || '';
    if (!_smartState[t]) _smartState[t] = { key: '', dismissedContent: null };
    return _smartState[t];
}
let _layoutShifting = false;    // true during smart-actions show/hide to suppress scroll-freeze

// The session a pane belongs to, from `session:w.p`, `%N` or a durable key
// `<16 hex>:%N`. A display target names its session itself (and a session name
// may hold ':' or '.', so this is not a split on ':'); a key or `%N` is looked
// up in the last /poll's pane list. The client twin of shared/tmux.py:session_of.
function sessionOf(target) {
    if (!target) return '';
    const m = /^(.*):(\d+)\.(\d+)$/.exec(target);
    if (m) return m[1];
    const panes = typeof _sessionPanes !== 'undefined' ? _sessionPanes : [];
    const pane = panes.find(p => p.key === target || p.pane_id === target);
    if (pane) return pane.session || '';
    if (/^[0-9a-f]{16}:%\d+$/.test(target) || /^%\d+$/.test(target)) return '';
    const colon = target.indexOf(':');
    return colon < 0 ? target : target.slice(0, colon);
}

// Input routing: when true, keyboard input goes to split pane instead of main
let _inputToSplit = false;

function getInputTarget() {
    if (typeof SplitView !== 'undefined' && SplitView.enabled) return SplitView.inputTarget();
    if (_inputToSplit) {
        const session = _termTarget ? sessionOf(_termTarget) : '';
        const state = _splitPanes[session];
        if (state) return state.target;
    }
    return _termTarget;
}

// Session state
let _sessionPanes = [];
let _sessionPrompts = {};       // target -> true if prompt detected
let _paneModelLast = {};        // target -> last model_changed_at this page has rendered
let _sessionStates = {};        // target -> { state, since, prevState }

// Input state
let _sending = false;
// Attachments for the message being composed. Each entry uploads on attach, so
// it carries {id, name, size, path, uploading}; the tray clears on send.
let _attachments = [];

// Notification state
let _notifPermission = typeof Notification !== 'undefined' ? Notification.permission : 'denied';
let _notifSentFor = {};         // target -> last notified tail hash

// Saved commands state
let _splitPanes = {};              // session -> { target, ws, wsConnected, lastContent, label }
let _cmdPanelOpen = false;
let _cmdOutputOpen = false;
let _currentCommands = [];
let _currentCommandsProject = '';

// Content-based activity detection (more reliable than tmux session_activity)
let _activityDecayTimers = {};  // target -> setTimeout id
let _lastScanContent = {};      // target -> last scanned tail (for change detection)


// Idle fade: suppress for first few poll cycles so content detection can establish baseline
const _pageLoadedAt = Date.now();

// A 401 means this browser's sign-in is gone: the token was rotated or the
// cookie cleared. Without this every open tab sat on a frozen terminal with a
// red dot and no way back. Returns true when it has sent the page to /login.
let _authRedirecting = false;
function authLost(resp) {
    if (!resp || resp.status !== 401) return false;
    if (!_authRedirecting) {
        _authRedirecting = true;
        location.replace('/login');
    }
    return true;
}

// Carry the selected pane generation across asynchronous input dispatch.
function inputIdentity() {
    if (typeof SplitView === 'undefined' || !SplitView.enabled) return {};
    return {expected_target_identity: SplitView.slots[SplitView.active].identity};
}
