"""Fixed Jimeng studio operations; no browser/CLI credential extraction.

Only the observed JSON bodies and origin below are accepted. Live requests need
an operator-provisioned, owned 0600 session file in a private directory. No
session is created by this module. No signature algorithm is implemented.
Browser success does not establish that a copied/static signature will work.
All results come from this request; there is no cached-success fallback. Writes
default to a local dry run and live writes obey JIMENG_READ_ONLY (default 1).
New writes have SDK shape evidence, not completed live acceptance. Numeric enum
values are never invented. Material URLs and script text are legitimate outputs
and are preserved; account credentials and exact secret echoes are removed.

Private session format (never place real values in this source or a job):
  {"enabled": true, "cookie": "...", "userAgent": "...",
   "headers": {"uifid": "..."},
   "actionHeaders": {"subject.list": {"x-secsdk-web-signature": "..."}},
   "validUntil": 1790000000}
validUntil is optional operator-supplied Unix seconds, not inferred from a
private header. Signature headers are accepted only per action, never invented.
The default path is ~/.local/share/jimeng-mcp/web-session.json. A trusted operator
may set JIMENG_WEB_SESSION_FILE; HTTP payloads cannot choose a session or path.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import urllib.error
import urllib.request
import uuid


ORIGIN = "https://jimeng.jianying.com"
DEFAULT_SESSION = str(Path.home() / ".local" / "share" / "jimeng-mcp" / "web-session.json")
MAX_SESSION_BYTES = 65536
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_INT64 = 9223372036854775807
MAX_SAFE_INTEGER = 9007199254740991
COMMON_HEADERS = frozenset({"uifid", "appid", "entrance-from", "pf", "referer",
                            "origin", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"})
SIGNATURE_HEADERS = frozenset({"x-secsdk-sign-config", "x-secsdk-web-expire",
                               "x-secsdk-web-signature"})


def _int(low=0, high=MAX_SAFE_INTEGER, **extra):
    return {"type": "integer", "minimum": low, "maximum": high, **extra}


def _str(maximum=256, **extra):
    return {"type": "string", "minLength": 1, "maxLength": maximum, **extra}


INT64 = {"oneOf": [_int(), _str(19, pattern=r"^(?:0|[1-9][0-9]{0,18})$")]}


def _array(item, maximum=100):
    return {"type": "array", "minItems": 1, "maxItems": maximum, "items": item}


# Limits here are bridge limits, not claims about upstream quotas. The subject
# source/order and style status are intentionally limited to observed values.
ACTIONS = {
    "subject.list": {
        "path": "/mweb/v1/dreamina_subject/get",
        "fields": {"cursor": _int(default=0), "limit": _int(1, 100, default=20),
                   "subjectSource": {"type": "string", "enum": ["mine"], "default": "mine"},
                   "sortField": {"type": "integer", "const": 0, "default": 0},
                   "sortOrder": {"type": "integer", "const": 0, "default": 0}},
        "required": (), "browserReadVerified": False,
    },
    "public_character.search": {
        "path": "/api/biz/v1/novel/template/role-library/search",
        "fields": {"pageNumber": _int(1, 100000, default=1),
                   "pageSize": _int(1, 100, default=5)},
        "required": (), "browserReadVerified": False,
    },
    "public_character.read": {
        "path": "/api/biz/v1/novel/template/role-library/detail",
        "fields": {"characterId": _int(1)},
        "required": ("characterId",), "browserReadVerified": False,
    },
    "studio.style.list": {
        "path": "/api/web/v1/novel_style_list",
        "fields": {"status": {"type": "integer", "const": 1, "default": 1},
                   "page": _int(1, 100000, default=1), "pageSize": _int(1, 100, default=5)},
        "required": (), "browserReadVerified": False,
    },
    "asset.query": {
        "path": "/api/biz/v1/asset/query",
        "fields": {"assetIds": _array(_str()), "projectId": _str(), "client": _str()},
        "required": ("assetIds", "client"), "browserReadVerified": False,
    },
    "studio.project.list": {
        "path": "/api/biz/v1/agent/list_creative_project",
        "fields": {"limit": _int(1, 100, default=20), "cursor": INT64,
                   "projectIdList": _array(INT64), "statusList": _array(_int(-2147483648, 2147483647))},
        "required": (), "browserReadVerified": False,
    },
}


CONTRACT_DIR = Path(__file__).with_name("contracts")
SDK_TYPES = json.loads(CONTRACT_DIR.joinpath("studio-sdk-contracts.json").read_text())["types"]
SDK_TYPES.update(json.loads(CONTRACT_DIR.joinpath("studio-gap-sdk-contracts.json").read_text())["types"])
BASELINE_READ_ACTIONS = frozenset(ACTIONS)


def _sdk_schema(descriptor, stack=()):
    """Convert the downloaded SDK's literal field descriptors, not guessed JSON."""
    if isinstance(descriptor, str):
        if descriptor == "string":
            return {"type": "string", "maxLength": 65536, "allowText": True}
        if descriptor == "number":
            return {"type": "number", "minimum": -MAX_SAFE_INTEGER, "maximum": MAX_SAFE_INTEGER}
        if descriptor == "boolean":
            return {"type": "boolean"}
        raise RuntimeError("Unsupported SDK primitive")
    if "ref" in descriptor:
        name = descriptor["ref"]
        if name.endswith(".Int64"):
            return {**deepcopy(INT64), "format": "int64"}
        # SDK role trees and video materials are recursive. Expose a deliberate
        # bounded subset: two occurrences of a referenced type, max depth 16.
        # A false JSON Schema rejects deeper values instead of accepting an
        # arbitrary object or silently dropping fields.
        if stack.count(name) >= 2 or len(stack) >= 16:
            return False
        schema = _sdk_schema(SDK_TYPES[name], (*stack, name))
        # SDK enum aliases expose number, not symbolic values. They may only be
        # passed as explicit integers; no default or numeric meaning is inferred.
        if SDK_TYPES[name] == "number":
            schema = _int(-2147483648, 2147483647)
            schema["description"] = "SDK numeric alias; value semantics are not inferred by this adapter"
        return schema
    if "union" in descriptor:
        return {"oneOf": [_sdk_schema(x, stack) for x in descriptor["union"]]}
    if "array" in descriptor:
        return {"type": "array", "maxItems": 256, "items": _sdk_schema(descriptor["array"], stack)}
    if "map" in descriptor:
        return {"type": "object", "maxProperties": 256,
                "additionalProperties": _sdk_schema(descriptor["map"], stack)}
    if "fields" in descriptor:
        return {"type": "object", "additionalProperties": False,
                "properties": {wire: _sdk_schema(kind, stack) for wire, unused, kind, required in descriptor["fields"]},
                "required": [wire for wire, unused, kind, required in descriptor["fields"] if required],
                "maxProperties": len(descriptor["fields"])}
    raise RuntimeError("Unsupported SDK type shape")


def _sdk_type(name):
    return _sdk_schema({"ref": name})


def _register(name, path, fields, required=(), *, mapping=None, response="object", write=False,
              sdk_type=None, notes=None, generation_unknown=False, base_client=False):
    fields = dict(fields)
    if write:
        fields["dryRun"] = {"type": "boolean", "default": True}
    ACTIONS[name] = {"path": path, "fields": fields, "required": tuple(required),
                     "mapping": mapping or {}, "response": response, "write": write,
                     "sdkType": sdk_type, "notes": notes, "generationUnknown": generation_unknown,
                     "baseClient": base_client,
                     "browserReadVerified": False}


SEARCH_FIELDS = SDK_TYPES["pippit_business_api_platform.SearchNovelCharacterLibraryRequest"]["fields"]
SEARCH_MAPPING = {camel: wire for wire, camel, kind, required in SEARCH_FIELDS if wire not in ("page_num", "page_size")}
for wire, camel, kind, required in SEARCH_FIELDS:
    if wire not in ("page_num", "page_size"):
        ACTIONS["public_character.search"]["fields"][camel] = _str(512)

_register("subject.get", "/mweb/v1/dreamina_subject/get",
          {"subjectId": _str(64, pattern=r"^[0-9]+$"), "pageSize": _int(1, 100, default=100),
           "maxPages": _int(1, 100, default=20)}, ("subjectId",), response="subject_lookup",
          notes="Paginated list filtering by the actual subject_id; not an invented getById endpoint")
_register("asset.artifact.query", "/api/biz/v1/asset/query_artifact",
          {"artifactIds": _array(_str()), "threadId": _str(), "runId": _str(), "client": _str()},
          ("client",), mapping={"artifactIds": "artifact_id", "threadId": "thread_id", "runId": "run_id"},
          response="array", sdk_type="pippit_business_api_asset.QueryArtifactRequest")
_register("studio.canvas.list", "/api/biz/v1/asset/canvas_list",
          {"pageNumber": _int(1, 100000, default=1), "pageSize": _int(1, 100, default=20), "client": _str()},
          ("client",), response="asset_list", sdk_type="pippit_business_api_asset.CanvasListRequest")
_register("studio.thread.read", "/api/biz/v1/agent/get_thread",
          {"threadId": _str(), "scopes": _array(_str(128), 20), "runId": _str(), "limit": _int(1, 100),
           "needFailureDetails": {"type": "boolean"}, "endRunId": _str(), "messageId": _str(),
           "endMessageId": _str(), "teamId": _str()}, ("threadId",),
          mapping={"threadId": "thread_id", "scopes": "scopes", "runId": "run_id", "limit": "limit",
                   "needFailureDetails": "is_need_fail_reason_detail", "endRunId": "end_run_id",
                   "messageId": "message_id", "endMessageId": "end_message_id", "teamId": "team_id"},
          sdk_type="pippit_business_api_agent.GetThreadRequest")
for name, target in (("studio.script.read", "scriptAssetId"), ("studio.storyboard.read", "storyboardAssetId")):
    _register(name, "/api/biz/v1/asset/query", {target: _str(), "client": _str(), "projectId": _str()},
              (target, "client"), response="asset_list",
              notes="Reads the specified real asset via asset.query; TextInfo.content is preserved verbatim except secret echoes")
_register("studio.script.resolve", "/api/web/v1/agent/novel/update_script_content",
          {"overviewAssetId": _str(), "threadId": _str()}, ("overviewAssetId",),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "threadId": "thread_id"},
          sdk_type="pippit_business_api_novel_agent.UpdateScriptContentRequest",
          notes="Always sends only_read=true; returns script asset identity, not the script content")

for name, path, type_name in (
    ("studio.project.create", "/api/biz/v1/agent/create_creative_project", "CreateCreativeProjectRequest"),
    ("studio.project.create_canvas", "/api/biz/v1/agent/create_creative_project_canvas", "CreateCreativeProjectCanvasRequest"),
):
    _register(name, path, {"title": _str(512), "projectType": _str(128)}, ("title",),
              mapping={"title": "title", "projectType": "project_type"}, response="project_create", write=True,
              sdk_type="pippit_business_api_agent." + type_name,
              notes="Creates a project/canvas shell only. A NOVEL canvas requires studio.project.initialize and subsequent readback before it is a usable short-drama workspace.")
_register("studio.project.update", "/api/biz/v1/agent/update_creative_project",
          {"projectId": INT64, "title": _str(512), "coverAssetId": _str(), "isCanvasUserOperated": {"type": "boolean"}},
          ("projectId",), mapping={"projectId": "project_id", "title": "title", "coverAssetId": "cover_asset_id",
                                  "isCanvasUserOperated": "is_canvas_user_operated"}, write=True,
          sdk_type="pippit_business_api_agent.UpdateCreativeProjectRequest",
          notes="Metadata fields only; unknown project status enums and deletion are not exposed")
_register("studio.project.initialize", "/api/biz/v1/agent/submit_run",
          {"projectId": INT64, "canvasAssetId": _str(64, pattern=r"^[0-9]+$"),
           "consumerUid": _str(64, pattern=r"^[0-9]+$"), "workspaceId": _str(64, pattern=r"^[0-9]+$"),
           "spaceId": _str(64, pattern=r"^[0-9]+$")},
          ("projectId", "canvasAssetId", "consumerUid", "workspaceId", "spaceId"),
          write=True, response="initialization_run", generation_unknown=True,
          notes="Observed empty NOVEL canvas initialization only. Fixed agent and biz/x_data_novel_canvas_independent_entry subtype; creates the missing conversation/overview. Deterministic job-derived identities aid inspection, but upstream idempotency is unverified; never retry uncertain submissions automatically.")
_register("studio.script.save", "/api/web/v1/agent/novel/update_ai_script_asset",
          {"overviewAssetId": _str(), "aiScriptAssetId": _str(),
           "patchOperations": _array(_sdk_type("pippit_business_api_novel_agent.UpdateAIScriptPatchOperation"), 100)},
          ("overviewAssetId", "patchOperations"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "aiScriptAssetId": "ai_script_asset_id", "patchOperations": "patch_operations"},
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.UpdateAIScriptAssetRequest",
          notes="Typed patch op/type strings must come from the actual product contract; no operation meanings are guessed")
_register("studio.script.characters.update", "/api/web/v1/agent/novel/novel_script_save",
          {"artifactId": _str(), "uid": _str(), "workspaceId": _str(),
           "modify": _sdk_type("pippit_business_api_novel_agent.NovelAgentScriptModify")}, ("artifactId", "modify"),
          mapping={"artifactId": "artifact_id", "uid": "uid", "workspaceId": "workspace_id", "modify": "modify"},
          write=True, sdk_type="pippit_business_api_novel_agent.NovelScriptSaveRequest",
          notes="This upstream modify object only contains character_list; it is not generic script text saving")
_register("studio.storyboard.create", "/api/web/v1/agent/novel/generate_new_story_board",
          {"overviewAssetId": _str(), "title": _str(512), "storyboardAssetId": _str()}, ("overviewAssetId", "title"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "title": "title", "storyboardAssetId": "storyboard_pippit_asset_id"},
          write=True, sdk_type="pippit_business_api_novel_agent.GenerateStoryBoardRequest", generation_unknown=True)
_register("studio.storyboard.save", "/api/web/v1/agent/video/storyboard_21_save",
          {"artifactId": _str(), "uid": _str(), "workspaceId": _str(),
           "storyboard": _sdk_type("storyboard21.StoryboardV21"), "saveMode": _int(-2147483648, 2147483647),
           "storyboardAssetId": _str(), "overviewAssetId": _str()}, ("storyboard",),
          mapping={"artifactId": "artifact_id", "uid": "uid", "workspaceId": "workspace_id", "storyboard": "storyboard",
                   "saveMode": "save_mode", "storyboardAssetId": "pippit_asset_id", "overviewAssetId": "overview_pippit_asset_id"},
          write=True, sdk_type="pippit_business_api_videoagent_v2.StoryBoardSave21Request",
          notes="V21 structure only. saveMode has no invented default; callers must use an observed value if needed")
_register("studio.storyboard.modify", "/api/web/v1/agent/video/storyboard_21_modify",
          {"artifactId": _str(), "uid": _str(), "workspaceId": _str(), "storyboardAssetId": _str(),
           "operations": _array(_sdk_type("capcut_business_video_workflow.StoryBoardModifyV2Operation"), 100)}, ("operations",),
          mapping={"artifactId": "artifact_id", "uid": "uid", "workspaceId": "workspace_id", "storyboardAssetId": "pippit_asset_id", "operations": "operations"},
          write=True, sdk_type="pippit_business_api_videoagent_v2.StoryBoard21ModifyRequest",
          notes="Typed operation structures are validated; OpType numeric meanings are not inferred")
_register("studio.storyboard.description.update", "/api/biz/v1/asset/batch_patch_asset",
          {"shotAssetId": _str(64, pattern=r"^[0-9]+$"), "canvasAssetId": _str(64, pattern=r"^[0-9]+$"),
           "expectedVersion": _int(1), "description": _str(65536, allowText=True)},
          ("shotAssetId", "canvasAssetId", "expectedVersion", "description"), write=True, response="patch_receipt",
          notes="Replaces only the existing storyboard-shot generation.prompt with the exact description string (including any duration markers). Reads and checks asset type/version first, preserves references and other fields, then verifies persisted text. Does not generate media or expose arbitrary JSON patch paths.")
_register("studio.episode.create", "/api/web/v1/agent/novel/generate_new_episode",
          {"overviewAssetId": _str(), "episodeName": _str(512), "episodeTitle": _str(512)}, ("overviewAssetId", "episodeName"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "episodeName": "episode_name", "episodeTitle": "episode_title"},
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.GenerateEpisodeRequest", generation_unknown=True)
_register("studio.episode.update", "/api/web/v1/agent/novel/update_ai_script_asset",
          {"overviewAssetId": _str(), "episodeAssetId": _str(), "episodeNum": _int(1, 100000),
           "aiScriptAssetId": _str(), "expectedVersion": _int(1), "content": _str(65536, minLength=0, allowText=True)},
          ("overviewAssetId", "episodeAssetId", "episodeNum", "expectedVersion", "content"),
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.UpdateAIScriptAssetRequest",
          notes="Exact observed updateEpisodeContent operation: updates raw_content only, preserving the episode title. Checks episode membership and asset version before submission, then reads back the saved content. expectedVersion is a bridge preflight check, not upstream atomic concurrency control; no automatic retry after an uncertain write.")

_register("studio.episode.delete", "/api/web/v1/agent/novel/delete_episode",
          {"overviewAssetId": _str(), "episodeId": _str(128), "episodeAssetId": _str(),
           "expectedOverviewVersion": _int(1), "confirmPermanentDelete": {"type": "boolean"}},
          ("overviewAssetId", "episodeId", "episodeAssetId", "expectedOverviewVersion"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "episodeId": "episode_id"},
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.DeleteEpisodeRequest",
          notes="Exact SDK delete-episode endpoint. Live calls require confirmPermanentDelete=true after contemporaneous human confirmation because recovery is unverified. Checks overview version and exact episode-to-asset association first; reads back absence from the overview. Does not claim physical asset deletion, undo support, or atomic version enforcement.")
_register("studio.asset.share_get", "/api/biz/v1/asset/query_share_asset",
          {"artifactId": _str(), "client": _str()}, ("artifactId", "client"),
          mapping={"artifactId": "ArtifactID"}, response="share_image", base_client=True,
          sdk_type="pippit_business_api_asset.QueryPippitShareAssetInfoRequest",
          notes="Reads existing shared image previews by ArtifactID. This SDK returns ShareImage, not a generic asset or thread share. Does not create a share, change visibility, or bypass expiry.")

# Additional routes are exact downloaded-SDK methods, with a deliberately
# bounded request subset. Numeric alias meanings are not invented; no generic
# endpoint, arbitrary Base/header, JSON-patch path or public-sharing write exists.
for name, path in (("public_character.face_config", "/api/biz/v1/novel/template/face-design/config"),
                   ("public_character.expression_config", "/api/biz/v1/novel/template/expression-library/config")):
    _register(name, path, {}, response="template_config",
              notes="Exact no-argument SDK configuration read; config is returned as product-owned text")
_register("studio.asset.list", "/api/biz/v1/asset/list",
          {"category": _int(-2147483648, 2147483647), "assetTypes": _array(_int(-2147483648, 2147483647), 20),
           "tags": _array(_str(128), 50), "pageSize": _int(1, 100), "pageNumber": _int(1, 100000), "client": _str()},
          ("client",), mapping={"category": "category", "assetTypes": "asset_type", "tags": "tag",
                                "pageSize": "PageSize", "pageNumber": "PageNum"}, response="asset_list", base_client=True,
          sdk_type="pippit_business_api_asset.AssetListRequest")
_register("studio.character_entity.list", "/api/biz/v1/character/entity/list",
          {"name": _str(512), "isTest": {"type": "boolean"}, "createType": _int(-2147483648, 2147483647),
           "bizType": _str(128), "scope": _str(128), "pageSize": _int(1, 100), "cursor": _str(512, minLength=0), "client": _str()},
          ("client",), mapping={"name": "character_name", "isTest": "is_test", "createType": "create_type",
              "bizType": "biz_type", "scope": "scope", "pageSize": "page_size", "cursor": "cursor"},
          response="character_list", base_client=True, sdk_type="pippit_business_api_asset.CharacterEntityListRequest")
_register("studio.character_entity.read", "/api/biz/v1/character/entity/detail",
          {"entityId": INT64, "client": _str()}, ("entityId", "client"), mapping={"entityId": "EntityID"},
          base_client=True, sdk_type="pippit_business_api_asset.CharacterEntityDetailRequest")
for name, path, type_name in (("studio.character.read", "/api/biz/v1/character/detail", "CharacterDetailV2Request"),
                              ("studio.character.resolve", "/api/biz/v1/character/use/resolve", "CharacterUseResolveRequest")):
    _register(name, path, {"characterId": _str(64), "scene": _str(128), "client": _str()},
              ("characterId", "client"), mapping={"characterId": "character_id", "scene": "scene"},
              base_client=True, sdk_type="pippit_business_api_asset." + type_name,
              notes="This is the studio character V2 identity, not the personal subject_id. No conversion between the two is inferred.")
_register("studio.project.clone", "/api/biz/v1/agent/clone_novel_project",
          {"sourceProjectId": INT64, "sourceThreadId": _str()}, (),
          mapping={"sourceProjectId": "source_project_id", "sourceThreadId": "source_thread_id"}, write=True,
          sdk_type="pippit_business_api_agent.CloneNovelProjectRequest",
          notes="Clones within the current authorized scope; target_team_id is not exposed. Actual copy coverage needs readback.")
_register("studio.settings.update", "/api/web/v1/agent/novel/update_novel_settings",
          {"overviewAssetId": _str(), "settings": _sdk_type("pippit_business_tool_novel_common.NovelSettings")},
          ("overviewAssetId", "settings"), mapping={"overviewAssetId": "pippit_asset_id", "settings": "novel_settings"},
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.UpdateNovelSettingsRequest")
_register("studio.episode.videos.update", "/api/web/v1/agent/novel/update_episode_video_list",
          {"overviewAssetId": _str(), "expectedSourceVersion": INT64,
           "operations": _array(_sdk_type("pippit_business_api_novel_agent.EpisodeVideoListOperation"), 100)},
          ("overviewAssetId", "expectedSourceVersion", "operations"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "expectedSourceVersion": "expected_source_version", "operations": "operations"},
          write=True, sdk_type="pippit_business_api_novel_agent.UpdateEpisodeVideoListRequest",
          notes="Versioned typed episode-video-list operations only. Numeric op meanings are not guessed; this is not declared to implement every shot reorder/bind operation.")
_register("studio.role.audio.generate", "/api/web/v1/agent/novel/generate_role_audio",
          {"overviewAssetId": _str(), "roleAssetId": _str(), "uid": _str(), "workspaceId": _str(),
           "prompt": _str(65536, allowText=True), "text": _str(65536, allowText=True), "mode": _int(-2147483648, 2147483647)},
          ("overviewAssetId", "roleAssetId", "mode"), mapping={"overviewAssetId": "overview_pippit_asset_id",
              "roleAssetId": "role_pippit_asset_id", "uid": "uid", "workspaceId": "workspace_id", "prompt": "prompt", "text": "text", "mode": "mode"},
          write=True, generation_unknown=True, sdk_type="pippit_business_api_novel_agent.GenerateRoleAudioRequest",
          notes="Requires the actual role asset and explicit observed mode; no subject voice vid is treated as a CLI TTS voice ID.")
_register("studio.video.erase_subtitle", "/api/web/v1/agent/novel/erase_video_subtitle_for_novel_agent",
          {"assetId": _str(), "shotId": _str(), "uid": _str(), "workspaceId": _str()}, ("assetId", "shotId"),
          mapping={"assetId": "pippit_asset_id", "shotId": "shot_id", "uid": "uid", "workspaceId": "workspace_id"},
          write=True, generation_unknown=True, sdk_type="pippit_business_api_novel_agent.EraseVideoSubtitleForNovelAgentRequest",
          notes="Returns the actual task receipt; media processing completion requires a separate observed result.")
_register("studio.images.generate", "/api/web/v1/agent/novel/generate_images",
          {"overviewAssetId": _str(), "generateType": _int(-2147483648, 2147483647), "generateScope": _str(128),
           "assetId": _str(), "nodeId": _str(), "assetNamespace": _sdk_type("pippit_business_api_novel_agent.AssetNamespace"),
           "appearanceAssetId": _str(), "appearanceAssetIds": _array(_str(), 100)},
          ("overviewAssetId", "generateType", "generateScope"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "generateType": "generate_type", "generateScope": "generate_scope",
              "assetId": "pippit_asset_id", "nodeId": "node_id", "assetNamespace": "asset_namespace",
              "appearanceAssetId": "appearance_asset_id", "appearanceAssetIds": "appearance_pippit_asset_ids"},
          write=True, generation_unknown=True, sdk_type="pippit_business_api_novel_agent.ImagesGenerateRequest",
          notes="Exact image-generation SDK route, caller supplies observed type/scope. force_generate is not exposed; batch results and billing are unverified.")
_register("studio.script.snapshot.restore", "/api/web/v1/agent/novel/snapshot_restore",
          {"overviewAssetId": _str(), "version": INT64, "snapshotType": _str(128)}, ("overviewAssetId", "version", "snapshotType"),
          mapping={"overviewAssetId": "overview_pippit_asset_id", "version": "version", "snapshotType": "snapshot_type"},
          write=True, response="optional_object", sdk_type="pippit_business_api_novel_agent.NovelSnapshotRestoreRequest",
          notes="Explicit existing snapshot version and type only; restoration scope must be checked before live use. No latest/default version is inferred.")
_register("studio.project.overview.read", "/api/biz/v1/asset/namespace_query",
          {"projectId": INT64}, ("projectId",),
          notes="Observed fixed /workspace/projects/overview.json namespace lookup. Does not accept arbitrary file paths.")


def supports(action):
    return isinstance(action, str) and action in ACTIONS


def request_schemas():
    return [{"type": "object", "additionalProperties": False,
             "properties": {"action": {"type": "string", "const": name}, **deepcopy(spec["fields"])},
             "required": ["action", *spec["required"]]} for name, spec in ACTIONS.items()]


def _check(value, schema, label, depth=0):
    if schema is False:
        raise ValueError(label + " exceeds the supported SDK recursive depth")
    if depth > 24:
        raise ValueError("Request exceeds the nested structure limit")
    if "oneOf" in schema:
        for option in schema["oneOf"]:
            try:
                _check(value, option, label, depth + 1)
                if (schema.get("format") == "int64" or schema == INT64) and isinstance(value, str) and int(value) > MAX_INT64:
                    raise ValueError(label + " is outside Int64 range")
                return
            except ValueError:
                pass
        raise ValueError(label + " does not match the SDK union type")
    kind = schema["type"]
    if kind == "integer":
        if type(value) is not int or not schema.get("minimum", -MAX_SAFE_INTEGER) <= value <= schema.get("maximum", MAX_SAFE_INTEGER):
            raise ValueError(label + " is outside the integer bridge limit")
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 256):
            raise ValueError(label + " must be a bounded plain string")
        if any(ord(c) < 32 and not (schema.get("allowText") and c in "\n\r\t") for c in value):
            raise ValueError(label + " contains unsupported control characters")
        if ("://" in value and not schema.get("allowText")) or ("pattern" in schema and not re.fullmatch(schema["pattern"], value)):
            raise ValueError(label + " is not a valid identifier")
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 256):
            raise ValueError(label + " is outside the array bridge limit")
        for item in value:
            _check(item, schema["items"], label + " item", depth + 1)
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError(label + " must be boolean")
    elif kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value) or not schema.get("minimum", -MAX_SAFE_INTEGER) <= value <= schema.get("maximum", MAX_SAFE_INTEGER):
            raise ValueError(label + " must be a bounded finite number")
    elif kind == "object":
        if not isinstance(value, dict) or len(value) > schema.get("maxProperties", 256):
            raise ValueError(label + " must be a bounded SDK object")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", False)
        if not extra and set(value) - set(props):
            raise ValueError(label + " has unknown SDK fields")
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError(label + " is missing required SDK fields")
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 256 or any(ord(c) < 32 for c in key):
                raise ValueError("Invalid SDK object key")
            _check(child, props[key] if key in props else extra, label + " field", depth + 1)
    if "const" in schema and value != schema["const"]:
        raise ValueError(label + " only supports the observed value")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(label + " only supports observed values")


def validate(payload):
    if not isinstance(payload, dict) or not supports(payload.get("action")):
        raise ValueError("Unsupported fixed web read action")
    spec = ACTIONS[payload["action"]]
    if set(payload) - {"action"} - set(spec["fields"]):
        # Do not include untrusted field names, which could contain secrets.
        raise ValueError("Unknown fields for fixed web read action")
    if any(k not in payload for k in spec["required"]):
        raise ValueError("Missing required fixed web read field")
    for key, value in payload.items():
        if key != "action":
            _check(value, spec["fields"][key], key)
    try:
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds bridge size limit")
    except (TypeError, OverflowError, RecursionError):
        raise ValueError("Request is not bounded JSON") from None
    action = payload["action"]
    if action == "asset.artifact.query" and not any(k in payload for k in ("artifactIds", "threadId", "runId")):
        raise ValueError("An artifact, thread or run selector is required")
    if action == "studio.project.update" and not any(k in payload for k in ("title", "coverAssetId", "isCanvasUserOperated")):
        raise ValueError("At least one supported project metadata change is required")
    if action == "studio.project.clone" and not any(k in payload for k in ("sourceProjectId", "sourceThreadId")):
        raise ValueError("An existing source project or thread is required")
    if action == "studio.settings.update" and not payload["settings"]:
        raise ValueError("At least one typed novel setting is required")
    if action == "studio.role.audio.generate" and not any(k in payload for k in ("text", "prompt")):
        raise ValueError("Explicit role audio text or prompt is required")
    if action == "studio.episode.delete" and payload.get("dryRun", True) is False and payload.get("confirmPermanentDelete") is not True:
        raise ValueError("Live episode deletion requires explicit contemporaneous human confirmation and confirmPermanentDelete=true")
    if action == "studio.script.characters.update" and "character_list" not in payload["modify"]:
        raise ValueError("modify.character_list is required for this operation")
    if action in ("studio.storyboard.save", "studio.storyboard.modify"):
        if not any(k in payload for k in ("artifactId", "storyboardAssetId")):
            raise ValueError("An existing artifact or storyboard asset identity is required")
        if action.endswith(".save") and not payload["storyboard"]:
            raise ValueError("An empty storyboard is not a valid save request")
        if action.endswith(".modify"):
            for op in payload["operations"]:
                if "OpType" not in op or len(op) < 2:
                    raise ValueError("Each storyboard operation needs explicit OpType and typed parameters")
    import project_policy
    project_policy.enforce(payload, {"writes": spec.get("write", False),
        "billable": spec.get("generationUnknown", False), "requiresUnlimited": spec.get("generationUnknown", False),
        "videoGeneration": False, "workflowStage": None})


def read_only():
    return os.environ.get("JIMENG_READ_ONLY", "1") != "0"


def _initialization_ids(job_id):
    # A stable bridge job has one set of upstream identities. This is a recovery
    # aid, not a claim that submit_run will deduplicate repeated submissions.
    return {key: str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:studio-initialize:" + str(job_id) + ":" + key))
            for key in ("threadId", "runId", "requestId")}


def _body(payload, job_id=None):
    action = payload["action"]
    if action == "studio.episode.update":
        body = {"overview_pippit_asset_id": payload["overviewAssetId"], "patch_operations": [{
            "op": "update", "type": "raw_content", "episode_asset_id": payload["episodeAssetId"],
            "episode_num": payload["episodeNum"], "content": {"content": payload["content"]}}]}
        if "aiScriptAssetId" in payload:
            body["ai_script_asset_id"] = payload["aiScriptAssetId"]
        return body
    if action == "studio.project.overview.read":
        return {"file_name": "/workspace/projects/overview.json", "project_id": payload["projectId"], "Base": {"Client": "web"}}
    if action == "studio.project.initialize":
        if not job_id:
            raise ValueError("Initialization requires a stable bridge job identity")
        ids = _initialization_ids(job_id)
        compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return {
            "message": {"content": [{"type": "data", "sub_type": "biz/x_data_novel_canvas_independent_entry",
                "data": compact({"episode_count": 1, "is_narration_script": True,
                                 "enable_narration_design": True, "canvas_asset_id": payload["canvasAssetId"]})}],
                "role": "user", "thread_id": ids["threadId"], "run_id": ids["runId"],
                "message_id": "", "created_at": int(time.time() * 1000)},
            "agent_name": "pippit_novel_agent_cn_v2",
            "user_info": {"consumer_uid": payload["consumerUid"], "workspace_id": payload["workspaceId"],
                          "space_id": payload["spaceId"], "app_id": "513695"},
            "project_id": payload["projectId"], "entrance_from": "home_page_canvas",
            "run_extra": compact({"client_extra": {"tab_name": "vids", "entrance_from": "home_page_canvas",
                "edit_type": "novel", "canvas_type": "novel", "placement_name": "novel"},
                "babi_param": {"generate_id": ids["runId"], "section_id": ids["runId"], "scene_lv1": "ai_agent",
                "scene_lv2": "conversation", "tool_id": "novel_manual_canvas", "tab_name": "vids",
                "edit_type": "novel", "enter_from": "home_page_canvas"}}),
            "thread_extra": compact({"entrance_from": "novel_page_canvas", "novel_entrance": "canvas",
                "novel_agent_version": "v2", "novel_project_mode": "project", "project_id": payload["projectId"]}),
            "request_id": ids["requestId"],
        }
    if action == "studio.storyboard.description.update":
        if not job_id:
            raise ValueError("Storyboard description update requires a stable bridge job identity")
        ids = _patch_ids(job_id)
        return {"batch_id": ids["batchId"], "client_id": ids["clientId"],
                "transactions": [{"transaction_id": ids["transactionId"], "intent": "shot.updateScript",
                    "merge_key": "shot-script:" + payload["shotAssetId"], "attempt": 1, "enqueued_at": int(time.time() * 1000),
                    "patches": [{"asset_id": payload["shotAssetId"], "base_asset_version": payload["expectedVersion"],
                                 "op": "replace", "path": "/content/generation/prompt", "value": payload["description"]}]}],
                "root_pippit_asset_id": payload["canvasAssetId"]}
    if action == "subject.list":
        return {"cursor": payload.get("cursor", 0), "limit": payload.get("limit", 20),
                "subject_source": payload.get("subjectSource", "mine"),
                "sort_field": payload.get("sortField", 0), "sort_order": payload.get("sortOrder", 0)}
    if action == "public_character.search":
        return {"page_num": payload.get("pageNumber", 1), "page_size": payload.get("pageSize", 5),
                **{wire: payload[key] for key, wire in SEARCH_MAPPING.items() if key in payload}}
    if action == "public_character.read":
        return {"character_id": payload["characterId"]}
    if action == "studio.style.list":
        return {"status": payload.get("status", 1), "page": payload.get("page", 1),
                "page_size": payload.get("pageSize", 5)}
    if action == "asset.query":
        result = {"pippit_asset_ids": payload["assetIds"], "Base": {"Client": payload["client"]}}
        if "projectId" in payload:
            result["project_id"] = payload["projectId"]
        return result
    if action in ("studio.script.read", "studio.storyboard.read"):
        field = "scriptAssetId" if action == "studio.script.read" else "storyboardAssetId"
        result = {"pippit_asset_ids": [payload[field]], "Base": {"Client": payload["client"]}}
        if "projectId" in payload:
            result["project_id"] = payload["projectId"]
        return result
    if action == "studio.canvas.list":
        return {"PageNum": payload.get("pageNumber", 1), "PageSize": payload.get("pageSize", 20),
                "Base": {"Client": payload["client"]}}
    if "mapping" in ACTIONS[action]:
        result = {wire: payload[key] for key, wire in ACTIONS[action]["mapping"].items() if key in payload}
        if action == "asset.artifact.query" or ACTIONS[action].get("baseClient"):
            result["Base"] = {"Client": payload["client"]}
        if action == "studio.script.resolve":
            result["only_read"] = True
        return result
    result = {"limit": payload.get("limit", 20)}
    for key, upstream in (("cursor", "cursor"), ("projectIdList", "project_id_list"), ("statusList", "status_list")):
        if key in payload:
            result[upstream] = payload[key]
    return result


class SessionError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def _header_value(value):
    if not isinstance(value, str) or not value or len(value) > 32768 or any(ord(c) < 32 or ord(c) > 255 for c in value):
        raise SessionError("WEB_SESSION_INVALID", "Private session contains an invalid header")
    return value


def _headers(values, *, signatures=False):
    if not isinstance(values, dict):
        raise SessionError("WEB_SESSION_INVALID", "Private session headers must be an object")
    allowed = COMMON_HEADERS | (SIGNATURE_HEADERS if signatures else frozenset())
    result = {}
    for name, value in values.items():
        key = name.lower() if isinstance(name, str) else ""
        if key not in allowed or key in result:
            raise SessionError("WEB_SESSION_INVALID", "Private session contains an unapproved header")
        value = _header_value(value)
        if key == "origin" and value != ORIGIN:
            raise SessionError("WEB_SESSION_INVALID", "Private session origin does not match")
        if key == "referer" and not value.startswith(ORIGIN + "/"):
            raise SessionError("WEB_SESSION_INVALID", "Private session referer does not match")
        result[key] = value
    return result


def _load_session():
    filename = os.environ.get("JIMENG_WEB_SESSION_FILE", DEFAULT_SESSION)
    path = Path(filename)
    if not path.is_absolute():
        raise SessionError("WEB_SESSION_INVALID", "Private session path must be absolute")
    fd = None
    try:
        if path.parent.stat().st_mode & 0o077:
            raise SessionError("WEB_SESSION_PERMISSIONS", "Private session directory must not be accessible to other users")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise SessionError("WEB_SESSION_PERMISSIONS", "Private session must be an owned regular 0600 file")
        if info.st_size > MAX_SESSION_BYTES:
            raise SessionError("WEB_SESSION_INVALID", "Private session exceeds size limit")
        raw = os.read(fd, MAX_SESSION_BYTES + 1)
    except FileNotFoundError:
        raise SessionError("WEB_SESSION_NOT_CONFIGURED", "Authenticated web session has not been configured") from None
    except SessionError:
        raise
    except OSError:
        raise SessionError("WEB_SESSION_UNREADABLE", "Private session could not be securely opened") from None
    finally:
        if fd is not None:
            os.close(fd)
    try:
        session = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SessionError("WEB_SESSION_INVALID", "Private session is not valid JSON") from None
    allowed = {"enabled", "cookie", "userAgent", "headers", "actionHeaders", "validUntil"}
    if not isinstance(session, dict) or set(session) - allowed:
        raise SessionError("WEB_SESSION_INVALID", "Private session has unsupported fields")
    if session.get("enabled") is not True:
        raise SessionError("WEB_SESSION_DISABLED", "Authenticated web adapter is disabled")
    cookie = _header_value(session.get("cookie"))
    agent = _header_value(session.get("userAgent"))
    headers = _headers(session.get("headers", {}))
    per_action = session.get("actionHeaders", {})
    if not isinstance(per_action, dict) or any(not supports(a) for a in per_action):
        raise SessionError("WEB_SESSION_INVALID", "Private session has unsupported action headers")
    per_action = {a: _headers(h, signatures=True) for a, h in per_action.items()}
    until = session.get("validUntil")
    if until is not None:
        if type(until) not in (int, float) or not math.isfinite(until):
            raise SessionError("WEB_SESSION_INVALID", "Private session expiration is invalid")
        if until <= time.time():
            raise SessionError("WEB_SESSION_EXPIRED", "Private web session has expired")
    # Values are kept inside this call chain. Never return them via capabilities.
    return {"cookie": cookie, "userAgent": agent, "headers": headers, "actionHeaders": per_action}


def _verified_executions():
    import acceptance_registry
    grouped = {}
    for row in acceptance_registry.all_verified():
        if row["action"] in ACTIONS:
            grouped.setdefault(row["action"], []).append(row)
    return grouped


def capabilities():
    error = None
    try:
        _load_session()
        configured = True
    except SessionError as exc:
        configured = False
        error = exc.code
    verified = _verified_executions()
    return {"adapter": "fixed-jimeng-web", "readOnly": read_only(),
            "authConfigured": configured, "sessionStatus": error or "configured_not_execution_verified",
            "signatureImplementation": "none; endpoint may require supported dynamic signing",
            "readVerificationScope": "Historical per-action evidence is reported separately; this is not a fresh session validity check",
            "nestedSchemaLimits": {"recursiveTypeOccurrences": 2, "schemaTypeDepth": 16, "requestBytes": MAX_REQUEST_BYTES},
            "actions": {name: {"implemented": True, "available": configured,
                "liveExecutionAllowed": configured and not (spec.get("write") and read_only()) and not (spec.get("generationUnknown") and os.environ.get("JIMENG_MAX_CREDITS", "0").strip() != "unlimited"), "authConfigured": configured,
                "serverExecutionVerified": name in verified, "executionVerification": verified.get(name),
                "browserReadVerified": spec["browserReadVerified"],
                "readOnly": not spec.get("write", False), "billable": False if not spec.get("generationUnknown") else None,
                "mayConsumeCredits": spec.get("generationUnknown", False), "mutatesWorkspace": spec.get("write", False),
                "requiresWebSession": True, "sdkType": spec.get("sdkType"), "notes": spec.get("notes")}
                for name, spec in ACTIONS.items()}}


def recovery(payload, job_id):
    spec = ACTIONS.get(payload.get("action"), {})
    if spec.get("write") and not payload.get("dryRun", True):
        result = {"action": payload["action"], "jobId": job_id, "doNotResubmit": True,
                "upstreamIdempotencyVerified": False,
                "inspectBeforeRetry": "Read the target project/asset or original job result; no automatic retry is safe after an uncertain write"}
        if payload["action"] == "studio.project.initialize":
            ids = _initialization_ids(job_id)
            result.update(ids)
            result.update({"projectId": payload["projectId"], "canvasAssetId": payload["canvasAssetId"],
                           "query": {"action": "studio.thread.read", "threadId": ids["threadId"], "runId": ids["runId"]}})
        elif payload["action"] == "studio.storyboard.description.update":
            result.update(_patch_ids(job_id))
            result.update({"shotAssetId": payload["shotAssetId"], "expectedVersion": payload["expectedVersion"],
                           "query": {"action": "asset.query", "assetIds": [payload["shotAssetId"]], "client": "web"}})
        elif payload["action"] in ("studio.episode.update", "studio.episode.delete"):
            result.update({"overviewAssetId": payload["overviewAssetId"], "episodeAssetId": payload["episodeAssetId"],
                           "query": {"action": "asset.query", "assetIds": [payload["overviewAssetId"], payload["episodeAssetId"]], "client": "web"}})
        return result
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _send(path, body, headers):
    request = urllib.request.Request(ORIGIN + path, data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                                     headers=headers, method="POST")
    # No ambient proxy or browser cookie jar. Redirects cannot carry credentials.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=25)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            exc.close()
            return exc.code, b""
        response = exc
    with response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        return response.code, raw


def _private_values(session):
    # Short public header/cookie flags (for example "0" or "web") must not
    # corrupt unrelated object IDs, response codes or text by substring removal.
    values = [session["cookie"], session["userAgent"]]
    values.extend(v for v in session["headers"].values() if len(v) >= 8)
    values.extend(v for h in session["actionHeaders"].values() for v in h.values() if len(v) >= 8)
    # Redact an echoed cookie value even when the complete Cookie header differs.
    values.extend(part.partition("=")[2].strip() for part in session["cookie"].split(";")
                  if "=" in part and len(part.partition("=")[2].strip()) >= 8)
    return sorted(set(v for v in values if v), key=len, reverse=True)


def _redact(value, private_values, depth=0):
    if depth > 30:
        return "[REDACTED_DEPTH]"
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            safe_key = _redact(key, private_values, depth + 1)
            if re.search(r"token|cookie|secret|device.?code|authorization|signature|secsdk|headers|user.?agent|uifid", key, re.I):
                result[safe_key] = "[REDACTED]"
            else:
                result[safe_key] = _redact(child, private_values, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact(x, private_values, depth + 1) for x in value]
    if isinstance(value, str):
        # Authorized material URLs and JSON script strings are content, not the
        # account Cookie. Preserve them while removing exact credential echoes.
        for secret in private_values:
            value = value.replace(secret, "[REDACTED]")
    return value


def _valid_data(action, data):
    shape = ACTIONS[action].get("response")
    if shape == "optional_object":
        return data is None or isinstance(data, dict)
    if shape == "array":
        return isinstance(data, list) and all(isinstance(x, dict) for x in data)
    if not isinstance(data, dict):
        return False
    if shape == "object":
        return True
    if shape == "initialization_run":
        return (isinstance(data.get("run"), dict) and isinstance(data["run"].get("run_id"), str)
                and isinstance(data["run"].get("thread_id"), str) and type(data["run"].get("state")) is int)
    if shape == "patch_receipt":
        return isinstance(data.get("results"), list) and all(isinstance(item, dict) for item in data["results"])
    if shape == "template_config":
        return isinstance(data.get("config"), str)
    if shape == "share_image":
        variants = {field[0] for field in SDK_TYPES["capcut_business_common_asset.ShareImage"]["fields"]}
        resolutions = {field[0] for field in SDK_TYPES["capcut_business_common_asset.PreviewInfos"]["fields"]}
        found = False
        if set(data) - variants:
            return False
        for previews in data.values():
            if previews is None:
                continue
            if not isinstance(previews, dict) or set(previews) - resolutions:
                return False
            for preview in previews.values():
                if preview is None:
                    continue
                if not isinstance(preview, dict) or set(preview) - {"preview_url", "metadata"}:
                    return False
                url = preview.get("preview_url")
                if url is not None and (not isinstance(url, str) or not url.strip()):
                    return False
                metadata = preview.get("metadata")
                if metadata is not None:
                    if not isinstance(metadata, dict):
                        return False
                    try:
                        _check({key: value for key, value in metadata.items() if value is not None},
                               _sdk_type("capcut_business_common_asset.Metadata"), "preview metadata")
                    except ValueError:
                        return False
                found = found or isinstance(url, str) and bool(url.strip())
        return found
    if shape == "character_list":
        return data.get("Total") in (0, "0") if "Character" not in data else isinstance(data["Character"], list)
    if shape == "project_create":
        return type(data.get("project_id")) in (str, int) or isinstance(data.get("project_info"), dict)
    if shape == "asset_list":
        if "Assets" not in data:
            return data.get("Total") in (0, "0")
        return isinstance(data["Assets"], list) and all(isinstance(x, dict) for x in data["Assets"])
    list_field = {"subject.list": "data_list", "public_character.search": "characters",
                  "studio.style.list": "list", "asset.query": "Assets", "studio.project.list": "project_list"}.get(action)
    if list_field:
        return isinstance(data.get(list_field), list) and all(isinstance(x, dict) for x in data[list_field])
    return type(data.get("CharacterID")) is int and isinstance(data.get("CharacterName"), str)


def _failure(base, code, message, *, http_status=None, upstream_ret=None, upstream_message=None):
    error = {"code": code, "message": message}
    if upstream_ret is not None:
        error["upstreamRet"] = upstream_ret
    if upstream_message is not None:
        error["upstreamMessage"] = upstream_message
    return {**base, "httpStatus": http_status, "upstream": {"ok": False, "error": error}}


def _execute_once(action, body, base, session):
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": session["cookie"], "User-Agent": session["userAgent"],
               **session["headers"], **session["actionHeaders"].get(action, {})}
    try:
        status, raw = _send(ACTIONS[action]["path"], body, headers)
    except Exception:
        # Network exceptions may contain request material. Never serialize them.
        return _failure(base, "WEB_TRANSPORT_FAILED", "Live web request failed; no cached data was substituted; inspect original target before any write retry")
    if 300 <= status < 400:
        return _failure(base, "WEB_REDIRECT_BLOCKED", "Upstream redirect was blocked; no credentials were forwarded", http_status=status)
    if len(raw) > MAX_RESPONSE_BYTES:
        return _failure(base, "WEB_RESPONSE_TOO_LARGE", "Upstream response exceeded the bridge limit", http_status=status)
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return _failure(base, "WEB_INVALID_RESPONSE", "Upstream did not return a JSON envelope", http_status=status)
    if not isinstance(result, dict) or type(result.get("ret")) not in (str, int) or not isinstance(result.get("errmsg", ""), str):
        return _failure(base, "WEB_INVALID_RESPONSE", "Upstream response envelope changed", http_status=status)
    ret = str(result["ret"])
    message = result.get("errmsg", "")
    private = _private_values(session)
    safe_ret = _redact(ret, private)
    safe_message = _redact(message[:1000], private)
    if not 200 <= status < 300 or ret != "0":
        code = "WEB_UPSTREAM_ERROR"
        if ret == "1015" or re.search(r"login|log.in|登录|session.*expired", message, re.I):
            code = "WEB_LOGIN_REQUIRED"
        elif re.search(r"signature|secsdk|签名|invalid sign|sign.*(?:invalid|fail|expir)", message, re.I):
            code = "WEB_SIGNATURE_REJECTED"
        return _failure(base, code, "Upstream rejected the live read; no cached data was substituted",
                        http_status=status, upstream_ret=safe_ret, upstream_message=safe_message)
    data = result.get("data")
    if action in ("studio.script.save", "studio.episode.update"):
        # This SDK response returns its receipt fields at the envelope root.
        data = {key: result[key] for key in ("ai_script_asset_id", "updated_episode_asset_ids") if key in result} or data
    if not _valid_data(action, data):
        return _failure(base, "WEB_RESPONSE_CONTRACT_MISMATCH", "Successful envelope did not contain the expected data shape",
                        http_status=status, upstream_ret=safe_ret)
    if action == "studio.project.initialize" and any(data["run"][key] != body["message"][key] for key in ("run_id", "thread_id")):
        return _failure(base, "WEB_RESPONSE_CONTRACT_MISMATCH", "Initialization receipt did not match the original submitted identities",
                        http_status=status, upstream_ret=safe_ret)
    return {**base, "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "httpStatus": status, "upstream": {"ok": True, "ret": safe_ret,
            "data": _redact(data, private)},
            "mediaUrlsPreserved": True, "signatureImplementation": "none"}


def _subject_lookup(payload, base, session):
    cursor, seen = 0, set()
    for page in range(1, payload.get("maxPages", 20) + 1):
        body = {"cursor": cursor, "limit": payload.get("pageSize", 100), "subject_source": "mine", "sort_field": 0, "sort_order": 0}
        result = _execute_once("subject.list", body, base, session)
        if not result["upstream"]["ok"]:
            return result
        data = result["upstream"]["data"]
        for subject in data["data_list"]:
            if subject.get("subject_id") == payload["subjectId"]:
                result["upstream"]["data"] = {"subject": subject, "lookup": {"method": "paginated_subject_list", "pagesRead": page}}
                return result
        if data.get("has_more") is False:
            return _failure(base, "WEB_SUBJECT_NOT_FOUND", "Subject was not present in the completed current-account list")
        next_cursor = data.get("next_cursor")
        if type(next_cursor) is not int or next_cursor < 0 or next_cursor == cursor or next_cursor in seen or data.get("has_more") is not True:
            return _failure(base, "WEB_PAGINATION_INVALID", "Upstream subject pagination was invalid; lookup did not complete")
        seen.add(cursor)
        cursor = next_cursor
    return _failure(base, "WEB_LOOKUP_LIMIT", "Subject lookup reached its explicit page limit; absence is not established")


def _patch_ids(job_id):
    suffix = str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:shot-description:" + str(job_id)))
    return {"transactionId": "collaborative_tx_" + suffix, "batchId": "batch_collaborative_tx_" + suffix,
            "clientId": "client_" + str(uuid.uuid5(uuid.NAMESPACE_URL, "jimeng-bridge:shot-description-client"))}


def _episode_update(payload, job_id, base, session):
    query = {"pippit_asset_ids": [payload["overviewAssetId"], payload["episodeAssetId"]], "Base": {"Client": "web"}}
    before = _execute_once("asset.query", query, base, session)
    if not before["upstream"]["ok"]:
        return before
    assets = before["upstream"]["data"]["Assets"]
    overview = [a for a in assets if a.get("PippitAssetID") == payload["overviewAssetId"]]
    episode = [a for a in assets if a.get("PippitAssetID") == payload["episodeAssetId"]]
    if len(overview) != 1 or len(episode) != 1:
        return _failure(base, "WEB_ASSET_NOT_FOUND", "The exact overview and episode assets were not returned")
    if episode[0].get("version") != payload["expectedVersion"]:
        return _failure(base, "WEB_ASSET_VERSION_CONFLICT", "Current episode version differs; read the episode before deciding a new edit")
    try:
        overview_doc = json.loads(overview[0]["TextInfo"]["content"])
        episode_doc = json.loads(episode[0]["TextInfo"]["content"])
        episode_info = episode_doc["episode_info"]
        matches = [entry for entry in overview_doc["episode_assets"]
                   if entry.get("pippit_asset_id") == payload["episodeAssetId"]
                   and str(entry.get("episode_id")) == str(payload["episodeNum"])]
        valid = (len(matches) == 1 and str(episode_info["episode_id"]) == str(payload["episodeNum"])
                 and isinstance(episode_info["raw_content"], str))
        if "aiScriptAssetId" in payload:
            valid = valid and overview_doc.get("ai_script_asset_id") == payload["aiScriptAssetId"]
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        return _failure(base, "WEB_EPISODE_IDENTITY_MISMATCH", "Asset is not the specified raw-content episode in this overview")
    base = {**base, "workspaceMutationAttempted": True, "upstreamIdempotencyVerified": False,
            "doNotResubmitAfterUncertainOutcome": True, "recovery": recovery(payload, job_id),
            "versionCheckScope": "bridge_preflight_only_not_upstream_atomic"}
    receipt = _execute_once(payload["action"], _body(payload, job_id), base, session)
    if not receipt["upstream"]["ok"]:
        return receipt
    after = _execute_once("asset.query", {"pippit_asset_ids": [payload["episodeAssetId"]], "Base": {"Client": "web"}}, base, session)
    if not after["upstream"]["ok"]:
        return {**after, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False}
    current = [a for a in after["upstream"]["data"]["Assets"] if a.get("PippitAssetID") == payload["episodeAssetId"]]
    try:
        saved = json.loads(current[0]["TextInfo"]["content"])["episode_info"]
        verified = (len(current) == 1 and saved["raw_content"] == payload["content"]
                    and saved["episode_id"] == episode_info["episode_id"]
                    and saved.get("episode_title") == episode_info.get("episode_title")
                    and type(current[0].get("version")) is int and current[0]["version"] >= payload["expectedVersion"])
    except (KeyError, TypeError, ValueError, IndexError):
        verified = False
    if not verified:
        return _failure({**base, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False},
                        "WEB_READBACK_MISMATCH", "Episode update returned success but the exact saved content and identity were not verified; inspect target before retry")
    return {**receipt, "persistenceVerified": True, "verifiedAsset": current[0]}


def _episode_delete(payload, job_id, base, session):
    query = {"pippit_asset_ids": [payload["overviewAssetId"]], "Base": {"Client": "web"}}
    before = _execute_once("asset.query", query, base, session)
    if not before["upstream"]["ok"]:
        return before
    assets = [a for a in before["upstream"]["data"]["Assets"] if a.get("PippitAssetID") == payload["overviewAssetId"]]
    if len(assets) != 1:
        return _failure(base, "WEB_ASSET_NOT_FOUND", "The exact overview asset was not returned")
    if assets[0].get("version") != payload["expectedOverviewVersion"]:
        return _failure(base, "WEB_ASSET_VERSION_CONFLICT", "Current overview version differs; read the episode list before deciding deletion")
    try:
        entries = json.loads(assets[0]["TextInfo"]["content"])["episode_assets"]
        matches = [entry for entry in entries if entry.get("episode_id") == payload["episodeId"]]
        valid = isinstance(entries, list) and len(matches) == 1 and matches[0].get("pippit_asset_id") == payload["episodeAssetId"]
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        return _failure(base, "WEB_EPISODE_IDENTITY_MISMATCH", "The exact episode-to-asset association was not found in this overview")
    base = {**base, "workspaceMutationAttempted": True, "upstreamIdempotencyVerified": False,
            "doNotResubmitAfterUncertainOutcome": True, "recovery": recovery(payload, job_id),
            "versionCheckScope": "bridge_preflight_only_not_upstream_atomic"}
    receipt = _execute_once(payload["action"], _body(payload, job_id), base, session)
    if not receipt["upstream"]["ok"]:
        return receipt
    after = _execute_once("asset.query", query, base, session)
    if not after["upstream"]["ok"]:
        return {**after, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False}
    current = [a for a in after["upstream"]["data"]["Assets"] if a.get("PippitAssetID") == payload["overviewAssetId"]]
    try:
        entries = json.loads(current[0]["TextInfo"]["content"])["episode_assets"]
        verified = (len(current) == 1 and isinstance(entries, list)
                    and not any(entry.get("pippit_asset_id") == payload["episodeAssetId"] for entry in entries)
                    and type(current[0].get("version")) is int and current[0]["version"] > payload["expectedOverviewVersion"])
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        verified = False
    if not verified:
        return _failure({**base, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False},
                        "WEB_READBACK_MISMATCH", "Delete returned success but removal of this episode asset from the overview was not verified; inspect target before retry")
    return {**receipt, "persistenceVerified": True, "deletionScopeVerified": "removed_from_overview_only",
            "verifiedOverviewAsset": current[0], "recoverabilityVerified": False}


def _storyboard_description_update(payload, job_id, base, session):
    query = {"pippit_asset_ids": [payload["shotAssetId"]], "Base": {"Client": "web"}}
    before = _execute_once("asset.query", query, base, session)
    if not before["upstream"]["ok"]:
        return before
    assets = before["upstream"]["data"]["Assets"]
    match = [a for a in assets if a.get("PippitAssetID") == payload["shotAssetId"]]
    if len(match) != 1:
        return _failure(base, "WEB_ASSET_NOT_FOUND", "The exact storyboard shot asset was not returned")
    asset = match[0]
    if asset.get("version") != payload["expectedVersion"]:
        return _failure(base, "WEB_ASSET_VERSION_CONFLICT", "Current asset version differs; read the shot before deciding a new edit")
    try:
        doc = json.loads(asset["TextInfo"]["content"])
        valid = (doc["type"] == "biz/video" and doc["content"]["bizVariant"] == "storyboard-shot"
                 and isinstance(doc["content"]["generation"]["prompt"], str))
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        return _failure(base, "WEB_ASSET_TYPE_MISMATCH", "Target is not an existing storyboard shot with a generation prompt")
    base = {**base, "workspaceMutationAttempted": True, "upstreamIdempotencyVerified": False,
            "doNotResubmitAfterUncertainOutcome": True, "recovery": recovery(payload, job_id)}
    receipt = _execute_once(payload["action"], _body(payload, job_id), base, session)
    if not receipt["upstream"]["ok"]:
        return receipt
    expected_tx = _patch_ids(job_id)["transactionId"]
    results = receipt["upstream"]["data"]["results"]
    if (len(results) != 1 or results[0].get("transaction_id") != expected_tx
            or results[0].get("status") != "ack"
            or type(results[0].get("asset_versions", {}).get(payload["shotAssetId"])) is not int):
        return _failure({**base, "writeReceipt": receipt["upstream"]["data"]}, "WEB_PATCH_NOT_ACKNOWLEDGED",
                        "Patch receipt was not a matching acknowledgement; inspect target before retry")
    after = _execute_once("asset.query", query, base, session)
    if not after["upstream"]["ok"]:
        return {**after, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False}
    current = next((a for a in after["upstream"]["data"]["Assets"] if a.get("PippitAssetID") == payload["shotAssetId"]), {})
    try:
        saved_prompt = json.loads(current["TextInfo"]["content"])["content"]["generation"]["prompt"]
    except (KeyError, TypeError, ValueError):
        saved_prompt = None
    if (saved_prompt != payload["description"]
            or type(current.get("version")) is not int
            or current["version"] < results[0]["asset_versions"][payload["shotAssetId"]]):
        return _failure({**base, "writeReceipt": receipt["upstream"]["data"], "persistenceVerified": False},
                        "WEB_READBACK_MISMATCH", "Patch was acknowledged but the exact saved description was not verified; inspect target before retry")
    receipt["persistenceVerified"] = True
    receipt["verifiedAsset"] = current
    return receipt


def execute(payload, job_id):
    validate(payload)
    action = payload["action"]
    spec = ACTIONS[action]
    writes = spec.get("write", False)
    base = {"action": action, "source": "live_web", "readOnly": not writes, "cacheUsed": False,
            "generationSubmitted": None if spec.get("generationUnknown") else False,
            "workspaceMutationAttempted": False, "requestedAt": datetime.now(timezone.utc).isoformat()}
    if writes and payload.get("dryRun", True):
        return {**base, "source": "local_plan", "dryRun": True, "dryRunKind": "bridge-local-plan",
                "generationSubmitted": False, "requestPlan": {"method": "POST", "path": spec["path"], "body": _body(payload, job_id)},
                "upstream": {"ok": True, "data": {"submitted": False}},
                "validationScope": "SDK structure only; does not verify project access, numeric enum meaning, price or upstream execution"}
    try:
        session = _load_session()
    except SessionError as exc:
        return _failure(base, exc.code, exc.message)
    if action == "subject.get":
        return _subject_lookup(payload, base, session)
    if action == "studio.storyboard.description.update":
        return _storyboard_description_update(payload, job_id, base, session)
    if action == "studio.episode.update":
        return _episode_update(payload, job_id, base, session)
    if action == "studio.episode.delete":
        return _episode_delete(payload, job_id, base, session)
    if writes:
        base["workspaceMutationAttempted"] = True
        base["upstreamIdempotencyVerified"] = False
        base["doNotResubmitAfterUncertainOutcome"] = True
        base["recovery"] = recovery(payload, job_id)
    return _execute_once(action, _body(payload, job_id), base, session)
