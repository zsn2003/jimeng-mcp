# Fixtures use synthetic identities and material; no user account data.
"""Hermetic bridge tests. Test doubles never contact or impersonate Jimeng."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import hashlib
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
from server import JobService, load_secret, make_server
from server import APIError
from outcome_normalizer import normalize


SECRET = "test-only-secret-" + "x" * 32


class RecordingAdapter:
    """Only a synchronization/contract test double, not a production backend."""
    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.error = None
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.result = {"upstreamState": "TEST_ONLY"}

    def validate(self, payload):
        if set(payload) != {"prompt"} or not isinstance(payload["prompt"], str):
            raise ValueError("Invalid test payload")

    def capabilities(self):
        return {"testDouble": True}

    def recovery(self, payload, job_id):
        return {"submitId": "test-only-" + job_id}

    def execute(self, payload, job_id):
        with self.lock:
            self.calls.append((payload, job_id))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if not self.release.wait(5):
                raise RuntimeError("Test did not release execution")
            if self.error:
                raise self.error
            return self.result
        finally:
            with self.lock:
                self.active -= 1


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "jobs.sqlite3"
        self.adapter = RecordingAdapter()
        self.service = JobService(self.db, self.adapter, SECRET)
        self.httpd = make_server(self.service, port=0)
        self.http_thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.http_thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.http_thread.join(2)
        self.adapter.release.set()
        self.assertTrue(self.service.close(timeout=5))
        self.temp.cleanup()

    def request(self, method, path, payload=None, key=None, token=SECRET, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=5)
        headers = {}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if key is not None:
            headers["Idempotency-Key"] = key
        body = raw if raw is not None else json.dumps(payload) if payload is not None else None
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    def wait_status(self, job_id, statuses, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.service.get_job(job_id)
            if job["status"] in statuses:
                return job
            time.sleep(0.01)
        self.fail(f"Job {job_id} did not reach {statuses}; last state was {job}")

    def test_auth_covers_all_api_routes_and_health_leaks_no_details(self):
        self.assertEqual(self.request("GET", "/healthz", token=None)[:2], (200, {"status": "ok"}))
        for path in ("/v1/capabilities", "/v1/jobs/" + "0" * 32):
            for token in (None, "wrong-secret"):
                self.assertEqual(self.request("GET", path, token=token)[0], 401)
        self.assertEqual(self.request("POST", "/v1/jobs", {"prompt": "a"}, "k", token=None)[0], 401)
        status, data, _ = self.request("GET", "/v1/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(data["service"]["completionMeaning"], "adapter_call_completed")
        self.assertEqual(self.adapter.calls, [])

    def test_missing_key_and_invalid_schema_never_persist_work(self):
        self.assertEqual(self.request("POST", "/v1/jobs", {"prompt": "a"})[0], 400)
        self.assertEqual(self.request("POST", "/v1/jobs", {"shell": "echo unsafe"}, "k")[0], 422)
        self.assertEqual(self.request("POST", "/v1/jobs", {"url": "https://example.invalid"}, "k")[0], 422)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_concurrent_identical_requests_persist_and_execute_exactly_once(self):
        self.service.start_worker()
        barrier = threading.Barrier(12)
        def send(_):
            barrier.wait()
            return self.request("POST", "/v1/jobs", {"prompt": "并发测试"}, "same-request")
        with ThreadPoolExecutor(max_workers=12) as executor:
            replies = list(executor.map(send, range(12)))
        self.assertEqual(sum(status == 202 for status, _, _ in replies), 1)
        self.assertTrue(all(status in (200, 202) for status, _, _ in replies))
        ids = {body["id"] for _, body, _ in replies}
        self.assertEqual(len(ids), 1)
        job_id = ids.pop()
        self.wait_status(job_id, {"COMPLETED"})
        self.assertEqual(self.adapter.calls, [({"prompt": "并发测试"}, job_id)])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_same_key_different_body_is_conflict_and_original_stays_intact(self):
        status, original, _ = self.request("POST", "/v1/jobs", {"prompt": "original"}, "same")
        self.assertEqual(status, 202)
        self.assertEqual(self.request("POST", "/v1/jobs", {"prompt": "changed"}, "same")[0], 409)
        # Even a changed body invalid under today's adapter must remain a conflict.
        self.assertEqual(self.request("POST", "/v1/jobs", {"other": "changed"}, "same")[0], 409)
        self.service.start_worker()
        self.wait_status(original["id"], {"COMPLETED"})
        self.assertEqual(self.adapter.calls[0][0], {"prompt": "original"})

    def test_persistence_precedes_execution_and_worker_is_sequential(self):
        self.adapter.release.clear()
        jobs = [self.service.submit({"prompt": str(i)}, f"request-{i}")[0] for i in range(2)]
        self.assertEqual(self.adapter.calls, [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM jobs WHERE status='QUEUED'").fetchone()[0], 2)
        self.service.start_worker()
        self.assertTrue(self.adapter.started.wait(2))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.service.get_job(jobs[1]["id"])["status"], "QUEUED")
        self.adapter.release.set()
        for job in jobs:
            self.wait_status(job["id"], {"COMPLETED"})
        self.assertEqual(self.adapter.max_active, 1)
        self.assertEqual([job_id for _, job_id in self.adapter.calls], [job["id"] for job in jobs])

    def test_recovery_marks_running_unknown_and_never_resubmits_it(self):
        running = self.service.submit({"prompt": "interrupted"}, "uncertain")[0]
        queued = self.service.submit({"prompt": "never-started"}, "queued")[0]
        # A separate committed SQLite transaction models the on-disk state at a
        # process crash, without doing any external work or a fake completion.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE jobs SET status='RUNNING' WHERE id=?", (running["id"],))
        self.assertTrue(self.service.close())
        self.service = JobService(self.db, self.adapter, SECRET)
        self.service.start_worker()
        recovered = self.service.get_job(running["id"])
        self.assertEqual(recovered["status"], "UNKNOWN")
        self.assertEqual(recovered["error"]["code"], "PROCESS_INTERRUPTED")
        self.assertEqual(recovered["recovery"], {"submitId": "test-only-" + running["id"]})
        self.wait_status(queued["id"], {"COMPLETED"})
        replay, created = self.service.submit({"prompt": "interrupted"}, "uncertain")
        self.assertFalse(created)
        self.assertEqual(replay["status"], "UNKNOWN")
        self.assertEqual(replay["recovery"], recovered["recovery"])
        self.assertEqual([job_id for _, job_id in self.adapter.calls], [queued["id"]])

    def test_exception_is_unknown_and_sensitive_diagnostics_are_not_exposed(self):
        self.adapter.error = RuntimeError("sensitive credential must never leak")
        job = self.service.submit({"prompt": "a"}, "exception")[0]
        self.service.start_worker()
        result = self.wait_status(job["id"], {"UNKNOWN"})
        self.assertNotIn("sensitive", json.dumps(result))
        replay, created = self.service.submit({"prompt": "a"}, "exception")
        self.assertFalse(created)
        self.assertEqual(replay["status"], "UNKNOWN")
        self.assertEqual(len(self.adapter.calls), 1)

    def test_timeout_does_not_overlap_work_or_allow_late_success_to_hide_uncertainty(self):
        self.service.execution_timeout = 0.05
        self.adapter.release.clear()
        first = self.service.submit({"prompt": "first"}, "first")[0]
        second = self.service.submit({"prompt": "second"}, "second")[0]
        self.service.start_worker()
        self.assertTrue(self.adapter.started.wait(2))
        result = self.wait_status(first["id"], {"UNKNOWN"})
        self.assertEqual(result["error"]["code"], "EXECUTION_TIMEOUT")
        self.assertEqual(self.service.get_job(second["id"])["status"], "QUEUED")
        self.assertEqual(len(self.adapter.calls), 1)
        self.adapter.release.set()
        self.wait_status(second["id"], {"COMPLETED"})
        self.assertEqual(self.service.get_job(first["id"])["status"], "UNKNOWN")
        self.assertEqual(self.adapter.max_active, 1)

    def test_invalid_result_after_execution_is_unknown_not_input_failure(self):
        self.adapter.result = {"bad": float("nan")}
        job = self.service.submit({"prompt": "a"}, "bad-result")[0]
        self.service.start_worker()
        result = self.wait_status(job["id"], {"UNKNOWN"})
        self.assertEqual(result["error"]["code"], "INVALID_ADAPTER_RESULT")

    def test_execution_value_error_is_explicit_input_rejection(self):
        self.adapter.error = ValueError("Known input rejection before submission")
        job = self.service.submit({"prompt": "a"}, "rejected")[0]
        self.service.start_worker()
        result = self.wait_status(job["id"], {"FAILED"})
        self.assertEqual(result["error"]["code"], "INPUT_REJECTED")

    def test_second_service_cannot_recover_or_execute_an_active_database(self):
        with self.assertRaisesRegex(RuntimeError, "active bridge process"):
            JobService(self.db, RecordingAdapter(), SECRET)

    def test_stopping_active_execution_retains_process_lock(self):
        self.adapter.release.clear()
        self.service.submit({"prompt": "active"}, "active")
        self.service.start_worker()
        self.assertTrue(self.adapter.started.wait(2))
        self.assertFalse(self.service.close(timeout=0.01))
        with self.assertRaisesRegex(RuntimeError, "active bridge process"):
            JobService(self.db, RecordingAdapter(), SECRET)
        self.adapter.release.set()
        self.assertTrue(self.service.close(timeout=2))

    def test_json_whitespace_is_not_a_new_idempotency_payload(self):
        first = self.request("POST", "/v1/jobs", key="canonical", raw='{"prompt":"a"}')
        second = self.request("POST", "/v1/jobs", key="canonical", raw='{ "prompt" : "a" }')
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1]["id"], second[1]["id"])

    def test_broken_optional_recovery_does_not_hide_durable_status(self):
        self.adapter.recovery = mock.Mock(side_effect=RuntimeError("private diagnostic"))
        job = self.service.submit({"prompt": "a"}, "recovery-failed")[0]
        self.assertIsNone(job["recovery"])
        result = self.service.get_job(job["id"])
        self.assertEqual(result["status"], "QUEUED")
        self.assertIsNone(result["recovery"])

    def test_invalid_json_duplicate_keys_and_nonfinite_values_are_rejected(self):
        for body in ('{"prompt":"a","prompt":"b"}', '{"prompt":NaN}', '[]', '{broken'):
            status, _, _ = self.request("POST", "/v1/jobs", key="invalid-json", raw=body)
            self.assertEqual(status, 400)

    def test_job_list_keyset_filters_summary_and_privacy(self):
        self.adapter.validate = lambda payload: None
        with mock.patch("server.time.time", return_value=1000.0):
            jobs = [self.service.submit({"action": "node.read", "projectId": "project1", "prompt": "private-value"}, f"page-{i}")[0]
                    for i in range(5)]
        cursor, seen = None, []
        while True:
            page = self.service.list_jobs(limit=2, cursor=cursor, action="node.read", projectId="project1")
            seen.extend(x["id"] for x in page["items"])
            self.assertNotIn("private-value", json.dumps(page))
            self.assertNotIn("result", json.dumps(page))
            if not page["hasMore"]:
                break
            cursor = page["nextCursor"]
        self.assertEqual(set(seen), {x["id"] for x in jobs})
        self.assertEqual(len(seen), 5)
        with self.assertRaises(APIError):
            self.service.list_jobs(cursor=cursor, action="other")
        self.assertEqual(self.request("GET", "/v1/jobs?limit=0")[0], 400)
        self.assertEqual(self.request("GET", "/v1/jobs?limit=1&limit=2")[0], 400)
        self.assertEqual(self.request("GET", "/v1/jobs", token=None)[0], 401)
        summary = self.request("GET", "/v1/queue")[1]
        self.assertEqual(summary["counts"], [{"status": "QUEUED", "lane": "read", "count": 5}])

    def test_queued_cancellation_is_atomic_idempotent_and_never_executes(self):
        job = self.service.submit({"prompt": "cancel this"}, "cancel-original")[0]
        route = "/v1/jobs/" + job["id"] + "/cancel"
        self.assertEqual(self.request("POST", route, {}, token=None)[0], 401)
        first = self.request("POST", route, {})
        self.assertEqual(first[0], 200)
        self.assertTrue(first[1]["cancelled"])
        self.assertEqual(first[1]["job"]["status"], "FAILED")
        self.assertEqual(first[1]["job"]["operationOutcome"], "rejected")
        self.assertFalse(first[1]["job"]["dispatchAttempted"])
        self.assertTrue(self.request("POST", route, {})[1]["alreadyCancelled"])
        self.service.start_worker()
        self.assertEqual(self.service.submit({"prompt": "cancel this"}, "cancel-original")[0]["id"], job["id"])
        self.assertEqual(self.adapter.calls, [])

    def test_cancel_and_claim_have_exactly_one_winner(self):
        for index in range(12):
            job = self.service.submit({"prompt": "race"}, "cancel-race-" + str(index))[0]
            barrier = threading.Barrier(2)
            def claim():
                barrier.wait()
                return self.service._claim_next()
            def cancel():
                barrier.wait()
                try:
                    return self.service.cancel_job(job["id"])["cancelled"]
                except APIError as exc:
                    self.assertEqual(exc.code, "JOB_ALREADY_STARTED")
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                a, b = pool.submit(claim), pool.submit(cancel)
                claimed, cancelled = a.result(), b.result()
            self.assertNotEqual(claimed is not None, cancelled)
            if claimed:
                self.assertEqual(claimed[0], job["id"])
                self.service._finish(job["id"], "FAILED", error={"code": "TEST_DONE", "message": "No actual call"})
        self.assertEqual(self.adapter.calls, [])

    def test_fixed_read_lane_finishes_while_single_writer_remains_blocked(self):
        self.adapter.validate = lambda payload: None
        write_started = threading.Event()
        def execute(payload, job_id):
            self.adapter.calls.append((payload, job_id))
            if payload["action"] == "text.update":
                write_started.set()
                self.adapter.release.wait(3)
            return {"action": payload["action"], "upstream": {"ok": True, "data": {}}}
        self.adapter.execute = execute
        self.adapter.release.clear()
        first = self.service.submit({"action": "text.update"}, "write-first")[0]
        second = self.service.submit({"action": "text.update"}, "write-second")[0]
        read = self.service.submit({"action": "node.read"}, "read-now")[0]
        self.service.start_worker()
        self.assertTrue(write_started.wait(1))
        self.assertEqual(self.wait_status(read["id"], {"COMPLETED"})["operationOutcome"], "succeeded")
        self.assertEqual(self.service.get_job(first["id"])["status"], "RUNNING")
        self.assertEqual(self.service.get_job(second["id"])["status"], "QUEUED")
        self.adapter.release.set()
        self.wait_status(second["id"], {"COMPLETED"})

    def test_read_worker_concurrency_is_bounded(self):
        self.adapter.validate = lambda payload: None
        self.adapter.release.clear()
        jobs = [self.service.submit({"action": "node.read"}, f"read-limited-{i}")[0] for i in range(5)]
        self.service.start_worker()
        deadline = time.monotonic() + 1
        while self.adapter.active < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.adapter.active, 2)
        self.assertEqual(self.adapter.max_active, 2)
        self.adapter.release.set()
        for job in jobs:
            self.wait_status(job["id"], {"COMPLETED"})
        self.assertLessEqual(self.adapter.max_active, 2)

    def test_structured_validation_details_are_bounded_and_plain_errors_hidden(self):
        class ValidationError(ValueError):
            details = [{"field": "duration", "code": "REQUIRED", "message": "duration is required"},
                       {"field": "x", "code": "BAD", "message": "hidden", "value": "secret"}]
        self.adapter.validate = mock.Mock(side_effect=ValidationError("private exception"))
        status, body, _ = self.request("POST", "/v1/jobs", {"prompt": "a"}, "validation-detail")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["details"], ValidationError.details[:1])
        self.assertNotIn("private exception", json.dumps(body))
        self.assertNotIn("secret", json.dumps(body))
        self.adapter.validate = mock.Mock(side_effect=ValueError("private exception"))
        self.assertNotIn("details", self.request("POST", "/v1/jobs", {}, "plain-error")[1]["error"])

    def test_project_policy_validation_details_reach_http_without_persisting(self):
        from project_policy import enforce
        payload = {"action": "video.generate", "projectId": "test-project", "dryRun": False}
        self.adapter.validate = lambda body: enforce(body,
            {"workflowStage": "ordinary", "writes": True, "videoGeneration": True})
        with mock.patch.dict(os.environ, {"JIMENG_VIDEO_WORKFLOW_POLICY": "sample_first", "JIMENG_READ_ONLY": "0"}, clear=True):
            status, body, _ = self.request("POST", "/v1/jobs", payload, "policy-rejection")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["details"][0]["code"], "ORDINARY_APPROVAL_REQUIRED")
        self.assertEqual(body["error"]["details"][0]["field"], "workflow.ordinaryApproval")
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def binary_request(self, path, *, token=SECRET, range_header=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)) as connection:
            headers = {"Authorization": "Bearer " + token} if token else {}
            if range_header is not None:
                headers["Range"] = range_header
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())

    def test_binary_artifact_auth_range_integrity_and_no_job_storage(self):
        content = bytes(range(256)) * 1025
        file = Path(self.temp.name) / "artifact.mp4"
        file.write_bytes(content)
        project, artifact = "10000000-0000-0000-0000-000000000001", "20000000-0000-0000-0000-000000000002"
        def descriptor(payload):
            if payload["projectId"] != project:
                raise ValueError("private managed path")
            return {"path": str(file), "byteSize": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                    "filename": "验收视频.mp4", "format": "mp4"}
        self.adapter.artifact_descriptor = descriptor
        path = f"/v1/artifacts/{artifact}?projectId={project}&kind=resource"
        self.assertEqual(self.binary_request(path, token=None)[0], 401)
        status, raw, headers = self.binary_request(path)
        self.assertEqual((status, raw), (200, content))
        self.assertEqual(headers["Content-Length"], str(len(content)))
        self.assertEqual(headers["ETag"], '"' + hashlib.sha256(content).hexdigest() + '"')
        for span, expected in (("bytes=0-0", content[:1]), ("bytes=100-", content[100:]), ("bytes=-37", content[-37:]),
                               ("bytes=255-300", content[255:301])):
            response = self.binary_request(path, range_header=span)
            self.assertEqual((response[0], response[1]), (206, expected))
            self.assertIn("Content-Range", response[2])
        for span in ("bytes=2-1", "bytes=999999-", "bytes=-0", "bytes=0-1,3-4", "invalid"):
            response = self.binary_request(path, range_header=span)
            self.assertEqual(response[0], 416)
            self.assertEqual(response[2]["Content-Range"], f"bytes */{len(content)}")
        manifest = self.request("GET", f"/v1/artifacts/{artifact}/manifest?projectId={project}")[1]
        self.assertNotIn(str(file), json.dumps(manifest))
        self.assertNotIn("path", manifest)
        self.assertTrue(manifest["requiresBearerAuth"])
        self.assertEqual(self.binary_request(path + "&path=/private")[0], 400)
        self.assertEqual(self.binary_request(path.replace(project, artifact))[0], 404)
        file.write_bytes(b"x" * len(content))
        self.assertEqual(self.binary_request(path)[0], 404)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_existing_schema_migrates_without_changing_jobs_or_idempotency(self):
        legacy = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE,body_hash TEXT,payload_json TEXT,status TEXT,created_at REAL,updated_at REAL,result_json TEXT,error_json TEXT)")
            payload = '{"action":"node.read"}'
            connection.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", ("1" * 32, "legacy-key", hashlib.sha256(payload.encode()).hexdigest(), payload, "QUEUED", 1, 1, None, None))
        migrated = JobService(legacy, self.adapter, SECRET)
        try:
            job = migrated.get_job("1" * 32)
            self.assertEqual(job["action"], "node.read")
            self.assertEqual(migrated.list_jobs()["items"][0]["lane"], "read")
            self.assertEqual(migrated.submit({"action": "node.read"}, "legacy-key")[0]["id"], "1" * 32)
        finally:
            migrated.close()


class OutcomeTests(unittest.TestCase):
    def test_completion_does_not_turn_unknown_or_pending_receipts_into_success(self):
        self.assertEqual(normalize("COMPLETED", {"arbitrary": "success"})["operationOutcome"], "unknown")
        value = normalize("COMPLETED", {"upstream": {"ok": True, "data": {"submission": {"state": "accepted"}}}}, payload={"action": "video.sample"})
        self.assertEqual(value["operationOutcome"], "accepted")
        self.assertIsNone(value["dispatchAttempted"])

    def test_subject_partial_and_explicit_reconciliation_are_preserved(self):
        failed = {"phase": "element-readback", "createdResources": [{"resourceId": "existing"}], "workspaceMutationAttempted": True,
                  "upstream": {"ok": False, "error": {"code": "SUBJECT_BINDING_READBACK_MISMATCH"}}}
        value = normalize("COMPLETED", failed, payload={"action": "subject.import_element"})
        self.assertEqual(value, {"operationOutcome": "partial", "reconciliationRequired": True, "dispatchAttempted": True})
        explicit = {"operationOutcome": "partial", "upstream": {"ok": True}, "reconciliationRequired": True, "dispatchAttempted": False}
        self.assertEqual(normalize("COMPLETED", explicit)["operationOutcome"], "partial")
        self.assertEqual(normalize("UNKNOWN", {"operationOutcome": "succeeded"})["operationOutcome"], "unknown")

    def test_known_before_dispatch_rejection_and_read_success(self):
        rejected = {"workspaceMutationAttempted": False, "generationSubmitted": False, "upstream": {"ok": False, "error": {"code": "POLICY_REJECTED"}}}
        self.assertEqual(normalize("COMPLETED", rejected), {"operationOutcome": "rejected", "reconciliationRequired": False, "dispatchAttempted": False})
        self.assertEqual(normalize("COMPLETED", {"upstream": {"ok": True}}, payload={"action": "node.read"})["operationOutcome"], "succeeded")


class SecretTests(unittest.TestCase):
    def test_secret_requires_explicit_strong_configuration_and_private_file(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                load_secret()
        with mock.patch.dict(os.environ, {"JIMENG_BRIDGE_SECRET": SECRET}, clear=True):
            self.assertEqual(load_secret(), SECRET)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            path.write_text(SECRET + "\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(load_secret(str(path)), SECRET)
            path.chmod(0o644)
            if os.name == "posix":
                with self.assertRaises(ValueError):
                    load_secret(str(path))


if __name__ == "__main__":
    unittest.main()
