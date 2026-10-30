"""Scoped historical acceptance, pinned to the exact reviewed evidence bytes.

This is not a live authentication check. A record never certifies other variants.
Missing, changed or malformed evidence fails closed for both public catalogs.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "acceptance_records.json"


def all_verified():
    try:
        index = json.loads(INDEX.read_text(encoding="utf-8"))
        records = index["records"]
        if index.get("version") != 1 or not isinstance(records, list):
            return []
    except (OSError, ValueError, KeyError, TypeError):
        return []
    checked = {}
    result = []
    for record in records:
        if not isinstance(record, dict):
            continue
        required = ("action", "variant", "scope", "evidence", "sha256", "verifiedAt")
        if any(not isinstance(record.get(k), str) or not record[k] for k in required):
            continue
        relative = Path(record["evidence"])
        if relative.is_absolute() or relative.parts[:1] != ("evidence",) or ".." in relative.parts:
            continue
        key = (record["evidence"], record["sha256"])
        if key not in checked:
            try:
                path = ROOT / relative
                if path.is_symlink() or not path.resolve().is_relative_to((ROOT / "evidence").resolve()):
                    raise ValueError("Untrusted evidence path")
                raw = path.read_bytes()
                if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != record["sha256"]:
                    raise ValueError("Evidence changed since acceptance")
                data = json.loads(raw)
                if not isinstance(data, (dict, list)):
                    raise ValueError("Invalid evidence object")
                checked[key] = True
            except (OSError, ValueError, TypeError):
                checked[key] = False
        if checked[key]:
            result.append(deepcopy(record))
    return result


def verified(action, variant=None):
    return [r for r in all_verified() if r["action"] == action and (variant is None or r["variant"] == variant)]


def annotate(actions):
    records = all_verified()
    for name, spec in actions.items():
        matches = [r for r in records if r["action"] == name]
        spec["serverExecutionVerified"] = bool(matches)
        spec["executionVerification"] = matches
        spec["verificationMeaning"] = "Historical exact variants only; not all inputs, current login or output quality."
    return actions


def annotate_catalog(catalog):
    records = all_verified()
    # Feature names that describe a narrower variant must not inherit evidence
    # from another variant of the same callable action.
    filters = {
        "resource.download_image": "image", "resource.download_video": "video",
        "resource.download_audio": "audio_mp3",
        "studio.script.update": "raw_content.update",
        "studio.video.extend": "prepared_target_submission",
    }
    for row in catalog.get("operations", []):
        names = row.get("callableActions", [])
        if not names and row.get("callableAction"):
            names = [row["callableAction"]]
        variant = filters.get(row.get("operation"))
        matched = {name: [r for r in records if r["action"] == name and (variant is None or r["variant"] == variant)] for name in names}
        row["verifiedActions"] = {name: rows for name, rows in matched.items() if rows}
        row["serverExecutionVerified"] = bool(names) and all(matched.values())
        row["verificationScope"] = "Only the listed exact evidence variants; code presence is separate from execution acceptance."
        if names:
            row["evidenceLevel"] = "server_execution_verified" if row["serverExecutionVerified"] else "adapter_implemented_execution_pending"
    summary = catalog.setdefault("coverageSummary", {})
    summary["featuresWithAllMappedActionsExecutionVerified"] = sum(r.get("serverExecutionVerified") is True for r in catalog.get("operations", []))
    catalog["acceptanceRegistry"] = {"source": "acceptance_records.json", "verifiedRecordCount": len(records), "integrity": "sha256-pinned-reviewed-evidence"}
    return catalog
