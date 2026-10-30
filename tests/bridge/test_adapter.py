# Fixtures use synthetic identities and material; no user account data.
"""No network or generation: verify the adapter's public CLI boundary."""
import importlib.util
from copy import deepcopy
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ADAPTER_PATH = (Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge") / "adapter.py"
sys.path.insert(0, str(ADAPTER_PATH.parent))
spec = importlib.util.spec_from_file_location("bridge_adapter_under_test", ADAPTER_PATH)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)

PROJECT = "1000000a-0000-4000-8000-000000000001"
NODE = "node_0123456789"
JOB = "f334243f3f1f407fb1ad3a931c762970"


def sample_payload(**changes):
    body = {"action": "video.sample", "projectId": PROJECT,
            "model": "test_canonical_sample", "prompt": "test prompt",
            "mode": "t2v", "dryRun": False, "creditCeiling": 20}
    body.update(changes)
    return body


def generation_match(model="test_canonical_sample", mode="t2v", kind="sample", total_count=None):
    match = {"kind": "generation", "resourceType": "video",
             "generation": {"model": model, "mode": mode, "generationKind": kind}}
    if total_count is not None:
        match["generation"]["materials"] = {"totalCount": total_count}
    return match


def model_response(*matches):
    return {"ok": True, "data": {"matches": list(matches)}}


ACCEPTED = {"ok": True, "data": {"submission": {"state": "accepted", "resources": []}}}


def verified_generation_evidence():
    evidence = {"sampleVerified": True, "promotionVerified": True,
                "completedAt": "2026-10-29T12:50:50Z", "project": {"projectId": PROJECT}, "steps": {}}
    for label, action, kind, suffix, node in (("sample", "video.sample", "sample", "1", NODE),
                                            ("promotion", "video.promote", "final_from_sample", "2", "node_abcdefghjk")):
        resource_id = "00000000-0000-4000-8000-00000000000" + suffix
        submit_id = "10000000-0000-4000-8000-00000000000" + suffix
        resolution = "480p" if label == "sample" else "1080p"
        resource = {"resourceId": resource_id, "type": "video", "status": "success",
                    "generationKind": kind, "video": {"resolution": resolution}}
        evidence[label + "Resources"] = [resource]
        evidence[label + "Identity"] = {"nodeId": node, "submitId": submit_id}
        data = ({"node": {"nodeId": node}, "submission": {"submitId": submit_id}} if label == "sample"
                else {"nodeId": node, "submitId": submit_id, "sourceNodeId": NODE,
                      "sourceResourceId": evidence["sampleResources"][0]["resourceId"], "resourceIds": [resource_id]})
        evidence["steps"][label] = {"status": "COMPLETED", "payload": {"action": action, "projectId": PROJECT,
            "dryRun": False, "resolution": resolution, "model": "seedance_2.5_draft"},
            "result": {"upstream": {"ok": True, "data": data}}}
        evidence["steps"][label + "-status"] = {"status": "COMPLETED", "payload": {
            "action": "operation.status", "projectId": PROJECT, "submitId": submit_id},
            "result": {"upstream": {"ok": True, "data": {"state": "succeeded", "resources": [
                {"resourceId": resource_id, "state": "succeeded"}]}}}}
        evidence["steps"][label + "-resource"] = {"status": "COMPLETED", "payload": {
            "action": "resource.read", "projectId": PROJECT, "resourceId": resource_id},
            "result": {"upstream": {"ok": True, "data": resource}}}
    return evidence


class AdapterBoundaryTests(unittest.TestCase):
    def test_live_verification_is_scoped_to_complete_source_linked_resources(self):
        with patch.object(adapter, "GENERATION_EVIDENCE") as source:
            source.stat.return_value.st_size = 100
            source.read_text.return_value = json.dumps(verified_generation_evidence())
            with patch.object(adapter.acceptance_registry, "verified", return_value=[{"evidence": "evidence/live-generation-acceptance.json", "sha256": hashlib.sha256(source.read_text.return_value.encode()).hexdigest()}]):
                result = adapter._live_generation_verification()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["actions"], ["video.sample", "video.promote"])
        self.assertEqual(result["verifiedAt"], "2026-10-29T12:50:50Z")
        self.assertFalse(result["visualQualityVerified"])

    def test_live_verification_does_not_trust_flags_or_accepted_only_jobs(self):
        original = verified_generation_evidence()
        invalid = []
        changed = deepcopy(original); changed["promotionVerified"] = False; invalid.append(changed)
        changed = deepcopy(original); changed["steps"]["promotion-status"]["result"]["upstream"]["data"]["state"] = "running"; invalid.append(changed)
        changed = deepcopy(original); del changed["steps"]["sample-resource"]; invalid.append(changed)
        changed = deepcopy(original); changed["steps"]["promotion"]["result"]["upstream"]["data"]["sourceNodeId"] = "node_0000000000"; invalid.append(changed)
        changed = deepcopy(original); changed["completedAt"] = "2026-10-29T12:50:50"; invalid.append(changed)
        for report in invalid:
            with self.subTest(report=invalid.index(report)), patch.object(adapter, "GENERATION_EVIDENCE") as source:
                source.stat.return_value.st_size = 100
                source.read_text.return_value = json.dumps(report)
                with patch.object(adapter.acceptance_registry, "verified", return_value=[{"evidence": "evidence/live-generation-acceptance.json", "sha256": hashlib.sha256(source.read_text.return_value.encode()).hexdigest()}]):
                    result = adapter._live_generation_verification()
                self.assertEqual(result["status"], "not_verified")
                self.assertEqual(result["actions"], [])

    def test_authorized_unlimited_sample_uses_official_yes_only_when_live(self):
        with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": "unlimited", "JIMENG_READ_ONLY": "0"}):
            payload = sample_payload()
            payload.pop("creditCeiling")
            with patch.object(adapter, "invoke", side_effect=[model_response(generation_match()), ACCEPTED]) as call:
                adapter.execute(payload, JOB)
            args = call.call_args_list[-1].args[0]
            self.assertIn("--yes", args)
            self.assertNotIn("--credit-ceiling", args)
            payload["dryRun"] = True
            with patch.object(adapter, "invoke", return_value=ACCEPTED) as call:
                adapter.execute(payload, JOB)
            self.assertNotIn("--yes", call.call_args.args[0])

    def test_explicit_request_limit_is_kept_under_unlimited_policy(self):
        with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": "unlimited", "JIMENG_READ_ONLY": "0"}):
            with patch.object(adapter, "invoke", side_effect=[model_response(generation_match()), ACCEPTED]) as call:
                adapter.execute(sample_payload(creditCeiling=5), JOB)
            args = call.call_args_list[-1].args[0]
            self.assertNotIn("--yes", args)
            self.assertEqual(args[args.index("--credit-ceiling") + 1], "5")

    def setUp(self):
        self.environ = patch.dict(os.environ, {"JIMENG_MAX_CREDITS": "50",
                                               "JIMENG_READ_ONLY": "0",
                                               "DREAMINA_CANVAS_BIN": "/not-executed/cli"})
        self.environ.start()
        self.addCleanup(self.environ.stop)

    def test_read_only_blocks_live_canvas_and_generation(self):
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "1"}):
            for body in ({"action": "canvas.create", "name": "test", "dryRun": False},
                         sample_payload(),
                         {"action": "video.promote", "projectId": PROJECT,
                          "nodeId": NODE, "resolution": "1080p", "dryRun": False,
                          "creditCeiling": 10}):
                with self.subTest(action=body['action']):
                    with patch.object(adapter, "invoke") as call, self.assertRaises(ValueError):
                        adapter.execute(body, JOB)
                    call.assert_not_called()

    def test_canvas_create_defaults_to_local_plan_without_cli(self):
        with patch.object(adapter, "invoke") as call:
            result = adapter.execute({"action": "canvas.create", "name": "test"}, JOB)
        call.assert_not_called()
        self.assertEqual(result["dryRunKind"], "bridge-local-plan")
        self.assertFalse(result["generationSubmitted"])
        self.assertFalse(result["workspaceMutationAttempted"])

    def test_sample_capability_count_merges_prompt_and_flag_references(self):
        refs = ["node:" + NODE, "node:" + NODE]
        prompt = "Use {{node:" + NODE + "}} and {{node:node_abcdefghjk}}"
        body = sample_payload(mode="m2v", refs=refs, prompt=prompt)
        self.assertEqual(adapter.effective_reference_count(body), 2)
        response = model_response(generation_match(mode="m2v", total_count={"min": 2, "max": 2}))
        with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
            got = adapter.execute(body, JOB)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(got["upstream"]["ok"])

    def test_error_json_on_stderr_is_preserved(self):
        error = {"schemaVersion": "1", "ok": False,
                 "error": {"code": "cli.authentication_required", "requiredAction": "login"}}
        proc = subprocess.CompletedProcess([], 11, "", json.dumps(error))
        with patch.object(adapter.subprocess, "run", return_value=proc) as run:
            got = adapter.invoke(["model", "list", "--type", "video"])
        self.assertFalse(got["ok"])
        self.assertEqual(got["exitCode"], 11)
        self.assertEqual(got["error"]["requiredAction"], "login")
        self.assertIsInstance(run.call_args.args[0], list)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_missing_dry_run_defaults_to_no_generation(self):
        body = sample_payload()
        del body["dryRun"]
        del body["creditCeiling"]
        with patch.object(adapter, "invoke", return_value={"ok": True, "data": {"dryRun": True}}) as call:
            got = adapter.execute(body, JOB)
        self.assertEqual(call.call_count, 1)
        self.assertIn("--dry-run", call.call_args.args[0])
        self.assertTrue(got["dryRun"])

    def test_live_requires_explicit_credit_ceiling_within_operator_cap(self):
        for value in [None, 0, -1, 51, True]:
            with self.subTest(value=value):
                body = sample_payload(creditCeiling=value)
                with patch.object(adapter, "invoke") as call, self.assertRaises(ValueError):
                    adapter.execute(body, JOB)
                call.assert_not_called()

    def test_model_lookup_failure_never_submits_generation(self):
        failure = {"ok": False, "exitCode": 11, "error": {"requiredAction": "login"}}
        with patch.object(adapter, "invoke", return_value=failure) as call:
            got = adapter.execute(sample_payload(), JOB)
        self.assertFalse(got["submitted"])
        self.assertEqual(call.call_count, 1)

    def test_ordinary_generation_never_substitutes_for_sample(self):
        with patch.object(adapter, "invoke", return_value=model_response(generation_match(kind="normal"))) as call:
            got = adapter.execute(sample_payload(), JOB)
        self.assertFalse(got["submitted"])
        self.assertEqual(call.call_count, 1)

    def test_generated_node_id_is_stable_and_crockford_compatible(self):
        captured = []
        for unused in range(2):
            with patch.object(adapter, "invoke", return_value=ACCEPTED) as call:
                adapter.execute(sample_payload(dryRun=True), JOB)
            args = call.call_args.args[0]
            captured.append(args[args.index("--node-id") + 1])
        self.assertEqual(captured[0], captured[1])
        self.assertRegex(captured[0], r"^node_[0123456789abcdefghjkmnpqrstvwxyz]{10}$")

    def test_valid_sample_keeps_ceiling_and_pending_upstream_status(self):
        with patch.object(adapter, "invoke", side_effect=[model_response(generation_match()), ACCEPTED]) as call:
            got = adapter.execute(sample_payload(), JOB)
        args = call.call_args.args[0]
        self.assertEqual(args[args.index("--credit-ceiling") + 1], "20")
        self.assertIn("--submit-id", args)
        self.assertNotIn("--yes", args)
        self.assertNotIn("--wait", args)
        self.assertEqual(got["upstream"]["data"]["submission"]["state"], "accepted")
        self.assertNotIn("mediaCompleted", got)

    def test_promote_has_required_source_resolution_and_no_run_flag(self):
        with patch.object(adapter, "invoke", return_value=ACCEPTED) as call:
            adapter.execute({"action": "video.promote", "projectId": PROJECT,
                             "nodeId": NODE, "resolution": "720P", "dryRun": False,
                             "creditCeiling": 20}, JOB)
        args = call.call_args.args[0]
        self.assertEqual(args[:3], ["node", "promote", "video-sample"])
        self.assertNotIn("--run", args)
        self.assertEqual(args[args.index("--resolution") + 1], "720P")

    def test_matching_sample_mode_is_selected_from_multimode_model(self):
        response = model_response(generation_match(mode="t2v"), generation_match(mode="m2v"))
        with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
            got = adapter.execute(sample_payload(mode="t2v"), JOB)
        self.assertEqual(call.call_count, 2, "A model can expose more than one generation mode")
        self.assertEqual(got["upstream"]["data"]["submission"]["state"], "accepted")

    def test_other_canonical_model_cannot_authorize_requested_model(self):
        response = model_response(generation_match(model="different_canonical_model"))
        with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
            got = adapter.execute(sample_payload(), JOB)
        self.assertFalse(got.get("submitted", True))
        self.assertEqual(call.call_count, 1)

    def test_other_mode_cannot_authorize_requested_mode(self):
        response = model_response(generation_match(mode="m2v"))
        with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
            got = adapter.execute(sample_payload(mode="t2v"), JOB)
        self.assertFalse(got.get("submitted", True))
        self.assertEqual(call.call_count, 1)

    def test_real_four_capability_shape_accepts_first_frame_and_frame_pair(self):
        # Shape observed in the read-only model inspection: the same canonical
        # model has t2v, m2v and two first_last_frame capabilities.
        response = model_response(
            generation_match(mode="t2v"),
            generation_match(mode="m2v"),
            generation_match(mode="first_last_frame", total_count={"min": 1, "max": 1}),
            generation_match(mode="first_last_frame", total_count={"min": 2, "max": 2}),
        )
        refs = ["node:" + NODE, "node:node_abcdefghjk"]
        for count in (1, 2):
            with self.subTest(count=count):
                with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
                    got = adapter.execute(sample_payload(mode="first_last_frame", refs=refs[:count]), JOB)
                self.assertEqual(call.call_count, 2)
                args = call.call_args.args[0]
                self.assertEqual(args.count("--ref"), count)
                self.assertEqual(got["upstream"]["data"]["submission"]["state"], "accepted")

    def test_no_capability_for_reference_count_is_rejected(self):
        response = model_response(generation_match(mode="first_last_frame", total_count={"min": 2, "max": 2}))
        with patch.object(adapter, "invoke", return_value=response) as call:
            got = adapter.execute(sample_payload(mode="first_last_frame", refs=["node:" + NODE]), JOB)
        self.assertFalse(got["submitted"])
        self.assertEqual(call.call_count, 1)

    def test_multiple_applicable_sample_capabilities_are_allowed(self):
        response = model_response(
            generation_match(mode="first_last_frame", total_count={"min": 1, "max": 1}),
            generation_match(mode="first_last_frame", total_count={"min": 1, "max": 2}),
        )
        with patch.object(adapter, "invoke", side_effect=[response, ACCEPTED]) as call:
            adapter.execute(sample_payload(mode="first_last_frame", refs=["node:" + NODE]), JOB)
        self.assertEqual(call.call_count, 2)

    def test_normal_or_unknown_kind_among_applicable_capabilities_is_rejected(self):
        for kind in ("normal", None):
            with self.subTest(kind=kind):
                response = model_response(
                    generation_match(mode="first_last_frame", total_count={"min": 1, "max": 1}),
                    generation_match(mode="first_last_frame", kind=kind, total_count={"min": 1, "max": 2}),
                )
                with patch.object(adapter, "invoke", return_value=response) as call:
                    got = adapter.execute(sample_payload(mode="first_last_frame", refs=["node:" + NODE]), JOB)
                self.assertFalse(got["submitted"])
                self.assertEqual(call.call_count, 1)

    def test_invalid_declared_reference_count_is_rejected(self):
        for count in ({"min": True, "max": 2}, {"min": 2, "max": 1}, {"min": 1}, "1"):
            with self.subTest(count=count):
                response = model_response(generation_match(mode="first_last_frame", total_count=count))
                with patch.object(adapter, "invoke", return_value=response) as call:
                    got = adapter.execute(sample_payload(mode="first_last_frame", refs=["node:" + NODE]), JOB)
                self.assertFalse(got["submitted"])
                self.assertEqual(call.call_count, 1)

    def test_timeout_recovery_identity_matches_the_attempted_submission(self):
        failure = subprocess.TimeoutExpired(["official-cli"], 90)
        with patch.object(adapter, "invoke", side_effect=[model_response(generation_match()), failure]) as call:
            with self.assertRaises(subprocess.TimeoutExpired):
                adapter.execute(sample_payload(), JOB)
        args = call.call_args.args[0]
        recovery = adapter.recovery(sample_payload(), JOB)
        self.assertEqual(recovery["submitId"], args[args.index("--submit-id") + 1])
        self.assertEqual(recovery["nodeId"], args[args.index("--node-id") + 1])
        self.assertEqual(recovery["updateId"], args[args.index("--update-id") + 1])
        self.assertEqual(recovery["projectId"], PROJECT)
        self.assertEqual(recovery["nextAction"], "operation.status")
        self.assertTrue(recovery["doNotResubmit"])
        self.assertEqual(call.call_count, 2)

    def test_confirmation_error_and_partial_ids_are_preserved_without_retry(self):
        stopped = {"ok": False, "exitCode": 10,
                   "error": {"code": "cli.generation_confirmation_required", "requiredAction": "confirm"},
                   "partialData": {"items": [{"nodeId": NODE, "submitId": PROJECT}]}}
        with patch.object(adapter, "invoke", side_effect=[model_response(generation_match()), stopped]) as call:
            got = adapter.execute(sample_payload(), JOB)
        self.assertFalse(got["upstream"]["ok"])
        self.assertEqual(got["upstream"]["partialData"], stopped["partialData"])
        self.assertEqual(call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
