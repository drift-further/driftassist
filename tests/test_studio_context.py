"""Pane isolation, stale forms, bounded observations and canonical writes."""
import copy
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from flask import Flask
from shared import studio_context as context
from shared import studio_events
from shared.studio_client import StudioClient
from routes import studio_context as routes
from routes.studio import studio_bp
from shared.tmux import ExpectedTargetIdentity


IDENTITY = dict(socket_path="/tmp/example", socket_device=1, socket_inode=2,
    server_pid=3, server_start_time="4", session_id="$0", window_id="@0",
    pane_id="%0", pane_pid=5, pane_start_time="6")


class StudioContextTests(unittest.TestCase):
    def setUp(self):
        patches = [patch.object(context, name, {}) for name in ("PANES", "TARGETS", "EFFORTS", "ACCOUNT", "_seen", "_sequences")]
        patches += [patch.object(context, "INDEX", []), patch.object(routes, "_actions", {}), patch.object(routes, "_uncertain_notes", {}),
            patch.object(context.client, "api_base", return_value="http://studio.test"),
            patch.object(context.client, "web_base", return_value="http://studio.test"),
            patch.object(context.client, "token", return_value="private-token"),
            patch.object(context.client, "cached_projects", return_value=[{"id": 10, "name": "Assist", "repo_path": "/work"}]),
            patch.object(context, "expected_target_identity", return_value=ExpectedTargetIdentity(**IDENTITY))]
        patches.append(patch.object(context, "verify_account", return_value=True))
        patches.append(patch.object(context, "_workspace", "not-loaded"))
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        context.connection_generation()
        context.ACCOUNT.update(active_workspace={"id": 11})
        self.target = "one:0.0"
        self.key = context.remember(self.target, IDENTITY, "/work", "claude")
        self.question = {"id": 30, "project_id": 10, "effort_id": 636, "task_id": None,
            "status": "pending", "prompt": "Continue?", "options_json": ["yes", "no"], "answer_json": None}
        self.task = {"id": 42, "effort_id": 636, "title": "Build", "status": "doing", "gates": [], "findings": []}
        context.EFFORTS[636] = {"effort": {"id": 636, "project_id": 10, "title": "Context", "phase": "build"},
            "tasks": [self.task], "documents": [], "checked_at": time.time(), "stale": False}
        self.snapshot = {"state": "connected", "inbox": [self.question, {"id": 31, "effort_id": 999, "project_id": 20}], "checked_at": time.time()}
        p = patch.object(routes, "snapshot", return_value=self.snapshot); p.start(); self.addCleanup(p.stop)
        app = Flask(__name__); app.register_blueprint(routes.context_bp); app.register_blueprint(studio_bp)
        self.browser = app.test_client()

    def bind(self):
        context._bind(context.PANES[self.key], 636, "manual", 42)

    def payload(self, **extra):
        current = context.context(self.target, self.snapshot)
        task_guard = {"task_revision": routes.task_revision(self.task)} if extra.get("task_id") == self.task["id"] else {}
        return {**{k: current[k] for k in ("target", "pane_generation", "binding_generation", "connection_generation")}, "request_id": "attempt-1", **task_guard, **extra}

    def event(self, **extra):
        return {"version": 1, "type": "http", "timestamp": time.time(), "pane": IDENTITY,
            "connection_generation": context.GENERATION,
            "event_id": "event-1", "invocation_id": "invocation-1", "sequence": 1,
            "connection": {"api_base": "http://studio.test", "workspace_id": 11},
            "method": "POST", "path": "/api/tasks/42/status", "status": 200,
            "refs": {"task_id": 42, "effort_id": 636}, **extra}

    def test_context_and_poll_summary_never_call_studio(self):
        self.bind()
        with patch.object(context.client, "request", side_effect=AssertionError("network on read")):
            result = self.browser.get('/studio/context?target=one:0.0').get_json()
            self.assertEqual(result["current_task"]["id"], 42)
            self.assertEqual([q["id"] for q in result["questions"]], [30])
            self.assertNotIn("private-token", json.dumps(result))

    def test_eligible_event_binds_only_originating_pane(self):
        other = {**IDENTITY, "pane_id": "%1", "pane_pid": 8}
        key2 = context.remember("two:0.0", other, "/work", "codex")
        self.assertTrue(context.ingest(self.event(), os.getuid()))
        self.assertEqual(context.PANES[self.key]["effort_id"], 636)
        self.assertEqual(context.PANES[self.key]["task_id"], 42)
        self.assertIsNone(context.PANES[key2]["effort_id"])

    def test_read_of_another_effort_does_not_switch(self):
        self.bind()
        self.assertTrue(context.ingest(self.event(method="GET", path="/api/efforts/999", refs={"effort_id": 999}), os.getuid()))
        self.assertEqual(context.PANES[self.key]["effort_id"], 636)
        self.assertEqual(context.PANES[self.key]["suggestions"][0]["effort_id"], 999)

    def test_workspace_unknown_stays_suggestion(self):
        context.ACCOUNT.clear()
        self.assertTrue(context.ingest(self.event(), os.getuid()))
        self.assertIsNone(context.PANES[self.key]["effort_id"])
        self.assertFalse(context.PANES[self.key]["suggestions"][0]["verified"])

    def test_workspace_mismatch_preserves_binding(self):
        self.bind()
        context.ingest(self.event(connection={"api_base": "http://studio.test", "workspace_id": 12}), os.getuid())
        self.assertEqual(context.PANES[self.key]["effort_id"], 636)
        self.assertIn("different", context.PANES[self.key]["conflict"])

    def test_duplicates_sequences_uid_and_old_pane_are_rejected(self):
        event = self.event()
        self.assertFalse(context.ingest(event, os.getuid() + 1))
        self.assertTrue(context.ingest(event, os.getuid()))
        self.assertFalse(context.ingest(event, os.getuid()))
        self.assertFalse(context.ingest({**event, "event_id": "later", "sequence": 0}, os.getuid()))
        self.assertFalse(context.ingest({**event, "event_id": "old", "pane": {**IDENTITY, "pane_start_time": "old"}}, os.getuid()))
        self.assertFalse(context.ingest({**event, "event_id": "old-connection", "connection_generation": "old"}, os.getuid()))

    def test_event_store_never_saves_prose_or_credentials(self):
        context.ingest(self.event(command="sto answer --text private", stdout="secret", token="secret"), os.getuid())
        self.assertNotIn("secret", json.dumps(context.PANES[self.key]))
        self.assertNotIn("private", json.dumps(context.PANES[self.key]))

    def test_screen_prose_is_not_command_evidence(self):
        self.assertEqual(context.screen_suggestions('Please run sto handoff show --effort 636'), [])
        self.assertEqual(context.screen_suggestions('⏺ Bash(sto handoff show --effort 636)'), [636])
        self.assertEqual(context.screen_suggestions('exec: sto handoff show --effort=637'), [637])

    def test_offline_snapshot_keeps_records_and_hides_attention(self):
        self.bind(); self.snapshot["state"] = "unreachable"
        result = context.context(self.target, self.snapshot)
        self.assertTrue(result["stale"]); self.assertIsNone(result["attention_count"])
        self.assertEqual(result["questions"][0]["id"], 30)

    def test_stale_form_and_wrong_effort_cannot_write(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="review")
        context._bind(context.PANES[self.key], 999, "manual")
        with patch.object(context.client, "request") as call:
            response = self.browser.post('/studio/task-status', json=payload)
            self.assertEqual(response.status_code, 409); call.assert_not_called()

    def test_status_rechecks_record_and_duplicate_attempt_writes_once(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="review")
        with patch.object(context.client, "request", side_effect=[(200, self.task), (200, {**self.task, "status": "review"})]) as call:
            first = self.browser.post('/studio/task-status', json=payload)
            second = self.browser.post('/studio/task-status', json=payload)
            self.assertTrue(first.get_json()["ok"]); self.assertEqual(first.get_json(), second.get_json())
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args.args[-1], {"status": "review"})

    def test_changed_status_and_permission_refusal_are_visible(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        with patch.object(context.client, "request", return_value=(200, {**self.task, "status": "review"})) as call:
            result = self.browser.post('/studio/task-status', json=payload)
            self.assertEqual(result.status_code, 409); self.assertEqual(call.call_count, 1)
        payload["request_id"] = "attempt-2"
        with patch.object(context.client, "request", side_effect=[(200, self.task), (403, {"detail": "human QA credential required"})]):
            result = self.browser.post('/studio/task-status', json=payload)
            self.assertEqual(result.status_code, 403); self.assertIn("human QA", result.get_json()["error"])

    def test_status_lost_acknowledgment_reads_committed_state_without_replaying(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        completed = {**self.task, "status": "done"}
        for index, ambiguous in enumerate(((0, None), (200, None), (202, {}))):
            payload["request_id"] = f"lost-ack-{index}"
            with patch.object(context.client, "request", side_effect=[(200, self.task), ambiguous, (200, completed)]) as call:
                with patch.object(context, "dirty") as dirty:
                    first = self.browser.post('/studio/task-status', json=payload)
                    second = self.browser.post('/studio/task-status', json=payload)
                self.assertTrue(first.get_json()["reconciled"])
                self.assertEqual(first.get_json(), second.get_json())
                self.assertEqual(first.get_json()["record"]["status"], "done")
                self.assertEqual([c.args[0] for c in call.call_args_list], ["GET", "POST", "GET"])
                dirty.assert_called_once_with(636)

    def test_status_unknown_outcome_keeps_refreshing_without_false_success(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        for index, lookup in enumerate(((0, None), (200, self.task),
                (200, {**self.task, "id": 43, "status": "done"}),
                (200, {**self.task, "effort_id": 999, "status": "done"}))):
            payload["request_id"] = f"unknown-{index}"
            with patch.object(context.client, "request", side_effect=[(200, self.task), (0, None), lookup]) as call:
                with patch.object(context, "dirty") as dirty:
                    result = self.browser.post('/studio/task-status', json=payload)
                self.assertEqual(result.status_code, 502)
                self.assertTrue(result.get_json()["uncertain"])
                self.assertEqual(call.call_count, 3)
                dirty.assert_called_once_with(636)

    def test_status_reconciliation_cannot_acknowledge_a_changed_binding(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        def reply(method, path, *args):
            if method == "POST":
                context._bind(context.PANES[self.key], 999, "manual")
                return 0, None
            return 200, {**self.task, "status": "done" if args or context.PANES[self.key]["effort_id"] == 999 else "doing"}
        with patch.object(context.client, "request", side_effect=reply):
            result = self.browser.post('/studio/task-status', json=payload)
        self.assertTrue(result.get_json()["uncertain"])

    def test_status_refusal_is_not_reconciled_as_success(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        with patch.object(context.client, "request", side_effect=[(200, self.task), (403, {"detail": "Human review required"})]) as call:
            result = self.browser.post('/studio/task-status', json=payload)
        self.assertEqual(result.status_code, 403)
        self.assertNotIn("uncertain", result.get_json())
        self.assertEqual(call.call_count, 2)

    def test_status_reconciles_a_real_http_response_lost_after_commit(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", status="done")
        canonical = dict(self.task); writes = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps(canonical).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                canonical.update(status=body['status']); writes.append(body)
                # The write committed, but the socket closes before the reply.
                self.close_connection = True

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        transport = StudioClient()
        try:
            with patch.object(transport, 'api_base', return_value=f'http://127.0.0.1:{server.server_port}'), \
                    patch.object(transport, 'token', return_value=''), \
                    patch.object(context.client, 'request', wraps=transport.request):
                first = self.browser.post('/studio/task-status', json=payload)
                second = self.browser.post('/studio/task-status', json=payload)
            self.assertTrue(first.get_json()['reconciled'])
            self.assertEqual(first.get_json(), second.get_json())
            self.assertEqual(canonical['status'], 'done')
            self.assertEqual(writes, [{'status': 'done'}])
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)

    def test_uncertain_note_is_not_repeated(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", note="Progress")
        with patch.object(context.client, "request", side_effect=[(200, self.task), (0, None)]) as call:
            first = self.browser.post('/studio/task-note', json=payload)
            second = self.browser.post('/studio/task-note', json=payload)
            self.assertTrue(first.get_json()["uncertain"]); self.assertEqual(second.status_code, 502)
            self.assertEqual(call.call_count, 2)

    def test_new_attempt_reconciles_uncertain_note_instead_of_appending(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", note="Progress")
        with patch.object(context.client, "request", side_effect=[(200, self.task), (0, None)]):
            self.browser.post('/studio/task-note', json=payload)
        payload["request_id"] = "another-attempt"
        with patch.object(context.client, "request", return_value=(200, [])) as call:
            result = self.browser.post('/studio/task-note', json=payload)
            self.assertTrue(result.get_json()["uncertain"]); self.assertEqual(call.call_count, 1)
        event = {"id": 99, "status": None, "note": "Progress", "created_at": datetime.now(timezone.utc).isoformat()}
        with patch.object(context.client, "request", return_value=(200, [event])) as call:
            result = self.browser.post('/studio/task-note', json=payload)
            self.assertTrue(result.get_json()["reconciled"]); self.assertEqual(call.call_count, 1)

    def test_question_reread_and_server_derived_attribution(self):
        self.bind(); payload = self.payload(question_id=30, question_revision=routes.question_revision(self.question), action="answer", select="yes")
        with patch.object(context.client, "request", side_effect=[(200, self.question), (200, {**self.question, "status": "answered"})]) as call:
            with patch('routes.studio._drop_from_snapshot'):
                response = self.browser.post('/studio/answer', json=payload)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(call.call_args.args[-1], {"status": "answered", "answer_json": {"selections": ["yes"]}})

    def test_changed_question_does_not_submit(self):
        self.bind(); payload = self.payload(question_id=30, question_revision=routes.question_revision(self.question), select="yes")
        with patch.object(context.client, "request", return_value=(200, {**self.question, "prompt": "Changed?"})) as call:
            response = self.browser.post('/studio/answer', json=payload)
            self.assertEqual(response.status_code, 409); self.assertEqual(call.call_count, 1)

    def test_invalid_recommendations_never_answer_upstream(self):
        self.bind()
        cases = [{}, {"rationale": "Choose yes"}, {"text": "   "}, {"text": 1},
                 {"selections": "yes"}, {"selections": [None]}, {"selections": [""]},
                 {"selections": ["unoffered"]}, {"text": "Yes", "rationale": []}]
        for index, answer in enumerate(cases):
            with self.subTest(answer=answer):
                question = {**self.question, "status": "recommended", "answer_json": answer}
                self.snapshot["inbox"][0] = question
                payload = self.payload(question_id=30, request_id=f"invalid-{index}",
                    question_revision=routes.question_revision(question), action="accept")
                with patch.object(context.client, "request", return_value=(200, question)) as call:
                    response = self.browser.post('/studio/answer', json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(call.call_count, 1)

    def test_unknown_options_do_not_invent_recommendation_labels(self):
        self.bind()
        question = {**self.question, "status": "recommended", "options_json": {"yes": "details"},
                    "answer_json": {"selections": ["yes"]}}
        self.snapshot["inbox"][0] = question
        payload = self.payload(question_id=30, question_revision=routes.question_revision(question), action="accept")
        with patch.object(context.client, "request", return_value=(200, question)) as call:
            response = self.browser.post('/studio/answer', json=payload)
        self.assertEqual(response.status_code, 400); self.assertEqual(call.call_count, 1)
        self.assertIn("Studio", response.get_json()["error"])

    def test_accept_preserves_the_exact_valid_proposal_and_rationale(self):
        self.bind()
        for index, proposal in enumerate(({"selections": ["yes"], "text": "Proceed", "rationale": "Checks passed"},
                                          {"selections": [], "text": "An explicit free-text answer", "rationale": "Because"})):
            question = {**self.question, "status": "recommended", "answer_json": proposal}
            self.snapshot["inbox"][0] = question
            payload = self.payload(question_id=30, request_id=f"valid-{index}",
                question_revision=routes.question_revision(question), action="accept")
            with patch.object(context.client, "request", side_effect=[(200, question), (200, {**question, "status": "answered"})]) as call:
                with patch('routes.studio._drop_from_snapshot'):
                    response = self.browser.post('/studio/answer', json=payload)
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(call.call_args.args[-1]["answer_json"], proposal)

    def test_changed_recommendation_rationale_invalidates_confirmation(self):
        self.bind()
        displayed = {**self.question, "status": "recommended", "answer_json": {"selections": ["yes"], "rationale": "Old reason"}}
        self.snapshot["inbox"][0] = displayed
        payload = self.payload(question_id=30, question_revision=routes.question_revision(displayed), action="accept")
        current = {**displayed, "answer_json": {"selections": ["yes"], "rationale": "Changed reason"}}
        with patch.object(context.client, "request", return_value=(200, current)) as call:
            response = self.browser.post('/studio/answer', json=payload)
        self.assertEqual(response.status_code, 409); self.assertEqual(call.call_count, 1)

    def test_changed_review_evidence_invalidates_completion(self):
        self.bind()
        self.task.update(status="review", status_changed_at="2026-10-03T16:35:00+00:00",
                         gates=[{"id": 1, "gate_name": "verification", "verdict": "pass", "reason": "Passed"}])
        payload = self.payload(task_id=42, expected_status="review", status="done")
        for index, change in enumerate(({"title": "Another task title"}, {"status_changed_at": "2026-10-03T17:35:00+00:00"},
                       {"gates": [{"id": 1, "gate_name": "verification", "verdict": "fail", "reason": "Failed"}]})):
            payload["request_id"] = f"review-{index}"
            with patch.object(context.client, "request", return_value=(200, {**self.task, **change})) as call:
                response = self.browser.post('/studio/task-status', json=payload)
            self.assertEqual(response.status_code, 409); self.assertEqual(call.call_count, 1)

    def test_task_revision_agrees_between_studio_list_and_detail(self):
        listed = {**self.task, "gates": [{"id": 1, "gate_name": "qa", "verdict": "pass", "reason": "Verified"}]}
        detail = {**listed, "gates": [{**listed["gates"][0], "round": 2, "verdict_label": "Passed"}],
                  "documents": [], "round": {"current": 2}, "attention": {"failed_gate": False}}
        self.assertEqual(routes.task_revision(listed), routes.task_revision(detail))

    def test_documents_are_bounded_version_cached_and_not_fetched_on_context_read(self):
        self.bind()
        documents = [{"id": number, "effort_id": 636, "kind": "handoff", "title": "Handoff note",
                      "version_no": 1, "version_created_at": f"2026-10-0{number}T12:00:00+00:00",
                      "studio_url": f"http://studio.test/document/{number}"} for number in range(1, 7)]
        bodies = {d["id"]: {**d, "content_md": f"```md\n# Fake heading\n```\n# Handoff note\n# Verified update {d['id']}\nActual progress recorded here."} for d in documents}
        calls = []
        def read(method, path, **kwargs):
            self.assertFalse(context.LOCK._is_owned())
            calls.append(path)
            return 200, bodies[int(path.rsplit('/', 1)[1])]
        with patch.object(context.client, "request", side_effect=read):
            cache = context.enrich_document_presentations(636, documents, {}, context.GENERATION)
        self.assertEqual(calls, [f"/api/documents/{n}" for n in range(6, 1, -1)])
        self.assertEqual(documents[0]["presentation_heading"], "Progress update")
        self.assertEqual(documents[5]["presentation_heading"], "Verified update 6")
        self.assertEqual(documents[5]["presentation_summary"], "Actual progress recorded here.")
        self.assertNotIn("content_md", json.dumps(cache))
        with patch.object(context.client, "request", side_effect=read):
            cache = context.enrich_document_presentations(636, documents, cache, context.GENERATION)
        self.assertEqual(calls[-1], '/api/documents/1'); self.assertEqual(len(calls), 6)
        context.EFFORTS[636]["documents"] = documents
        with patch.object(context.client, "request", side_effect=AssertionError("I/O on cached read")):
            result = self.browser.get('/studio/context?target=one:0.0').get_json()
        self.assertEqual(result["documents"][5]["presentation_heading"], "Verified update 6")
        self.assertEqual(result["documents"][5]["studio_url"], documents[5]["studio_url"])

    def test_document_failures_retry_and_never_reuse_an_old_version_heading(self):
        document = {"id": 9, "kind": "handoff", "title": "Handoff note", "version_no": 1,
                    "version_created_at": "2026-10-03T12:00:00+00:00"}
        with patch.object(context.client, "request", return_value=(0, None)) as call:
            cache = context.enrich_document_presentations(636, [document], {}, context.GENERATION)
            context.enrich_document_presentations(636, [document], cache, context.GENERATION)
        self.assertEqual(call.call_count, 1)
        cache[9]["attempted_at"] -= 61
        body = {**document, "effort_id": 636, "content_md": "# Completed verification\nAll checks passed."}
        with patch.object(context.client, "request", return_value=(200, body)) as call:
            cache = context.enrich_document_presentations(636, [document], cache, context.GENERATION)
        self.assertEqual(document["presentation_heading"], "Completed verification")
        newer = {**document, "version_no": 2}
        with patch.object(context.client, "request", return_value=(200, body)) as call:
            cache = context.enrich_document_presentations(636, [newer], cache, context.GENERATION)
        self.assertEqual(newer["presentation_heading"], "Progress update")
        self.assertFalse(cache[9].get("loaded")); self.assertEqual(call.call_count, 1)

    def test_binding_refresh_keeps_matching_cached_heading_without_body_io(self):
        self.bind()
        document = {"id": 9, "effort_id": 636, "kind": "handoff", "title": "Handoff note", "version_no": 1,
                    "version_created_at": "2026-10-03T12:00:00+00:00"}
        context.EFFORTS[636].update(documents_at=time.time() - 31, document_presentations={9: {
            "version": (1, document["version_created_at"]), "loaded": True,
            "presentation_heading": "Implementation verified", "presentation_summary": "Ready for human review."}})
        def read(method, path, *args, **kwargs):
            if path == '/api/efforts/636': return 200, {"id": 636, "project_id": 10}
            if path.startswith('/api/tasks?'): return 200, {"items": [self.task]}
            if path.startswith('/api/documents?'): return 200, {"items": [copy.deepcopy(document)]}
            if path == '/api/link/resolve': return 200, {"items": [{"kind": "document", "id": 9,
                "url": "http://studio.test/#/p/10/e/636/documents/9"}]}
            self.fail(f"Binding fetched a document body: {path}")
        with patch.object(context.client, "request", side_effect=read) as call:
            self.assertTrue(context.enrich(636, document_bodies=False))
        cached = context.EFFORTS[636]["documents"][0]
        self.assertEqual(cached["presentation_heading"], "Implementation verified")
        self.assertEqual(cached["presentation_summary"], "Ready for human review.")
        self.assertEqual(cached["version_created_at"], document["version_created_at"])
        self.assertIn('/documents/9', cached["studio_url"])
        self.assertTrue(any('/api/documents?' in c.args[1] for c in call.call_args_list))
        document.update(version_no=2, version_created_at="2026-10-03T13:00:00+00:00")
        context.EFFORTS[636]["documents_at"] -= 31
        with patch.object(context.client, "request", side_effect=read):
            self.assertTrue(context.enrich(636, document_bodies=False))
        changed = context.EFFORTS[636]["documents"][0]
        self.assertEqual(changed["presentation_heading"], "Progress update")
        self.assertEqual(changed["presentation_summary"], "")
        self.assertFalse(context.EFFORTS[636]["document_presentations"][9].get("loaded"))

    def test_document_summary_skips_process_metadata_and_commands(self):
        result = context.document_presentation({"content_md": "# Implementation verified\n"
            "Process: /home/tester/private/process\nMetadata: generated\n"
            "**Updated:** today\nsto task get 42\n/home/tester/file.md\n"
            "The completed interface awaits your visual review.", "title": "Handoff note"})
        self.assertEqual(result["presentation_summary"], "The completed interface awaits your visual review.")

    def test_document_fetch_cannot_publish_into_a_changed_workspace(self):
        self.bind()
        context.publish_account(200, {"active_workspace": {"id": 11}}, context.GENERATION)
        document = {"id": 9, "effort_id": 636, "kind": "handoff", "title": "Handoff note", "version_no": 1,
                    "version_created_at": "2026-10-03T12:00:00+00:00"}
        def read(method, path, *args, **kwargs):
            if path == '/api/efforts/636': return 200, {"id": 636, "project_id": 10}
            if path.startswith('/api/tasks?'): return 200, {"items": []}
            if path.startswith('/api/documents?'): return 200, {"items": [document]}
            if path == '/api/link/resolve': return 200, {"items": []}
            context.publish_account(200, {"active_workspace": {"id": 12}}, context.GENERATION)
            return 200, {**document, "content_md": "# Old account update\nPrivate prose."}
        with patch.object(context.client, "request", side_effect=read):
            self.assertFalse(context.enrich(636))
        self.assertFalse(context.EFFORTS)

    def test_connection_change_clears_bindings_and_account(self):
        self.bind()
        old = context.GENERATION
        with patch.object(context.client, "token", return_value="another-token"):
            self.assertNotEqual(context.connection_generation(), old)
            self.assertFalse(context.PANES); self.assertFalse(context.ACCOUNT)

    def test_connection_change_drops_legacy_question_snapshot(self):
        from routes import studio
        with studio._snapshot_lock:
            studio._snapshot["inbox"] = [self.question]
        with patch.object(context.client, "token", return_value="another-token"):
            context.connection_generation()
            self.assertEqual(studio.studio_snapshot()["inbox"], [])

    def test_workspace_change_with_same_token_invalidates_forms(self):
        self.bind()
        old = context.GENERATION
        context.publish_account(200, {"active_workspace": {"id": 11}}, old)
        self.assertTrue(context.PANES)
        context.publish_account(200, {"active_workspace": {"id": 12}}, old)
        self.assertNotEqual(old, context.GENERATION)
        self.assertFalse(context.PANES); self.assertFalse(context.EFFORTS)

    def test_failed_auth_status_disables_context_writes(self):
        self.bind()
        context.publish_account(403, None, context.GENERATION)
        result = context.context(self.target, self.snapshot)
        self.assertTrue(result["stale"]); self.assertEqual(result["state"], "unauthorized")

    def test_task_execution_resolves_parent_before_binding(self):
        event = self.event(path="/api/tasks/42/execution", refs={"task_id": 42})
        context.ingest(event, os.getuid())
        self.assertIsNone(context.PANES[self.key]["effort_id"])
        with patch.object(context.client, "request", return_value=(200, self.task)):
            context.resolve_pending_tasks()
        self.assertEqual(context.PANES[self.key]["effort_id"], 636)
        self.assertEqual(context.PANES[self.key]["task_id"], 42)

    def test_pending_work_cannot_restore_manual_unlink(self):
        event = self.event(refs={"effort_id": 637, "task_id": 43})
        context.ingest(event, os.getuid())
        self.assertIn("pending_binding", context.PANES[self.key])
        context._bind(context.PANES[self.key], None, None)
        self.assertNotIn("pending_binding", context.PANES[self.key])

    def test_read_link_resolution_never_picks_current_task(self):
        self.bind(); context.PANES[self.key]["task_id"] = None
        context.ingest(self.event(path="/api/link/resolve"), os.getuid())
        self.assertIsNone(context.PANES[self.key]["task_id"])

    def test_launcher_prevents_initial_event_binding(self):
        context.PANES[self.key]["launch_effort"] = 637
        context.ingest(self.event(), os.getuid())
        self.assertIsNone(context.PANES[self.key]["effort_id"])

    def test_cross_project_work_is_only_suggested(self):
        context.EFFORTS[636]["effort"]["project_id"] = 20
        context.ingest(self.event(), os.getuid())
        self.assertIsNone(context.PANES[self.key]["effort_id"])

    def test_invalid_successful_note_response_is_uncertain(self):
        self.bind(); payload = self.payload(task_id=42, expected_status="doing", note="Progress")
        with patch.object(context.client, "request", side_effect=[(200, self.task), (201, {})]):
            response = self.browser.post('/studio/task-note', json=payload)
        self.assertTrue(response.get_json()["uncertain"])

    def test_codex_boundary_and_wrapped_reference_are_suggestions(self):
        self.assertEqual(context.screen_suggestions('• Ran sto handoff show\n  --effort 636\n  └ context'), [636])
        self.assertEqual(context.screen_suggestions('exec_command: sto effort get 637'), [637])

    def test_owner_only_bounded_receiver(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close); self.addCleanup(right.close)
        event = self.event(installation=studio_events.INSTALLATION)
        right.sendall(json.dumps(event).encode()); right.shutdown(socket.SHUT_WR)
        studio_events.receive(left)
        stored, uid = studio_events.EVENTS.get_nowait(); studio_events.EVENTS.task_done()
        self.assertEqual(stored["event_id"], event["event_id"]); self.assertEqual(uid, os.getuid())
        left2, right2 = socket.socketpair(); self.addCleanup(left2.close); self.addCleanup(right2.close)
        right2.sendall(b"x" * 8193); right2.shutdown(socket.SHUT_WR)
        studio_events.receive(left2); self.assertTrue(studio_events.EVENTS.empty())


@unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
class StudioPaneLifetimeTests(unittest.TestCase):
    def test_two_panes_rename_and_recreate_without_browser(self):
        from tests.test_poll_perf import IsolatedTmux
        tmux = IsolatedTmux(self)
        for name in ("PANES", "TARGETS", "EFFORTS", "ACCOUNT", "_seen", "_sequences"):
            p = patch.object(context, name, {}); p.start(); self.addCleanup(p.stop)
        context.connection_generation()
        tmux.tmux('new-session', '-d', '-s', 'observer-one', 'sleep 600')
        tmux.tmux('new-session', '-d', '-s', 'observer-two', 'sleep 600')
        context.scan_once()
        self.assertEqual(len(context.PANES), 2)
        key = context.TARGETS['observer-one:0.0']
        context._bind(context.PANES[key], 636, 'manual', 42)
        tmux.tmux('rename-session', '-t', 'observer-one', 'renamed')
        context.scan_once()
        self.assertEqual(context.TARGETS['renamed:0.0'], key)
        self.assertEqual(context.PANES[key]['task_id'], 42)
        tmux.tmux('kill-session', '-t', 'renamed')
        tmux.tmux('new-session', '-d', '-s', 'renamed', 'sleep 600')
        context.scan_once()
        replacement = context.TARGETS['renamed:0.0']
        self.assertNotEqual(key, replacement)
        self.assertIsNone(context.PANES[replacement]['effort_id'])
        self.assertNotIn(key, context.PANES)
