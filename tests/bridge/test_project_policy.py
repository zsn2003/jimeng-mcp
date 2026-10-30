# Fixtures use synthetic identities and material; no user account data.
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import project_policy as policy

PROJECT = "1000000b-0000-4000-8000-000000000002"
ORDINARY = {"writes": True, "billable": True, "requiresUnlimited": False,
            "videoGeneration": True, "workflowStage": "ordinary"}
READ = {"writes": False, "billable": False, "requiresUnlimited": False,
        "videoGeneration": False, "workflowStage": None}


class ProjectPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "policy-state"
        self.environment = patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "unlimited",
            "JIMENG_VIDEO_WORKFLOW_POLICY": "sample_first", "JIMENG_PROJECT_POLICY_FILE": "",
            "JIMENG_POLICY_STATE_DIR": str(self.state)})
        self.environment.start(); self.addCleanup(self.environment.stop)
        self.payload = {"action": "video.extend", "projectId": PROJECT, "prompt": "Continue the same shot", "dryRun": False,
            "workflow": {"ordinaryApproval": {"approvalId": "task-specific-approval", "reason": "User authorized this one ordinary extension"}}}

    def assert_policy_error(self, payload, effects=ORDINARY, code=None, **kwargs):
        with self.assertRaises(policy.ValidationError) as caught:
            policy.enforce(payload, effects, **kwargs)
        self.assertEqual(set(caught.exception.details), {"field", "code", "message"})
        if code: self.assertEqual(caught.exception.details["code"], code)
        self.assertNotIn(self.payload["workflow"]["ordinaryApproval"]["reason"], str(caught.exception))
        return caught.exception.details

    def test_validation_needs_no_state_directory_or_filesystem_when_config_unset(self):
        with patch.object(policy.os, "open", side_effect=AssertionError("must not read or write")), \
             patch.object(policy.Path, "mkdir", side_effect=AssertionError("must not create state")):
            summary = policy.enforce(self.payload, ORDINARY)
        self.assertTrue(summary["ordinaryApprovalRequired"])
        self.assertFalse(summary["ordinaryApprovalClaim"]["claimed"])
        self.assertFalse(self.state.exists())

    def test_dryrun_default_and_reads_do_not_claim_or_require_approval(self):
        for payload, effects in (({k:v for k,v in self.payload.items() if k not in ("dryRun", "workflow")}, ORDINARY),
                                 ({**self.payload, "dryRun": True}, ORDINARY),
                                 ({"action": "node.read", "projectId": PROJECT}, READ),
                                 ({**self.payload, "action": "node.read"}, READ)):
            with self.subTest(payload=payload):
                result = policy.enforce(payload, effects, job_id="read-or-plan", claim=True)
                self.assertFalse(result["ordinaryApprovalRequired"])
                self.assertFalse(result["ordinaryApprovalClaim"]["claimed"])
        self.assertFalse(self.state.exists())

    def test_readonly_rejects_live_write_before_claim_but_allows_reads_and_plans(self):
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "1"}):
            self.assert_policy_error(self.payload, code="READ_ONLY_POLICY", job_id="job-a", claim=True)
            policy.enforce({**self.payload, "dryRun": True}, ORDINARY, claim=True)
            policy.enforce({"action": "node.read"}, READ, claim=True)
        self.assertFalse(self.state.exists())

    def test_sample_and_promotion_need_no_ordinary_exception(self):
        payload = policy.strip_workflow(self.payload)
        for stage in ("sample", "promote"):
            summary = policy.enforce(payload, {**ORDINARY, "workflowStage": stage}, job_id="job-a", claim=True)
            self.assertFalse(summary["ordinaryApprovalRequired"])
        self.assertFalse(self.state.exists())

    def test_unknown_video_stage_and_extend_require_task_approval(self):
        for stage in (None, "ordinary", "extend"):
            self.assert_policy_error(policy.strip_workflow(self.payload), {**ORDINARY, "workflowStage": stage},
                                     code="ORDINARY_APPROVAL_REQUIRED")

    def test_general_default_does_not_consume_an_unused_approval(self):
        with patch.dict(os.environ, {"JIMENG_VIDEO_WORKFLOW_POLICY": "general"}):
            summary = policy.enforce(self.payload, ORDINARY, job_id="job-a", claim=True)
            policy.enforce(policy.strip_workflow(self.payload), ORDINARY)
        self.assertFalse(summary["ordinaryApprovalRequired"])
        self.assertFalse(self.state.exists())

    def test_finite_cap_requires_positive_explicit_ceiling_within_cap(self):
        with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": "10"}):
            for ceiling in (None, 0, 11):
                payload = dict(self.payload)
                if ceiling is not None: payload["creditCeiling"] = ceiling
                self.assert_policy_error(payload, code="CREDIT_APPROVAL_REQUIRED")
            summary = policy.enforce({**self.payload, "creditCeiling": 10}, ORDINARY)
            self.assertEqual(summary["liveCreditCeiling"], 10)
        with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": "0"}):
            self.assert_policy_error({**self.payload, "creditCeiling": 1}, code="CREDIT_APPROVAL_REQUIRED")

    def test_unlimited_keeps_explicit_ceiling_for_provider_enforcement(self):
        for ceiling in (0, 10, 100000):
            payload = {**self.payload, "creditCeiling": ceiling}
            summary = policy.enforce(payload, ORDINARY)
            self.assertEqual(policy.strip_workflow(payload)["creditCeiling"], ceiling)
            self.assertEqual(summary["creditPolicy"], "unlimited")

    def test_requires_unlimited_rejects_finite_policy_and_any_explicit_ceiling(self):
        effects = {**ORDINARY, "requiresUnlimited": True}
        for cap in ("0", "10"):
            with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": cap}):
                self.assert_policy_error(self.payload, effects, code="UNLIMITED_POLICY_REQUIRED")
                policy.enforce({**self.payload, "dryRun": True}, effects)
        for dry_run in (True, False):
            self.assert_policy_error({**self.payload, "creditCeiling": 0, "dryRun": dry_run}, effects,
                                     code="NUMERIC_CEILING_UNSUPPORTED")
        policy.enforce(self.payload, effects)

    def test_claim_binds_full_nonworkflow_payload_and_preserves_idempotent_replay(self):
        first = policy.enforce(self.payload, ORDINARY, job_id="job-a", claim=True)
        changed_reason = copy.deepcopy(self.payload)
        changed_reason["workflow"]["ordinaryApproval"]["reason"] = "Same authorization, revised explanation"
        replay = policy.enforce(changed_reason, ORDINARY, job_id="job-a", claim=True)
        self.assertTrue(first["ordinaryApprovalClaim"]["claimed"])
        self.assertFalse(first["ordinaryApprovalClaim"]["replayed"])
        self.assertTrue(replay["ordinaryApprovalClaim"]["replayed"])
        self.assert_policy_error(self.payload, code="ORDINARY_APPROVAL_ALREADY_CLAIMED", job_id="job-b", claim=True)
        for change in ({"prompt": "different"}, {"action": "video.other"}, {"projectId": "different"}, {"title": "new"}):
            self.assert_policy_error({**self.payload, **change}, code="ORDINARY_APPROVAL_ALREADY_CLAIMED", job_id="job-a", claim=True)
        records = list(self.state.iterdir())
        self.assertEqual(len(records), 1)
        stored = records[0].read_text()
        self.assertNotIn(self.payload["workflow"]["ordinaryApproval"]["reason"], stored)
        self.assertNotIn(self.payload["workflow"]["ordinaryApproval"]["approvalId"], stored)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    def test_concurrent_jobs_cannot_claim_the_same_approval(self):
        barrier = threading.Barrier(2)
        def claim(job_id):
            barrier.wait()
            try:
                return policy.enforce(self.payload, ORDINARY, job_id=job_id, claim=True)
            except policy.ValidationError as exc:
                return {"denied": exc.details["code"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("job-a", "job-b")))
        self.assertEqual(sum("ordinaryApprovalClaim" in result for result in results), 1)
        self.assertEqual(sum("denied" in result for result in results), 1)
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)

    def test_claim_cannot_work_without_stable_job_or_private_absolute_state(self):
        self.assert_policy_error(self.payload, code="APPROVAL_JOB_REQUIRED", claim=True)
        with patch.dict(os.environ, {"JIMENG_POLICY_STATE_DIR": "relative-path"}):
            self.assert_policy_error(self.payload, code="POLICY_STATE_REQUIRED", job_id="job-a", claim=True)
        self.state.mkdir(mode=0o755)
        self.assert_policy_error(self.payload, code="POLICY_STATE_INVALID", job_id="job-a", claim=True)
        self.state.chmod(0o500)
        try:
            self.assert_policy_error(self.payload, code="POLICY_STATE_INVALID", job_id="job-a", claim=True)
        finally:
            self.state.chmod(0o700)

    def test_uncertain_claim_persistence_keeps_reservation_and_never_reassigns_it(self):
        with patch.object(policy.os, "fsync", side_effect=OSError("private path diagnostic")):
            details = self.assert_policy_error(self.payload, code="APPROVAL_CLAIM_UNCERTAIN", job_id="job-a", claim=True)
        self.assertNotIn("private path diagnostic", json.dumps(details))
        self.assertEqual(len(list(self.state.glob("*.json"))), 1)
        self.assert_policy_error(self.payload, code="ORDINARY_APPROVAL_ALREADY_CLAIMED", job_id="job-b", claim=True)

    def test_policy_config_and_credit_bounds_are_not_silently_relaxed(self):
        for cap in ("-1", "100001", "UNLIMITED", "not-a-cap"):
            with patch.dict(os.environ, {"JIMENG_MAX_CREDITS": cap}):
                self.assert_policy_error(self.payload, code="CREDIT_POLICY_INVALID")
        for ceiling in (-1, True, 1.5, 100001):
            self.assert_policy_error({**self.payload, "creditCeiling": ceiling}, code="CREDIT_CEILING_INVALID")
        with patch.dict(os.environ, {"JIMENG_VIDEO_WORKFLOW_POLICY": "remember_ordinary"}):
            self.assert_policy_error(self.payload, code="WORKFLOW_POLICY_INVALID")

    def test_symlink_or_corrupt_existing_claim_is_never_reused(self):
        policy.enforce(self.payload, ORDINARY, job_id="job-a", claim=True)
        claim = next(self.state.glob("*.json"))
        saved = claim.with_suffix(".saved")
        claim.rename(saved); claim.symlink_to(saved)
        self.assert_policy_error(self.payload, code="APPROVAL_CLAIM_INVALID", job_id="job-a", claim=True)
        claim.unlink(); claim.write_text("partial-json"); claim.chmod(0o600)
        self.assert_policy_error(self.payload, code="APPROVAL_CLAIM_INVALID", job_id="job-a", claim=True)

    def test_trusted_project_override_is_readonly_and_payload_cannot_override_it(self):
        config = Path(self.temp.name) / "projects.json"
        config.write_text(json.dumps({"projects": {PROJECT: "general", "12345": "sample_first"}})); config.chmod(0o600)
        with patch.dict(os.environ, {"JIMENG_PROJECT_POLICY_FILE": str(config)}):
            summary = policy.enforce(policy.strip_workflow(self.payload), ORDINARY)
            self.assertEqual(summary["videoWorkflowPolicy"], "general")
            self.assertEqual(summary["workflowPolicySource"], "project_override")
            self.assert_policy_error({**self.payload, "workflow": {"policy": "general"}}, code="WORKFLOW_SCHEMA_INVALID")
            self.assert_policy_error({**policy.strip_workflow(self.payload), "projectId": 12345}, code="ORDINARY_APPROVAL_REQUIRED")
        self.assertFalse(self.state.exists())

    def test_configured_invalid_or_missing_project_file_fails_closed(self):
        config = Path(self.temp.name) / "projects.json"
        with patch.dict(os.environ, {"JIMENG_PROJECT_POLICY_FILE": str(config)}):
            self.assert_policy_error(self.payload, code="PROJECT_POLICY_UNAVAILABLE")
            config.write_text('{"projects":{"one":"allow_everything"}}'); config.chmod(0o600)
            self.assert_policy_error(self.payload, code="PROJECT_POLICY_INVALID")
            config.write_text('{"projects":{}}'); config.chmod(0o666)
            self.assert_policy_error(self.payload, code="PROJECT_POLICY_UNAVAILABLE")

    def test_closed_schema_and_fixed_errors_do_not_echo_invalid_approval(self):
        invalid = [None, "secret-cookie-value", {"ordinaryApproval": {}},
            {"ordinaryApproval": {"approvalId": "Bearer secret-cookie-value", "reason": "valid"}},
            {"ordinaryApproval": {"approvalId": "id", "reason": " "}},
            {"ordinaryApproval": {"approvalId": "id", "reason": "x", "remember": True}},
            {"ordinaryApproval": {"approvalId": "非ASCII", "reason": "x"}}]
        for workflow in invalid:
            details = self.assert_policy_error({**self.payload, "workflow": workflow})
            self.assertNotIn("secret-cookie-value", json.dumps(details))
        schema = policy.public_workflow_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["ordinaryApproval"]["additionalProperties"])
        schema["properties"].clear()
        self.assertIn("ordinaryApproval", policy.public_workflow_schema()["properties"])

    def test_strip_workflow_returns_independent_payload_without_reason(self):
        source = {**self.payload, "nested": {"refs": ["node-a"]}}
        stripped = policy.strip_workflow(source)
        stripped["nested"]["refs"].append("node-b")
        self.assertNotIn("workflow", stripped)
        self.assertEqual(source["nested"]["refs"], ["node-a"])
        self.assertIn("workflow", source)


if __name__ == "__main__":
    unittest.main()
