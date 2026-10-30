"""Bounded edits of the authenticated account's personal subjects.

Official loaded modules KgO/uMO/xaV/dXX (2026-10-30) define the create and
update endpoints. Their update request is {subject_id, content}, with no
conditional data_id. We perform an explicit version preflight and serialize
our own edits, but do not claim an upstream compare-and-swap guarantee.
Only existing owned image/voice material may be copied; no upload, generation,
media deletion, or caller-supplied URL/body is exposed. Permanent subject deletion
is a separate explicitly confirmed action; it is not archival or recovery.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

import web_actions as web

CREATE = "personal.subject.create"
UPDATE = "personal.subject.update"
DELETE = "personal.subject.delete"
VOICE_CREATE = "personal.voice.create"
VOICE_UPDATE = "personal.voice.update"
VOICE_ARCHIVE = "personal.voice.archive"
ACTIONS = (CREATE, UPDATE, DELETE, VOICE_CREATE, VOICE_UPDATE, VOICE_ARCHIVE)
BASE_PATH = "/mweb/v1/dreamina_subject/"
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_VOICES = 20  # Conservative bridge bound, not an upstream quota claim.
ID = web._str(64, pattern=r"^[0-9]+$")
NAME = web._str(20, allowText=True)
DESCRIPTION = web._str(4000, minLength=0, allowText=True)
SOURCE_FIELDS = {"sourceSubjectId": ID, "expectedSourceDataId": ID,
                 "sourceVoiceIndex": web._int(0, MAX_VOICES - 1)}
TARGET_FIELDS = {"subjectId": ID, "expectedDataId": ID}
CONTENT_KEYS = frozenset({"name", "description", "main_image", "auxiliary_images",
                          "video_info", "audio_info", "voice_list", "selected_voice"})


def supports(action):
    return action in ACTIONS


def is_mutating(action):
    return supports(action)


def request_schemas():
    fields = {
        CREATE: {"sourceSubjectId": ID, "expectedSourceDataId": ID,
                 "name": NAME, "description": DESCRIPTION,
                 "includeAuxiliary": {"type": "boolean", "default": True},
                 "includeVoices": {"type": "boolean", "default": False}},
        UPDATE: {**TARGET_FIELDS, "name": NAME, "description": DESCRIPTION},
        DELETE: {**TARGET_FIELDS, "confirmPermanentDelete": {"type": "boolean", "default": False,
                    "description": "Caller attests that a human explicitly authorized permanent deletion of this exact subject. Setting true does not obtain or substitute for that authorization."}},
        VOICE_CREATE: {**TARGET_FIELDS, **SOURCE_FIELDS, "select": {"type": "boolean", "default": False}},
        VOICE_UPDATE: {**TARGET_FIELDS, **SOURCE_FIELDS, "voiceIndex": web._int(0, MAX_VOICES - 1),
                       "select": {"type": "boolean", "default": False}},
        VOICE_ARCHIVE: {**TARGET_FIELDS, "voiceIndex": web._int(0, MAX_VOICES - 1)},
    }
    required = {
        CREATE: ["sourceSubjectId", "expectedSourceDataId", "name"],
        UPDATE: ["subjectId", "expectedDataId"],
        DELETE: ["subjectId", "expectedDataId"],
        VOICE_CREATE: [*TARGET_FIELDS, *SOURCE_FIELDS],
        VOICE_UPDATE: [*TARGET_FIELDS, *SOURCE_FIELDS, "voiceIndex"],
        VOICE_ARCHIVE: [*TARGET_FIELDS, "voiceIndex"],
    }
    descriptions = {
        CREATE: "Create one new personal image subject by copying existing same-account material. Name required. Source retained. Voices excluded unless includeVoices=true.",
        UPDATE: "Update only name and/or description; preserve all existing material bindings. Requires the current data_id.",
        DELETE: "Permanently delete exactly one personal subject. Not archive; no restoration contract established. Live execution additionally requires prior human authorization for this exact object, confirmPermanentDelete=true as the caller's attestation, and current deletable=true. A preserved local before-state is not proof of recoverability.",
        VOICE_CREATE: "Append one saved same-account voice binding. Does not create or generate audio. Rejects duplicate vids.",
        VOICE_UPDATE: "Replace one saved voice binding from another current owned subject. Selection follows a replaced selected voice; select=true explicitly selects the replacement.",
        VOICE_ARCHIVE: "Detach one voice binding, preserving media and the private before-state journal. This is not remote media archive/deletion. No automatic restore is claimed.",
    }
    return {action: {"type": "object", "additionalProperties": False,
            "properties": {"action": {"type": "string", "const": action}, **deepcopy(props),
                           "dryRun": {"type": "boolean", "default": True}},
            "required": ["action", *required[action]],
            "description": descriptions[action] + " Default dryRun=true. Version check is preflight only; the platform provides no atomic conditional update here."}
            for action, props in fields.items()}


def validate(payload):
    if not isinstance(payload, dict) or not supports(payload.get("action")):
        raise ValueError("Unsupported personal subject operation")
    spec = request_schemas()[payload["action"]]
    web._check(payload, spec, "personal operation")
    if payload["action"] == UPDATE and not ({"name", "description"} & payload.keys()):
        raise ValueError("Subject update requires name or description")
    if "name" in payload and (not payload["name"].strip() or len(payload["name"].encode("utf-16-le")) // 2 > 20):
        raise ValueError("Subject name must contain text and be at most 20 UTF-16 units")
    if (payload["action"] == DELETE and not payload.get("dryRun", True)
            and payload.get("confirmPermanentDelete") is not True):
        raise ValueError("Live subject deletion requires explicit confirmPermanentDelete=true")
    if not payload.get("dryRun", True) and web.read_only():
        raise ValueError("Service is read-only; live personal edits are disabled")


def _identity(job_id):
    if not isinstance(job_id, str) or not job_id or len(job_id) > 180 or any(ord(c) < 32 for c in job_id):
        raise ValueError("A stable bounded job identity is required")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:personal:" + job_id))


def recovery(payload, job_id):
    if payload.get("dryRun", True):
        return None
    result = {"operationId": _identity(job_id), "doNotResubmit": True,
              "upstreamIdempotencyVerified": False, "upstreamConditionalVersioning": False,
              "originalStatePreservedInPrivateJournal": True,
              "pendingIntentId": _pending_key(payload),
              "pendingResolution": "Read the original journal and live subject/list first. An operator may resolve a pending guard only after verifying the actual outcome; a new job ID is not a retry authorization.",
              "note": "Inspect the original receipt and current subject. Do not create a new job after an uncertain write. No automatic retry, rollback or recovery guarantee is provided."}
    if payload["action"] == CREATE:
        result["query"] = {"action": "subject.list", "limit": 100}
        result["nameHint"] = payload["name"]
    else:
        result["subjectId"] = payload["subjectId"]
        result["query"] = {"action": "subject.get", "subjectId": payload["subjectId"]}
    return result


def capabilities():
    try:
        web._load_session()
        configured = True
    except web.SessionError:
        configured = False
    try:
        import acceptance_registry
        records = {action: acceptance_registry.verified(action) for action in ACTIONS}
    except (ImportError, OSError, ValueError, TypeError):
        records = {}
    return {"actions": {action: {
        "implemented": True, "available": configured,
        "liveExecutionAllowed": configured and not web.read_only(), "defaultDryRun": True,
        "mutatesWorkspace": True, "billable": False, "generationSubmitted": False,
        "requiresExistingOwnedMaterial": action not in (UPDATE, DELETE),
        "destructive": action == DELETE, "requiresPriorHumanAuthorization": action == DELETE,
        "restoreSupported": False if action == DELETE else None,
        "crossJobUncertainWriteGuard": True,
        "upstreamConditionalVersioning": False, "upstreamIdempotencyVerified": False,
        "serverExecutionVerified": bool(records.get(action)), "executionVerification": records.get(action, []),
        "scope": request_schemas()[action]["description"],
    } for action in ACTIONS}, "unimplemented": {
        "personal.subject.archive": "Only permanent personal.subject.delete is implemented separately. Reversible archival and restoration remain unsupported.",
        "personal.voice.generate": "Official generate_voice endpoint identified but not invoked; pricing and generation acceptance remain separate.",
    }}


class PersonalFailure(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _root():
    parent = os.environ.get("DREAMINA_CANVAS_STATE_DIR")
    if not parent or not Path(parent).is_absolute():
        raise PersonalFailure("PERSONAL_STATE_NOT_CONFIGURED", "Configure a private canvas state directory")
    path = Path(parent) / "personal-edits"
    if path.is_symlink():
        raise PersonalFailure("PERSONAL_STATE_INVALID", "Personal journal cannot be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise PersonalFailure("PERSONAL_STATE_INVALID", "Personal journal must be a private owned directory")
    return path


def _save(path, record):
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_JOURNAL_BYTES:
        raise PersonalFailure("PERSONAL_STATE_LIMIT", "Personal journal exceeds the private size bound")
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def _begin(payload, job_id):
    path = _root() / (_identity(job_id) + ".json")
    record = {"operationId": _identity(job_id), "jobId": job_id, "payload": deepcopy(payload),
              "state": "started", "phase": "preflight", "startedAt": datetime.now(timezone.utc).isoformat()}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.geteuid() or info.st_size > MAX_JOURNAL_BYTES):
                    raise ValueError("invalid journal")
                previous = json.load(source)
        except (OSError, ValueError, UnicodeError):
            raise PersonalFailure("PERSONAL_STATE_INVALID", "Original personal journal cannot be read safely") from None
        if not isinstance(previous, dict):
            raise PersonalFailure("PERSONAL_STATE_INVALID", "Original journal does not contain a record")
        if previous.get("payload") != payload:
            raise PersonalFailure("PERSONAL_IDENTITY_CONFLICT", "This job identity already has a different personal edit")
        if previous.get("state") == "complete" and isinstance(previous.get("result"), dict):
            return path, previous, {**previous["result"], "replayedOriginalReceipt": True}
        raise PersonalFailure("PERSONAL_RECONCILIATION_REQUIRED", "This job already started. Inspect its original receipt and current subject before any new edit")
    with os.fdopen(fd, "w") as out:
        json.dump(record, out, ensure_ascii=False)
        out.flush()
        os.fsync(out.fileno())
    return path, record, None


def _pending_key(payload):
    if payload["action"] != CREATE:
        return "target-" + payload["subjectId"]
    # Conservatively group all unresolved creations of this source/name.
    # Omitted description inherits live source text; an explicit identical
    # description must not bypass the guard. Copy options and expected source
    # versions likewise cannot authorize a retry of this unresolved subject.
    intent = {"action": CREATE, "sourceSubjectId": payload["sourceSubjectId"],
              "name": payload["name"].strip()}
    return "create-" + hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _assert_no_pending(payload):
    path = _root() / ("pending-" + _pending_key(payload) + ".json")
    # lexists also catches a dangling symlink; do not follow or overwrite it.
    if os.path.lexists(path):
        raise PersonalFailure("PERSONAL_PENDING_WRITE", "An earlier write for this target or creation intent is unresolved. Inspect its private journal and live state; changing job identity does not permit resubmission")
    return path


def _claim_pending(payload, operation_id):
    path = _assert_no_pending(payload)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        raise PersonalFailure("PERSONAL_PENDING_WRITE", "This target or creation intent already has an unresolved write; do not resubmit") from None
    created = os.fstat(fd)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump({"operationId": operation_id, "action": payload["action"],
                       "pendingIntentId": _pending_key(payload), "createdAt": datetime.now(timezone.utc).isoformat()}, out)
            out.flush()
            os.fsync(out.fileno())
    except Exception:
        # No request can have started before this function returns. Remove
        # only the exact inode this O_EXCL invocation created, never another
        # operation's guard after a concurrent pathname replacement.
        try:
            current = path.lstat()
            if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                path.unlink()
        except OSError:
            pass
        raise
    return path


def _release_pending(path, operation_id):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_size > 4096):
            raise PersonalFailure("PERSONAL_STATE_INVALID", "Pending write guard cannot be read safely")
        record = json.load(source)
    if not isinstance(record, dict) or record.get("operationId") != operation_id:
        raise PersonalFailure("PERSONAL_STATE_INVALID", "Pending guard belongs to a different operation")
    path.unlink()


@contextmanager
def _subject_lock(subject_id):
    path = _root() / ("subject-" + subject_id + ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()):
            raise PersonalFailure("PERSONAL_STATE_INVALID", "Personal lock must be an owned regular private file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PersonalFailure("PERSONAL_TARGET_BUSY", "Another local personal edit is active for this subject") from None
        yield
    finally:
        os.close(fd)


def _read(subject_id, session, base):
    result = web._subject_lookup({"subjectId": subject_id}, base, session)
    if result.get("upstream", {}).get("ok") is not True:
        raise PersonalFailure("PERSONAL_READ_FAILED", "Live owned-subject lookup failed; no cached data was substituted")
    subject = result["upstream"]["data"].get("subject")
    if (not isinstance(subject, dict) or subject.get("subject_id") != subject_id
            or not isinstance(subject.get("data_id"), str) or not re.fullmatch(r"[0-9]+", subject["data_id"])
            or not isinstance(subject.get("content"), dict)):
        raise PersonalFailure("PERSONAL_READ_CONTRACT", "Live subject identity, data version or content is invalid")
    return subject


def _check_version(subject, expected):
    if subject["data_id"] != expected:
        raise PersonalFailure("PERSONAL_VERSION_CONFLICT", "Current data_id differs from the expected version; read the subject before deciding a new edit")


def _content(subject):
    content = deepcopy(subject["content"])
    # Current list responses may add an asynchronous image caption. The UI's
    # dXX._buildSubjectContent explicitly excludes it; it is not an editable
    # body field. Keep it in beforeSubject but never invent a caption write.
    content.pop("caption", None)
    if set(content) - CONTENT_KEYS:
        raise PersonalFailure("PERSONAL_CONTENT_CONTRACT", "Subject content has unknown fields; refuse a full-content replacement")
    if not isinstance(content.get("name"), str) or not isinstance(content.get("main_image"), dict):
        raise PersonalFailure("PERSONAL_IMAGE_SUBJECT_REQUIRED", "This bounded adapter supports existing image subjects only")
    if content.get("video_info") or content.get("audio_info"):
        raise PersonalFailure("PERSONAL_IMAGE_SUBJECT_REQUIRED", "Mixed video/audio subjects are outside this adapter")
    image = content["main_image"]
    if (not isinstance(image.get("image_uri"), str) or not image["image_uri"]
            or type(image.get("width")) is not int or type(image.get("height")) is not int):
        raise PersonalFailure("PERSONAL_IMAGE_CONTRACT", "Existing subject main image is incomplete")
    voices = content.get("voice_list") or []
    if not isinstance(voices, list) or len(voices) > MAX_VOICES:
        raise PersonalFailure("PERSONAL_VOICE_CONTRACT", "Subject voice list is outside the supported bound")
    return content


def _voice_vid(voice):
    if not isinstance(voice, dict) or not isinstance(voice.get("audio_info"), dict):
        raise PersonalFailure("PERSONAL_VOICE_CONTRACT", "Saved voice must have an audio_info object")
    vid = voice["audio_info"].get("vid")
    if not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", vid):
        raise PersonalFailure("PERSONAL_VOICE_CONTRACT", "Saved voice has no bounded stable vid")
    if voice.get("source") not in ("generated", "upload"):
        raise PersonalFailure("PERSONAL_VOICE_CONTRACT", "Saved voice source is outside the observed contract")
    return vid


def _wire_content(content):
    # uMO.b converts only the nested AIGC image/video item identity to a string
    # wire field before recursive camel-to-snake conversion. Preserve all other
    # source metadata, including newly refreshed platform URLs.
    content = deepcopy(content)
    images = [content.get("main_image"), *(content.get("auxiliary_images") or [])]
    for image in images:
        if not isinstance(image, dict):
            raise PersonalFailure("PERSONAL_IMAGE_CONTRACT", "Saved image list contains an unsupported item")
        if image.get("aigc_image"):
            item = image["aigc_image"]
            item_id = item.get("item_id_str", item.get("item_id")) if isinstance(item, dict) else None
            if not isinstance(item_id, (str, int)) or isinstance(item_id, bool) or not re.fullmatch(r"[0-9]+", str(item_id)):
                raise PersonalFailure("PERSONAL_IMAGE_CONTRACT", "Saved image AIGC identity is invalid")
            image["aigc_image"] = {"item_id_str": str(item_id)}
    return content


def _voice_wire(voice):
    # Official Iq4Q.g -> Iq4Q.M conversion used by dXX._buildSubjectContent.
    # Material URLs originate in this account's authenticated subject record.
    vid = _voice_vid(voice)
    info = voice["audio_info"]
    url = info.get("audio_url") or info.get("url")
    if not isinstance(url, str) or not url.startswith("https://") or len(url) > 16000:
        raise PersonalFailure("PERSONAL_VOICE_CONTRACT", "Saved voice has no supported HTTPS material URL")
    audio = {"vid": vid, "audio_url": url, "url": url}
    for key in ("duration", "title"):
        if info.get(key) is not None:
            audio[key] = deepcopy(info[key])
    return {"audio_info": audio, "source": voice["source"]}


def _material_identity(image):
    if not isinstance(image, dict):
        raise PersonalFailure("PERSONAL_IMAGE_CONTRACT", "Saved image identity is missing")
    identity = {key: image.get(key) for key in ("image_uri", "width", "height")}
    # The JSON number can be rounded by JS; the official string wins even when
    # both fields occur in the receipt in an unexpected insertion order.
    aigc = image.get("aigc_image")
    if isinstance(aigc, dict):
        item_id = aigc.get("item_id_str", aigc.get("item_id"))
        if item_id is not None:
            identity["aigc_item_id"] = str(item_id)
    return identity


def _fingerprint(content):
    # Platform response adds format/default/duration/transcode fields and
    # refreshes signed URLs. Verify text, ordered image identities/dimensions,
    # ordered voice identity/source/title and explicit selected voice.
    def voice_identity(voice):
        return {"vid": _voice_vid(voice), "source": voice["source"],
                "title": voice["audio_info"].get("title") or ""}
    proof = {"name": content.get("name"), "description": content.get("description") or "",
             "main_image": _material_identity(content.get("main_image")),
             "auxiliary_images": [_material_identity(x) for x in content.get("auxiliary_images") or []],
             "voice_list": [voice_identity(x) for x in content.get("voice_list") or []],
             "selected_voice": voice_identity(content["selected_voice"]) if content.get("selected_voice") else None}
    return hashlib.sha256(json.dumps(proof, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _build(payload, target, source):
    action = payload["action"]
    content = _content(source if action == CREATE else target)
    if action == CREATE:
        content["name"] = payload["name"]
        if "description" in payload:
            content["description"] = payload["description"]
        if not payload.get("includeAuxiliary", True):
            content.pop("auxiliary_images", None)
        if not payload.get("includeVoices", False):
            content.pop("voice_list", None)
            content.pop("selected_voice", None)
    elif action == UPDATE:
        for key in ("name", "description"):
            if key in payload:
                content[key] = payload[key]
    else:
        voices = content.get("voice_list") or []
        for voice in voices:
            _voice_vid(voice)
        if action != VOICE_ARCHIVE:
            source_voices = _content(source).get("voice_list") or []
            if payload["sourceVoiceIndex"] >= len(source_voices):
                raise PersonalFailure("PERSONAL_VOICE_NOT_FOUND", "Source voice index is not present in the expected version")
            selected = _voice_wire(source_voices[payload["sourceVoiceIndex"]])
            selected_vid = _voice_vid(selected)
        if action == VOICE_CREATE:
            if len(voices) >= MAX_VOICES:
                raise PersonalFailure("PERSONAL_VOICE_LIMIT", "Subject already reaches the bridge voice bound")
            if any(_voice_vid(v) == selected_vid for v in voices):
                raise PersonalFailure("PERSONAL_VOICE_DUPLICATE", "That saved voice is already bound to this subject")
            voices.append(selected)
            if payload.get("select", False):
                content["selected_voice"] = selected
        else:
            index = payload["voiceIndex"]
            if index >= len(voices):
                raise PersonalFailure("PERSONAL_VOICE_NOT_FOUND", "Target voice index is not present in the expected version")
            old_vid = _voice_vid(voices[index])
            explicit = content.get("selected_voice")
            was_selected = explicit is not None and _voice_vid(explicit) == old_vid
            if action == VOICE_ARCHIVE:
                voices.pop(index)
                if was_selected:
                    content.pop("selected_voice", None)
            else:
                if any(i != index and _voice_vid(v) == selected_vid for i, v in enumerate(voices)):
                    raise PersonalFailure("PERSONAL_VOICE_DUPLICATE", "Replacement would duplicate another bound voice")
                voices[index] = selected
                if was_selected or payload.get("select", False):
                    content["selected_voice"] = selected
        content["voice_list"] = voices
    body = {"content": _wire_content(content)}
    if action != CREATE:
        body["subject_id"] = payload["subjectId"]
    if len(json.dumps(body).encode()) > web.MAX_REQUEST_BYTES:
        raise PersonalFailure("PERSONAL_REQUEST_LIMIT", "The complete subject content exceeds the request bound")
    return body


def _send(path, body, session, *, deletion=False):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": session["cookie"], "User-Agent": session["userAgent"], **session["headers"]}
    try:
        status, raw = web._send(path, body, headers)
    except Exception:
        raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Write transport outcome is unknown. Inspect the original journal and target before any new edit") from None
    if len(raw) > web.MAX_RESPONSE_BYTES:
        raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Write response exceeded the bound; inspect the original target")
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Write did not return a valid JSON receipt; inspect the original target") from None
    if not isinstance(envelope, dict) or type(envelope.get("ret")) not in (str, int):
        raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Write receipt envelope changed; inspect the original target")
    if not 200 <= status < 300 or str(envelope["ret"]) != "0":
        raise PersonalFailure("PERSONAL_WRITE_REJECTED", "Platform did not acknowledge the personal edit; raw error content is withheld. Inspect original job before retrying")
    data = envelope.get("data")
    if deletion:
        if data is not None and not isinstance(data, dict):
            raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Delete receipt data changed from the observed optional-object contract")
        return {"ret": "0", "acknowledged": True}
    if (not isinstance(data, dict) or not isinstance(data.get("subject_id"), str)
            or not re.fullmatch(r"[0-9]+", data["subject_id"])
            or not isinstance(data.get("data_id"), str) or not isinstance(data.get("content"), dict)):
        raise PersonalFailure("PERSONAL_WRITE_UNCERTAIN", "Successful response lacks the observed subject identity/version/content receipt")
    return web._redact(data, web._private_values(session))


def execute(payload, job_id):
    validate(payload)
    action = payload["action"]
    endpoint = BASE_PATH + ("create" if action == CREATE else "delete" if action == DELETE else "update")
    base = {"action": action, "source": "live_web", "cacheUsed": False, "readOnly": False,
            "generationSubmitted": False, "workspaceMutationAttempted": False,
            "upstreamConditionalVersioning": False, "upstreamIdempotencyVerified": False,
            "requestedAt": datetime.now(timezone.utc).isoformat()}
    if payload.get("dryRun", True):
        return {**base, "source": "local_plan", "dryRun": True, "dryRunKind": "bridge-local-plan",
                "requestPlan": {"method": "POST", "path": endpoint,
                                **({"body": {"subject_id": payload["subjectId"]}, "requiresPermanentDeleteConfirmation": True} if action == DELETE else {"bodyBuiltFromLiveOwnedSubject": True})},
                "validationScope": "Fixed input schema only; live source, version, permissions, full-content body and persistence are checked only during execution",
                "upstream": {"ok": True, "data": {"submitted": False}}}
    base["recovery"] = recovery(payload, job_id)
    path = record = pending = None
    try:
        path, record, previous = _begin(payload, job_id)
        if previous:
            return previous
        session = web._load_session()
        target_id = payload.get("subjectId", payload.get("sourceSubjectId"))
        with _subject_lock(target_id):
            _assert_no_pending(payload)
            target = None
            if action != CREATE:
                target = _read(payload["subjectId"], session, base)
                _check_version(target, payload["expectedDataId"])
                permission = "deletable" if action == DELETE else "editable"
                if target.get("subject_control", {}).get(permission) is not True:
                    raise PersonalFailure("PERSONAL_NOT_DELETABLE" if action == DELETE else "PERSONAL_NOT_EDITABLE",
                                          "Current subject does not explicitly permit this operation")
            source = None
            if "sourceSubjectId" in payload:
                source = target if target and target["subject_id"] == payload["sourceSubjectId"] else _read(payload["sourceSubjectId"], session, base)
                _check_version(source, payload["expectedSourceDataId"])
            body = {"subject_id": payload["subjectId"]} if action == DELETE else _build(payload, target, source)
            record.update(phase="prepared", beforeSubject=target, sourceIdentity={"subjectId": source["subject_id"], "dataId": source["data_id"]} if source else None, requestBody=body)
            _save(path, record)
            pending = _claim_pending(payload, record["operationId"])
            record["phase"] = "write_attempted"
            _save(path, record)
            base["workspaceMutationAttempted"] = True
            receipt = _send(endpoint, body, session, deletion=action == DELETE)
            record.update(phase="write_acknowledged", receipt=receipt)
            _save(path, record)
            if action == DELETE:
                absence = web._subject_lookup({"subjectId": payload["subjectId"]}, base, session)
                error = absence.get("upstream", {}).get("error", {})
                if error.get("code") != "WEB_SUBJECT_NOT_FOUND":
                    raise PersonalFailure("PERSONAL_DELETE_READBACK_UNCERTAIN",
                                          "Delete was acknowledged but absence was not verified by a completed owned-subject listing; do not retry")
                result = {**base, "persistenceVerified": True, "deletionVerified": True,
                          "deletedSubjectId": payload["subjectId"], "beforeDataId": target["data_id"],
                          "operationId": record["operationId"], "restoreSupported": False,
                          "verification": "Exact subject absent after completed current-account paginated list",
                          "upstream": {"ok": True, "data": {"deletedSubjectId": payload["subjectId"]}}}
                record.update(state="complete", phase="delete_absence_verified", result=result,
                              completedAt=datetime.now(timezone.utc).isoformat())
                _save(path, record)
                _release_pending(pending, record["operationId"])
                pending = None
                return result
            expected_id = receipt["subject_id"] if action == CREATE else payload["subjectId"]
            if receipt["subject_id"] != expected_id or (action == CREATE and source and expected_id == source["subject_id"]):
                raise PersonalFailure("PERSONAL_RECEIPT_MISMATCH", "Write receipt did not identify the expected distinct target")
            if _fingerprint(receipt["content"]) != _fingerprint(body["content"]):
                raise PersonalFailure("PERSONAL_RECEIPT_MISMATCH", "Write receipt content differs from the planned complete subject content")
            current = _read(expected_id, session, base)
            if current["data_id"] != receipt["data_id"] or _fingerprint(current["content"]) != _fingerprint(body["content"]):
                raise PersonalFailure("PERSONAL_READBACK_MISMATCH", "Write was acknowledged but current version/content did not match; inspect before any further edit")
            result = {**base, "persistenceVerified": True, "subjectId": expected_id, "dataId": current["data_id"],
                      "operationId": record["operationId"], "beforeDataId": target["data_id"] if target else None,
                      "contentSha256": _fingerprint(current["content"]),
                      "contentProofScope": "text, ordered image identity/dimensions, ordered voice identity/source/title, selected voice; excludes refreshed URLs and derived metadata",
                      "sourceSubjectModified": False if action == CREATE else None,
                      "mediaDeleted": False, "voiceOperationScope": "binding_only" if action.startswith("personal.voice.") else None,
                      "upstream": {"ok": True, "data": {"subject": current}}}
            record.update(state="complete", phase="readback_verified", result=result, completedAt=datetime.now(timezone.utc).isoformat())
            _save(path, record)
            _release_pending(pending, record["operationId"])
            pending = None
            return result
    except web.SessionError as exc:
        result = web._failure(base, exc.code, exc.message)
    except PersonalFailure as exc:
        result = web._failure(base, exc.code, exc.message)
    except (OSError, ValueError, TypeError, KeyError):
        result = web._failure(base, "PERSONAL_INTERNAL_UNCERTAIN" if base["workspaceMutationAttempted"] else "PERSONAL_PREFLIGHT_FAILED",
                              "Personal edit did not complete. Inspect the original journal; no automatic retry was performed")
    if pending is not None and not base["workspaceMutationAttempted"]:
        # We know transport was never invoked. A preflight/storage failure
        # must not leave a false uncertain-write guard behind.
        try:
            _release_pending(pending, record["operationId"])
        except (OSError, ValueError, PersonalFailure):
            pass
    if record is not None and path is not None:
        record.update(state="attention_required", result=result)
        try:
            _save(path, record)
        except (OSError, PersonalFailure):
            pass
    return result
