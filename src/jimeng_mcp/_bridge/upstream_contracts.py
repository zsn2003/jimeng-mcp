"""Pure parsers for the observed official CLI response contracts.

Keep transport, retries and business policy outside this module. Bindings refer
to resources; they do not embed the separate resource's readiness state.
"""
from copy import deepcopy


class UpstreamContractError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def parse_node_show(envelope, node_id, expected_type=None):
    """Return an isolated exact FOUND node; reject ambiguous/changed envelopes.

    `envelope` is the CLI {ok, data:{nodes:[{result,node}]}} object, not an
    adapter's outer receipt. No resource or binding fields are synthesized.
    Resources, when present, remain a separate unique resourceId-indexed list.
    """
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise UpstreamContractError("NODE_READ_REJECTED", "Official node read was not successful")
    data = envelope.get("data")
    rows = data.get("nodes") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UpstreamContractError("NODE_ENVELOPE_MISMATCH", "Official node read did not contain the observed nodes envelope")
    found = [row.get("node") for row in rows if row.get("result") == "FOUND"]
    if not found:
        raise UpstreamContractError("NODE_NOT_FOUND", "The exact node was not returned as FOUND")
    if len(found) != 1:
        raise UpstreamContractError("NODE_AMBIGUOUS", "Official node read returned more than one FOUND node")
    node = found[0]
    if not isinstance(node, dict) or node.get("nodeId") != node_id:
        raise UpstreamContractError("NODE_ID_MISMATCH", "Official node identity did not match the requested node")
    if expected_type is not None and node.get("type") != expected_type:
        raise UpstreamContractError("NODE_TYPE_MISMATCH", "Official node type did not match the expected type")
    if "resources" in node:
        resources = node["resources"]
        if (not isinstance(resources, list)
                or any(not isinstance(item, dict) or not isinstance(item.get("resourceId"), str)
                       or not item["resourceId"] for item in resources)
                or len({item["resourceId"] for item in resources}) != len(resources)):
            raise UpstreamContractError("NODE_RESOURCES_INVALID", "Official node resources must have unique explicit identities")
    return deepcopy(node)
