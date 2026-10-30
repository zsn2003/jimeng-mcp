# Fixtures use synthetic identities and material; no user account data.
"""Business completion requires an adapter's verified, successful result."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
from outcome_normalizer import normalize


class PersistenceOutcomeTests(unittest.TestCase):
    actions = (
        "studio.episode.update", "studio.episode.delete", "studio.storyboard.description.update",
        "personal.subject.create", "personal.subject.update", "personal.subject.delete",
        "personal.voice.create", "personal.voice.update", "personal.voice.archive",
    )

    def saved(self, **changes):
        return {"workspaceMutationAttempted": True, "generationSubmitted": False,
                "persistenceVerified": True, "upstream": {"ok": True, "data": {}}, **changes}

    def test_each_fixed_write_completes_only_after_successful_readback(self):
        for action in self.actions:
            with self.subTest(action=action):
                self.assertEqual(normalize("COMPLETED", self.saved(), payload={"action": action}),
                    {"operationOutcome": "succeeded", "reconciliationRequired": False,
                     "dispatchAttempted": True})

    def test_successful_receipt_without_literal_readback_confirmation_is_only_accepted(self):
        for action in self.actions:
            for marker in (False, None, 1, "true"):
                with self.subTest(action=action, marker=marker):
                    value = normalize("COMPLETED", self.saved(persistenceVerified=marker),
                                      payload={"action": action})
                    self.assertEqual(value["operationOutcome"], "accepted")

    def test_unregistered_actions_and_missing_success_envelope_are_not_certified(self):
        self.assertEqual(normalize("COMPLETED", self.saved(),
            payload={"action": "studio.script.save"})["operationOutcome"], "accepted")
        for action in self.actions:
            with self.subTest(action=action):
                self.assertEqual(normalize("COMPLETED", self.saved(upstream={}),
                    payload={"action": action})["operationOutcome"], "unknown")

    def test_readback_failure_cannot_be_overridden_by_a_stale_success_flag(self):
        for action in self.actions:
            for code, expected in (("WEB_READBACK_MISMATCH", "partial"),
                                   ("WEB_TRANSPORT_FAILED", "unknown")):
                with self.subTest(action=action, code=code):
                    result = self.saved(upstream={"ok": False, "error": {"code": code}})
                    value = normalize("COMPLETED", result, payload={"action": action})
                    self.assertEqual(value, {"operationOutcome": expected,
                        "reconciliationRequired": True, "dispatchAttempted": True})

    def test_incomplete_receipt_states_and_explicit_partial_remain_authoritative(self):
        for state, expected in (("running", "running"), ("partial_success", "partial"),
                                ("saved-unverified", "partial"), ("failed", "rejected")):
            with self.subTest(state=state):
                result = self.saved(upstream={"ok": True, "data": {"state": state}})
                self.assertEqual(normalize("COMPLETED", result,
                    payload={"action": "personal.voice.create"})["operationOutcome"], expected)
        explicit = self.saved(operationOutcome="partial", reconciliationRequired=True)
        self.assertEqual(normalize("COMPLETED", explicit,
            payload={"action": "studio.episode.delete"})["operationOutcome"], "partial")

    def test_interrupted_job_cannot_be_completed_from_an_old_readback_receipt(self):
        for status, expected in (("UNKNOWN", "unknown"), ("FAILED", "rejected"), ("RUNNING", "running")):
            with self.subTest(status=status):
                self.assertEqual(normalize(status, self.saved(),
                    payload={"action": "studio.episode.update"})["operationOutcome"], expected)

    def test_shared_preview_read_is_synchronous_and_does_not_dispatch_a_write(self):
        result = {"workspaceMutationAttempted": False, "generationSubmitted": False,
                  "upstream": {"ok": True, "data": {"share_preview": {
                      "previewinfo": {"preview_url": "https://image.example/existing.png"}}}}}
        self.assertEqual(normalize("COMPLETED", result, payload={"action": "studio.asset.share_get"}),
            {"operationOutcome": "succeeded", "reconciliationRequired": False,
             "dispatchAttempted": False})
        result["upstream"] = {"ok": False, "error": {"code": "WEB_RESPONSE_CONTRACT_MISMATCH"}}
        self.assertEqual(normalize("COMPLETED", result, payload={"action": "studio.asset.share_get"}),
            {"operationOutcome": "rejected", "reconciliationRequired": False,
             "dispatchAttempted": False})


if __name__ == "__main__":
    unittest.main()
