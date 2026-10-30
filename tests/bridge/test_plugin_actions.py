# Fixtures use synthetic identities and material; no user account data.
"""Official plugin contracts; no upstream call, login or generation in tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import plugin_actions as plugin

PROJECT = '1000000b-0000-4000-8000-000000000002'
BASE = {'action': plugin.MULTIFRAME, 'projectId': PROJECT, 'artifactIds': ['art-a', 'art-b'], 'resolution': '720p', 'prompt': '纸飞机起飞'}
MEDIA = {'path': '/private/managed/image.png', 'mimeType': 'image/png', 'byteSize': 30, 'sha256': 'a' * 64}


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'JIMENG_READ_ONLY': '0', 'JIMENG_MAX_CREDITS': 'unlimited'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_schema_inventory_is_eight_actions_and_no_arbitrary_commands(self):
        self.assertEqual(len(plugin.request_schemas()), 8)
        for action, schema in plugin.request_schemas().items():
            self.assertTrue(plugin.supports(action))
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(schema['properties']['action']['const'], action)
        for action in ['plugin.login', 'plugin.exec', 'plugin.download', None, []]:
            self.assertFalse(plugin.supports(action))

    def test_dryrun_never_invokes_or_resolves_media(self):
        call, resolve = Mock(), Mock()
        result = plugin.execute(BASE, 'job-1', invoke=call, resolve_media=resolve)
        self.assertTrue(result['dryRun']); self.assertFalse(result['cliInvoked'])
        self.assertFalse(result['generationSubmitted'])
        call.assert_not_called(); resolve.assert_not_called()
        self.assertNotIn('/private', json.dumps(result))
        self.assertEqual(result['plan']['artifactIds'], ['art-a', 'art-b'])

    def test_two_frames_compile_exact_flags_and_no_model(self):
        invoke = Mock(return_value={'ok': True, 'data': {'submit_id': 'abc-123'}})
        resolve = Mock(return_value=MEDIA)
        result = plugin.execute({**BASE, 'dryRun': False, 'duration': 2, 'sessionId': 123}, 'job', invoke=invoke, resolve_media=resolve)
        args = invoke.call_args.args[0]
        self.assertEqual(args, ['multiframe2video', '--images=/private/managed/image.png,/private/managed/image.png', '--video_resolution=720p', '--session=123', '--poll=0', '--prompt=纸飞机起飞', '--duration=2'])
        self.assertTrue(result['generationSubmitted'])
        self.assertEqual(result['recovery']['query'], {'action': 'plugin.task.read', 'submitId': 'abc-123'})
        self.assertNotIn('/private', json.dumps(result))
        self.assertEqual(resolve.call_count, 2)
        resolve.assert_any_call(PROJECT, 'art-a')

    def test_three_frames_repeat_array_flags(self):
        payload = {**BASE, 'artifactIds': ['a', 'b', 'c'], 'transitionPrompts': ['A到B', 'B到C'], 'transitionDurations': [1, 1.5], 'resolution': '1080p'}
        payload.pop('prompt')
        plugin.validate(payload)
        args = plugin._compile(payload, ['/a.png', '/b.png', '/c.png'])
        self.assertEqual(args[-4:], ['--transition-prompt=A到B', '--transition-prompt=B到C', '--transition-duration=1', '--transition-duration=1.5'])
        self.assertNotIn('--prompt=', ' '.join(args))

    def test_three_frames_duration_can_default(self):
        payload = {**BASE, 'artifactIds': ['a', 'b', 'c'], 'transitionPrompts': ['one', 'two']}
        payload.pop('prompt'); plugin.validate(payload)
        self.assertFalse(any(x.startswith('--transition-duration') for x in plugin._compile(payload, ['/a', '/b', '/c'])))

    def test_repeated_frames_are_not_silently_deduplicated(self):
        plugin.validate({**BASE, 'artifactIds': ['art-a', 'art-a']})

    def test_invalid_cross_field_combinations(self):
        invalid = [dict(BASE, duration=1), dict(BASE, duration=float('nan')), dict(BASE, duration=True),
            dict(BASE, resolution='480p'), dict(BASE, model='seedance2.5'), dict(BASE, transitionPrompts=['x']),
            dict(BASE, artifactIds=['a']), dict(BASE, artifactIds=['a'] * 21),
            dict(BASE, artifactIds=['a', 'b', 'c']), dict(BASE, prompt='')]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError): plugin.validate(payload)
        p = {**BASE, 'artifactIds': ['a', 'b', 'c'], 'transitionPrompts': ['one']}; p.pop('prompt')
        with self.assertRaises(ValueError): plugin.validate(p)
        p['transitionPrompts'] = ['one', 'two']; p['transitionDurations'] = [2]
        with self.assertRaises(ValueError): plugin.validate(p)

    def test_paths_urls_credentials_poll_and_ceiling_are_not_payload_fields(self):
        for key in ['images', 'url', 'path', 'downloadDir', 'cookie', 'args', 'poll', 'creditCeiling', 'allowOrdinary']:
            with self.subTest(key=key), self.assertRaises(ValueError): plugin.validate({**BASE, key: 'value'})

    def test_readonly_and_numeric_cap_block_live_not_dryrun(self):
        for env in [{'JIMENG_READ_ONLY': '1'}, {'JIMENG_MAX_CREDITS': '0'}, {'JIMENG_MAX_CREDITS': '20'}]:
            with self.subTest(env=env), patch.dict(os.environ, env):
                plugin.validate(BASE)
                with self.assertRaises(ValueError): plugin.validate({**BASE, 'dryRun': False})
        with patch.dict(os.environ, {'JIMENG_MAX_CREDITS': '0'}):
            plugin.validate({'action': 'plugin.session.create', 'name': 'Test', 'dryRun': False})

    def test_session_exact_compilation_and_flag_like_names_are_positional(self):
        cases = [({'action': 'plugin.session.create'}, ['session', 'create']),
            ({'action': 'plugin.session.create', 'name': '--help'}, ['session', 'create', '--', '--help']),
            ({'action': 'plugin.session.list'}, ['session', 'list', '--max-count=30']),
            ({'action': 'plugin.session.list', 'limit': 100}, ['session', 'list', '--max-count=100']),
            ({'action': 'plugin.session.search', 'name': '视频'}, ['session', 'search', '--', '视频']),
            ({'action': 'plugin.session.rename', 'sessionId': 123, 'name': '测试'}, ['session', 'rename', '--', '123', '测试']),
            ({'action': 'plugin.session.delete', 'sessionId': 123}, ['session', 'delete', '--', '123'])]
        for payload, expected in cases:
            plugin.validate(payload); self.assertEqual(plugin._compile(payload), expected)

    def test_default_session_rename_delete_refused_and_names_bounded(self):
        invalid = [{'action': 'plugin.session.rename', 'sessionId': 0, 'name': 'x'},
            {'action': 'plugin.session.delete', 'sessionId': 0}, {'action': 'plugin.session.delete', 'sessionId': -1},
            {'action': 'plugin.session.delete', 'sessionId': True},
            {'action': 'plugin.session.create', 'name': ' '}, {'action': 'plugin.session.create', 'name': ' x'},
            {'action': 'plugin.session.create', 'name': 'x' * 51}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError): plugin.validate(payload)

    def test_task_filter_compilation_and_read_only_query(self):
        payload = {'action': 'plugin.task.list', 'status': 'success', 'taskType': 'video', 'submitId': 'abc-123', 'limit': 4, 'offset': 10}
        plugin.validate(payload)
        self.assertEqual(plugin._compile(payload), ['list_task', '--gen_status=success', '--gen_task_type=video', '--submit_id=abc-123', '--limit=4', '--offset=10'])
        call = Mock(return_value={'ok': True, 'data': {'gen_status': 'success'}})
        result = plugin.execute({'action': 'plugin.task.read', 'submitId': 'abc-123'}, 'job', invoke=call)
        call.assert_called_once_with(['query_result', '--submit_id=abc-123'])
        self.assertFalse(result['workspaceMutationAttempted']); self.assertFalse(result['generationSubmitted'])

    def test_no_resolver_does_not_submit(self):
        call = Mock()
        with self.assertRaises(ValueError): plugin.execute({**BASE, 'dryRun': False}, 'job', invoke=call)
        call.assert_not_called()

    def test_invalid_managed_metadata_does_not_submit(self):
        for change in [{'mimeType': 'video/mp4'}, {'path': 'https://evil/x.png'}, {'path': '/file,a.png'},
            {'path': 'relative.png'}, {'byteSize': 0}, {'sha256': 'invalid'}, {'byteSize': 31 * 1024 * 1024}]:
            call = Mock()
            with self.subTest(change=change), self.assertRaises(ValueError):
                plugin.execute({**BASE, 'dryRun': False}, 'job', invoke=call, resolve_media=lambda p, a: {**MEDIA, **change})
            call.assert_not_called()

    def test_output_never_returns_private_media_paths_or_tokens(self):
        call = Mock(return_value={'ok': False, 'data': {'error': '/private/managed/image.png denied', 'access_token': 'private', 'path': '/var/private', 'url': 'https://cdn/media.mp4'}})
        result = plugin.execute({**BASE, 'dryRun': False}, 'job', invoke=call, resolve_media=lambda p, a: MEDIA)
        text = json.dumps(result)
        self.assertNotIn('/private', text); self.assertNotIn('/var/private', text)
        self.assertEqual(result['upstream']['data']['access_token'], '[REDACTED]')
        self.assertEqual(result['upstream']['data']['url'], 'https://cdn/media.mp4')

    def test_upstream_unknown_envelope_raises_without_retry(self):
        call = Mock(return_value={'result': 'unknown'})
        with self.assertRaises(RuntimeError):
            plugin.execute({'action': 'plugin.session.create', 'dryRun': False}, 'job', invoke=call)
        self.assertEqual(call.call_count, 1)

    def test_recovery_never_invents_submit_identity(self):
        result = plugin.recovery({**BASE, 'dryRun': False}, 'original-job')
        self.assertFalse(result['upstreamIdentityKnownBeforeSubmit']); self.assertTrue(result['doNotResubmit'])
        self.assertNotIn('submitId', result)
        self.assertEqual(result['nextAction'], 'plugin.task.list')
        self.assertIsNone(plugin.recovery(BASE, 'dry-job'))

    def test_capabilities_ordinary_only_and_contract_evidence(self):
        caps = plugin.capabilities()['actions'][plugin.MULTIFRAME]
        self.assertTrue(caps['billable']); self.assertFalse(caps['sampleSupported'])
        self.assertFalse(caps['numericCreditCeilingSupported'])
        self.assertTrue((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge").joinpath(caps['contractEvidence']).is_file())

    def test_runner_parses_warning_plus_json(self):
        completed = subprocess.CompletedProcess([], 0, '版本提示\n{"submit_id":"abc-123","gen_status":"querying"}\n', '')
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', return_value=completed) as run:
            result = plugin._invoke(['query_result', '--submit_id=abc-123'])
        self.assertTrue(result['ok']); self.assertEqual(result['data']['submit_id'], 'abc-123')
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)

    def test_runner_rejects_nonzero_business_code(self):
        completed = subprocess.CompletedProcess([], 0, '{"code":401,"message":"expired"}', '')
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', return_value=completed):
            self.assertFalse(plugin._invoke(['list_task'])['ok'])

    def test_runner_preserves_unparsed_session_table_without_claiming_media(self):
        completed = subprocess.CompletedProcess([], 0, 'ID NAME PINNED UPDATED_AT\n12 demo no 2026-10-29', '')
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', return_value=completed):
            result = plugin._invoke(['session', 'list'])
        self.assertEqual(result['format'], 'text'); self.assertTrue(result['businessStatusUnparsed'])

    def test_runner_timeout_never_retries_or_echoes_exception_args(self):
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', side_effect=subprocess.TimeoutExpired(['/secret/path'], 75)) as run:
            with self.assertRaisesRegex(RuntimeError, 'inspect the original') as error: plugin._invoke(['session', 'create'])
        self.assertEqual(run.call_count, 1); self.assertNotIn('/secret', str(error.exception))

    def test_actual_official_session_output_shapes(self):
        fixtures = [(['session', 'create'], 'Created session "验收" (ID: 10000000000004)\n', {'sessionId': 10000000000004, 'name': '验收'}),
            (['session', 'rename'], 'Renamed session 10000000000004 to "验收已验证"\n', {'sessionId': 10000000000004, 'name': '验收已验证'}),
            (['session', 'delete'], 'Deleted session 10000000000004\n', {'sessionId': 10000000000004, 'deleted': True, 'softDelete': True, 'historyDestinationSessionId': 0})]
        for args, output, expected in fixtures:
            self.assertEqual(plugin._session_output(args, output, 0)['data'], expected)
        table = 'ID              NAME          PINNED  UPDATED_AT\n--------------  ------------  ------  ----------------\n0 默认对话      Yes     2026-10-26 12:41\n12              has  spaces   No      2026-10-29 22:34\n'
        items = plugin._session_output(['session', 'list'], table, 0)['data']['items']
        self.assertTrue(items[0]['pinned']); self.assertEqual(items[1]['name'], 'has  spaces')
        search = 'Found 1 sessions containing "验收":\nID              NAME          UPDATED_AT\n--------------  ------------  ----------------\n10000000000004  验收  2026-10-29 22:34\n'
        self.assertEqual(plugin._session_output(['session', 'search'], search, 0)['data']['items'][0]['sessionId'], 10000000000004)

    def test_search_no_match_exit_one_is_successful_empty_result(self):
        completed = subprocess.CompletedProcess([], 1, 'No sessions found matching "验收已删除"\n', '')
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', return_value=completed):
            result = plugin.execute({'action': 'plugin.session.search', 'name': '验收已删除'}, 'job')
        self.assertEqual(result['operationOutcome'], 'succeeded')
        self.assertEqual(result['upstream']['data']['items'], [])
        self.assertEqual(result['upstream']['exitCode'], 1)

    def test_output_shape_drift_does_not_guess_session_mutation_success(self):
        self.assertIsNone(plugin._session_output(['session', 'create'], 'new unknown output', 0))
        self.assertIsNone(plugin._session_output(['session', 'list'], 'ID NAME PINNED UPDATED_AT\n12 wrong row', 0))
        result = plugin.execute({'action': 'plugin.session.create', 'dryRun': False}, 'job',
            invoke=lambda a: {'ok': True, 'businessStatusUnparsed': True, 'format': 'text', 'data': {'text': 'unknown'}})
        self.assertEqual(result['operationOutcome'], 'unknown')
        self.assertTrue(result['dispatchAttempted'])

    def test_definite_preflight_auth_error_is_rejected_without_dispatch(self):
        completed = subprocess.CompletedProcess([], 1, '', '未检测到有效登录态，请先登录')
        with patch.object(plugin, 'binary', return_value='/official/dreamina'), patch.object(plugin.subprocess, 'run', return_value=completed):
            result = plugin.execute({'action': 'plugin.session.create', 'dryRun': False}, 'job')
        self.assertEqual(result['operationOutcome'], 'rejected')
        self.assertFalse(result['dispatchAttempted'])
        self.assertFalse(result['workspaceMutationAttempted'])

    def test_generation_accepted_is_separate_from_completion(self):
        result = plugin.execute({**BASE, 'dryRun': False}, 'job',
            invoke=lambda a: {'ok': True, 'data': {'submit_id': 'task-abc', 'gen_status': 'querying'}}, resolve_media=lambda p, a: MEDIA)
        self.assertEqual(result['operationOutcome'], 'accepted')
        self.assertTrue(result['dispatchAttempted'])
        ambiguous = plugin.execute({**BASE, 'dryRun': False}, 'job',
            invoke=lambda a: {'ok': True, 'data': {}}, resolve_media=lambda p, a: MEDIA)
        self.assertEqual(ambiguous['operationOutcome'], 'unknown')
        self.assertIsNone(ambiguous['generationSubmitted'])

    def test_unknown_cli_is_not_reported_available(self):
        with patch.object(plugin, 'binary', return_value=None):
            self.assertFalse(plugin.capabilities()['actions'][plugin.MULTIFRAME]['available'])
            self.assertFalse(plugin._invoke(['list_task'])['ok'])


if __name__ == '__main__': unittest.main()
