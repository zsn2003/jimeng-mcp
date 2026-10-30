# Fixtures use synthetic identities and material; no user account data.
"""Real local HTTP MCP/bridge round trips; no official CLI or external network.

The two listening servers, HTTP client, JSON-RPC envelopes and SQLite jobs are
real. Only the final Jimeng adapter execution is a labelled test double.
"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = (Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")
sys.path.insert(0, str(ROOT))
import adapter
import mcp_server as mcp
from server import JobService, make_server as make_backend_server


MCP_TOKEN = "mcp-test-only-" + "m" * 40
BACKEND_TOKEN = "backend-test-only-" + "b" * 40
PROJECT = "1000000a-0000-4000-8000-000000000001"


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    def validate(self, payload):
        adapter.validate(payload)

    def capabilities(self):
        return {"testDouble": True, "readOnly": True, "liveCreditCeiling": 0}

    def studio_catalog(self):
        return {"testDouble": True, "implemented": False, "kind": "planning_only"}

    def execute(self, payload, job_id):
        self.calls.append((payload, job_id))
        return {"testDouble": True, "upstream": {"ok": True,
            "data": {"submission": {"state": "accepted", "resources": []}}}}


class MCPHTTPTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {"JIMENG_READ_ONLY": "1", "JIMENG_MAX_CREDITS": "0"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "jobs.sqlite3"
        self.adapter = RecordingAdapter()
        self.jobs = JobService(self.db, self.adapter, BACKEND_TOKEN)
        self.addCleanup(self.jobs.close)
        self.backend_http = make_backend_server(self.jobs, port=0)
        self.backend_thread = threading.Thread(target=self.backend_http.serve_forever,
                                                kwargs={"poll_interval": 0.02}, daemon=True)
        self.backend_thread.start()
        self.addCleanup(self.stop_http, self.backend_http, self.backend_thread)
        self.client = mcp.BackendClient(BACKEND_TOKEN, self.backend_http.server_port)
        self.service = mcp.MCPService(self.client, MCP_TOKEN,
            mcp.load_payload_schema(ROOT / "openapi.json"), {"https://agent.example.test"})
        self.http = mcp.make_server(self.service, port=0)
        self.thread = threading.Thread(target=self.http.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_http, self.http, self.thread)

    @staticmethod
    def stop_http(server, thread):
        server.shutdown()
        server.server_close()
        thread.join(2)

    def request(self, message=None, *, method="POST", path="/mcp", token=MCP_TOKEN,
                headers=None, raw=None, duplicate_headers=()):
        request_headers = {"Accept": "application/json, text/event-stream",
                           "Content-Type": "application/json",
                           "MCP-Protocol-Version": mcp.PROTOCOL_VERSION}
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        for name, value in (headers or {}).items():
            if value is None:
                request_headers.pop(name, None)
            else:
                request_headers[name] = value
        body = raw if raw is not None else mcp.canonical_json(message).encode("utf-8")
        if isinstance(body, str):
            body = body.encode("utf-8")
        with closing(http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)) as connection:
            if duplicate_headers:
                connection.putrequest(method, path)
                for name, value in request_headers.items():
                    connection.putheader(name, value)
                for name, value in duplicate_headers:
                    connection.putheader(name, value)
                connection.putheader("Content-Length", str(len(body)))
                connection.endheaders(body)
            else:
                connection.request(method, path, body, request_headers)
            response = connection.getresponse()
            response_body = response.read()
            result = json.loads(response_body) if response_body else None
            return response.status, result, dict(response.getheaders())

    def rpc(self, method, params=None, request_id=1, **kwargs):
        return self.request({"jsonrpc": "2.0", "id": request_id, "method": method,
                             **({"params": params} if params is not None else {})}, **kwargs)

    def tool(self, name, arguments=None, request_id=1, **kwargs):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, request_id, **kwargs)

    def submit(self, key="logical-request", payload=None, request_id=1):
        return self.tool("submit_job", {"idempotencyKey": key,
            "payload": payload or {"action": "account.status"}}, request_id)

    def job_count(self):
        with closing(sqlite3.connect(self.db)) as connection:
            return connection.execute("SELECT count(*) FROM jobs").fetchone()[0]

    def test_complete_stateless_protocol_handshake_and_tool_discovery(self):
        status, initialized, headers = self.rpc("initialize", {
            "protocolVersion": mcp.PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "local-protocol-test", "version": "1.0"}}, request_id="init")
        self.assertEqual(status, 200)
        self.assertEqual(initialized["id"], "init")
        self.assertEqual(initialized["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)
        self.assertEqual(initialized["result"]["capabilities"], {"tools": {"listChanged": False}})
        self.assertNotIn("Mcp-Session-Id", headers)
        status, response, headers = self.request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, response, headers["Content-Length"]), (202, None, "0"))
        self.assertEqual(self.rpc("ping")[1]["result"], {})
        status, response, _ = self.rpc("tools/list")
        self.assertEqual(status, 200)
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        self.assertEqual(set(tools), {"capabilities", "studio_catalog", "action_schema", "submit_job", "submit_job_json", "get_job",
                                     "list_jobs", "queue_summary", "cancel_job", "artifact_manifest"})
        schema = tools["submit_job"]["inputSchema"]
        self.assertEqual(set(schema["required"]), {"payload", "idempotencyKey"})
        expected = mcp.load_payload_schema(ROOT / "openapi.json")
        self.assertEqual(self.service.payload_schema, expected)
        self.assertNotIn("oneOf", schema["properties"]["payload"])
        self.assertLess(len(json.dumps(tools)), 10000)
        self.assertFalse(tools["submit_job"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["submit_job"]["annotations"]["idempotentHint"])
        self.assertEqual(self.adapter.calls, [])

    def test_capability_and_catalog_calls_reach_real_backend_http(self):
        for tool, key, expected in (("capabilities", "backend", {"testDouble": True, "readOnly": True, "liveCreditCeiling": 0}),
                                    ("studio_catalog", "kind", "planning_only")):
            status, response, _ = self.tool(tool)
            self.assertEqual(status, 200)
            result = response["result"]
            self.assertFalse(result["isError"])
            structured = result["structuredContent"]
            self.assertEqual(structured["bridgeHttpStatus"], 200)
            self.assertEqual(structured["data"][key], expected)
            self.assertEqual(json.loads(result["content"][0]["text"]), structured)
        self.assertEqual(self.job_count(), 0)

    def test_mcp_and_backend_tokens_are_distinct_and_authorization_is_required(self):
        for token in (None, "wrong", BACKEND_TOKEN):
            status, response, headers = self.rpc("tools/list", token=token)
            self.assertEqual(status, 401)
            self.assertIn("WWW-Authenticate", headers)
            self.assertNotIn(BACKEND_TOKEN, json.dumps(response))
        status, _, _ = self.rpc("tools/list", duplicate_headers=(("Authorization", "Bearer " + MCP_TOKEN),))
        self.assertEqual(status, 401)
        with self.assertRaisesRegex(ValueError, "different"):
            mcp.MCPService(self.client, BACKEND_TOKEN, mcp.load_payload_schema(ROOT / "openapi.json"))

    def test_origin_validation_is_exact_and_optional_for_server_clients(self):
        for origin in ("https://attacker.example", "https://agent.example.test.attacker.example", "null"):
            self.assertEqual(self.rpc("ping", headers={"Origin": origin})[0], 403)
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://agent.example.test"})[0], 200)
        self.assertEqual(self.rpc("ping")[0], 200)
        self.assertEqual(self.rpc("ping", headers={"Origin": "https://agent.example.test"},
            duplicate_headers=(("Origin", "https://agent.example.test"),))[0], 403)

    def test_mcp_endpoint_get_and_delete_explicitly_decline_sse_and_sessions(self):
        for method in ("GET", "DELETE"):
            status, _, headers = self.request(method=method)
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "POST")
        self.assertEqual(self.request(method="GET", path="/healthz", token=None)[0], 200)
        self.assertEqual(self.rpc("ping", path="/mcp?token=ignored")[0], 404)

    def test_version_negotiation_and_legacy_json_transport(self):
        for requested, expected in (("2025-03-26", "2025-03-26"), ("future-version", mcp.PROTOCOL_VERSION)):
            status, response, _ = self.rpc("initialize", {"protocolVersion": requested,
                "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
            self.assertEqual(status, 200)
            self.assertEqual(response["result"]["protocolVersion"], expected)
        self.assertEqual(self.rpc("ping", headers={"MCP-Protocol-Version": "future-version"})[0], 400)
        status, response, _ = self.tool("capabilities", headers={"MCP-Protocol-Version": None})
        self.assertEqual(status, 200)
        self.assertNotIn("structuredContent", response["result"])
        self.assertTrue(json.loads(response["result"]["content"][0]["text"])["data"]["backend"]["testDouble"])
        batch = [{"jsonrpc": "2.0", "id": "ping", "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        status, response, _ = self.request(batch, headers={"MCP-Protocol-Version": "2025-03-26"})
        self.assertEqual(status, 200)
        self.assertEqual(response, [{"jsonrpc": "2.0", "id": "ping", "result": {}}])
        self.assertEqual(self.request(batch)[0], 400)

    def test_tool_protocol_errors_are_distinct_from_execution_errors(self):
        self.assertEqual(self.rpc("unknown/method")[1]["error"]["code"], -32601)
        self.assertEqual(self.tool("unknown_tool")[1]["error"]["code"], -32602)
        self.assertEqual(self.rpc("initialize", {})[1]["error"]["code"], -32602)
        self.assertEqual(self.rpc("tools/list", {"cursor": "unknown"})[1]["error"]["code"], -32602)
        status, response, _ = self.tool("get_job", {"jobId": "a" * 32})
        self.assertEqual(status, 200)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["structuredContent"]["bridgeHttpStatus"], 404)

    def test_requests_cannot_supply_urls_commands_or_invalid_operations(self):
        inputs = [
            ("capabilities", {"url": "https://example.invalid"}),
            ("get_job", {"jobId": "../../healthz"}),
            ("submit_job", {"idempotencyKey": "x", "payload": {"action": "shell", "command": "id"}}),
            ("submit_job", {"idempotencyKey": "x", "payload": {"action": "account.status", "url": "http://other"}}),
            ("submit_job", {"payload": {"action": "account.status"}}),
            ("submit_job", {"idempotencyKey": "x\r\nAuthorization: bad", "payload": {"action": "account.status"}}),
            ("submit_job_json", {"idempotencyKey": "x", "payloadJson": '{"action":"shell"}'}),
            ("submit_job_json", {"idempotencyKey": "x", "payloadJson": '{"action":"account.status","url":"http://other"}'}),
            ("submit_job_json", {"idempotencyKey": "x", "payloadJson": '{"action":"account.status","action":"canvas.list"}'}),
        ]
        for name, arguments in inputs:
            status, response, _ = self.tool(name, arguments)
            self.assertEqual(status, 200)
            self.assertTrue(response["result"]["isError"])
            self.assertEqual(response["result"]["structuredContent"]["error"]["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.job_count(), 0)
        with self.assertRaises(ValueError):
            self.client.request("GET", "http://example.invalid/")

    def test_compact_json_entrypoint_keeps_idempotency_and_exact_action_schema(self):
        first = self.submit(key="compact-json-same-key")[1]["result"]["structuredContent"]
        result = self.tool("submit_job_json", {"idempotencyKey": "compact-json-same-key",
            "payloadJson": '{"action":"account.status"}'})[1]["result"]["structuredContent"]
        self.assertEqual(first["data"]["id"], result["data"]["id"])
        self.assertTrue(result["idempotentReplayed"])
        schema = self.tool("action_schema", {"action": "video.sample"})[1]["result"]
        self.assertFalse(schema["isError"])
        self.assertFalse(schema["structuredContent"]["schema"]["additionalProperties"])
        self.assertTrue(self.tool("action_schema", {"action": "shell"})[1]["result"]["isError"])

    def test_original_idempotency_key_survives_mcp_ids_and_preserves_conflicts(self):
        first = self.submit(key="original-KEY:1", request_id="transport-1")[1]["result"]["structuredContent"]
        second = self.submit(key="original-KEY:1", request_id="transport-2")[1]["result"]["structuredContent"]
        self.assertEqual(first["data"]["id"], second["data"]["id"])
        self.assertEqual((first["bridgeHttpStatus"], second["bridgeHttpStatus"]), (202, 200))
        self.assertEqual((first["idempotentReplayed"], second["idempotentReplayed"]), (False, True))
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT idempotency_key FROM jobs").fetchone()[0], "original-KEY:1")
        conflict = self.submit(key="original-KEY:1", payload={"action": "model.list", "type": "video"})[1]["result"]
        self.assertTrue(conflict["isError"])
        self.assertEqual(conflict["structuredContent"]["bridgeHttpStatus"], 409)
        self.assertEqual(conflict["structuredContent"]["data"]["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.job_count(), 1)

    def test_concurrent_http_retries_create_exactly_one_durable_job(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda n: self.submit("same-operation", request_id=n), range(6)))
        jobs = [r[1]["result"]["structuredContent"] for r in results]
        self.assertEqual(len({r["data"]["id"] for r in jobs}), 1)
        self.assertEqual(sum(r["bridgeHttpStatus"] == 202 for r in jobs), 1)
        self.assertEqual(self.job_count(), 1)

    def test_job_status_preserves_upstream_pending_without_claiming_media_success(self):
        created = self.submit()[1]["result"]["structuredContent"]["data"]
        self.jobs.start_worker()
        for _ in range(100):
            result = self.tool("get_job", {"jobId": created["id"]})[1]["result"]["structuredContent"]["data"]
            if result["status"] == "COMPLETED":
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["result"]["upstream"]["data"]["submission"]["state"], "accepted")
        self.assertNotIn("mediaCompleted", json.dumps(result))
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(result["operationOutcome"], "accepted")

    def test_business_partial_is_error_but_preserves_original_recovery(self):
        self.adapter.execute = lambda p, j: {"operationOutcome": "partial", "reconciliationRequired": True,
            "dispatchAttempted": True, "recovery": {"nodeId": "node_original", "doNotResubmit": True},
            "upstream": {"ok": False, "error": {"code": "SUBJECT_BINDING_READBACK_MISMATCH"}}}
        created = self.submit(key="partial-receipt")[1]["result"]["structuredContent"]["data"]
        self.jobs.start_worker()
        for _ in range(100):
            response = self.tool("get_job", {"jobId": created["id"]})[1]["result"]
            if response["structuredContent"]["data"]["status"] == "COMPLETED":
                break
            time.sleep(0.01)
        self.assertTrue(response["isError"])
        job = response["structuredContent"]["data"]
        self.assertEqual(job["status"], "COMPLETED")
        self.assertEqual(job["operationOutcome"], "partial")
        self.assertEqual(job["result"]["recovery"]["nodeId"], "node_original")
        replay = self.submit(key="partial-receipt")[1]["result"]
        self.assertTrue(replay["isError"])
        self.assertTrue(replay["structuredContent"]["idempotentReplayed"])

    def test_queue_tools_are_read_only_except_atomic_pending_cancel(self):
        first = self.submit(key="queue-1")[1]["result"]["structuredContent"]["data"]
        second = self.submit(key="queue-2")[1]["result"]["structuredContent"]["data"]
        page = self.tool("list_jobs", {"limit": 1, "action": "account.status"})[1]["result"]
        self.assertFalse(page["isError"])
        self.assertTrue(page["structuredContent"]["data"]["hasMore"])
        cursor = page["structuredContent"]["data"]["nextCursor"]
        next_page = self.tool("list_jobs", {"limit": 1, "action": "account.status", "cursor": cursor})[1]["result"]
        ids = {page["structuredContent"]["data"]["items"][0]["id"], next_page["structuredContent"]["data"]["items"][0]["id"]}
        self.assertEqual(ids, {first["id"], second["id"]})
        self.assertEqual(self.job_count(), 2)
        summary = self.tool("queue_summary")[1]["result"]["structuredContent"]["data"]
        self.assertEqual(summary["counts"][0]["count"], 2)
        cancelled = self.tool("cancel_job", {"jobId": first["id"]})[1]["result"]
        self.assertFalse(cancelled["isError"])
        self.assertTrue(cancelled["structuredContent"]["data"]["cancelled"])
        self.assertEqual(cancelled["structuredContent"]["data"]["job"]["operationOutcome"], "rejected")
        self.assertEqual(self.job_count(), 2)
        self.assertEqual(self.adapter.calls, [])

    def test_managed_artifact_manifest_and_authenticated_binary_proxy(self):
        data = b"verified-binary-artifact\x00" * 10000
        file = Path(self.directory.name) / "managed.mp4"
        file.write_bytes(data)
        artifact_id = "20000000-0000-0000-0000-000000000002"
        def descriptor(payload):
            if payload["projectId"] != PROJECT or payload["artifactId"] != artifact_id:
                raise ValueError("wrong identity")
            return {"path": str(file), "filename": "verified.mp4", "format": "mp4", "byteSize": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
        self.adapter.artifact_descriptor = descriptor
        self.service.public_path = "/jimeng/mcp"
        tool = self.tool("artifact_manifest", {"projectId": PROJECT, "artifactId": artifact_id})[1]["result"]
        self.assertFalse(tool["isError"])
        manifest = tool["structuredContent"]["data"]
        self.assertTrue(manifest["downloadRoute"].startswith("/jimeng/mcp/artifacts/"))
        self.assertTrue(manifest["requiresBearerAuth"])
        self.assertNotIn(str(file), json.dumps(tool))
        self.assertNotIn(MCP_TOKEN, json.dumps(tool))
        self.assertNotIn(BACKEND_TOKEN, json.dumps(tool))
        route = f"/mcp/artifacts/{artifact_id}?projectId={PROJECT}&kind=resource"
        def download(token=MCP_TOKEN, span=None, path=route):
            headers = {"Authorization": "Bearer " + token} if token else {}
            if span:
                headers["Range"] = span
            with closing(http.client.HTTPConnection("127.0.0.1", self.http.server_port, timeout=3)) as connection:
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                return response.status, response.read(), dict(response.getheaders())
        self.assertEqual(download()[0:2], (200, data))
        status, chunk, headers = download(span="bytes=10-29")
        self.assertEqual((status, chunk), (206, data[10:30]))
        self.assertEqual(headers["Content-Range"], f"bytes 10-29/{len(data)}")
        self.assertEqual(download(span="bytes=999999999-")[0], 416)
        self.assertEqual(download(token=None)[0], 401)
        self.assertEqual(download(token=BACKEND_TOKEN)[0], 401)
        self.assertEqual(download(path=route + "&path=/private")[0], 400)
        self.assertEqual(self.job_count(), 0)
        file.write_bytes(b"x" * len(data))
        self.assertEqual(download()[0], 404)

    def test_backend_policy_rejection_remains_visible_without_queueing(self):
        payload = {"action": "video.sample", "projectId": PROJECT, "model": "seedance_2.5_draft",
                   "prompt": "not generated", "dryRun": False, "creditCeiling": 1}
        response = self.submit(payload=payload)[1]["result"]
        self.assertTrue(response["isError"])
        self.assertEqual(response["structuredContent"]["bridgeHttpStatus"], 422)
        self.assertEqual(self.job_count(), 0)
        self.assertEqual(self.adapter.calls, [])

    def test_malformed_json_duplicate_keys_and_nonfinite_values_never_dispatch(self):
        for body in ('{broken', '{"jsonrpc":"2.0","method":"ping","id":1,"id":2}',
                     '{"jsonrpc":"2.0","method":"ping","id":NaN}', b'\xff'):
            status, response, _ = self.request(raw=body)
            self.assertEqual(status, 400)
            self.assertEqual(response["error"]["code"], -32700)
        for value in (None, 4, True):
            self.assertEqual(self.request(value)[1]["error"]["code"], -32600)
        for request_id in (None, True, 1.5):
            self.assertEqual(self.rpc("ping", request_id=request_id)[1]["error"]["code"], -32600)
        self.assertEqual(self.job_count(), 0)

    def test_transport_content_type_accept_body_size_and_duplicate_lengths(self):
        self.assertEqual(self.rpc("ping", headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.rpc("ping", headers={"Accept": "application/json"})[0], 406)
        self.assertEqual(self.request(raw=b"", headers={"Content-Length": str(mcp.MAX_BODY_BYTES + 1)})[0], 413)
        self.assertEqual(self.rpc("ping", duplicate_headers=(("Content-Length", "1"),))[0], 400)
        self.assertEqual(self.rpc("ping", headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_notification_calls_are_acknowledged_but_never_execute_tools(self):
        status, response, _ = self.request({"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "submit_job", "arguments": {"idempotencyKey": "ignored", "payload": {"action": "account.status"}}}})
        self.assertEqual((status, response), (202, None))
        self.assertEqual(self.job_count(), 0)

    def test_backend_transport_error_has_no_retry_and_does_not_leak_secrets(self):
        with mock.patch.object(mcp.http.client.HTTPConnection, "request",
                               side_effect=OSError(BACKEND_TOKEN)) as request:
            result = self.service.call_tool("submit_job", {"idempotencyKey": "uncertain-original",
                                                          "payload": {"action": "account.status"}})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(result["isError"])
        self.assertTrue(result["structuredContent"]["outcomeUnknown"])
        self.assertNotIn(BACKEND_TOKEN, json.dumps(result))
        self.assertNotIn(MCP_TOKEN, json.dumps(result))

    def test_returned_credentials_are_redacted_at_the_mcp_boundary(self):
        body = {"unexpected": BACKEND_TOKEN + " " + MCP_TOKEN,
                "nested": {"accessToken": "hidden-value", "safe": "visible"}}
        with mock.patch.object(self.client, "request", return_value=(200, body, None)):
            result = self.service.call_tool("capabilities", {})
        serialized = json.dumps(result)
        for secret in (MCP_TOKEN, BACKEND_TOKEN, "hidden-value"):
            self.assertNotIn(secret, serialized)
        self.assertIn("visible", serialized)


class MCPConfigurationTests(unittest.TestCase):
    def test_bounded_sdk_trees_and_typed_maps_remain_enforced(self):
        schema = {"type": "object", "maxProperties": 2,
                  "properties": {"children": {"type": "array", "items": False}},
                  "additionalProperties": {"type": "integer"}}
        mcp._validate({"children": [], "count": 2}, schema)
        for value in ({"children": [{}]}, {"count": "2"}, {"a": 1, "b": 2, "c": 3}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mcp._validate(value, schema)

    def test_not_schema_preserves_mutually_exclusive_director_sources(self):
        schema = {"type": "object", "not": {"required": ["nodeId", "draftId"]}}
        mcp._validate({"nodeId": "node_test"}, schema)
        with self.assertRaises(ValueError):
            mcp._validate({"nodeId": "node_test", "draftId": "draft_test"}, schema)

    def test_private_secret_files_are_required_and_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mcp-token"
            path.write_text(MCP_TOKEN + "\n", encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(mcp.load_private_token(path), MCP_TOKEN)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                mcp.load_private_token(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            if hasattr(os, "O_NOFOLLOW"):
                with self.assertRaises(OSError):
                    mcp.load_private_token(link)

    def test_listener_is_loopback_only_and_backend_target_is_not_caller_configurable(self):
        service = mcp.MCPService(mcp.BackendClient(BACKEND_TOKEN), MCP_TOKEN,
                                 mcp.load_payload_schema(ROOT / "openapi.json"))
        with self.assertRaisesRegex(ValueError, "loopback"):
            mcp.make_server(service, host="0.0.0.0", port=0)
        for kwargs in ({"port": 0}, {"port": True}, {"timeout": float("inf")}):
            with self.assertRaises(ValueError):
                mcp.BackendClient(BACKEND_TOKEN, **kwargs)
        with self.assertRaises(ValueError):
            mcp.MCPService(mcp.BackendClient(BACKEND_TOKEN), MCP_TOKEN,
                mcp.load_payload_schema(ROOT / "openapi.json"), ["https://agent.example.test/path"])

    def test_contract_loader_fails_closed_on_external_references(self):
        with tempfile.TemporaryDirectory() as directory:
            document = json.loads((ROOT / "openapi.json").read_text())
            payload = document["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
            payload["oneOf"][0]["properties"]["remote"] = {"$ref": "https://example.invalid/schema"}
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "unsupported"):
                mcp.load_payload_schema(path)


if __name__ == "__main__":
    unittest.main()
