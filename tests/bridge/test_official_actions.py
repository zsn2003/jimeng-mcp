# Fixtures use synthetic identities and material; no user account data.
"""Contract/security tests only; no actual CLI, login, write, or generation."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import official_actions as actions


PROJECT = "11111111-2222-4333-8444-555555555555"
RESOURCE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NODE = "node_0123456789"
OTHER_NODE = "node_abcdefghjk"
JOB = "unit-test-stable-job-01"


class OfficialActionsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"JIMENG_READ_ONLY": "1", "JIMENG_MAX_CREDITS": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.invoke = Mock(return_value={"ok": True, "data": {"testDouble": True}})
        self.invoke_sensitive = Mock()
        self.command_log = []

    def execute(self, payload, job_id=JOB):
        return actions.execute(payload, job_id, invoke=self.invoke, invoke_sensitive=self.invoke_sensitive)

    def approved_generation_flow(self, node_type="video", total=3):
        def invoke(args):
            self.command_log.append(list(args))
            if args[:2] == ["node", "show"]:
                return {"ok": True, "data": {"nodes": [{"nodeId": NODE, "result": "FOUND",
                    "node": {"nodeId": NODE, "type": node_type}}]}}
            if args[:2] == ["node", "quote"]:
                return {"ok": True, "data": {"items": [{"nodeId": NODE, "maxCredits": total}],
                    "totalMaxCredits": total, "draftVersion": 1, "confirmable": True}}
            if args[:2] == ["node", "run"]:
                return {"ok": True, "data": {"items": [{"nodeId": NODE, "state": "accepted"}],
                    "message": "diagnostic test-only-private-token must be redacted"}}
            raise AssertionError("Unexpected test command")
        def sensitive(args):
            self.command_log.append(list(args))
            self.assertEqual(args[:2], ["node", "confirm"])
            ceiling = int(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--credit-ceiling=")))
            return {"ok": True, "data": {"creditConfirmationToken": "test-only-private-token",
                    "creditCeiling": ceiling, "expiresAt": "test-only-expiry"}}
        self.invoke.side_effect = invoke
        self.invoke_sensitive.side_effect = sensitive

    def assert_invalid(self, payload):
        with self.assertRaises(ValueError):
            self.execute(payload)
        self.invoke.assert_not_called()

    def test_capability_and_request_schema_exports_are_closed_and_independent(self):
        schemas = actions.request_schemas()
        self.assertEqual(set(schemas), set(actions.ACTIONS))
        for name, schema in schemas.items():
            self.assertEqual(schema["properties"]["action"]["const"], name)
            self.assertFalse(schema["additionalProperties"])
            if actions.is_mutating(name):
                self.assertTrue(schema["properties"]["dryRun"]["default"])
        schemas["image.create"]["properties"]["prompt"]["maxLength"] = 1
        self.assertGreater(actions.request_schemas()["image.create"]["properties"]["prompt"]["maxLength"], 1)
        cap = actions.capabilities()
        self.assertFalse(cap["liveWritesEnabled"])
        self.assertEqual(cap["defaultCreditCeiling"], 0)
        self.assertTrue(cap["actions"]["image.create"]["mutatesWorkspace"])
        self.assertFalse(cap["actions"]["image.create"]["billable"])
        self.assertTrue(cap["actions"]["image.generate"]["billable"])
        self.assertTrue(cap["actions"]["space.select"]["mutatesLocalState"])
        json.dumps(cap, allow_nan=False)

    def test_read_only_calls_have_fixed_argv_and_do_not_accept_paths(self):
        cases = [
            ({"action": "account.read"}, ["auth", "account"]),
            ({"action": "space.current"}, ["space", "current"]),
            ({"action": "team.list", "limit": 5}, ["team", "ls", "--limit=5"]),
            ({"action": "voice.list", "model": "tts-model", "count": 10}, ["voice", "list", "--count=10", "--model=tts-model"]),
            ({"action": "director.get", "projectId": PROJECT, "nodeId": NODE},
             ["director", "show", "--project-id=" + PROJECT, "--node-id=" + NODE]),
        ]
        for payload, expected in cases:
            with self.subTest(action=payload["action"]):
                self.invoke.reset_mock()
                result = self.execute(payload)
                self.invoke.assert_called_once_with(expected)
                self.assertFalse(result["dryRun"])
        self.invoke.reset_mock()
        self.assert_invalid({"action": "director.get", "file": "/etc/passwd"})

    def test_operations_without_upstream_dryrun_produce_only_local_plan(self):
        for payload in (
            {"action": "canvas.create", "name": "测试画布"},
            {"action": "space.select", "kind": "team", "teamId": "123"},
            {"action": "canvas.select", "projectId": PROJECT},
            {"action": "account.refresh"},
            {"action": "node.confirm", "projectId": PROJECT, "nodeId": NODE},
        ):
            with self.subTest(action=payload["action"]):
                result = self.execute(payload)
                self.assertEqual(result["dryRunKind"], "bridge-local-plan")
                self.assertFalse(result["cliInvoked"])
                self.assertFalse(result["generationSubmitted"])
                self.assertFalse(result["workspaceMutationAttempted"])
        self.invoke.assert_not_called()

    def test_every_node_type_creates_only_a_draft_with_stable_identity(self):
        payloads = {
            "image": {}, "video": {}, "audio": {"voiceId": "1"}, "element": {"main": NODE},
            "text": {"text": "# 正式文本\n正文"},
            "timeline": {"clips": [{"sourceId": NODE, "mediaType": "image", "durationMs": 1000}]},
        }
        for kind, fields in payloads.items():
            with self.subTest(kind=kind):
                self.invoke.reset_mock()
                payload = {"action": kind + ".create", "projectId": PROJECT, "title": "测试", **fields}
                first = self.execute(payload)
                args = self.invoke.call_args.args[0]
                self.assertEqual(args[:3], ["node", "create", kind])
                self.assertIn("--dry-run", args)
                self.assertNotIn("--run", args)
                self.assertFalse(any(arg.startswith("--credit-ceiling") for arg in args))
                self.assertIn("--node-id=" + first["recovery"]["nodeId"], args)
                self.assertIn("--update-id=" + first["recovery"]["updateId"], args)
                second = self.execute(payload)
                self.assertEqual(first["recovery"], second["recovery"])

    def test_update_is_save_only_and_rejects_implicit_run_or_credit_fields(self):
        payload = {"action": "video.update", "projectId": PROJECT, "nodeId": NODE,
                   "mode": "t2v", "prompt": "a scene"}
        self.execute(payload)
        args = self.invoke.call_args.args[0]
        self.assertEqual(args[:3], ["node", "edit", "video"])
        self.assertNotIn("--run", args)
        self.invoke.reset_mock()
        for extra in ({"run": True}, {"creditCeiling": 1}, {"shell": "id"}, {"args": ["--run"]}):
            self.assert_invalid({**payload, **extra})

    def test_read_only_blocks_zero_cost_writes_as_well_as_generation(self):
        for payload in (
            {"action": "canvas.create", "name": "x", "dryRun": False},
            {"action": "text.create", "projectId": PROJECT, "title": "x", "text": "y", "dryRun": False},
            {"action": "space.select", "kind": "personal", "dryRun": False},
            {"action": "node.confirm", "projectId": PROJECT, "nodeId": NODE, "dryRun": False},
            {"action": "video.generate", "projectId": PROJECT, "nodeId": NODE, "dryRun": False},
        ):
            with self.subTest(action=payload["action"]):
                self.assert_invalid(payload)

    def test_live_generation_requires_positive_approved_operator_bounded_ceiling(self):
        payload = {"action": "video.regenerate", "projectId": PROJECT, "nodeId": NODE, "dryRun": False}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "5"}):
            for ceiling in (None, 0, -1, 6, True):
                candidate = {**payload, **({"creditCeiling": ceiling} if ceiling is not None else {})}
                self.assert_invalid(candidate)
            self.approved_generation_flow()
            result = self.execute({**payload, "creditCeiling": 5})
        args = self.command_log[-1]
        self.assertEqual([a[:2] for a in self.command_log], [["node", "show"], ["node", "quote"], ["node", "confirm"], ["node", "run"]])
        self.assertEqual(args[:2], ["node", "run"])
        self.assertIn("--credit-token=test-only-private-token", args)
        self.assertFalse(any(arg.startswith("--credit-ceiling") for arg in args))
        self.assertFalse(any(arg.startswith("--update-id") for arg in args))
        self.assertNotIn("--dry-run", args)
        self.assertFalse(result["dryRun"])
        self.assertIsNone(result["generationSubmitted"])
        self.assertTrue(result["workspaceMutationAttempted"])
        self.assertTrue(result["creditApprovalAttempted"])
        self.assertNotIn("test-only-private-token", json.dumps(result))

    def test_separate_generation_defaults_to_zero_ceiling_and_local_plan(self):
        for action in ("image.generate", "video.generate", "audio.generate", "video.regenerate", "node.run"):
            self.invoke.reset_mock()
            payload = {"action": action, "projectId": PROJECT, "nodeId": NODE}
            if action == "node.run":
                payload["nodeType"] = "audio"
            result = self.execute(payload)
            self.invoke.assert_not_called()
            self.invoke_sensitive.assert_not_called()
            self.assertEqual(result["plan"]["creditCeiling"], 0)
            self.assertEqual(result["dryRunKind"], "bridge-local-plan")
            self.assertEqual(result["recovery"]["nextAction"], "operation.status")

    def test_live_generation_requires_private_confirmation_callback_without_fallback(self):
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "5"}):
            result = actions.execute({"action": "video.generate", "projectId": PROJECT,
                "nodeId": NODE, "dryRun": False, "creditCeiling": 5}, JOB, invoke=self.invoke)
        self.assertFalse(result["generationSubmitted"])
        self.assertEqual(result["error"]["code"], "PRIVATE_CONFIRM_CHANNEL_REQUIRED")
        self.invoke.assert_not_called()

    def test_incomplete_or_expensive_quote_never_confirms_or_runs(self):
        payload = {"action": "video.generate", "projectId": PROJECT, "nodeId": NODE,
                   "dryRun": False, "creditCeiling": 5}
        for total in (None, 6):
            self.approved_generation_flow(total=total)
            self.invoke_sensitive.reset_mock()
            self.command_log.clear()
            with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "5"}):
                result = self.execute(payload)
            self.assertFalse(result["generationSubmitted"])
            self.invoke_sensitive.assert_not_called()
            self.assertEqual([a[:2] for a in self.command_log], [["node", "show"], ["node", "quote"]])

    def test_failed_confirmation_or_missing_token_never_runs(self):
        payload = {"action": "video.generate", "projectId": PROJECT, "nodeId": NODE,
                   "dryRun": False, "creditCeiling": 5}
        for response in ({"ok": False, "error": {"code": "denied"}},
                         {"ok": True, "data": {"creditCeiling": 5}},
                         {"ok": True, "data": {"creditConfirmationToken": "[REDACTED]", "creditCeiling": 5}},
                         {"ok": True, "data": {"creditConfirmationToken": "token", "creditCeiling": 99}}):
            self.approved_generation_flow()
            self.invoke_sensitive.side_effect = None
            self.invoke_sensitive.return_value = response
            self.command_log.clear()
            with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "5"}):
                result = self.execute(payload)
            self.assertFalse(result["generationSubmitted"])
            self.assertFalse(result["workspaceMutationAttempted"])
            self.assertTrue(result["creditApprovalAttempted"])
            self.assertFalse(any(a[:2] == ["node", "run"] for a in self.command_log))

    def test_source_type_mismatch_stops_before_quote_or_approval(self):
        self.approved_generation_flow(node_type="image")
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "5"}):
            result = self.execute({"action": "video.generate", "projectId": PROJECT,
                                  "nodeId": NODE, "dryRun": False, "creditCeiling": 5})
        self.assertEqual(result["error"]["code"], "SOURCE_NODE_TYPE_UNVERIFIED")
        self.invoke_sensitive.assert_not_called()
        self.assertEqual([a[:2] for a in self.command_log], [["node", "show"]])

    def test_postprocessing_uses_stable_submit_id_without_run_flag(self):
        for action, fields in (
            ("image.upscale", {"resolution": "4K"}),
            ("image.cutout", {}),
            ("video.upscale", {"resolution": "2K"}),
            ("video.interpolate", {"fps": 60}),
        ):
            self.invoke.reset_mock()
            result = self.execute({"action": action, "projectId": PROJECT, "nodeId": NODE, **fields})
            args = self.invoke.call_args.args[0]
            self.assertNotIn("--run", args)
            self.assertIn("--dry-run", args)
            self.assertIn("--credit-ceiling=0", args)
            self.assertIn("--submit-id=" + result["recovery"]["submitId"], args)

    def test_postprocessing_rejects_invalid_modes_and_parameter_combinations(self):
        for action, fields in (
            ("image.upscale", {}),
            ("image.upscale", {"mode": "normal", "resolution": "4K"}),
            ("image.upscale", {"mode": "normal", "detail": 50}),
            ("video.upscale", {"resolution": "8K"}),
            ("video.interpolate", {"fps": 120}),
        ):
            self.assert_invalid({"action": action, "projectId": PROJECT, "nodeId": NODE, **fields})

    def test_prompt_and_reference_sources_cannot_bypass_node_res_allowlist(self):
        payload = {"action": "image.create", "projectId": PROJECT, "title": "x", "mode": "i2i"}
        for ref in ("uri:https://example.com/a.png", "vid:external", "node:/etc/passwd", "res:not-a-uuid"):
            self.assert_invalid({**payload, "refs": [ref]})
        for prompt in ("{{uri:https://example.com/x}}", "{{vid:private-path}}", "{{node:/etc/passwd}}",
                       "{{uri:https://example.com/unclosed", "{{node:" + NODE):
            self.assert_invalid({**payload, "prompt": prompt})
        self.assert_invalid({**payload, "sourceUrl": "https://example.com"})
        self.assert_invalid({**payload, "file": "/tmp/image.png"})
        self.execute({**payload, "prompt": "Use {{node:" + NODE + "}}", "refs": ["res:" + RESOURCE]})
        self.assertTrue(self.invoke.called)

    def test_free_text_stays_inside_single_flag_value_and_never_becomes_argv(self):
        prompt = "--run --credit-ceiling=999; $(touch /tmp/should-never-run)"
        self.execute({"action": "image.create", "projectId": PROJECT, "title": "x", "mode": "t2i", "prompt": prompt})
        args = self.invoke.call_args.args[0]
        self.assertIn("--prompt=" + prompt, args)
        self.assertNotIn("--run", args)
        self.assertNotIn("--credit-ceiling=999", args)
        self.invoke.reset_mock()
        self.assert_invalid({"action": "canvas.create", "name": "--use"})

    def test_first_last_frame_counts_merged_prompt_and_explicit_references(self):
        payload = {"action": "video.create", "projectId": PROJECT, "title": "x", "mode": "first_last_frame"}
        self.execute({**payload, "prompt": "{{node:" + NODE + "}}", "refs": ["node:" + NODE, "res:" + RESOURCE]})
        self.invoke.reset_mock()
        self.assert_invalid({**payload, "prompt": "{{node:" + OTHER_NODE + "}}", "refs": ["node:" + NODE, "res:" + RESOURCE]})

    def test_structured_timeline_compiles_only_documented_clip_options(self):
        result = self.execute({"action": "timeline.create", "projectId": PROJECT, "title": "序列",
            "clips": [{"sourceId": NODE, "mediaType": "image", "durationMs": 1200},
                      {"sourceId": RESOURCE, "mediaType": "video", "trimStartMs": 0, "trimDurationMs": 4000, "volume": 0.5}],
            "audioClips": [{"sourceId": OTHER_NODE, "startMs": 500, "trimStartMs": 0, "trimDurationMs": 2000, "muted": True}]})
        args = self.invoke.call_args.args[0]
        self.assertIn("--clip=" + NODE + ",kind=canvas_node,duration=1200", args)
        self.assertIn("--clip=" + RESOURCE + ",kind=resource,trim=0-4000,volume=0.5", args)
        self.assertIn("--audio-clip=" + OTHER_NODE + ",kind=canvas_node,start=500,trim=0-2000,muted", args)
        self.assertFalse(result["generationSubmitted"])
        self.assertNotIn("--run", args)

    def test_timeline_rejects_unknown_sources_options_and_conflicting_clears(self):
        base = {"action": "timeline.update", "projectId": PROJECT, "nodeId": NODE}
        invalid = [
            {"clips": [{"sourceId": "https://example.com", "mediaType": "video"}]},
            {"clips": [{"sourceId": NODE, "mediaType": "image", "speed": 2}]},
            {"clips": [{"sourceId": NODE, "mediaType": "video", "startMs": 100}]},
            {"clips": [{"sourceId": NODE, "mediaType": "video", "trimStartMs": 2}]},
            {"audioClips": [{"sourceId": NODE, "durationMs": 20}]},
            {"clearTracks": True, "clips": [{"sourceId": NODE, "mediaType": "video"}]},
        ]
        for fields in invalid:
            self.assert_invalid({**base, **fields})

    def test_timeline_creation_requires_visual_or_audio_clips_in_validation_and_schema(self):
        base = {"action": "timeline.create", "projectId": PROJECT, "title": "sequence"}
        self.assert_invalid(base)
        self.assert_invalid({**base, "clips": []})
        self.execute({**base, "audioClips": [{"sourceId": NODE}]})
        self.assertIn("--audio-clip=" + NODE + ",kind=canvas_node", self.invoke.call_args.args[0])
        self.assertEqual(actions.request_schemas()["timeline.create"]["anyOf"],
                         [{"required": ["clips"]}, {"required": ["audioClips"]}])

    def test_live_nonbillable_save_reports_workspace_attempt_without_claiming_generation(self):
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0"}):
            result = self.execute({"action": "text.create", "projectId": PROJECT,
                                   "title": "live-save-test", "text": "test", "dryRun": False})
        self.assertTrue(result["workspaceMutationAttempted"])
        self.assertFalse(result["generationSubmitted"])
        self.assertFalse(result["localStateMutationAttempted"])
        self.assertNotIn("submitted", result)

    def test_element_reference_binding_and_clear_semantics_are_explicit(self):
        base = {"action": "element.update", "projectId": PROJECT, "nodeId": NODE}
        self.assert_invalid({**base, "clearMain": True, "voice": OTHER_NODE})
        self.assert_invalid({**base, "clearAuxiliary": True, "auxiliary": [RESOURCE]})
        self.assert_invalid({**base, "auxiliary": [RESOURCE, OTHER_NODE]})
        self.assert_invalid({**base, "main": "/tmp/face.png"})
        self.execute({**base, "main": OTHER_NODE, "auxiliary": [NODE, RESOURCE]})
        args = self.invoke.call_args.args[0]
        self.assertIn("--main=" + OTHER_NODE, args)
        self.assertNotIn("--run", args)

    def test_audio_rules_use_only_verified_modes_voices_and_scenarios(self):
        base = {"action": "audio.create", "projectId": PROJECT, "title": "配音"}
        for fields in ({}, {"mode": "tts", "prompt": "missing voice"}, {"voiceName": "   "}, {"mode": "music"}, {"voiceId": "001"}, {"voiceId": "1", "voiceName": "speaker"},
                       {"scenario": "text-reading"}, {"mode": "music", "model": "m", "voiceId": "1"},
                       {"mode": "tts", "duration": 5}):
            self.assert_invalid({**base, **fields})
        self.execute({**base, "mode": "tts", "model": "m", "scenario": "text-reading", "voiceId": "123", "prompt": "台词"})
        self.assertIn("--voice-id=123", self.invoke.call_args.args[0])

    def test_omni_dubbing_voice_is_optional_only_with_explicit_scenario_and_model(self):
        self.execute({"action": "audio.create", "projectId": PROJECT, "title": "omni",
                      "mode": "tts", "model": "catalog-model", "scenario": "omni-dubbing",
                      "refs": ["node:" + NODE], "prompt": "voice reference"})
        self.assertIn("--scenario=omni-dubbing", self.invoke.call_args.args[0])
        self.assertFalse(any(arg.startswith("--voice-id") or arg.startswith("--voice-name") for arg in self.invoke.call_args.args[0]))

    def test_imported_resources_keep_stable_batch_and_source_project_identities(self):
        base = {"action": "image.create", "projectId": PROJECT, "title": "导入", "resourceIds": [RESOURCE]}
        self.assert_invalid(base)
        result = self.execute({**base, "importKind": "local_upload", "resourceSources": [{"resourceId": RESOURCE, "sourceProjectId": PROJECT}]})
        args = self.invoke.call_args.args[0]
        self.assertIn("--resource-source=" + RESOURCE + "@" + PROJECT, args)
        self.assertIn("--submit-id=" + result["recovery"]["submitId"], args)
        self.assertNotIn("--run", args)

    def test_recovery_status_queries_original_submit_identity_with_bounded_wait(self):
        generation = {"action": "video.generate", "projectId": PROJECT, "nodeId": NODE}
        original = actions.recovery(generation, JOB)
        self.execute({"action": "operation.wait", "projectId": PROJECT, "submitId": original["submitId"],
                      "timeoutSeconds": 0, "intervalSeconds": 1})
        args = self.invoke.call_args.args[0]
        self.assertEqual(args[:3], ["operation", "wait", original["submitId"]])
        self.assertIn("--timeout=0s", args)
        self.assertFalse(any("--run" == item for item in args))
        self.assertNotEqual(actions.recovery(generation, JOB + "new")["submitId"], original["submitId"])

    def test_unverified_director_actions_return_explicit_unavailable_without_invocation(self):
        for name in ("director.preview", "director.export", "director.freeze"):
            result = self.execute({"action": name, "projectId": PROJECT, "nodeId": NODE})
            self.assertFalse(result["available"])
            self.assertFalse(result["cliInvoked"])
            self.assertFalse(result["generationSubmitted"])
            self.assertFalse(result["workspaceMutationAttempted"])
        self.invoke.assert_not_called()
        self.assert_invalid({"action": "director.apply", "commands": [{"type": "motion.import", "file": "/etc/passwd"}]})

    def test_unlimited_node_run_uses_exact_quote_not_a_large_sentinel(self):
        self.approved_generation_flow(total=123456)
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}):
            result = self.execute({"action": "video.generate", "projectId": PROJECT,
                                   "nodeId": NODE, "dryRun": False})
            self.assertEqual(actions.capabilities()["liveCreditCeiling"], "unlimited")
        self.assertEqual(result["effectiveCreditCeiling"], 123456)
        self.assertIn("--credit-ceiling=123456", self.command_log[2])
        self.assertFalse(any("unlimited" in arg for row in self.command_log for arg in row))

    def test_unlimited_explicit_ceiling_remains_strict_and_post_approval_is_one_call(self):
        self.approved_generation_flow(total=3)
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}):
            result = self.execute({"action": "video.generate", "projectId": PROJECT,
                "nodeId": NODE, "dryRun": False, "creditCeiling": 2})
            self.assertEqual(result["error"]["code"], "CREDIT_CEILING_EXCEEDED")
            self.invoke_sensitive.assert_not_called()
            self.invoke.side_effect = None
            self.invoke.reset_mock()
            self.execute({"action": "video.interpolate", "projectId": PROJECT, "nodeId": NODE,
                          "fps": 60, "dryRun": False})
            args = self.invoke.call_args.args[0]
            self.assertIn("--yes", args)
            self.assertFalse(any(a.startswith("--credit-ceiling") for a in args))
            self.invoke.assert_called_once()
            self.invoke.reset_mock()
            self.execute({"action": "video.interpolate", "projectId": PROJECT, "nodeId": NODE,
                          "fps": 60, "dryRun": False, "creditCeiling": 0})
            self.assertIn("--credit-ceiling=0", self.invoke.call_args.args[0])
            self.assertNotIn("--yes", self.invoke.call_args.args[0])

    def test_unlimited_exact_confirmation_rejects_incomplete_quote_and_hides_token(self):
        payload = {"action": "node.confirm", "projectId": PROJECT, "nodeId": NODE, "dryRun": False}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited"}):
            self.invoke.side_effect = [
                {"ok": True, "data": {"items": [{"nodeId": NODE, "maxCredits": 12}],
                                      "totalMaxCredits": 12, "confirmable": True}},
                {"ok": True, "data": {"creditConfirmationToken": "private-token", "creditCeiling": 12}},
            ]
            result = self.execute(payload)
            self.assertEqual(result["effectiveCreditCeiling"], 12)
            self.assertTrue(result["creditApprovalAttempted"])
            self.assertNotIn("private-token", json.dumps(result))
            self.assertIn("--credit-ceiling=12", self.invoke.call_args.args[0])
            self.invoke.reset_mock()
            self.invoke.side_effect = None
            self.invoke.return_value = {"ok": True, "data": {"totalMaxCredits": 12}}
            result = self.execute(payload)
            self.assertEqual(result["error"]["code"], "INCOMPLETE_QUOTE")
            self.invoke.assert_called_once()

    def test_director_typed_dryrun_uses_no_cli_and_rejects_opaque_imports(self):
        result = self.execute({"action": "director.apply", "projectId": PROJECT, "nodeId": NODE,
            "commands": [{"type": "object.primitive", "primitive": "cube", "id": "prop"},
                         {"type": "storyboard.add", "id": "shot1", "cameraId": "camera", "durationFrames": 30}]})
        self.assertEqual(result["dryRunKind"], "bridge-local-plan")
        self.invoke.assert_not_called()
        self.assert_invalid({"action": "director.apply", "projectId": PROJECT, "nodeId": NODE,
                             "commands": [{"type": "motion.import", "file": "/etc/passwd"}]})
        self.assert_invalid({"action": "director.apply", "projectId": PROJECT, "nodeId": NODE,
                             "commands": [{"type": "camera.set", "id": "cam", "patch": {"url": "https://example.com"}}]})

    def test_raw_approval_tokens_never_enter_requests_or_results(self):
        payload = {"action": "node.confirm", "projectId": PROJECT, "nodeId": NODE,
                   "dryRun": False, "creditCeiling": 3}
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "3"}):
            self.assert_invalid({**payload, "creditToken": "not-accepted"})
            self.invoke.return_value = {"ok": True, "data": {"creditConfirmationToken": "secret-token",
                "nested": {"authorization": "Bearer sensitive", "safe": "visible"}}}
            result = self.execute(payload)
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertNotIn("Bearer sensitive", json.dumps(result))
        self.assertEqual(result["upstream"]["data"]["nested"]["safe"], "visible")
        self.assertTrue(result["creditApprovalAttempted"])
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertFalse(result["generationSubmitted"])

    def test_transport_or_result_failure_is_uncertain_not_validation_failure(self):
        self.invoke.side_effect = ValueError("private downstream parsing error")
        with self.assertRaises(RuntimeError):
            self.execute({"action": "account.read"})
        self.invoke.side_effect = None
        self.invoke.return_value = {"unexpected": "shape"}
        with self.assertRaises(RuntimeError):
            self.execute({"action": "account.read"})

    def test_numbers_boolean_types_and_existing_identity_formats_are_strict(self):
        base = {"action": "image.create", "projectId": PROJECT, "title": "x"}
        for fields in ({"dryRun": "true"}, {"x": float("nan"), "y": 1}, {"x": 1},
                       {"mode": "t2i", "count": True}, {"mode": "t2i", "count": 10 ** 400},
                       {"title": "😀" * 300}):
            self.assert_invalid({**base, **fields})
        self.assert_invalid({"action": "node.read", "projectId": PROJECT.upper(), "nodeId": "node_invalid"})
        self.assertFalse(actions.supports(["node", "run"]))
        self.assertFalse(actions.supports("node.delete"))


class DirectorManagedActionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = (Path(temporary.name) / "private-director").resolve()
        self.env = patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited",
            "JIMENG_DIRECTOR_STATE_DIR": str(self.root), "DREAMINA_CANVAS_PROFILE": "test-director-profile"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.catalog = json.loads((Path(__file__).with_name("fixtures") /
            "director-builtin-catalog.json").read_text(encoding="utf-8"))
        self.calls = []
        self.saved_version = 1
        self.fail_next = None
        self.mismatch_readback = False
        self.mismatch_artifact = False
        self.invoke = Mock(side_effect=self.fake_cli)

    @staticmethod
    def flags(args):
        return dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)

    def fake_cli(self, args):
        self.calls.append(list(args))
        flags = self.flags(args)
        verb = args[1]
        if verb == "catalog":
            return {"ok": True, "data": self.catalog}
        if verb == "export":
            self.assertEqual(flags["type"], "fbx")
            self.assertNotIn("to-canvas", flags)
            output = Path(flags["output"])
            self.assertFalse(output.exists())
            content = b"Kaydara FBX Binary  \x00\x1a\x00" + bytes(range(256)) * 3
            output.write_bytes(content)
            return {"ok": True, "data": {"path": str(output), "format": "fbx", "byteSize": len(content),
                "sha256": "incorrect" if self.mismatch_artifact else hashlib.sha256(content).hexdigest(),
                "source": {"projectId": PROJECT, "nodeId": NODE, "versionId": str(self.saved_version)}}}
        file = Path(flags["file"]) if "file" in flags else None
        if verb in ("create", "pull"):
            self.assertFalse(file.exists(), "CLI create/pull requires a new managed file")
            file.write_text(json.dumps({"projectId": PROJECT, "nodeId": NODE, "testContent": "initial"}))
        if self.fail_next and verb == self.fail_next:
            self.fail_next = None
            recovery = file if file else self.root / "upstream-recovery.json"
            recovery.write_text(json.dumps({"pending": {"writeId": "test-write"}, "nodeId": NODE}))
            return {"ok": False, "error": {"code": "test.interrupted",
                "details": {"operationRef": str(recovery)}}}
        if verb == "apply":
            commands_file = Path(flags["input"])
            self.assertTrue(commands_file.is_relative_to(self.root))
            self.assertEqual(commands_file.stat().st_mode & 0o777, 0o600)
            commands = json.loads(commands_file.read_text())
            if file:
                content = json.loads(file.read_text())
                content["commands"] = commands
                file.write_text(json.dumps(content))
        cloud_write = verb in ("create", "push") or verb == "apply" and file is None
        if cloud_write:
            self.saved_version += 1
        data = {"projectId": PROJECT, "nodeId": NODE, "versionId": str(self.saved_version),
                "savedDraftVersion": self.saved_version, "webUrl": "https://jimeng.example.test/canvas"}
        if file:
            data["file"] = str(file)
            if verb == "show":
                data["document"] = json.loads(file.read_text())
        elif verb == "show" and self.mismatch_readback:
            data["versionId"] = "different-version"
        return {"ok": True, "data": data}

    def execute(self, payload, job):
        return actions.execute(payload, job, invoke=self.invoke)

    def create(self, job="director-create"):
        return self.execute({"action": "director.create", "projectId": PROJECT,
                             "title": "Managed Scene", "dryRun": False}, job)

    def test_create_draft_edit_push_reads_back_and_never_exposes_paths(self):
        created = self.create()
        self.assertTrue(created["workspaceSaveVerified"])
        self.assertTrue(created["workspaceMutationAttempted"])
        draft = created["draftId"]
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual([a[:2] for a in self.calls], [["director", "create"], ["director", "show"]])
        changed = self.execute({"action": "director.draft.apply", "projectId": PROJECT,
            "draftId": draft, "commands": [{"type": "document.title", "title": "Updated scene"}],
            "dryRun": False}, "director-local-edit")
        self.assertFalse(changed["workspaceMutationAttempted"])
        self.assertTrue(changed["localStateMutationAttempted"])
        shown = self.execute({"action": "director.draft.get", "projectId": PROJECT, "draftId": draft}, "read")
        self.assertEqual(shown["upstream"]["data"]["document"]["commands"][0]["title"], "Updated scene")
        pushed = self.execute({"action": "director.draft.push", "projectId": PROJECT,
                               "draftId": draft, "dryRun": False}, "director-push")
        self.assertTrue(pushed["workspaceSaveVerified"])
        viewed = self.execute({"action": "director.operation.get", "operationId": pushed["operationId"]}, "status")
        self.assertEqual(viewed["operation"]["state"], "complete")
        self.assertNotIn(str(self.root), json.dumps([created, changed, shown, pushed, viewed]))
        self.assertFalse(any("--force" in call or "--yes" in call for call in self.calls))

    def test_direct_apply_checks_real_catalog_presets_then_saves_and_reads_back(self):
        result = self.execute({"action": "director.apply", "projectId": PROJECT, "nodeId": NODE,
            "commands": [{"type": "object.preset", "preset": "chair", "id": "chair1"},
                         {"type": "workspace.performance", "copyStaging": True},
                         {"type": "timeline.set", "durationFrames": 60}],
            "dryRun": False}, "direct-apply")
        self.assertTrue(result["workspaceSaveVerified"])
        self.assertEqual([a[:2] for a in self.calls],
                         [["director", "catalog"], ["director", "apply"], ["director", "show"]])

    def test_catalog_mismatch_cannot_mutate_or_accept_unverified_presets(self):
        result = self.execute({"action": "director.apply", "projectId": PROJECT, "nodeId": NODE,
            "commands": [{"type": "object.preset", "preset": "not-in-real-catalog", "id": "unknown"}],
            "dryRun": False}, "bad-preset")
        self.assertEqual(result["error"]["code"], "DIRECTOR_CATALOG_MISMATCH")
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertEqual([a[:2] for a in self.calls], [["director", "catalog"]])

    def test_same_operation_is_replayed_without_second_write_and_changed_payload_is_rejected(self):
        first = self.create()
        self.invoke.reset_mock()
        second = self.create()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["draftId"], second["draftId"])
        self.invoke.assert_not_called()
        with self.assertRaises(ValueError):
            self.execute({"action": "director.create", "projectId": PROJECT,
                          "title": "different", "dryRun": False}, "director-create")
        self.invoke.assert_not_called()

    def test_pull_creates_a_new_bound_draft_and_rejects_project_mismatch(self):
        pulled = self.execute({"action": "director.draft.pull", "projectId": PROJECT,
            "nodeId": NODE, "versionId": "1", "dryRun": False}, "pull")
        self.assertFalse(pulled["workspaceMutationAttempted"])
        self.assertTrue(pulled["localStateMutationAttempted"])
        self.assertIn("--version-id=1", self.calls[-1])
        with self.assertRaises(ValueError):
            self.execute({"action": "director.draft.get", "projectId": RESOURCE,
                          "draftId": pulled["draftId"]}, "mismatched-project")

    def test_missing_or_changed_readback_never_claims_saved_or_repeats_mutation(self):
        self.mismatch_readback = True
        result = self.create()
        self.assertFalse(result["workspaceSaveVerified"])
        self.assertEqual(result["error"]["code"], "DIRECTOR_SAVE_UNVERIFIED")
        self.invoke.reset_mock()
        repeated = self.create()
        self.assertTrue(repeated["replayed"])
        self.invoke.assert_not_called()

    def test_official_recovery_file_resumes_push_and_marks_original_complete(self):
        self.fail_next = "create"
        failed = self.create()
        original = failed["operationId"]
        self.assertEqual(failed["recovery"]["nextAction"], "director.resume")
        state = self.execute({"action": "director.operation.get", "operationId": original}, "status")
        self.assertTrue(state["operation"]["resumable"])
        self.calls.clear()
        resumed = self.execute({"action": "director.resume", "projectId": PROJECT,
            "operationId": original, "dryRun": False}, "resume")
        self.assertTrue(resumed["workspaceSaveVerified"])
        self.assertEqual([a[:2] for a in self.calls], [["director", "push"], ["director", "show"]])
        state = self.execute({"action": "director.operation.get", "operationId": original}, "status2")
        self.assertEqual(state["operation"]["state"], "complete")
        self.invoke.reset_mock()
        done = self.execute({"action": "director.resume", "projectId": PROJECT,
            "operationId": original, "dryRun": False}, "resume-new-id")
        self.assertTrue(done["alreadyComplete"])
        self.invoke.assert_not_called()

    def test_recovery_rejects_changed_work_file_without_replaying_write(self):
        self.fail_next = "create"
        failed = self.create()
        record = json.loads((self.root / failed["operationId"] / "operation.json").read_text())
        Path(record["recoveryFile"]).write_text("changed after failed operation")
        self.invoke.reset_mock()
        result = self.execute({"action": "director.resume", "projectId": PROJECT,
            "operationId": failed["operationId"], "dryRun": False}, "tampered-resume")
        self.assertEqual(result["error"]["code"], "DIRECTOR_MANAGED_INPUT_REJECTED")
        self.invoke.assert_not_called()

    def test_uncertain_operation_stays_unknown_and_cannot_recreate(self):
        self.invoke.side_effect = TimeoutError("private runtime details")
        with self.assertRaises(RuntimeError):
            self.create()
        operation = actions.recovery({"action": "director.create", "projectId": PROJECT}, "director-create")["operationId"]
        state = self.execute({"action": "director.operation.get", "operationId": operation}, "status")
        self.assertEqual(state["operation"]["state"], "unknown")
        self.invoke.reset_mock()
        result = self.create()
        self.assertEqual(result["error"]["code"], "DIRECTOR_RECONCILIATION_REQUIRED")
        self.invoke.assert_not_called()

    def test_dryrun_never_creates_director_files_and_insecure_directory_is_rejected(self):
        result = self.execute({"action": "director.create", "projectId": PROJECT}, "plan")
        self.assertEqual(result["dryRunKind"], "bridge-local-plan")
        self.assertFalse(self.root.exists())
        self.root.mkdir(mode=0o755)
        self.root.chmod(0o755)
        result = self.create()
        self.assertEqual(result["error"]["code"], "DIRECTOR_STORAGE_UNAVAILABLE")
        self.invoke.assert_not_called()

    def test_fbx_export_returns_integrity_checked_chunked_artifact_without_browser(self):
        payload = {"action": "director.fbx.export", "projectId": PROJECT,
                   "nodeId": NODE, "motionId": "official-walk", "dryRun": False}
        exported = self.execute(payload, "fbx-export")
        artifact = exported["artifact"]
        self.assertFalse(exported["workspaceMutationAttempted"])
        self.assertTrue(exported["localStateMutationAttempted"])
        self.assertIn("--type=fbx", self.calls[0])
        self.assertNotIn("--to-canvas", self.calls[0])
        self.assertNotIn(str(self.root), json.dumps(exported))
        data = bytearray()
        offset = 0
        self.invoke.reset_mock()
        while True:
            result = self.execute({"action": "director.artifact.read", "projectId": PROJECT,
                "artifactId": artifact["artifactId"], "offset": offset, "length": 137}, "read")
            part = result["artifact"]
            chunk = base64.b64decode(part["data"], validate=True)
            self.assertEqual(part["length"], len(chunk))
            data.extend(chunk)
            if part["eof"]:
                self.assertIsNone(part["nextOffset"])
                break
            offset = part["nextOffset"]
        self.assertEqual(len(data), artifact["byteSize"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), artifact["sha256"])
        self.invoke.assert_not_called()
        replay = self.execute(payload, "fbx-export")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["artifact"], artifact)
        self.invoke.assert_not_called()

    def test_fbx_export_supports_managed_draft_and_rejects_ambiguous_or_external_sources(self):
        created = self.create()
        payload = {"action": "director.fbx.export", "projectId": PROJECT,
                   "draftId": created["draftId"], "motionId": "local-walk", "dryRun": False}
        result = self.execute(payload, "draft-fbx")
        self.assertIn("artifact", result)
        self.assertTrue(any(arg.startswith("--file=") for arg in self.calls[-1]))
        self.invoke.reset_mock()
        for changed in ({**payload, "nodeId": NODE}, {**payload, "file": "/etc/passwd"},
                        {**payload, "output": "/tmp/arbitrary.fbx"},
                        {k: v for k, v in payload.items() if k != "draftId"}):
            with self.assertRaises(ValueError):
                self.execute(changed, "invalid-source")
        self.invoke.assert_not_called()

    def test_fbx_wrong_receipt_scope_or_modified_file_is_never_delivered(self):
        payload = {"action": "director.fbx.export", "projectId": PROJECT,
                   "nodeId": NODE, "motionId": "walk", "dryRun": False}
        self.mismatch_artifact = True
        result = self.execute(payload, "wrong-receipt")
        self.assertEqual(result["error"]["code"], "DIRECTOR_ARTIFACT_UNVERIFIED")
        self.assertNotIn("artifact", result)
        self.mismatch_artifact = False
        exported = self.execute(payload, "valid-receipt")
        read = {"action": "director.artifact.read", "projectId": PROJECT,
                "artifactId": exported["artifact"]["artifactId"]}
        for changed in ({**read, "projectId": RESOURCE}, {**read, "offset": 64 * 1024 * 1024},
                        {**read, "length": 65537}, {**read, "file": "/etc/passwd"}):
            with self.assertRaises(ValueError):
                self.execute(changed, "bad-read")
        record = json.loads((self.root / read["artifactId"] / "operation.json").read_text())
        Path(record["artifactFile"]).write_bytes(b"tampered")
        with self.assertRaises(ValueError):
            self.execute(read, "tampered-read")


class ResourceManagedActionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = (Path(temporary.name) / "private-resources").resolve()
        self.env = patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_RESOURCE_STATE_DIR": str(self.root),
                                          "DREAMINA_CANVAS_PROFILE": "test-media-profile"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.calls = []
        self.media = {}
        self.read_type = "image"
        self.read_status = "success"
        self.read_format = "png"
        self.bad_upload_receipt = False
        self.bad_download_receipt = False
        self.invoke = Mock(side_effect=self.fake_cli)

    def fake_cli(self, args):
        self.calls.append(list(args))
        flags = DirectorManagedActionsTests.flags(args)
        self.assertEqual(flags["project-id"], PROJECT)
        verb = args[1]
        if verb == "upload":
            source = Path(flags["file"])
            self.assertTrue(source.is_relative_to(self.root))
            resource = flags["resource-id"]
            self.media[resource] = source.read_bytes()
            return {"ok": True, "data": {"resourceId": RESOURCE if self.bad_upload_receipt else resource,
                                           "mediaType": flags["type"]}}
        resource = args[2]
        if verb == "get":
            return {"ok": True, "data": {"resourceId": resource, "type": self.read_type,
                "status": self.read_status, "image": {"format": self.read_format} if self.read_format else {"width": 16, "height": 16}}}
        if verb == "download":
            output = Path(flags["output"])
            self.assertTrue(output.is_relative_to(self.root))
            self.assertFalse(output.exists())
            content = self.media.get(resource, b"\x89PNG\r\n\x1a\nfixture-image")
            output.write_bytes(content)
            return {"ok": True, "data": {"resourceId": resource, "path": str(output),
                "size": len(content), "sha256": "incorrect" if self.bad_download_receipt else hashlib.sha256(content).hexdigest()}}
        raise AssertionError("Unexpected media command")

    def execute(self, payload, job="media-job"):
        return actions.execute(payload, job, invoke=self.invoke)

    @staticmethod
    def direct(content=b"\x89PNG\r\n\x1a\nfixture-image"):
        return {"action": "resource.upload", "projectId": PROJECT, "mediaType": "image", "fileExtension": "png",
                "name": "Character reference", "contentBase64": base64.b64encode(content).decode(),
                "sha256": hashlib.sha256(content).hexdigest(), "dryRun": False}

    @staticmethod
    def stage(full, chunk, offset=0, upload_id=None):
        return {"action": "resource.upload.stage", "projectId": PROJECT, "mediaType": "image", "fileExtension": "png",
                "byteSize": len(full), "sha256": hashlib.sha256(full).hexdigest(),
                "contentBase64": base64.b64encode(chunk).decode(), "chunkSha256": hashlib.sha256(chunk).hexdigest(),
                "offset": offset, "dryRun": False, **({"uploadId": upload_id} if upload_id else {})}

    def test_direct_upload_is_hash_checked_uses_stable_resource_id_and_reads_back(self):
        payload = self.direct()
        result = self.execute(payload)
        self.assertTrue(result["resourceVerified"])
        self.assertTrue(result["resourceReady"])
        self.assertTrue(result["workspaceMutationAttempted"])
        self.assertEqual([call[:2] for call in self.calls], [["resource", "upload"], ["resource", "get"]])
        self.assertIn("--resource-id=" + result["resourceId"], self.calls[0])
        self.assertEqual(result["resourceId"], result["recovery"]["resourceId"])
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn(payload["contentBase64"], json.dumps(result))
        self.invoke.reset_mock()
        self.assertTrue(self.execute(payload)["replayed"])
        self.invoke.assert_not_called()

    def test_dryrun_never_writes_files_or_calls_cli_and_readonly_blocks_upload(self):
        payload = self.direct()
        result = self.execute({k: v for k, v in payload.items() if k != "dryRun"})
        self.assertEqual(result["dryRunKind"], "bridge-local-plan")
        self.assertFalse(self.root.exists())
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "1"}):
            with self.assertRaises(ValueError):
                self.execute(payload)
        self.invoke.assert_not_called()

    def test_upload_rejects_bad_hash_base64_type_path_and_url_without_files(self):
        payload = self.direct()
        invalid = ({"sha256": "0" * 64}, {"contentBase64": "not base64!"}, {"fileExtension": "mp3"},
                   {"file": "/etc/passwd"}, {"sourceUrl": "https://example.com/image.png"},
                   {"contentBase64": base64.b64encode(b"x" * (actions.UPLOAD_CHUNK_BYTES + 1)).decode()})
        for changes in invalid:
            with self.assertRaises(ValueError):
                self.execute({**payload, **changes})
        self.assertFalse(self.root.exists())
        self.invoke.assert_not_called()

    def test_chunked_upload_preserves_bytes_checks_complete_hash_and_uses_one_resource_identity(self):
        content = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2700
        chunk = content[:actions.UPLOAD_CHUNK_BYTES]
        first = self.execute(self.stage(content, chunk), "stage-first")
        upload_id = first["upload"]["uploadId"]
        self.assertFalse(first["upload"]["ready"])
        self.assertEqual(first["upload"]["nextOffset"], len(chunk))
        second_payload = self.stage(content, content[len(chunk):], len(chunk), upload_id)
        second = self.execute(second_payload, "stage-second")
        self.assertTrue(second["upload"]["ready"])
        self.assertEqual(second["recovery"]["operationId"], upload_id)
        replay = self.execute(second_payload, "stage-replayed-different-job")
        self.assertTrue(replay["replayedChunk"])
        self.invoke.assert_not_called()
        upload = {"action": "resource.upload", "projectId": PROJECT, "uploadId": upload_id, "dryRun": False}
        uploaded = self.execute(upload, "upload-staged")
        self.assertEqual(self.media[uploaded["resourceId"]], content)
        self.assertEqual(actions.recovery(upload, "another-job")["resourceId"], uploaded["resourceId"])
        self.assertLess(len(json.dumps(second_payload).encode()), 1024 * 1024)

    def test_chunks_cannot_overwrite_skip_metadata_or_cross_project(self):
        full = b"abcdefgh"
        first = self.execute(self.stage(full, full[:4]), "first")
        upload_id = first["upload"]["uploadId"]
        for payload in (self.stage(full, b"xxxx", 0, upload_id),
                        self.stage(full, b"h", 7, upload_id),
                        {**self.stage(full, full[4:], 4, upload_id), "name": "changed"},
                        {**self.stage(full, full[4:], 4, upload_id), "projectId": RESOURCE}):
            with self.assertRaises(ValueError):
                self.execute(payload, "bad-chunk")
        inspected = self.execute({"action": "resource.upload.inspect", "projectId": PROJECT, "uploadId": upload_id}, "inspect")
        self.assertEqual(inspected["upload"]["receivedBytes"], 4)
        blocked = self.execute({"action": "resource.upload", "projectId": PROJECT, "uploadId": upload_id, "dryRun": False}, "incomplete")
        self.assertEqual(blocked["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.invoke.assert_not_called()

    def test_full_hash_mismatch_and_changed_staged_file_never_upload(self):
        payload = self.stage(b"good", b"evil")
        result = self.execute(payload, "wrong-hash")
        self.assertEqual(result["error"]["code"], "UPLOAD_HASH_MISMATCH")
        blocked = self.execute({"action": "resource.upload", "projectId": PROJECT,
            "uploadId": result["upload"]["uploadId"], "dryRun": False}, "bad-full-hash")
        self.assertEqual(blocked["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        valid = self.execute(self.stage(b"good", b"good"), "good-stage")
        record = json.loads((self.root / valid["upload"]["uploadId"] / "operation.json").read_text())
        Path(record["uploadFile"]).write_bytes(b"changed")
        blocked = self.execute({"action": "resource.upload", "projectId": PROJECT,
            "uploadId": valid["upload"]["uploadId"], "dryRun": False}, "changed-file")
        self.assertEqual(blocked["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.invoke.assert_not_called()

    def test_upload_identity_mismatch_does_not_claim_verified_and_running_does_not_claim_ready(self):
        self.bad_upload_receipt = True
        result = self.execute(self.direct(), "wrong-resource")
        self.assertEqual(result["error"]["code"], "RESOURCE_UPLOAD_UNVERIFIED")
        self.assertFalse(result["resourceVerified"])
        self.assertEqual(len(self.calls), 1)
        self.bad_upload_receipt = False
        self.read_status = "running"
        result = self.execute(self.direct(), "running-resource")
        self.assertTrue(result["resourceVerified"])
        self.assertFalse(result["resourceReady"])

    def test_image_download_exposes_verified_chunked_bytes_not_server_paths(self):
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE, "dryRun": False}, "download")
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertIn("artifact", result)
        self.assertEqual([call[:2] for call in self.calls], [["resource", "get"], ["resource", "download"]])
        artifact = self.execute({"action": "resource.artifact.read", "projectId": PROJECT,
            "artifactId": result["artifact"]["artifactId"]}, "download-read")["artifact"]
        content = base64.b64decode(artifact["data"], validate=True)
        self.assertEqual(hashlib.sha256(content).hexdigest(), result["artifact"]["sha256"])
        self.assertTrue(artifact["eof"])
        self.assertTrue(artifact["filename"].endswith(".png"))
        self.assertNotIn(str(self.root), json.dumps(result))
        with self.assertRaises(ValueError):
            self.execute({"action": "resource.artifact.read", "projectId": "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff",
                "artifactId": result["artifact"]["artifactId"]}, "other-project")

    def test_download_rejects_unsupported_pending_resources_and_wrong_hash_receipt(self):
        payload = {"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE, "dryRun": False}
        for kind, state in (("text", "success"), ("audio", "running"), ("video", "running"), ("image", "running")):
            self.read_type, self.read_status = kind, state
            self.calls.clear()
            result = self.execute(payload, kind + state)
            self.assertEqual(result["error"]["code"], "RESOURCE_MEDIA_NOT_READY")
            self.assertEqual(len(self.calls), 1)
        self.read_type, self.read_status = "image", "success"
        self.bad_download_receipt = True
        result = self.execute(payload, "wrong-download-hash")
        self.assertEqual(result["error"]["code"], "RESOURCE_DOWNLOAD_UNVERIFIED")
        self.assertNotIn("artifact", result)

    def test_download_sniffs_png_when_real_resource_read_omits_format(self):
        self.read_format = None
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "format-omitted")
        self.assertEqual(result["artifact"]["format"], "png")
        self.assertTrue(result["artifact"]["filename"].endswith(".png"))

    def test_uncertain_upload_preserves_recovery_identity_and_never_auto_reuploads(self):
        self.invoke.side_effect = TimeoutError("private downstream state")
        with self.assertRaises(RuntimeError):
            self.execute(self.direct())
        recovery = actions.recovery(self.direct(), "media-job")
        state = self.execute({"action": "resource.operation.get", "operationId": recovery["operationId"]}, "inspect")
        self.assertEqual(state["operation"]["state"], "unknown")
        self.invoke.reset_mock()
        result = self.execute(self.direct())
        self.assertEqual(result["error"]["code"], "RESOURCE_RECONCILIATION_REQUIRED")
        self.invoke.assert_not_called()

    def mock_video_network(self, *, content=None, envelope=None, status=200, headers=None, open_error=None):
        import web_actions
        if content is None:
            content = (24).to_bytes(4, "big") + b"ftypisom" + b"\x00\x00\x02\x00isomiso2" + (12).to_bytes(4, "big") + b"mdatDATA"
        if envelope is None:
            envelope = {"ret": "0", "data": {"resources": [{"resource_id": RESOURCE,
                "type": 2, "status": 1000, "video": {"vid": "vtest_media_id", "width": 1920, "height": 1080, "duration": 4.05}}],
                "media_urls": {"vtest_media_id": {"download_url": "https://v3-dreamina-de.jianying.com/video.mp4?signature=private-cdn-signature",
                                                    "original_format": "mp4"}}}}
        session = {"cookie": "sessionid=private-browser-cookie", "userAgent": "private-browser-user-agent",
                   "headers": {"origin": "https://jimeng.jianying.com", "uifid": "private-browser-id"}, "actionHeaders": {}}
        self.web_calls = []
        def send(path, body, headers):
            self.web_calls.append((path, body, dict(headers)))
            return 200, json.dumps(envelope).encode()
        class Response(io.BytesIO):
            code = status
            def __init__(inner):
                super().__init__(content)
                inner.headers = {"Content-Type": "video/mp4", "Content-Length": str(len(content)), **(headers or {})}
        opener = Mock()
        opener.open.side_effect = open_error if open_error else lambda *_args, **_kwargs: Response()
        patches = [patch.object(web_actions, "_load_session", return_value=session),
                   patch.object(web_actions, "_send", side_effect=send),
                   patch("urllib.request.build_opener", return_value=opener)]
        handles = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        self.read_type = "video"
        return content, envelope, opener, handles[-1]

    def test_video_download_uses_exact_authenticated_resource_then_credential_free_cdn(self):
        content, _, opener, build = self.mock_video_network()
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "video-download")
        self.assertEqual(result["artifact"]["format"], "mp4")
        self.assertTrue(result["artifact"]["filename"].endswith(".mp4"))
        self.assertEqual([call[:2] for call in self.calls], [["resource", "get"]])
        self.assertEqual(self.web_calls[0][:2], ("/octo_api/v1/resource/batch_get", {"project_id": PROJECT, "resource_ids": [RESOURCE]}))
        self.assertEqual(self.web_calls[0][2]["Cookie"], "sessionid=private-browser-cookie")
        request = opener.open.call_args.args[0]
        sent = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(set(sent), {"accept", "user-agent"})
        self.assertNotIn("private-browser", json.dumps(sent))
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(type(build.call_args.args[1]).__name__, "_NoRedirect")
        serialized = json.dumps(result)
        self.assertNotIn("private-cdn-signature", serialized)
        self.assertNotIn("private-browser-cookie", serialized)
        self.assertNotIn("download_url", serialized)
        part = self.execute({"action": "resource.artifact.read", "projectId": PROJECT,
            "artifactId": result["artifact"]["artifactId"]}, "video-chunk")["artifact"]
        self.assertEqual(base64.b64decode(part["data"]), content)
        self.assertEqual(part["sha256"], hashlib.sha256(content).hexdigest())

    def test_video_download_rejects_untrusted_urls_before_any_cdn_request(self):
        _, envelope, opener, _ = self.mock_video_network()
        media = envelope["data"]["media_urls"]["vtest_media_id"]
        for number, url in enumerate(("http://v3-dreamina-de.jianying.com/a.mp4", "https://example.com/a.mp4",
                "https://v3-dreamina-de.jianying.com.evil.test/a.mp4", "https://user:pass@v3-dreamina-de.jianying.com/a.mp4",
                "https://v3-dreamina-de.jianying.com:444/a.mp4", "https://v3-dreamina-de.jianying.com/a.mp4#part",
                "https://v3-dreamina-de.jianying.com/a.mp4\nheader")):
            media["download_url"] = url
            result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                                   "dryRun": False}, "bad-video-url-" + str(number))
            self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
            self.assertNotIn("artifact", result)
        opener.open.assert_not_called()

    def test_video_download_accepts_the_second_exact_observed_rotating_cdn(self):
        _, envelope, opener, _ = self.mock_video_network()
        envelope["data"]["media_urls"]["vtest_media_id"]["download_url"] = "https://v26-dreamina-de.jianying.com/video.mp4?private=cdn-value"
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "video-v26")
        self.assertEqual(result["upstream"]["data"]["downloadHost"], "v26-dreamina-de.jianying.com")
        self.assertEqual(result["artifact"]["format"], "mp4")
        self.assertEqual({key.lower() for key, _ in opener.open.call_args.args[0].header_items()}, {"accept", "user-agent"})

    def test_video_download_checks_exact_resource_and_vid_mapping(self):
        _, envelope, opener, _ = self.mock_video_network()
        resource = envelope["data"]["resources"][0]
        for number, changes in enumerate(({"resource_id": PROJECT}, {"status": 1}, {"type": 1},
                                         {"video": {"vid": "unmapped"}})):
            original = dict(resource)
            resource.update(changes)
            result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                                   "dryRun": False}, "bad-video-mapping-" + str(number))
            self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
            resource.clear()
            resource.update(original)
        opener.open.assert_not_called()

    def test_video_download_rejects_redirect_response_wrong_magic_and_oversize_length(self):
        self.mock_video_network(content=b"<html>not mp4</html>")
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "wrong-mp4-header")
        self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.assertFalse(list(self.root.glob("*/resource.mp4")))

    def test_video_download_never_follows_redirect_or_leaves_partial_file(self):
        self.mock_video_network(status=302, headers={"Location": "https://untrusted.example/video.mp4"})
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "cdn-redirect")
        self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.assertFalse(list(self.root.glob("*/resource.mp4")))

    def test_video_download_honors_announced_and_streamed_size_bounds(self):
        _, _, opener, _ = self.mock_video_network(headers={"Content-Length": str(actions.UPLOAD_MAX_BYTES + 1)})
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                               "dryRun": False}, "cdn-oversize")
        self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.assertFalse(list(self.root.glob("*/resource.mp4")))
        response = io.BytesIO(b"x" * 129)
        response.code = 200
        response.headers = {"Content-Type": "video/mp4"}
        opener.open.side_effect = None
        opener.open.return_value = response
        with patch.object(actions, "UPLOAD_MAX_BYTES", 128):
            result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                                   "dryRun": False}, "cdn-stream-oversize")
        self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.assertFalse(list(self.root.glob("*/resource.mp4")))

    def mock_audio_network(self, *, content=b"\xff\xfb\x90\x00MP3-frame-fixture", **kwargs):
        envelope = {"ret": "0", "data": {"resources": [{"resource_id": RESOURCE,
            "type": 3, "status": 1000, "audio": {"vid": "saved-audio-id", "duration": 4.56}}],
            "media_urls": {"saved-audio-id": {"download_url": "https://v26-dreamina-de.jianying.com/audio?signature=private-audio-signature",
                                               "original_format": "mp3"}}}}
        result = self.mock_video_network(content=content, envelope=envelope,
            headers={"Content-Type": "audio/mpeg", **kwargs.pop("headers", {})}, **kwargs)
        self.read_type = "audio"
        return result

    def test_mp3_download_verifies_exact_audio_without_exposing_urls_or_credentials(self):
        content, _, opener, _ = self.mock_audio_network()
        payload = {"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE, "dryRun": False}
        result = self.execute(payload, "audio-download")
        artifact = result["artifact"]
        self.assertEqual((artifact["format"], artifact["filename"]), ("mp3", RESOURCE + ".mp3"))
        self.assertEqual([call[:2] for call in self.calls], [["resource", "get"]])
        self.assertEqual(self.web_calls[0][:2], ("/octo_api/v1/resource/batch_get",
            {"project_id": PROJECT, "resource_ids": [RESOURCE]}))
        self.assertFalse(result["generationSubmitted"])
        self.assertFalse(result["workspaceMutationAttempted"])
        self.assertEqual(result["upstream"]["data"]["audioId"], "saved-audio-id")
        self.assertEqual({key.lower() for key, _ in opener.open.call_args.args[0].header_items()}, {"accept", "user-agent"})
        serialized = json.dumps(result)
        for private in ("private-audio-signature", "private-browser-cookie", "download_url", str(self.root)):
            self.assertNotIn(private, serialized)
        part = self.execute({"action": "resource.artifact.read", "projectId": PROJECT,
            "artifactId": artifact["artifactId"]}, "audio-chunk")["artifact"]
        self.assertEqual(base64.b64decode(part["data"]), content)
        descriptor = actions.artifact_descriptor({"projectId": PROJECT, "artifactId": artifact["artifactId"]})
        self.assertEqual(descriptor["format"], "mp3")
        self.assertEqual(descriptor["sha256"], hashlib.sha256(content).hexdigest())
        self.calls.clear()
        self.assertTrue(self.execute(payload, "audio-download")["replayed"])
        self.assertEqual(self.calls, [])

    def test_audio_download_rejects_unverified_encoding_or_identity_before_cdn(self):
        _, envelope, opener, _ = self.mock_audio_network()
        original = json.loads(json.dumps(envelope))
        variants = []
        for change in ({"resource_id": PROJECT}, {"type": 2}, {"status": 1}, {"audio": {"vid": "other"}}):
            candidate = json.loads(json.dumps(original))
            candidate["data"]["resources"][0].update(change)
            variants.append(candidate)
        for change in ({"original_format": "wav"}, {"download_url": "https://example.com/a.mp3"},
                       {"download_url": "https://v26-dreamina-de.jianying.com:444/a.mp3"},
                       {"download_url": "https://u:p@v26-dreamina-de.jianying.com/a.mp3"}):
            candidate = json.loads(json.dumps(original))
            candidate["data"]["media_urls"]["saved-audio-id"].update(change)
            variants.append(candidate)
        for index, variant in enumerate(variants):
            envelope.clear(); envelope.update(variant)
            result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                "dryRun": False}, "audio-mismatch-" + str(index))
            self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
            self.assertNotIn("artifact", result)
        opener.open.assert_not_called()

    def test_audio_download_rejects_non_mp3_bytes_and_deletes_partial_file(self):
        # ADTS AAC shares MPEG sync bits but is not an accepted MP3 stream.
        for index, content in enumerate((b"<html>not audio</html>", b"\xff\xf1\x50\x80AAC-body", b"ID3")):
            with self.subTest(index=index):
                self.mock_audio_network(content=content)
                result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
                    "dryRun": False}, "bad-audio-header-" + str(index))
                self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
                self.assertNotIn("artifact", result)
        self.assertFalse(list(self.root.glob("*/resource.mp3")))

    def test_audio_download_accepts_id3_and_rejects_mime_mismatch(self):
        content = b"ID3\x04\x00\x00\x00\x00\x00\x00\xff\xfb\x90\x00body"
        self.mock_audio_network(content=content)
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
            "dryRun": False}, "id3-audio")
        self.assertEqual(result["artifact"]["sha256"], hashlib.sha256(content).hexdigest())
        self.mock_audio_network(headers={"Content-Type": "text/html"})
        result = self.execute({"action": "resource.download", "projectId": PROJECT, "resourceId": RESOURCE,
            "dryRun": False}, "wrong-audio-mime")
        self.assertEqual(result["error"]["code"], "RESOURCE_MANAGED_INPUT_REJECTED")
        self.assertNotIn("artifact", result)


if __name__ == "__main__":
    unittest.main()
