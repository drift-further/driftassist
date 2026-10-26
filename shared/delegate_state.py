"""Delegate pane classification: classify(sample, memory) -> (result, memory'), pure.

Spec 7185 §4.5–§4.9. Everything stateful (captures, /proc, the Auto-Yes maps,
the clock, the screen patterns) is read by the sample adapter in
routes/delegates.py and arrives here as data; this module reads no file,
holds no screen pattern and imports nothing with state, so every rule runs
from a plain dict (tests/test_delegate_state.py).

The sample's fields are §4.2's. `memory` is what the watcher keeps per pane
generation; pass None for a pane seen for the first time. Neither argument is
mutated. The result carries the published state with its reason, detail,
since, episode, health and observed_at, the current `candidate` state (a
WAITING still inside its hold), the alert key and `alert`, set only on the
sample that first publishes a key (the one-shot event), and `key_at`, when
the published key was first published.
"""

import copy

AGENT_KINDS = ("claude", "codex", "cursor", "opencode", "gemini")
# Kinds whose chrome or session file shows working evidence; the others count
# content changes instead (§4.8).
_SIGNAL_KINDS = ("claude", "codex")

DEFAULTS = {"alert_hold_sec": 8, "finish_quiet_sec": 45, "stall_sec": 300}
_SCANNER_TICKS_SEC = 3       # §4.5: three scanner ticks for a record to progress
_NO_RECORD_SEC = 5           # armed, eligible, and no scanner record at all
_PROBE_GRACE_SEC = 15
_MAX_FAILED_DELIVERIES = 3
_DEAD_CONFIRM_SEC = 5
_STARTING_SEC = 120
_ALERTED_KEEP = 64
_IDLE_STATES = ("waiting", "finished", "dead")
_ALERTING_STATES = ("waiting", "finished", "dead")
# Disposition statuses that are not a delivery result.
_SCANNER_STATUSES = ("countdown", "fired", "withheld", "probe-failed", "cancelled", "vetoed", "recent")


def identity_of(observation):
    """§4.3's identity, from a shared.agent_identity.ProcessObservation."""
    agent = observation.agent
    if agent:
        status = "present"
    elif not observation.complete:
        status = "incomplete"
    elif observation.wrapper:
        status = "opaque"
    else:
        status = "absent"
    return {
        "status": status,
        "kind": agent["kind"] if agent else None,
        "agent_pid": agent["pid"] if agent else None,
        "agent_start": agent["start"] if agent else None,
        "errors": list(observation.errors),
    }


def _new_memory(generation):
    return {
        "generation": generation,
        "published": None,      # {state, reason, detail, since, key}
        "candidate": None,      # {state, reason, detail, key, since}
        "health": "unknown",
        "observed_at": None,
        "episode": 0,
        "agent_gen": None,
        "ever_present": False,
        "streak": 0,
        "changes": 0,
        "last_rev": None,
        "idle_since_increment": False,
        "absent_run": 0,
        "absent_first_at": None,
        "request": None,        # (request_id, first seen)
        "stall": None,          # {value, since}
        "alerted": [],          # [key, first published at], oldest first
    }


def _key(sample, memory, state, reason_key):
    gen = "/".join(str(part) for part in (sample["generation"] or ()))
    return f"{sample['pane_id']}|{gen}|{memory['episode']}|{state}|{reason_key}"


def _publish(sample, memory, state, reason, detail, reason_key=None):
    """Publish a state now. Returns the alert, or None when the key was seen."""
    now = sample["now"]
    key = _key(sample, memory, state, reason_key) if state in _ALERTING_STATES else None
    memory["candidate"] = None
    pub = memory["published"]
    if pub and (pub["state"], pub["reason"], pub["key"]) == (state, reason, key):
        pub["detail"] = detail
    else:
        memory["published"] = {"state": state, "reason": reason, "detail": detail,
                               "since": now, "key": key}
    if state in _IDLE_STATES:
        memory["idle_since_increment"] = True
    if key is None or key in dict(memory["alerted"]):
        return None
    memory["alerted"] = (memory["alerted"] + [[key, now]])[-_ALERTED_KEEP:]
    return {"key": key, "state": state, "reason": reason, "detail": detail}


def _hold(sample, memory, config, state, reason, detail, reason_key):
    """A WAITING candidate: published once the same key has held for alert_hold_sec."""
    now = sample["now"]
    key = _key(sample, memory, state, reason_key)
    cand = memory["candidate"]
    if cand is None or cand["key"] != key:
        cand = memory["candidate"] = {"state": state, "reason": reason, "detail": detail,
                                      "key": key, "since": now}
    cand["detail"] = detail
    pub = memory["published"]
    if (pub and pub["key"] == key) or now - cand["since"] >= config["alert_hold_sec"]:
        return _publish(sample, memory, state, reason, detail, reason_key)
    return None


def _working_evidence(sample):
    chrome = sample.get("chrome") or {}
    return bool(chrome.get("working") or chrome.get("title_busy")
                or sample.get("claude_status") == "busy")


def _break_evidence(memory):
    """No sample: every consecutive count starts over (§4.8, §4.9, rule 10)."""
    memory["absent_run"], memory["absent_first_at"] = 0, None
    memory["streak"] = memory["changes"] = 0
    memory["last_rev"] = None
    memory["stall"] = None


def _track_agent(sample, memory):
    """§4.8: a new agent generation is a new episode. Complete samples only."""
    identity = sample["identity"]
    if identity["status"] == "present":
        gen = (identity.get("agent_pid"), identity.get("agent_start"))
        memory["ever_present"] = True
        if gen != memory["agent_gen"]:
            memory["agent_gen"] = gen
            memory["episode"] += 1
            memory["idle_since_increment"] = False
            memory["streak"] = 0
            memory["changes"] = 0


def _track_stall(sample, memory):
    """Rule 10's interval, on every captured sample, whatever rule wins it: the
    reconnect line disappearing or advancing starts it over."""
    stall = sample.get("stall")
    if stall is None:
        memory["stall"] = None
    elif not memory["stall"] or memory["stall"]["value"] != stall:
        memory["stall"] = {"value": stall, "since": sample["now"]}


def _track_streak(sample, memory):
    """§4.8: a working streak of 2 after an idle publication starts a new
    episode. Counted on every captured sample, incomplete ones included: any
    sample without working evidence resets it."""
    identity = sample["identity"]
    if sample.get("pane_dead") or identity["status"] == "absent":
        # A dead pane's animation (or stale working chrome) is not a new turn.
        memory["streak"] = memory["changes"] = 0
        memory["last_rev"] = None
        return
    memory["streak"] = memory["streak"] + 1 if _working_evidence(sample) else 0
    rev = sample.get("content_rev")
    moved = memory["last_rev"] is not None and rev != memory["last_rev"]
    memory["changes"] = memory["changes"] + 1 if moved else 0
    memory["last_rev"] = rev
    kind = identity.get("kind") or sample.get("content_kind")
    reached = memory["streak"] >= 2 or (kind not in _SIGNAL_KINDS and memory["changes"] >= 3)
    if reached and memory["idle_since_increment"]:
        memory["episode"] += 1
        memory["idle_since_increment"] = False
        memory["changes"] = 0


def _ownership(sample, memory):
    """§4.5: (owned, detail) for an answerable prompt."""
    now = sample["now"]
    request = sample.get("request_id")
    pane = sample["pane_id"]
    autoyes = sample.get("autoyes") or {}
    countdown = autoyes.get("countdown")
    disp = autoyes.get("disposition")
    answered = autoyes.get("answered")
    if request is None:
        return False, "Auto-Yes withheld: request not placed"
    if not autoyes.get("armed"):
        return False, ""
    seen = memory["request"]
    if not seen or seen[0] != request:
        memory["request"] = seen = (request, now)
    mine = disp if disp and disp.get("pane_id") == pane else None
    # (a) a live countdown for this request and pane is authoritative.
    if countdown:
        if (countdown.get("prompt_hash") == request and countdown.get("pane_id") == pane
                and now <= countdown.get("deadline", 0) + _SCANNER_TICKS_SEC):
            return True, ""
        return False, "Auto-Yes countdown is for another request"
    # (d) just answered.
    if answered and answered[0] == request and now - answered[1] < _SCANNER_TICKS_SEC:
        return True, ""
    if mine and mine.get("status") in ("withheld", "vetoed") and now - mine["at"] <= _SCANNER_TICKS_SEC:
        if mine["status"] == "vetoed":
            return False, "Auto-Yes vetoed: luna or a model menu on screen"
        return False, "Auto-Yes withheld: request not placed"
    if mine and mine.get("request_id") == request:
        status = mine.get("status")
        fresh = now - mine["at"] <= _SCANNER_TICKS_SEC
        if status == "cancelled":
            return False, "Auto-Yes cancelled"
        if status == "probe-failed":
            since = mine.get("probe_failing_since")
            if fresh and since is not None and now - since <= _PROBE_GRACE_SEC:
                return True, ""
            return False, "Auto-Yes identity probe failing"
        if status not in _SCANNER_STATUSES:
            # (b) a failed delivery: the gap before the scanner's next countdown.
            if fresh and mine.get("failed_deliveries", 0) < _MAX_FAILED_DELIVERIES:
                return True, ""
            return False, f"Auto-Yes delivery failed {mine.get('failed_deliveries', 0)} times"
        if fresh and status in ("countdown", "fired", "recent"):
            return True, ""
        return False, "no Auto-Yes countdown"
    # No scanner record of any kind for this request.
    if now - seen[1] < _NO_RECORD_SEC:
        return True, ""
    return False, "no Auto-Yes countdown"


def _result(memory, alert=None):
    pub = memory["published"] or {"state": "unknown", "reason": "", "detail": "", "since": None, "key": None}
    cand = memory["candidate"]
    return {
        "state": pub["state"], "reason": pub["reason"], "detail": pub["detail"],
        "since": pub["since"], "key": pub["key"], "episode": memory["episode"],
        "health": memory["health"], "observed_at": memory["observed_at"],
        "candidate": cand["state"] if cand else None, "alert": alert,
        # When the published key was first published: the wire alert's time,
        # unchanged by a later re-publication of the same key.
        "key_at": dict(memory["alerted"]).get(pub["key"]),
    }


def classify(sample, memory=None):
    """Rules 0–12 of §4.6 in order, first match wins, with §4.9's holds."""
    if memory is None or memory.get("generation") != sample["generation"]:
        memory = _new_memory(sample["generation"])
    else:
        memory = copy.deepcopy(memory)
    config = {**DEFAULTS, **(sample.get("config") or {})}
    now = sample["now"]

    # 0. No sample: keep the published state; no consecutive evidence survives it.
    if not sample.get("listing_ok", True) or not sample.get("capture_ok", True):
        memory["health"] = "stale"
        _break_evidence(memory)
        return _result(memory), memory

    # Observation bookkeeping comes before the first-match decision below, so
    # a sample that a higher rule wins still updates every running count.
    complete = sample["identity"]["status"] != "incomplete"
    if complete and not sample.get("pane_dead"):
        _track_agent(sample, memory)
    _track_streak(sample, memory)
    _track_stall(sample, memory)
    if sample.get("pane_dead") or not complete or sample["identity"]["status"] != "absent":
        memory["absent_run"], memory["absent_first_at"] = 0, None

    # 1. DEAD: tmux says the pane is dead.
    if sample.get("pane_dead"):
        memory["health"], memory["observed_at"] = "ok", now
        return _result(memory, _publish(sample, memory, "dead", "pane-dead", "", "dead")), memory

    identity = sample["identity"]
    status = identity["status"]
    # 2. Incomplete observation: keep the state (the absent run was reset above).
    if status == "incomplete":
        memory["health"] = "unknown"
        if memory["published"] is None:
            memory["published"] = {"state": "unknown", "reason": "incomplete", "detail": "",
                                   "since": now, "key": None}
        return _result(memory), memory

    memory["health"], memory["observed_at"] = "ok", now

    if status == "absent":
        memory["candidate"] = None
        memory["absent_run"] += 1
        if memory["absent_run"] == 1:
            memory["absent_first_at"] = now
        confirmed = now - memory["absent_first_at"] >= _DEAD_CONFIRM_SEC
        alert = None
        if memory["absent_run"] < 2:
            pass                                                    # 3. first absence
        elif memory["ever_present"]:
            if confirmed:                                           # 4.
                alert = _publish(sample, memory, "dead", "exited", "", "dead")
        elif now - sample.get("generation_born", now) < _STARTING_SEC:
            alert = _publish(sample, memory, "working", "starting", "")   # 5.
        elif confirmed:                                             # 6.
            alert = _publish(sample, memory, "dead", "no-agent", "", "dead")
        if memory["published"] is None:
            memory["published"] = {"state": "unknown", "reason": "", "detail": "",
                                   "since": now, "key": None}
        return _result(memory, alert), memory

    # 7. UNKNOWN: a wrapper hides the process and the screen names no agent.
    if status == "opaque" and sample.get("content_kind") not in AGENT_KINDS:
        return _result(memory, _publish(sample, memory, "unknown", "opaque", "")), memory

    attention = sample.get("attention")
    if attention:
        if attention["kind"] == "answerable":
            owned, detail = _ownership(sample, memory)
            if owned:                                               # 8.
                return _result(memory, _publish(sample, memory, "working", "answering", "")), memory
            reason = "approval"
        else:
            detail, reason = "", attention["kind"]
        detail = detail or attention.get("summary") or ""
        return _result(memory, _hold(sample, memory, config, "waiting", reason, detail,
                                     attention["key"])), memory  # 9.
    memory["request"] = None

    stall = memory["stall"]
    if stall and now - stall["since"] >= config["stall_sec"]:  # 10.
        return _result(memory, _hold(sample, memory, config, "waiting", "stalled",
                                     f"reconnecting {stall['value']}, unchanged", "stalled")), memory

    changed_at = sample.get("changed_at")
    quiet = now - changed_at if changed_at is not None else 0
    if _working_evidence(sample) or quiet < config["finish_quiet_sec"]:  # 11.
        return _result(memory, _publish(sample, memory, "working", "working", "")), memory

    # 12. FINISHED; a report row only refines the detail (§4.7).
    chrome = sample.get("chrome") or {}
    reason = "turn done" if chrome.get("turn_done") else "quiet"
    line = sample.get("report_line")
    detail = f"last report line: {line}" if line else ""
    return _result(memory, _publish(sample, memory, "finished", reason, detail, "finished")), memory
