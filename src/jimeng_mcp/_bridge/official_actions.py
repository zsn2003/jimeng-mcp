"""Fixed official CLI operations, based on dreamina-canvas public 1.2.0 schemas.

Integration (no subprocess or shell is implemented in this module):
    if official_actions.supports(payload.get("action")):
        official_actions.validate(payload)
        return official_actions.execute(payload, job_id, invoke=existing_invoke,
                                        invoke_sensitive=private_confirm_invoke)

`invoke` receives only a fixed command plus validated argv, and must return the
official {"ok": bool, ...} JSON envelope. Exceptions remain uncertain outcomes.
All writes default to dryRun=True. JIMENG_READ_ONLY defaults to 1; live writes
also require operator opt-in. Numeric credit policies retain their ceiling.
JIMENG_MAX_CREDITS=unlimited is an explicit operator policy: omitted node-run
ceilings use the authoritative exact quote, while scoped post-processing uses
one explicitly authorized --yes call. Explicit request ceilings always apply.

Create/update operations ONLY save. Running existing media uses node show,
quote, private confirm, then node run with an ephemeral ceiling-scoped token.
Ordinary node run has no --credit-ceiling or --dry-run flag. Its dry run is a
bridge-local plan. Tokens are neither accepted in requests nor persisted or
returned in results. invoke_sensitive is used ONLY for node confirm.

The schema, not a successful media run, was verified. Director mutations use
private managed files and a typed catalog subset. Browser-dependent actions
remain blocked; no external browser is launched.
Video and MP3 download use one fixed authenticated canvas-resource read and a separate,
credential-free HTTPS fetch from an observed CDN host, with no redirects.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import base64
import json
import math
import os
from pathlib import Path
import re
import hashlib
import stat
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid


CONTRACT_VERSION = "official-canvas-1.2.0-fixed-actions-v5"
NODE_PATTERN = r"^node_[0123456789abcdefghjkmnpqrstvwxyz]{10}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
SOURCE_PATTERN = r"(?:node_[0123456789abcdefghjkmnpqrstvwxyz]{10}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
TYPES = ("image", "video", "audio", "element", "text", "timeline")
TAGS = tuple(f"default-tag-{n}" for n in range(1, 6))


def _string(maximum=512, minimum=1, **extra):
    return {"type": "string", "minLength": minimum, "maxLength": maximum, **extra}


def _integer(minimum=0, maximum=2147483647, **extra):
    return {"type": "integer", "minimum": minimum, "maximum": maximum, **extra}


def _number(minimum=None, maximum=None, positive=False):
    result = {"type": "number"}
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    if positive:
        result["exclusiveMinimum"] = 0
    return result


def _enum(*values, **extra):
    return {"type": "string", "enum": list(values), **extra}


def _array(item, maximum=32, minimum=1, unique=False):
    return {"type": "array", "items": item, "minItems": minimum,
            "maxItems": maximum, "uniqueItems": unique}


def _object(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


UUID_FIELD = _string(36, 36, pattern=UUID_PATTERN)
NODE_FIELD = _string(15, 15, pattern=NODE_PATTERN)
SOURCE_FIELD = _string(36, 15, pattern="^" + SOURCE_PATTERN + "$")
REF_FIELD = _string(40, 20, pattern=r"^(?:node:node_[0123456789abcdefghjkmnpqrstvwxyz]{10}|res:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
BOOL_FIELD = {"type": "boolean"}
DRY_RUN = {"type": "boolean", "default": True,
           "description": "Default true. A false value is a live write, even if it costs zero credits."}
CREDIT_CEILING = {"type": "integer", "minimum": 0, "default": 0,
    "description": "Optional explicit approved ceiling. In unlimited operator mode omission means authoritative exact-cost confirmation for node runs, or the explicitly authorized global approval for post-processing. Explicit ceilings are always enforced."}
PROJECT = {"projectId": UUID_FIELD}
SHA256_FIELD = _string(64, 64, pattern=r"^[0-9a-f]{64}$")
UPLOAD_CHUNK_BYTES = 512 * 1024
UPLOAD_MAX_BYTES = 64 * 1024 * 1024
# Both exact hosts were returned for the same authorized resource by the live
# server batch_get response. No suffix/wildcard matching is permitted.
VIDEO_DOWNLOAD_HOSTS = frozenset({"v3-dreamina-de.jianying.com", "v26-dreamina-de.jianying.com"})
AUDIO_DOWNLOAD_HOSTS = frozenset({"v26-dreamina-de.jianying.com"})
VIDEO_RESOURCE_PATH = "/octo_api/v1/resource/batch_get"
BASE64_FIELD = _string(((UPLOAD_CHUNK_BYTES + 2) // 3) * 4, 4,
                       pattern=r"^[A-Za-z0-9+/]*={0,2}$")
MEDIA_EXTENSIONS = {"image": ("png", "jpg", "jpeg", "webp", "gif"),
                    "video": ("mp4", "mov", "webm"),
                    "audio": ("mp3", "wav", "m4a", "aac", "ogg", "flac")}
UPLOAD_METADATA = {"mediaType": _enum(*MEDIA_EXTENSIONS),
    "fileExtension": _enum(*(extension for values in MEDIA_EXTENSIONS.values() for extension in values)),
    "name": _string(512), "importKind": _enum("local_upload", "external_generated", default="local_upload"),
    "sha256": SHA256_FIELD}
NODE_TARGET = {**PROJECT, "nodeId": NODE_FIELD}
RESOURCE_SOURCES = _array(_object({"resourceId": UUID_FIELD, "sourceProjectId": UUID_FIELD},
                                  ("resourceId", "sourceProjectId")), unique=True)


def _clip_schema(audio=False):
    fields = {
        "sourceId": SOURCE_FIELD,
        "trimStartMs": _integer(0), "trimDurationMs": _integer(1),
        "speed": _number(positive=True), "volume": _number(0, 1), "muted": BOOL_FIELD,
    }
    if audio:
        fields["startMs"] = _integer(0)
        return _object(fields, ("sourceId",))
    fields.update({"mediaType": _enum("image", "video"), "durationMs": _integer(1)})
    return _object(fields, ("sourceId", "mediaType"))


# Captured from the official `director catalog --kind builtin` on 2026-10-29.
# Commands whose nested structure is described only as "as in show" are not
# accepted. No local paths, arbitrary JSON patches, or binary imports are exposed.
DIRECTOR_COMMANDS: dict[str, dict[str, Any]] = {}
DIRECTOR_ID = _string(256)
VECTOR3 = _object({axis: _number() for axis in ("x", "y", "z")}, ("x", "y", "z"))
PROPERTY_CHANNELS = tuple(f"{kind}.{axis}" for kind in ("position", "rotation", "scale") for axis in ("x", "y", "z"))
CAMERA_CHANNELS = ("position.x", "position.y", "position.z", "target.x", "target.y", "target.z", "lens.fov", "lens.roll")
PRIMITIVES = ("cube", "sphere", "cuboid", "cylinder", "cone", "pyramid", "torus", "plane", "capsule", "disc", "semicircle-arc", "half-cylinder", "simple-human")
PROPERTY_TANGENTS = _object({"mode": _enum("free", "aligned", "vector"), **{
    side: _object({"frameDelta": _number(), "valueDelta": _number()}, ("frameDelta", "valueDelta"))
    for side in ("in", "out")}}, ("mode",))
SPATIAL_TANGENTS = _object({"mode": _enum("auto", "aligned", "free", "linear"),
                           "in": VECTOR3, "out": VECTOR3}, ("mode",))


def _director_command(name, fields, required=()):
    DIRECTOR_COMMANDS[name] = _object({"type": {"type": "string", "const": name}, **fields}, ("type", *required))


_director_command("document.title", {"title": _string(512)}, ("title",))
_director_command("workspace.select", {"workspace": _enum("staging", "performance")}, ("workspace",))
_director_command("workspace.performance", {"copyStaging": BOOL_FIELD}, ("copyStaging",))
_director_command("object.primitive", {"primitive": _enum(*PRIMITIVES), "id": DIRECTOR_ID, "name": _string(512)}, ("primitive", "id"))
_director_command("object.preset", {"preset": DIRECTOR_ID, "id": DIRECTOR_ID, "name": _string(512)}, ("preset", "id"))
for _name in ("object.duplicate", "camera.duplicate"):
    _director_command(_name, {"id": DIRECTOR_ID, "newId": DIRECTOR_ID, "name": _string(512)}, ("id", "newId", "name"))
for _name in ("object.delete", "object.ground"):
    _director_command(_name, {"ids": _array(DIRECTOR_ID, 256, unique=True)}, ("ids",))
_director_command("group.create", {"id": DIRECTOR_ID, "name": _string(512), "ids": _array(DIRECTOR_ID, 256, unique=True)}, ("id", "name", "ids"))
for _name in ("group.delete", "camera.delete", "camera.output", "motion.delete", "motion.remove", "storyboard.delete"):
    _director_command(_name, {"id": DIRECTOR_ID}, ("id",))
_camera_common = {"id": DIRECTOR_ID, "name": _string(512), "outputPolicy": _enum("preserve", "select-new")}
_camera_view = _object({"position": VECTOR3, "target": VECTOR3, "fov": _number(positive=True),
                        "roll": _number(), "aspectRatio": _number(positive=True)},
                       ("position", "target", "fov", "roll", "aspectRatio"))
_camera_target = _object({"objectId": DIRECTOR_ID, "center": VECTOR3, "height": _number(positive=True),
                          "width": _number(positive=True), "yawDegrees": _number()},
                         ("objectId", "center", "height", "yawDegrees"))
_camera_presets = ("front-medium", "front-close-up", "front-wide", "side-follow", "side-close", "rear-medium",
                   "high-angle-wide", "three-quarter-high-angle", "low-angle-up", "low-angle-wide", "birds-eye", "dutch-angle")
_camera_add = {"oneOf": [
    _object({**_camera_common, "presetId": {"type": "string", "const": "current-view"}, "currentView": _camera_view},
            (*_camera_common, "presetId", "currentView")),
    _object({**_camera_common, "presetId": _enum(*_camera_presets), "target": _camera_target},
            (*_camera_common, "presetId", "target")),
    _object({**_camera_common, "presetId": {"type": "string", "const": "over-shoulder"},
             "foregroundTarget": _camera_target, "targetPoint": VECTOR3},
            (*_camera_common, "presetId", "foregroundTarget", "targetPoint")),
]}
_director_command("camera.add", {"camera": _camera_add}, ("camera",))
_director_command("camera.set", {"id": DIRECTOR_ID,
    "patch": _object(_camera_view["properties"]), "name": _string(512)}, ("id", "patch"))
_director_command("timeline.set", {"fps": _number(positive=True), "durationFrames": _integer(1), "loop": BOOL_FIELD})
_director_command("motion.duplicate", {"id": DIRECTOR_ID, "newId": DIRECTOR_ID, "frame": _integer(0)}, ("id", "newId", "frame"))
_director_command("motion.transition", {"id": DIRECTOR_ID, "frames": _integer(0)}, ("id", "frames"))
_director_command("motion.move", {"id": DIRECTOR_ID, "frame": _integer(0)}, ("id", "frame"))
_director_command("motion.resize", {"id": DIRECTOR_ID, "edge": _enum("start", "end"), "frame": _integer(0)}, ("id", "edge", "frame"))
_director_command("motion.playback", {"id": DIRECTOR_ID, "speed": _number(positive=True), "loop": BOOL_FIELD}, ("id",))
for _name, _channels in (("keyframe.set", PROPERTY_CHANNELS), ("camera.keyframe", CAMERA_CHANNELS)):
    _director_command(_name, {"id": DIRECTOR_ID, "channel": _enum(*_channels), "frame": _integer(0),
        "value": _number(), "interpolation": _enum("linear", "smooth", "hold"),
        "tangents": PROPERTY_TANGENTS, "spatialTangents": SPATIAL_TANGENTS}, ("id", "channel", "frame", "value"))
for _name, _channels in (("keyframe.delete", PROPERTY_CHANNELS), ("camera.keyframe.delete", CAMERA_CHANNELS)):
    _director_command(_name, {"id": DIRECTOR_ID, "channel": _enum(*_channels), "keyframeId": DIRECTOR_ID}, ("id", "channel", "keyframeId"))
_director_command("storyboard.add", {"id": DIRECTOR_ID, "cameraId": DIRECTOR_ID, "durationFrames": _integer(1)}, ("id", "cameraId", "durationFrames"))
_director_command("storyboard.trim", {"id": DIRECTOR_ID, "durationFrames": _integer(1)}, ("id", "durationFrames"))
_director_command("storyboard.move", {"id": DIRECTOR_ID, "index": _integer(0)}, ("id", "index"))
DIRECTOR_COMMAND_ARRAY = _array({"oneOf": list(DIRECTOR_COMMANDS.values())}, 256)
DIRECTOR_UNSUPPORTED_COMMANDS = ("object.add", "object.set", "environment.set", "pose.set", "motion.add", "motion.import", "motion.import-fbx")


@dataclass(frozen=True)
class Action:
    command: tuple[str, ...]
    fields: dict[str, Any]
    required: tuple[str, ...] = ()
    mode: str = "read"
    billable: bool = False
    local_state: bool = False
    unavailable_reason: str | None = None
    unavailable_code: str | None = None

    @property
    def writes(self):
        return self.mode not in ("read", "blocked")


ACTIONS: dict[str, Action] = {}


def _add(name, command, fields=None, required=(), mode="read", billable=False,
         local_state=False, reason=None, code=None):
    fields = dict(fields or {})
    if mode not in ("read", "blocked"):
        fields["dryRun"] = DRY_RUN
    if billable or mode == "confirm":
        fields["creditCeiling"] = CREDIT_CEILING
    ACTIONS[name] = Action(tuple(command), fields, tuple(required), mode,
                           billable, local_state, reason, code)


_add("account.status", ("auth", "status"))
_add("account.read", ("auth", "account"))
_add("account.refresh", ("auth", "refresh"), mode="local-plan", local_state=True)
_add("space.current", ("space", "current"))
_add("space.select", ("space", "use"),
     {"kind": _enum("personal", "team"), "teamId": _string(19, pattern=r"^[1-9][0-9]*$")},
     ("kind",), mode="local-plan", local_state=True)
_add("team.list", ("team", "ls"), {"limit": _integer(1, 100, default=50), "cursor": _string(4096)})
_add("voice.list", ("voice", "list"),
     {"offset": _integer(0, default=0), "count": _integer(1, 100, default=50), "model": _string()})
_add("canvas.list", ("canvas", "ls"), {"limit": _integer(1, 100, default=50), "cursor": _string(4096)})
_add("canvas.current", ("canvas", "current"))
_add("canvas.select", ("canvas", "use"), PROJECT, ("projectId",), mode="local-plan", local_state=True)
_add("canvas.create", ("canvas", "create"), {"name": _string(100)}, ("name",), mode="local-plan")
_add("node.list", ("node", "find"), {**PROJECT,
     "name": _string(2048), "type": _enum(*TYPES), "types": _array(_enum(*TYPES), 6, unique=True),
     "statuses": _array(_enum("empty", "running", "success", "partial_success", "failed", "canceled", "unknown"), 7, unique=True),
     "tags": _array(_enum(*TAGS), 5, unique=True),
     "limit": _integer(1, 50, default=50), "offset": _integer(0, 1000, default=0)}, ("projectId",))
_add("node.read", ("node", "show"), NODE_TARGET, ("projectId", "nodeId"))
_add("node.quote", ("node", "quote"), NODE_TARGET, ("projectId", "nodeId"))
_add("node.confirm", ("node", "confirm"), NODE_TARGET, ("projectId", "nodeId"), mode="confirm")
_add("node.run", ("node", "run"), {**NODE_TARGET, "nodeType": _enum("image", "video", "audio")},
     ("projectId", "nodeId", "nodeType"), mode="generate", billable=True)

MEDIA_FIELDS = {
    "image": {"mode": _enum("t2i", "i2i"), "prompt": _string(20000, 0), "model": _string(),
              "ratio": _string(32), "resolution": _string(32), "count": _integer(1),
              "refs": _array(REF_FIELD)},
    "video": {"mode": _enum("t2v", "first_last_frame", "m2v"), "prompt": _string(20000, 0),
              "model": _string(), "ratio": _string(32), "resolution": _string(32),
              "duration": _number(positive=True), "count": _integer(1), "refs": _array(REF_FIELD)},
    "audio": {"mode": _enum("tts", "music"), "prompt": _string(20000, 0), "model": _string(),
              "duration": _number(positive=True), "voiceId": _string(19, pattern=r"^[1-9][0-9]*$"),
              "voiceName": _string(), "scenario": _enum("text-reading", "omni-dubbing"),
              "refs": _array(REF_FIELD)},
}

for _kind in TYPES:
    for _verb in ("create", "update"):
        _fields = {**PROJECT, "title": _string(512), "x": _number(), "y": _number()}
        _required = ["projectId"]
        if _verb == "create":
            _required.append("title")
        else:
            _fields["nodeId"] = NODE_FIELD
            _required.append("nodeId")
        if _kind in ("image", "video", "audio", "element"):
            _fields["tags"] = _array(_enum(*TAGS), 5, unique=True)
            if _verb == "update":
                _fields["clearTags"] = BOOL_FIELD
        if _kind != "text":
            _fields["resourceSources"] = RESOURCE_SOURCES
        if _kind in MEDIA_FIELDS:
            _fields.update(MEDIA_FIELDS[_kind])
            if _verb == "create":
                _fields.update({"resourceIds": _array(UUID_FIELD),
                                "importKind": _enum("local_upload", "external_generated")})
            else:
                _fields["clearGeneration"] = BOOL_FIELD
        elif _kind == "element":
            _fields.update({"description": _string(20000, 0), "main": SOURCE_FIELD,
                            "voice": SOURCE_FIELD, "auxiliary": _array(SOURCE_FIELD)})
            if _verb == "create":
                _required.append("main")
            else:
                _fields.update({key: BOOL_FIELD for key in
                    ("clearDescription", "clearMain", "clearVoice", "clearAuxiliary")})
        elif _kind == "text":
            _fields["text"] = _string(200000, 0)
            if _verb == "create":
                _required.append("text")
        elif _kind == "timeline":
            _fields.update({"clips": _array(_clip_schema(), 256),
                            "audioClips": _array(_clip_schema(True), 256)})
            if _verb == "update":
                _fields["clearTracks"] = BOOL_FIELD
        _add(f"{_kind}.{_verb}", ("node", "create" if _verb == "create" else "edit", _kind),
             _fields, _required, mode="save")

for _kind in ("image", "video", "audio"):
    _add(f"{_kind}.generate", ("node", "run"), NODE_TARGET,
         ("projectId", "nodeId"), mode="generate", billable=True)
_add("video.regenerate", ("node", "run"), NODE_TARGET,
     ("projectId", "nodeId"), mode="generate", billable=True)

_post_fields = {**NODE_TARGET, "title": _string(512)}
_add("image.upscale", ("node", "upscale", "image"), {**_post_fields,
     "mode": _enum("normal", "pro", default="pro"), "resolution": _enum("2K", "4K", "8K"),
     "detail": _integer(1, 100, default=50)}, ("projectId", "nodeId"), mode="post", billable=True)
_add("image.cutout", ("node", "cutout", "image"), _post_fields,
     ("projectId", "nodeId"), mode="post", billable=True)
_add("video.upscale", ("node", "upscale", "video"),
     {**_post_fields, "resolution": _enum("2K", "4K")},
     ("projectId", "nodeId", "resolution"), mode="post", billable=True)
_add("video.interpolate", ("node", "interpolate", "video"),
     {**_post_fields, "fps": {"type": "integer", "enum": [30, 60]}},
     ("projectId", "nodeId", "fps"), mode="post", billable=True)
_add("operation.status", ("operation", "status"), {**PROJECT, "submitId": UUID_FIELD},
     ("projectId", "submitId"))
_add("operation.wait", ("operation", "wait"), {**PROJECT, "submitId": UUID_FIELD,
     "timeoutSeconds": _integer(0, 60, default=30), "intervalSeconds": _integer(1, 60, default=5)},
     ("projectId", "submitId"))
_add("resource.read", ("resource", "get"), {**PROJECT, "resourceId": UUID_FIELD},
     ("projectId", "resourceId"))
_add("resource.upload", ("resource", "upload"), {**PROJECT, **UPLOAD_METADATA,
     "contentBase64": BASE64_FIELD, "uploadId": UUID_FIELD}, ("projectId",), mode="resource-cloud")
_add("resource.upload.stage", (), {**PROJECT, **UPLOAD_METADATA, "contentBase64": BASE64_FIELD,
     "uploadId": UUID_FIELD, "byteSize": _integer(1, UPLOAD_MAX_BYTES),
     "offset": _integer(0, UPLOAD_MAX_BYTES), "chunkSha256": SHA256_FIELD},
     ("projectId", "mediaType", "fileExtension", "sha256", "contentBase64", "byteSize", "offset", "chunkSha256"),
     mode="resource-local", local_state=True)
_add("resource.upload.inspect", (), {**PROJECT, "uploadId": UUID_FIELD}, ("projectId", "uploadId"))
_add("resource.download", ("resource", "download"), {**PROJECT, "resourceId": UUID_FIELD},
     ("projectId", "resourceId"), mode="resource-local", local_state=True)
_add("resource.artifact.read", (), {**PROJECT, "artifactId": UUID_FIELD,
     "offset": _integer(0, UPLOAD_MAX_BYTES, default=0), "length": _integer(1, 65536, default=16384)},
     ("projectId", "artifactId"))
_add("resource.operation.get", (), {"operationId": UUID_FIELD}, ("operationId",))
RESOURCE_MANAGED_ACTIONS = frozenset(("resource.upload", "resource.upload.stage", "resource.upload.inspect",
    "resource.download", "resource.artifact.read", "resource.operation.get"))
_add("director.catalog", ("director", "catalog"),
     {"kind": _enum("builtin", "character", "motion", default="builtin"),
      "offset": _integer(0, default=0), "count": _integer(1, 100, default=50)})
_add("director.get", ("director", "show"), NODE_TARGET, ("projectId", "nodeId"))

_add("director.create", ("director", "create"), {**PROJECT, "title": _string(512)},
     ("projectId",), mode="director-cloud")
_add("director.apply", ("director", "apply"), {**NODE_TARGET, "commands": DIRECTOR_COMMAND_ARRAY},
     ("projectId", "nodeId", "commands"), mode="director-cloud")
_add("director.draft.pull", ("director", "pull"), {**NODE_TARGET,
     "versionId": _string(20, pattern=r"^(?:0|[1-9][0-9]*)$")},
     ("projectId", "nodeId"), mode="director-local", local_state=True)
_draft_target = {**PROJECT, "draftId": UUID_FIELD}
_add("director.draft.get", ("director", "show"), _draft_target, ("projectId", "draftId"))
_add("director.draft.apply", ("director", "apply"), {**_draft_target, "commands": DIRECTOR_COMMAND_ARRAY},
     ("projectId", "draftId", "commands"), mode="director-local", local_state=True)
_add("director.draft.push", ("director", "push"), _draft_target,
     ("projectId", "draftId"), mode="director-cloud")
_add("director.operation.get", (), {"operationId": UUID_FIELD}, ("operationId",))
_add("director.resume", ("director", "push"), {**PROJECT, "operationId": UUID_FIELD},
     ("projectId", "operationId"), mode="director-cloud")
_add("director.fbx.export", ("director", "export"), {**NODE_TARGET, "draftId": UUID_FIELD,
     "motionId": DIRECTOR_ID}, ("projectId", "motionId"), mode="director-local", local_state=True)
_add("director.artifact.read", (), {**PROJECT, "artifactId": UUID_FIELD,
     "offset": _integer(0, 64 * 1024 * 1024, default=0),
     "length": _integer(1, 65536, default=16384)}, ("projectId", "artifactId"))
DIRECTOR_MANAGED_ACTIONS = frozenset(("director.create", "director.apply", "director.draft.pull",
    "director.draft.get", "director.draft.apply", "director.draft.push", "director.operation.get", "director.resume",
    "director.fbx.export", "director.artifact.read"))

for _name in ("director.preview", "director.export", "director.freeze"):
    _add(_name, (), NODE_TARGET, mode="blocked", code="BROWSER_RUNTIME_REQUIRED",
         reason="Image/video rendering and freeze require a local external browser; no approved Codex Browser-compatible runtime is implemented. No browser is launched.")


def supports(action: Any) -> bool:
    return isinstance(action, str) and action in ACTIONS


def is_mutating(action: str) -> bool:
    return ACTIONS[action].writes if action in ACTIONS else False


def request_schemas() -> dict[str, dict[str, Any]]:
    """Closed JSON Schemas for integration/OpenAPI; no caller mutation of registry."""
    schemas = {}
    for name, spec in ACTIONS.items():
        schemas[name] = _object({"action": {"type": "string", "const": name}, **spec.fields},
                                ("action", *spec.required))
        schemas[name]["title"] = name
        if name == "timeline.create":
            schemas[name]["anyOf"] = [{"required": ["clips"]}, {"required": ["audioClips"]}]
        if name == "audio.create":
            schemas[name]["anyOf"] = [
                {"required": ["voiceId"]}, {"required": ["voiceName"]},
                {"required": ["mode"], "properties": {"mode": {"const": "music"}}},
                {"required": ["scenario"], "properties": {"scenario": {"const": "omni-dubbing"}}},
                {"required": ["resourceIds"]},
            ]
        if name == "director.fbx.export":
            schemas[name]["anyOf"] = [{"required": ["nodeId"]}, {"required": ["draftId"]}]
            schemas[name]["not"] = {"required": ["nodeId", "draftId"]}
        if name == "resource.upload":
            schemas[name]["anyOf"] = [{"required": ["uploadId"]},
                {"required": ["mediaType", "fileExtension", "sha256", "contentBase64"]}]
            schemas[name]["not"] = {"required": ["uploadId", "contentBase64"]}
        if spec.unavailable_reason:
            schemas[name]["description"] = "Unavailable: " + spec.unavailable_reason
        elif spec.mode == "save":
            schemas[name]["description"] = "Save only; never starts generation. dryRun defaults to true."
        elif spec.mode == "generate":
            schemas[name]["description"] = "Run the existing saved media draft through read/type-check, quote, private confirm, and node run. dryRun returns only a bridge-local plan; the saved draft is not edited."
        elif name in DIRECTOR_MANAGED_ACTIONS:
            schemas[name]["description"] = "Director files are server-managed and addressed only by opaque IDs. Mutations default to a local plan; no arbitrary paths or force-overwrite are accepted."
        elif name in RESOURCE_MANAGED_ACTIONS:
            schemas[name]["description"] = "Media bytes use bounded base64 chunks with SHA-256, or opaque managed upload/artifact IDs. No caller-supplied paths or URLs. Uploads use stable resource IDs. Downloads support successful images through the CLI and successful MP4 video or MP3 audio through an authenticated fixed resource read and credential-free allowlisted CDN fetch. Other audio encodings are not enabled."
    return deepcopy(schemas)


def _validate_schema(value, schema, path="payload"):
    if "oneOf" in schema:
        matches = 0
        for alternative in schema["oneOf"]:
            try:
                _validate_schema(value, alternative, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(f"{path} must match exactly one supported typed structure")
        return
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "boolean": type(value) is bool,
             "integer": type(value) is int, "number": type(value) in (int, float)}.get(kind, False)
    if not valid:
        raise ValueError(f"{path} must be {kind}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} has an invalid constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if kind == "object":
        if any(not isinstance(k, str) for k in value):
            raise ValueError(f"{path} keys must be strings")
        unknown = set(value) - set(schema["properties"])
        if unknown:
            raise ValueError(f"{path} has unsupported fields: {', '.join(sorted(unknown))}")
        if set(schema.get("required", ())) - set(value):
            raise ValueError(f"{path} is missing required fields")
        for key, item in value.items():
            _validate_schema(item, schema["properties"][key], path + "." + key)
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 2147483647):
            raise ValueError(f"{path} has an invalid number of items")
        for item in value:
            _validate_schema(item, schema["items"], path + "[]")
        if schema.get("uniqueItems"):
            serialized = [json.dumps(item, sort_keys=True, allow_nan=False) for item in value]
            if len(set(serialized)) != len(serialized):
                raise ValueError(f"{path} must contain unique items")
    elif kind == "string":
        if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError(f"{path} contains unsupported Unicode")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 2147483647):
            raise ValueError(f"{path} has an invalid length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"{path} has an invalid format")
    elif kind in ("number", "integer"):
        if type(value) is float and not math.isfinite(value):
            raise ValueError(f"{path} must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below the minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above the maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} must be positive")


def _credit_cap() -> int | str:
    raw = os.environ.get("JIMENG_MAX_CREDITS", "0")
    if raw == "unlimited":
        return "unlimited"
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        return 0
    return min(int(raw), 100000)


def _prompt_references(prompt: str) -> list[str]:
    """Apply the same node/res restriction to typed prompt placeholders."""
    references = []
    # Reject unsupported typed ingestion even if the placeholder is malformed
    # or escaped. Plain URLs in prose are not interpreted as references here.
    if re.search(r"\{\{\s*(?:uri|vid)\s*:", prompt):
        raise ValueError("Prompt placeholders only support existing node: or res: references")
    for match in re.finditer(r"\{\{", prompt):
        if match.start() > 0 and prompt[match.start() - 1] == "\\":
            continue
        end = prompt.find("}}", match.end())
        if end < 0:
            raise ValueError("Typed prompt reference has no closing braces")
        content = prompt[match.end():end]
        if not re.fullmatch(REF_FIELD["pattern"], content):
            raise ValueError("Typed prompt references must be canonical node: or res: identities")
        references.append(content)
    return references


def _validate_clips(payload):
    if payload["action"] == "timeline.create" and not any(k in payload for k in ("clips", "audioClips")):
        raise ValueError("Timeline creation requires at least one of clips or audioClips")
    if payload.get("clearTracks") and any(k in payload for k in ("clips", "audioClips")):
        raise ValueError("clearTracks cannot be combined with clips or audioClips")
    for key in ("clips", "audioClips"):
        for clip in payload.get(key, []):
            if ("trimStartMs" in clip) != ("trimDurationMs" in clip):
                raise ValueError("Both trimStartMs and trimDurationMs are required for a trim")
            if key == "clips" and clip["mediaType"] == "image":
                if set(clip) - {"sourceId", "mediaType", "durationMs"}:
                    raise ValueError("Image timeline clips only support durationMs")
            elif "durationMs" in clip:
                raise ValueError("Only image clips accept durationMs")


def validate(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not supports(payload.get("action")):
        raise ValueError("Unsupported fixed official action")
    action = payload["action"]
    spec = ACTIONS[action]
    _validate_schema(payload, request_schemas()[action])
    if spec.mode == "blocked":
        return
    if spec.writes and not payload.get("dryRun", True):
        if os.environ.get("JIMENG_READ_ONLY", "1") != "0":
            raise ValueError("Live writes are disabled by JIMENG_READ_ONLY; keep dryRun true")
        if spec.billable or spec.mode == "confirm":
            ceiling = payload.get("creditCeiling", 0)
            cap = _credit_cap()
            if cap != "unlimited" and not 0 < ceiling <= cap:
                raise ValueError("Live approval/generation requires an explicit positive creditCeiling within JIMENG_MAX_CREDITS")
    if "title" in payload and (not payload["title"].strip() or len(payload["title"].encode("utf-16-le")) // 2 > 512):
        raise ValueError("title must be nonempty and at most 512 UTF-16 code units")
    if "versionId" in payload and int(payload["versionId"]) > 18446744073709551615:
        raise ValueError("versionId exceeds uint64")
    if action == "director.fbx.export" and ("nodeId" in payload) == ("draftId" in payload):
        raise ValueError("FBX export requires exactly one source nodeId or managed draftId")
    if action in ("resource.upload", "resource.upload.stage"):
        if action == "resource.upload" and "uploadId" in payload:
            if set(payload) - {"action", "projectId", "uploadId", "dryRun"}:
                raise ValueError("A staged upload uses its original metadata; do not include replacement media or metadata")
        else:
            if not {"mediaType", "fileExtension", "sha256", "contentBase64"} <= payload.keys():
                raise ValueError("Direct media upload requires type, extension, SHA-256 and bounded base64 data")
            if payload["fileExtension"] not in MEDIA_EXTENSIONS[payload["mediaType"]]:
                raise ValueError("Media extension does not match the selected media type")
            chunk = _decode_media(payload["contentBase64"])
            expected = payload["chunkSha256"] if action == "resource.upload.stage" else payload["sha256"]
            if hashlib.sha256(chunk).hexdigest() != expected:
                raise ValueError("Decoded media does not match its declared SHA-256")
            if action == "resource.upload.stage":
                if "uploadId" not in payload and payload["offset"] != 0:
                    raise ValueError("The first media chunk must start at offset zero")
                if payload["offset"] + len(chunk) > payload["byteSize"]:
                    raise ValueError("Media chunk exceeds the declared complete file size")
    for command in payload.get("commands", []):
        if command["type"] == "camera.set" and not command["patch"]:
            raise ValueError("camera.set requires a nonempty typed patch")
        if command["type"] == "timeline.set" and set(command) == {"type"}:
            raise ValueError("timeline.set must specify a field to change")
        if command["type"] == "motion.playback" and not any(k in command for k in ("speed", "loop")):
            raise ValueError("motion.playback requires speed or loop")
        if "spatialTangents" in command and not command["channel"].startswith("position."):
            raise ValueError("Spatial tangents are only supported on position channels")
    if action == "canvas.create" and (not payload["name"].strip() or payload["name"].startswith("-")):
        raise ValueError("Canvas name must not be blank or begin with a CLI option prefix")
    if ("x" in payload) != ("y" in payload):
        raise ValueError("x and y must be supplied together")
    if "type" in payload and "types" in payload:
        raise ValueError("Supply type or types, not both")
    if payload.get("clearTags") and "tags" in payload:
        raise ValueError("clearTags conflicts with tags")
    for key in ("voiceId", "teamId"):
        if key in payload and int(payload[key]) > 9223372036854775807:
            raise ValueError(f"{key} exceeds int64")
    if action == "space.select":
        if payload["kind"] == "team" and "teamId" not in payload:
            raise ValueError("teamId is required for a team space")
        if payload["kind"] == "personal" and "teamId" in payload:
            raise ValueError("Personal space must not include teamId")
    if spec.mode == "save":
        kind, verb = action.split(".")
        if verb == "update" and not set(payload) - {"action", "projectId", "nodeId", "dryRun"}:
            raise ValueError("An update must explicitly change at least one field")
        if "resourceIds" in payload and "importKind" not in payload:
            raise ValueError("resourceIds require importKind")
        if kind in MEDIA_FIELDS:
            generation_fields = set(payload) & set(MEDIA_FIELDS[kind])
            prompt_refs = _prompt_references(payload.get("prompt", ""))
            effective_refs = set(payload.get("refs", [])) | set(prompt_refs)
            if len(effective_refs) > 32:
                raise ValueError("At most 32 combined typed prompt and explicit references are supported")
            if payload.get("clearGeneration") and generation_fields:
                raise ValueError("clearGeneration conflicts with generation fields")
            if kind in ("image", "video") and generation_fields and "mode" not in payload:
                raise ValueError("mode is required when saving generation parameters")
            if kind == "video" and payload.get("mode") == "first_last_frame" and effective_refs and len(effective_refs) not in (1, 2):
                raise ValueError("first_last_frame accepts one or two ordered references")
            if kind == "audio":
                mode = payload.get("mode", "tts")
                pure_resource_import = "resourceIds" in payload and not generation_fields
                if verb == "create" and mode == "tts" and payload.get("scenario") != "omni-dubbing" and not pure_resource_import and not any(k in payload for k in ("voiceId", "voiceName")):
                    raise ValueError("TTS audio creation requires voiceId or voiceName from voice.list; omni-dubbing and pure resource imports have separate contracts")
                if mode == "music" and "model" not in payload:
                    raise ValueError("Music requires a model discovered from model list")
                if "voiceId" in payload and "voiceName" in payload:
                    raise ValueError("voiceId and voiceName are mutually exclusive")
                if "voiceName" in payload and not payload["voiceName"].strip():
                    raise ValueError("voiceName must be a nonempty catalog voice name")
                if mode != "tts" and any(k in payload for k in ("voiceId", "voiceName", "scenario")):
                    raise ValueError("Voice and scenario fields require tts")
                if "scenario" in payload and "model" not in payload:
                    raise ValueError("scenario requires an explicit model")
                if "duration" in payload and mode != "music":
                    raise ValueError("Audio duration applies to music mode")
        if kind == "element":
            for clear, field in (("clearDescription", "description"), ("clearVoice", "voice"), ("clearAuxiliary", "auxiliary")):
                if payload.get(clear) and field in payload:
                    raise ValueError(f"{clear} conflicts with {field}")
            if payload.get("clearMain") and any(k in payload for k in ("main", "voice", "auxiliary")):
                raise ValueError("clearMain conflicts with binding assignments")
            found_resource = False
            for source in payload.get("auxiliary", []):
                if source.startswith("node_"):
                    if found_resource:
                        raise ValueError("Auxiliary node sources must precede resource sources")
                else:
                    found_resource = True
        if kind == "timeline":
            _validate_clips(payload)
        if "resourceSources" in payload:
            by_resource = {}
            for source in payload["resourceSources"]:
                old = by_resource.setdefault(source["resourceId"], source["sourceProjectId"])
                if old != source["sourceProjectId"]:
                    raise ValueError("Conflicting resource source projects")
    if action == "image.upscale":
        if payload.get("mode", "pro") == "pro" and "resolution" not in payload:
            raise ValueError("Pro upscale requires resolution")
        if payload.get("mode", "pro") == "normal" and any(k in payload for k in ("resolution", "detail")):
            raise ValueError("Normal upscale must omit resolution and detail")


def _identities(job_id: str) -> tuple[str, str]:
    if not isinstance(job_id, str) or not job_id or len(job_id) > 200 or "\x00" in job_id:
        raise ValueError("A stable nonempty local job id is required")
    stable = str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:" + job_id))
    number = uuid.UUID(stable).int
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    node_id = "node_" + "".join(alphabet[(number >> (5 * i)) & 31] for i in range(9, -1, -1))
    return stable, node_id


def recovery(payload: dict[str, Any], job_id: str) -> dict[str, Any] | None:
    if not supports(payload.get("action")):
        return None
    stable, created_node = _identities(job_id)
    action = payload["action"]
    spec = ACTIONS[action]
    base = {"projectId": payload["projectId"]} if "projectId" in payload else {}
    if action in DIRECTOR_MANAGED_ACTIONS:
        if not spec.writes:
            return None
        return {**base, "operationId": stable, "nextAction": "director.operation.get",
                "doNotResubmit": True,
                **({"draftId": payload["draftId"]} if "draftId" in payload else {}),
                **({"draftId": stable} if action in ("director.create", "director.draft.pull") else {})}
    if action in RESOURCE_MANAGED_ACTIONS:
        if not spec.writes:
            return None
        result = {**base, "operationId": stable, "nextAction": "resource.operation.get", "doNotResubmit": True}
        if action == "resource.upload.stage":
            result.update({"operationId": payload.get("uploadId", stable),
                           "uploadId": payload.get("uploadId", stable), "nextAction": "resource.upload.inspect"})
        elif action == "resource.upload":
            result.update({"resourceId": _resource_upload_id(payload, job_id), "nextAction": "resource.read"})
        return result
    if spec.mode in ("generate", "post"):
        return {**base, "nodeId": payload["nodeId"], "submitId": stable,
                "nextAction": "operation.status", "doNotResubmit": True}
    if spec.mode == "save":
        result = {**base, "nodeId": payload.get("nodeId", created_node), "updateId": stable,
                  "nextAction": "node.read", "doNotResubmit": True}
        if "resourceIds" in payload:
            result["submitId"] = stable
        return result
    if action == "canvas.create":
        return {"projectId": stable, "nextAction": "canvas.list", "doNotResubmit": True}
    if action.startswith("operation."):
        return {**base, "submitId": payload["submitId"], "nextAction": "operation.status", "doNotResubmit": True}
    return None


def capabilities() -> dict[str, Any]:
    result = {}
    for name, spec in ACTIONS.items():
        result[name] = {
            "available": spec.mode != "blocked", "backend": "official-dreamina-canvas",
            "contractVerified": True, "executionVerified": False,
            "mutatesWorkspace": spec.writes and not spec.local_state and spec.mode != "confirm",
            "mutatesLocalState": spec.local_state or spec.mode.startswith(("director-", "resource-")), "billable": spec.billable,
            "authorizesSpending": spec.billable or spec.mode == "confirm",
            "defaultDryRun": True if spec.writes else None,
            "dryRunKind": ("official-local" if spec.mode in ("save", "post")
                           else "bridge-local-plan" if spec.writes else None),
        }
        if spec.unavailable_reason:
            result[name].update({"reason": spec.unavailable_reason, "code": spec.unavailable_code,
                                 "contractVerified": name != "director.apply"})
        if name in DIRECTOR_MANAGED_ACTIONS:
            result[name]["managedFiles"] = True
            result[name]["storageConfigured"] = bool(os.environ.get("JIMENG_DIRECTOR_STATE_DIR") or os.environ.get("DREAMINA_CANVAS_STATE_DIR"))
        if name in RESOURCE_MANAGED_ACTIONS:
            result[name]["managedFiles"] = True
            result[name]["maxBase64DecodedChunkBytes"] = UPLOAD_CHUNK_BYTES
            result[name]["maxManagedFileBytes"] = UPLOAD_MAX_BYTES
        if name == "resource.download":
            result[name].update({"mediaTypes": ["image", "video", "audio"],
                "videoRequiresWebSession": True, "videoFormats": ["mp4"],
                "audioRequiresWebSession": True, "audioFormats": ["mp3"],
                "videoDownloadHosts": sorted(VIDEO_DOWNLOAD_HOSTS),
                "audioDownloadHosts": sorted(AUDIO_DOWNLOAD_HOSTS), "redirectsAllowed": False})
        if name in ("director.apply", "director.draft.apply"):
            result[name]["supportedCommandTypes"] = sorted(DIRECTOR_COMMANDS)
            result[name]["unsupportedCommandTypes"] = list(DIRECTOR_UNSUPPORTED_COMMANDS)
    return {"contractVersion": CONTRACT_VERSION, "actions": result,
            "liveWritesEnabled": os.environ.get("JIMENG_READ_ONLY", "1") == "0",
            "liveCreditCeiling": _credit_cap(), "defaultCreditCeiling": 0,
            "limitations": ["No canvas-node deletion, arbitrary URL/file import, shell, or raw CLI forwarding",
                "Create/update only save; generation is a separate action",
                "One source node per job; submitId and updateId are stable per job",
                "Director apply exposes only its published typed command subset and checks the live catalog",
                "Director image/video rendering remains unavailable; FBX export needs no browser and returns a managed artifact ID",
                "CLI completion is distinct from upstream media completion"]}


def _flag(args, name, value):
    # A complete --flag=value argv element prevents free text being interpreted
    # as another CLI option. No shell is involved in this contract.
    if type(value) is bool:
        if value:
            args.append("--" + name)
    else:
        args.append("--" + name + "=" + str(value))


def _clip(clip):
    source = clip["sourceId"]
    parts = [source, "kind=" + ("canvas_node" if source.startswith("node_") else "resource")]
    if "durationMs" in clip:
        parts.append("duration=" + str(clip["durationMs"]))
    if "startMs" in clip:
        parts.append("start=" + str(clip["startMs"]))
    if "trimStartMs" in clip:
        parts.append(f"trim={clip['trimStartMs']}-{clip['trimDurationMs']}")
    for name in ("speed", "volume"):
        if name in clip:
            parts.append(name + "=" + str(clip[name]))
    if clip.get("muted"):
        parts.append("muted")
    return ",".join(parts)


def _compile(payload, job_id):
    action = payload["action"]
    spec = ACTIONS[action]
    stable, node_id = _identities(job_id)
    args = list(spec.command)
    if action == "canvas.create":
        args.append(payload["name"])
        _flag(args, "project-id", stable)
    elif action == "canvas.select":
        args.append(payload["projectId"])
    elif action == "space.select":
        args.append(payload["kind"])
        if "teamId" in payload:
            args.append(payload["teamId"])
    elif action.startswith("operation."):
        args.append(payload["submitId"])
    elif action == "resource.read":
        args.append(payload["resourceId"])
    if "projectId" in payload and action != "canvas.select":
        _flag(args, "project-id", payload["projectId"])
    if "nodeId" in payload:
        _flag(args, "node-id", payload["nodeId"])
    if spec.mode == "save":
        if action.endswith(".create"):
            _flag(args, "node-id", node_id)
        _flag(args, "update-id", stable)
        if "resourceIds" in payload:
            _flag(args, "submit-id", stable)
    if spec.mode in ("generate", "post"):
        _flag(args, "submit-id", stable)
    if spec.mode in ("post", "confirm"):
        if spec.mode == "post" and not payload.get("dryRun", True) and "creditCeiling" not in payload and _credit_cap() == "unlimited":
            args.append("--yes")
        else:
            _flag(args, "credit-ceiling", payload.get("creditCeiling", 0))

    mapping = {
        "name": "name", "limit": "limit", "cursor": "cursor", "offset": "offset", "count": "count",
        "title": "title", "x": "x", "y": "y", "mode": "mode", "prompt": "prompt", "model": "model",
        "ratio": "ratio", "resolution": "resolution", "duration": "duration", "voiceId": "voice-id",
        "voiceName": "voice-name", "scenario": "scenario", "text": "text", "description": "description",
        "main": "main", "voice": "voice", "importKind": "import-kind", "detail": "detail", "fps": "fps",
        "clearTags": "clear-tags", "clearGeneration": "clear-generation", "clearDescription": "clear-description",
        "clearMain": "clear-main", "clearVoice": "clear-voice", "clearAuxiliary": "clear-auxiliary",
        "clearTracks": "clear-tracks", "type": "type",
    }
    for key, flag in mapping.items():
        if key in payload and not (key == "name" and action == "canvas.create"):
            _flag(args, flag, payload[key])
    for key, flag in (("tags", "tag"), ("types", "type"), ("statuses", "status"),
                      ("refs", "ref"), ("resourceIds", "resource-id"), ("auxiliary", "auxiliary")):
        for item in payload.get(key, []):
            _flag(args, flag, item)
    for source in payload.get("resourceSources", []):
        _flag(args, "resource-source", source["resourceId"] + "@" + source["sourceProjectId"])
    for key, flag in (("clips", "clip"), ("audioClips", "audio-clip")):
        for clip in payload.get(key, []):
            _flag(args, flag, _clip(clip))
    if action == "director.catalog":
        _flag(args, "kind", payload.get("kind", "builtin"))
    if action == "operation.wait":
        _flag(args, "timeout", str(payload.get("timeoutSeconds", 30)) + "s")
        _flag(args, "interval", str(payload.get("intervalSeconds", 5)) + "s")
    if spec.mode in ("save", "post") and payload.get("dryRun", True):
        args.append("--dry-run")
    return args


def _redact(value, secrets=()):
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.search(r"token|cookie|secret|device.?code|authorization|signature", str(key), re.I)
                      else _redact(item, secrets)) for key, item in value.items()}
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def _call(invoke, args):
    try:
        upstream = invoke(args)
        if not isinstance(upstream, dict) or type(upstream.get("ok")) is not bool:
            raise TypeError("Invalid CLI response")
        return upstream
    except Exception:
        # Suppress exceptions containing CLI argv, because run argv can carry
        # the ephemeral token. The caller treats this as an uncertain outcome.
        raise RuntimeError("Official CLI call did not return a reliable result; reconcile the original recovery identity before any retry") from None


def _execute_generation(payload, job_id, invoke, invoke_sensitive):
    action = payload["action"]
    recovery_info = recovery(payload, job_id)
    base = {"action": action, "dryRun": False, "dryRunKind": None,
            "generationSubmitted": False, "workspaceMutationAttempted": False,
            "localStateMutationAttempted": False, "creditApprovalAttempted": False,
            "recovery": recovery_info}
    if invoke_sensitive is None:
        return {**base, "available": False, "cliInvoked": False,
                "error": {"code": "PRIVATE_CONFIRM_CHANNEL_REQUIRED",
                          "message": "Live generation requires the internal private confirmation callback. No quote, approval, edit, or run was attempted."}}
    target = ["--project-id=" + payload["projectId"], "--node-id=" + payload["nodeId"]]
    view = _call(invoke, ["node", "show", *target])
    base["cliInvoked"] = True
    if not view["ok"]:
        return {**base, "phase": "node-read", "upstream": _redact(view)}
    expected_type = payload["nodeType"] if action == "node.run" else action.split(".")[0]
    from upstream_contracts import parse_node_show, UpstreamContractError
    try:
        node = parse_node_show(view, payload["nodeId"], expected_type)
    except UpstreamContractError:
        return {**base, "phase": "node-read", "error": {"code": "SOURCE_NODE_TYPE_UNVERIFIED",
                "message": "The saved source node identity/type could not be verified; no approval or generation was attempted."}}
    quote = _call(invoke, ["node", "quote", *target])
    if not quote["ok"]:
        return {**base, "phase": "quote", "upstream": _redact(quote)}
    data = quote.get("data", {})
    items = data.get("items", []) if isinstance(data, dict) else []
    total = data.get("totalMaxCredits") if isinstance(data, dict) else None
    item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else None
    max_credits = item.get("maxCredits") if item else None
    if (item is None or item.get("nodeId") != payload["nodeId"] or item.get("error")
            or type(total) is not int or total < 0 or type(max_credits) is not int or max_credits < 0
            or ("confirmable" in data and data["confirmable"] is not True)):
        return {**base, "phase": "quote", "upstream": _redact(quote),
                "error": {"code": "INCOMPLETE_QUOTE", "message": "A complete confirmable quote was not returned; no approval or generation was attempted."}}
    ceiling = payload["creditCeiling"] if "creditCeiling" in payload else total
    if total > ceiling or max_credits > ceiling:
        return {**base, "phase": "quote", "upstream": _redact(quote),
                "error": {"code": "CREDIT_CEILING_EXCEEDED", "message": "The quote exceeds the explicit approved ceiling; no approval or generation was attempted."}}
    confirmation = _call(invoke_sensitive, ["node", "confirm", *target, "--credit-ceiling=" + str(ceiling)])
    base["creditApprovalAttempted"] = True
    if not confirmation["ok"]:
        return {**base, "phase": "confirm", "upstream": _redact(confirmation)}
    confirmed = confirmation.get("data", {})
    token = confirmed.get("creditConfirmationToken") if isinstance(confirmed, dict) else None
    approved = confirmed.get("creditCeiling") if isinstance(confirmed, dict) else None
    if (not isinstance(token, str) or not token.strip() or len(token) > 8192
            or any(c.isspace() for c in token) or "\x00" in token
            or token in ("[REDACTED]", "<redacted>", "***")
            or type(approved) is not int or not max(total, max_credits) <= approved <= ceiling):
        return {**base, "phase": "confirm", "error": {"code": "CONFIRMATION_CREDENTIAL_UNAVAILABLE",
                "message": "A valid scoped confirmation credential and bounded ceiling were not returned; generation was not attempted."}}
    run_args = _compile(payload, job_id)
    run_args.append("--credit-token=" + token)
    try:
        upstream = _call(invoke, run_args)
        safe_result = _redact(upstream, (token,))
    finally:
        # No token, raw confirmation envelope, or token-bearing argv is returned
        # or persisted. This also prevents accidental later reuse in this frame.
        token = None
        confirmed = None
        confirmation = None
        run_args.clear()
    return {**base, "phase": "run", "generationSubmitted": None,
            "workspaceMutationAttempted": True, "upstream": safe_result,
            "quote": _redact(quote), "effectiveCreditCeiling": ceiling,
            "note": "The original saved draft was not edited. node run may only be accepted; query the original submitId for actual media completion."}


def _execute_director(payload, job_id, invoke):
    action = payload["action"]
    spec = ACTIONS[action]
    base = {"action": action, "dryRun": payload.get("dryRun", True) if spec.writes else False,
            "cliInvoked": False, "generationSubmitted": False, "workspaceMutationAttempted": False,
            "localStateMutationAttempted": False, "creditApprovalAttempted": False,
            "recovery": recovery(payload, job_id)}
    if base["dryRun"]:
        return {**base, "dryRunKind": "bridge-local-plan",
                "plan": {"command": list(spec.command), "commandCount": len(payload.get("commands", [])),
                         "managedFiles": True, "forceOverwrite": False},
                "note": "Only the fixed request structure was validated. No CLI call, managed file, workspace change or visual validation occurred."}
    try:
        root = _director_root(create=spec.writes)
    except (ValueError, OSError):
        return {**base, "available": False, "error": {"code": "DIRECTOR_STORAGE_UNAVAILABLE",
                "message": "Configure a private absolute JIMENG_DIRECTOR_STATE_DIR, or DREAMINA_CANVAS_STATE_DIR. No CLI call was made."}}
    if action == "director.operation.get":
        record = _director_read_record(root, payload["operationId"])
        return {**base, "operation": _director_record_view(record)}
    if action == "director.artifact.read":
        return {**base, "artifact": _director_read_artifact(root, payload)}
    if action == "director.draft.get":
        file = _director_draft(root, payload["draftId"], payload["projectId"])
        result = _call(invoke, ["director", "show", "--file=" + str(file), "--project-id=" + payload["projectId"]])
        return {**base, "cliInvoked": True, "draftId": payload["draftId"], "upstream": _director_public(result)}

    operation_id = _identities(job_id)[0]
    record, fresh = _director_begin(root, operation_id, payload)
    if not fresh:
        if record.get("result") is not None and record["state"] in ("complete", "failed", "saved-unverified"):
            return {**record["result"], "replayed": True}
        return {**base, "operationId": operation_id,
                "error": {"code": "DIRECTOR_RECONCILIATION_REQUIRED",
                          "message": "This operation already started. Inspect director.operation.get; do not reapply or recreate it."}}
    directory = _director_operation_dir(root, operation_id)
    base.update({"operationId": operation_id, "localStateMutationAttempted": True})
    try:
        if "commands" in payload:
            catalog = _call(invoke, ["director", "catalog", "--kind=builtin"])
            base["cliInvoked"] = True
            if not catalog["ok"] or not _director_catalog_accepts(payload["commands"], catalog.get("data", {})):
                result = {**base, "phase": "catalog", "upstream": _director_public(catalog),
                    "error": {"code": "DIRECTOR_CATALOG_MISMATCH", "message": "The current official catalog did not confirm every submitted typed field. No director mutation was attempted."}}
                return _director_complete(root, record, result, "failed")
            commands_file = directory / "commands.json"
            _director_write_json(commands_file, payload["commands"])
        args = list(spec.command)
        work_file = None
        source_record = None
        if action in ("director.create", "director.draft.pull"):
            work_file = directory / "work.json"
            record["workFile"] = str(work_file)
            args.append("--file=" + str(work_file))
            if action == "director.create":
                _flag(args, "title", payload.get("title", "Director Stage"))
            else:
                _flag(args, "node-id", payload["nodeId"])
                if "versionId" in payload:
                    _flag(args, "version-id", payload["versionId"])
        elif action.startswith("director.draft."):
            work_file = _director_draft(root, payload["draftId"], payload["projectId"])
            record["workFile"] = str(work_file)
            args.append("--file=" + str(work_file))
        elif action == "director.apply":
            _flag(args, "node-id", payload["nodeId"])
        elif action == "director.fbx.export":
            if "draftId" in payload:
                source_file = _director_draft(root, payload["draftId"], payload["projectId"])
                _flag(args, "file", str(source_file))
            else:
                _flag(args, "node-id", payload["nodeId"])
            _flag(args, "type", "fbx")
            _flag(args, "motion-id", payload["motionId"])
            _flag(args, "output", str(directory / "motion.fbx"))
        elif action == "director.resume":
            source_record = _director_read_record(root, payload["operationId"])
            if source_record.get("projectId") != payload["projectId"]:
                raise ValueError("Director recovery project does not match")
            if source_record.get("state") == "complete" and source_record.get("result"):
                result = {**base, "sourceOperationId": payload["operationId"], "alreadyComplete": True,
                          "sourceResult": source_record["result"]}
                return _director_complete(root, record, result)
            candidate = source_record.get("recoveryFile")
            if not candidate:
                raise ValueError("No official recovery file was recorded; inspect the current director instead of repeating the write")
            work_file = _director_trusted_file(root, candidate)
            if _director_sha(work_file) != source_record.get("recoverySha256"):
                raise ValueError("The official recovery file changed; automatic recovery is not permitted")
            record["workFile"] = str(work_file)
            args.append("--file=" + str(work_file))
        _flag(args, "project-id", payload["projectId"])
        if "commands" in payload:
            args.append("--input=" + str(commands_file))
        record["state"] = "attempting"
        _director_write_record(root, record)
        base["workspaceMutationAttempted"] = spec.mode == "director-cloud"
        base["cliInvoked"] = True
        upstream = _call(invoke, args)
        data = upstream.get("data", {})
        if not isinstance(data, dict):
            data = {}
        if isinstance(data.get("file"), str):
            try:
                recorded_file = _director_trusted_file(root, data["file"])
                if work_file is None or recorded_file == work_file.resolve():
                    record["workFile"] = str(recorded_file)
            except (ValueError, OSError):
                pass
        recovery_files = _director_find_recovery_files(upstream)
        if len(recovery_files) == 1:
            try:
                recovery_file = _director_trusted_file(root, next(iter(recovery_files)))
                record["recoveryFile"] = str(recovery_file)
                record["recoverySha256"] = _director_sha(recovery_file)
            except (ValueError, OSError):
                pass
        result = {**base, "upstream": _director_public(upstream)}
        if record.get("workFile") and Path(record["workFile"]).is_file():
            result["draftId"] = payload.get("draftId", operation_id)
        if not upstream["ok"]:
            state = "pending" if record.get("recoveryFile") else "failed"
            if state == "pending":
                result["recovery"]["nextAction"] = "director.resume"
            return _director_complete(root, record, result, state)
        if action == "director.fbx.export":
            try:
                artifact = _director_trusted_file(root, str(directory / "motion.fbx"))
                size = artifact.stat().st_size
                digest = _director_sha(artifact)
                source = data.get("source", {})
                valid = (data.get("format") == "fbx" and data.get("byteSize") == size and size > 0
                         and data.get("sha256") == digest and isinstance(data.get("path"), str)
                         and Path(data["path"]).resolve() == artifact
                         and isinstance(source, dict)
                         and source.get("projectId", payload["projectId"]) == payload["projectId"]
                         and ("nodeId" not in payload or source.get("nodeId") == payload["nodeId"]))
            except (ValueError, OSError):
                valid = False
            if not valid:
                result["error"] = {"code": "DIRECTOR_ARTIFACT_UNVERIFIED",
                    "message": "The FBX export receipt did not match a bounded managed file. No artifact is available for download."}
                return _director_complete(root, record, result, "saved-unverified")
            record.update({"artifactFile": str(artifact), "artifactSha256": digest, "artifactBytes": size})
            result["artifact"] = {"artifactId": operation_id, "projectId": payload["projectId"],
                "filename": "motion.fbx", "format": "fbx", "byteSize": size, "sha256": digest,
                "nextAction": "director.artifact.read", "encoding": "base64", "defaultChunkBytes": 16384}
        if spec.mode == "director-cloud":
            node = data.get("nodeId")
            version = data.get("versionId")
            saved = data.get("savedDraftVersion")
            valid = (isinstance(node, str) and re.fullmatch(NODE_PATTERN, node)
                     and isinstance(version, str) and bool(version)
                     and type(saved) is int and saved > 0
                     and data.get("projectId", payload["projectId"]) == payload["projectId"]
                     and ("nodeId" not in payload or node == payload["nodeId"]))
            record["state"] = "save-returned"
            record["result"] = result
            _director_write_record(root, record)
            if valid:
                verified = _call(invoke, ["director", "show", "--project-id=" + payload["projectId"], "--node-id=" + node])
                readback = verified.get("data", {})
                valid = (verified["ok"] and isinstance(readback, dict) and readback.get("nodeId") == node
                         and readback.get("versionId") == version
                         and readback.get("projectId", payload["projectId"]) == payload["projectId"])
                result["verification"] = _director_public(verified)
            result["workspaceSaveVerified"] = bool(valid)
            if not valid:
                result["error"] = {"code": "DIRECTOR_SAVE_UNVERIFIED",
                    "message": "A write was attempted, but the saved version could not be verified. Inspect the current director; do not repeat the mutation."}
                return _director_complete(root, record, result, "saved-unverified")
        if source_record is not None:
            source_record["state"] = "complete"
            source_record["resolvedByOperationId"] = operation_id
            source_record["result"] = result
            _director_write_record(root, source_record)
        return _director_complete(root, record, result)
    except ValueError as exc:
        result = {**base, "error": {"code": "DIRECTOR_MANAGED_INPUT_REJECTED", "message": str(exc)}}
        return _director_complete(root, record, result, "failed")
    except Exception:
        record["state"] = "unknown"
        record["updatedAt"] = time.time()
        _director_write_record(root, record)
        raise RuntimeError("Director operation outcome is uncertain; inspect its stable operationId before any recovery") from None


def _director_root(create=False):
    configured = os.environ.get("JIMENG_DIRECTOR_STATE_DIR")
    if not configured:
        parent = os.environ.get("DREAMINA_CANVAS_STATE_DIR")
        configured = str(Path(parent) / "bridge-director") if parent else None
    if not configured or not Path(configured).is_absolute():
        raise ValueError("Director state directory must be configured and absolute")
    path = Path(configured)
    if path.is_symlink():
        raise ValueError("Director state directory cannot be a symbolic link")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (not path.is_dir() or (os.name == "posix" and path.stat().st_mode & 0o077)):
        raise ValueError("Director state directory must be private")
    return path.resolve()


def _director_operation_dir(root, operation_id):
    if not isinstance(operation_id, str) or not re.fullmatch(UUID_PATTERN, operation_id):
        raise ValueError("Invalid director operation identity")
    path = root / operation_id
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Invalid director operation directory")
    return path


def _director_read_record(root, operation_id):
    path = _director_operation_dir(root, operation_id) / "operation.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Director operation was not found or is not a regular managed record")
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("operationId") != operation_id:
        raise ValueError("Director operation record is invalid")
    if record.get("profile") != os.environ.get("DREAMINA_CANVAS_PROFILE", "jimeng-mcp"):
        raise ValueError("Director operation belongs to another CLI profile")
    return record


def _director_write_json(path, value):
    if path.is_symlink():
        raise ValueError("Managed files must not be symbolic links")
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _director_write_record(root, record):
    record["updatedAt"] = time.time()
    _director_write_json(_director_operation_dir(root, record["operationId"]) / "operation.json", record)


def _director_begin(root, operation_id, payload):
    import fcntl
    directory = _director_operation_dir(root, operation_id)
    directory.mkdir(mode=0o700, exist_ok=True)
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if (directory / "operation.json").exists():
            record = _director_read_record(root, operation_id)
            if record.get("requestHash") != request_hash:
                raise ValueError("Director operation identity is already bound to another request")
            return record, False
        record = {"operationId": operation_id, "requestHash": request_hash, "action": payload["action"],
                  "projectId": payload["projectId"], "state": "prepared", "createdAt": time.time(),
                  "profile": os.environ.get("DREAMINA_CANVAS_PROFILE", "jimeng-mcp")}
        _director_write_record(root, record)
        return record, True


def _director_trusted_file(root, filename):
    path = Path(filename)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular server-managed director file")
    resolved = path.resolve()
    allowed_roots = [root]
    cli_root = os.environ.get("DREAMINA_CANVAS_STATE_DIR")
    if cli_root and Path(cli_root).is_absolute():
        allowed_roots.append(Path(cli_root).resolve())
    if not any(resolved.is_relative_to(allowed) for allowed in allowed_roots):
        raise ValueError("Director file is outside the configured managed directories")
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("Director file is not an eligible bounded regular file")
    return resolved


def _director_draft(root, draft_id, project_id):
    record = _director_read_record(root, draft_id)
    if record.get("projectId") != project_id or not record.get("workFile"):
        raise ValueError("Director draft identity or project does not match")
    return _director_trusted_file(root, record["workFile"])


def _director_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _director_find_recovery_files(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "document":
                continue
            if key == "operationRef" and isinstance(item, str):
                found.add(item)
            elif isinstance(item, (dict, list)):
                found.update(_director_find_recovery_files(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_director_find_recovery_files(item))
    return found


def _director_public(value):
    value = _redact(value)
    def scrub(item):
        if isinstance(item, dict):
            return {key: scrub(val) for key, val in item.items()
                    if not (key in ("file", "path", "operationRef", "directory") and isinstance(val, str))}
        if isinstance(item, list):
            return [scrub(val) for val in item]
        if isinstance(item, str):
            for key in ("JIMENG_DIRECTOR_STATE_DIR", "DREAMINA_CANVAS_STATE_DIR"):
                private_root = os.environ.get(key)
                if private_root:
                    item = item.replace(private_root, "[server-managed]")
        return item
    return scrub(value)


def _director_complete(root, record, result, state="complete"):
    record["state"] = state
    record["result"] = result
    _director_write_record(root, record)
    return result


def _director_record_view(record):
    return {"operationId": record["operationId"], "action": record["action"], "projectId": record["projectId"],
            "state": record["state"], "updatedAt": record["updatedAt"],
            "hasManagedDraft": bool(record.get("workFile")),
            "resumable": bool(record.get("recoveryFile")) and record["state"] in ("pending", "unknown"),
            "result": record.get("result"), "doNotResubmit": True}


def _director_read_artifact(root, payload, allowed_action="director.fbx.export"):
    record = _director_read_record(root, payload["artifactId"])
    if (record.get("projectId") != payload["projectId"] or record.get("action") != allowed_action
            or record.get("state") != "complete" or not record.get("artifactFile")):
        raise ValueError("No completed artifact matches this project, kind and identity")
    file = _director_trusted_file(root, record["artifactFile"])
    size = file.stat().st_size
    if size != record.get("artifactBytes") or _director_sha(file) != record.get("artifactSha256"):
        raise ValueError("The managed artifact changed after export; downloading it is not permitted")
    offset = payload.get("offset", 0)
    if offset > size:
        raise ValueError("Artifact offset exceeds the verified file size")
    with file.open("rb") as source:
        source.seek(offset)
        chunk = source.read(payload.get("length", 16384))
    end = offset + len(chunk)
    return {"artifactId": payload["artifactId"], "filename": record.get("artifactFilename", "motion.fbx"),
            "format": record.get("artifactFormat", "fbx"),
            "byteSize": size, "sha256": record["artifactSha256"], "offset": offset,
            "length": len(chunk), "nextOffset": end if end < size else None,
            "eof": end == size, "encoding": "base64", "data": base64.b64encode(chunk).decode("ascii")}


def artifact_descriptor(payload):
    """Private descriptor for the authenticated stream handler, never a tool result.

    The HTTP handler opens this file once with O_NOFOLLOW, verifies its size/SHA
    from that fd and streams from the same fd. Paths never cross the API.
    """
    if not isinstance(payload, dict) or set(payload) - {"projectId", "artifactId", "kind"}:
        raise ValueError("Invalid fixed artifact selector")
    for key in ("projectId", "artifactId"):
        if not isinstance(payload.get(key), str) or not re.fullmatch(UUID_PATTERN, payload[key]):
            raise ValueError("Artifact selectors require canonical UUIDs")
    kind = payload.get("kind", "resource")
    if kind not in ("resource", "director"):
        raise ValueError("Unsupported managed artifact kind")
    root = _resource_root() if kind == "resource" else _director_root()
    expected = "resource.download" if kind == "resource" else "director.fbx.export"
    record = _director_read_record(root, payload["artifactId"])
    if (record.get("projectId") != payload["projectId"] or record.get("action") != expected
            or record.get("state") != "complete" or not record.get("artifactFile")):
        raise ValueError("No completed artifact matches this exact project and kind")
    file = _director_trusted_file(root, record["artifactFile"])
    size, digest = record.get("artifactBytes"), record.get("artifactSha256")
    if type(size) is not int or not 0 < size <= 64 * 1024 * 1024 or file.stat().st_size != size:
        raise ValueError("Managed artifact size changed")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Managed artifact has no valid integrity record")
    return {"path": str(file), "byteSize": size, "sha256": digest,
            "filename": record.get("artifactFilename", "artifact.bin"), "format": record.get("artifactFormat", "bin")}


def _director_catalog_accepts(commands, catalog):
    if not isinstance(catalog, dict) or not isinstance(catalog.get("commandFields"), dict):
        return False
    for command in commands:
        name = command["type"]
        fields = catalog["commandFields"].get(name)
        if name not in catalog.get("commands", []) or not isinstance(fields, dict):
            return False
        if any(f.get("required") and key not in command for key, f in fields.items() if isinstance(f, dict)):
            return False
        for key, value in command.items():
            if key == "type":
                continue
            remote = fields.get(key)
            if not isinstance(remote, dict):
                return False
            expected = ("boolean" if type(value) is bool else "number" if type(value) in (int, float)
                        else "string" if isinstance(value, str) else "string[]" if isinstance(value, list)
                        else "object" if isinstance(value, dict) else None)
            if remote.get("type") != expected or ("values" in remote and value not in remote["values"]):
                return False
        if name == "object.primitive" and command["primitive"] not in catalog.get("primitives", []):
            return False
        if name == "object.preset" and command["preset"] not in {x.get("presetId") for x in catalog.get("presets", []) if isinstance(x, dict)}:
            return False
    return True


def _execute_exact_confirmation(payload, job_id, invoke):
    target = ["--project-id=" + payload["projectId"], "--node-id=" + payload["nodeId"]]
    quote = _call(invoke, ["node", "quote", *target])
    base = {"action": payload["action"], "dryRun": False, "cliInvoked": True,
            "generationSubmitted": False, "workspaceMutationAttempted": False,
            "localStateMutationAttempted": False, "creditApprovalAttempted": False}
    data = quote.get("data", {})
    items = data.get("items") if isinstance(data, dict) else None
    total = data.get("totalMaxCredits") if isinstance(data, dict) else None
    item = items[0] if isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict) else None
    if (not quote["ok"] or item is None or item.get("nodeId") != payload["nodeId"] or item.get("error")
            or type(total) is not int or total < 0 or type(item.get("maxCredits")) is not int
            or not 0 <= item["maxCredits"] <= total
            or ("confirmable" in data and data["confirmable"] is not True)):
        return {**base, "phase": "quote", "upstream": _redact(quote),
                "error": {"code": "INCOMPLETE_QUOTE", "message": "An authoritative complete quote is required before unlimited-mode exact confirmation."}}
    upstream = _call(invoke, _compile({**payload, "creditCeiling": total}, job_id))
    return {**base, "phase": "confirm", "creditApprovalAttempted": True,
            "effectiveCreditCeiling": total, "quote": _redact(quote), "upstream": _redact(upstream)}


def _decode_media(encoded):
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError("Media must be strict standard base64 without a data URL or whitespace") from None
    if not 0 < len(data) <= UPLOAD_CHUNK_BYTES:
        raise ValueError("Each decoded media chunk must be between one byte and 512 KiB")
    return data


def _resource_root(create=False):
    configured = os.environ.get("JIMENG_RESOURCE_STATE_DIR")
    if not configured:
        parent = os.environ.get("DREAMINA_CANVAS_STATE_DIR")
        if parent:
            configured = str(Path(parent) / "bridge-resources")
        elif os.environ.get("JIMENG_DIRECTOR_STATE_DIR"):
            configured = str(Path(os.environ["JIMENG_DIRECTOR_STATE_DIR"]).parent / "resources")
    if not configured or not Path(configured).is_absolute():
        raise ValueError("Configure a private absolute JIMENG_RESOURCE_STATE_DIR or DREAMINA_CANVAS_STATE_DIR")
    path = Path(configured)
    if path.is_symlink():
        raise ValueError("Resource state directory cannot be a symbolic link")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and (not path.is_dir() or (os.name == "posix" and path.stat().st_mode & 0o077)):
        raise ValueError("Resource state directory must be private")
    return path.resolve()


def _resource_public(value, root):
    value = _director_public(value)
    def scrub(item):
        if isinstance(item, dict):
            return {key: scrub(val) for key, val in item.items()}
        if isinstance(item, list):
            return [scrub(val) for val in item]
        return item.replace(str(root), "[server-managed]") if isinstance(item, str) else item
    return scrub(value)


def _resource_upload_id(payload, job_id):
    if "uploadId" in payload:
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
            "jimeng-bridge:media:" + payload["projectId"] + ":" + payload["uploadId"]))
    return _identities(job_id)[0]


def _resource_metadata(payload, byte_size):
    extension = payload["fileExtension"]
    return {"mediaType": payload["mediaType"], "fileExtension": extension,
            "name": payload.get("name", "media." + extension),
            "importKind": payload.get("importKind", "local_upload"),
            "sha256": payload["sha256"], "byteSize": byte_size}


def _write_binary_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _resource_image_format(file, fallback):
    with file.open("rb") as source:
        header = source.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    return fallback


def _resource_media_download_url(project_id, resource_id, expected, media_type):
    # Import lazily so the official CLI paths do not require a configured web
    # session. The session is read by its existing owned-0600-file validator.
    import web_actions
    if (media_type not in ("video", "audio") or not isinstance(expected, dict)
            or expected.get("resourceId") != resource_id or expected.get("type") != media_type
            or expected.get("status") != "success"):
        raise ValueError("The official resource identity, media type and success state must be verified first")
    wire_type, extension, hosts = ((2, "mp4", VIDEO_DOWNLOAD_HOSTS) if media_type == "video"
                                   else (3, "mp3", AUDIO_DOWNLOAD_HOSTS))
    try:
        session = web_actions._load_session()
    except web_actions.SessionError:
        raise ValueError("A configured valid private web session is required for media download") from None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": session["cookie"], "User-Agent": session["userAgent"], **session["headers"]}
    try:
        status, raw = web_actions._send(VIDEO_RESOURCE_PATH,
            {"project_id": project_id, "resource_ids": [resource_id]}, headers)
    except Exception:
        raise RuntimeError("The fixed authenticated media resource read failed") from None
    finally:
        session = None
        headers.clear()
    if not 200 <= status < 300 or len(raw) > web_actions.MAX_RESPONSE_BYTES:
        raise ValueError("The fixed media resource read was rejected or exceeded its response bound")
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("The fixed media resource read did not return valid JSON") from None
    if not isinstance(response, dict) or str(response.get("ret")) != "0" or not isinstance(response.get("data"), dict):
        raise ValueError("The authoritative media resource response was unsuccessful")
    data = response["data"]
    resources = data.get("resources")
    matches = [item for item in resources if isinstance(item, dict) and item.get("resource_id") == resource_id] if isinstance(resources, list) else []
    if len(matches) != 1 or len(resources) != 1:
        raise ValueError("The authoritative media response did not match the sole requested resource")
    resource = matches[0]
    content = resource.get(media_type)
    if type(resource.get("type")) is not int or resource["type"] != wire_type or resource.get("status") != 1000 or not isinstance(content, dict):
        raise ValueError("The authoritative resource is not successful media of the expected type")
    vid = content.get("vid")
    media_urls = data.get("media_urls")
    media = media_urls.get(vid) if isinstance(vid, str) and isinstance(media_urls, dict) else None
    if (not isinstance(vid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", vid)
            or not isinstance(media, dict) or media.get("original_format") != extension):
        raise ValueError("A supported format download mapping for the exact media ID is required")
    expected_content = expected.get(media_type, {})
    if not isinstance(expected_content, dict):
        raise ValueError("The official media facts must be an object")
    for key in ("width", "height"):
        if key in expected_content and content.get(key) != expected_content[key]:
            raise ValueError("Official CLI and web resource dimensions did not match")
    url = media.get("download_url")
    if (not isinstance(url, str) or not 1 <= len(url) <= 16384
            or any(ord(char) < 33 or ord(char) > 126 for char in url) or "\\" in url):
        raise ValueError("The official media download URL has an invalid format")
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in hosts
                 and parsed.port in (None, 443) and parsed.username is None and parsed.password is None
                 and not parsed.fragment and bool(parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("The official download URL is outside the verified HTTPS CDN allowlist")
    facts = {"resourceId": resource_id, "projectId": project_id, media_type + "Id": vid,
             "downloadHost": parsed.hostname, "format": extension,
             "resourceDurationSeconds": content.get("duration")}
    if media_type == "video":
        facts.update(resourceWidth=content.get("width"), resourceHeight=content.get("height"))
    return url, facts


def _download_media_resource(project_id, resource_id, file, expected, media_type):
    import web_actions
    url, facts = _resource_media_download_url(project_id, resource_id, expected, media_type)
    mime_types = (("video/mp4", "application/mp4", "application/octet-stream") if media_type == "video"
                  else ("audio/mpeg", "audio/mp3", "application/octet-stream"))
    # This is deliberately a new opener with no CookieJar, ambient proxy,
    # origin headers, authorization or copied browser headers.
    request = urllib.request.Request(url, headers={"Accept": ",".join(mime_types),
                                                   "User-Agent": "jimeng-bridge/1.0"}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), web_actions._NoRedirect())
    created = False
    try:
        try:
            response = opener.open(request, timeout=30)
        except Exception:
            raise RuntimeError("The credential-free allowlisted CDN download failed; redirects are not followed") from None
        with response:
            if response.code != 200:
                raise ValueError("The media CDN did not return a complete successful response")
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if encoding != "identity" or content_type not in mime_types:
                raise ValueError("The CDN response does not have the expected unencoded media type")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and (not re.fullmatch(r"[0-9]+", content_length)
                                               or not 0 < int(content_length) <= UPLOAD_MAX_BYTES):
                raise ValueError("The CDN content length is invalid or exceeds the managed file limit")
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            created = True
            digest, size, header = hashlib.sha256(), 0, bytearray()
            with os.fdopen(fd, "wb") as output:
                while True:
                    chunk = response.read(min(65536, UPLOAD_MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > UPLOAD_MAX_BYTES:
                        raise ValueError("The media download exceeded the managed file limit")
                    if len(header) < 64:
                        header.extend(chunk[:64 - len(header)])
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if content_length is not None and size != int(content_length):
            raise ValueError("The CDN download length did not match its complete response")
        if media_type == "video":
            brands = {b"isom", b"iso2", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V "}
            if (len(header) < 16 or header[4:8] != b"ftyp" or bytes(header[8:12]) not in brands
                    or not 16 <= int.from_bytes(header[:4], "big") <= size):
                raise ValueError("The downloaded file did not have a supported MP4 ftyp header")
        elif not (len(header) >= 10 and header[:3] == b"ID3" and header[3] in (2, 3, 4)
                  and all(value < 128 for value in header[6:10])
                  or len(header) >= 4 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
                  and header[1] & 0x18 != 0x08 and header[1] & 0x06 == 0x02
                  and header[2] >> 4 not in (0, 15) and header[2] & 0x0C != 0x0C):
            raise ValueError("The downloaded audio did not have a supported MP3 header")
        return {"ok": True, "data": {**facts, "path": str(file), "size": size,
                "sha256": digest.hexdigest(), "transport": "credential-free-allowlisted-https",
                "redirectsFollowed": False, "mediaHeaderVerified": True}}
    except Exception:
        if created:
            file.unlink(missing_ok=True)
        raise
    finally:
        # Signed URLs remain inside this stack frame, never in operation state,
        # returned results, diagnostics, or managed artifact metadata.
        url = None
        request = None


def _resource_upload_record(root, upload_id, project_id):
    record = _director_read_record(root, upload_id)
    if (record.get("projectId") != project_id or record.get("action") != "resource.upload.stage"
            or not isinstance(record.get("uploadMetadata"), dict) or not record.get("uploadFile")):
        raise ValueError("The managed upload does not match this project and identity")
    return record


def _resource_upload_view(root, record):
    file = _director_trusted_file(root, record["uploadFile"])
    metadata = record["uploadMetadata"]
    size = file.stat().st_size
    complete = size == metadata["byteSize"]
    ready = complete and _director_sha(file) == metadata["sha256"]
    return {"uploadId": record["operationId"], "projectId": record["projectId"],
            **metadata, "receivedBytes": size, "nextOffset": size if not complete else None,
            "ready": ready, "complete": complete, "nextAction": "resource.upload" if ready else "resource.upload.stage"}


def _execute_resource_stage(payload, job_id, root, base):
    import fcntl
    upload_id = payload.get("uploadId", _identities(job_id)[0])
    metadata = _resource_metadata(payload, payload["byteSize"])
    if "uploadId" not in payload:
        record, _ = _director_begin(root, upload_id, payload)
    else:
        record = _resource_upload_record(root, upload_id, payload["projectId"])
    directory = _director_operation_dir(root, upload_id)
    fd = os.open(directory / ".upload.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        record = _director_read_record(root, upload_id)
        if "uploadMetadata" not in record:
            file = directory / ("upload." + metadata["fileExtension"])
            if not file.exists():
                _write_binary_new(file, b"")
            record.update({"uploadMetadata": metadata, "uploadFile": str(file), "state": "staging"})
            _director_write_record(root, record)
        if record["uploadMetadata"] != metadata:
            raise ValueError("Upload metadata changed between chunks; use the original project, size, SHA-256 and media settings")
        file = _director_trusted_file(root, record["uploadFile"])
        data = _decode_media(payload["contentBase64"])
        size = file.stat().st_size
        offset = payload["offset"]
        replayed = offset < size
        if offset == size:
            with file.open("ab") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        elif offset + len(data) <= size:
            with file.open("rb") as source:
                source.seek(offset)
                if source.read(len(data)) != data:
                    raise ValueError("A different chunk already occupies this offset; existing bytes were preserved")
        else:
            raise ValueError("Chunks must append at the next offset or exactly replay existing bytes")
        view = _resource_upload_view(root, record)
        result = {**base, "operationId": upload_id, "upload": view,
                  "localStateMutationAttempted": True, "replayedChunk": replayed}
        if view["complete"] and not view["ready"]:
            result["error"] = {"code": "UPLOAD_HASH_MISMATCH",
                "message": "All declared bytes arrived but their SHA-256 did not match. No upload to the account was attempted."}
        record.update({"state": "ready" if view["ready"] else "hash-mismatch" if view["complete"] else "staging",
                       "result": result})
        _director_write_record(root, record)
        return result


def _execute_resource(payload, job_id, invoke):
    action = payload["action"]
    spec = ACTIONS[action]
    base = {"action": action, "dryRun": payload.get("dryRun", True) if spec.writes else False,
            "cliInvoked": False, "generationSubmitted": False, "workspaceMutationAttempted": False,
            "localStateMutationAttempted": False, "creditApprovalAttempted": False,
            "recovery": recovery(payload, job_id)}
    if base["dryRun"]:
        return {**base, "dryRunKind": "bridge-local-plan",
            "plan": {"command": list(spec.command), "managedFiles": True,
                     "maxChunkBytes": UPLOAD_CHUNK_BYTES, "maxFileBytes": UPLOAD_MAX_BYTES},
            "note": "Media bytes and request structure were validated locally. No file, upload, download or CLI call was made."}
    try:
        root = _resource_root(create=spec.writes)
    except (ValueError, OSError):
        return {**base, "available": False, "error": {"code": "RESOURCE_STORAGE_UNAVAILABLE",
            "message": "Configure a private resource state directory before transferring media. No CLI call was made."}}
    if action == "resource.operation.get":
        return {**base, "operation": _director_record_view(_director_read_record(root, payload["operationId"]))}
    if action == "resource.artifact.read":
        return {**base, "artifact": _director_read_artifact(root, payload, "resource.download")}
    if action == "resource.upload.inspect":
        record = _resource_upload_record(root, payload["uploadId"], payload["projectId"])
        return {**base, "upload": _resource_upload_view(root, record)}
    if action == "resource.upload.stage":
        return _execute_resource_stage(payload, job_id, root, base)

    operation_id = _identities(job_id)[0]
    record, fresh = _director_begin(root, operation_id, payload)
    if not fresh:
        if record.get("result") is not None and record["state"] in ("complete", "failed", "saved-unverified"):
            return {**record["result"], "replayed": True}
        return {**base, "operationId": operation_id,
                "error": {"code": "RESOURCE_RECONCILIATION_REQUIRED",
                          "message": "This media operation already started. Inspect its stable operation/resource identity before repeating it."}}
    directory = _director_operation_dir(root, operation_id)
    base.update({"operationId": operation_id, "localStateMutationAttempted": True})
    try:
        if action == "resource.upload":
            if "uploadId" in payload:
                source_record = _resource_upload_record(root, payload["uploadId"], payload["projectId"])
                view = _resource_upload_view(root, source_record)
                if not view["ready"]:
                    raise ValueError("The staged media is incomplete or failed its full SHA-256 verification")
                metadata = source_record["uploadMetadata"]
                file = _director_trusted_file(root, source_record["uploadFile"])
            else:
                content = _decode_media(payload["contentBase64"])
                metadata = _resource_metadata(payload, len(content))
                file = directory / ("upload." + metadata["fileExtension"])
                _write_binary_new(file, content)
            resource_id = _resource_upload_id(payload, job_id)
            record.update({"resourceId": resource_id, "sourceSha256": metadata["sha256"], "state": "attempting"})
            _director_write_record(root, record)
            args = ["resource", "upload", "--file=" + str(file), "--project-id=" + payload["projectId"],
                    "--resource-id=" + resource_id, "--type=" + metadata["mediaType"],
                    "--name=" + metadata["name"], "--import-kind=" + metadata["importKind"]]
            base.update({"cliInvoked": True, "workspaceMutationAttempted": True})
            upstream = _call(invoke, args)
            result = {**base, "resourceId": resource_id, "sha256": metadata["sha256"],
                      "byteSize": metadata["byteSize"], "upstream": _resource_public(upstream, root)}
            if not upstream["ok"]:
                return _director_complete(root, record, result, "failed")
            data = upstream.get("data", {})
            acknowledged = isinstance(data, dict) and data.get("resourceId") == resource_id and data.get("mediaType") == metadata["mediaType"]
            if acknowledged:
                readback = _call(invoke, ["resource", "get", resource_id, "--project-id=" + payload["projectId"]])
                result["verification"] = _resource_public(readback, root)
                resource = readback.get("data", {})
                verified = readback["ok"] and isinstance(resource, dict) and resource.get("resourceId") == resource_id and resource.get("type") == metadata["mediaType"]
                result["resourceVerified"] = bool(verified)
                result["resourceReady"] = bool(verified and resource.get("status") == "success")
            else:
                result["resourceVerified"] = False
                result["resourceReady"] = False
            if not result["resourceVerified"]:
                result["error"] = {"code": "RESOURCE_UPLOAD_UNVERIFIED",
                    "message": "Upload was attempted but its stable resource identity/type was not verified. Query resource.read with the recorded ID; do not create a new upload."}
                return _director_complete(root, record, result, "saved-unverified")
            return _director_complete(root, record, result)

        resource_id = payload["resourceId"]
        base["cliInvoked"] = True
        readback = _call(invoke, ["resource", "get", resource_id, "--project-id=" + payload["projectId"]])
        resource = readback.get("data", {})
        if (not readback["ok"] or not isinstance(resource, dict) or resource.get("resourceId") != resource_id
                or resource.get("type") not in ("image", "video", "audio") or resource.get("status") != "success"):
            result = {**base, "upstream": _resource_public(readback, root),
                "error": {"code": "RESOURCE_MEDIA_NOT_READY", "message": "Download requires the exact verified successful image, video or audio resource."}}
            return _director_complete(root, record, result, "failed")
        media_type = resource["type"]
        extension = resource.get("image", {}).get("format", "bin") if isinstance(resource.get("image"), dict) else "bin"
        extension = extension.lower() if isinstance(extension, str) else "bin"
        extension = ("mp4" if media_type == "video" else "mp3" if media_type == "audio"
                     else extension if extension in MEDIA_EXTENSIONS["image"] else "bin")
        file = directory / ("resource." + extension)
        record.update({"resourceId": resource_id, "state": "attempting"})
        _director_write_record(root, record)
        if media_type in ("video", "audio"):
            base["webReadAttempted"] = True
            upstream = _download_media_resource(payload["projectId"], resource_id, file, resource, media_type)
        else:
            upstream = _call(invoke, ["resource", "download", resource_id,
                "--project-id=" + payload["projectId"], "--output=" + str(file)])
        result = {**base, "resourceId": resource_id, "upstream": _resource_public(upstream, root)}
        if not upstream["ok"]:
            return _director_complete(root, record, result, "failed")
        data = upstream.get("data", {})
        try:
            artifact = _director_trusted_file(root, str(file))
            size = artifact.stat().st_size
            digest = _director_sha(artifact)
            valid = (isinstance(data, dict) and data.get("resourceId") == resource_id
                     and type(data.get("size")) is int and data["size"] == size and size > 0
                     and data.get("sha256") == digest and isinstance(data.get("path"), str)
                     and Path(data["path"]).resolve() == artifact)
        except (ValueError, OSError):
            valid = False
        if not valid:
            result["error"] = {"code": "RESOURCE_DOWNLOAD_UNVERIFIED",
                "message": "The download receipt did not match the bounded managed media. No artifact was exposed."}
            return _director_complete(root, record, result, "saved-unverified")
        if media_type == "image":
            extension = _resource_image_format(artifact, extension)
        filename = resource_id + "." + extension
        record.update({"artifactFile": str(artifact), "artifactSha256": digest, "artifactBytes": size,
                       "artifactFilename": filename, "artifactFormat": extension})
        result["artifact"] = {"artifactId": operation_id, "projectId": payload["projectId"],
            "resourceId": resource_id, "filename": filename, "format": extension, "byteSize": size,
            "sha256": digest, "nextAction": "resource.artifact.read", "encoding": "base64", "defaultChunkBytes": 16384}
        return _director_complete(root, record, result)
    except ValueError as exc:
        return _director_complete(root, record, {**base,
            "error": {"code": "RESOURCE_MANAGED_INPUT_REJECTED", "message": str(exc)}}, "failed")
    except Exception:
        record["state"] = "unknown"
        _director_write_record(root, record)
        raise RuntimeError("Media operation outcome is uncertain; inspect its stable operation/resource identity before retrying") from None


def execute(payload: dict[str, Any], job_id: str, *, invoke: Callable[[list[str]], dict[str, Any]],
            invoke_sensitive: Callable[[list[str]], dict[str, Any]] | None = None) -> dict[str, Any]:
    validate(payload)
    action = payload["action"]
    spec = ACTIONS[action]
    if action in RESOURCE_MANAGED_ACTIONS:
        return _execute_resource(payload, job_id, invoke)
    if action in DIRECTOR_MANAGED_ACTIONS:
        return _execute_director(payload, job_id, invoke)
    if spec.mode == "blocked":
        return {"action": action, "available": False, "cliInvoked": False,
                "generationSubmitted": False, "workspaceMutationAttempted": False,
                "localStateMutationAttempted": False, "creditApprovalAttempted": False,
                "error": {"code": spec.unavailable_code, "message": spec.unavailable_reason}}
    recovery_info = recovery(payload, job_id)
    dry_run = payload.get("dryRun", True) if spec.writes else False
    if dry_run and spec.mode in ("local-plan", "confirm", "generate"):
        return {"action": action, "dryRun": True, "dryRunKind": "bridge-local-plan",
                "cliInvoked": False, "generationSubmitted": False,
                "workspaceMutationAttempted": False, "localStateMutationAttempted": False,
                "creditApprovalAttempted": False, "recovery": recovery_info,
                "plan": {"command": list(spec.command), "mutatesWorkspace": not spec.local_state and spec.mode != "confirm",
                         "authorizesSpending": spec.mode in ("confirm", "generate"),
                         **({"steps": [["node", "show"], ["node", "quote"], ["node", "confirm"], ["node", "run"]],
                             "creditCeiling": payload.get("creditCeiling", 0)} if spec.mode == "generate" else {})},
                "note": "Local plan only. This official command has no dry-run flag; no upstream call, write, or approval was made."}
    if spec.mode == "generate":
        return _execute_generation(payload, job_id, invoke, invoke_sensitive)
    if spec.mode == "confirm" and "creditCeiling" not in payload and _credit_cap() == "unlimited":
        return _execute_exact_confirmation(payload, job_id, invoke)
    args = _compile(payload, job_id)
    upstream = _call(invoke, args)
    return {"action": action, "dryRun": dry_run,
            "dryRunKind": "official-local" if dry_run else None,
            "cliInvoked": True, "generationSubmitted": False if dry_run or not spec.billable else None,
            "workspaceMutationAttempted": not dry_run and spec.writes and not spec.local_state and spec.mode != "confirm",
            "localStateMutationAttempted": not dry_run and spec.local_state,
            "creditApprovalAttempted": not dry_run and spec.mode == "confirm",
            "recovery": recovery_info, "upstream": _redact(upstream),
            "note": "CLI completion is not media completion. Inspect upstream state and use operation.status/resource.read. A dry-run validates local arguments only."}
