# Fixtures use synthetic identities and material; no user account data.
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
from upstream_contracts import parse_node_show, UpstreamContractError


class NodeContractTests(unittest.TestCase):
    def setUp(self):
        self.node = {"nodeId": "node_0123456789", "type": "element",
            "resources": [{"resourceId": "resource-a", "type": "image", "status": "success"}],
            "subjectBindings": {"main": {"kind": "resource", "id": "resource-a", "type": "image"}}}
        self.response = {"ok": True, "data": {"nodes": [{"result": "FOUND", "node": self.node}]}}

    def test_observed_envelope_preserves_separate_resource_state_without_aliasing(self):
        parsed = parse_node_show(self.response, self.node["nodeId"], "element")
        self.assertEqual(parsed, self.node)
        self.assertNotIn("status", parsed["subjectBindings"]["main"])
        parsed["resources"][0]["status"] = "pending"
        self.assertEqual(self.node["resources"][0]["status"], "success")

    def test_rejects_changed_legacy_shape_and_unsuccessful_response(self):
        for value in ({"ok": False, "data": self.response["data"]},
                      {"ok": True, "data": {"items": self.response["data"]["nodes"]}},
                      {"ok": True, "data": {"nodes": None}},
                      {"ok": True, "data": {"nodes": [None]}}):
            with self.subTest(value=value), self.assertRaises(UpstreamContractError):
                parse_node_show(value, self.node["nodeId"])

    def test_requires_one_found_node_with_exact_identity_and_expected_type(self):
        for rows in ([], [{"result": "NOT_FOUND"}],
                     [self.response["data"]["nodes"][0]] * 2,
                     [{"result": "FOUND", "node": {**self.node, "nodeId": "node_wrong"}}],
                     [{"result": "FOUND", "node": {**self.node, "type": "video"}}]):
            with self.subTest(rows=rows), self.assertRaises(UpstreamContractError):
                parse_node_show({"ok": True, "data": {"nodes": rows}}, self.node["nodeId"], "element")

    def test_rejects_duplicate_missing_or_malformed_resource_identities(self):
        for resources in (None, [None], [{}], [{"resourceId": ""}], self.node["resources"] * 2):
            response = copy.deepcopy(self.response)
            response["data"]["nodes"][0]["node"]["resources"] = resources
            with self.subTest(resources=resources), self.assertRaises(UpstreamContractError):
                parse_node_show(response, self.node["nodeId"])

    def test_absent_resources_are_not_invented_by_parser(self):
        del self.node["resources"]
        parsed = parse_node_show(self.response, self.node["nodeId"])
        self.assertNotIn("resources", parsed)


if __name__ == "__main__":
    unittest.main()
