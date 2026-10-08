"""Cached Studio presentation and typed, generation-guarded mutations."""
import hashlib
import json
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request
from shared import studio_context as contexts
from shared.studio_client import client

context_bp = Blueprint("studio_context", __name__)
_actions = {}
_uncertain_notes = {}
_action_lock = threading.Lock()


def snapshot():
    from routes.studio import studio_snapshot
    return studio_snapshot()


def question_revision(question):
    fields = ("id", "status", "prompt", "options_json", "answer_json", "effort_id", "task_id", "profile_statement_id")
    return hashlib.sha256(json.dumps({k: question.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()[:24]


def task_revision(task):
    """The review facts shared by Studio's list and individual-task contracts."""
    state = {k: task.get(k) for k in ("id", "effort_id", "title", "status", "status_changed_at")}
    for field, keys in (("gates", ("id", "gate_name", "verdict", "reason")),
                        ("findings", ("id", "gate_name", "title", "severity", "status"))):
        records = task.get(field)
        state[field] = sorted([{k: row.get(k) for k in keys} for row in records if isinstance(row, dict)],
                              key=lambda row: json.dumps(row, sort_keys=True)) if isinstance(records, list) else None
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()[:24]


def recommendation_error(question):
    answer = question.get("answer_json")
    if question.get("status") != "recommended" or not isinstance(answer, dict):
        return "No current recommendation to accept"
    text = answer.get("text", "")
    if not isinstance(text, str) or len(text) > 10000:
        return "The recommendation contains invalid answer text. Review it in Studio."
    if "rationale" in answer and not isinstance(answer["rationale"], str):
        return "The recommendation contains an invalid rationale. Review it in Studio."
    selections = answer.get("selections", [])
    if not isinstance(selections, list) or not all(isinstance(value, str) and value.strip() for value in selections):
        return "The recommendation contains invalid selections. Review it in Studio."
    if selections:
        options = question.get("options_json")
        if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
            return "Review this recommendation in Studio; its option labels cannot be verified here."
        if any(value not in options for value in selections):
            return "The recommendation selects an option the question does not offer."
    if not text.strip() and not selections:
        return "No answer has been proposed; a rationale alone is not a recommendation."
    return None


def present(target):
    result = contexts.context(target, snapshot())
    for q in result["questions"]:
        q["assist_revision"] = question_revision(q)
    for task in result["tasks"]:
        task["assist_revision"] = task_revision(task)
    if result.get("current_task"):
        result["current_task"]["assist_revision"] = task_revision(result["current_task"])
    return result


@context_bp.get("/studio/context")
def studio_context():
    return jsonify(present(request.args.get("target", "")))


def failure(message, status=409, **extra):
    return jsonify(ok=False, error=message, **extra), status


def upstream(code, record, uncertain_write=True):
    detail = record.get("detail", record.get("error")) if isinstance(record, dict) else None
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail)
    if detail and client.token():
        detail = str(detail).replace(client.token(), "[redacted]")
    if code == 0 or 200 <= code < 300:
        if not uncertain_write:
            return failure("Studio could not supply the current record. No update was submitted.", 502)
        return failure("Studio did not confirm the outcome. Check the record in Studio before submitting again.", 502, uncertain=True)
    return failure(str(detail or f"Studio returned HTTP {code}")[:2000], code if 400 <= code < 500 else 502)


def checked(data, fresh=True):
    if not isinstance(data, dict):
        return None, failure("Object body required", 400)
    current, error = contexts.validate_context(data, snapshot())
    if error:
        return None, failure(error)
    if fresh and (current["stale"] or current.get("conflict")):
        return None, failure("Studio context is stale or uses a different workspace. Refresh or review the connection.")
    return current, None


@context_bp.route("/studio/context-binding", methods=["PUT", "DELETE"])
def studio_binding():
    data = request.get_json(silent=True)
    current, error = checked(data, fresh=False)
    if error:
        return error
    if current["state"] != "connected":
        return failure("Connect to Studio before changing this link.")
    eid = data.get("effort_id") if request.method == "PUT" else None
    if eid is not None and (type(eid) is not int or eid < 1):
        return failure("effort_id must be a positive integer", 400)
    if request.method == "PUT" and eid is None:
        return failure("Choose an effort", 400)
    if current["source"] == "launch" and (eid != (current["effort"] or {}).get("id")):
        return failure("This pane was launched by Studio for its current effort. Use a separate pane for another effort.")
    if eid:
        if not contexts.verify_account():
            return failure("Studio could not verify this account. Review the connection.")
        # Explicit human link resolves access through the selected credential.
        verified = contexts.enrich(eid, document_bodies=False)
        with contexts.LOCK:
            effort = contexts.EFFORTS.get(eid, {}).get("effort")
        if not verified or not effort:
            return failure("Studio could not verify that effort.", 404)
        if current["project_id"] and effort.get("project_id") != current["project_id"]:
            return failure("That effort belongs to another project.")
    task_id = data.get("task_id")
    if task_id is not None:
        if type(task_id) is not int:
            return failure("task_id must be an integer", 400)
        code, task = client.request("GET", f"/api/tasks/{task_id}")
        if code != 200 or not isinstance(task, dict) or task.get("effort_id") != eid:
            return failure("Task does not belong to this effort.")
    # Account/tab changes during the verification request must invalidate it.
    current, error = checked(data, fresh=False)
    if error:
        return error
    with contexts.LOCK:
        pane = contexts.PANES.get(current["pane_generation"])
        if not pane or pane["binding_generation"] != data["binding_generation"]:
            return failure("The link changed while Studio was being checked.")
        contexts._bind(pane, eid, "launch" if current["source"] == "launch" else "manual" if eid else None, task_id)
    return jsonify(ok=True, context=present(data["target"]))


def action_once(handler):
    """The same UI attempt is never written twice, including uncertain writes."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return failure("Object body required", 400)
    attempt = data.get("request_id")
    if not isinstance(attempt, str) or len(attempt) > 80 or not attempt:
        return failure("request_id required", 400)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    with _action_lock:
        old = _actions.get(attempt)
        if old:
            if old[0] != digest:
                return failure("This submission id already belongs to a different action.")
            return old[1]
        current, error = checked(data)
        if error:
            return error
        if not contexts.verify_account():
            return failure("Studio could not verify this account. Review the connection.")
        current, error = checked(data)
        if error:
            return error
        note_key = (current["connection_generation"], current["pane_generation"], data.get("task_id"))
        previous_note = _uncertain_notes.get(note_key) if request.path == "/studio/task-note" else None
        if previous_note:
            code, events = client.request("GET", f"/api/tasks/{data['task_id']}/events")
            match = None
            for event in events if code == 200 and isinstance(events, list) else []:
                try:
                    created = datetime.fromisoformat(event.get("created_at", "").replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    continue
                if event.get("status") is None and event.get("note") == previous_note["note"] and created >= previous_note["at"] - 1:
                    match = event
                    break
            if not match:
                return failure("The previous note has an unconfirmed outcome. Studio has not yet supplied its execution record; another note will not be sent.", 502, uncertain=True)
            _uncertain_notes.pop(note_key, None)
            if data.get("note", "").strip() == previous_note["note"]:
                return jsonify(ok=True, reconciled=True, record={"id": match.get("id"), "task_id": data["task_id"]})
        started = time.time()
        result = handler(data, current)
        response = result[0] if isinstance(result, tuple) else result
        body = response.get_json(silent=True)
        if request.path == "/studio/task-note" and isinstance(body, dict) and body.get("uncertain"):
            _uncertain_notes[note_key] = {"note": data.get("note", "").strip(), "at": started}
            while len(_uncertain_notes) > 512:
                _uncertain_notes.pop(next(iter(_uncertain_notes)))
        _actions[attempt] = (digest, result)
        while len(_actions) > 512:
            _actions.pop(next(iter(_actions)))
        return result


@context_bp.post("/studio/task-status")
def studio_task_status():
    return action_once(_task_status)


def _task_record(data, current):
    tid = data.get("task_id")
    if type(tid) is not int or not current["effort"]:
        return None, failure("A task in the linked effort is required", 400)
    code, task = client.request("GET", f"/api/tasks/{tid}")
    if code != 200 or not isinstance(task, dict):
        return None, upstream(code, task, uncertain_write=False)
    if task.get("effort_id") != current["effort"].get("id"):
        return None, failure("Task belongs to a different effort.")
    if task.get("status") != data.get("expected_status"):
        return None, failure("Task status changed. Refresh before updating it.")
    if data.get("task_revision") is not None and data["task_revision"] != task_revision(task):
        return None, failure("Task or review evidence changed. Review it again before updating it.")
    # Recheck local context after the network read, before any write.
    _, error = checked(data)
    return task, error


def _task_status(data, current):
    status = data.get("status")
    if status not in ("todo", "doing", "review", "done", "blocked"):
        return failure("Unknown task status", 400)
    if status == "done" and not isinstance(data.get("task_revision"), str):
        return failure("Review the current task before confirming completion.")
    task, error = _task_record(data, current)
    if error:
        return error
    code, record = client.request("POST", f"/api/tasks/{task['id']}/status", {"status": status})
    confirmed = (code == 200 and isinstance(record, dict) and record.get("id") == task["id"]
                 and record.get("effort_id") == task["effort_id"] and record.get("status") == status)
    if not confirmed:
        if code == 0 or 200 <= code < 300:
            # A lost response does not mean the write failed. Read its canonical
            # result once, and let the background cache keep checking; never replay.
            contexts.dirty(task["effort_id"])
            read_code, latest = client.request("GET", f"/api/tasks/{task['id']}")
            _, context_error = checked(data, fresh=False)
            if (not context_error and read_code == 200 and isinstance(latest, dict)
                    and latest.get("id") == task["id"] and latest.get("effort_id") == task["effort_id"]
                    and latest.get("status") == status):
                return jsonify(ok=True, reconciled=True, record=latest)
        return upstream(code, record)
    contexts.dirty(task["effort_id"])
    return jsonify(ok=True, record=record)


@context_bp.post("/studio/task-note")
def studio_task_note():
    return action_once(_task_note)


def _task_note(data, current):
    note = data.get("note")
    if not isinstance(note, str) or not note.strip() or len(note) > 10000:
        return failure("Write a progress note (maximum 10,000 characters)", 400)
    task, error = _task_record(data, current)
    if error:
        return error
    code, record = client.request("POST", f"/api/tasks/{task['id']}/execution", {"note": note.strip()})
    if code != 201 or not isinstance(record, dict) or record.get("task_id") != task["id"]:
        return upstream(code, record)
    contexts.dirty(task["effort_id"])
    return jsonify(ok=True, record=record)


def answer_question():
    return action_once(_answer_question)


def _answer_question(data, current):
    qid = data.get("question_id")
    if type(qid) is not int:
        return failure("question_id must be an integer", 400)
    displayed = next((q for q in current["questions"] if q.get("id") == qid), None)
    if not displayed:
        return failure("Question is no longer in this tab's attention queue.")
    code, question = client.request("GET", f"/api/questions/{qid}")
    if code != 200 or not isinstance(question, dict):
        return upstream(code, question, uncertain_write=False)
    if question.get("status") == "answered":
        from routes.studio import _drop_from_snapshot
        _drop_from_snapshot(qid)
        return failure("This question has already been answered. Refresh the panel.")
    if question_revision(question) != data.get("question_revision"):
        return failure("Question or recommendation changed. Review it again before answering.")
    if question.get("profile_statement_id"):
        return failure("Accept this profile statement in Studio, where its current revision can be reviewed.", 409)
    if data.get("action") == "accept":
        proposal_error = recommendation_error(question)
        if proposal_error:
            return failure(proposal_error, 400)
        answer = question["answer_json"]
    elif data.get("action", "answer") == "answer":
        answer = {}
        selection, text = data.get("select"), data.get("text")
        if selection is not None:
            if not isinstance(selection, str) or selection not in (question.get("options_json") or []):
                return failure("Option not offered", 400)
            answer["selections"] = [selection]
        if text is not None:
            if not isinstance(text, str) or len(text) > 10000:
                return failure("Invalid answer text", 400)
            if text.strip():
                answer["text"] = text.strip()
        if not answer:
            return failure("Empty answer", 400)
    else:
        return failure("Unknown answer action", 400)
    _, error = checked(data)
    if error:
        return error
    code, record = client.answer(qid, answer)
    if code != 200 or not isinstance(record, dict) or record.get("status") != "answered":
        return upstream(code, record)
    from routes.studio import _drop_from_snapshot
    _drop_from_snapshot(qid)
    contexts.dirty(question.get("effort_id"))
    return jsonify(ok=True, record=record)
