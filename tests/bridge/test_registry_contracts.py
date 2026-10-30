# Fixtures use synthetic identities and material; no user account data.
"""Contract regressions reproduced during the architecture review; no network."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import acceptance_registry as acceptance
import action_registry
import adapter
import input_contracts

PROJECT = "00000000-0000-0000-0000-000000000001"
R1, R2, R3 = "node:node_0123456789", "node:node_abcdefghjk", "node:node_pqrstvwxyz"


class InputAndDispatchTests(unittest.TestCase):
    def payload(self, **kwargs):
        return {"action": "video.sample", "projectId": PROJECT, "model": "test_sample", "prompt": "paper plane",
                "mode": "first_last_frame", "dryRun": True, **kwargs}

    def test_prompt_only_first_frame_and_pair_are_valid(self):
        for refs in ([R1], [R1, R2]):
            payload = self.payload(prompt=" ".join("{{" + r + "}}" for r in refs))
            adapter.validate(payload)
            self.assertEqual(input_contracts.references(payload), refs)

    def test_three_merged_references_are_rejected_before_any_invocation(self):
        with patch.object(adapter, "invoke") as invoke, self.assertRaises(input_contracts.ValidationError):
            adapter.execute(self.payload(refs=[R1, R2], prompt="{{"+R3+"}}"), "test-job")
        invoke.assert_not_called()

    def test_reference_order_and_dedup_are_shared_with_compiled_command(self):
        payload = self.payload(refs=[R2], prompt="{{"+R1+"}} and {{"+R2+"}}")
        with patch.object(adapter, "invoke", return_value={"ok": True, "data": {"dryRun": True}}) as invoke:
            adapter.execute(payload, "test-job")
        args = invoke.call_args.args[0]
        self.assertEqual([args[i+1] for i,x in enumerate(args) if x=="--ref"], [R2, R1])

    def test_typed_reference_identity_is_checked_in_prompt_too(self):
        for token in ("node:node_INVALID", "res:00000000-0000-0000-0000-00000000000z", "https://example.com"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                adapter.validate(self.payload(prompt="{{"+token+"}}"))

    def test_registry_schema_dispatch_and_capabilities_have_one_action_set(self):
        schemas = action_registry.request_schemas()
        self.assertEqual(set(schemas), set(adapter.capabilities()["actions"]))
        self.assertEqual(set(input_contracts.schemas()), set(adapter.FIELDS))
        for name, spec in action_registry.operations().items():
            self.assertEqual(spec.schema["properties"]["action"]["const"], name)
            if spec.provider != "baseline":
                self.assertTrue(action_registry.provider_for({"action": name}).supports(name))

    def test_workflow_cannot_be_attached_to_a_read_action(self):
        with self.assertRaises(ValueError):
            adapter.validate({"action": "model.list", "workflow": {"ordinaryApproval": {"approvalId": "x", "reason": "x"}}})

    def test_unknown_web_spending_is_blocked_at_zero_before_http(self):
        import web_actions
        with patch.dict(os.environ, {"JIMENG_READ_ONLY": "0", "JIMENG_MAX_CREDITS": "0"}), patch.object(web_actions, "_send") as send:
            with self.assertRaises(ValueError):
                adapter.execute({"action": "studio.images.generate", "overviewAssetId": "123", "generateType": 1,
                    "generateScope": "test", "dryRun": False}, "no-spend")
        send.assert_not_called()


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "evidence").mkdir()
        self.file = self.root / "evidence/readback.json"
        self.file.write_text(json.dumps({"projectId": "original", "saved": True}))
        self.index = self.root / "acceptance_records.json"
        self.record = {"action": "studio.script.save", "variant": "raw_content.add", "scope": "one add and exact readback",
            "evidence": "evidence/readback.json", "verifiedAt": "2026-10-29",
            "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest()}
        self.write_index([self.record])
        for key, value in (("ROOT", self.root), ("INDEX", self.index)):
            patcher = patch.object(acceptance, key, value); patcher.start(); self.addCleanup(patcher.stop)

    def write_index(self, records):
        self.index.write_text(json.dumps({"version": 1, "records": records}))

    def test_exact_variant_has_one_shared_capability_and_catalog_verdict(self):
        actions = acceptance.annotate({"studio.script.save": {}})
        catalog = acceptance.annotate_catalog({"operations": [
            {"operation": "studio.script.save", "callableActions": ["studio.script.save"]},
            {"operation": "studio.script.update", "callableActions": ["studio.script.save"]}]})
        self.assertTrue(actions["studio.script.save"]["serverExecutionVerified"])
        self.assertTrue(catalog["operations"][0]["serverExecutionVerified"])
        self.assertFalse(catalog["operations"][1]["serverExecutionVerified"])
        self.assertEqual(acceptance.verified("studio.script.save", "raw_content.update"), [])

    def test_evidence_change_including_identity_mismatch_fails_closed(self):
        self.file.write_text(json.dumps({"projectId": "unrelated", "saved": True}))
        self.assertEqual(acceptance.all_verified(), [])

    def test_missing_malformed_or_changed_evidence_never_inherits_old_success(self):
        self.file.unlink(); self.assertEqual(acceptance.all_verified(), [])
        self.file.write_text('{bad'); self.assertEqual(acceptance.all_verified(), [])
        self.index.write_text('{bad'); self.assertEqual(acceptance.all_verified(), [])

    def test_path_escape_or_symlink_is_not_accepted(self):
        self.write_index([{**self.record, "evidence": "evidence/../acceptance_records.json"}])
        self.assertEqual(acceptance.all_verified(), [])
        self.write_index([self.record]); self.file.unlink()
        self.file.symlink_to(self.index)
        self.assertEqual(acceptance.all_verified(), [])


if __name__ == "__main__":
    unittest.main()
