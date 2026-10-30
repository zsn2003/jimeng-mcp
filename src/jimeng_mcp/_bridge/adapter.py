"""Fixed-operation adapter for official CLI and privately configured web reads.

No client-provided credentials, arbitrary commands, URLs, or browser automation.
Command completion is NOT media generation completion.
"""
import json
import hashlib
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import official_actions
import web_actions
import canvas_web_actions
import subject_actions
import action_registry
import acceptance_registry
import input_contracts
import project_policy

GENERATION_EVIDENCE = Path(__file__).with_name("evidence") / "live-generation-acceptance.json"


FIELDS = {name: set(schema["properties"]) - {"action"} for name, schema in input_contracts.schemas().items()}


def effective_reference_count(p):
    return len(input_contracts.references(p))


def validate(p):
    if not isinstance(p, dict):
        raise input_contracts.ValidationError("payload", "INVALID_TYPE", "Request body must be an object.")
    spec = action_registry.resolve(p.get("action"))
    if "workflow" in p and "workflow" not in spec.schema["properties"]:
        raise input_contracts.ValidationError("workflow", "UNSUPPORTED_FIELD", "This action does not accept a video workflow declaration.")
    clean = project_policy.strip_workflow(p)
    provider = action_registry.provider_for(clean)
    if provider is None:
        input_contracts.validate(clean)
    else:
        provider.validate(clean)
    project_policy.enforce(p, action_registry.effects_for(p))


def binary():
    return os.environ.get("DREAMINA_CANVAS_BIN") or shutil.which("dreamina-canvas")


def read_only():
    return os.environ.get("JIMENG_READ_ONLY", "1") != "0"


def credit_cap():
    """Only an explicit operator setting enables the user's unlimited policy."""
    value = os.environ.get("JIMENG_MAX_CREDITS", "0").strip()
    if value == "unlimited":
        return None
    cap = int(value)
    if cap < 0:
        raise ValueError("JIMENG_MAX_CREDITS must be nonnegative or unlimited")
    return cap


def spending_flags(payload):
    if not payload.get("dryRun", True) and credit_cap() is None and "creditCeiling" not in payload:
        # Official CLI's unlimited approval is enabled only by the current
        # operator's explicit JIMENG_MAX_CREDITS=unlimited configuration.
        return ["--yes"]
    return ["--credit-ceiling", str(payload.get("creditCeiling", 0))]


def _redact(value):
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if isinstance(v, str) and re.search(r"token|cookie|secret|devicecode|authorization|signature", k, re.I) else _redact(v)) for k, v in value.items()}
    return value


def _invoke(args, *, redact):
    exe = binary()
    if not exe:
        return {"ok": False, "error": {"code": "CLI_UNAVAILABLE", "message": "Official dreamina-canvas binary is not configured"}}
    proc = subprocess.run([exe, "--format", "json", "--non-interactive", "--profile", os.environ.get("DREAMINA_CANVAS_PROFILE", "jimeng-mcp"), *args], capture_output=True, text=True, timeout=90, check=False, stdin=subprocess.DEVNULL)
    result = None
    for stream in (proc.stdout, proc.stderr):
        try:
            candidate = json.loads(stream)
            if isinstance(candidate, dict) and type(candidate.get("ok")) is bool:
                result = candidate
                break
        except (ValueError, TypeError):
            pass
    if result is None:
        raise RuntimeError("CLI returned invalid JSON; query the original operation before retry")
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise RuntimeError("Unexpected official CLI response envelope")
    return {"exitCode": proc.returncode, **(_redact(result) if redact else result)}


def invoke(args):
    return _invoke(args, redact=True)


def invoke_sensitive(args):
    """One internal approval exchange; never exposed as an HTTP action."""
    if not isinstance(args, list) or args[:2] != ["node", "confirm"]:
        raise ValueError("Sensitive invocation is restricted to node confirm")
    return _invoke(args, redact=False)


def _live_generation_verification():
    """Report this acceptance run's exact scopes, never a blanket live claim."""
    unverified = {"status": "not_verified", "verifiedAt": None, "actions": [],
                  "scope": "No complete sample-and-promotion resource evidence is available"}
    try:
        if GENERATION_EVIDENCE.stat().st_size > 2 * 1024 * 1024:
            return unverified
        raw_evidence = GENERATION_EVIDENCE.read_text(encoding="utf-8")
        digest = hashlib.sha256(raw_evidence.encode("utf-8")).hexdigest()
        for action in ("video.sample", "video.promote"):
            records = acceptance_registry.verified(action, "source_linked_sample_flow")
            if not any(row["sha256"] == digest and row["evidence"] == "evidence/live-generation-acceptance.json" for row in records):
                return unverified
        evidence = json.loads(raw_evidence)
        if evidence.get("sampleVerified") is not True or evidence.get("promotionVerified") is not True:
            return unverified
        verified_at = evidence["completedAt"]
        if datetime.fromisoformat(verified_at.replace("Z", "+00:00")).tzinfo is None:
            return unverified
        project = evidence["project"]["projectId"]
        if str(uuid.UUID(project)) != project:
            return unverified
        steps = evidence["steps"]
        outputs = {}
        for label, action, kind, resource_key, identity_key in (
                ("sample", "video.sample", "sample", "sampleResources", "sampleIdentity"),
                ("promotion", "video.promote", "final_from_sample", "promotionResources", "promotionIdentity")):
            entry, identity = steps[label], evidence[identity_key]
            payload, upstream = entry["payload"], entry["result"]["upstream"]
            if (entry["status"] != "COMPLETED" or upstream.get("ok") is not True
                    or payload.get("action") != action or payload.get("dryRun") is not False
                    or payload.get("projectId") != project):
                return unverified
            resources = evidence[resource_key]
            if not isinstance(resources, list) or len(resources) != 1:
                return unverified
            resource = resources[0]
            resource_id, submit_id, node_id = resource["resourceId"], identity["submitId"], identity["nodeId"]
            if (str(uuid.UUID(resource_id)) != resource_id or str(uuid.UUID(submit_id)) != submit_id
                    or not re.fullmatch(r"node_[0123456789abcdefghjkmnpqrstvwxyz]{10}", node_id)
                    or resource.get("status") != "success" or resource.get("type") != "video"
                    or resource.get("generationKind") != kind
                    or resource.get("video", {}).get("resolution") != payload.get("resolution")):
                return unverified
            data = upstream["data"]
            if label == "sample":
                if data["node"]["nodeId"] != node_id or data["submission"]["submitId"] != submit_id:
                    return unverified
            elif (data["nodeId"] != node_id or data["submitId"] != submit_id
                    or data["sourceNodeId"] != evidence["sampleIdentity"]["nodeId"]
                    or data["sourceResourceId"] != evidence["sampleResources"][0]["resourceId"]
                    or resource_id not in data["resourceIds"]):
                return unverified
            terminal = False
            resource_read = False
            for read_step in steps.values():
                read_payload = read_step.get("payload", {})
                read_upstream = (read_step.get("result") or {}).get("upstream", {})
                if (read_step.get("status") != "COMPLETED" or read_upstream.get("ok") is not True
                        or read_payload.get("projectId") != project):
                    continue
                read_data = read_upstream.get("data", {})
                if (read_payload.get("action") == "operation.status" and read_payload.get("submitId") == submit_id
                        and read_data.get("state") == "succeeded"
                        and any(x.get("resourceId") == resource_id and x.get("state") == "succeeded"
                                for x in read_data.get("resources", []))):
                    terminal = True
                if (read_payload.get("action") == "resource.read" and read_payload.get("resourceId") == resource_id
                        and read_data == resource):
                    resource_read = True
            if not terminal or not resource_read:
                return unverified
            outputs[action] = {"nodeId": node_id, "submitId": submit_id, "resourceId": resource_id,
                               "generationKind": kind, "video": resource["video"]}
        return {"status": "verified", "verifiedAt": verified_at, "actions": list(outputs),
                "projectId": project, "model": steps["sample"]["payload"]["model"],
                "mode": steps["sample"]["payload"].get("mode", "t2v"), "outputs": outputs,
                "scope": "One official sample and its source-linked promotion reached successful video resources; other operations and visual quality are not certified",
                "visualQualityVerified": False, "evidence": "evidence/live-generation-acceptance.json"}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unverified


def capabilities():
    registry = action_registry.operations()
    groups = {name: module.capabilities() for name, module in action_registry.providers().items()}
    for name, group in groups.items():
        acceptance_registry.annotate(group["actions"])
        for action, spec in group["actions"].items():
            if action not in registry:
                spec["liveExecutionAllowed"] = False
                continue
            effects = registry[action].effects
            enabled = spec.get("available", spec.get("implemented", False)) and not (effects["writes"] and read_only())
            if effects["requiresUnlimited"] and credit_cap() is not None:
                enabled = False
            if effects["billable"] and credit_cap() is not None and credit_cap() <= 0:
                enabled = False
            spec["liveExecutionAllowed"] = enabled
            spec["workflowPolicyRequired"] = effects["videoGeneration"] and effects["workflowStage"] in ("ordinary", "extend")
    baseline = acceptance_registry.annotate({name: {"implemented": True, "available": bool(binary())} for name in FIELDS})
    web = groups["web"]
    return {"adapter": "jimeng-studio-bridge", "contractVersion": "0.6.0",
        "cliConfigured": bool(binary()), "actions": sorted(registry),
        "registeredOfficialActions": groups["official"]["actions"],
        "registeredWebActions": web["actions"],
        "registeredCanvasWebActions": groups["canvas_web"]["actions"],
        "registeredSubjectActions": groups["subject"]["actions"],
        "registeredPersonalActions": groups["personal"]["actions"],
        "registeredPluginActions": groups.get("plugin", {}).get("actions", {}),
        "registeredBaselineActions": baseline,
        "webSession": {k:v for k,v in web.items() if k != "actions"},
        "readOnly": read_only(), "liveWritesEnabled": not read_only(),
        "liveGenerationVerification": _live_generation_verification(),
        "liveCreditCeiling": credit_cap(), "creditPolicy": "unlimited" if credit_cap() is None else "limited",
        "videoDefault": "dry-run; sample generation must be verified by the live model's generationKind=sample",
        "workflowPolicy": {"default": os.environ.get("JIMENG_VIDEO_WORKFLOW_POLICY", "general"),
            "ordinaryApproval": "Per-request caller attestation bound once to one job and exact payload; never a reusable global exception.",
            "sampleStages": ["video.sample", "video.promote"], "sourceLinkedExtension": "video.extend"},
        "studioCatalog": {"path": "/v1/studio/catalog", "kind": "coverage_and_acceptance", "serverWebAdapterEnabled": web["authConfigured"]},
        "unavailable": {"video.extend.auto_prepare": "An existing native extension draft is required; automatic target creation remains unavailable",
            "personal.subject.cross_clip_consistency": "Imported reference bindings are verified separately from generated cross-clip visual or voice consistency",
            "studio.script.auto_generate": "Fixed script/storyboard operations do not implement all AI authoring features",
            "director.render": "Not enabled: requires external browser rendering"},
        "completionSemantics": "status describes bridge execution; operationOutcome and reconciliationRequired describe business certainty; mediaState remains separate."}


def studio_catalog():
    """Static capability gaps, not a tool dispatch table or live subject query."""
    return acceptance_registry.annotate_catalog(json.loads(Path(__file__).with_name("studio_capabilities.json").read_text(encoding="utf-8")))


def recovery(p, job_id):
    p = project_policy.strip_workflow(p)
    provider = action_registry.provider_for(p)
    if provider is not None:
        return provider.recovery(p, job_id)
    stable = str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:" + job_id))
    if p.get("action") in ("video.sample", "video.promote"):
        identity = {"projectId": p["projectId"], "submitId": stable, "nextAction": "operation.status", "doNotResubmit": True,
                    "note": "If operation status is absent, inspect the original node; absence does not authorize resubmission."}
        if p["action"] == "video.sample":
            alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
            number = uuid.UUID(stable).int
            identity.update(nodeId="node_" + "".join(alphabet[(number >> (5*i)) & 31] for i in range(9, -1, -1)), updateId=stable)
        else:
            identity["sourceNodeId"] = p["nodeId"]
        return identity
    if p.get("action") == "canvas.create":
        return {"projectId": stable, "nextAction": "canvas.list", "doNotResubmit": True}
    return None


def execute(p, job_id):
    validate(p)
    policy = project_policy.enforce(p, action_registry.effects_for(p), job_id=job_id, claim=True)
    p = project_policy.strip_workflow(p)
    spec = action_registry.resolve(p["action"])
    module = action_registry.provider_for(p)
    if spec.provider == "official":
        result = module.execute(p, job_id, invoke=invoke, invoke_sensitive=invoke_sensitive)
    elif spec.provider in ("subject", "canvas_web"):
        result = module.execute(p, job_id, invoke=invoke)
    elif spec.provider in ("web", "personal"):
        result = module.execute(p, job_id)
    elif spec.provider == "plugin":
        result = module.execute(p, job_id, resolve_media=resolve_plugin_media)
    else:
        result = _execute_baseline(p, job_id)
    if isinstance(result, dict):
        result["workflowPolicy"] = policy
    return result


def _execute_baseline(p, job_id):
    a = p["action"]
    stable = str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:" + job_id))
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    number = uuid.UUID(stable).int
    node_id = "node_" + "".join(alphabet[(number >> (5*i)) & 31] for i in range(9, -1, -1))
    flags = []
    if "projectId" in p:
        flags += ["--project-id", p["projectId"]]
    if a == "model.list":
        args = ["model", "list", "--type", p.get("type", "video")]
    elif a == "model.inspect":
        args = ["model", p["model"], "--type", p.get("type", "video")]
    elif a == "video.sample":
        if not p.get("dryRun", True):
            spec = invoke(["model", p["model"], "--type", "video"])
            if not spec.get("ok"):
                return {"phase": "sample-model-verification", "upstream": spec, "submitted": False}
            matches = spec.get("data", {}).get("matches", [])
            matching = []
            reference_count = effective_reference_count(p)
            for match in matches:
                if not isinstance(match, dict) or match.get("resourceType") != "video":
                    continue
                generation = match.get("generation")
                if not isinstance(generation, dict) or generation.get("model") != p["model"] or generation.get("mode") != p.get("mode", "t2v"):
                    continue
                # One canonical mode can have separate first-frame and
                # first/last-frame capabilities. Resolve the reference count
                # before checking that every applicable capability is a sample.
                materials = generation.get("materials", {})
                total_count = materials.get("totalCount") if isinstance(materials, dict) else None
                if total_count is not None:
                    minimum = total_count.get("min") if isinstance(total_count, dict) else None
                    maximum = total_count.get("max") if isinstance(total_count, dict) else None
                    if type(minimum) is not int or type(maximum) is not int or not 0 <= minimum <= maximum:
                        return {"submitted": False, "error": {"code": "SAMPLE_MODEL_REQUIRED", "message": "Sample reference-count capability is invalid; generation was not submitted"}}
                    if not minimum <= reference_count <= maximum:
                        continue
                matching.append(generation)
            if not matching or any(generation.get("generationKind") != "sample" for generation in matching):
                return {"submitted": False, "error": {"code": "SAMPLE_MODEL_REQUIRED", "message": "Canonical model, mode and reference count must resolve only to video generationKind=sample capabilities; ordinary generation is never a fallback"}}
        args = ["node", "create", "video", *flags, "--node-id", node_id, "--update-id", stable, "--mode", p.get("mode", "t2v"), "--model", p["model"], "--prompt", p["prompt"], "--title", p.get("title", "样片 " + job_id[:8]), "--duration", str(p.get("duration", 5)), "--count", "1", "--run", "--submit-id", stable, *spending_flags(p)]
        for field in ("ratio", "resolution"):
            if field in p:
                args += ["--" + field, p[field]]
        for ref in input_contracts.references(p):
            args += ["--ref", ref]
        if p.get("dryRun", True):
            args.append("--dry-run")
    elif a == "video.promote":
        args = ["node", "promote", "video-sample", *flags, "--node-id", p["nodeId"], "--resolution", p["resolution"], "--submit-id", stable, *spending_flags(p)]
        if p.get("dryRun", True):
            args.append("--dry-run")
    else:
        raise ValueError("Unsupported action")
    result = invoke(args)
    return {"action": a, "submitId": stable if a in ("video.sample", "video.promote") else None, "createdProjectId": stable if a == "canvas.create" else None, "dryRun": p.get("dryRun", True) if a.startswith("video.") else False, "upstream": result, "note": "A successful command can mean only accepted. Query operation.status and resource.read to verify actual media results."}


def artifact_descriptor(payload):
    return official_actions.artifact_descriptor(payload)


def resolve_plugin_media(project_id, artifact_id):
    descriptor = artifact_descriptor({"projectId": project_id, "artifactId": artifact_id, "kind": "resource"})
    kind = descriptor["format"].lower()
    if kind not in ("png", "jpg", "jpeg", "webp"):
        raise ValueError("Official multi-frame generation accepts managed image artifacts only")
    path = Path(descriptor["path"])
    if descriptor["byteSize"] > 30 * 1024 * 1024 or official_actions._director_sha(path) != descriptor["sha256"]:
        raise ValueError("Managed image no longer matches its accepted integrity record")
    return {**descriptor, "mimeType": "image/jpeg" if kind in ("jpg", "jpeg") else "image/" + kind}
