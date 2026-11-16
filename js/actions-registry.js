// actions-registry.js — every control in index.html, by name.
//
// The page is served with `script-src 'self'`, so an inline `onclick="…"`
// would not run (and neither would an injected one: that is the point). Each
// control instead names its action in data attributes, and one delegated
// listener per event type looks the name up here:
//
//   data-action="name"   on click      data-input="name"   on input
//   data-change="name"   on change     data-keydown / data-submit likewise
//   data-arg='<JSON>'    one argument, a JSON literal ("shift", 58, true)
//   data-then="name"     a second, argument-less action after the first
//
// The map is explicit on purpose. Resolving any global function a data
// attribute names would let injected markup call anything on `window`; this
// way an unknown name does nothing. Entries are arrows, so the functions they
// call may be defined by scripts that load after this one.
//
// Clicks are handled in the capture phase and walk from the target up through
// every ancestor carrying data-action, innermost first — the order inline
// handlers fired in, and something that stopPropagation() in a bubbling
// listener elsewhere cannot cancel. tests/test_csp.py pins that every name in
// index.html is a key here.

const ACTIONS = {
    selectTab: a => selectTab(...a),
    toggleDelegateRail: a => toggleDelegateRail(...a),
    openDelegateSheet: () => openDelegateSheet(),
    showDelegateChip: a => setDelegateChipCollapsed(a[0], false),
    closeDelegateSheet: () => closeDelegateSheet(),
    openOutboxSheet: () => openOutboxSheet(),
    closeOutboxSheet: () => closeOutboxSheet(),
    outboxDownload: a => outboxDownload(...a),
    outboxDismiss: a => outboxDismiss(...a),
    'SplitView.openPicker': () => SplitView.openPicker(),
    accessClose: a => accessClose(...a),
    accessOpen: a => accessOpen(...a),
    ayPickAdjust: a => ayPickAdjust(...a),
    ayPickCancel: a => ayPickCancel(...a),
    ayPickConfirm: a => ayPickConfirm(...a),
    ayPickShellToggle: a => ayPickShellToggle(...a),
    ayPickTurnOff: a => ayPickTurnOff(...a),
    cancelAutoYesCountdown: a => cancelAutoYesCountdown(...a),
    changePurgeThreshold: a => changePurgeThreshold(...a),
    clearHistFilter: a => clearHistFilter(...a),
    clearHistory: a => clearHistory(...a),
    clearSelection: a => clearSelection(...a),
    closeActionsDeck: a => closeActionsDeck(...a),
    closeBottomDrawer: a => closeBottomDrawer(...a),
    closeCmdOutput: a => closeCmdOutput(...a),
    closeDrawers: a => closeDrawers(...a),
    closeLeftDrawer: a => closeLeftDrawer(...a),
    closePurgeIdle: a => closePurgeIdle(...a),
    closeStaleSheet: a => closeStaleSheet(...a),
    closeStudioConnect: a => closeStudioConnect(...a),
    closeTermSearch: a => closeTermSearch(...a),
    copyAttachCommand: a => copyAttachCommand(...a),
    createVenv: a => createVenv(...a),
    cycleTuiMode: a => cycleTuiMode(...a),
    dismissSmartActions: a => dismissSmartActions(...a),
    doPaste: a => doPaste(...a),
    filterProjects: a => filterProjects(...a),
    fitMenuApplyCustom: a => fitMenuApplyCustom(...a),
    fitMenuPick: a => fitMenuPick(...a),
    fitMenuUnpin: a => fitMenuUnpin(...a),
    gitCommitPushConfirm: a => gitCommitPushConfirm(...a),
    gitPush: a => gitPush(...a),
    gitStatus: a => gitStatus(...a),
    killPurgeSelected: a => killPurgeSelected(...a),
    killSession: a => killSession(...a),
    loadMore: a => loadMore(...a),
    onFileSelected: (a, el) => onFileSelected(el),
    onTermSearch: a => onTermSearch(...a),
    openPurgeIdle: a => openPurgeIdle(...a),
    openSoftwareUpdate: a => openSoftwareUpdate(...a),
    openStudio: a => openStudio(...a),
    pickFolderNative: a => pickFolderNative(...a),
    resetAllSettings: a => resetAllSettings(...a),
    restartServer: a => restartServer(...a),
    resumeTerminal: a => resumeTerminal(...a),
    scrollTabStrip: a => scrollTabStrip(...a),
    sendCombo: a => sendCombo(...a),
    sendInsertMode: a => sendInsertMode(...a),
    sendKey: a => sendKey(...a),
    showKbLayer: a => showKbLayer(...a),
    studioConnectSave: a => studioConnectSave(...a),
    switchHistTab: a => switchHistTab(...a),
    termFontLarger: a => termFontLarger(...a),
    termFontSmaller: a => termFontSmaller(...a),
    termSearchNext: a => termSearchNext(...a),
    termSearchPrev: a => termSearchPrev(...a),
    toggleAccessSheet: a => toggleAccessSheet(...a),
    toggleActionsDeck: a => toggleActionsDeck(...a),
    toggleAutoYes: a => toggleAutoYes(...a),
    toggleBottomDrawer: a => toggleBottomDrawer(...a),
    toggleChrome: a => toggleChrome(...a),
    toggleClaudeInfo: a => toggleClaudeInfo(...a),
    toggleCmdPanel: a => toggleCmdPanel(...a),
    toggleComboMod: a => toggleComboMod(...a),
    toggleEnterLock: a => toggleEnterLock(...a),
    toggleFitMenu: a => toggleFitMenu(...a),
    toggleGitPanel: a => toggleGitPanel(...a),
    toggleIdleTucking: a => toggleIdleTucking(...a),
    toggleInputTarget: a => toggleInputTarget(...a),
    toggleKeyCombo: a => toggleKeyCombo(...a),
    toggleKeyboardSheet: a => toggleKeyboardSheet(...a),
    toggleKeysPanel: a => toggleKeysPanel(...a),
    toggleLeftDrawer: a => toggleLeftDrawer(...a),
    toggleProjects: a => toggleProjects(...a),
    toggleSettingsPanel: a => toggleSettingsPanel(...a),
    toggleSkillsPanel: a => toggleSkillsPanel(...a),
    toggleStudioPanel: a => toggleStudioPanel(...a),
    toggleTabWrap: a => toggleTabWrap(...a),
    toggleTermSearch: a => toggleTermSearch(...a),
    triggerUpload: a => triggerUpload(...a),
    tuiJumpEnd: a => tuiJumpEnd(...a),
    tuiPage: a => tuiPage(...a),
    typeCmd: a => typeCmd(...a),
    typePrefix: a => typePrefix(...a),
    vaultSendDefault: a => vaultSendDefault(...a),
};

function _runAction(name, args, el, ev) {
    const fn = Object.prototype.hasOwnProperty.call(ACTIONS, name) ? ACTIONS[name] : null;
    if (!fn) {
        console.warn('actions-registry: no action named', name);
        return;
    }
    fn(args, el, ev);
}

function _actionArgs(el) {
    const raw = el.dataset.arg;
    if (raw === undefined) return [];
    try {
        return [JSON.parse(raw)];
    } catch (e) {
        console.warn('actions-registry: data-arg is not JSON', raw);
        return [];
    }
}

function _dispatchActions(attr, ev) {
    for (let el = ev.target; el; el = el.parentElement) {
        const name = el.dataset ? el.dataset[attr] : undefined;
        if (!name) continue;
        _runAction(name, _actionArgs(el), el, ev);
        if (el.dataset.then) _runAction(el.dataset.then, [], el, ev);
        if (attr !== 'action') return;  // only clicks bubble through ancestors
    }
}

document.addEventListener('click', ev => _dispatchActions('action', ev), true);
document.addEventListener('input', ev => _dispatchActions('input', ev), true);
document.addEventListener('change', ev => _dispatchActions('change', ev), true);
document.addEventListener('keydown', ev => _dispatchActions('keydown', ev), true);
document.addEventListener('submit', ev => _dispatchActions('submit', ev), true);
