"""One fixed operation registry for dispatch, schemas and effect metadata."""
from copy import deepcopy
from dataclasses import dataclass
import importlib
import importlib.util

import input_contracts
import official_actions
import web_actions
import canvas_web_actions
import subject_actions
import personal_actions


@dataclass(frozen=True)
class OperationSpec:
    name: str
    provider: str
    schema: dict
    effects: dict


def providers():
    result = {"official": official_actions, "web": web_actions,
              "canvas_web": canvas_web_actions, "subject": subject_actions, "personal": personal_actions}
    if importlib.util.find_spec("plugin_actions") is not None:
        result["plugin"] = importlib.import_module("plugin_actions")
    return result


def _effects(provider, name):
    effects = {"writes": False, "billable": False, "requiresUnlimited": False,
               "videoGeneration": False, "workflowStage": None}
    if provider == "official":
        spec = official_actions.ACTIONS[name]
        effects.update(writes=spec.writes, billable=spec.billable)
        if name in ("video.generate", "video.regenerate", "node.run"):
            effects.update(videoGeneration=True, workflowStage="ordinary")
    elif provider == "web":
        spec = web_actions.ACTIONS[name]
        effects.update(writes=spec.get("write", False), billable=spec.get("generationUnknown", False),
                       requiresUnlimited=spec.get("generationUnknown", False))
    elif provider == "canvas_web" and name == "video.extend":
        effects.update(writes=True, billable=True, requiresUnlimited=True, videoGeneration=True, workflowStage="extend")
    elif provider == "personal":
        effects["writes"] = True
    elif provider == "subject":
        effects["writes"] = name == "subject.import_element"
    elif provider == "plugin":
        effects["writes"] = name in ("plugin.session.create", "plugin.session.rename", "plugin.session.delete", "plugin.video.multiframe")
        if name == "plugin.video.multiframe":
            effects.update(billable=True, requiresUnlimited=True, videoGeneration=True, workflowStage="ordinary")
    elif provider == "baseline" and name.startswith("video."):
        effects.update(writes=True, billable=True, videoGeneration=True,
                       workflowStage="sample" if name == "video.sample" else "promote")
    return effects


def public_schema(schema):
    if isinstance(schema, bool):
        return schema
    clean = {k: deepcopy(v) for k, v in schema.items() if k != "allowText"}
    if "properties" in clean:
        clean["properties"] = {k: public_schema(v) for k, v in clean["properties"].items()}
    for key in ("items", "not", "additionalProperties"):
        if key in clean and isinstance(clean[key], (dict, bool)):
            clean[key] = public_schema(clean[key])
    for key in ("oneOf", "anyOf", "allOf"):
        if key in clean:
            clean[key] = [public_schema(v) for v in clean[key]]
    return clean


def operations():
    entries = {}
    groups = {"baseline": input_contracts.schemas()}
    for name, module in providers().items():
        schemas = module.request_schemas()
        if isinstance(schemas, list):
            schemas = {s["properties"]["action"]["const"]: s for s in schemas}
        groups[name] = schemas
    for provider, schemas in groups.items():
        for name, raw in schemas.items():
            if provider == "official" and official_actions.ACTIONS[name].mode == "blocked":
                continue
            if name in entries:
                raise RuntimeError("Duplicate fixed action registration")
            effects = _effects(provider, name)
            schema = public_schema(raw)
            if effects["videoGeneration"]:
                import project_policy
                schema.setdefault("properties", {})["workflow"] = project_policy.public_workflow_schema()
            entries[name] = OperationSpec(name, provider, schema, effects)
    return entries


def resolve(action):
    try:
        return operations()[action]
    except (KeyError, TypeError):
        raise input_contracts.ValidationError("action", "UNSUPPORTED_ACTION", "Choose an action from the current capability registry.") from None


def request_schemas():
    return {name: spec.schema for name, spec in operations().items()}


def effects_for(payload):
    effects = deepcopy(resolve(payload.get("action")).effects)
    if payload.get("action") == "node.run" and payload.get("nodeType") != "video":
        effects.update(videoGeneration=False, workflowStage=None)
    return effects


def provider_for(payload):
    name = resolve(payload.get("action")).provider
    return None if name == "baseline" else providers()[name]
