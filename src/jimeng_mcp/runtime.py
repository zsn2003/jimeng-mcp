"""MCP stdio transport backed by an in-process durable job queue.

No network listener, shared account, reverse proxy or HTTP credential is needed.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import tempfile
from urllib.parse import urlsplit

from . import __version__
from .bridge_loader import load_bridge, BRIDGE_ROOT
from .config import ensure_private_dir


class ShutdownRequested(BaseException):
    pass


class LocalBackend:
    def __init__(self, service, server, data_dir):
        self.service, self.server, self.data_dir = service, server, data_dir
        # The shared RPC result scrubber expects two distinct sentinel values.
        # These are process-local random values, never credentials or listeners.
        self._token = secrets.token_urlsafe(48)

    def request(self, method, route, *, payload=None, key=None):
        parsed = urlsplit(route)
        path, query = parsed.path, parsed.query
        try:
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise self.server.APIError(400, 'INVALID_LOCAL_ROUTE', 'Unsupported internal route')
            if method == 'GET' and route == '/v1/capabilities':
                result = self.service.capabilities()
                result['localRuntime'] = {'transport': 'stdio', 'networkListener': False,
                    'accountSource': 'Your own official CLI login; web features use a separate optional local session',
                    'onboardingCommand': 'jimeng-mcp auth --help', 'spendingCommand': 'jimeng-mcp config --help',
                    'localArtifactExport': True, 'liveAcceptanceBundled': False}
            elif method == 'GET' and route == '/v1/studio/catalog':
                result = self.service.studio_catalog()
            elif method == 'GET' and route == '/v1/queue':
                result = self.service.queue_summary()
            elif method == 'GET' and path == '/v1/jobs':
                filters = self.server._query(query, {'limit', 'cursor', 'status', 'action', 'projectId'})
                if 'limit' in filters:
                    if not re.fullmatch(r'[0-9]{1,3}', filters['limit']):
                        raise self.server.APIError(400, 'INVALID_LIMIT', 'Invalid limit')
                    filters['limit'] = int(filters['limit'])
                result = self.service.list_jobs(**filters)
            elif method == 'GET' and not query and re.fullmatch(r'/v1/jobs/[0-9a-f]{32}', path):
                result = self.service.get_job(path.rsplit('/', 1)[1])
            elif method == 'POST' and route == '/v1/jobs':
                job, created = self.service.submit(payload, key)
                return (202 if created else 200), job, ('false' if created else 'true')
            elif method == 'POST' and not query and re.fullmatch(r'/v1/jobs/[0-9a-f]{32}/cancel', path) and payload == {}:
                result = self.service.cancel_job(path.split('/')[3])
            else:
                raise self.server.APIError(404, 'LOCAL_ROUTE_NOT_FOUND', 'Unsupported internal route')
            return 200, result, None
        except self.server.APIError as exc:
            error = {'code': exc.code, 'message': exc.message}
            if exc.details:
                error['details'] = exc.details
            return exc.status, {'error': error}, None

    def export_artifact(self, payload):
        destination = ensure_private_dir(self.data_dir / 'exports')
        # Use the SAME verified open file for both content and metadata. Never
        # resolve the descriptor again between checking and exporting.
        with self.service.artifact(payload) as (source, manifest):
            target = destination / (payload['artifactId'] + '.' + manifest['format'].lower())
            fd, temporary = tempfile.mkstemp(prefix='.artifact-', dir=destination)
            try:
                digest = hashlib.sha256(); size = 0
                with os.fdopen(fd, 'wb') as out:
                    while block := source.read(65536):
                        size += len(block)
                        if size > manifest['byteSize']:
                            raise ValueError('Artifact changed during export')
                        digest.update(block); out.write(block)
                    if size != manifest['byteSize'] or digest.hexdigest() != manifest['sha256']:
                        raise ValueError('Artifact changed during export')
                    out.flush(); os.fsync(out.fileno())
                os.replace(temporary, target)
                folder_fd = os.open(destination, os.O_RDONLY)
                try: os.fsync(folder_fd)
                finally: os.close(folder_fd)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return {**manifest, 'localPath': str(target), 'transport': 'local-file', 'requiresBearerAuth': False}


class LocalRuntime:
    def __init__(self, data_dir: Path, *, adapter_override=None):
        adapter, server, mcp = load_bridge()
        self.mcp, self.server = mcp, server
        self.service = server.JobService(data_dir / 'jobs.sqlite3', adapter_override or adapter, secrets.token_urlsafe(48))
        backend = LocalBackend(self.service, server, data_dir)
        try:
            schema = mcp.load_payload_schema(BRIDGE_ROOT / 'openapi.json')

            class LocalRPC(mcp.MCPService):
                def call_tool(self, name, arguments, version=mcp.PROTOCOL_VERSION):
                    if name != 'artifact_manifest':
                        return super().call_tool(name, arguments, version)
                    try:
                        mcp._validate(arguments, self._tools[name]['inputSchema'])
                    except (ValueError, TypeError, RecursionError):
                        return self._tool_result({'error': {'code': 'INVALID_ARGUMENTS', 'message': 'Use a canonical project/artifact identity'}}, True, version)
                    try:
                        result = self.backend.export_artifact({**arguments, 'kind': arguments.get('kind', 'resource')})
                        return self._tool_result({'data': result}, version=version)
                    except Exception:
                        return self._tool_result({'error': {'code': 'ARTIFACT_NOT_AVAILABLE', 'message': 'No verified local artifact matches this identity'}}, True, version)

                def dispatch(self, message, version=mcp.PROTOCOL_VERSION):
                    result = super().dispatch(message, version)
                    if isinstance(message, dict) and message.get('method') == 'initialize' and isinstance(result, dict) and 'result' in result:
                        result['result']['serverInfo'] = {'name': 'jimeng-mcp', 'version': __version__}
                        result['result']['instructions'] = ('Use capabilities and action_schema before submitting. Every user logs into their own official CLI from a local terminal. '
                            'Writes default to dryRun and generation credits default to zero; the user configures spending locally. '
                            'Keep jobId and idempotencyKey, inspect operationOutcome, and reconcile unknown results without resubmitting. '
                            'artifact_manifest exports a verified local file and returns localPath. This server does not listen on a network port. '
                            'Web Studio and personal-subject features require a separately supplied local web session; CLI OAuth alone does not authorize them.')
                    return result

            self.rpc = LocalRPC(backend, secrets.token_urlsafe(48), schema)
            for tool in self.rpc.tools:
                tool['annotations'] = dict(tool['annotations'])
                if tool['name'] in ('submit_job', 'submit_job_json'):
                    tool['annotations']['destructiveHint'] = True
                if tool['name'] == 'artifact_manifest':
                    tool['title'] = '导出已验证成品到本地'
                    tool['description'] = '核验已有受管成品并导出到本机私有目录，返回localPath、大小和SHA256。只接受项目与成品ID，不接受任意路径。无需HTTP或Bearer凭据。'
                    tool['annotations']['readOnlyHint'] = False
                    tool['annotations']['openWorldHint'] = False
            self.service.start_worker()
        except BaseException:
            self.service.close()
            raise
        self.version = mcp.PROTOCOL_VERSION
        self.initialized = False
        self.ready = False

    def dispatch(self, message):
        if isinstance(message, dict) and message.get('jsonrpc') == '2.0':
            method = message.get('method')
            if method == 'notifications/initialized' and 'id' not in message:
                self.ready = self.initialized
                return None
            if method == 'initialize' and self.initialized:
                return self.mcp._rpc_error(message.get('id'), -32600, 'Already initialized')
            if method not in ('initialize', 'ping') and 'id' in message and not self.ready:
                return self.mcp._rpc_error(message.get('id'), -32002, 'Initialize and send notifications/initialized first')
        response = self.rpc.dispatch(message, self.version)
        if isinstance(message, dict) and message.get('method') == 'initialize' and response and 'result' in response:
            self.initialized = True
            self.version = response['result']['protocolVersion']
        return response

    def close(self):
        # EOF or SIGTERM stops new queue claims. Keep the lock while already
        # launched work finishes; never turn a disconnect into a new submission.
        reported = False
        while not self.service.close(timeout=5):
            if not reported:
                print('Waiting for the already-started operation to finish; no new work will be claimed.', file=sys.stderr)
                reported = True


def serve(data_dir: Path, *, input_stream=None, output_stream=None):
    incoming = input_stream if input_stream is not None else sys.stdin.buffer
    outgoing = output_stream if output_stream is not None else sys.stdout.buffer
    runtime = LocalRuntime(data_dir)
    previous = {}

    def stop(signum, frame):
        raise ShutdownRequested()

    try:
        for name in ('SIGINT', 'SIGTERM'):
            signum = getattr(signal, name, None)
            if signum is not None:
                previous[signum] = signal.signal(signum, stop)
        while True:
            raw = incoming.readline(runtime.mcp.MAX_BODY_BYTES + 1)
            if not raw:
                break
            if len(raw) > runtime.mcp.MAX_BODY_BYTES:
                response = runtime.mcp._rpc_error(None, -32700, 'Message exceeds size limit')
                outgoing.write((runtime.mcp.canonical_json(response) + '\n').encode('utf-8')); outgoing.flush()
                break
            try:
                message = runtime.mcp.parse_json(raw)
                response = runtime.dispatch(message)
            except (ValueError, UnicodeError, TypeError, RecursionError):
                response = runtime.mcp._rpc_error(None, -32700, 'Invalid JSON-RPC message')
            except Exception:
                response = runtime.mcp._rpc_error(None, -32603, 'Local operation failed; inspect the original job before retrying')
            if response is not None:
                outgoing.write((runtime.mcp.canonical_json(response) + '\n').encode('utf-8')); outgoing.flush()
    except (ShutdownRequested, BrokenPipeError):
        pass
    finally:
        # A second SIGTERM cannot make us abandon an active operation and its lock.
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        runtime.close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0
