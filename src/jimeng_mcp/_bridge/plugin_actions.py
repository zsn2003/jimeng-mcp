"""Fixed extensions supplied by the official Dreamina generation CLI 1.4.22.

This is independent of dreamina-canvas and its OAuth state. No login, arbitrary
command, caller URL/path, or file-download destination is exposed. Media paths
come only from an injected managed-artifact resolver. Mutations default to a
bridge-local plan: the official commands have no dry-run flag. Multiframe is
ordinary generation, never an official draft/sample, and has no quote/ceiling
facility. The dispatcher's project policy must approve it before execution.
"""
from __future__ import annotations
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import web_actions as web

VERSION = '1.4.22'
CONTRACT = 'contracts/official-plugin-command-contracts.json'
MULTIFRAME = 'plugin.video.multiframe'
MAX_OUTPUT = 4 * 1024 * 1024
MAX_MEDIA_BYTES = 30 * 1024 * 1024  # Bridge limit; not an upstream quota claim.
ID = {'type': 'integer', 'minimum': 0, 'maximum': 9007199254740991}
POS_ID = {**ID, 'minimum': 1}
TOKEN = web._str(128, pattern=r'[A-Za-z0-9_-]+')
NAME = web._str(50)
TEXT = web._str(10000, allowText=True)
DRY = {'type': 'boolean', 'default': True}
PROJECT = web._str(36, pattern=r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
DURATION = {'type': 'number', 'minimum': 1, 'maximum': 8}
ACTIONS = {
    MULTIFRAME: {'command': ['multiframe2video'], 'writes': True, 'billable': True,
        'required': ['projectId', 'artifactIds', 'resolution'],
        'fields': {'projectId': PROJECT, 'artifactIds': {'type': 'array', 'minItems': 2, 'maxItems': 20, 'items': TOKEN},
            'resolution': {'type': 'string', 'enum': ['720p', '1080p']},
            'prompt': TEXT, 'duration': DURATION,
            'transitionPrompts': {'type': 'array', 'minItems': 1, 'maxItems': 19, 'items': TEXT},
            'transitionDurations': {'type': 'array', 'minItems': 1, 'maxItems': 19, 'items': DURATION},
            'sessionId': {**ID, 'default': 0}, 'dryRun': DRY}},
    'plugin.session.create': {'command': ['session', 'create'], 'writes': True, 'fields': {'name': NAME, 'dryRun': DRY}},
    'plugin.session.list': {'command': ['session', 'list'], 'fields': {'limit': web._int(1, 100, default=30)}},
    'plugin.session.search': {'command': ['session', 'search'], 'required': ['name'], 'fields': {'name': NAME}},
    'plugin.session.rename': {'command': ['session', 'rename'], 'writes': True, 'required': ['sessionId', 'name'],
        'fields': {'sessionId': POS_ID, 'name': NAME, 'dryRun': DRY}},
    'plugin.session.delete': {'command': ['session', 'delete'], 'writes': True, 'required': ['sessionId'],
        'fields': {'sessionId': POS_ID, 'dryRun': DRY}},
    'plugin.task.list': {'command': ['list_task'], 'fields': {'status': TOKEN, 'taskType': TOKEN,
        'submitId': TOKEN, 'limit': web._int(1, 100, default=20), 'offset': web._int(0, 1000000, default=0)}},
    'plugin.task.read': {'command': ['query_result'], 'required': ['submitId'], 'fields': {'submitId': TOKEN}},
}


def supports(action):
    return isinstance(action, str) and action in ACTIONS


def request_schemas():
    return {name: {'type': 'object', 'additionalProperties': False,
        'properties': {'action': {'type': 'string', 'const': name}, **deepcopy(spec['fields'])},
        'required': ['action', *spec.get('required', [])],
        'description': ('Official ordinary fixed-model multi-frame generation. 2-20 managed images; no sample mode. Exactly two images require prompt, optionally duration; 3+ require N-1 transitionPrompts and optional N-1 transitionDurations. No generation during dryRun.' if name == MULTIFRAME else
            'Search is case-sensitive substring matching of only the first 100 sessions.' if name == 'plugin.session.search' else
            'Soft delete: moves history into default session 0; default session cannot be deleted.' if name == 'plugin.session.delete' else
            'Official Dreamina CLI session or task operation. This account state is separate from dreamina-canvas.')}
        for name, spec in ACTIONS.items()}


def validate(payload):
    if not isinstance(payload, dict) or not supports(payload.get('action')):
        raise ValueError('Unsupported official plugin operation')
    schema = request_schemas()[payload['action']]
    web._check(payload, schema, 'plugin request')
    if 'name' in payload and (not payload['name'].strip() or payload['name'] != payload['name'].strip()):
        raise ValueError('Session name must be nonempty and trimmed')
    if 'projectId' in payload and str(uuid.UUID(payload['projectId'])) != payload['projectId']:
        raise ValueError('projectId must be a canonical UUID')
    if payload['action'] == MULTIFRAME:
        count = len(payload['artifactIds'])
        if count == 2:
            if 'prompt' not in payload or any(k in payload for k in ('transitionPrompts', 'transitionDurations')):
                raise ValueError('Two images require prompt and optional duration, without transition arrays')
            durations = [payload.get('duration', 3)]
        else:
            if 'prompt' in payload or 'duration' in payload or len(payload.get('transitionPrompts', [])) != count - 1:
                raise ValueError('For N>=3 images provide exactly N-1 transitionPrompts without shorthand prompt/duration')
            durations = payload.get('transitionDurations', [3] * (count - 1))
            if len(durations) != count - 1:
                raise ValueError('For N images provide exactly N-1 transitionDurations or omit the array')
        if sum(durations) < 2:
            raise ValueError('Multi-frame total duration must be at least two seconds')
    if is_mutating(payload['action']) and not payload.get('dryRun', True):
        if web.read_only():
            raise ValueError('Service is read-only; live plugin mutations are disabled')
        if payload['action'] == MULTIFRAME and os.environ.get('JIMENG_MAX_CREDITS', '0').strip() != 'unlimited':
            raise ValueError('Official plugin cannot enforce a numeric credit ceiling; ordinary generation requires explicit unlimited service policy')


def is_mutating(action):
    return bool(ACTIONS.get(action, {}).get('writes'))


def effects(action):
    spec = ACTIONS.get(action, {})
    return {'mutatesWorkspace': bool(spec.get('writes')), 'billable': bool(spec.get('billable')),
        'sampleSupported': False, 'authorizesSpending': bool(spec.get('billable')),
        'readOnlyUpstream': not spec.get('writes', False)}


def binary():
    return os.environ.get('DREAMINA_PLUGIN_BIN') or shutil.which('dreamina')


def capabilities():
    exe = binary()
    configured = bool(exe and Path(exe).is_file() and os.access(exe, os.X_OK))
    return {'backend': 'official-dreamina-cli', 'contractVersion': VERSION,
        'authentication': 'Separate OAuth Device Flow; operator provisions official CLI state. Login is not exposed as a tool.',
        'actions': {name: {'implemented': True, 'available': configured, 'contractVerified': True,
            'contractEvidence': CONTRACT, 'defaultDryRun': True if is_mutating(name) else None,
            'dryRunKind': 'bridge-local-plan' if is_mutating(name) else None,
            'cliConfigured': configured, 'authenticationCheck': 'not-performed-by-capabilities', **effects(name),
            **({'managedMediaRequired': True, 'ordinaryGenerationOnly': True, 'fixedModel': True,
                'numericCreditCeilingSupported': False, 'generationLiveVerified': False} if name == MULTIFRAME else {})}
            for name in ACTIONS}}


def recovery(payload, job_id):
    if payload.get('action') == 'plugin.task.read':
        return {'submitId': payload['submitId'], 'query': {'action': 'plugin.task.read', 'submitId': payload['submitId']}}
    if not is_mutating(payload.get('action')) or payload.get('dryRun', True):
        return None
    return {'jobId': job_id, 'upstreamIdentityKnownBeforeSubmit': False, 'upstreamIdempotencyVerified': False,
        'doNotResubmit': True, 'nextAction': 'plugin.task.list' if payload['action'] == MULTIFRAME else 'plugin.session.list',
        **({'sessionId': payload.get('sessionId', 0)} if payload['action'] == MULTIFRAME else {}),
        'note': 'Preserve the original receipt. After timeout inspect the saved submit_id/session ID and history before any new mutation; a new job ID is not a retry.'}


def _sanitize(value, private_paths=()):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'token|cookie|secret|device.?code|authorization|signature|(?:^|_)(?:file_?)?path$', str(k), re.I)
                else _sanitize(v, private_paths) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, private_paths) for v in value]
    if isinstance(value, str):
        for path in sorted(private_paths, key=len, reverse=True):
            value = value.replace(path, '[MANAGED_IMAGE]')
        value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~-]+', r'\1[REDACTED]', value)
        value = re.sub(r'(?i)((?:access_token|refresh_token|sessionid|device_code|cookie|authorization)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', value)
    return value


def _json_output(text):
    # Some builds print a version warning before their JSON result.
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^\s*[\[{]', text):
        try:
            data, end = decoder.raw_decode(text[match.start():].lstrip())
            if isinstance(data, (dict, list)):
                return data
        except ValueError:
            pass
    return None


def _session_output(args, text, exit_code):
    """Parse only shapes captured from official 1.4.22, not guessed JSON IDs."""
    if len(args) < 2 or args[0] != 'session':
        return None
    command = args[1]
    stripped = text.strip()
    if command == 'search' and exit_code == 1 and re.fullmatch(r'No sessions found matching ".*"', stripped):
        return {'ok': True, 'exitCode': exit_code, 'format': 'normalized-text',
            'data': {'items': [], 'matchedWithinFirst': 100, 'searchCaseSensitive': True}, 'emptySearch': True}
    if exit_code != 0:
        return None
    patterns = {
        'create': r'Created session "(.*)" \(ID: ([0-9]+)\)',
        'rename': r'Renamed session ([0-9]+) to "(.*)"',
        'delete': r'Deleted session ([0-9]+)',
    }
    if command in patterns:
        match = re.fullmatch(patterns[command], stripped)
        if not match:
            return None
        if command == 'create':
            data = {'name': match.group(1), 'sessionId': int(match.group(2))}
        else:
            data = {'sessionId': int(match.group(1))}
            if command == 'rename':
                data['name'] = match.group(2)
            else:
                data.update(deleted=True, softDelete=True, historyDestinationSessionId=0)
        return {'ok': True, 'exitCode': exit_code, 'format': 'normalized-text', 'data': data}
    if command not in ('list', 'search') or not re.search(r'(?m)^ID\s+NAME\s+(?:PINNED\s+)?UPDATED_AT\s*$', stripped):
        return None
    items = []
    for line in stripped.splitlines():
        if not re.match(r'^\d+\s', line):
            continue
        pattern = (r'^([0-9]+)\s+(.*?)\s+(Yes|No)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$'
            if command == 'list' else r'^([0-9]+)\s+(.*?)\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*$')
        match = re.fullmatch(pattern, line)
        if not match:
            return None
        item = {'sessionId': int(match.group(1)), 'name': match.group(2), 'updatedAtLocal': match.group(4 if command == 'list' else 3)}
        if command == 'list':
            item['pinned'] = match.group(3) == 'Yes'
        items.append(item)
    data = {'items': items}
    if command == 'search':
        data.update(matchedWithinFirst=100, searchCaseSensitive=True)
    return {'ok': True, 'exitCode': exit_code, 'format': 'normalized-text', 'data': data}


def _invoke(args):
    exe = binary()
    if not exe:
        return {'ok': False, 'dispatchAttempted': False, 'error': {'code': 'PLUGIN_CLI_UNAVAILABLE', 'message': 'Official dreamina CLI is not configured'}}
    if not Path(exe).is_absolute():
        raise ValueError('DREAMINA_PLUGIN_BIN must resolve to an absolute official executable')
    try:
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=75, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Official plugin timed out; inspect the original operation before resubmission') from None
    except OSError:
        return {'ok': False, 'dispatchAttempted': False, 'error': {'code': 'PLUGIN_CLI_UNAVAILABLE', 'message': 'Official dreamina executable could not be started'}}
    if len(proc.stdout) + len(proc.stderr) > MAX_OUTPUT:
        raise RuntimeError('Official plugin response exceeds the response limit; inspect the original operation')
    if any(marker in (proc.stdout + proc.stderr) for marker in ('未检测到有效登录态', 'not logged in', 'login required')):
        return {'ok': False, 'exitCode': proc.returncode, 'dispatchAttempted': False,
            'error': {'code': 'PLUGIN_AUTH_REQUIRED', 'message': 'Official plugin OAuth login is required'}}
    session_result = _session_output(args, proc.stdout or proc.stderr, proc.returncode)
    if session_result is not None:
        return session_result
    data = _json_output(proc.stdout)
    if data is None:
        data = _json_output(proc.stderr)
    if data is not None:
        ok = proc.returncode == 0
        if isinstance(data, dict):
            if type(data.get('ok')) is bool:
                ok = ok and data['ok']
            for key in ('ret', 'code', 'status_code'):
                if key in data and isinstance(data[key], (int, str)) and str(data[key]) not in ('0', '200'):
                    ok = False
        return {'ok': ok, 'exitCode': proc.returncode, 'format': 'json', 'data': data}
    # Session commands use human-readable tables. Preserve exact noncredential
    # output without guessing IDs; exit success is not media success.
    return {'ok': proc.returncode == 0, 'exitCode': proc.returncode, 'format': 'text',
        'data': {'text': proc.stdout.strip() or proc.stderr.strip()}, 'businessStatusUnparsed': True}


def _managed_paths(payload, resolver):
    if resolver is None:
        raise ValueError('Managed image artifact resolver is required')
    paths = []
    for artifact_id in payload['artifactIds']:
        item = resolver(payload['projectId'], artifact_id)
        if (not isinstance(item, dict) or item.get('mimeType') not in ('image/png', 'image/jpeg', 'image/webp')
                or type(item.get('byteSize')) is not int or not 0 < item['byteSize'] <= MAX_MEDIA_BYTES
                or not isinstance(item.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
            raise ValueError('Resolver must return a verified bounded managed image artifact')
        path = item.get('path')
        if (not isinstance(path, str) or not Path(path).is_absolute() or any(c in path for c in ',\n\r\x00')
                or '://' in path):
            raise ValueError('Resolver did not return a supported managed local image path')
        paths.append(path)
    return paths


def _compile(payload, paths=()):
    action = payload['action']
    args = list(ACTIONS[action]['command'])
    if action == MULTIFRAME:
        args += ['--images=' + ','.join(paths), '--video_resolution=' + payload['resolution'],
            '--session=' + str(payload.get('sessionId', 0)), '--poll=0']
        if len(paths) == 2:
            args += ['--prompt=' + payload['prompt'], '--duration=' + str(payload.get('duration', 3))]
        else:
            args += ['--transition-prompt=' + text for text in payload['transitionPrompts']]
            if 'transitionDurations' in payload:
                args += ['--transition-duration=' + str(seconds) for seconds in payload['transitionDurations']]
    elif action == 'plugin.session.list':
        args += ['--max-count=' + str(payload.get('limit', 30))]
    elif action in ('plugin.session.create', 'plugin.session.search'):
        if 'name' in payload:
            args += ['--', payload['name']]
    elif action in ('plugin.session.rename', 'plugin.session.delete'):
        args += ['--', str(payload['sessionId'])]
        if 'name' in payload:
            args.append(payload['name'])
    else:
        for key, flag in (('status', 'gen_status'), ('taskType', 'gen_task_type'), ('submitId', 'submit_id'), ('limit', 'limit'), ('offset', 'offset')):
            if key in payload:
                args.append('--' + flag + '=' + str(payload[key]))
    return args


def execute(payload, job_id, *, invoke=None, resolve_media=None):
    validate(payload)
    action = payload['action']
    dry = is_mutating(action) and payload.get('dryRun', True)
    base = {'action': action, 'dryRun': dry, 'backend': 'official-dreamina-cli',
        'generationSubmitted': False, 'workspaceMutationAttempted': False,
        'cliInvoked': False, 'dispatchAttempted': False, 'operationOutcome': 'planned' if dry else 'unknown',
        'recovery': recovery(payload, job_id)}
    if dry:
        return {**base, 'dryRunKind': 'bridge-local-plan',
            'plan': {'command': ACTIONS[action]['command'], **{k: v for k, v in payload.items() if k not in ('action', 'dryRun')}},
            'note': 'No official command invoked and no media opened/uploaded. Multi-frame remains ordinary generation, not sample mode.'}
    paths = _managed_paths(payload, resolve_media) if action == MULTIFRAME else []
    upstream = _sanitize((invoke or _invoke)(_compile(payload, paths)), paths)
    if not isinstance(upstream, dict) or type(upstream.get('ok')) is not bool:
        raise RuntimeError('Official plugin returned an unsupported response envelope')
    dispatched = upstream.get('dispatchAttempted') is not False
    result = {**base, 'cliInvoked': True, 'dispatchAttempted': is_mutating(action) and dispatched,
        'operationOutcome': ('succeeded' if upstream['ok'] and action != MULTIFRAME and not upstream.get('businessStatusUnparsed') else 'rejected' if not dispatched else 'unknown'),
        'workspaceMutationAttempted': is_mutating(action) and dispatched, 'upstream': upstream,
        'generationSubmitted': None if action == MULTIFRAME else False,
        'note': 'CLI process completion is not generation completion; query the returned submit_id with plugin.task.read.'}
    if action == MULTIFRAME:
        data = upstream.get('data')
        submit = data.get('submit_id') if isinstance(data, dict) else None
        if isinstance(submit, str) and re.fullmatch(TOKEN['pattern'], submit) and len(submit) <= 128:
            result['recovery'] = {**(result['recovery'] or {}), 'submitId': submit,
                'query': {'action': 'plugin.task.read', 'submitId': submit}, 'nextAction': 'plugin.task.read'}
            result['generationSubmitted'] = True if upstream['ok'] else None
            result['operationOutcome'] = 'accepted' if upstream['ok'] else 'unknown'
    return result
