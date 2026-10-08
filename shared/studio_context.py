"""Pane-lifetime Studio context. Reads are snapshots; only workers enrich them.

CLI evidence is observation, never authorization. Unknown workspace identity is
kept as a suggestion. Explicit links resolve records with the configured client.
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from shared.studio_client import client
from shared.tmux import _process_start_time, _socket_path, expected_target_identity, pane_tab_target

LOCK = threading.RLock()
PANES = {}
TARGETS = {}
EFFORTS = {}
INDEX = []
ACCOUNT = {}
GENERATION = uuid.uuid4().hex
_connection = None
_workspace = "not-loaded"
_sequences = {}
_seen = {}
_launch = re.compile(r"^studio_e(\d+)_s\d+$")
_identity_fields = ("socket_path", "socket_device", "socket_inode", "server_pid",
                    "server_start_time", "pane_id", "pane_pid", "pane_start_time")


def origin(value):
    try:
        u = urlsplit(value)
        if u.scheme not in ("http", "https") or not u.hostname or u.username:
            return ""
        port = u.port or (443 if u.scheme == "https" else 80)
        return f"{u.scheme.lower()}://{u.hostname.lower()}:{port}" + u.path.rstrip("/")
    except (ValueError, TypeError):
        return ""


def connection_generation():
    global _connection, GENERATION, _workspace
    signature = hashlib.sha256((client.api_base() + "\0" + client.token() + "\0" + client.web_base()).encode()).digest()
    with LOCK:
        if signature != _connection:
            _connection = signature
            _workspace = "not-loaded"
            GENERATION = uuid.uuid4().hex
            PANES.clear()
            TARGETS.clear()
            EFFORTS.clear()
            INDEX.clear()
            ACCOUNT.clear()
            reset_connection_caches()
            _seen.clear()
            _sequences.clear()
        return GENERATION


def pane_key(identity):
    return hashlib.sha256(json.dumps([identity[k] for k in _identity_fields]).encode()).hexdigest()[:24]


def remember(target, identity, cwd="", command=""):
    key = pane_key(identity)
    with LOCK:
        pane = PANES.setdefault(key, {"pane_generation": key, "binding_generation": uuid.uuid4().hex,
            "identity": identity, "effort_id": None, "task_id": None, "source": None,
            "activity": [], "suggestions": [], "conflict": None})
        pane.update(target=target, cwd=cwd, command=command)
        match = _launch.match(target.split(":")[0])
        if match:
            pane["launch_effort"] = int(match[1])
        project = next((p for p in sorted(client.cached_projects(), key=lambda p: len(p.get("repo_path") or ""), reverse=True)
                        if p.get("repo_path") and (cwd == p["repo_path"] or cwd.startswith(p["repo_path"].rstrip("/") + "/"))), None)
        canonical = EFFORTS.get(pane["effort_id"], {}).get("effort")
        if project and canonical and canonical.get("project_id") != project.get("id"):
            _bind(pane, None, None)
            pane.pop("launch_effort", None)
        TARGETS[target] = key
        candidate = pane.get("pending_binding")
        if candidate and cwd:
            dirty(candidate["effort_id"])
    return key


def _rows(data):
    return data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])


def refresh_catalog():
    """Background only. Suppress results from a replaced credential."""
    gen = connection_generation()
    acode, account = client.request("GET", "/api/auth/status")
    gen = publish_account(acode, account, gen)
    if not gen or acode != 200:
        return
    projects = client.projects()
    with LOCK:
        directories = [p.get("cwd", "") for p in PANES.values()]
        linked_projects = {e.get("effort", {}).get("project_id") for e in EFFORTS.values()}
    selected = [p for p in projects if p.get("id") in linked_projects or p.get("repo_path") and any(
        cwd == p["repo_path"] or cwd.startswith(p["repo_path"].rstrip("/") + "/") for cwd in directories)]
    # Studio's list contract requires project_id. Collect relevant projects once
    # for all panes, retaining other catalog entries until credential rotation.
    selected = selected or projects[:50]
    catalogs = {}
    for project in selected:
        code, rows = client.request("GET", f"/api/efforts?project_id={project['id']}&limit=500")
        if code != 200:
            break
        catalogs[project["id"]] = _rows(rows)
    with LOCK:
        if gen != connection_generation():
            return
        if catalogs:
            INDEX[:] = ([e for e in INDEX if e.get("project_id") not in catalogs] +
                        [e for rows in catalogs.values() for e in rows])[:5000]


def publish_account(code, account, expected_generation):
    """Workspace selection is part of the credential generation, too."""
    global _workspace, GENERATION
    with LOCK:
        if expected_generation != connection_generation():
            return None
        if code != 200 or not isinstance(account, dict):
            ACCOUNT.update(auth_state="unauthorized" if code in (401, 403) else "unreachable")
            return GENERATION
        workspace = (account.get("active_workspace") or {}).get("id")
        if _workspace != "not-loaded" and workspace != _workspace:
            GENERATION = uuid.uuid4().hex
            PANES.clear(); TARGETS.clear(); EFFORTS.clear(); INDEX.clear()
            _seen.clear(); _sequences.clear()
            reset_connection_caches()
        _workspace = workspace
        ACCOUNT.clear()
        ACCOUNT.update({k: account.get(k) for k in ("principal_kind", "authentication_kind", "active_workspace")})
        ACCOUNT.update(auth_state="connected", verified_at=time.time())
        return GENERATION


def verify_account():
    """Mutation preflight; protected metadata, never normal poll I/O."""
    generation = connection_generation()
    code, account = client.request("GET", "/api/auth/status")
    published = publish_account(code, account, generation)
    return code == 200 and bool(published)


def reset_connection_caches():
    client.invalidate_caches()
    module = sys.modules.get("routes.studio")
    if module is not None and hasattr(module, "clear_connection_snapshot"):
        module.clear_connection_snapshot()


def _document_time(document):
    try:
        stamp = datetime.fromisoformat(document.get("version_created_at", "").replace("Z", "+00:00"))
        return stamp.timestamp() if stamp.tzinfo is not None else float("-inf")
    except (ValueError, TypeError, AttributeError):
        return float("-inf")


def _presentation_text(value, limit):
    """Bound plain metadata; keep markdown link labels rather than destinations."""
    if not isinstance(value, str):
        return ""
    value = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return " ".join(value.strip(" #*_`").split())[:limit]


def _meaningful_heading(value):
    heading = _presentation_text(value, 160)
    generic = {"handoff", "handoff note", "progress update", "status update", "update", "context"}
    if not heading or heading.lower() in generic or re.match(r"^(?:/|\.\.?/|[A-Za-z]:\\)", heading):
        return ""
    if re.fullmatch(r"\S+\.(?:md|txt|html|json)", heading, re.I):
        return ""
    return heading


def document_presentation(record):
    """Extract explicit headings/prose only, without retaining document bodies."""
    content = record.get("content_md")
    lines = content[:65536].splitlines() if isinstance(content, str) else []
    heading, summary, fence = "", "", None
    for index, line in enumerate(lines):
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence or not stripped:
            continue
        h1 = re.match(r"^#\s+(.+?)\s*#*\s*$", stripped)
        setext = index + 1 < len(lines) and re.fullmatch(r"\s*=+\s*", lines[index + 1])
        if h1 or setext:
            if not heading:
                heading = _meaningful_heading(h1[1] if h1 else stripped)
            continue
        if re.match(r"^(?:process|metadata|status|date|updated|created|author|task|effort|project|source|file|path|document|version)\s*[:=]", stripped.strip("*_`"), re.I):
            continue
        if re.match(r"^(?:sto|studio|assist|git|python\d*|node|npm|curl|cd|ls|rg)\s|^(?:https?://|/|\$\s|[A-Za-z]:\\)", stripped):
            continue
        if not summary and not re.match(r"^(?:[#>|]|[-*+]\s|\d+[.)]\s|={3,}|-{3,}|<|!?\[)", stripped):
            summary = _presentation_text(stripped, 360)
    return {"presentation_heading": heading or _meaningful_heading(record.get("title")) or "Progress update",
            "presentation_summary": summary}


def enrich_document_presentations(effort_id, documents, previous, generation, *, fetch_bodies=True):
    """Always apply version-matching metadata; optionally fetch five bodies."""
    cache = {}
    handoffs = [d for d in documents if (d.get("kind") or d.get("type")) == "handoff"]
    pending = []
    now = time.time()
    for document in handoffs:
        document_id = document.get("document_id") or document.get("id")
        version = (document.get("version_no"), document.get("version_created_at"))
        old = previous.get(document_id, {})
        entry = copy.deepcopy(old) if old.get("version") == version else {"version": version}
        cache[document_id] = entry
        document.update(presentation_heading=entry.get("presentation_heading") or _meaningful_heading(document.get("title")) or "Progress update",
                        presentation_summary=entry.get("presentation_summary", ""))
        if fetch_bodies and type(document_id) is int and not entry.get("loaded") and now - entry.get("attempted_at", 0) >= 60:
            pending.append(document)
    for document in sorted(pending, key=_document_time, reverse=True)[:5]:
        if generation != connection_generation():
            break
        document_id = document.get("document_id") or document.get("id")
        entry = cache[document_id]
        code, record = client.request("GET", f"/api/documents/{document_id}", timeout=1.0)
        entry["attempted_at"] = time.time()
        if (code != 200 or not isinstance(record, dict) or record.get("effort_id") != effort_id
                or (record.get("document_id") or record.get("id")) != document_id
                or record.get("version_no") != document.get("version_no")
                or record.get("version_created_at") != document.get("version_created_at")):
            continue
        entry.update(document_presentation(record), loaded=True)
        document.update({key: entry[key] for key in ("presentation_heading", "presentation_summary")})
    return cache


def enrich(effort_id, *, document_bodies=True):
    gen = connection_generation()
    code, effort = client.request("GET", f"/api/efforts/{effort_id}")
    if code != 200 or not isinstance(effort, dict):
        with LOCK:
            if gen == GENERATION:
                entry = EFFORTS.setdefault(effort_id, {})
                entry.update(stale=True, attempted_at=time.time())
        return False
    tcode, tasks = client.request("GET", f"/api/tasks?effort_id={effort_id}&limit=500")
    with LOCK:
        previous_docs = copy.deepcopy(EFFORTS.get(effort_id, {}))
    docs_at = previous_docs.get("documents_at", 0)
    if time.time() - docs_at >= 30:
        dcode, docs = client.request("GET", f"/api/documents?effort_id={effort_id}&limit=100")
        if dcode == 200:
            docs_at = time.time()
    else:
        dcode, docs = 200, previous_docs.get("documents", [])
    task_rows, doc_rows = _rows(tasks), _rows(docs)
    if dcode != 200:
        doc_rows = previous_docs.get("documents", [])
    link_items = [{"kind": "effort", "id": effort_id}]
    link_items += [{"kind": "task", "id": t["id"]} for t in task_rows if type(t.get("id")) is int]
    link_items += [{"kind": "document", "id": d.get("document_id") or d.get("id")} for d in doc_rows if type(d.get("document_id") or d.get("id")) is int]
    lcode, links = client.request("POST", "/api/link/resolve", {"items": link_items[:500]}) if link_items else (200, {"items": []})
    resolved = {}
    if lcode == 200:
        for link in _rows(links):
            value = urlsplit(link.get("url") or "")
            if value.scheme in ("http", "https") and value.fragment.startswith(f"/p/{effort.get('project_id')}/"):
                resolved[(link.get("kind"), link.get("id"))] = client.web_base() + "/#" + value.fragment
    for task in task_rows:
        task["studio_url"] = resolved.get(("task", task.get("id")))
    for doc in doc_rows:
        doc["studio_url"] = resolved.get(("document", doc.get("document_id") or doc.get("id")))
    document_cache = enrich_document_presentations(effort_id, doc_rows,
        previous_docs.get("document_presentations", {}), gen, fetch_bodies=document_bodies)
    effort["studio_url"] = resolved.get(("effort", effort_id))
    with LOCK:
        if gen != GENERATION:
            return False
        previous = EFFORTS.get(effort_id, {})
        EFFORTS[effort_id] = {"effort": effort, "tasks": task_rows if tcode == 200 else previous.get("tasks", []),
            "documents": doc_rows if dcode == 200 else previous.get("documents", []),
            "documents_at": docs_at,
            "document_presentations": document_cache,
            "checked_at": time.time(), "attempted_at": time.time(), "stale": tcode != 200 or dcode != 200}
        for pane in PANES.values():
            if not pane.get("cwd"):
                continue
            compatible = project_for(pane.get("cwd", ""))
            if compatible and compatible.get("id") != effort.get("project_id"):
                continue
            if pane.get("launch_effort") == effort_id and (pane["effort_id"] != effort_id or pane["source"] != "launch"):
                _bind(pane, effort_id, "launch")
            candidate = pane.get("pending_binding")
            if candidate and candidate.get("binding_generation") == pane["binding_generation"] and candidate["effort_id"] == effort_id and not pane["effort_id"] and not pane.get("launch_effort"):
                _bind(pane, effort_id, "command", candidate.get("task_id"))
        return True


def _bind(pane, effort_id, source, task_id=None):
    pane.pop("pending_binding", None)
    pane.pop("pending_task", None)
    pane.update(effort_id=effort_id, task_id=task_id, source=source,
                binding_generation=uuid.uuid4().hex, conflict=None)


def project_for(cwd):
    return next((p for p in sorted(client.cached_projects(), key=lambda p: len(p.get("repo_path") or ""), reverse=True)
        if p.get("repo_path") and (cwd == p["repo_path"] or cwd.startswith(p["repo_path"].rstrip("/") + "/"))), None)


def work_event(pane, eid, tid, eligible, verified, timestamp):
    """Shared resolution for immediate and asynchronously resolved task events."""
    if eid and eid != pane["effort_id"] and not any(s["effort_id"] == eid for s in pane["suggestions"]):
        pane["suggestions"].append({"effort_id": eid, "source": "command", "verified": verified})
        pane["suggestions"] = pane["suggestions"][-5:]
    if not eid or not verified or not eligible:
        return
    canonical = EFFORTS.get(eid, {}).get("effort")
    project = project_for(pane.get("cwd", ""))
    if canonical and project and canonical.get("project_id") != project.get("id"):
        return
    if not pane["effort_id"] and not pane.get("launch_effort"):
        if canonical and pane.get("cwd"):
            _bind(pane, eid, "command", tid)
        else:
            pane["pending_binding"] = {"effort_id": eid, "task_id": tid, "binding_generation": pane["binding_generation"]}
    if tid and eid == pane["effort_id"] and timestamp >= pane.get("task_event_at", 0):
        pane.update(task_id=tid, task_event_at=timestamp)


def resolve_pending_tasks():
    generation = connection_generation()
    with LOCK:
        pending = [(key, copy.deepcopy(p["pending_task"])) for key, p in PANES.items() if p.get("pending_task")]
    # One task read for every pane that references it in this pass.
    records = {}
    for key, event in pending[:50]:
        tid = event["task_id"]
        if tid not in records:
            code, record = client.request("GET", f"/api/tasks/{tid}")
            records[tid] = record if code == 200 and isinstance(record, dict) else None
        record = records[tid]
        with LOCK:
            pane = PANES.get(key)
            if generation != GENERATION or not pane or pane.get("pending_task") != event:
                continue
            pane.pop("pending_task", None)
            if not record or pane["binding_generation"] != event["binding_generation"]:
                continue
            eid = record.get("effort_id")
            if type(eid) is not int:
                continue
            work_event(pane, eid, tid, True, event["verified"], event["at"])
            EFFORTS.setdefault(eid, {})["attempted_at"] = 0


def dirty(effort_id):
    with LOCK:
        if effort_id in EFFORTS:
            EFFORTS[effort_id]["attempted_at"] = 0


def context(target, snapshot=None):
    gen = connection_generation()
    snap = snapshot or {}
    with LOCK:
        pane = PANES.get(TARGETS.get(target), {})
        eid = pane.get("effort_id")
        cached = EFFORTS.get(eid, {})
        effort = cached.get("effort")
        projects = client.cached_projects()
        cwd = pane.get("cwd", "")
        project = next((p for p in sorted(projects, key=lambda p: len(p.get("repo_path") or ""), reverse=True)
                        if p.get("repo_path") and (cwd == p["repo_path"] or cwd.startswith(p["repo_path"].rstrip("/") + "/"))), None)
        pid = effort.get("project_id") if effort else (project or {}).get("id")
        inbox = [q for q in snap.get("inbox", []) if (q.get("effort_id") == eid if eid else pid is not None and q.get("project_id") == pid)]
        state = ACCOUNT.get("auth_state", snap.get("state", "unconfigured"))
        stale = snap.get("state") != "connected" or state != "connected" or time.time() - ACCOUNT.get("verified_at", time.time()) > 65 or bool(cached.get("stale")) or (eid is not None and (not cached or time.time() - cached.get("checked_at", 0) > 65))
        tasks = cached.get("tasks", [])
        result = {"target": target, "connection_generation": gen,
            "pane_generation": pane.get("pane_generation"), "binding_generation": pane.get("binding_generation"),
            "state": state, "stale": stale, "checked_at": cached.get("checked_at", snap.get("last_success_at", snap.get("checked_at", 0))),
            "account": ACCOUNT, "source": pane.get("source"), "effort": effort, "project_id": pid,
            "project_name": (project or next((p for p in projects if p.get("id") == pid), {})).get("name"),
            "questions": inbox, "attention_count": None if stale else len(inbox),
            "tasks": tasks, "current_task": next((t for t in tasks if t.get("id") == pane.get("task_id")), None),
            "documents": cached.get("documents", []), "activity": pane.get("activity", [])[-60:][::-1],
            "suggestions": pane.get("suggestions", [])[-5:], "conflict": pane.get("conflict"),
            "efforts": [e for e in INDEX if not pid or e.get("project_id") == pid],
            "related_tabs": [{"target": p["target"], "task_id": p.get("task_id"), "command": p.get("command")}
                             for p in PANES.values() if eid and p.get("effort_id") == eid and p is not pane],
            "web_base": client.web_base()}
        result["url"] = (effort or {}).get("studio_url") or client.web_base() + (f"/#/p/{pid}/e/{eid}/context" if eid and pid else f"/#/p/{pid}" if pid else "/#/")
        from shared.studio_events import GAPS
        result["observation_gaps"] = GAPS
        return copy.deepcopy(result)


def validate_context(data, snapshot):
    """Freeze all action routing to the displayed pane, binding and credential."""
    current = context(data.get("target", ""), snapshot)
    for field in ("connection_generation", "pane_generation", "binding_generation"):
        if not current.get(field) or data.get(field) != current[field]:
            return None, "Tab or Studio connection changed. Refresh this panel."
    identity = expected_target_identity(current["target"])
    if identity is None or pane_key(identity.as_dict()) != current["pane_generation"]:
        return None, "The original terminal pane no longer exists."
    return current, None


def ingest(event, uid):
    """Validate safe event fields and verify the pane before touching state."""
    connection_generation()
    if uid != os.getuid() or not isinstance(event, dict) or event.get("version") != 1:
        return False
    if event.get("type") not in ("http", "finish") or type(event.get("sequence")) is not int:
        return False
    if event.get("connection_generation") != GENERATION:
        return False
    if not isinstance(event.get("timestamp"), (int, float)) or abs(time.time() - event["timestamp"]) > 120:
        return False
    identity = event.get("pane")
    if not isinstance(identity, dict):
        return False
    verified = expected_target_identity(identity.get("pane_id", ""))
    if verified is None or any(verified.as_dict().get(k) != identity.get(k) for k in _identity_fields):
        return False
    key = pane_key(identity)
    with LOCK:
        known = key in PANES
    if not known:
        # Commands in a newly created pane must not wait for the five-second
        # scan. Membership and lifetime were bracket-verified above.
        remember(pane_tab_target(verified.pane_id), verified.as_dict())
    with LOCK:
        if event.get("connection_generation") != connection_generation():
            return False
        pane = PANES.get(key)
        if not pane:
            return False
        event_id = event.get("event_id")
        invocation = event.get("invocation_id")
        if not isinstance(event_id, str) or not isinstance(invocation, str) or len(event_id) > 80 or len(invocation) > 80:
            return False
        seq_key = (key, invocation)
        if event_id in _seen or event["sequence"] <= _sequences.get(seq_key, -1):
            return False
        _seen[event_id] = time.time()
        _sequences[seq_key] = event["sequence"]
        while len(_seen) > 10000:
            _seen.pop(next(iter(_seen)))
        while len(_sequences) > 10000:
            _sequences.pop(next(iter(_sequences)))
        workspace = (ACCOUNT.get("active_workspace") or {}).get("id")
        connection = event.get("connection") or {}
        matched_origin = origin(connection.get("api_base", "")) == origin(client.api_base())
        verified_connection = matched_origin and workspace is not None and connection.get("workspace_id") == workspace
        if not matched_origin or (workspace is not None and connection.get("workspace_id") not in (None, workspace)):
            pane["conflict"] = "Command used a different Studio connection or workspace."
        refs = event.get("refs") or {}
        eid = refs.get("effort_id") if type(refs.get("effort_id")) is int else None
        tid = refs.get("task_id") if type(refs.get("task_id")) is int else None
        path = event.get("path", "")
        success = event["type"] == "http" and type(event.get("status")) is int and 200 <= event["status"] < 300
        mutation = success and event.get("method") in ("POST", "PUT", "PATCH", "DELETE") and path != "/api/link/resolve"
        eligible = mutation and tid or success and path.endswith("/handoff")
        if matched_origin:
            if mutation and tid and not eid:
                pane["pending_task"] = {"task_id": tid, "binding_generation": pane["binding_generation"], "verified": verified_connection, "at": event["timestamp"]}
            else:
                work_event(pane, eid, tid if mutation else None, eligible, verified_connection, event["timestamp"])
        # Store only a strict safe summary, never caller-supplied command/prose.
        if re.fullmatch(r"/api/[a-z0-9_/-]{1,180}", str(path)):
            pane["activity"].append({"event_id": event_id, "at": event["timestamp"],
                "summary": str(event.get("method", "GET"))[:6] + " " + path,
                "status": event.get("status"), "effort_id": eid, "task_id": tid,
                "confirmed": success, "kind": "write" if mutation else "read"})
        elif event["type"] == "finish":
            pane["activity"].append({"event_id": event_id, "at": event["timestamp"], "summary": "Studio command",
                "confirmed": False, "kind": "finish", "exit_code": event.get("exit_code")})
        pane["activity"] = pane["activity"][-500:]
        while sum(len(p["activity"]) for p in PANES.values()) > 10000:
            oldest = min((p for p in PANES.values() if p["activity"]), key=lambda p: p["activity"][0]["at"])
            oldest["activity"].pop(0)
        if eid and matched_origin:
            EFFORTS.setdefault(eid, {})["attempted_at"] = 0
        return True


def screen_suggestions(text):
    """Only rendered tool boundaries qualify; prose is deliberately ignored."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    result = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"(?:[●⏺•] ?(?:Bash\(|(?:Ran|Running) )|(?:^|\s)(?:exec|exec_command|tool)\s*[:(])", line):
            continue
        line = " ".join(lines[i:i + 3])
        if not re.search(r"\b(?:sto|studio)\s+", line):
            continue
        for match in re.finditer(r"(?:--effort(?:=|\s+)|\beffort get\s+)(\d+)\b", line):
            result.add(int(match.group(1)))
    return sorted(result)


def scan_once():
    connection_generation()
    socket_path = _socket_path()
    fmt = "#{session_name}:#{window_index}.#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_path}\t#{pane_current_command}\t#{pid}\t#{session_id}\t#{window_id}\t#{window_activity}\t#{history_size}\t#{cursor_x}\t#{cursor_y}\t#{alternate_on}"
    proc = subprocess.run(["tmux", "-S", socket_path, "list-panes", "-a", "-F", fmt], capture_output=True, text=True, timeout=3)
    if proc.returncode:
        return  # outage is not proof that every pane was killed
    info = os.stat(socket_path)
    live = set()
    targets = {}
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 13:
            continue
        target, pane_id, pid, cwd, command, server, session, window = fields[:8]
        identity = dict(socket_path=socket_path, socket_device=info.st_dev, socket_inode=info.st_ino,
            server_pid=int(server), server_start_time=_process_start_time(int(server)), session_id=session,
            window_id=window, pane_id=pane_id, pane_pid=int(pid), pane_start_time=_process_start_time(int(pid)))
        if not identity["pane_start_time"] or not identity["server_start_time"]:
            continue
        key = remember(target, identity, cwd, command)
        live.add(key)
        targets[target] = key
        with LOCK:
            match = _launch.match(target.split(":")[0])
            if match:
                eid = int(match[1])
                PANES[key]["launch_effort"] = eid
                EFFORTS.setdefault(eid, {})
            pane = PANES[key]
            marker = tuple(fields[8:])
            if pane.get("capture_marker") == marker and time.time() - pane.get("capture_at", 0) < 30:
                continue
            pane.update(capture_marker=marker, capture_at=time.time())
        # Bounded capture independently of browser polling. Bash/zsh panes also
        # host Claude/Codex descendants; boundaries determine whether it qualifies.
        captured = subprocess.run(["tmux", "-S", socket_path, "capture-pane", "-p", "-t", pane_id, "-S", "-60"], capture_output=True, text=True, timeout=2)
        digest = hashlib.sha256(captured.stdout.encode()).hexdigest()
        with LOCK:
            pane = PANES[key]
            if pane.get("capture_digest") != digest:
                pane["capture_digest"] = digest
                for eid in screen_suggestions(captured.stdout):
                    if eid != pane["effort_id"] and not any(s["effort_id"] == eid for s in pane["suggestions"]):
                        pane["suggestions"].append({"effort_id": eid, "source": "screen", "verified": False})
                        pane["suggestions"] = pane["suggestions"][-5:]
    with LOCK:
        for key in list(PANES):
            if key not in live:
                PANES.pop(key)
        TARGETS.clear()
        TARGETS.update(targets)


def studio_observer():
    while True:
        try:
            scan_once()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        time.sleep(5)


def studio_context_refresher():
    next_catalog = 0
    while True:
        try:
            if time.time() >= next_catalog:
                refresh_catalog()
                next_catalog = time.time() + (30 if ACCOUNT.get("auth_state") == "connected" else 60)
            resolve_pending_tasks()
            with LOCK:
                from shared import state
                active = PANES.get(TARGETS.get(state.tmux_target), {}).get("effort_id")
                ids = {p["effort_id"] for p in PANES.values() if p["effort_id"]}
                ids.update(p.get("launch_effort") for p in PANES.values() if p.get("launch_effort"))
                ids.update(s["effort_id"] for p in PANES.values() for s in p["suggestions"])
                due = [eid for eid in ids if time.time() - EFFORTS.get(eid, {}).get("attempted_at", 0) > (60 if EFFORTS.get(eid, {}).get("stale") else 10 if eid == active else 30)]
            for eid in due[:20]:
                enrich(eid)
            with LOCK:
                for eid in list(EFFORTS):
                    if eid not in ids:
                        EFFORTS.pop(eid, None)
        except Exception:
            pass
        time.sleep(2)
