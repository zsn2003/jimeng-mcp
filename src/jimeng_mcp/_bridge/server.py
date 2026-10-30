"""Persistent single-writer bridge with bounded read workers; Python 3.12+.

Local adapter contract:
    validate(payload: dict) -> None  # pure; ValueError means invalid input
    capabilities() -> dict
    execute(payload: dict, job_id: str) -> JSON-serializable result
    recovery(payload: dict, job_id: str) -> dict  # optional, pure, no secrets/prompts

COMPLETED means execute returned, NOT that upstream media is ready. Its result
must retain the upstream state. Execution ValueError is reserved for an input
rejection known to have no side effects. All other execution errors and timeouts
are UNKNOWN and never retried automatically. A timed-out call continues to
occupy its worker until it actually returns. Only fixed read actions can run
concurrently with the single writer. Cancellation only affects queued jobs.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import hmac
import importlib
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import urlsplit, parse_qs, urlencode, quote
import uuid

from outcome_normalizer import normalize, READ_ACTIONS


LOG = logging.getLogger("jimeng_bridge")
MAX_BODY_BYTES = 1024 * 1024
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "UNKNOWN"})
JOB_STATUSES = TERMINAL_STATUSES | {"QUEUED", "RUNNING"}
READ_WORKERS = 2
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A deliberately fixed allowlist. A new adapter is never implicitly concurrent.
READ_LANE_ACTIONS = READ_ACTIONS - {"director.draft.get"}


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details=None, headers=None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = details, headers


def _validation_details(exc):
    """Only an explicit structured adapter contract may expose field errors."""
    details = getattr(exc, "details", None)
    if type(exc).__name__ != "ValidationError" or not isinstance(details, (dict, list)):
        return None
    if isinstance(details, dict):
        details = [details]
    safe = []
    for item in details[:20]:
        if not isinstance(item, dict) or set(item) - {"field", "code", "message"}:
            continue
        field, code, message = (item.get(key) for key in ("field", "code", "message"))
        if (isinstance(field, str) and re.fullmatch(r"[\w.$\[\]-]{1,160}", field)
                and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code)
                and isinstance(message, str) and 0 < len(message) <= 256
                and not any(ord(c) < 32 for c in message)):
            safe.append({"field": field, "code": code, "message": message})
    return safe or None


def _query(raw, allowed):
    try:
        values = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    except ValueError:
        raise APIError(400, "INVALID_QUERY", "Invalid query parameters") from None
    if set(values) - set(allowed) or any(len(v) != 1 or not v[0] for v in values.values()):
        raise APIError(400, "INVALID_QUERY", "Unsupported, duplicate or empty query parameter")
    return {key: val[0] for key, val in values.items()}


def artifact_request(artifact_id, query):
    values = _query(query, {"projectId", "kind"})
    project, kind = values.get("projectId"), values.get("kind", "resource")
    if not re.fullmatch(UUID_PATTERN, artifact_id) or not isinstance(project, str) or not re.fullmatch(UUID_PATTERN, project) or kind not in ("resource", "director"):
        raise APIError(400, "INVALID_ARTIFACT_IDENTITY", "Use canonical project/artifact UUIDs and a fixed artifact kind")
    return {"projectId": project, "artifactId": artifact_id, "kind": kind}


def byte_range(value, size):
    if value is None:
        return 0, size - 1, 200
    match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", value)
    if not match or not any(match.groups()) or size == 0:
        raise APIError(416, "RANGE_NOT_SATISFIABLE", "Use one valid byte range", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if not first:
        count = int(last)
        if count == 0:
            raise APIError(416, "RANGE_NOT_SATISFIABLE", "Suffix range must be positive", headers={"Content-Range": f"bytes */{size}"})
        start, end = max(0, size - count), size - 1
    else:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
        if start >= size or start > end:
            raise APIError(416, "RANGE_NOT_SATISFIABLE", "Range is outside the artifact", headers={"Content-Range": f"bytes */{size}"})
    return start, end, 206


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def load_secret(secret_file: str | None = None) -> str:
    """Read a dedicated secret, never print it or accept it on a CLI argument."""
    filename = secret_file or os.environ.get("JIMENG_BRIDGE_SECRET_FILE")
    if filename:
        path = Path(filename)
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("Secret file must be a regular file")
        if os.name == "posix" and file_stat.st_mode & 0o077:
            raise ValueError("Secret file must not be readable by group or others")
        if file_stat.st_size > 8192:
            raise ValueError("Secret file is too large")
        secret = path.read_text(encoding="utf-8").strip()
    else:
        secret = os.environ.get("JIMENG_BRIDGE_SECRET", "").strip()
    if len(secret) < 32 or len(secret) > 4096 or any(c.isspace() for c in secret):
        raise ValueError("Provide a secret of 32-4096 characters without whitespace")
    return secret


class JobService:
    def __init__(self, db_path: str | Path, adapter: Any, secret: str,
                 execution_timeout: float = 600.0):
        if len(secret) < 32:
            raise ValueError("Secret must contain at least 32 characters")
        if not math.isfinite(execution_timeout) or execution_timeout <= 0:
            raise ValueError("Execution timeout must be positive and finite")
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.adapter = adapter
        self.secret = secret
        self.execution_timeout = execution_timeout
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._read_workers: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._lock_file = None
        self._acquire_process_lock()
        try:
            self._initialize()
        except BaseException:
            self._release_process_lock()
            raise

    def _acquire_process_lock(self) -> None:
        # Keep a second process from recovering or executing this same queue.
        import fcntl
        lock_path = Path(str(self.db_path) + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self._lock_file = os.fdopen(fd, "a+b")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("This database already has an active bridge process") from None

    def _release_process_lock(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 15000")
            conn.execute("PRAGMA synchronous = FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        fd = os.open(self.db_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(self.db_path, 0o600)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                body_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('QUEUED','RUNNING','COMPLETED','FAILED','UNKNOWN')),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                result_json TEXT,
                error_json TEXT
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created_at)")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for name, declaration in (("action", "TEXT"), ("project_id", "TEXT"), ("lane", "TEXT NOT NULL DEFAULT 'write'")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            for row in conn.execute("SELECT id,payload_json FROM jobs WHERE action IS NULL").fetchall():
                payload = json.loads(row["payload_json"])
                action = payload.get("action", "")
                conn.execute("UPDATE jobs SET action=?,project_id=?,lane=? WHERE id=?",
                    (action, payload.get("projectId"), "read" if action in READ_LANE_ACTIONS else "write", row["id"]))
            conn.execute("CREATE INDEX IF NOT EXISTS jobs_lane_queue ON jobs(lane,status,created_at)")
            conn.execute("""UPDATE jobs SET status = 'UNKNOWN', updated_at = ?,
                error_json = ? WHERE status = 'RUNNING'""", (
                time.time(), canonical_json({"code": "PROCESS_INTERRUPTED",
                    "message": "Process interrupted during execution; upstream outcome is unknown. No retry was sent."})))

    def _public_job(self, row: sqlite3.Row) -> dict[str, Any]:
        recovery = None
        recover = getattr(self.adapter, "recovery", None)
        if callable(recover):
            try:
                recovery = recover(json.loads(row["payload_json"]), row["id"])
                if not isinstance(recovery, dict):
                    raise TypeError("Recovery metadata must be an object")
                canonical_json(recovery)
            except Exception:
                # Metadata failure must not hide the durable job status.
                recovery = None
        result = json.loads(row["result_json"]) if row["result_json"] else None
        error = json.loads(row["error_json"]) if row["error_json"] else None
        payload = json.loads(row["payload_json"])
        return {
            "id": row["id"], "status": row["status"],
            "createdAt": row["created_at"], "updatedAt": row["updated_at"],
            "action": payload.get("action"), "projectId": payload.get("projectId"),
            "result": result, "error": error,
            "recovery": recovery,
            **normalize(row["status"], result, error, payload),
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise APIError(404, "JOB_NOT_FOUND", "Job was not found")
        return self._public_job(row)

    def list_jobs(self, *, limit=20, cursor=None, status=None, action=None, projectId=None):
        if type(limit) is not int or not 1 <= limit <= 100 or status is not None and status not in JOB_STATUSES:
            raise APIError(400, "INVALID_JOB_FILTER", "Invalid limit or job status")
        if action is not None and (not isinstance(action, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", action)):
            raise APIError(400, "INVALID_JOB_FILTER", "Invalid action filter")
        if projectId is not None and (not isinstance(projectId, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", projectId)):
            raise APIError(400, "INVALID_JOB_FILTER", "Invalid project filter")
        scope = hashlib.sha256(canonical_json([status, action, projectId]).encode()).hexdigest()[:16]
        clauses, params = [], []
        for column, value in (("status", status), ("action", action), ("project_id", projectId)):
            if value is not None:
                clauses.append(column + "=?")
                params.append(value)
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", cursor):
                    raise ValueError()
                point = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                if (not isinstance(point, list) or len(point) != 3 or point[2] != scope
                        or type(point[0]) not in (int, float) or not math.isfinite(point[0])
                        or not isinstance(point[1], str) or not re.fullmatch(r"[0-9a-f]{32}", point[1])):
                    raise ValueError()
                clauses.append("(created_at<? OR (created_at=? AND id<?))")
                params += [point[0], point[0], point[1]]
            except (ValueError, TypeError, UnicodeError):
                raise APIError(400, "INVALID_CURSOR", "Cursor does not match the original filters") from None
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM jobs" + where + " ORDER BY created_at DESC,id DESC LIMIT ?", params + [limit + 1]).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result = json.loads(row["result_json"]) if row["result_json"] else None
            error = json.loads(row["error_json"]) if row["error_json"] else None
            items.append({"id": row["id"], "status": row["status"], "createdAt": row["created_at"],
                "updatedAt": row["updated_at"], "action": row["action"], "projectId": row["project_id"],
                "lane": row["lane"], "errorCode": error.get("code") if error else None,
                **normalize(row["status"], result, error, payload)})
        next_cursor = None
        if has_more:
            row = rows[-1]
            next_cursor = base64.urlsafe_b64encode(canonical_json([row["created_at"], row["id"], scope]).encode()).decode().rstrip("=")
        return {"items": items, "hasMore": has_more, "nextCursor": next_cursor}

    def queue_summary(self):
        with self._connect() as conn:
            rows = conn.execute("SELECT status,lane,count(*) AS count FROM jobs GROUP BY status,lane").fetchall()
            oldest = conn.execute("SELECT min(created_at) FROM jobs WHERE status='QUEUED'").fetchone()[0]
        return {"counts": [{"status": row["status"], "lane": row["lane"], "count": row["count"]} for row in rows],
            "oldestQueuedAt": oldest, "writeWorkers": 1, "readWorkers": READ_WORKERS,
            "stopping": self._closed, "automaticExecutionRetries": False}

    def cancel_job(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise APIError(400, "INVALID_JOB_ID", "Invalid job identity")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise APIError(404, "JOB_NOT_FOUND", "Job was not found")
            error = json.loads(row["error_json"]) if row["error_json"] else {}
            if row["status"] == "FAILED" and error.get("code") == "CANCELLED_BEFORE_EXECUTION":
                return {"cancelled": True, "alreadyCancelled": True, "job": self._public_job(row)}
            if row["status"] != "QUEUED":
                raise APIError(409, "JOB_ALREADY_STARTED", "Only QUEUED jobs may be cancelled; no upstream cancellation was sent")
            conn.execute("UPDATE jobs SET status='FAILED',updated_at=?,error_json=? WHERE id=? AND status='QUEUED'",
                (time.time(), canonical_json({"code": "CANCELLED_BEFORE_EXECUTION", "message": "Cancelled before execution; no upstream call was made."}), job_id))
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {"cancelled": True, "alreadyCancelled": False, "job": self._public_job(row)}

    @contextmanager
    def artifact(self, payload):
        descriptor = getattr(self.adapter, "artifact_descriptor", None)
        if not callable(descriptor):
            raise APIError(503, "ARTIFACT_UNAVAILABLE", "Managed artifact download is unavailable")
        source = None
        try:
            info = descriptor(payload)
            if (not isinstance(info, dict) or not isinstance(info.get("path"), str) or not Path(info["path"]).is_absolute()
                    or type(info.get("byteSize")) is not int or not 0 < info["byteSize"] <= MAX_ARTIFACT_BYTES
                    or not isinstance(info.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", info["sha256"])
                    or not isinstance(info.get("filename"), str) or not 1 <= len(info["filename"]) <= 255
                    or any(c in info["filename"] for c in ('/', '\\', '\r', '\n', '\x00'))
                    or not isinstance(info.get("format"), str) or not re.fullmatch(r"[a-zA-Z0-9]{1,16}", info["format"])):
                raise ValueError()
            fd = os.open(info["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            source = os.fdopen(fd, "rb")
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != info["byteSize"]:
                raise ValueError()
            digest = hashlib.file_digest(source, "sha256").hexdigest()
            after = os.fstat(source.fileno())
            if digest != info["sha256"] or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError()
            source.seek(0)
            manifest = {key: info[key] for key in ("byteSize", "sha256", "filename", "format")}
            manifest.update(payload)
        except Exception:
            if source is not None:
                source.close()
            raise APIError(404, "ARTIFACT_NOT_AVAILABLE", "No verified managed artifact matches this identity") from None
        try:
            yield source, manifest
        finally:
            source.close()

    def submit(self, payload: dict[str, Any], key: str) -> tuple[dict[str, Any], bool]:
        if self._closed:
            raise APIError(503, "SERVICE_STOPPING", "Service is stopping")
        if not re.fullmatch(r"[\x21-\x7e]{1,200}", key):
            raise APIError(400, "INVALID_IDEMPOTENCY_KEY",
                           "Idempotency-Key must contain 1-200 visible ASCII characters")
        if not isinstance(payload, dict):
            raise APIError(400, "INVALID_JSON", "Request body must be a JSON object")
        try:
            payload_json = canonical_json(payload)
        except (TypeError, ValueError, RecursionError):
            raise APIError(400, "INVALID_JSON", "Body must be finite, valid JSON") from None
        body_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        # Replay and conflict detection precede validation: changed adapter rules
        # cannot convert an already persisted request into a new submission.
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
        if row is not None:
            return self._replay(row, body_hash), False
        try:
            self.adapter.validate(json.loads(payload_json))
        except ValueError as exc:
            raise APIError(422, "INVALID_PAYLOAD", "Payload did not pass adapter validation", _validation_details(exc)) from None
        except Exception:
            raise APIError(503, "ADAPTER_UNAVAILABLE", "Adapter validation is unavailable") from None
        now = time.time()
        job_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
            if row is not None:
                return self._replay(row, body_hash), False
            conn.execute("""INSERT INTO jobs
                (id, idempotency_key, body_hash, payload_json, status, created_at, updated_at, action, project_id, lane)
                VALUES (?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?)""",
                (job_id, key, body_hash, payload_json, now, now, payload.get("action", ""),
                 payload.get("projectId"), "read" if payload.get("action") in READ_LANE_ACTIONS else "write"))
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        # Transaction has committed before either notification or execution.
        self._wake.set()
        return self._public_job(row), True

    def _replay(self, row: sqlite3.Row, body_hash: str) -> dict[str, Any]:
        if row["body_hash"] != body_hash:
            raise APIError(409, "IDEMPOTENCY_CONFLICT",
                           "This Idempotency-Key is already associated with a different body")
        return self._public_job(row)

    def capabilities(self) -> dict[str, Any]:
        try:
            backend = self.adapter.capabilities()
            if not isinstance(backend, dict):
                raise TypeError("Capabilities must be an object")
            canonical_json(backend)
        except Exception:
            raise APIError(503, "ADAPTER_UNAVAILABLE", "Adapter capabilities are unavailable") from None
        return {"service": {"version": "1", "maxBodyBytes": MAX_BODY_BYTES,
                "completionMeaning": "adapter_call_completed",
                "automaticExecutionRetries": False, "workers": 1, "readWorkers": READ_WORKERS,
                "operationOutcomeSemantics": "business outcome is separate from adapter completion",
                "dispatchAttemptedSemantics": "upstream write/generation attempt; null means unknown"}, "backend": backend}

    def studio_catalog(self) -> dict[str, Any]:
        try:
            catalog = self.adapter.studio_catalog()
            if not isinstance(catalog, dict):
                raise TypeError("Catalog must be an object")
            canonical_json(catalog)
            return catalog
        except Exception:
            raise APIError(503, "CATALOG_UNAVAILABLE", "Studio capability catalog is unavailable") from None

    def start_worker(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("Service is closed")
            if self._worker is None:
                self._worker = threading.Thread(target=self._worker_loop,
                                                name="jimeng-worker", daemon=True)
                self._worker.start()
                for index in range(READ_WORKERS):
                    worker = threading.Thread(target=self._worker_loop, args=("read",),
                                              name=f"jimeng-read-{index}", daemon=True)
                    self._read_workers.append(worker)
                    worker.start()

    def _claim_next(self, lane="write") -> tuple[str, dict[str, Any]] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT id, payload_json FROM jobs
                WHERE status = 'QUEUED' AND lane=? ORDER BY created_at, rowid LIMIT 1""", (lane,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE jobs SET status = 'RUNNING', updated_at = ? WHERE id = ?",
                         (time.time(), row["id"]))
        return row["id"], json.loads(row["payload_json"])

    def _finish(self, job_id: str, status: str, *, result_json: str | None = None,
                error: dict[str, str] | None = None) -> None:
        with self._connect() as conn:
            # A late result must not erase timeout/interruption uncertainty.
            conn.execute("""UPDATE jobs SET status = ?, updated_at = ?, result_json = ?,
                error_json = ? WHERE id = ? AND status = 'RUNNING'""", (
                status, time.time(), result_json,
                canonical_json(error) if error else None, job_id))

    def _timed_out(self, job_id: str) -> None:
        try:
            self._finish(job_id, "UNKNOWN", error={"code": "EXECUTION_TIMEOUT",
                "message": "Execution exceeded its deadline; upstream outcome is unknown. No retry was sent."})
        except Exception:
            LOG.error("Could not persist timeout for job %s", job_id)

    def _execute(self, job_id: str, payload: dict[str, Any]) -> None:
        timer = threading.Timer(self.execution_timeout, self._timed_out, args=(job_id,))
        timer.daemon = True
        timer.start()
        try:
            result = self.adapter.execute(payload, job_id)
        except ValueError:
            self._finish(job_id, "FAILED", error={"code": "INPUT_REJECTED",
                "message": "Adapter rejected the input before any upstream side effect."})
        except Exception:
            self._finish(job_id, "UNKNOWN", error={"code": "EXECUTION_UNCERTAIN",
                "message": "Execution failed with an unknown upstream outcome. No retry was sent."})
        else:
            try:
                result_json = canonical_json(result)
            except Exception:
                self._finish(job_id, "UNKNOWN", error={"code": "INVALID_ADAPTER_RESULT",
                    "message": "Adapter returned an invalid result; upstream outcome is unknown. No retry was sent."})
            else:
                self._finish(job_id, "COMPLETED", result_json=result_json)
        finally:
            timer.cancel()

    def _worker_loop(self, lane="write") -> None:
        while not self._stop.is_set():
            try:
                item = self._claim_next(lane)
                if item is not None:
                    self._execute(*item)
                    continue
            except Exception:
                # No in-process retry of a claimed RUNNING job. Recovery on a
                # future start will mark it UNKNOWN if persistence failed.
                LOG.error("Worker encountered a persistence or execution error")
            self._wake.wait(0.5)
            self._wake.clear()

    def close(self, timeout: float = 5.0) -> bool:
        """Return False if execution is still active; retain its process lock."""
        with self._lifecycle_lock:
            self._closed = True
            self._stop.set()
            self._wake.set()
            deadline = time.monotonic() + timeout
            for worker in ([self._worker] if self._worker is not None else []) + self._read_workers:
                worker.join(max(0, deadline - time.monotonic()))
                if worker.is_alive():
                    return False
            self._release_process_lock()
            return True


def make_server(service: JobService, host: str = "127.0.0.1",
                port: int = 8787) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "JimengBridge/1"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt: str, *args: Any) -> None:
            # Avoid URLs, headers, payloads, credentials and adapter diagnostics.
            pass

        def _reply(self, status: int, value: Any, headers: dict[str, str] | None = None) -> None:
            raw = canonical_json(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def _authorize(self) -> None:
            values = self.headers.get_all("Authorization", [])
            scheme, _, value = values[0].partition(" ") if len(values) == 1 else ("", "", "")
            if scheme.lower() != "bearer" or not hmac.compare_digest(
                    value.encode("utf-8"), service.secret.encode("utf-8")):
                raise APIError(401, "UNAUTHORIZED", "A valid Bearer token is required")

        def _body(self) -> dict[str, Any]:
            if self.headers.get("Transfer-Encoding"):
                raise APIError(400, "INVALID_BODY", "Transfer-Encoding is not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise APIError(411, "LENGTH_REQUIRED", "Content-Length is required")
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise APIError(400, "INVALID_LENGTH", "Content-Length is invalid")
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise APIError(413, "BODY_TOO_LARGE", "Request body exceeds the size limit")
            if self.headers.get_content_type() != "application/json":
                raise APIError(415, "UNSUPPORTED_MEDIA_TYPE", "Use application/json")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise APIError(400, "INVALID_BODY", "Request body is incomplete")
            try:
                payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                if not isinstance(payload, dict):
                    raise ValueError("Expected object")
                canonical_json(payload)
                return payload
            except (UnicodeError, ValueError, TypeError, RecursionError):
                raise APIError(400, "INVALID_JSON", "Request body must be a valid JSON object") from None

        def _dispatch(self) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            if self.command == "GET" and path == "/healthz":
                self._reply(200, {"status": "ok"})
                return
            self._authorize()
            artifact_match = re.fullmatch(r"/v1/artifacts/(" + UUID_PATTERN + r")(/manifest)?", path)
            if self.command == "GET" and artifact_match:
                payload = artifact_request(artifact_match[1], parsed.query)
                with service.artifact(payload) as (source, manifest):
                    if artifact_match[2]:
                        self._reply(200, {**manifest, "downloadRoute": path.removesuffix("/manifest") + "?" + urlencode({"projectId": payload["projectId"], "kind": payload["kind"]}),
                            "requiresBearerAuth": True, "rangeSupported": True})
                        return
                    ranges = self.headers.get_all("Range", [])
                    if len(ranges) > 1:
                        raise APIError(416, "RANGE_NOT_SATISFIABLE", "Only one Range header is accepted", headers={"Content-Range": f"bytes */{manifest['byteSize']}"})
                    start, end, status = byte_range(ranges[0] if ranges else None, manifest["byteSize"])
                    self.send_response(status)
                    mime = {"mp4": "video/mp4", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                            "webp": "image/webp", "mp3": "audio/mpeg"}.get(manifest["format"].lower(), "application/octet-stream")
                    self.send_header("Content-Type", mime)
                    self.send_header("Content-Length", str(end - start + 1))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(manifest["filename"], safe=""))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("ETag", '"' + manifest["sha256"] + '"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{manifest['byteSize']}")
                    self.end_headers()
                    self._stream_started = True
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        data = source.read(min(65536, remaining))
                        if not data:
                            # Headers already sent: close instead of appending JSON to media.
                            self.close_connection = True
                            return
                        self.wfile.write(data)
                        remaining -= len(data)
                return
            if self.command == "GET" and path == "/v1/jobs":
                filters = _query(parsed.query, {"limit", "cursor", "status", "action", "projectId"})
                if "limit" in filters:
                    if not re.fullmatch(r"[0-9]{1,3}", filters["limit"]):
                        raise APIError(400, "INVALID_JOB_FILTER", "Invalid limit")
                    filters["limit"] = int(filters["limit"])
                self._reply(200, service.list_jobs(**filters))
            elif self.command == "GET" and path == "/v1/queue":
                if parsed.query:
                    raise APIError(400, "INVALID_QUERY", "Queue summary takes no parameters")
                self._reply(200, service.queue_summary())
            elif self.command == "POST" and re.fullmatch(r"/v1/jobs/[0-9a-f]{32}/cancel", path):
                if parsed.query or self._body():
                    raise APIError(400, "INVALID_CANCEL_REQUEST", "Cancellation accepts an empty JSON object only")
                self._reply(200, service.cancel_job(path.split("/")[3]))
            elif self.command == "GET" and path == "/v1/capabilities":
                self._reply(200, service.capabilities())
            elif self.command == "GET" and path == "/v1/studio/catalog":
                self._reply(200, service.studio_catalog())
            elif self.command == "GET" and re.fullmatch(r"/v1/jobs/[0-9a-f]{32}", path):
                self._reply(200, service.get_job(path.rsplit("/", 1)[1]))
            elif self.command == "POST" and path == "/v1/jobs":
                keys = self.headers.get_all("Idempotency-Key", [])
                if len(keys) != 1:
                    raise APIError(400, "IDEMPOTENCY_KEY_REQUIRED", "Exactly one Idempotency-Key is required")
                job, created = service.submit(self._body(), keys[0])
                self._reply(202 if created else 200, job, {
                    "Location": "/v1/jobs/" + job["id"],
                    "Idempotent-Replayed": "false" if created else "true"})
            else:
                raise APIError(404, "NOT_FOUND", "Route was not found")

        def _handle(self) -> None:
            self._stream_started = False
            try:
                self._dispatch()
            except APIError as exc:
                error = {"code": exc.code, "message": exc.message}
                if exc.details:
                    error["details"] = exc.details
                self._reply(exc.status, {"error": error},
                            {"WWW-Authenticate": "Bearer"} if exc.status == 401 else exc.headers)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                if self._stream_started:
                    self.close_connection = True
                else:
                    self._reply(500, {"error": {"code": "INTERNAL_ERROR",
                        "message": "Request could not be completed; retry with the same Idempotency-Key"}})

        do_GET = _handle
        do_POST = _handle

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default="data/jobs.sqlite3")
    parser.add_argument("--secret-file")
    parser.add_argument("--execution-timeout", type=float, default=600.0)
    parser.add_argument("--allow-remote", action="store_true",
                        help="Explicitly allow a non-loopback bind; protect with a TLS reverse proxy")
    args = parser.parse_args()
    try:
        is_loopback = args.host == "localhost" or ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback and not args.allow_remote:
        parser.error("Non-loopback binding requires --allow-remote")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        secret = load_secret(args.secret_file)
        adapter = importlib.import_module("adapter")
        service = JobService(args.db, adapter, secret, args.execution_timeout)
    except (ValueError, OSError, RuntimeError, ImportError) as exc:
        parser.error(str(exc))
    httpd = None
    try:
        httpd = make_server(service, args.host, args.port)
        service.start_worker()
        LOG.info("Bridge listening on %s:%s", args.host, httpd.server_port)
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if httpd is not None:
            httpd.server_close()
        if not service.close():
            LOG.warning("Execution is still active; its outcome will require reconciliation")


if __name__ == "__main__":
    main()
