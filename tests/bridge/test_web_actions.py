# Fixtures use synthetic identities and material; no user account data.
"""Web boundary tests use synthetic sessions and mocked HTTP only."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

MODULE = (Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge") / "web_actions.py"
sys.path.insert(0, str(MODULE.parent))
spec = importlib.util.spec_from_file_location("web_actions_under_test", MODULE)
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)

COOKIE = "sessionid=synthetic-secret-cookie-value; csrf=synthetic-secret-csrf-value"
SIGNATURE = "synthetic-signature-value"


class WebActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "web-session.json"
        self.env = patch.dict(os.environ, {"JIMENG_WEB_SESSION_FILE": str(self.path), "JIMENG_READ_ONLY": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def session(self, **changes):
        value = {"enabled": True, "cookie": COOKIE, "userAgent": "Synthetic Test Agent/1.0",
                 "headers": {"uifid": "synthetic-uifid-value"},
                 "actionHeaders": {"subject.list": {"x-secsdk-web-signature": SIGNATURE}}}
        value.update(changes)
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)
        return value

    def result(self, data, **extra):
        return 200, json.dumps({"ret": "0", "errmsg": "", "data": data, **extra}).encode()

    def test_absent_session_disables_without_network(self):
        with patch.object(web, "_send") as send:
            result = web.execute({"action": "subject.list"}, "job")
        send.assert_not_called()
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_SESSION_NOT_CONFIGURED")
        self.assertFalse(web.capabilities()["authConfigured"])

    def test_disabled_session_does_not_send(self):
        self.session(enabled=False)
        with patch.object(web, "_send") as send:
            result = web.execute({"action": "subject.list"}, "job")
        send.assert_not_called()
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_SESSION_DISABLED")

    def test_registered_actions_strict_schemas_and_no_request_secrets(self):
        self.assertEqual(len(web.request_schemas()), len(web.ACTIONS))
        self.assertEqual(len(web.ACTIONS), 42)
        for schema in web.request_schemas():
            self.assertFalse(schema["additionalProperties"])
        for key in ("url", "path", "headers", "cookie", "sessionFile", "userAgent", "body", "dryRun"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                web.validate({"action": "subject.list", key: "anything"})
        self.assertFalse(web.supports("subject.update"))
        self.assertFalse(web.supports([]))

    def test_pagination_boolean_unknown_enum_and_string_injection_rejected(self):
        bad = [{"action": "subject.list", "limit": True},
               {"action": "subject.list", "subjectSource": "all"},
               {"action": "subject.list", "sortOrder": 1},
               {"action": "public_character.search", "pageSize": 101},
               {"action": "public_character.read", "characterId": "12"},
               {"action": "asset.query", "client": "web", "assetIds": ["https://elsewhere"]},
               {"action": "asset.query", "client": "web\r\nx: y", "assetIds": ["asset_123"]}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                web.validate(payload)

    def test_project_body_does_not_require_or_inject_uid(self):
        payload = {"action": "studio.project.list", "limit": 1,
                   "projectIdList": [10000000000005], "statusList": [1]}
        web.validate(payload)
        self.assertEqual(web._body(payload), {"limit": 1, "project_id_list": [10000000000005], "status_list": [1]})
        payload["cursor"] = "9223372036854775807"
        web.validate(payload)
        self.assertEqual(web._body(payload)["cursor"], payload["cursor"])
        payload["cursor"] = "9223372036854775808"
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_project_int64_number_requires_safe_json_integer(self):
        for value in [True, -1, 9007199254740992, "01", "-1", "abc"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                web.validate({"action": "studio.project.list", "projectIdList": [value]})

    def test_exact_subject_body_and_private_headers_only(self):
        self.session()
        with patch.object(web, "_send", return_value=self.result({"data_list": [], "next_cursor": 0, "has_more": False})) as send:
            result = web.execute({"action": "subject.list"}, "job")
        self.assertTrue(result["upstream"]["ok"])
        path, body, headers = send.call_args.args
        self.assertEqual(path, "/mweb/v1/dreamina_subject/get")
        self.assertEqual(body, {"cursor": 0, "limit": 20, "subject_source": "mine", "sort_field": 0, "sort_order": 0})
        self.assertEqual(headers["Cookie"], COOKIE)
        self.assertEqual(headers["x-secsdk-web-signature"], SIGNATURE)
        self.assertNotIn(COOKIE, json.dumps(result))

    def test_signatures_are_not_silently_reused_across_actions(self):
        self.session()
        with patch.object(web, "_send", return_value=self.result({"characters": [], "total": 0})) as send:
            web.execute({"action": "public_character.search"}, "job")
        self.assertNotIn("x-secsdk-web-signature", send.call_args.args[2])
        self.assertEqual(send.call_args.args[1], {"page_num": 1, "page_size": 5})

    def test_other_exact_bodies(self):
        cases = [({"action": "public_character.read", "characterId": 123}, {"character_id": 123}),
                 ({"action": "studio.style.list"}, {"status": 1, "page": 1, "page_size": 5}),
                 ({"action": "asset.query", "assetIds": ["a1"], "client": "web", "projectId": "p1"},
                  {"pippit_asset_ids": ["a1"], "Base": {"Client": "web"}, "project_id": "p1"})]
        for payload, body in cases:
            with self.subTest(action=payload["action"]):
                web.validate(payload)
                self.assertEqual(web._body(payload), body)

    def test_private_file_and_directory_permissions_required(self):
        self.session()
        self.path.chmod(0o644)
        self.assertEqual(web.capabilities()["sessionStatus"], "WEB_SESSION_PERMISSIONS")
        self.path.chmod(0o600)
        self.path.parent.chmod(0o755)
        try:
            self.assertEqual(web.capabilities()["sessionStatus"], "WEB_SESSION_PERMISSIONS")
        finally:
            self.path.parent.chmod(0o700)

    def test_symlink_session_rejected(self):
        self.session()
        saved = self.path.with_name("real.json")
        self.path.rename(saved)
        self.path.symlink_to(saved)
        self.assertEqual(web.capabilities()["sessionStatus"], "WEB_SESSION_UNREADABLE")

    def test_expiration_stops_network(self):
        self.session(validUntil=time.time() - 1)
        with patch.object(web, "_send") as send:
            result = web.execute({"action": "subject.list"}, "job")
        send.assert_not_called()
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_SESSION_EXPIRED")

    def test_session_rejects_unknown_or_unsafe_header_and_global_signature(self):
        cases = [{"headers": {"Authorization": "synthetic-private-token"}},
                 {"headers": {"x-secsdk-web-signature": SIGNATURE}},
                 {"headers": {"Origin": "https://elsewhere.invalid"}},
                 {"headers": {"Referer": "https://jimeng.jianying.com.evil/"}},
                 {"cookie": "x=y\r\nInjected: bad"},
                 {"actionHeaders": {"unknown": {}}}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.session(**changes)
                self.assertEqual(web.capabilities()["sessionStatus"], "WEB_SESSION_INVALID")

    def test_login_error_is_not_success_or_cached_data(self):
        self.session()
        response = 200, json.dumps({"ret": "1015", "errmsg": "check login error", "data": {"characters": [{"cached": True}]}}).encode()
        with patch.object(web, "_send", return_value=response):
            result = web.execute({"action": "public_character.search"}, "job")
        self.assertFalse(result["upstream"]["ok"])
        self.assertFalse(result["cacheUsed"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_LOGIN_REQUIRED")
        self.assertEqual(result["upstream"]["error"]["upstreamRet"], "1015")
        self.assertNotIn("data", result["upstream"])

    def test_signature_error_preserves_code_and_safe_message(self):
        self.session()
        response = 403, json.dumps({"ret": "40301", "errmsg": "signature invalid " + SIGNATURE}).encode()
        with patch.object(web, "_send", return_value=response):
            result = web.execute({"action": "subject.list"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_SIGNATURE_REJECTED")
        self.assertEqual(result["upstream"]["error"]["upstreamRet"], "40301")
        self.assertNotIn(SIGNATURE, json.dumps(result))

    def test_generic_upstream_error_does_not_guess_auth_failure(self):
        self.session()
        with patch.object(web, "_send", return_value=(200, b'{"ret":"1","errmsg":"list novel styles failed"}')):
            result = web.execute({"action": "studio.style.list"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_UPSTREAM_ERROR")

    def test_response_preserves_authorized_material_urls_and_excludes_credentials(self):
        self.session()
        data = {"data_list": [{"subject_id": "123", "token": "sensitive", "description": COOKIE,
                                "echo": "synthetic-secret-cookie-value",
                                "image_url": "https://media.invalid/?signature=private", "nested": {SIGNATURE: "x"}}]}
        with patch.object(web, "_send", return_value=self.result(data)):
            result = web.execute({"action": "subject.list"}, "job")
        encoded = json.dumps(result)
        for secret in [COOKIE, "synthetic-secret-cookie-value", SIGNATURE, "sensitive"]:
            self.assertNotIn(secret, encoded)
        self.assertEqual(result["upstream"]["data"]["data_list"][0]["subject_id"], "123")
        self.assertEqual(result["upstream"]["data"]["data_list"][0]["image_url"], "https://media.invalid/?signature=private")

    def test_public_cookie_flags_do_not_corrupt_response_codes_or_ids(self):
        self.session(cookie=COOKIE + "; public_flag=0", headers={"pf": "web"})
        with patch.object(web, "_send", return_value=self.result({"data_list": [{"subject_id": "102030", "name": "web demo"}]})):
            result = web.execute({"action": "subject.list"}, "job")
        self.assertEqual(result["upstream"]["ret"], "0")
        self.assertEqual(result["upstream"]["data"]["data_list"][0]["subject_id"], "102030")

    def test_schema_mismatch_cannot_become_success(self):
        self.session()
        for data in [None, {"data_list": "bad"}, {"data_list": [1]}]:
            with self.subTest(data=data), patch.object(web, "_send", return_value=self.result(data)):
                result = web.execute({"action": "subject.list"}, "job")
            self.assertEqual(result["upstream"]["error"]["code"], "WEB_RESPONSE_CONTRACT_MISMATCH")

    def test_bad_json_and_oversize_are_bounded_errors(self):
        self.session()
        for raw, expected in [(b'<html>login</html>', "WEB_INVALID_RESPONSE"),
                              (b'x' * (web.MAX_RESPONSE_BYTES + 1), "WEB_RESPONSE_TOO_LARGE")]:
            with patch.object(web, "_send", return_value=(200, raw)):
                result = web.execute({"action": "subject.list"}, "job")
            self.assertEqual(result["upstream"]["error"]["code"], expected)

    def test_transport_exception_does_not_echo_sensitive_material(self):
        self.session()
        with patch.object(web, "_send", side_effect=RuntimeError(COOKIE)):
            result = web.execute({"action": "subject.list"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_TRANSPORT_FAILED")
        self.assertNotIn(COOKIE, json.dumps(result))

    def test_no_redirect_and_no_ambient_proxy(self):
        handler = web._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "go", {}, "https://other.invalid"))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.code = 200
        response.read.return_value = b'{}'
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(web.urllib.request, "build_opener", return_value=opener) as build:
            web._send("/mweb/v1/dreamina_subject/get", {}, {"Cookie": COOKIE})
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, web._NoRedirect)
        self.assertEqual(opener.open.call_args.args[0].full_url, web.ORIGIN + "/mweb/v1/dreamina_subject/get")
        self.assertEqual(response.read.call_args.args[0], web.MAX_RESPONSE_BYTES + 1)

    def test_redirect_result_is_error_without_location(self):
        self.session()
        with patch.object(web, "_send", return_value=(302, b'')):
            result = web.execute({"action": "subject.list"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_REDIRECT_BLOCKED")

    def test_capabilities_separate_verified_baseline_and_unverified_extensions(self):
        self.session()
        with patch.object(web, "_verified_executions", return_value={"subject.list": {"scope": "one test"}}):
            caps = web.capabilities()
        self.assertTrue(caps["authConfigured"])
        self.assertEqual(len(caps["actions"]), 42)
        for name, meta in caps["actions"].items():
            self.assertTrue(meta["available"])
            self.assertEqual(meta["readOnly"], not web.ACTIONS[name].get("write", False))
            self.assertEqual(meta["serverExecutionVerified"], name == "subject.list")
            if web.ACTIONS[name].get("write"):
                self.assertFalse(meta["liveExecutionAllowed"])
        self.assertNotIn(COOKIE, json.dumps(caps))
        self.assertIsNone(web.recovery({"action": "subject.list"}, "job"))

    def test_web_acceptance_uses_the_shared_reviewed_registry(self):
        import acceptance_registry
        rows = [{"action": "subject.list", "variant": "baseline", "scope": "one read"},
                {"action": "studio.script.save", "variant": "raw_content.add", "scope": "one add"},
                {"action": "video.sample", "variant": "sample", "scope": "not web"}]
        with patch.object(acceptance_registry, "all_verified", return_value=rows):
            verified = web._verified_executions()
        self.assertEqual(set(verified), {"subject.list", "studio.script.save"})
        self.assertEqual(verified["studio.script.save"][0]["variant"], "raw_content.add")

    def test_full_public_search_filters_map_only_sdk_fields(self):
        payload = {"action": "public_character.search", "name": "示例", "era": "caller-value",
                   "characterCategory": "caller-category", "skinTone": "caller-tone", "pageNumber": 2}
        web.validate(payload)
        body = web._body(payload)
        self.assertEqual(body, {"name": "示例", "era": "caller-value", "character_category": "caller-category",
                                "skin_tone": "caller-tone", "page_num": 2, "page_size": 5})

    def test_subject_get_filters_pages_without_fabricating_endpoint(self):
        self.session()
        pages = [self.result({"data_list": [{"subject_id": "11"}], "has_more": True, "next_cursor": 100}),
                 self.result({"data_list": [{"subject_id": "22", "content": {"name": "target"}}], "has_more": False, "next_cursor": 100})]
        with patch.object(web, "_send", side_effect=pages) as send:
            result = web.execute({"action": "subject.get", "subjectId": "22"}, "job")
        self.assertTrue(result["upstream"]["ok"])
        self.assertEqual(result["upstream"]["data"]["subject"]["subject_id"], "22")
        self.assertEqual(result["upstream"]["data"]["lookup"]["pagesRead"], 2)
        self.assertEqual(send.call_args_list[1].args[1]["cursor"], 100)
        self.assertTrue(all(c.args[0] == "/mweb/v1/dreamina_subject/get" for c in send.call_args_list))

    def test_subject_lookup_stops_on_real_error_without_partial_cache_fallback(self):
        self.session()
        pages = [self.result({"data_list": [{"subject_id": "11"}], "has_more": True, "next_cursor": 100}),
                 (200, b'{"ret":"1015","errmsg":"login error"}')]
        with patch.object(web, "_send", side_effect=pages) as send:
            result = web.execute({"action": "subject.get", "subjectId": "22"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_LOGIN_REQUIRED")
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("data", result["upstream"])

    def test_subject_limit_does_not_falsely_report_absence(self):
        self.session()
        with patch.object(web, "_send", return_value=self.result({"data_list": [], "has_more": True, "next_cursor": 100})):
            result = web.execute({"action": "subject.get", "subjectId": "22", "maxPages": 1}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_LOOKUP_LIMIT")
        with patch.object(web, "_send", return_value=self.result({"data_list": [], "has_more": False, "next_cursor": 0})):
            result = web.execute({"action": "subject.get", "subjectId": "22"}, "job")
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_SUBJECT_NOT_FOUND")

    def test_write_default_is_local_plan_without_session_or_network(self):
        with patch.object(web, "_send") as send:
            result = web.execute({"action": "studio.project.create", "title": "验收项目"}, "job")
        send.assert_not_called()
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["requestPlan"]["body"], {"title": "验收项目"})
        self.assertFalse(result["workspaceMutationAttempted"])

    def test_live_write_requires_read_only_policy_off(self):
        self.session()
        payload = {"action": "studio.project.create", "title": "验收项目", "dryRun": False}
        with patch.object(web, "_send") as send, self.assertRaises(ValueError):
            web.execute(payload, "job")
        send.assert_not_called()
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=self.result({"project_id": 123})):
            result = web.execute(payload, "job")
        self.assertTrue(result["upstream"]["ok"])
        self.assertTrue(result["workspaceMutationAttempted"])
        self.assertTrue(web.recovery(payload, "job")["doNotResubmit"])

    def test_script_patch_schema_preserves_multiline_content_and_media_links(self):
        text = '第一行\n{"image_url":"https://media.invalid/image?sign=asset-signature"}'
        payload = {"action": "studio.script.save", "overviewAssetId": "overview-1",
                   "patchOperations": [{"op": "caller-op", "type": "caller-type", "content": {"content": text}}]}
        result = web.execute(payload, "job")
        self.assertEqual(result["requestPlan"]["body"]["patch_operations"][0]["content"]["content"], text)
        payload["patchOperations"][0]["content"]["unobserved"] = "value"
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_script_read_keeps_json_content_in_success_response(self):
        self.session()
        content = '{"shot":"intro","url":"https://media.invalid/?signature=material-only"}'
        with patch.object(web, "_send", return_value=self.result({"Assets": [{"PippitAssetID": "script-1", "TextInfo": {"content": content}}]})):
            result = web.execute({"action": "studio.script.read", "scriptAssetId": "script-1", "client": "web"}, "job")
        self.assertEqual(result["upstream"]["data"]["Assets"][0]["TextInfo"]["content"], content)

    def test_script_save_preserves_top_level_receipt_fields(self):
        self.session()
        payload = {"action": "studio.script.save", "overviewAssetId": "overview-1", "dryRun": False,
                   "patchOperations": [{"op": "caller-op", "type": "caller-type", "content": {"title": "Title"}}]}
        response = (200, b'{"ret":"0","errmsg":"","ai_script_asset_id":"script-1","updated_episode_asset_ids":["episode-1"]}')
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=response):
            result = web.execute(payload, "job")
        self.assertEqual(result["upstream"]["data"]["ai_script_asset_id"], "script-1")

    def test_character_modify_uses_real_role_tree_with_bounded_recursion(self):
        payload = {"action": "studio.script.characters.update", "artifactId": "artifact-1",
                   "modify": {"character_list": [{"character_name": "role", "role_tree": {"role_id": "r1", "children": [{"role_id": "r2"}]}}]}}
        web.validate(payload)
        payload["modify"]["character_list"][0]["role_tree"]["children"][0]["children"] = [{"role_id": "r3"}]
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_storyboard_save_does_not_invent_save_mode(self):
        payload = {"action": "studio.storyboard.save", "storyboardAssetId": "board-1",
                   "storyboard": {"storyline": "plot", "shot_list": []}}
        result = web.execute(payload, "job")
        self.assertNotIn("save_mode", result["requestPlan"]["body"])
        self.assertEqual(result["requestPlan"]["path"], "/api/web/v1/agent/video/storyboard_21_save")
        payload["storyboard"]["unknown_field"] = "bad"
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_storyboard_modify_requires_explicit_type_and_known_parameters(self):
        payload = {"action": "studio.storyboard.modify", "artifactId": "artifact-1",
                   "operations": [{"OpType": 123, "UpdateDescriptionParams": {"EntityID": "shot-1", "NewDescription": "desc"}}]}
        web.validate(payload)
        del payload["operations"][0]["OpType"]
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_extra_reads_and_episode_bodies_are_fixed(self):
        cases = [({"action": "asset.artifact.query", "artifactIds": ["a1"], "client": "web"},
                  {"artifact_id": ["a1"], "Base": {"Client": "web"}}),
                 ({"action": "studio.canvas.list", "client": "web"},
                  {"PageNum": 1, "PageSize": 20, "Base": {"Client": "web"}}),
                 ({"action": "studio.script.resolve", "overviewAssetId": "overview-1"},
                  {"overview_pippit_asset_id": "overview-1", "only_read": True}),
                 ({"action": "studio.episode.create", "overviewAssetId": "overview-1", "episodeName": "Ep1"},
                  {"overview_pippit_asset_id": "overview-1", "episode_name": "Ep1"})]
        for payload, body in cases:
            web.validate(payload)
            self.assertEqual(web._body(payload), body)

    def test_empty_list_ack_and_array_response_match_sdk_shapes(self):
        self.assertTrue(web._valid_data("studio.canvas.list", {"Total": 0}))
        self.assertTrue(web._valid_data("studio.episode.create", None))
        self.assertTrue(web._valid_data("asset.artifact.query", [{"artifact_id": "a1"}]))
        self.assertFalse(web._valid_data("asset.artifact.query", {"artifact_id": "a1"}))

    def test_request_size_and_nonfinite_numbers_fail_before_network(self):
        payload = {"action": "studio.storyboard.save", "storyboardAssetId": "board-1",
                   "storyboard": {"global_random_seed": float('nan')}}
        with self.assertRaises(ValueError):
            web.validate(payload)

    def test_project_initialize_fixed_body_and_stable_recovery_identities(self):
        payload = {"action": "studio.project.initialize", "projectId": 123, "canvasAssetId": "456",
                   "consumerUid": "789", "workspaceId": "1011", "spaceId": "1213"}
        with patch.object(web, "_send") as send:
            result = web.execute(payload, "stable-job")
        send.assert_not_called()
        body = result["requestPlan"]["body"]
        self.assertEqual(body["agent_name"], "pippit_novel_agent_cn_v2")
        self.assertEqual(body["message"]["content"][0]["sub_type"], "biz/x_data_novel_canvas_independent_entry")
        self.assertEqual(json.loads(body["message"]["content"][0]["data"]),
                         {"episode_count": 1, "is_narration_script": True, "enable_narration_design": True, "canvas_asset_id": "456"})
        again = web._body(payload, "stable-job")
        self.assertEqual(body["request_id"], again["request_id"])
        self.assertNotEqual(body["request_id"], web._body(payload, "another-job")["request_id"])
        recovery = web.recovery({**payload, "dryRun": False}, "stable-job")
        self.assertEqual(recovery["query"]["threadId"], body["message"]["thread_id"])
        self.assertEqual(recovery["query"]["runId"], body["message"]["run_id"])
        self.assertFalse(recovery["upstreamIdempotencyVerified"])
        for name in ("agentName", "subType", "content", "threadId", "requestId"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                web.validate({**payload, name: "anything"})

    def test_project_initialize_transport_failure_retains_identities_and_never_retries(self):
        self.session()
        payload = {"action": "studio.project.initialize", "projectId": 123, "canvasAssetId": "456",
                   "consumerUid": "789", "workspaceId": "1011", "spaceId": "1213", "dryRun": False}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}), patch.object(web, "_send", side_effect=TimeoutError) as send:
            result = web.execute(payload, "stable-job")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_TRANSPORT_FAILED")
        self.assertEqual(result["recovery"]["runId"], web._initialization_ids("stable-job")["runId"])
        self.assertTrue(result["doNotResubmitAfterUncertainOutcome"])

    def test_project_initialize_receipt_is_accepted_run_not_completed_workspace(self):
        self.session()
        payload = {"action": "studio.project.initialize", "projectId": 123, "canvasAssetId": "456",
                   "consumerUid": "789", "workspaceId": "1011", "spaceId": "1213", "dryRun": False}
        ids = web._initialization_ids("stable-job")
        data = {"run": {"run_id": ids["runId"], "thread_id": ids["threadId"], "state": 1}}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}), patch.object(web, "_send", return_value=self.result(data)):
            result = web.execute(payload, "stable-job")
        self.assertTrue(result["upstream"]["ok"])
        self.assertEqual(result["upstream"]["data"]["run"]["state"], 1)
        data["run"]["run_id"] = "unrelated-run"
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}), patch.object(web, "_send", return_value=self.result(data)):
            result = web.execute(payload, "stable-job")
        self.assertFalse(result["upstream"]["ok"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_RESPONSE_CONTRACT_MISMATCH")

    def shot_asset(self, prompt="old description", version=5):
        doc = {"type": "biz/video", "content": {"bizVariant": "storyboard-shot", "generation":
               {"prompt": prompt, "references": [{"assetId": "keep-reference"}]}}}
        return {"PippitAssetID": "456", "version": version, "TextInfo": {"content": json.dumps(doc)}}

    def description_payload(self, **changes):
        return {"action": "studio.storyboard.description.update", "shotAssetId": "456", "canvasAssetId": "123",
                "expectedVersion": 5, "description": "分镜1<duration-ms>10000</duration-ms>：纸飞机。", **changes}

    def test_description_update_local_plan_has_one_fixed_path_and_no_reference_reset(self):
        with patch.object(web, "_send") as send:
            result = web.execute(self.description_payload(), "job")
        send.assert_not_called()
        body = result["requestPlan"]["body"]
        self.assertEqual(body["transactions"][0]["patches"], [{"asset_id": "456", "base_asset_version": 5,
                         "op": "replace", "path": "/content/generation/prompt", "value": self.description_payload()["description"]}])
        self.assertEqual(body["transactions"][0]["attempt"], 1)
        for field in ("path", "patches", "references", "op"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                web.validate(self.description_payload(**{field: "unrestricted"}))

    def test_description_update_checks_version_before_mutating(self):
        self.session()
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=self.result({"Assets": [self.shot_asset(version=6)]})) as send:
            result = web.execute(self.description_payload(dryRun=False), "job")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_ASSET_VERSION_CONFLICT")
        self.assertFalse(result["workspaceMutationAttempted"])

    def test_description_update_checks_target_type_before_mutating(self):
        self.session()
        asset = self.shot_asset()
        asset["TextInfo"]["content"] = '{"type":"blueprint"}'
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=self.result({"Assets": [asset]})) as send:
            result = web.execute(self.description_payload(dryRun=False), "job")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_ASSET_TYPE_MISMATCH")

    def test_description_update_verifies_ack_and_exact_saved_text(self):
        self.session()
        payload = self.description_payload(dryRun=False)
        ack = {"results": [{"transaction_id": web._patch_ids("job")["transactionId"], "status": "ack", "asset_versions": {"456": 6}}]}
        responses = [self.result({"Assets": [self.shot_asset()]}), self.result(ack),
                     self.result({"Assets": [self.shot_asset(payload["description"], 6)]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=responses) as send:
            result = web.execute(payload, "job")
        self.assertEqual(send.call_count, 3)
        self.assertTrue(result["persistenceVerified"])
        self.assertEqual(json.loads(result["verifiedAsset"]["TextInfo"]["content"])["content"]["generation"]["references"], [{"assetId": "keep-reference"}])
        self.assertEqual(result["recovery"]["query"]["assetIds"], ["456"])

    def test_description_ack_without_matching_readback_does_not_claim_success(self):
        self.session()
        ack = {"results": [{"transaction_id": web._patch_ids("job")["transactionId"], "status": "ack", "asset_versions": {"456": 6}}]}
        responses = [self.result({"Assets": [self.shot_asset()]}), self.result(ack), self.result({"Assets": [self.shot_asset("different", 6)]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=responses) as send:
            result = web.execute(self.description_payload(dryRun=False), "job")
        self.assertEqual(send.call_count, 3)
        self.assertFalse(result["upstream"]["ok"])
        self.assertFalse(result["persistenceVerified"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_READBACK_MISMATCH")

    def test_description_conflict_receipt_is_not_retried(self):
        self.session()
        ack = {"results": [{"transaction_id": web._patch_ids("job")["transactionId"], "status": "conflict"}]}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=[self.result({"Assets": [self.shot_asset()]}), self.result(ack)]) as send:
            result = web.execute(self.description_payload(dryRun=False), "job")
        self.assertEqual(send.call_count, 2)
        self.assertFalse(result["upstream"]["ok"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_PATCH_NOT_ACKNOWLEDGED")

    def test_expanded_sdk_reads_use_fixed_paths_and_base_client_subset(self):
        cases = [
            ({"action": "public_character.face_config"}, {}),
            ({"action": "public_character.expression_config"}, {}),
            ({"action": "studio.asset.list", "client": "web", "assetTypes": [1], "pageNumber": 1},
             {"Base": {"Client": "web"}, "asset_type": [1], "PageNum": 1}),
            ({"action": "studio.character_entity.list", "client": "web", "name": "角色", "cursor": ""},
             {"Base": {"Client": "web"}, "character_name": "角色", "cursor": ""}),
            ({"action": "studio.character_entity.read", "client": "web", "entityId": "12345"},
             {"Base": {"Client": "web"}, "EntityID": "12345"}),
            ({"action": "studio.character.read", "client": "web", "characterId": "12345"},
             {"Base": {"Client": "web"}, "character_id": "12345"}),
            ({"action": "studio.character.resolve", "client": "web", "characterId": "12345", "scene": "explicit-scene"},
             {"Base": {"Client": "web"}, "character_id": "12345", "scene": "explicit-scene"}),
            ({"action": "studio.project.overview.read", "projectId": 12345},
             {"Base": {"Client": "web"}, "project_id": 12345, "file_name": "/workspace/projects/overview.json"})]
        for payload, expected in cases:
            with self.subTest(action=payload["action"]):
                web.validate(payload)
                self.assertEqual(web._body(payload), expected)
                with self.assertRaises(ValueError):
                    web.validate({**payload, "Base": {"Client": "arbitrary"}})

    def test_expanded_sdk_writes_default_local_and_require_explicit_semantics(self):
        cases = [
            {"action": "studio.project.clone", "sourceProjectId": 12345},
            {"action": "studio.settings.update", "overviewAssetId": "ov", "settings": {"video_ratio": "9:16"}},
            {"action": "studio.episode.videos.update", "overviewAssetId": "ov", "expectedSourceVersion": 3,
             "operations": [{"op": 7, "episode_video_id": "ep1", "title": "Explicit operation"}]},
            {"action": "studio.role.audio.generate", "overviewAssetId": "ov", "roleAssetId": "role1", "mode": 7, "text": "台词\n第二句"},
            {"action": "studio.video.erase_subtitle", "assetId": "video1", "shotId": "S1"},
            {"action": "studio.images.generate", "overviewAssetId": "ov", "generateType": 7, "generateScope": "explicit-scope"},
            {"action": "studio.script.snapshot.restore", "overviewAssetId": "ov", "version": 3, "snapshotType": "explicit-type"}]
        for payload in cases:
            with self.subTest(action=payload["action"]), patch.object(web, "_send") as send:
                result = web.execute(payload, "job")
                self.assertTrue(result["dryRun"])
                self.assertFalse(result["workspaceMutationAttempted"])
                send.assert_not_called()
                with self.assertRaises(ValueError):
                    web.validate({**payload, "dryRun": False})
        with self.assertRaises(ValueError):
            web.validate({"action": "studio.project.clone"})
        with self.assertRaises(ValueError):
            web.validate({"action": "studio.settings.update", "overviewAssetId": "ov", "settings": {}})
        with self.assertRaises(ValueError):
            web.validate({"action": "studio.role.audio.generate", "overviewAssetId": "ov", "roleAssetId": "role1", "mode": 7})
        with self.assertRaises(ValueError):
            web.validate({"action": "studio.images.generate", "overviewAssetId": "ov", "generateType": 7, "generateScope": "s", "forceGenerate": True})
        with self.assertRaises(ValueError):
            web.validate({"action": "studio.episode.videos.update", "overviewAssetId": "ov", "expectedSourceVersion": 3,
                          "operations": [{"op": "invented-reorder", "path": "/anything"}]})

    def test_expanded_response_shapes_do_not_accept_empty_config_as_success(self):
        self.assertTrue(web._valid_data("public_character.face_config", {"config": "{}"}))
        self.assertFalse(web._valid_data("public_character.face_config", {}))
        self.assertTrue(web._valid_data("studio.character_entity.list", {"Total": 0}))
        self.assertFalse(web._valid_data("studio.character_entity.list", {}))
        self.assertTrue(web._valid_data("studio.settings.update", None))


    def episode_assets(self, content="before", version=4, overview_version=7, episode_id="2"):
        return [{"PippitAssetID": "overview-1", "version": overview_version, "TextInfo": {"content": json.dumps({
            "episode_assets": [{"episode_id": episode_id, "pippit_asset_id": "episode-1"}]})}},
            {"PippitAssetID": "episode-1", "version": version, "TextInfo": {"content": json.dumps({
                "episode_info": {"episode_id": episode_id, "episode_title": "preserve-title", "raw_content": content}})}}]

    def episode_update_payload(self, **changes):
        return {"action": "studio.episode.update", "overviewAssetId": "overview-1", "episodeAssetId": "episode-1",
                "episodeNum": 2, "expectedVersion": 4, "content": "保存正文\n第二行", **changes}

    def episode_delete_payload(self, **changes):
        return {"action": "studio.episode.delete", "overviewAssetId": "overview-1", "episodeAssetId": "episode-1",
                "episodeId": "2", "expectedOverviewVersion": 7, **changes}

    def test_episode_update_is_only_the_observed_raw_content_operation(self):
        with patch.object(web, "_send") as send:
            result = web.execute(self.episode_update_payload(), "episode-job")
        send.assert_not_called()
        self.assertEqual(result["requestPlan"]["body"], {
            "overview_pippit_asset_id": "overview-1", "patch_operations": [{
                "op": "update", "type": "raw_content", "episode_asset_id": "episode-1", "episode_num": 2,
                "content": {"content": "保存正文\n第二行"}}]})
        for extra in ({"title": "unsupported"}, {"patchOperations": []}, {"episodeNum": 0}, {"expectedVersion": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                web.validate(self.episode_update_payload(**extra))
        web.validate(self.episode_update_payload(content=""))
        self.assertEqual(web._body(self.episode_update_payload(content=""))["patch_operations"][0]["content"], {"content": ""})

    def test_episode_update_checks_version_and_membership_before_mutation(self):
        self.session()
        for assets, code in [(self.episode_assets(version=5), "WEB_ASSET_VERSION_CONFLICT"),
                             (self.episode_assets(episode_id="3"), "WEB_EPISODE_IDENTITY_MISMATCH")]:
            with self.subTest(code=code), patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=self.result({"Assets": assets})) as send:
                result = web.execute(self.episode_update_payload(dryRun=False), "episode-job")
            self.assertEqual(send.call_count, 1)
            self.assertFalse(result["workspaceMutationAttempted"])
            self.assertEqual(result["upstream"]["error"]["code"], code)

    def test_episode_update_verifies_exact_content_and_preserves_title(self):
        self.session()
        payload = self.episode_update_payload(dryRun=False)
        after = self.episode_assets(payload["content"], 5)[1]
        replies = [self.result({"Assets": self.episode_assets()}),
                   self.result(None, updated_episode_asset_ids=["episode-1"]), self.result({"Assets": [after]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=replies) as send:
            result = web.execute(payload, "episode-job")
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args_list[1].args[0], "/api/web/v1/agent/novel/update_ai_script_asset")
        self.assertTrue(result["persistenceVerified"])
        self.assertEqual(result["upstream"]["data"], {"updated_episode_asset_ids": ["episode-1"]})
        self.assertEqual(result["versionCheckScope"], "bridge_preflight_only_not_upstream_atomic")
        self.assertEqual(result["verifiedAsset"], after)

    def test_episode_success_receipt_without_matching_readback_is_not_verified(self):
        self.session()
        replies = [self.result({"Assets": self.episode_assets()}), self.result(None),
                   self.result({"Assets": [self.episode_assets("wrong", 5)[1]]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=replies):
            result = web.execute(self.episode_update_payload(dryRun=False), "episode-job")
        self.assertFalse(result["persistenceVerified"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_READBACK_MISMATCH")

    def test_episode_delete_local_plan_and_live_confirmation_gate(self):
        with patch.object(web, "_send") as send:
            result = web.execute(self.episode_delete_payload(), "episode-job")
        send.assert_not_called()
        self.assertEqual(result["requestPlan"]["body"], {"episode_id": "2", "overview_pippit_asset_id": "overview-1"})
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}):
            for confirmation in (None, False):
                payload = self.episode_delete_payload(dryRun=False)
                if confirmation is not None:
                    payload["confirmPermanentDelete"] = confirmation
                with self.subTest(confirmation=confirmation), self.assertRaisesRegex(ValueError, "confirmation"):
                    web.validate(payload)

    def test_episode_delete_requires_exact_asset_association(self):
        self.session()
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", return_value=self.result({"Assets": [self.episode_assets()[0]]})) as send:
            result = web.execute(self.episode_delete_payload(episodeAssetId="other", dryRun=False, confirmPermanentDelete=True), "episode-job")
        self.assertEqual(send.call_count, 1)
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_EPISODE_IDENTITY_MISMATCH")

    def test_episode_delete_readback_reports_only_overview_removal(self):
        self.session()
        after = {"PippitAssetID": "overview-1", "version": 8, "TextInfo": {"content": '{"episode_assets":[]}'}}
        replies = [self.result({"Assets": [self.episode_assets()[0]]}), self.result(None), self.result({"Assets": [after]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=replies) as send:
            result = web.execute(self.episode_delete_payload(dryRun=False, confirmPermanentDelete=True), "episode-job")
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args_list[1].args[1], {"episode_id": "2", "overview_pippit_asset_id": "overview-1"})
        self.assertTrue(result["persistenceVerified"])
        self.assertEqual(result["deletionScopeVerified"], "removed_from_overview_only")
        self.assertFalse(result["recoverabilityVerified"])

    def test_episode_delete_success_receipt_does_not_prove_removal(self):
        self.session()
        replies = [self.result({"Assets": [self.episode_assets()[0]]}), self.result(None),
                   self.result({"Assets": [self.episode_assets(overview_version=8)[0]]})]
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}), patch.object(web, "_send", side_effect=replies):
            result = web.execute(self.episode_delete_payload(dryRun=False, confirmPermanentDelete=True), "episode-job")
        self.assertFalse(result["persistenceVerified"])
        self.assertEqual(result["upstream"]["error"]["code"], "WEB_READBACK_MISMATCH")

    def test_share_get_is_artifact_scoped_preview_read_without_sharing_write(self):
        self.session()
        payload = {"action": "studio.asset.share_get", "artifactId": "artifact-existing", "client": "web"}
        preview = {"share_preview": {"previewinfo": {"preview_url": "https://image.example/existing.png"}}}
        with patch.object(web, "_send", return_value=self.result(preview)) as send:
            result = web.execute(payload, "share-job")
        self.assertEqual(send.call_args.args[:2], ("/api/biz/v1/asset/query_share_asset", {"ArtifactID": "artifact-existing", "Base": {"Client": "web"}}))
        self.assertTrue(result["readOnly"])
        self.assertTrue(result["upstream"]["ok"])
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertEqual(result["upstream"]["data"], preview)
        for extra in ({"shareId": "unknown"}, {"ignoreExpire": True}, {"allowClone": True}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                web.validate({**payload, **extra})
        for data in ({}, {"Assets": []}, {"share_preview": None}, {"share_preview": {"unknown": "wrong"}},
                     {"share_preview": {"previewinfo": {"url": "wrong_field"}}},
                     {"share_preview": {"previewinfo": {"preview_url": 123}}},
                     {"share_preview": {"previewinfo": {"metadata": {"width": 640}}}},
                     {"share_preview": {"previewinfo": {"preview_url": "https://image.example/image.png", "metadata": {"unknown": 1}}}}):
            self.assertFalse(web._valid_data("studio.asset.share_get", data))
        self.assertTrue(web._valid_data("studio.asset.share_get", {
            "share_preview": {"previewinfo_1080p": {"preview_url": "https://image.example/image.png", "metadata": {"width": 1080, "height": None}}, "previewinfo": None},
            "download_preview": None}))


if __name__ == "__main__":
    unittest.main()
