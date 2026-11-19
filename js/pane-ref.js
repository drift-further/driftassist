// pane-ref.js — the selected pane as a PaneRef (effort 621 Phase 7 step B).
//
// `session:w.p` names a POSITION: kill a pane and its neighbour takes the name,
// restart tmux and a new pane takes the `%N`. /poll ships each pane's durable
// key `<generation>:%N` (shared/tmux.py:PaneRef.key), which names the pane
// itself for as long as it lives and never names another one.
//
// _termTarget is the selected pane's display target — what humans, tabs and
// most of the UI read. _termKey is its durable key, or null when the server
// sends none (an older server, or a pane whose server start time it could not
// read) or has not listed the pane yet. setTermTarget() is the ONLY writer of
// either; tests/test_pane_ref_client.py fails on any other assignment.
//
// On the wire, per-pane requests (subscribe, capture, /type, /key, drafts,
// snooze) send the key with `proto: 2` when one is known, and fall back to the
// display target otherwise, which is what an older server understands.

const _PANE_KEY_RE = /^[0-9a-f]{16}:%\d+$/;

function isPaneKey(value) {
    return typeof value === 'string' && _PANE_KEY_RE.test(value);
}

function _listedPanes() {
    return (typeof _sessionPanes !== 'undefined' && _sessionPanes) || [];
}

// The durable key of the pane the user sees at `target` (the last list), or
// null. The selected pane answers with its own bound key.
function paneKeyOf(target) {
    if (!target) return null;
    if (isPaneKey(target)) return target;
    if (target === _termTarget && _termKey) return _termKey;
    const pane = _listedPanes().find(p => p.target === target);
    return (pane && pane.key) || null;
}

// The key of the pane a SELECTION at `target` picks: the listed PaneRef's,
// never the current binding. After a tmux restart a new pane can sit at the
// exact old address, and that is a different pane (sign-off M3 F3). Only a
// target the list does not show yet (a pane just launched) keeps the current
// binding when it is the current target. paneKeyOf keeps preferring the
// current binding: that is what input routing must do until the selection
// itself moves.
function selectionKeyOf(target) {
    if (!target) return null;
    if (isPaneKey(target)) return target;
    const pane = _listedPanes().find(p => p.target === target);
    if (pane) return pane.key || null;
    return target === _termTarget ? _termKey : null;
}

// Where a key's pane sits now, by the last list; anything else unchanged.
function displayOf(target) {
    if (!isPaneKey(target)) return target;
    if (target === _termKey && _termTarget) return _termTarget;
    const pane = _listedPanes().find(p => p.key === target);
    return pane ? pane.target : target;
}

// The single writer of the selection. `key` omitted: the listed pane's key
// at `target` (re-selecting the current, not yet listed target keeps its key).
function setTermTarget(target, key) {
    if (key === undefined) {
        const pane = target ? _listedPanes().find(p => p.target === target) : null;
        key = pane ? pane.key : (target && target === _termTarget ? _termKey : null);
    }
    _termTarget = target || null;
    _termKey = _termTarget ? (key || null) : null;
}

// What identifies the selected pane in per-pane client state (drafts): the
// key when bound, else the display target.
function currentPaneId() {
    return _termKey || _termTarget || '';
}

// A pane id (key or display target) for client state keyed by pane.
function paneIdOf(target) {
    if (!target) return '';
    return isPaneKey(target) ? target : (paneKeyOf(target) || target);
}

// The `target` (and `proto`) fields for one per-pane request.
function wireTarget(target) {
    const key = paneKeyOf(target);
    return key ? { target: key, proto: 2 } : { target: target || '' };
}

// Routing fields for /type and /key: the target on the wire plus a split
// pane's carried identity. Split view keeps addressing its slot by its own
// target with that identity (js/split-view.js), so it is passed through as is.
function wireRoute(target, identity) {
    identity = identity || {};
    if (typeof SplitView !== 'undefined' && SplitView.enabled) return { target, ...identity };
    return { ...wireTarget(target), ...identity };
}

// wireRoute for wherever input is routed now (getInputTarget/inputIdentity).
function inputRoute() {
    return wireRoute(getInputTarget(), typeof inputIdentity === 'function' ? inputIdentity() : {});
}

// Follow the selected pane through a fresh pane list.
// 'same': still listed where it was. 'moved': listed under another display
// target (renumbered, renamed) — the selection follows it. 'gone': not listed
// (its key is gone even if another pane took its address). 'none': nothing
// selected. A pane with no key yet is matched by display target once, and its
// key bound from then on.
function followTermPane(panes) {
    if (!_termTarget) return 'none';
    if (_termKey) {
        const pane = panes.find(p => p.key === _termKey);
        if (!pane) {
            // A pane still at our address but listed without a key: the
            // server could not read its tmux start time this once. That is
            // not the pane being gone; keep the selection and its key.
            if (panes.some(p => p.target === _termTarget && !p.key)) return 'same';
            return 'gone';
        }
        if (pane.target !== _termTarget) {
            setTermTarget(pane.target, pane.key);
            return 'moved';
        }
        return 'same';
    }
    const pane = panes.find(p => p.target === _termTarget);
    if (!pane) return 'gone';
    setTermTarget(pane.target, pane.key || null);
    return 'same';
}
