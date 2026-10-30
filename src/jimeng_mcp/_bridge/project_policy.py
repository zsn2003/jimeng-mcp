"""Shared operator policy and one-job ordinary-video approval declarations.

An approval is the caller's explicit record of authorization already received
for this task. This service does not independently verify the human's identity
or consent. The declaration never changes a project or global workflow policy.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

POLICIES = ("general", "sample_first")
STAGES = (None, "sample", "promote", "ordinary", "extend")
APPROVAL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
MAX_CONFIG_BYTES = 256 * 1024
MAX_CLAIM_BYTES = 8192


class ValidationError(ValueError):
    """Only fixed field/code/message are public; no payload or OS diagnostics."""
    def __init__(self, field, code, message):
        self.details = {"field": field, "code": code, "message": message}
        super().__init__(message)


def _error(field, code, message):
    raise ValidationError(field, code, message)


def public_workflow_schema():
    return {"type": "object", "additionalProperties": False,
        "properties": {"ordinaryApproval": {"type": "object", "additionalProperties": False,
            "required": ["approvalId", "reason"], "properties": {
                "approvalId": {"type": "string", "minLength": 1, "maxLength": 128,
                    "pattern": APPROVAL_PATTERN,
                    "description": "Non-secret audit identifier for this task's existing ordinary-video authorization. Never put a credential here; reuse across jobs is rejected."},
                "reason": {"type": "string", "minLength": 1, "maxLength": 2000,
                    "description": "State the already authorized ordinary-mode exception for this task. This is not a request for new permission and is not returned or persisted by the policy claim."}}}},
        "description": "Optional task-level authorization declaration. It cannot change the server or project workflow policy."}


def strip_workflow(payload):
    if not isinstance(payload, dict):
        _error("payload", "POLICY_PAYLOAD_INVALID", "The policy input must be an object")
    return deepcopy({key: value for key, value in payload.items() if key != "workflow"})


def _approval(payload):
    if "workflow" not in payload:
        return None
    workflow = payload["workflow"]
    if not isinstance(workflow, dict) or set(workflow) - {"ordinaryApproval"}:
        _error("workflow", "WORKFLOW_SCHEMA_INVALID", "Workflow accepts only the ordinaryApproval declaration")
    if "ordinaryApproval" not in workflow:
        return None
    approval = workflow["ordinaryApproval"]
    if not isinstance(approval, dict) or set(approval) != {"approvalId", "reason"}:
        _error("workflow.ordinaryApproval", "ORDINARY_APPROVAL_INVALID", "Ordinary approval requires only approvalId and reason")
    identity, reason = approval["approvalId"], approval["reason"]
    if not isinstance(identity, str) or not re.fullmatch(APPROVAL_PATTERN, identity):
        _error("workflow.ordinaryApproval.approvalId", "APPROVAL_ID_INVALID", "Approval identifier must be a bounded non-secret ASCII identifier")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
            or any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF for c in reason)):
        _error("workflow.ordinaryApproval.reason", "APPROVAL_REASON_INVALID", "Approval reason must be nonempty bounded plain text")
    return approval


def _credit_cap():
    raw = os.environ.get("JIMENG_MAX_CREDITS", "0").strip()
    if raw == "unlimited":
        return None
    if not re.fullmatch(r"[0-9]{1,6}", raw) or int(raw) > 100000:
        _error("configuration.creditCap", "CREDIT_POLICY_INVALID", "Server credit policy must be a bounded nonnegative integer or unlimited")
    return int(raw)


def _read_private_json(path, maximum, *, config=False):
    """Bounded no-follow reads; operator config may be public but not writable."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            disallowed = 0o022 if config else 0o077
            if not stat.S_ISREG(info.st_mode) or info.st_mode & disallowed or info.st_size > maximum:
                raise ValueError()
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError()
        return json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        _error("configuration.projectPolicy" if config else "workflow.ordinaryApproval",
            "PROJECT_POLICY_UNAVAILABLE" if config else "APPROVAL_CLAIM_INVALID",
            "Configured project policy cannot be read safely" if config else "Existing approval claim cannot be read safely")


def _workflow_policy(project_id):
    default = os.environ.get("JIMENG_VIDEO_WORKFLOW_POLICY", "general")
    if default not in POLICIES:
        _error("configuration.workflowPolicy", "WORKFLOW_POLICY_INVALID", "Server video workflow policy must be general or sample_first")
    configured = os.environ.get("JIMENG_PROJECT_POLICY_FILE")
    if not configured:
        return default, "server_default"
    path = Path(configured)
    if not path.is_absolute():
        _error("configuration.projectPolicy", "PROJECT_POLICY_INVALID", "Project policy file must use an absolute operator-configured path")
    document = _read_private_json(path, MAX_CONFIG_BYTES, config=True)
    projects = document.get("projects") if isinstance(document, dict) else None
    if (not isinstance(document, dict) or set(document) != {"projects"} or not isinstance(projects, dict)
            or len(projects) > 1000
            or any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key)
                   or value not in POLICIES for key, value in projects.items())):
        _error("configuration.projectPolicy", "PROJECT_POLICY_INVALID", "Project policy file must contain only bounded project policy mappings")
    selected = str(project_id) if project_id is not None else None
    return (projects[selected], "project_override") if selected in projects else (default, "server_default")


def _claim(approval_id, payload, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,179}", job_id):
        _error("jobId", "APPROVAL_JOB_REQUIRED", "A stable bounded server job identity is required to claim approval")
    configured = os.environ.get("JIMENG_POLICY_STATE_DIR")
    if not configured or not Path(configured).is_absolute():
        _error("configuration.policyState", "POLICY_STATE_REQUIRED", "An absolute private policy state directory is required")
    root = Path(configured)
    try:
        if root.is_symlink():
            raise ValueError()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o777 != 0o700:
            raise ValueError()
    except (OSError, ValueError):
        _error("configuration.policyState", "POLICY_STATE_INVALID", "Policy state directory must be private and not a symbolic link")
    action, project_id = payload.get("action"), payload.get("projectId")
    if (not isinstance(action, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", action)
            or project_id is not None and (type(project_id) not in (str, int)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(project_id)))):
        _error("payload", "APPROVAL_TARGET_INVALID", "Approval binding requires bounded action and project identities")
    try:
        body = json.dumps(strip_workflow(payload), ensure_ascii=True, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError, RecursionError):
        _error("payload", "POLICY_PAYLOAD_INVALID", "Approval binding requires a canonical JSON payload")
    approval_hash = hashlib.sha256(approval_id.encode("ascii")).hexdigest()
    binding = {"approvalIdSha256": approval_hash, "jobId": job_id,
        "payloadSha256": hashlib.sha256(body).hexdigest(),
        "action": action, "projectId": project_id}
    path = root / (approval_hash + ".json")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        existing = _read_private_json(path, MAX_CLAIM_BYTES)
        if not isinstance(existing, dict) or any(existing.get(key) != value for key, value in binding.items()):
            _error("workflow.ordinaryApproval.approvalId", "ORDINARY_APPROVAL_ALREADY_CLAIMED", "This ordinary approval is already bound to a different job or payload")
        return {"claimed": True, "replayed": True, "approvalIdSha256": approval_hash}
    except OSError:
        _error("workflow.ordinaryApproval", "APPROVAL_CLAIM_UNAVAILABLE", "Ordinary approval claim could not be persisted")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump({**binding, "claimedAt": datetime.now(timezone.utc).isoformat()}, target,
                      ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, ValueError, TypeError):
        # Keep even an incomplete reservation. Deleting it could authorize a
        # second job after a write whose persistence outcome is uncertain.
        _error("workflow.ordinaryApproval", "APPROVAL_CLAIM_UNCERTAIN", "Ordinary approval reservation is uncertain; inspect its original job before reuse")
    return {"claimed": True, "replayed": False, "approvalIdSha256": approval_hash}


def enforce(payload, effects, job_id=None, claim=False):
    """Validate policy, optionally claim one ordinary authorization before dispatch.

    With no project-policy file, validation without claim performs no file I/O.
    Configured project policies require only a bounded read. Reads/dry-runs
    never create or consume an approval, even when claim=True.
    """
    if not isinstance(payload, dict) or not isinstance(effects, dict):
        _error("payload", "POLICY_PAYLOAD_INVALID", "Policy input and trusted effects must be objects")
    for field in ("writes", "billable", "requiresUnlimited", "videoGeneration"):
        if field in effects and type(effects[field]) is not bool:
            _error("effects", "POLICY_EFFECTS_INVALID", "Trusted operation effects must use boolean flags")
    stage = effects.get("workflowStage")
    if stage not in STAGES:
        _error("effects", "POLICY_EFFECTS_INVALID", "Trusted video stage is unsupported")
    if "dryRun" in payload and type(payload["dryRun"]) is not bool:
        _error("dryRun", "DRY_RUN_INVALID", "dryRun must be boolean")
    approval = _approval(payload)
    dry_run = payload.get("dryRun", True) if effects.get("writes") else False
    live = not dry_run
    read_only = os.environ.get("JIMENG_READ_ONLY", "1") != "0"
    cap = _credit_cap()
    policy, policy_source = _workflow_policy(payload.get("projectId"))
    if effects.get("writes") and live and read_only:
        _error("dryRun", "READ_ONLY_POLICY", "Live mutations are disabled by the server read-only policy")
    if "creditCeiling" in payload:
        ceiling = payload["creditCeiling"]
        if type(ceiling) is not int or not 0 <= ceiling <= 100000:
            _error("creditCeiling", "CREDIT_CEILING_INVALID", "Credit ceiling must be a bounded nonnegative integer")
        if effects.get("billable") and effects.get("requiresUnlimited"):
            _error("creditCeiling", "NUMERIC_CEILING_UNSUPPORTED", "This operation cannot enforce a numeric credit ceiling")
    if effects.get("billable") and live:
        if effects.get("requiresUnlimited") and cap is not None:
            _error("creditCeiling", "UNLIMITED_POLICY_REQUIRED", "This operation requires the explicit unlimited server credit policy")
        if cap is not None and (type(payload.get("creditCeiling")) is not int or not 0 < payload["creditCeiling"] <= cap):
            _error("creditCeiling", "CREDIT_APPROVAL_REQUIRED", "Live spending requires a positive credit ceiling within the server cap")
    required = bool(effects.get("videoGeneration") and live and policy == "sample_first" and stage not in ("sample", "promote"))
    if required and approval is None:
        _error("workflow.ordinaryApproval", "ORDINARY_APPROVAL_REQUIRED", "This sample-first project requires a task-specific declaration for ordinary video generation")
    approval_claim = {"claimed": False, "replayed": False}
    if required and claim:
        approval_claim = _claim(approval["approvalId"], payload, job_id)
    return {"readOnly": read_only, "dryRun": dry_run, "creditPolicy": "unlimited" if cap is None else "limited",
        "liveCreditCeiling": cap, "videoWorkflowPolicy": policy, "workflowPolicySource": policy_source,
        "workflowStage": stage, "ordinaryApprovalRequired": required,
        "ordinaryApprovalDeclared": approval is not None, "ordinaryApprovalClaim": approval_claim,
        "approvalSemantics": "Caller declaration of existing task authorization; not independent human identity or consent verification"}
