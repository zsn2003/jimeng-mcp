"""Private, stateless MCP Streamable HTTP facade for the local Jimeng bridge.

Python 3.12+, standard library only (the official MCP SDK is not installed in
the target's dependency-free runtime). This deliberately small implementation
supports MCP 2025-06-18 and the 2025-03-26 HTTP compatibility version:
initialize, ping, tools/list, tools/call, and notification acknowledgements.
Responses are JSON; GET/DELETE /mcp return 405. There are no sessions, SSE feeds,
server-initiated requests, shell commands, arbitrary upstream URLs or retries.

Protocol references:
https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
https://modelcontextprotocol.io/specification/2025-06-18/server/tools

Deployment: keep the default 127.0.0.1:8774 behind a TLS reverse proxy. Provide
two distinct, private token files with --secret-file and --backend-secret-file.
The backend is always 127.0.0.1 (default port 8773), never caller-selected.
No Origin header is needed for server-to-server clients. Browser Origins are
rejected unless explicitly listed with --allowed-origin. Token values are never
accepted on the command line or returned in protocol results. Actual write and
credit policies remain authoritative in the backend service.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
from urllib.parse import urlsplit, urlencode
from contextlib import contextmanager

from server import artifact_request, APIError, UUID_PATTERN, MAX_ARTIFACT_BYTES, _query


LOG = logging.getLogger("jimeng_mcp")
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = frozenset({"2025-03-26", PROTOCOL_VERSION})
MAX_BODY_BYTES = 1024 * 1024
MAX_BACKEND_BYTES = 8 * 1024 * 1024
KEY_PATTERN = r"[\x21-\x7e]{1,200}"
JOB_PATTERN = r"[0-9a-f]{32}"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_json(raw: bytes) -> Any:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    canonical_json(value)
    return value


def load_private_token(filename: str | Path) -> str:
    """Read one regular private file without following a final symlink."""
    fd = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError("Token must be in a small regular file")
        if os.name == "posix" and info.st_mode & 0o077:
            raise ValueError("Token file must not be accessible to group or others")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            token = stream.read(8193).strip()
    finally:
        if fd is not None:
            os.close(fd)
    if not re.fullmatch(r"[\x21-\x7e]{32,4096}", token):
        raise ValueError("Token must contain 32-4096 visible ASCII characters")
    return token


def _object(properties=None, required=()):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}


def _validate(value, schema):
    """Validate the closed JSON Schema subset used by the bundled OpenAPI.

    Runtime model/credit/authorization checks belong to the backend, so this
    does not import adapter.validate or infer policy from the MCP process env.
    """
    if schema is False:
        raise ValueError("Input exceeds the supported schema boundary")
    if schema is True:
        return
    if "not" in schema:
        try:
            _validate(value, schema["not"])
        except ValueError:
            pass
        else:
            raise ValueError("Input matches a prohibited schema")
    for keyword in ("oneOf", "anyOf"):
        if keyword in schema:
            matches = 0
            for choice in schema[keyword]:
                try:
                    _validate(value, choice)
                    matches += 1
                except ValueError:
                    pass
            if not matches or (keyword == "oneOf" and matches != 1):
                raise ValueError("Input does not match a supported operation schema")
    kind = schema.get("type")
    if kind is not None:
        valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
                 "string": isinstance(value, str), "boolean": type(value) is bool,
                 "integer": type(value) is int, "number": type(value) in (int, float)}
        if not valid.get(kind, False):
            raise ValueError("Input has an invalid type")
    if "const" in schema and value != schema["const"]:
        raise ValueError("Input has an invalid constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Input has an invalid enum value")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Object keys must be strings")
        if not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", MAX_BODY_BYTES):
            raise ValueError("Input has an invalid property count")
        if set(schema.get("required", ())) - set(value):
            raise ValueError("Input is missing required fields")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("Input contains unsupported fields")
        for key in set(value) & set(properties):
            _validate(value[key], properties[key])
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for key in set(value) - set(properties):
                _validate(value[key], additional)
    elif isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", MAX_BODY_BYTES):
            raise ValueError("Input has an invalid item count")
        if "items" in schema:
            for item in value:
                _validate(item, schema["items"])
        if schema.get("uniqueItems") and len({canonical_json(v) for v in value}) != len(value):
            raise ValueError("Input items must be unique")
    elif isinstance(value, str):
        if "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("Input contains unsupported Unicode")
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", MAX_BODY_BYTES):
            raise ValueError("Input has an invalid string length")
        # JSON Schema pattern uses a search, not a full-string match. Identity
        # schemas carry their own ^/$ anchors; prefix rules intentionally do not.
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError("Input has an invalid string format")
    elif type(value) in (int, float):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("Input must be finite")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError("Input is below its minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError("Input exceeds its maximum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError("Input must exceed its minimum")


def load_payload_schema(filename: str | Path) -> dict[str, Any]:
    document = parse_json(Path(filename).read_bytes())
    schema = document["paths"]["/v1/jobs"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    choices = schema.get("oneOf")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAPI must declare fixed action request schemas")
    names = set()
    for choice in choices:
        name = choice.get("properties", {}).get("action", {}).get("const")
        if not isinstance(name, str) or name in names or choice.get("additionalProperties") is not False:
            raise ValueError("Every action schema must be closed and have one unique constant action")
        names.add(name)
    # Refuse unsupported validation constructs rather than quietly weakening
    # a future contract. Descriptions and defaults do not affect validation.
    supported = {"type", "const", "enum", "properties", "required", "additionalProperties",
                 "items", "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
                 "pattern", "minimum", "maximum", "exclusiveMinimum", "oneOf", "anyOf",
                 "minProperties", "maxProperties", "not", "format",
                 "description", "title", "default"}
    def check(node):
        if type(node) is bool:
            return
        if not isinstance(node, dict) or set(node) - supported:
            raise ValueError("OpenAPI uses an unsupported validation keyword")
        for child in node.get("properties", {}).values():
            check(child)
        if "items" in node:
            check(node["items"])
        if "not" in node:
            check(node["not"])
        if isinstance(node.get("additionalProperties"), dict):
            check(node["additionalProperties"])
        for keyword in ("oneOf", "anyOf"):
            for child in node.get(keyword, []):
                check(child)
    check(schema)
    return deepcopy(schema)


class BackendUnavailable(Exception):
    pass


class BackendClient:
    """Fixed loopback HTTP client. No proxy env, redirects or automatic retries."""
    def __init__(self, token: str, port: int = 8773, timeout: float = 20.0):
        if not re.fullmatch(r"[\x21-\x7e]{32,4096}", token):
            raise ValueError("Invalid backend token")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid backend port")
        if not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Backend timeout must be in (0, 60]")
        self._token, self.port, self.timeout = token, port, timeout

    def request(self, method: str, path: str, *, payload=None, key=None):
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("Unsupported backend route")
        allowed = (method == "GET" and path in ("/v1/capabilities", "/v1/studio/catalog", "/v1/queue"))
        allowed |= method == "GET" and re.fullmatch(r"/v1/jobs/" + JOB_PATTERN, path) is not None
        allowed |= method == "POST" and path == "/v1/jobs"
        cancel = method == "POST" and re.fullmatch(r"/v1/jobs/" + JOB_PATTERN + r"/cancel", path) is not None
        allowed |= cancel
        if method == "GET" and parsed.path == "/v1/jobs":
            _query(parsed.query, {"limit", "cursor", "status", "action", "projectId"})
            allowed = True
        artifact = re.fullmatch(r"/v1/artifacts/(" + UUID_PATTERN + r")/manifest", parsed.path)
        if method == "GET" and artifact:
            artifact_request(artifact[1], parsed.query)
            allowed = True
        if not allowed:
            raise ValueError("Unsupported backend route")
        if method == "POST" and not cancel and (not isinstance(key, str) or not re.fullmatch(KEY_PATTERN, key)):
            raise ValueError("A valid original Idempotency-Key is required")
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        body = None
        if method == "POST":
            body = canonical_json(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if not cancel:
                headers["Idempotency-Key"] = key
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_BACKEND_BYTES + 1)
            if len(raw) > MAX_BACKEND_BYTES or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("Unexpected backend response")
            data = parse_json(raw)
            if not isinstance(data, dict) or not 200 <= response.status < 600 or 300 <= response.status < 400:
                raise ValueError("Unexpected backend response")
            return response.status, data, response.getheader("Idempotent-Replayed")
        except (OSError, ValueError, TypeError, RecursionError, http.client.HTTPException):
            raise BackendUnavailable("Backend did not return a reliable response") from None
        finally:
            connection.close()

    @contextmanager
    def artifact_stream(self, payload, range_header=None):
        path = "/v1/artifacts/" + payload["artifactId"] + "?" + urlencode({"projectId": payload["projectId"], "kind": payload["kind"]})
        artifact_request(payload["artifactId"], urlsplit(path).query)
        headers = {"Authorization": "Bearer " + self._token}
        if range_header is not None:
            if not isinstance(range_header, str) or len(range_header) > 100 or any(ord(c) < 32 for c in range_header):
                raise BackendUnavailable("Invalid artifact range")
            headers["Range"] = range_header
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            length = response.getheader("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,10}", length) or not 0 <= int(length) <= MAX_ARTIFACT_BYTES or response.status not in (200, 206, 400, 401, 404, 416, 503):
                raise BackendUnavailable("Unexpected artifact response")
            yield response
        except (OSError, ValueError, http.client.HTTPException):
            raise BackendUnavailable("Artifact response is unavailable") from None
        finally:
            connection.close()


class RPCError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def _rpc_error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class MCPService:
    def __init__(self, backend: BackendClient, token: str, payload_schema: dict,
                 allowed_origins=()):
        if not re.fullmatch(r"[\x21-\x7e]{32,4096}", token):
            raise ValueError("Invalid MCP token")
        if hmac.compare_digest(token, backend._token):
            raise ValueError("MCP and backend must use different token values")
        self.backend, self._token = backend, token
        self.public_path = os.environ.get("JIMENG_MCP_PUBLIC_PATH", "/mcp").rstrip("/")
        if not re.fullmatch(r"/[A-Za-z0-9/_-]{1,128}", self.public_path):
            raise ValueError("Public MCP path must be a bounded absolute URL path")
        self.allowed_origins = frozenset(allowed_origins)
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("Allowed Origin must contain only an HTTP(S) scheme and authority")
        readonly = {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": True}
        # Publish compact tool inputs: some enterprise MCP clients silently
        # drop tools containing a very large nested oneOf. The complete closed
        # operation schema remains authoritative at runtime and is discoverable
        # one action at a time, instead of weakening validation.
        self.payload_schema = deepcopy(payload_schema)
        self.action_schemas = {item["properties"]["action"]["const"]: item
                               for item in self.payload_schema["oneOf"]}
        self.tools = [
            {"name": "capabilities", "title": "即梦实际接口能力",
             "description": "查询已实现操作、只读开关与积分上限；先查能力再执行。可调用不等于真实生成已验收。",
             "inputSchema": _object(), "annotations": readonly},
            {"name": "studio_catalog", "title": "工作室功能与缺口目录",
             "description": "读取网页角色库、剧本、故事板等功能的证据与缺口。此目录仅供规划，不能把 implemented=false 的条目当可调用工具。",
             "inputSchema": _object(), "annotations": readonly},
            {"name": "action_schema", "title": "查询固定操作参数",
             "description": "按 action 名查询一项固定操作的完整参数 schema。提交前查此工具，严格按真实字段填写。",
             "inputSchema": _object({"action": {"type": "string", "minLength": 1, "maxLength": 128}}, ("action",)),
             "annotations": readonly},
            {"name": "submit_job", "title": "提交固定即梦操作",
             "description": "先用 action_schema 查询字段，再将 payload 提交为持久化 job。idempotencyKey 必填；超时复用原 key 和相同 payload。写入默认 dryRun=true，真实写入满足后端实时政策。COMPLETED 仅表示调用返回，须检查上游与素材状态；UNKNOWN 不得重新生成。",
             "inputSchema": _object({"idempotencyKey": {"type": "string", "minLength": 1,
                 "maxLength": 200, "pattern": "^" + KEY_PATTERN + "$"},
                 "payload": {"type": "object", "properties": {"action": {"type": "string"}},
                             "required": ["action"], "additionalProperties": True}}, ("idempotencyKey", "payload")),
             "annotations": {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": True}},
            {"name": "submit_job_json", "title": "提交固定即梦操作 JSON",
             "description": "与 submit_job 完全等价的简洁输入入口。先查 action_schema，将含 action 的完整参数对象序列化为 payloadJson。后端仍逐字段严格校验；超时必须复用原 idempotencyKey，不自动重提。",
             "inputSchema": _object({"idempotencyKey": {"type": "string", "minLength": 1,
                 "maxLength": 200, "pattern": "^" + KEY_PATTERN + "$"},
                 "payloadJson": {"type": "string", "minLength": 2, "maxLength": MAX_BODY_BYTES}}, ("idempotencyKey", "payloadJson")),
             "annotations": {"readOnlyHint": False, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": True}},
            {"name": "get_job", "title": "查询已有即梦任务",
             "description": "只查询原jobId；status表示调用状态，operationOutcome表示业务结果。COMPLETED不代表媒体完成；partial/unknown按recovery核查原节点、资源或submitId，不重提。",
             "inputSchema": _object({"jobId": {"type": "string", "pattern": "^" + JOB_PATTERN + "$"}}, ("jobId",)),
             "annotations": readonly},
            {"name": "list_jobs", "title": "分页查询即梦任务",
             "description": "按状态、action或项目分页查询持久化任务摘要；不返回提示词或媒体内容。继续翻页须复用相同筛选和nextCursor。",
             "inputSchema": _object({"limit": {"type": "integer", "minimum": 1, "maximum": 100},
                 "cursor": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,512}$"},
                 "status": {"type": "string", "enum": ["QUEUED", "RUNNING", "COMPLETED", "FAILED", "UNKNOWN"]},
                 "action": {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,128}$"},
                 "projectId": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,128}$"}}), "annotations": readonly},
            {"name": "queue_summary", "title": "查询即梦队列摘要",
             "description": "查询持久化状态计数、读写队列和最早排队时间；不发起上游操作。",
             "inputSchema": _object(), "annotations": readonly},
            {"name": "cancel_job", "title": "取消尚未执行的任务",
             "description": "只原子取消QUEUED任务。已经开始的任务返回冲突，不发送任何上游取消或重试。重复取消原任务安全。",
             "inputSchema": _object({"jobId": {"type": "string", "pattern": "^" + JOB_PATTERN + "$"}}, ("jobId",)),
             "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
            {"name": "artifact_manifest", "title": "获取成品下载清单",
             "description": "核验已完成的受管成品并返回SHA256、大小和受鉴权下载路径，支持Range；不把媒体Base64写入任务数据库，不返回凭据或服务器路径。下载使用已有MCP Bearer凭据。",
             "inputSchema": _object({"projectId": {"type": "string", "pattern": "^" + UUID_PATTERN + "$"},
                 "artifactId": {"type": "string", "pattern": "^" + UUID_PATTERN + "$"},
                 "kind": {"type": "string", "enum": ["resource", "director"]}}, ("projectId", "artifactId")), "annotations": readonly},
        ]
        self._tools = {tool["name"]: tool for tool in self.tools}

    def _sanitize(self, value):
        if isinstance(value, dict):
            return {key: ("[REDACTED]" if re.search(r"token|cookie|secret|authorization|signature", key, re.I)
                          else self._sanitize(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return value.replace(self._token, "[REDACTED]").replace(self.backend._token, "[REDACTED]")
        return value

    def _tool_result(self, data, error=False, version=PROTOCOL_VERSION):
        safe = self._sanitize(data)
        result = {"content": [{"type": "text", "text": canonical_json(safe)}], "isError": error}
        if version == PROTOCOL_VERSION:
            result["structuredContent"] = safe
        return result

    def call_tool(self, name, arguments, version=PROTOCOL_VERSION):
        if name not in self._tools:
            raise RPCError(-32602, "Unknown fixed tool")
        try:
            _validate(arguments, self._tools[name]["inputSchema"])
            if name in ("submit_job", "submit_job_json"):
                payload = (parse_json(arguments["payloadJson"].encode("utf-8"))
                           if name == "submit_job_json" else arguments["payload"])
                _validate(payload, self.payload_schema)
        except (ValueError, TypeError, RecursionError):
            return self._tool_result({"error": {"code": "INVALID_ARGUMENTS",
                "message": "Arguments did not match this tool's fixed input schema; no backend call was made."}}, True, version)
        method, path, extra = "GET", "", {}
        if name == "capabilities":
            path = "/v1/capabilities"
        elif name == "action_schema":
            schema = self.action_schemas.get(arguments["action"])
            return self._tool_result({"action": arguments["action"], "schema": schema,
                "error": None if schema else "Unsupported action"}, schema is None, version)
        elif name == "studio_catalog":
            path = "/v1/studio/catalog"
        elif name == "get_job":
            path = "/v1/jobs/" + arguments["jobId"]
        elif name == "list_jobs":
            path = "/v1/jobs" + ("?" + urlencode(arguments) if arguments else "")
        elif name == "queue_summary":
            path = "/v1/queue"
        elif name == "cancel_job":
            method, path, extra = "POST", "/v1/jobs/" + arguments["jobId"] + "/cancel", {"payload": {}}
        elif name == "artifact_manifest":
            path = "/v1/artifacts/" + arguments["artifactId"] + "/manifest?" + urlencode({"projectId": arguments["projectId"], "kind": arguments.get("kind", "resource")})
        elif name in ("submit_job", "submit_job_json"):
            method, path = "POST", "/v1/jobs"
            extra = {"payload": payload, "key": arguments["idempotencyKey"]}
        try:
            status, data, replayed = self.backend.request(method, path, **extra)
        except BackendUnavailable:
            return self._tool_result({"error": {"code": "BACKEND_UNAVAILABLE",
                "message": "Backend response is unavailable. No automatic retry was sent. For submit_job, retry only with the original idempotencyKey and identical payload."},
                "outcomeUnknown": method == "POST"}, True, version)
        result = {"bridgeHttpStatus": status, "data": data}
        if name == "artifact_manifest" and status == 200:
            data["downloadRoute"] = self.public_path + "/artifacts/" + arguments["artifactId"] + "?" + urlencode({"projectId": arguments["projectId"], "kind": arguments.get("kind", "resource")})
            data.pop("authorization", None)
            data["requiresBearerAuth"] = True
            data["credentialSource"] = "Use the existing MCP Bearer credential; it is never included in this result"
        if replayed in ("true", "false"):
            result["idempotentReplayed"] = replayed == "true"
        business_error = name in ("submit_job", "submit_job_json", "get_job") and data.get("operationOutcome") in ("rejected", "partial", "unknown")
        return self._tool_result(result, not 200 <= status < 300 or business_error, version)

    def dispatch(self, message, version=PROTOCOL_VERSION):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _rpc_error(None, -32600, "Invalid JSON-RPC request")
        request_id = message.get("id")
        if "id" in message and type(request_id) not in (str, int):
            return _rpc_error(None, -32600, "Request id must be a string or integer")
        method = message.get("method")
        if method is None and "id" in message and (("result" in message) != ("error" in message)):
            # There are no server-initiated requests; accepting a well-formed
            # response performs no action and has the transport's 202 response.
            return None
        if not isinstance(method, str) or any(key in message for key in ("result", "error")):
            return _rpc_error(request_id, -32600, "Invalid JSON-RPC request")
        if "id" not in message:
            # Notifications never dispatch tools. In particular, cancellation
            # cannot remove or resubmit an already persisted backend job.
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Params must be an object")
        try:
            if method == "initialize":
                client = params.get("clientInfo")
                if (not isinstance(params.get("protocolVersion"), str)
                        or not isinstance(params.get("capabilities"), dict)
                        or not isinstance(client, dict)
                        or not isinstance(client.get("name"), str)
                        or not isinstance(client.get("version"), str)):
                    raise RPCError(-32602, "Initialize requires protocolVersion, capabilities and clientInfo")
                requested = params["protocolVersion"]
                result = {"protocolVersion": requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "jimeng-private-bridge", "version": "0.2.0"},
                    "instructions": "先调用capabilities读取实时策略，用action_schema取得准确参数。提交保存jobId和原idempotencyKey；超时只复用原key与相同参数。status仅表示调用状态，须看operationOutcome；partial/unknown按recovery核查原节点、资源或submitId，不能重提。list_jobs/queue_summary用于找回任务；cancel_job仅取消未执行任务。成品用artifact_manifest及受鉴权下载路径，避免Base64搬运。COMPLETED或dry-run不能被描述为媒体生成成功。"}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                if set(params) - {"_meta", "cursor"} or params.get("cursor") is not None:
                    raise RPCError(-32602, "This fixed tool list has no pagination cursor")
                result = {"tools": deepcopy(self.tools)}
            elif method == "tools/call":
                if set(params) - {"name", "arguments", "_meta"} or not isinstance(params.get("name"), str):
                    raise RPCError(-32602, "Tool call requires a fixed tool name")
                result = self.call_tool(params["name"], params.get("arguments", {}), version)
            else:
                raise RPCError(-32601, "Method not found")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RPCError as exc:
            return _rpc_error(request_id, exc.code, exc.message)


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def make_server(service: MCPService, host="127.0.0.1", port=8774):
    if not ipaddress.ip_address(host).is_loopback:
        raise ValueError("MCP listener must bind a loopback address; publish through a TLS reverse proxy")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        server_version = "JimengMCP/1"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, fmt, *args):
            # Never log paths, headers, tokens, payloads or backend diagnostics.
            pass

        def reply(self, status, value=None, extra=None):
            raw = b"" if value is None else canonical_json(value).encode("utf-8")
            self.send_response(status)
            if value is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def authorize(self):
            headers = self.headers.get_all("Authorization", [])
            scheme, _, token = headers[0].partition(" ") if len(headers) == 1 else ("", "", "")
            if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode("utf-8"), service._token.encode("ascii")):
                raise HTTPError(401, "A valid MCP Bearer token is required")

        def check_origin(self):
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] not in service.allowed_origins):
                raise HTTPError(403, "Origin is not allowed")

        def read_body(self):
            if self.headers.get_all("Transfer-Encoding", []):
                raise HTTPError(400, "Transfer-Encoding is not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise HTTPError(411, "Content-Length is required")
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise HTTPError(400, "Invalid Content-Length")
            length = int(lengths[0])
            if length > MAX_BODY_BYTES:
                raise HTTPError(413, "Request exceeds the size limit")
            content_types = self.headers.get_all("Content-Type", [])
            if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise HTTPError(415, "Use application/json")
            accepted = {item.split(";", 1)[0].strip().lower()
                        for item in self.headers.get("Accept", "").split(",")}
            if not {"application/json", "text/event-stream"} <= accepted:
                raise HTTPError(406, "Accept must include application/json and text/event-stream")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise HTTPError(400, "Incomplete request body")
            try:
                return parse_json(raw)
            except (ValueError, UnicodeError, TypeError, RecursionError):
                raise RPCError(-32700, "Parse error") from None

        def handle_request(self):
            media_started = False
            try:
                self.check_origin()
                if self.path == "/healthz" and self.command == "GET":
                    self.reply(200, {"status": "ok", "protocolVersion": PROTOCOL_VERSION})
                    return
                self.authorize()
                parsed = urlsplit(self.path)
                artifact_match = re.fullmatch(r"/mcp/artifacts/(" + UUID_PATTERN + r")", parsed.path)
                if self.command == "GET" and artifact_match:
                    try:
                        payload = artifact_request(artifact_match[1], parsed.query)
                    except APIError as exc:
                        raise HTTPError(exc.status, exc.message) from None
                    ranges = self.headers.get_all("Range", [])
                    if len(ranges) > 1:
                        raise HTTPError(416, "Only one Range header is accepted")
                    with service.backend.artifact_stream(payload, ranges[0] if ranges else None) as response:
                        if response.status not in (200, 206):
                            raw = response.read(8193)
                            try:
                                value = parse_json(raw) if len(raw) <= 8192 else None
                            except (ValueError, UnicodeError):
                                value = None
                            extra = {"Content-Range": response.getheader("Content-Range")} if response.getheader("Content-Range") else None
                            self.reply(response.status, service._sanitize(value) if isinstance(value, dict) else {"error": "Artifact unavailable"}, extra)
                            return
                        self.send_response(response.status)
                        for header in ("Content-Type", "Content-Length", "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag"):
                            value = response.getheader(header)
                            if value is not None:
                                self.send_header(header, value)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("X-Content-Type-Options", "nosniff")
                        self.end_headers()
                        media_started = True
                        remaining = int(response.getheader("Content-Length"))
                        while remaining:
                            data = response.read(min(65536, remaining))
                            if not data:
                                self.close_connection = True
                                return
                            self.wfile.write(data)
                            remaining -= len(data)
                    return
                if self.path != "/mcp":
                    raise HTTPError(404, "MCP endpoint is /mcp")
                versions = self.headers.get_all("MCP-Protocol-Version", [])
                if len(versions) > 1 or (versions and versions[0] not in SUPPORTED_VERSIONS):
                    raise HTTPError(400, "Unsupported MCP-Protocol-Version")
                version = versions[0] if versions else "2025-03-26"
                if self.command != "POST":
                    self.reply(405, {"error": "Only POST is supported; no SSE stream or sessions are exposed"}, {"Allow": "POST"})
                    return
                message = self.read_body()
                if isinstance(message, list):
                    if version != "2025-03-26" or not 1 <= len(message) <= 32:
                        self.reply(400, _rpc_error(None, -32600, "This protocol version requires a single JSON-RPC message"))
                        return
                    # Compatibility with pre-2025-06-18 batching; execute once
                    # in order, retaining each caller's own idempotency key.
                    responses = [service.dispatch(item, version) for item in message]
                    responses = [item for item in responses if item is not None]
                    self.reply(200, responses) if responses else self.reply(202)
                else:
                    response = service.dispatch(message, version)
                    self.reply(200, response) if response is not None else self.reply(202)
            except HTTPError as exc:
                self.reply(exc.status, {"error": {"message": exc.message}},
                           {"WWW-Authenticate": 'Bearer realm="jimeng-mcp"'} if exc.status == 401 else None)
            except RPCError as exc:
                self.reply(400, _rpc_error(None, exc.code, exc.message))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except BackendUnavailable:
                if media_started:
                    self.close_connection = True
                else:
                    self.reply(503, {"error": "Artifact backend response unavailable; no retry was sent"})
            except Exception:
                if media_started:
                    self.close_connection = True
                else:
                    self.reply(500, _rpc_error(None, -32603, "Internal error. For submit_job, retain the original idempotencyKey; no automatic retry was sent."))

        do_POST = handle_request
        do_GET = handle_request
        do_DELETE = handle_request
        do_OPTIONS = handle_request
        do_PUT = handle_request
        do_PATCH = handle_request

    return LimitedHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--secret-file", required=True, help="Private MCP access token file")
    parser.add_argument("--backend-secret-file", required=True, help="Separate private bridge token file")
    parser.add_argument("--backend-port", type=int, default=8773)
    parser.add_argument("--backend-timeout", type=float, default=20)
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--openapi", default=str(Path(__file__).with_name("openapi.json")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        backend = BackendClient(load_private_token(args.backend_secret_file), args.backend_port, args.backend_timeout)
        service = MCPService(backend, load_private_token(args.secret_file),
                             load_payload_schema(args.openapi), args.allowed_origin)
        httpd = make_server(service, args.host, args.port)
    except (ValueError, KeyError, TypeError, OSError):
        parser.error("Invalid server configuration, private token files, or bundled OpenAPI schema; no token values are displayed")
    LOG.info("Jimeng MCP listening on %s:%s", args.host, httpd.server_port)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
