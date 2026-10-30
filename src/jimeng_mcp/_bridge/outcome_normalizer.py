"""Conservative business outcomes, separate from durable transport job status.

dispatchAttempted describes an upstream write/generation attempt, not a read.
None means the adapter did not provide enough evidence. This module never
dispatches, retries, or upgrades an interrupted job based on an old receipt.
"""
from __future__ import annotations

OUTCOMES = frozenset({"accepted", "running", "succeeded", "rejected", "partial", "unknown"})
READ_ACTIONS = frozenset({
    "account.status", "account.read", "space.current", "team.list", "voice.list",
    "model.list", "model.inspect", "canvas.list", "canvas.current", "node.list", "node.read",
    "operation.status", "operation.wait", "resource.read", "resource.operation.get",
    "resource.artifact.read", "resource.upload.inspect", "director.catalog", "director.get",
    "director.draft.get", "director.operation.get", "director.artifact.read",
    "subject.list", "subject.get", "subject.import.inspect", "public_character.search",
    "public_character.read", "public_character.face_config", "public_character.expression_config",
    "asset.query", "asset.artifact.query", "studio.asset.list", "studio.asset.share_get", "studio.canvas.list",
    "studio.character.read", "studio.character.resolve", "studio.character_entity.list",
    "studio.character_entity.read", "studio.project.list", "studio.project.overview.read",
    "studio.script.read", "studio.script.resolve", "studio.storyboard.read", "studio.style.list",
    "studio.thread.read",
    "video.extend.status", "plugin.session.list", "plugin.session.search", "plugin.task.list", "plugin.task.read",
})
GENERATION_ACTIONS = frozenset({"node.run", "image.generate", "video.generate", "video.regenerate",
    "audio.generate", "video.sample", "video.promote", "video.extend", "image.upscale", "image.cutout",
    "video.upscale", "video.interpolate", "studio.images.generate", "studio.role.audio.generate",
    "studio.video.erase_subtitle", "plugin.video.multiframe"})
# These fixed adapters verify the saved target before setting this flag. A
# generic successful receipt or a similarly named field must not certify other
# writes, finished media, or persistence after a failed/uncertain request.
PERSISTENCE_VERIFIED_ACTIONS = frozenset({
    "studio.episode.update", "studio.episode.delete", "studio.storyboard.description.update",
    "personal.subject.create", "personal.subject.update", "personal.subject.delete",
    "personal.voice.create", "personal.voice.update", "personal.voice.archive",
})
_STATE = {"accepted": "accepted", "queued": "accepted", "pending": "accepted", "submitted": "accepted",
    "running": "running", "processing": "running", "generating": "running",
    "success": "succeeded", "succeeded": "succeeded", "completed": "succeeded", "complete": "succeeded",
    "failed": "rejected", "fail": "rejected", "rejected": "rejected", "canceled": "rejected",
    "cancelled": "rejected", "partial_success": "partial", "partial": "partial",
    "unknown": "unknown", "needs-inspection": "unknown", "saved-unverified": "partial"}


def normalize(status, result=None, error=None, payload=None):
    result = result if isinstance(result, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {}
    action = payload.get("action") or result.get("action")
    explicit_dispatch = result.get("dispatchAttempted")
    attempt_flags = [result.get(key) for key in ("workspaceMutationAttempted", "generationSubmitted",
        "creditApprovalAttempted", "webWriteAttempted")]
    if type(explicit_dispatch) is bool:
        dispatched = explicit_dispatch
    elif any(value is True for value in attempt_flags):
        dispatched = True
    elif action in READ_ACTIONS or result.get("dryRun") is True or payload.get("dryRun") is True:
        dispatched = False
    elif any(value is False for value in attempt_flags) and all(value is not True for value in attempt_flags):
        # A false generationSubmitted alone does not rule out a workspace write.
        dispatched = False if result.get("workspaceMutationAttempted") is False else None
    else:
        dispatched = None
    upstream = result.get("upstream") if isinstance(result.get("upstream"), dict) else {}
    data = upstream.get("data") if isinstance(upstream.get("data"), dict) else {}
    business_error = result.get("error") or upstream.get("error")
    if status == "QUEUED":
        outcome, dispatched, reconcile = "accepted", False, False
    elif status == "RUNNING":
        outcome, reconcile = "running", False
    elif status == "UNKNOWN":
        outcome, reconcile = "unknown", True
    elif status == "FAILED":
        outcome, reconcile = "rejected", False
        if error.get("code") in ("INPUT_REJECTED", "CANCELLED_BEFORE_EXECUTION"):
            dispatched = False
    else:
        explicit = result.get("operationOutcome")
        if explicit in OUTCOMES:
            outcome = explicit
        elif business_error or upstream.get("ok") is False or result.get("available") is False:
            code = business_error.get("code", "") if isinstance(business_error, dict) else ""
            if (result.get("createdResources") or result.get("phase") in ("element-save", "element-readback")
                    or "UNVERIFIED" in code or "READBACK_MISMATCH" in code):
                outcome = "partial"
            elif any(word in code for word in ("UNCERTAIN", "RECONCILIATION", "UNKNOWN")) or dispatched is True:
                outcome = "unknown"
            else:
                outcome = "rejected"
        else:
            # Only inspect recognized envelopes; arbitrary nested state fields
            # (for example a subject description) cannot certify completion.
            submission = data.get("submission") if isinstance(data.get("submission"), dict) else {}
            operation = result.get("operation") if isinstance(result.get("operation"), dict) else {}
            states = [submission.get("state"), data.get("state"), data.get("status"),
                      result.get("state"), operation.get("state")]
            recognized = [_STATE[value.lower()] for value in states if isinstance(value, str) and value.lower() in _STATE]
            if recognized:
                # If receipts disagree, retain the least conclusive state.
                outcome = next((v for v in ("unknown", "partial", "rejected", "running", "accepted", "succeeded") if v in recognized), "unknown")
            elif result.get("bindingsVerified") is True or result.get("resourceReady") is True:
                outcome = "succeeded"
            elif (action in PERSISTENCE_VERIFIED_ACTIONS and result.get("persistenceVerified") is True
                    and upstream.get("ok") is True):
                outcome = "succeeded"
            elif result.get("dryRun") is True:
                outcome = "succeeded"
            elif isinstance(result.get("artifact"), dict) and result["artifact"].get("sha256"):
                outcome = "succeeded"
            elif (isinstance(result.get("upload"), dict) and type(result["upload"].get("receivedBytes")) is int
                    and action in ("resource.upload.stage", "resource.upload.inspect")):
                outcome = "succeeded"
            elif upstream.get("ok") is True and action in READ_ACTIONS:
                outcome = "succeeded"
            elif action in GENERATION_ACTIONS and (result.get("generationSubmitted") is True or upstream.get("ok") is True):
                outcome = "accepted"
            elif upstream.get("ok") is True and isinstance(action, str):
                # A known successful adapter envelope acknowledges the call,
                # but does not certify saved bindings or finished media.
                outcome = "accepted"
            else:
                outcome = "unknown"
        reconcile = outcome in ("partial", "unknown")
        if type(result.get("reconciliationRequired")) is bool:
            reconcile = result["reconciliationRequired"]
    return {"operationOutcome": outcome, "reconciliationRequired": reconcile,
            "dispatchAttempted": dispatched}
