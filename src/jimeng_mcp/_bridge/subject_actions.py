"""Import an authenticated personal subject into one new canvas element.

The original personal subject is never edited. Image URIs come only from its
live record and use the observed resource/upload contract. A saved MP3 voice is
copied through the official managed upload path. Caller-supplied URLs, bytes,
resource IDs, headers and private file paths are not accepted. No generation.
"""
from __future__ import annotations
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import urllib.error
import urllib.parse
import urllib.request
import uuid

import official_actions as official
import web_actions as web
from upstream_contracts import parse_node_show, UpstreamContractError

ACTION = "subject.import_element"
INSPECT_ACTION = "subject.import.inspect"
RECONCILE_ACTION = "subject.import.reconcile"
INSPECTION_ACTIONS = (INSPECT_ACTION, RECONCILE_ACTION)
MAX_AUXILIARIES = 16
MAX_VOICE_BYTES = official.UPLOAD_CHUNK_BYTES
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
FIELDS = {
    "projectId": deepcopy(official.UUID_FIELD),
    "subjectId": web._str(64, pattern=r"^[0-9]+$"),
    "title": web._str(512),
    "includeAuxiliary": {"type": "boolean", "default": True},
    "includeVoice": {"type": "boolean", "default": True},
    "voiceIndex": web._int(0, 99),
    "dryRun": {"type": "boolean", "default": True},
}


def supports(action):
    return action == ACTION or action in INSPECTION_ACTIONS


def request_schemas():
    schemas = {ACTION: {"type": "object", "additionalProperties": False,
        "properties": {"action": {"type": "string", "const": ACTION}, **deepcopy(FIELDS)},
        "required": ["action", "projectId", "subjectId"],
        "description": "Copy the live personal subject's images and optional saved voice into a NEW element. Original subject unchanged. No generation. A single voice defaults to index 0; multiple voices require voiceIndex. Default dryRun=true."}}
    for action in INSPECTION_ACTIONS:
        schemas[action] = {"type": "object", "additionalProperties": False,
            "properties": {"action": {"type": "string", "const": action},
                "projectId": deepcopy(official.UUID_FIELD), "operationId": deepcopy(official.UUID_FIELD)},
            "required": ["action", "projectId", "operationId"],
            "description": "Read only the original import journal's exact planned node/resources and verify bindings and description hash. No upload, creation or generation. "
                + ("Append a separate local reconciliation receipt; preserve the original import result." if action == RECONCILE_ACTION else "Do not modify the journal.")}
    return schemas


def validate(payload):
    if not isinstance(payload, dict) or not supports(payload.get("action")):
        raise ValueError("Unsupported subject operation")
    spec = request_schemas()[payload["action"]]
    if set(payload) - set(spec["properties"]) or set(spec["required"]) - set(payload):
        raise ValueError("Invalid fixed subject import fields")
    for name, value in payload.items():
        if name != "action":
            web._check(value, spec["properties"][name], name)
    for key in ("projectId", "operationId"):
        if key in payload and str(uuid.UUID(payload[key])) != payload[key]:
            raise ValueError(key + " must be a canonical UUID")
    if payload["action"] in INSPECTION_ACTIONS:
        return
    if payload.get("includeVoice") is False and "voiceIndex" in payload:
        raise ValueError("voiceIndex conflicts with includeVoice=false")
    if not payload.get("dryRun", True) and web.read_only():
        raise ValueError("Service is read-only; live subject import is disabled")


def is_mutating(action):
    return action in (ACTION, RECONCILE_ACTION)


def _identity(job_id, suffix):
    if not isinstance(job_id, str) or not job_id or len(job_id) > 180 or "\x00" in job_id:
        raise ValueError("A stable bounded job identity is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:subject-import:" + job_id + ":" + suffix))


def recovery(payload, job_id):
    if payload.get("action") in INSPECTION_ACTIONS:
        return {"projectId": payload["projectId"], "operationId": payload["operationId"],
            "nextAction": INSPECT_ACTION, "upstreamMutationAllowed": False,
            "query": {"action": INSPECT_ACTION, "projectId": payload["projectId"], "operationId": payload["operationId"]}}
    if payload.get("dryRun", True):
        return None
    node_id = official._identities(job_id + ":element")[1]
    return {"projectId": payload["projectId"], "subjectId": payload["subjectId"],
        "operationId": _identity(job_id, "operation"), "nodeId": node_id,
        "mainResourceId": _identity(job_id, "image-0"),
        "voiceResourceId": official._identities(job_id + ":voice")[0],
        "doNotResubmit": True, "upstreamIdempotencyVerified": False,
        "query": {"action": "node.read", "projectId": payload["projectId"], "nodeId": node_id},
        "note": "IDs are planned recovery identities, not proof of creation. Inspect original job and resources; never automatically start a new import after an uncertain outcome."}


def capabilities():
    try:
        web._load_session()
        configured = True
    except web.SessionError:
        configured = False
    actions = {ACTION: {"implemented": True, "available": configured,
        "liveExecutionAllowed": configured and not web.read_only(), "mutatesWorkspace": True,
        "billable": False, "generationSubmitted": False, "defaultDryRun": True,
        "sourceSubjectModified": False,
        "limits": {"maxAuxiliaryImages": MAX_AUXILIARIES, "maxVoiceBytes": MAX_VOICE_BYTES, "voiceFormat": "mp3"},
        "notes": "New frozen asset copies preserve source IDs/version metadata; future subject edits do not auto-update the canvas element. Voice identity/quality across generation is not certified."}}
    for action in INSPECTION_ACTIONS:
        actions[action] = {"implemented": True, "available": True,
            "liveExecutionAllowed": True, "mutatesWorkspace": False,
            "mutatesLocalState": action == RECONCILE_ACTION,
            "billable": False, "generationSubmitted": False, "readOnlyUpstream": True,
            "notes": "Needs the private original import journal and official CLI account. Never retries an import or changes original receipts."}
    for action, spec in actions.items():
        records = _verified(action)
        spec.update(serverExecutionVerified=bool(records), executionVerification=records)
    return {"actions": actions}


def _verified(action):
    try:
        import acceptance_registry
        return acceptance_registry.verified(action)
    except (ImportError, OSError, ValueError, TypeError):
        return []


class ImportFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _root(*, create=True):
    parent = os.environ.get("DREAMINA_CANVAS_STATE_DIR")
    if not parent or not Path(parent).is_absolute():
        raise ImportFailure("SUBJECT_STATE_NOT_CONFIGURED", "Configure a private canvas state directory")
    path = Path(parent) / "subject-imports"
    if path.is_symlink():
        raise ImportFailure("SUBJECT_STATE_INVALID", "Subject journal cannot be a symbolic link")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ImportFailure("SUBJECT_STATE_INVALID", "Subject journal must be private")
    return path


def _save(path, record):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(record, out, ensure_ascii=False, separators=(",", ":"))
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def _begin(payload, job_id):
    path = _root() / (_identity(job_id, "operation") + ".json")
    record = {"operationId": _identity(job_id, "operation"), "jobId": job_id,
        "payload": payload, "phase": "preflight", "state": "started", "createdResources": [],
        "startedAt": datetime.now(timezone.utc).isoformat()}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ImportFailure("SUBJECT_STATE_INVALID", "Subject journal file is not private")
        previous = json.loads(path.read_text())
        if previous.get("payload") != payload:
            raise ImportFailure("SUBJECT_IDENTITY_CONFLICT", "The original import job has a different payload")
        if previous.get("state") == "complete" and isinstance(previous.get("result"), dict):
            return path, previous, {**previous["result"], "replayedOriginalReceipt": True}
        raise ImportFailure("SUBJECT_RECONCILIATION_REQUIRED", "This import already started; inspect the original job and planned IDs before any new import")
    with os.fdopen(fd, "w") as out:
        json.dump(record, out, ensure_ascii=False)
    return path, record, None


def _safe_image(info):
    if not isinstance(info, dict):
        raise ImportFailure("SUBJECT_IMAGE_INVALID", "Personal subject has no usable image metadata")
    uri = info.get("image_uri")
    if (not isinstance(uri, str) or len(uri) > 512 or not re.fullmatch(r"tos-[a-z0-9-]+/[A-Za-z0-9/_-]+", uri)
            or any(type(info.get(k)) is not int or not 0 < info[k] <= 32768 for k in ("width", "height"))):
        raise ImportFailure("SUBJECT_IMAGE_INVALID", "Subject image URI or dimensions do not match the observed import contract")
    fmt = info.get("format", "")
    if fmt not in ("", "png", "jpg", "jpeg", "webp"):
        raise ImportFailure("SUBJECT_IMAGE_INVALID", "Subject image format is outside the observed image contract")
    return {"uri": uri, "width": info["width"], "height": info["height"], "format": fmt}


def _voice_info(content, payload):
    if payload.get("includeVoice", True) is False:
        return None, None
    voices = content.get("voice_list") or []
    if not isinstance(voices, list):
        raise ImportFailure("SUBJECT_VOICE_INVALID", "Subject voice list has an unsupported shape")
    if not voices:
        if "voiceIndex" in payload:
            raise ImportFailure("SUBJECT_VOICE_INVALID", "Selected voice is absent")
        return None, None
    if len(voices) > 1 and "voiceIndex" not in payload:
        raise ImportFailure("SUBJECT_VOICE_SELECTION_REQUIRED", "The subject has multiple voices; specify voiceIndex explicitly")
    index = payload.get("voiceIndex", 0)
    if index >= len(voices) or not isinstance(voices[index], dict) or not isinstance(voices[index].get("audio_info"), dict):
        raise ImportFailure("SUBJECT_VOICE_INVALID", "Selected saved voice is absent or invalid")
    info = voices[index]["audio_info"]
    if info.get("format") != "mp3" or not isinstance(info.get("vid"), str) or not info["vid"]:
        raise ImportFailure("SUBJECT_VOICE_INVALID", "Current fixed import supports saved MP3 voices with a real vid")
    return index, info


def _download_voice(info):
    # URL is taken only from the authenticated subject result, never HTTP input.
    url = info.get("audio_url") or info.get("url")
    if not isinstance(url, str) or len(url) > 10000:
        raise ImportFailure("SUBJECT_VOICE_URL_INVALID", "Saved voice has no usable material URL")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443)
            or not re.fullmatch(r"v[0-9]+-artist\.vlabvod\.com", parts.hostname or "")):
        raise ImportFailure("SUBJECT_VOICE_URL_INVALID", "Saved voice is outside the observed HTTPS media origin allowlist")
    addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ImportFailure("SUBJECT_VOICE_URL_INVALID", "Saved voice media origin did not resolve to a public address")
    request = urllib.request.Request(url, headers={"Accept": "audio/mpeg", "User-Agent": "JimengBridge/1.0"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), web._NoRedirect())
    try:
        with opener.open(request, timeout=25) as response:
            if response.code != 200:
                raise ImportFailure("SUBJECT_VOICE_DOWNLOAD_FAILED", "Saved voice media was not returned successfully")
            data = response.read(MAX_VOICE_BYTES + 1)
    except ImportFailure:
        raise
    except Exception:
        raise ImportFailure("SUBJECT_VOICE_DOWNLOAD_FAILED", "Saved voice download failed; no credentials were sent to the media host") from None
    if not 0 < len(data) <= MAX_VOICE_BYTES:
        raise ImportFailure("SUBJECT_VOICE_SIZE_LIMIT", "Saved voice is empty or exceeds the fixed import byte limit")
    if not (data.startswith(b"ID3") or len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        raise ImportFailure("SUBJECT_VOICE_FORMAT_INVALID", "Saved voice bytes do not have a supported MP3 header")
    return data


def _upload_image(project, resource_id, info, name, session):
    body = {"project_id": project, "resource_id": resource_id, "type": 1, "value": info["uri"],
        "image_info": {"width": info["width"], "height": info["height"], "format": info["format"]}}
    if name:
        body["name"] = name
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Cookie": session["cookie"],
        "User-Agent": session["userAgent"], **session["headers"]}
    try:
        status, raw = web._send("/octo_api/v1/resource/upload", body, headers)
        result = json.loads(raw)
    except Exception:
        raise ImportFailure("SUBJECT_IMAGE_UPLOAD_UNCERTAIN", "Image registration outcome is uncertain; inspect the planned resource ID before any retry") from None
    if status != 200 or not isinstance(result, dict) or str(result.get("ret")) != "0":
        raise ImportFailure("SUBJECT_IMAGE_UPLOAD_REJECTED", "The platform rejected the subject image registration")
    resource = (result.get("data") or {}).get("resource", {})
    if (resource.get("resource_id") != resource_id or resource.get("type") != 1
            or resource.get("image", {}).get("uri") != info["uri"]):
        raise ImportFailure("SUBJECT_IMAGE_UPLOAD_UNCERTAIN", "Image registration receipt did not match the planned source and resource identity")
    return {"resourceId": resource_id, "sourceUri": info["uri"], "width": info["width"], "height": info["height"]}


def _official(payload, job_id, invoke):
    result = official.execute(payload, job_id, invoke=invoke)
    if result.get("upstream", {}).get("ok") is not True or result.get("error"):
        raise ImportFailure("SUBJECT_OFFICIAL_STEP_FAILED", "An official import step did not verify successfully; inspect the original job and resource identities")
    return result


def _binding_matches(binding, resource_id, media_type, resources):
    if not isinstance(binding, dict) or not isinstance(resources, list):
        return False
    # Official node.show resolves bindings and resource state into separate
    # fields: subjectBindings.{main,voice,auxiliaries}, plus node.resources.
    matches = [r for r in resources if isinstance(r, dict) and r.get("resourceId") == resource_id]
    return (binding.get("id") == resource_id and binding.get("kind") == "resource"
        and binding.get("type") == media_type and len(matches) == 1
        and matches[0].get("type") == media_type and matches[0].get("status") == "success")


def _load_original(payload):
    path = _root(create=False) / (payload["operationId"] + ".json")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_JOURNAL_BYTES:
                raise ImportFailure("SUBJECT_STATE_INVALID", "The original import journal is not a bounded private file")
            raw = source.read(MAX_JOURNAL_BYTES + 1)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise ImportFailure("SUBJECT_STATE_INVALID", "The original import journal exceeds the read bound")
        record = json.loads(raw)
    except FileNotFoundError:
        raise ImportFailure("SUBJECT_IMPORT_NOT_FOUND", "The original import journal was not found") from None
    except (OSError, ValueError, UnicodeError):
        raise ImportFailure("SUBJECT_STATE_INVALID", "The original import journal cannot be read safely") from None
    original = record.get("payload") if isinstance(record, dict) else None
    original_job = record.get("jobId") if isinstance(record, dict) else None
    if (not isinstance(original, dict) or original.get("action") != ACTION
            or original.get("projectId") != payload["projectId"] or original.get("dryRun", True) is not False
            or not isinstance(original_job, str) or not original_job
            or len(original_job) > 180 or "\x00" in original_job
            or record.get("operationId") != payload["operationId"]
            or _identity(original_job, "operation") != payload["operationId"]):
        raise ImportFailure("SUBJECT_JOURNAL_IDENTITY_MISMATCH", "The journal does not match the original import and requested project")
    return path, record, hashlib.sha256(raw).hexdigest()


def _expected_import(record):
    """Derive identities from the original job, never from new caller fields."""
    source = record.get("sourceSubject")
    images = record.get("plannedImages")
    original = record["payload"]
    job_id = record["jobId"]
    if (not isinstance(source, dict) or source.get("subjectId") != original.get("subjectId")
            or not isinstance(source.get("descriptionSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", source["descriptionSha256"])
            or not isinstance(images, list) or not 1 <= len(images) <= MAX_AUXILIARIES + 1):
        raise ImportFailure("SUBJECT_PLAN_INCOMPLETE", "The original journal has no complete source snapshot and resource plan")
    expected_images = []
    for index, image in enumerate(images):
        expected = _identity(job_id, "image-" + str(index))
        if not isinstance(image, dict) or image.get("resourceId") != expected:
            raise ImportFailure("SUBJECT_JOURNAL_IDENTITY_MISMATCH", "A planned image does not match the original job identity")
        expected_images.append(expected)
    if (source.get("imageUris") != [image.get("uri") for image in images]
            or original.get("includeAuxiliary") is False and len(images) != 1):
        raise ImportFailure("SUBJECT_JOURNAL_IDENTITY_MISMATCH", "The original source snapshot and image plan disagree")
    voice = official._identities(job_id + ":voice")[0] if source.get("voiceVid") is not None else None
    if original.get("includeVoice") is False and voice is not None:
        raise ImportFailure("SUBJECT_JOURNAL_IDENTITY_MISMATCH", "The original voice selection and import plan disagree")
    node_id = official._identities(job_id + ":element")[1]
    stored_recovery = record.get("elementRecovery")
    if stored_recovery is not None and (not isinstance(stored_recovery, dict) or stored_recovery.get("nodeId") != node_id):
        raise ImportFailure("SUBJECT_JOURNAL_IDENTITY_MISMATCH", "The planned element does not match the original job identity")
    return {"nodeId": node_id, "main": expected_images[0], "auxiliaries": expected_images[1:],
            "voice": voice, "descriptionSha256": source["descriptionSha256"],
            "resources": [(resource_id, "image") for resource_id in expected_images]
                + ([(voice, "audio")] if voice else [])}


def _inspect_snapshot(payload, job_id, record, expected, invoke):
    node, read_errors = None, []
    try:
        result = _official({"action": "node.read", "projectId": payload["projectId"],
            "nodeId": expected["nodeId"]}, job_id + ":inspect-node", invoke)
        node = parse_node_show(result["upstream"], expected["nodeId"], expected_type="element")
    except UpstreamContractError as exc:
        read_errors.append(exc.code)
    except Exception:
        read_errors.append("SUBJECT_NODE_READ_UNAVAILABLE")
    resource_checks = []
    for resource_id, media_type in expected["resources"]:
        check = {"resourceId": resource_id, "expectedType": media_type, "identityVerified": False, "ready": False}
        try:
            resource = _official({"action": "resource.read", "projectId": payload["projectId"],
                "resourceId": resource_id}, job_id + ":inspect-resource:" + resource_id, invoke)["upstream"]["data"]
            check["identityVerified"] = isinstance(resource, dict) and resource.get("resourceId") == resource_id and resource.get("type") == media_type
            check["ready"] = check["identityVerified"] and resource.get("status") == "success"
            check["status"] = resource.get("status") if isinstance(resource, dict) else None
        except Exception:
            check["readError"] = "SUBJECT_RESOURCE_READ_UNAVAILABLE"
            read_errors.append(check["readError"])
        resource_checks.append(check)
    bindings = (node.get("subjectBindings") or {}) if node is not None else {}
    resources = node.get("resources") if node is not None else None
    auxiliaries = (bindings.get("auxiliaries") or []) if isinstance(bindings, dict) else []
    description = node.get("description", "") if node is not None else None
    checks = {"nodeVerified": node is not None,
        "descriptionHashVerified": isinstance(description, str) and hashlib.sha256(description.encode()).hexdigest() == expected["descriptionSha256"],
        "mainBindingVerified": isinstance(bindings, dict) and _binding_matches(bindings.get("main"), expected["main"], "image", resources),
        "auxiliaryBindingsVerified": isinstance(auxiliaries, list) and len(auxiliaries) == len(expected["auxiliaries"])
            and all(_binding_matches(binding, resource_id, "image", resources) for binding, resource_id in zip(auxiliaries, expected["auxiliaries"])),
        "voiceBindingVerified": isinstance(bindings, dict) and (_binding_matches(bindings.get("voice"), expected["voice"], "audio", resources)
            if expected["voice"] else bindings.get("voice") in (None, {})),
        "resourcesVerified": all(item["ready"] for item in resource_checks)}
    matched = all(checks.values())
    uncertain = any(code not in ("NODE_NOT_FOUND", "NODE_TYPE_MISMATCH", "NODE_ID_MISMATCH") for code in read_errors)
    outcome = "succeeded" if matched else "unknown" if uncertain else "partial" if node is not None or any(item["ready"] for item in resource_checks) else "rejected"
    return {"operationOutcome": outcome, "bindingsVerified": matched, "persistenceVerified": matched,
        "reconciliationRequired": not matched, "checks": checks, "resourceChecks": resource_checks,
        "readErrors": sorted(set(read_errors)), "nodeId": expected["nodeId"],
        "expectedDescriptionSha256": expected["descriptionSha256"], "voiceExpected": expected["voice"] is not None}


def _inspect_import(payload, job_id, invoke):
    base = {"action": payload["action"], "source": "original_import_inspection",
        "projectId": payload["projectId"], "operationId": payload["operationId"],
        "generationSubmitted": False, "sourceSubjectModified": False, "workspaceMutationAttempted": False,
        "dispatchAttempted": False, "readOnlyUpstream": True, "localJournalUpdated": False,
        "doNotResubmit": True, "recovery": recovery(payload, job_id)}
    try:
        path, record, before_hash = _load_original(payload)
        previous = record.get("reconciliations", [])
        if not isinstance(previous, list):
            raise ImportFailure("SUBJECT_STATE_INVALID", "The original journal has invalid reconciliation metadata")
        if payload["action"] == RECONCILE_ACTION:
            for entry in previous:
                if isinstance(entry, dict) and entry.get("jobId") == job_id and isinstance(entry.get("result"), dict):
                    return {**entry["result"], "replayedReconciliationReceipt": True}
        expected = _expected_import(record)
        snapshot = _inspect_snapshot(payload, job_id, record, expected, invoke)
        timestamp = datetime.now(timezone.utc).isoformat()
        result = {**base, **snapshot, "inspectedAt": timestamp,
            "originalJobId": record["jobId"], "originalPhase": record.get("phase"),
            "originalState": record.get("state"), "originalReceiptPreserved": True,
            "originalReceiptSha256": hashlib.sha256(json.dumps(record.get("result"), sort_keys=True,
                ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
            "upstream": {"ok": snapshot["operationOutcome"] != "unknown", "data": {"inspectionCompleted": True,
                "originalImportVerified": snapshot["bindingsVerified"]}},
            "note": "Only original planned objects were read. This does not submit, repair or repeat the import; the original import receipt remains unchanged."}
        if payload["action"] == RECONCILE_ACTION:
            if hashlib.sha256(path.read_bytes()).hexdigest() != before_hash:
                raise ImportFailure("SUBJECT_JOURNAL_CHANGED", "The original journal changed during inspection; no reconciliation was saved")
            result.update(localJournalUpdated=True, reconciledAt=timestamp)
            record["reconciliations"] = previous + [{"jobId": job_id, "reconciledAt": timestamp,
                "operationOutcome": snapshot["operationOutcome"], "result": result}]
            record.update(reconciledAt=timestamp, reconciliationOutcome=snapshot["operationOutcome"])
            if len(json.dumps(record, ensure_ascii=False).encode()) > MAX_JOURNAL_BYTES:
                raise ImportFailure("SUBJECT_JOURNAL_LIMIT", "The reconciliation journal reached its bounded storage limit")
            _save(path, record)
        return result
    except ImportFailure as exc:
        return {**base, "operationOutcome": "rejected", "reconciliationRequired": True,
            "upstream": {"ok": False, "error": {"code": exc.code, "message": exc.message}}}
    except Exception:
        return {**base, "operationOutcome": "unknown", "reconciliationRequired": True,
            "upstream": {"ok": False, "error": {"code": "SUBJECT_INSPECTION_UNAVAILABLE",
                "message": "Original import inspection could not complete; no upstream write or generation was attempted"}}}


def execute(payload, job_id, *, invoke):
    validate(payload)
    if payload["action"] in INSPECTION_ACTIONS:
        return _inspect_import(payload, job_id, invoke)
    base = {"action": ACTION, "dryRun": payload.get("dryRun", True), "generationSubmitted": False,
        "sourceSubjectModified": False, "workspaceMutationAttempted": False, "recovery": recovery(payload, job_id)}
    if base["dryRun"]:
        return {**base, "source": "local_plan", "dryRunKind": "bridge-local-plan", "upstream": {"ok": True, "data": {"submitted": False}},
            "plan": ["Read exact personal subject", "Select saved voice explicitly unless it is the only one",
                "Register its verified image URIs as new stable resources", "Copy saved MP3 through official managed upload",
                "Create one new element", "Read back main/auxiliary/voice resource identities and success states"],
            "note": "No account, source material, network, mutation or voice quality was checked by this local plan."}
    path = record = None
    try:
        path, record, replay = _begin(payload, job_id)
        if replay:
            return replay
        source = web.execute({"action": "subject.get", "subjectId": payload["subjectId"]}, job_id + ":subject")
        if source.get("upstream", {}).get("ok") is not True:
            raise ImportFailure("SUBJECT_LOOKUP_FAILED", "The exact personal subject could not be read from the current account")
        subject = source["upstream"]["data"]["subject"]
        content = subject.get("content")
        if subject.get("subject_id") != payload["subjectId"] or not isinstance(content, dict):
            raise ImportFailure("SUBJECT_RESPONSE_INVALID", "Live subject identity or content did not match")
        name, description = content.get("name"), content.get("description", "")
        if not isinstance(name, str) or not name or not isinstance(description, str) or len(description) > 20000:
            raise ImportFailure("SUBJECT_RESPONSE_INVALID", "Live subject name or description is invalid")
        images = [_safe_image(content.get("main_image"))]
        auxiliary = content.get("auxiliary_images") or []
        if not isinstance(auxiliary, list) or len(auxiliary) > MAX_AUXILIARIES:
            raise ImportFailure("SUBJECT_IMAGE_LIMIT", "Auxiliary image list exceeds the fixed import limit")
        if payload.get("includeAuxiliary", True):
            images += [_safe_image(info) for info in auxiliary]
        voice_index, voice = _voice_info(content, payload)
        planned_element = {"action": "element.create", "projectId": payload["projectId"], "title": payload.get("title", name),
            "description": description, "main": _identity(job_id, "image-0"), "dryRun": False}
        if len(images) > 1:
            planned_element["auxiliary"] = [_identity(job_id, "image-" + str(i)) for i in range(1, len(images))]
        if voice:
            planned_element["voice"] = official._identities(job_id + ":voice")[0]
        # Reject invalid source titles/descriptions before registering any media.
        official.validate(planned_element)
        voice_bytes = _download_voice(voice) if voice else None
        source_meta = {"subjectId": payload["subjectId"], "dataId": subject.get("data_id"), "updatedAt": subject.get("update_time"),
            "name": name, "descriptionSha256": hashlib.sha256(description.encode()).hexdigest(), "imageUris": [i["uri"] for i in images],
            "voiceIndex": voice_index, "voiceVid": voice.get("vid") if voice else None,
            "voiceDurationMs": voice.get("duration_ms") if voice else None}
        record.update(sourceSubject=source_meta, phase="image-registration",
            plannedImages=[{"resourceId": _identity(job_id, "image-" + str(i)), **info} for i, info in enumerate(images)])
        _save(path, record)
        session = web._load_session()
        base["workspaceMutationAttempted"] = True
        for index, info in enumerate(images):
            resource_id = _identity(job_id, "image-" + str(index))
            entry = _upload_image(payload["projectId"], resource_id, info, name if index == 0 else None, session)
            record["createdResources"].append(entry)
            _save(path, record)
            resource = _official({"action": "resource.read", "projectId": payload["projectId"], "resourceId": resource_id}, job_id + ":image-read:" + str(index), invoke)["upstream"]["data"]
            if resource.get("resourceId") != resource_id or resource.get("type") != "image" or resource.get("status") != "success":
                raise ImportFailure("SUBJECT_IMAGE_NOT_READY", "Registered image is not a verified usable image resource")
        voice_resource = None
        if voice_bytes:
            record["phase"] = "voice-upload"
            _save(path, record)
            result = _official({"action": "resource.upload", "projectId": payload["projectId"], "mediaType": "audio", "fileExtension": "mp3",
                "name": name + "-保存音色", "sha256": hashlib.sha256(voice_bytes).hexdigest(),
                "contentBase64": base64.b64encode(voice_bytes).decode(), "dryRun": False}, job_id + ":voice", invoke)
            if result.get("resourceReady") is not True or result.get("resourceVerified") is not True:
                raise ImportFailure("SUBJECT_VOICE_NOT_READY", "Saved voice copy has not become a verified usable audio resource")
            voice_resource = result["resourceId"]
            if voice_resource != planned_element["voice"]:
                raise ImportFailure("SUBJECT_VOICE_ID_MISMATCH", "Voice receipt did not match its planned recovery identity")
            record["createdResources"].append({"resourceId": voice_resource, "sourceVid": voice["vid"], "type": "audio",
                "sha256": hashlib.sha256(voice_bytes).hexdigest(), "byteSize": len(voice_bytes)})
            _save(path, record)
        element_payload = planned_element
        record["phase"] = "element-save"
        record["elementRecovery"] = official.recovery(element_payload, job_id + ":element")
        _save(path, record)
        saved = _official(element_payload, job_id + ":element", invoke)
        node_id = record["elementRecovery"]["nodeId"]
        record["phase"] = "element-readback"
        _save(path, record)
        read = _official({"action": "node.read", "projectId": payload["projectId"], "nodeId": node_id}, job_id + ":element-read", invoke)
        try:
            node = parse_node_show(read["upstream"], node_id, expected_type="element")
        except UpstreamContractError:
            raise ImportFailure("SUBJECT_BINDING_READBACK_MISMATCH", "The exact saved element was not returned as a unique valid FOUND node") from None
        bindings = node.get("subjectBindings") or {}
        resources = node.get("resources")
        aux_bindings = bindings.get("auxiliaries") or []
        if (node.get("nodeId") != node_id or node.get("type") != "element" or node.get("description", "") != description
                or not _binding_matches(bindings.get("main"), element_payload["main"], "image", resources)
                or not isinstance(aux_bindings, list) or len(aux_bindings) != len(images) - 1
                or any(not _binding_matches(binding, _identity(job_id, "image-" + str(i)), "image", resources) for i, binding in enumerate(aux_bindings, 1))
                or voice_resource and not _binding_matches(bindings.get("voice"), voice_resource, "audio", resources)):
            raise ImportFailure("SUBJECT_BINDING_READBACK_MISMATCH", "Element was saved but exact main/auxiliary/voice bindings were not all verified")
        result = {**base, "source": "live_subject_to_canvas", "sourceSubject": source_meta,
            "upstream": {"ok": True, "data": {"projectId": payload["projectId"], "nodeId": node_id}},
            "node": node, "createdResources": record["createdResources"], "bindingsVerified": True,
            "voiceBound": voice_resource is not None, "voiceQualityVerified": False,
            "completedAt": datetime.now(timezone.utc).isoformat(),
            "note": "Frozen asset copies were bound to one new canvas element. Original personal subject was not edited; no media generation or cross-shot identity test was performed."}
        record.update(state="complete", phase="complete", result=result)
        _save(path, record)
        return result
    except (ImportFailure, web.SessionError) as exc:
        result = {**base, "upstream": {"ok": False, "error": {"code": exc.code, "message": exc.message}}}
    except Exception:
        result = {**base, "upstream": {"ok": False, "error": {"code": "SUBJECT_IMPORT_UNCERTAIN",
            "message": "Import outcome is uncertain; inspect the original job and planned resources before any retry"}}}
    if record is not None and path is not None:
        result.update(phase=record["phase"], createdResources=record["createdResources"], sourceSubject=record.get("sourceSubject"),
                      plannedImages=record.get("plannedImages", []))
        record.update(state="needs-inspection", result=result)
        _save(path, record)
    return result
