"""Fixed mode-9 video extension of an already prepared canonical target.

The bridge reads and checks the current raw draft before one submission. It
does not create or edit extension drafts. No write/generation call is retried;
an uncertain response raises so the job server preserves UNKNOWN identities.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid

import web_actions
from upstream_contracts import parse_node_show

ORIGIN = web_actions.ORIGIN
RESOURCE_PATH = "/octo_api/v1/resource/batch_get"
GENERATE_PATH = "/octo_api/v1/node_generate"
SPECS_PATH = "/octo_api/v1/generation_specs"
DRAFT_PATH = "/octo_api/v1/project/draft/get"
DRAFT_DATA_VERSION = "2.38.0"
DRAFT_SCHEMA_PROFILE = "sha256:d06ef3ea7ebaef3e2914d3909fb355b89d31f8d739c4c7dc792f654520b39fa4"
NODE_PATTERN = r"^node_[0123456789abcdefghjkmnpqrstvwxyz]{10}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UUID_FIELD = {"type": "string", "pattern": UUID_PATTERN}
NODE_FIELD = {"type": "string", "pattern": NODE_PATTERN}
BLOCKERS = (
    "Browser target creation uses acknowledged canvas WebSocket addNode/addEdge revisions; a metadata-only CLI save has not been verified as equivalent",
    "This distribution includes no user execution evidence for server-side mode-9 submission; verify the original task and resource state before claiming completion",
)
SCHEMAS = {
    "video.extend": {
        "type": "object", "additionalProperties": False,
        "required": ["action", "projectId", "sourceNodeId", "sourceResourceId", "targetNodeId", "prompt"],
        "description": "Preflight or one submission for an already prepared canonical extension draft. Defaults to a successful 1080p final source, ordinary seedance_2.5 and five-second extension. Does not create/edit a target draft. Live requires exact saved prompt, expectedDraftVersion, allowOrdinaryModel and an unlimited service budget.",
        "properties": {"action": {"const": "video.extend", "type": "string"},
            "projectId": UUID_FIELD, "sourceNodeId": NODE_FIELD, "sourceResourceId": UUID_FIELD,
            "targetNodeId": NODE_FIELD, "prompt": {"type": "string", "minLength": 1, "maxLength": 12000},
            "model": {"type": "string", "const": "seedance_2.5", "default": "seedance_2.5"},
            "durationSeconds": {"type": "integer", "const": 5, "default": 5},
            "expectedDraftVersion": {"type": "integer", "minimum": 1,
                "description": "Canonical version observed after preparing the target. Required for live submission; a newer head requires another read/review."},
            "allowOrdinaryModel": {"type": "boolean", "default": False,
                "description": "Explicit per-call acknowledgement: this is ordinary video extension, not official sample generation."},
            "allowSampleSource": {"type": "boolean", "default": False,
                "description": "Explicit per-call exception only; normal workflow promotes the sample to 1080p first."},
            "dryRun": {"type": "boolean", "default": True}}},
    "video.extend.status": {
        "type": "object", "additionalProperties": False,
        "required": ["action", "projectId", "targetNodeId", "submitId", "resourceId"],
        "description": "Read an existing mode-9 extension using its original identities. Never submits or retries generation.",
        "properties": {"action": {"const": "video.extend.status", "type": "string"},
            "projectId": UUID_FIELD, "targetNodeId": NODE_FIELD,
            "submitId": UUID_FIELD, "resourceId": UUID_FIELD,
            "sourceNodeId": NODE_FIELD, "sourceResourceId": UUID_FIELD}},
}


def supports(action):
    return isinstance(action, str) and action in SCHEMAS


def request_schemas():
    return deepcopy(SCHEMAS)


def validate(payload):
    if not isinstance(payload, dict) or not supports(payload.get("action")):
        raise ValueError("Unsupported fixed canvas web action")
    spec = SCHEMAS[payload["action"]]
    if set(payload) - set(spec["properties"]) or set(spec["required"]) - set(payload):
        raise ValueError("Missing or unknown fixed canvas fields")
    for key, value in payload.items():
        rule = spec["properties"][key]
        kind = rule["type"]
        if (kind == "string" and not isinstance(value, str)
                or kind == "boolean" and type(value) is not bool
                or kind == "integer" and type(value) is not int):
            raise ValueError("Invalid fixed canvas field type")
        if "const" in rule and value != rule["const"]:
            raise ValueError("Only the observed extension model and duration are supported")
        if kind == "integer" and value < rule.get("minimum", 0):
            raise ValueError("Invalid canonical draft version")
        if isinstance(value, str):
            if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
                raise ValueError("Unsupported text encoding")
            if not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", 128):
                raise ValueError("Invalid fixed canvas text length")
            if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
                raise ValueError("Invalid fixed canvas identity")
            if key in ("projectId", "sourceResourceId", "resourceId", "submitId") and str(uuid.UUID(value)) != value:
                raise ValueError("Identity must be a canonical lowercase UUID")
    if payload["action"] == "video.extend":
        if not payload["prompt"].strip() or "{{" in payload["prompt"] or "}}" in payload["prompt"]:
            raise ValueError("Prompt must be plain text; the bridge binds the sole source video")
        if payload["sourceNodeId"] == payload["targetNodeId"]:
            raise ValueError("Source and target must be different nodes")
        if not payload.get("dryRun", True):
            # Validate this even while live execution is contract-blocked, so a
            # later implementation cannot accidentally reuse a dry-run default.
            if os.environ.get("JIMENG_READ_ONLY", "1") != "0":
                raise ValueError("Service is read-only")
            if payload.get("allowOrdinaryModel") is not True:
                raise ValueError("Ordinary extension requires explicit per-call acknowledgement")
            if "expectedDraftVersion" not in payload:
                raise ValueError("Live extension requires the prepared canonical draft version")
            if os.environ.get("JIMENG_MAX_CREDITS", "0").strip().lower() != "unlimited":
                raise ValueError("The fixed web extension cannot enforce a numeric credit ceiling")


def capabilities():
    try:
        web_actions._load_session()
        configured = True
    except web_actions.SessionError:
        configured = False
    try:
        import acceptance_registry
        submitted = acceptance_registry.verified("video.extend", variant="prepared_target_submission")
        inspected = acceptance_registry.verified("video.extend.status")
    except (ImportError, OSError, ValueError, TypeError):
        submitted, inspected = [], []
    return {"adapter": "fixed-canvas-video-extension", "authConfigured": configured,
            "actions": {
                "video.extend": {"implemented": True, "available": configured, "dryRunAvailable": True,
                    "liveExecutionAllowed": configured and os.environ.get("JIMENG_READ_ONLY", "1") == "0"
                        and os.environ.get("JIMENG_MAX_CREDITS", "0").strip().lower() == "unlimited",
                    "serverExecutionVerified": bool(submitted), "executionVerification": submitted,
                    "billable": True, "mutatesWorkspace": True,
                    "executionScope": "prepared target only; read canonical draft twice, then one mode-9 submission",
                    "defaultSource": "successful 1080p final video", "generationKind": "ordinary",
                    "targetAutoCreationImplemented": False, "blockers": list(BLOCKERS)},
                "video.extend.status": {"implemented": True, "available": configured,
                    "liveExecutionAllowed": configured, "billable": False, "mutatesWorkspace": False,
                    "serverExecutionVerified": bool(inspected), "executionVerification": inspected,
                    "executionScope": "read existing mode-9 task and verify original source/target/resource identities"}}}


def _identities(payload, job_id):
    return {"projectId": payload["projectId"], "sourceNodeId": payload["sourceNodeId"],
            "sourceResourceId": payload["sourceResourceId"], "targetNodeId": payload["targetNodeId"],
            "submitId": str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:extend-submit:" + job_id)),
            "resourceId": str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:extend-resource:" + job_id))}


def recovery(payload, job_id):
    if payload.get("action") == "video.extend":
        return {**_identities(payload, job_id), "targetProvidedByCaller": True, "draftSaveAttempted": False,
                "nextAction": "video.extend.status", "doNotResubmit": True,
                "note": "Reserved identities are not proof of submission; no target update identity is invented because this adapter does not save drafts"}
    return None


def _base(payload):
    return {"action": payload["action"], "generationSubmitted": False,
            "workspaceMutationAttempted": False, "requestedAt": datetime.now(timezone.utc).isoformat(),
            "source": "live_read_preflight", "cacheUsed": False}


def _failure(base, code, message, data=None):
    return {**base, "upstream": {"ok": False, "error": {"code": code, "message": message}},
            **({"diagnostic": data} if data is not None else {})}


def _node(project, node_id, invoke):
    try:
        result = invoke(["node", "show", "--project-id", project, "--node-id", node_id])
    except Exception:
        raise RuntimeError("Official read failed") from None
    return parse_node_show(result, node_id, expected_type="video")


def _request(path, body, session):
    if path not in (RESOURCE_PATH, SPECS_PATH, DRAFT_PATH):
        raise ValueError("Unsupported fixed canvas read path")
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": session["cookie"], "User-Agent": session["userAgent"], **session["headers"]}
    try:
        if path in (RESOURCE_PATH, DRAFT_PATH):
            status, raw = web_actions._send(path, body, headers)
        else:
            request = urllib.request.Request(ORIGIN + SPECS_PATH, headers=headers, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), web_actions._NoRedirect())
            with opener.open(request, timeout=25) as response:
                status, raw = response.code, response.read(web_actions.MAX_RESPONSE_BYTES + 1)
    except Exception:
        raise RuntimeError("Fixed authenticated read failed") from None
    if len(raw) > web_actions.MAX_RESPONSE_BYTES:
        raise ValueError("Canvas response exceeded the bound")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError("Canvas response was not JSON") from None
    if not isinstance(result, dict):
        raise ValueError("Canvas response envelope changed")
    if not 200 <= status < 300 or str(result.get("ret")) != "0":
        raise ValueError("Authoritative canvas read was rejected")
    return result.get("data")


def _draft(project, session):
    """Read the current canonical JSON snapshot, never a CLI projection/cache."""
    data = _request(DRAFT_PATH, {"project_id": project, "need_draft_json": True}, session)
    if (not isinstance(data, dict) or not isinstance(data.get("project"), dict)
            or data["project"].get("project_id") != project
            or type(data.get("draft_version")) is not int or data["draft_version"] < 1
            or data.get("head_draft_version") != data["draft_version"]
            or data.get("data_version") != DRAFT_DATA_VERSION
            or data.get("draft_schema_profile") != DRAFT_SCHEMA_PROFILE
            or not isinstance(data.get("draft_json"), str)):
        raise ValueError("Current canonical draft version or schema is unsupported")
    try:
        document = json.loads(data["draft_json"])
    except (ValueError, UnicodeError):
        raise ValueError("Current canonical draft is not valid JSON") from None
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1
            or document.get("draftVersion") != data["draft_version"]
            or not isinstance(document.get("nodes"), list)
            or not isinstance(document.get("edges"), list)):
        raise ValueError("Canonical draft identity or document shape changed")
    for key in ("nodes", "edges"):
        ids = [item.get("id") for item in document[key] if isinstance(item, dict)]
        if (len(ids) != len(document[key]) or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Canonical draft contains ambiguous identities")
    return {"version": data["draft_version"], "dataVersion": data["data_version"],
            "schemaProfile": data["draft_schema_profile"], "document": document}


def _target_draft(payload, source, snapshot):
    """Validate the raw target and source selection omitted by node.show."""
    if "expectedDraftVersion" in payload and snapshot["version"] != payload["expectedDraftVersion"]:
        raise ValueError("Canonical draft changed after the caller prepared the target")
    document = snapshot["document"]
    nodes = {n["id"]: n for n in document["nodes"]}
    target = nodes.get(payload["targetNodeId"], {})
    source_node = nodes.get(payload["sourceNodeId"], {})
    target_data = target.get("data", {})
    generation = target_data.get("generationDraft", {})
    settings = generation.get("contentSettings", {})
    refs = generation.get("references", [])
    parts = generation.get("parts", [])
    if (source_node.get("type") != "video"
            or source_node.get("data", {}).get("resourceId") != payload["sourceResourceId"]
            or target.get("type") != "video" or target_data.get("resourceId")
            or target_data.get("resourceBatches") != []
            or generation.get("kind") != "video" or generation.get("outputCount") != 1
            or settings != {"aspectRatio": source["aspectRatio"], "durationSeconds": 5,
                            "mode": "videoExtend", "modelName": "seedance_2.5",
                            "resolution": source["resolution"]}
            or not isinstance(refs, list) or len(refs) != 1 or not isinstance(refs[0], dict)
            or not isinstance(parts, list)):
        raise ValueError("Canonical target is not an empty matching video-extension draft")
    ref = refs[0]
    slot = ref.get("slotId")
    if (not isinstance(slot, str) or not re.fullmatch(UUID_PATTERN, slot)
            or ref.get("type") != "extension" or ref.get("extensionType") != "node"
            or ref.get("role") != "source" or ref.get("nodeId") != payload["sourceNodeId"]
            or ref.get("sourceInstanceId") != payload["sourceNodeId"]
            or any(k in ref for k in ("resourceId", "resourceIds"))):
        raise ValueError("Canonical target extension reference is not the selected source")
    reference_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "extension"]
    text_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "text"]
    if (len(reference_parts) != 1 or len(reference_parts) + len(text_parts) != len(parts)
            or reference_parts[0].get("extension_type") != "node"
            or reference_parts[0].get("object_id") != slot
            or any(not isinstance(p.get("text"), str) for p in text_parts)):
        raise ValueError("Canonical target prompt/reference parts are unsupported")
    incoming = [e for e in document["edges"] if e.get("target") == payload["targetNodeId"]]
    if (len(incoming) != 1 or incoming[0].get("source") != payload["sourceNodeId"]
            or incoming[0].get("type") != "reference"):
        raise ValueError("Canonical target dependency edge is not the selected source")
    stored_prompt = "".join(p["text"] for p in text_parts)
    return {"version": snapshot["version"], "dataVersion": snapshot["dataVersion"],
            "schemaProfile": snapshot["schemaProfile"],
            "generationDraftSha256": hashlib.sha256(json.dumps(generation, sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            "promptMatchesRequested": stored_prompt == payload["prompt"],
            "sourceResourceSelectionVerified": True, "sourceReferenceSlotVerified": True,
            "parts": [{"type": p["type"], **({"text": p["text"]} if p["type"] == "text" else {})} for p in parts]}


def _commerce(resolution, session):
    data = _request(SPECS_PATH, None, session)
    try:
        price = data["commerce_config"]["model_config"]["seedance_2.5"]["video_resolution_price_configs"][resolution]["price"]
    except (TypeError, KeyError):
        raise ValueError("Current ordinary extension commerce mapping is unavailable") from None
    observed = {"480p": "seedance_25_480p_output", "1080p": "seedance_25_1080p_output"}
    if (price.get("resource_type") != "aigc" or price.get("resource_id") != "generate_video"
            or price.get("benefit_type") != observed.get(resolution)):
        raise ValueError("Current ordinary extension commerce mapping changed")
    return {"benefit_type": price["benefit_type"]}


def _resource(project, resource_id, session):
    # The path is a constant and only the validated resource/project IDs vary.
    data = _request(RESOURCE_PATH, {"project_id": project, "resource_ids": [resource_id]}, session)
    resources = (data or {}).get("resources", [])
    matches = [r for r in resources if isinstance(r, dict) and r.get("resource_id") == resource_id]
    if len(matches) != 1 or matches[0].get("type") != 2:
        raise ValueError("Exact video resource was not returned")
    # No media_urls, Cookie, provider diagnostics or raw exception are exposed.
    return matches[0]


def _resource_in_node(node, resource_id):
    return any(x.get("resourceId") == resource_id for x in node.get("resources", []) if isinstance(x, dict))


def _metrics(resource):
    video = resource.get("video", {})
    return {key: video.get(upstream) for key, upstream in (("width", "width"), ("height", "height"),
            ("durationSeconds", "duration"), ("fps", "fps"), ("resolution", "resolution"))}


def _source(payload, node, resource):
    video = resource.get("video", {})
    if (node.get("status") != "success" or not _resource_in_node(node, payload["sourceResourceId"])
            or resource.get("status") != 1000 or not isinstance(video, dict)):
        raise ValueError("Source must be a successful video resource owned by the exact source node")
    kind = resource.get("generation_kind")
    sample = kind == 2
    if sample and payload.get("allowSampleSource") is not True:
        raise ValueError("Sample sources are rejected by default; promote the sample to 1080p before extending")
    if kind not in (1, 2, 3):
        raise ValueError("Source generation kind is unknown")
    resolution = video.get("resolution")
    if not sample and (resolution != "1080p" or min(video.get("width", 0), video.get("height", 0)) < 1080):
        raise ValueError("Normal workflow requires a successful 1080p final source")
    if sample and resolution != "480p":
        raise ValueError("Only the observed 480p sample exception is described")
    vid = video.get("vid")
    ratio = video.get("gen", {}).get("aspect_ratio")
    if (not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", vid)
            or ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")):
        raise ValueError("Authoritative source media identity or aspect ratio is unavailable")
    return {"vid": vid, "aspectRatio": ratio, "resolution": resolution, "sampleException": sample}


def _target(payload, target):
    source = payload["sourceNodeId"]
    if (target.get("status") != "empty" or target.get("resources")
            or set(target.get("upstreamNodeIds", [])) != {source}
            or set(target.get("executionDependencyNodeIds", [])) != {source}):
        raise ValueError("Target must be an empty existing draft with exactly the selected source dependency")


def _observed_body(payload, identities, source, commerce, draft=None):
    """Captured mode-9 shape plus freshly read, fixed-model commerce mapping."""
    text = payload["prompt"]
    parts = draft["parts"] if draft is not None else [{"type": "text", "text": text}, {"type": "extension"}]
    meta, prompt_parts = [], []
    for part in parts:
        if part["type"] == "text":
            meta.append({"text": part["text"], "type": 1})
            prompt_parts.append(part["text"])
        else:
            meta.extend([{"text": "参考", "type": 1}, {"material_idx": 0, "type": 3},
                {"text": "的内容，延长视频", "type": 1}, {"text": "5s", "type": 1}, {"text": "：", "type": 1}])
            prompt_parts.append("{{video:0}}")
    params = {"mode": 9, "model_name": "seedance_2.5", "aspect_ratio": source["aspectRatio"],
        "resolution": source["resolution"], "materials": [{"type": 3, "vid": source["vid"], "role": 4}],
        "meta_list": meta, "prompt": "".join(prompt_parts), "duration_ms": 5000}
    return {"project_id": payload["projectId"], "op": 1,
        "initial": {"node_id": payload["targetNodeId"], "submit_id": identities["submitId"], "items": [{
            "commerce_selection": commerce, "params": {"video_params": params},
            "reference_sources": [{"material_idx": 0, "node_id": payload["sourceNodeId"], "resource_id": payload["sourceResourceId"]}],
            "resource_id": identities["resourceId"], "type": 2,
            "metric": json.dumps({"sources": {"quote_node_array": [{"node_id": payload["sourceNodeId"], "node_type": "video", "resource_ids": [payload["sourceResourceId"]]}]}}, separators=(",", ":"))}]}}


def _status(payload, base, session, invoke):
    target = _node(payload["projectId"], payload["targetNodeId"], invoke)
    resource = _resource(payload["projectId"], payload["resourceId"], session)
    video = resource.get("video", {})
    refs = resource.get("reference_sources", [])
    if (not _resource_in_node(target, payload["resourceId"]) or resource.get("submit_id") != payload["submitId"]
            or video.get("generation_mode") != 9 or video.get("model_name") != "seedance_2.5"
            or resource.get("generation_kind") != 1 or len(refs) != 1 or refs[0].get("material_idx") != 0):
        raise ValueError("Existing resource is not the expected original ordinary extension")
    ref = refs[0]
    if (ref.get("node_id") not in target.get("upstreamNodeIds", [])
            or ref.get("node_id") not in target.get("executionDependencyNodeIds", [])
            or ("sourceNodeId" in payload and payload["sourceNodeId"] != ref.get("node_id"))
            or ("sourceResourceId" in payload and payload["sourceResourceId"] != ref.get("resource_id"))):
        raise ValueError("Existing extension source relationship does not match")
    state = {200: "running", 1000: "succeeded", 1001: "failed", 1002: "canceled", 2000: "deleted"}.get(resource.get("status"), "unknown")
    return {**base, "readOnly": True, "upstream": {"ok": True, "ret": "0", "data": {
        "projectId": payload["projectId"], "targetNodeId": payload["targetNodeId"], "submitId": payload["submitId"],
        "resourceId": payload["resourceId"], "sourceNodeId": ref["node_id"], "sourceResourceId": ref["resource_id"],
        "state": state, "upstreamStatus": resource.get("status"), "generationKind": "ordinary",
        "generationMode": "video_extend", "upstreamMode": 9, "model": "seedance_2.5",
        "extensionSeconds": video.get("gen", {}).get("duration_ms", 0) / 1000,
        "video": _metrics(resource), "doNotResubmit": True,
        "completionMeaning": "Resource state and lineage verified; visual continuity is not verified"}}}


def _submit_once(body, session):
    """One fixed HTTP POST; every unrecognized acknowledgement is UNKNOWN."""
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": session["cookie"], "User-Agent": session["userAgent"], **session["headers"]}
    try:
        status, raw = web_actions._send(GENERATE_PATH, body, headers)
        if len(raw) > web_actions.MAX_RESPONSE_BYTES:
            raise RuntimeError()
        envelope = json.loads(raw)
        if not 200 <= status < 300 or not isinstance(envelope, dict) or "ret" not in envelope:
            raise RuntimeError()
        if str(envelope["ret"]) != "0":
            return False
        results = envelope.get("data", {}).get("results", [])
        if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                or results[0].get("resource_id") != body["initial"]["items"][0]["resource_id"]
                or type(results[0].get("error_code")) is not int):
            raise RuntimeError()
        if results[0]["error_code"] != 0:
            return False
        if results[0].get("state") != 1:
            raise RuntimeError()
        return True
    except Exception:
        # Never return a normal COMPLETED result after an uncertain write.
        # The server persists UNKNOWN and exposes the original recovery IDs.
        raise RuntimeError("Extension submission outcome is unknown; inspect original identities, never resubmit") from None


def execute(payload, job_id, *, invoke):
    validate(payload)
    base = _base(payload)
    try:
        session = web_actions._load_session()
        if payload["action"] == "video.extend.status":
            return _status(payload, base, session, invoke)
        source_node = _node(payload["projectId"], payload["sourceNodeId"], invoke)
        source_resource = _resource(payload["projectId"], payload["sourceResourceId"], session)
        source = _source(payload, source_node, source_resource)
        commerce = _commerce(source["resolution"], session)
        target = _node(payload["projectId"], payload["targetNodeId"], invoke)
        _target(payload, target)
        snapshot = _draft(payload["projectId"], session)
        target_draft = _target_draft(payload, source, snapshot)
    except web_actions.SessionError as exc:
        return _failure(base, exc.code, exc.message)
    except ValueError as exc:
        # These messages are local validations; raw upstream diagnostics and
        # credential-bearing transport exceptions are never echoed.
        return _failure(base, "EXTEND_PREFLIGHT_REJECTED", str(exc))
    except Exception:
        return _failure(base, "EXTEND_READ_UNAVAILABLE", "Authoritative read failed; no write or generation was attempted")
    identity = recovery(payload, job_id)
    plan = {"sourceValidated": True, "source": {"nodeId": payload["sourceNodeId"], "resourceId": payload["sourceResourceId"],
            "sampleException": source["sampleException"], "video": _metrics(source_resource)},
        "targetStateValidated": True, "rawTargetDraftVerified": True, "targetAutoCreated": False,
        "canonicalDraft": {k: v for k, v in target_draft.items() if k != "parts"},
        "targetNodeId": payload["targetNodeId"], "model": "seedance_2.5", "generationKind": "ordinary",
        "mode": "video_extend", "upstreamMode": 9, "extensionSeconds": 5,
        "resolution": source["resolution"], "ordinaryModelAcknowledged": payload.get("allowOrdinaryModel", False),
        "commerceSelection": commerce, "commerceReadPath": SPECS_PATH,
        "fixedGenerationPath": GENERATE_PATH, "submitRequestSent": False,
        "serverExecutionVerified": False, "limitations": list(BLOCKERS)}
    if payload.get("dryRun", True):
        return {**base, "dryRun": True, "dryRunKind": "read-only-preflight", "recovery": identity,
                "plan": plan, "upstream": {"ok": True, "data": {"preflightOnly": True,
                    "preparedPromptMatches": target_draft["promptMatchesRequested"],
                    "liveSubmissionRequirements": ["service write/unlimited policy", "allowOrdinaryModel:true",
                        "expectedDraftVersion matching current head", "exact saved prompt"]}}}
    if not target_draft["promptMatchesRequested"]:
        return _failure({**base, "recovery": identity, "plan": plan}, "EXTEND_DRAFT_PROMPT_MISMATCH",
                        "Save the exact requested prompt in the target draft before live submission")
    # Recheck both graph head and empty runtime target immediately before the
    # write. The upstream endpoint has no observed atomic version precondition;
    # this detects changes during preflight but cannot lock another editor.
    try:
        _target(payload, _node(payload["projectId"], payload["targetNodeId"], invoke))
        latest = _target_draft(payload, source, _draft(payload["projectId"], session))
        if latest != target_draft:
            raise ValueError("Canonical target changed during preflight")
    except ValueError as exc:
        return _failure({**base, "recovery": identity, "plan": plan}, "EXTEND_DRAFT_CHANGED", str(exc))
    except Exception:
        return _failure({**base, "recovery": identity, "plan": plan}, "EXTEND_READ_UNAVAILABLE",
                        "Final authoritative read failed; generation was not submitted")
    body = _observed_body(payload, identity, source, commerce, target_draft)
    accepted = _submit_once(body, session)
    after = {**base, "source": "live_web", "dryRun": False, "workspaceMutationAttempted": True,
             "generationSubmitted": accepted, "recovery": identity,
             "plan": {**plan, "submitRequestSent": True, "atomicDraftVersionPrecondition": False}}
    if not accepted:
        return _failure(after, "EXTEND_SUBMISSION_REJECTED", "Upstream rejected the single extension request; no retry was sent")
    return {**after, "upstream": {"ok": True, "data": {"submissionAccepted": True,
        "generationComplete": False, "state": "accepted", "targetNodeId": payload["targetNodeId"],
        "resourceId": identity["resourceId"], "submitId": identity["submitId"],
        "generationKind": "ordinary", "upstreamMode": 9, "nextAction": "video.extend.status"}}}
