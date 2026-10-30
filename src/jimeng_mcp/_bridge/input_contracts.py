"""Shared input schema and ordered typed references for baseline operations."""
import re
import uuid
from copy import deepcopy

import official_actions as official


class ValidationError(ValueError):
    """Only these static, field-level details may cross the HTTP boundary."""
    def __init__(self, field, code, message):
        super().__init__(message)
        self.details = [{"field": field, "code": code, "message": message}]


def references(payload):
    explicit = payload.get("refs", [])
    if not isinstance(explicit, list):
        raise ValidationError("refs", "INVALID_TYPE", "References must be an array.")
    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        raise ValidationError("prompt", "INVALID_TYPE", "Prompt must be a string.")
    # Preserve the official order: explicit refs followed by prompt-only refs.
    # Duplicates bind one slot, including duplicates across the two sources.
    tokens = re.findall(r"\{\{([^{}]*)\}\}", prompt)
    if any(not token.startswith(("node:", "res:")) for token in tokens):
        raise ValidationError("prompt", "INVALID_REFERENCE", "Typed placeholders require a node: or res: identity.")
    result = []
    for value in [*explicit, *tokens]:
        if not isinstance(value, str) or not re.fullmatch(r"(?:node:node_[0123456789abcdefghjkmnpqrstvwxyz]{10}|res:[0-9a-f-]{36})", value):
            raise ValidationError("refs", "INVALID_REFERENCE", "Use existing typed node or resource identities.")
        if value.startswith("res:"):
            try:
                valid = str(uuid.UUID(value[4:])) == value[4:]
            except ValueError:
                valid = False
            if not valid:
                raise ValidationError("refs", "INVALID_RESOURCE_ID", "Resource references require canonical lowercase UUIDs.")
        if value not in result:
            result.append(value)
    if len(result) > 10:
        raise ValidationError("refs", "TOO_MANY_REFERENCES", "The merged prompt and explicit references may contain at most ten unique items.")
    return result


def schemas():
    string = lambda n: {"type": "string", "minLength": 1, "maxLength": n, "pattern": r"[^-][\s\S]*"}
    common = {"projectId": official.UUID_FIELD, "dryRun": official.DRY_RUN,
              "creditCeiling": {"type": "integer", "minimum": 0, "maximum": 100000}}
    fields = {
        "model.list": {"type": {"type": "string", "enum": ["image", "video", "audio"], "default": "video"}},
        "model.inspect": {"model": string(512), "type": {"type": "string", "enum": ["image", "video", "audio"], "default": "video"}},
        "video.sample": {**common, "model": string(512), "prompt": string(12000), "title": string(256),
            "mode": {"type": "string", "enum": ["t2v", "first_last_frame", "m2v"], "default": "t2v"},
            "ratio": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]},
            "resolution": string(32), "duration": {"type": "number", "exclusiveMinimum": 0, "maximum": 60},
            "refs": {"type": "array", "items": official.REF_FIELD, "maxItems": 10}},
        "video.promote": {**common, "nodeId": official.NODE_FIELD, "resolution": string(32)},
    }
    required = {"model.list": [], "model.inspect": ["model"],
        "video.sample": ["projectId", "model", "prompt"], "video.promote": ["projectId", "nodeId", "resolution"]}
    return {name: {"type": "object", "title": name, "additionalProperties": False,
        "required": ["action", *required[name]], "properties": {"action": {"type": "string", "const": name}, **deepcopy(props)}}
        for name, props in fields.items()}


def validate(payload):
    action = payload.get("action") if isinstance(payload, dict) else None
    schema = schemas().get(action)
    if schema is None:
        raise ValidationError("action", "UNSUPPORTED_ACTION", "Choose an action from the current capability registry.")
    try:
        official._validate_schema(payload, schema)
    except ValueError:
        # Never echo an invalid prompt, credential-like value or arbitrary key.
        raise ValidationError("payload", "SCHEMA_MISMATCH", "Use action_schema to check required fields, types and bounds.") from None
    if action == "video.sample":
        refs = references(payload)
        if payload.get("mode", "t2v") == "first_last_frame" and len(refs) not in (1, 2):
            raise ValidationError("refs", "FRAME_REFERENCE_COUNT", "First/last-frame mode requires one or two unique ordered references across refs and prompt.")
