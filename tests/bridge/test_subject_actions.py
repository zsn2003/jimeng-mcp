# Fixtures use synthetic identities and material; no user account data.
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = (Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")
sys.path.insert(0, str(ROOT))
import subject_actions as subject

PROJECT = '1000000b-0000-4000-8000-000000000002'
SUBJECT_ID = '10000000000001'
AUDIO = b'ID3' + b'synthetic saved voice'
SESSION = {'cookie': 'sessionid=synthetic-secret', 'userAgent': 'Synthetic Test Agent', 'headers': {}, 'actionHeaders': {}}


class SubjectActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DREAMINA_CANVAS_STATE_DIR': self.tmp.name, 'JIMENG_READ_ONLY': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.payload = {'action': subject.ACTION, 'projectId': PROJECT, 'subjectId': SUBJECT_ID, 'dryRun': False}
        self.source = {'subject_id': SUBJECT_ID, 'data_id': '12345', 'update_time': 1790000000000,
            'content': {'name': '测试角色', 'description': '保持角色原始描述。',
                'main_image': {'image_uri': 'tos-cn-i-test/main123', 'width': 100, 'height': 120, 'format': ''},
                'auxiliary_images': [{'image_uri': 'tos-cn-i-test/aux456', 'width': 90, 'height': 120, 'format': ''}],
                'voice_list': [{'audio_info': {'format': 'mp3', 'vid': 'source-voice-vid', 'duration_ms': 4560,
                    'audio_url': 'https://v6-artist.vlabvod.com/voice?expires=1'}}]}}
        self.saved = {}
        self.official_actions = []

    def fake_official(self, payload, job_id, *, invoke):
        self.official_actions.append(payload['action'])
        action = payload['action']
        if action == 'resource.read':
            media_type = 'audio' if payload['resourceId'] == self.saved.get('voice') else 'image'
            return {'upstream': {'ok': True, 'data': {'resourceId': payload['resourceId'], 'type': media_type, 'status': 'success'}}}
        if action == 'resource.upload':
            self.assertEqual(base64.b64decode(payload['contentBase64']), AUDIO)
            self.assertEqual(payload['mediaType'], 'audio')
            rid = subject.official._identities(job_id)[0]
            return {'resourceId': rid, 'resourceReady': True, 'resourceVerified': True, 'upstream': {'ok': True, 'data': {'resourceId': rid}}}
        if action == 'element.create':
            self.saved = copy.deepcopy(payload)
            return {'upstream': {'ok': True, 'data': {'node': {'nodeId': subject.official._identities(job_id)[1]}}}}
        if action == 'node.read':
            resources = []
            def binding(resource_id, kind):
                resources.append({'resourceId': resource_id, 'type': kind, 'status': 'success'})
                return {'id': resource_id, 'kind': 'resource', 'type': kind, 'writebackSupported': True}
            bindings = {'main': binding(self.saved['main'], 'image'), 'auxiliaries': [binding(x, 'image') for x in self.saved.get('auxiliary', [])]}
            if 'voice' in self.saved:
                bindings['voice'] = binding(self.saved['voice'], 'audio')
            node = {'nodeId': payload['nodeId'], 'type': 'element', 'description': self.saved['description'], 'subjectBindings': bindings, 'resources': resources}
            return {'upstream': {'ok': True, 'data': {'nodes': [{'result': 'FOUND', 'node': node}]}}}
        self.fail('Unexpected or generation action ' + action)

    def fake_send(self, path, body, headers):
        self.assertEqual(path, '/octo_api/v1/resource/upload')
        self.assertEqual(body['type'], 1)
        self.assertFalse('://' in body['value'])
        return 200, json.dumps({'ret': '0', 'data': {'resource': {'resource_id': body['resource_id'], 'type': 1,
                             'image': {'uri': body['value']}}}}).encode()

    def mocks(self):
        return (patch.object(subject.web, 'execute', return_value={'upstream': {'ok': True, 'data': {'subject': self.source}}}),
                patch.object(subject.web, '_load_session', return_value=SESSION),
                patch.object(subject.web, '_send', side_effect=self.fake_send),
                patch.object(subject, '_download_voice', return_value=AUDIO),
                patch.object(subject.official, 'execute', side_effect=self.fake_official))

    def test_default_dry_run_does_not_read_sources_or_write_journal(self):
        p = {k: v for k, v in self.payload.items() if k != 'dryRun'}
        with patch.object(subject.web, 'execute') as web_read, patch.object(subject, '_begin') as begin:
            result = subject.execute(p, 'job', invoke=Mock())
        web_read.assert_not_called()
        begin.assert_not_called()
        self.assertTrue(result['dryRun'])
        self.assertFalse(result['generationSubmitted'])

    def test_payload_rejects_urls_headers_arbitrary_ids_and_readonly(self):
        for key in ['url', 'imageUri', 'voiceUrl', 'headers', 'cookie', 'resourceId', 'nodeId', 'file']:
            with self.subTest(key=key), self.assertRaises(ValueError):
                subject.validate({**self.payload, key: 'https://untrusted.invalid'})
        with patch.dict(os.environ, {'JIMENG_READ_ONLY': '1'}), self.assertRaises(ValueError):
            subject.validate(self.payload)
        with self.assertRaises(ValueError):
            subject.validate({**self.payload, 'includeVoice': False, 'voiceIndex': 0})
        self.assertEqual(subject.recovery(self.payload, 'job'), subject.recovery(self.payload, 'job'))
        self.assertNotEqual(subject.recovery(self.payload, 'job')['nodeId'], subject.recovery(self.payload, 'other')['nodeId'])

    def test_full_import_preserves_source_and_verifies_all_three_bindings(self):
        original = copy.deepcopy(self.source)
        a,b,c,d,e = self.mocks()
        with a,b,c as send,d,e:
            result = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertTrue(result['upstream']['ok'])
        self.assertTrue(result['bindingsVerified'])
        self.assertTrue(result['voiceBound'])
        self.assertFalse(result['sourceSubjectModified'])
        self.assertFalse(result['generationSubmitted'])
        self.assertEqual(self.source, original)
        self.assertEqual(result['sourceSubject']['voiceIndex'], 0)
        self.assertEqual(result['sourceSubject']['voiceVid'], 'source-voice-vid')
        self.assertEqual(send.call_count, 2)
        self.assertEqual(self.official_actions, ['resource.read','resource.read','resource.upload','element.create','node.read'])
        self.assertNotIn('https://', json.dumps(result))
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertEqual(len(result['createdResources']), 3)
        self.assertEqual(self.saved['description'], original['content']['description'])

    def test_multiple_voices_need_selection_before_any_workspace_mutation(self):
        self.source['content']['voice_list'].append(copy.deepcopy(self.source['content']['voice_list'][0]))
        a,b,c,d,e = self.mocks()
        with a,b,c as send,d as download,e as official:
            result = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_VOICE_SELECTION_REQUIRED')
        self.assertFalse(result['workspaceMutationAttempted'])
        send.assert_not_called(); download.assert_not_called(); official.assert_not_called()

    def test_explicit_no_voice_keeps_original_voice_and_reports_unbound(self):
        a,b,c,d,e = self.mocks()
        with a,b,c,d as download,e:
            result = subject.execute({**self.payload, 'includeVoice': False}, 'job', invoke=Mock())
        download.assert_not_called()
        self.assertTrue(result['upstream']['ok'])
        self.assertFalse(result['voiceBound'])
        self.assertNotIn('voice', self.saved)
        self.assertEqual(len(self.source['content']['voice_list']), 1)

    def test_image_failure_records_partial_resources_and_never_replays_mutations(self):
        def send(path, body, headers):
            if body['value'].endswith('aux456'):
                return 200, b'{"ret":"500","errmsg":"failed"}'
            return self.fake_send(path, body, headers)
        a,b,c,d,e = self.mocks()
        with a,b,patch.object(subject.web, '_send', side_effect=send) as sent,d,e:
            first = subject.execute(self.payload, 'job', invoke=Mock())
            second = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertEqual(first['upstream']['error']['code'], 'SUBJECT_IMAGE_UPLOAD_REJECTED')
        self.assertEqual(len(first['createdResources']), 1)
        self.assertEqual(second['upstream']['error']['code'], 'SUBJECT_RECONCILIATION_REQUIRED')
        self.assertEqual(sent.call_count, 2)
        self.assertNotIn('element.create', self.official_actions)

    def test_successful_same_job_replays_original_receipt_without_new_import(self):
        a,b,c,d,e = self.mocks()
        with a as source,b,c as send,d,e:
            first = subject.execute(self.payload, 'job', invoke=Mock())
            again = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertTrue(again['replayedOriginalReceipt'])
        self.assertEqual(first['upstream']['data'], again['upstream']['data'])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(source.call_count, 1)

    def test_wrong_readback_binding_does_not_claim_completed_import(self):
        def official(payload, job_id, *, invoke):
            result = self.fake_official(payload, job_id, invoke=invoke)
            if payload['action'] == 'node.read':
                result['upstream']['data']['nodes'][0]['node']['subjectBindings']['voice']['id'] = 'wrong-resource'
            return result
        a,b,c,d,e = self.mocks()
        with a,b,c,d,patch.object(subject.official, 'execute', side_effect=official):
            result = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertFalse(result['upstream']['ok'])
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_BINDING_READBACK_MISMATCH')
        self.assertFalse(result.get('bindingsVerified', False))

    def test_invalid_source_title_fails_before_media_registration(self):
        self.source['content']['name'] = 'x' * 513
        a,b,c,d,e = self.mocks()
        with a,b,c as send,d as download,e as official:
            result = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertFalse(result['upstream']['ok'])
        self.assertFalse(result['workspaceMutationAttempted'])
        send.assert_not_called(); download.assert_not_called(); official.assert_not_called()

    def test_binding_needs_unique_successful_resource_in_separate_resources_array(self):
        binding = {'id': 'saved', 'kind': 'resource', 'type': 'audio'}
        resource = {'resourceId': 'saved', 'type': 'audio', 'status': 'success'}
        self.assertTrue(subject._binding_matches(binding, 'saved', 'audio', [resource]))
        for resources in [[], [resource, resource], [{**resource, 'status': 'pending'}], [{**resource, 'type': 'image'}]]:
            self.assertFalse(subject._binding_matches(binding, 'saved', 'audio', resources))
        self.assertFalse(subject._binding_matches({**binding, 'type': 'image'}, 'saved', 'audio', [resource]))

    def test_readback_requires_observed_nodes_envelope(self):
        def official(payload, job_id, *, invoke):
            result = self.fake_official(payload, job_id, invoke=invoke)
            if payload['action'] == 'node.read':
                data = result['upstream']['data']
                data['items'] = data.pop('nodes')
            return result
        a,b,c,d,e = self.mocks()
        with a,b,c,d,patch.object(subject.official, 'execute', side_effect=official):
            result = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertFalse(result['upstream']['ok'])
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_BINDING_READBACK_MISMATCH')
        self.assertEqual(len(result['plannedImages']), 2)
        self.assertEqual(len(result['createdResources']), 3)
        self.assertEqual(result['recovery']['nodeId'], subject.official._identities('job:element')[1])

    def test_subject_image_uri_is_not_an_arbitrary_url(self):
        for uri in ['https://evil.invalid/x','file:///secret','tos-cn-i-test/../../secret','tos-cn-i-test/image?x=1']:
            with self.subTest(uri=uri), self.assertRaises(subject.ImportFailure):
                subject._safe_image({'image_uri':uri,'width':100,'height':100})

    def test_voice_download_rejects_other_origins_private_dns_and_credentials(self):
        for url in ['http://v6-artist.vlabvod.com/x','https://evil.invalid/x','https://u:p@v6-artist.vlabvod.com/x','https://v6-artist.vlabvod.com:8443/x']:
            with self.subTest(url=url), patch.object(subject.socket,'getaddrinfo') as dns, self.assertRaises(subject.ImportFailure):
                subject._download_voice({'audio_url':url})
            dns.assert_not_called()
        with patch.object(subject.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]), self.assertRaises(subject.ImportFailure):
            subject._download_voice({'audio_url':'https://v6-artist.vlabvod.com/x'})

    def seed_failed_receipt(self):
        a,b,c,d,e = self.mocks()
        with a,b,c,d,e:
            imported = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertTrue(imported['bindingsVerified'])
        operation_id = subject.recovery(self.payload, 'job')['operationId']
        path = subject._root() / (operation_id + '.json')
        record = json.loads(path.read_text())
        original_error = {'upstream': {'ok': False, 'error': {'code': 'SUBJECT_BINDING_READBACK_MISMATCH'}},
                          'workspaceMutationAttempted': True, 'recovery': imported['recovery']}
        record.update(state='needs-inspection', phase='element-readback', result=original_error)
        subject._save(path, record)
        self.official_actions.clear()
        return path, record, {'action': subject.INSPECT_ACTION, 'projectId': PROJECT, 'operationId': operation_id}

    def inspection(self, payload, *, official=None, job_id='inspection-job'):
        with patch.object(subject.web, 'execute') as web_read, patch.object(subject.web, '_send') as upload, \
             patch.object(subject, '_download_voice') as download, \
             patch.object(subject.official, 'execute', side_effect=official or self.fake_official):
            result = subject.execute(payload, job_id, invoke=Mock())
        web_read.assert_not_called(); upload.assert_not_called(); download.assert_not_called()
        self.assertTrue(all(action in ('node.read', 'resource.read') for action in self.official_actions))
        self.assertFalse(result['workspaceMutationAttempted'])
        self.assertFalse(result['generationSubmitted'])
        self.assertFalse(result['dispatchAttempted'])
        return result

    def test_inspect_original_import_is_readonly_and_verifies_description_and_all_resources(self):
        path, record, payload = self.seed_failed_receipt()
        before = path.read_bytes()
        with patch.dict(os.environ, {'JIMENG_READ_ONLY': '1'}):
            result = self.inspection(payload)
        self.assertEqual(result['operationOutcome'], 'succeeded')
        self.assertTrue(result['bindingsVerified'])
        self.assertTrue(result['checks']['descriptionHashVerified'])
        self.assertEqual(len(result['resourceChecks']), 3)
        self.assertFalse(result['localJournalUpdated'])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(self.official_actions, ['node.read', 'resource.read', 'resource.read', 'resource.read'])

    def test_reconcile_appends_receipt_without_rewriting_original_failure_or_reimporting(self):
        path, original, payload = self.seed_failed_receipt()
        payload['action'] = subject.RECONCILE_ACTION
        with patch.dict(os.environ, {'JIMENG_READ_ONLY': '1'}):
            result = self.inspection(payload)
            replay = self.inspection(payload)
        after = json.loads(path.read_text())
        self.assertEqual(result['operationOutcome'], 'succeeded')
        self.assertTrue(result['localJournalUpdated'])
        self.assertTrue(result['originalReceiptPreserved'])
        self.assertEqual(after['result'], original['result'])
        self.assertEqual(after['state'], original['state'])
        self.assertEqual(after['phase'], original['phase'])
        self.assertEqual(after['reconciliationOutcome'], 'succeeded')
        self.assertEqual(len(after['reconciliations']), 1)
        self.assertTrue(replay['replayedReconciliationReceipt'])
        self.assertEqual(self.official_actions.count('node.read'), 1)
        with patch.object(subject.web, 'execute') as no_reimport:
            original_replay = subject.execute(self.payload, 'job', invoke=Mock())
        self.assertEqual(original_replay['upstream']['error']['code'], 'SUBJECT_RECONCILIATION_REQUIRED')
        no_reimport.assert_not_called()
        self.assertEqual(json.loads(path.read_text())['result'], original['result'])

    def test_inspection_reports_partial_for_mismatched_description_or_pending_resource(self):
        _, _, payload = self.seed_failed_receipt()
        for variation in ('description', 'resource'):
            def read(p, job_id, *, invoke):
                result = self.fake_official(p, job_id, invoke=invoke)
                if variation == 'description' and p['action'] == 'node.read':
                    result['upstream']['data']['nodes'][0]['node']['description'] = 'changed after import'
                if variation == 'resource' and p['action'] == 'resource.read':
                    result['upstream']['data']['status'] = 'pending'
                return result
            with self.subTest(variation=variation):
                result = self.inspection(payload, official=read)
                self.assertEqual(result['operationOutcome'], 'partial')
                self.assertFalse(result['bindingsVerified'])
                self.assertTrue(result['reconciliationRequired'])

    def test_transport_failure_is_unknown_and_does_not_repair_original_objects(self):
        path, _, payload = self.seed_failed_receipt()
        before = path.read_bytes()
        result = self.inspection(payload, official=Mock(side_effect=RuntimeError('synthetic-cookie-private')))
        self.assertEqual(result['operationOutcome'], 'unknown')
        self.assertNotIn('synthetic-cookie-private', json.dumps(result))
        self.assertEqual(path.read_bytes(), before)

    def test_inspection_rejects_tampered_plan_before_any_upstream_read(self):
        path, record, payload = self.seed_failed_receipt()
        record['plannedImages'][0]['resourceId'] = 'wrong-resource'
        subject._save(path, record)
        read = Mock()
        result = self.inspection(payload, official=read)
        self.assertEqual(result['operationOutcome'], 'rejected')
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_JOURNAL_IDENTITY_MISMATCH')
        read.assert_not_called()

    def test_inspection_rejects_wrong_project_and_missing_or_symlink_journal(self):
        path, _, payload = self.seed_failed_receipt()
        for variant in ('project', 'missing', 'symlink'):
            candidate = dict(payload)
            if variant == 'project': candidate['projectId'] = '11111111-1111-4111-8111-111111111111'
            if variant == 'missing': candidate['operationId'] = '22222222-2222-4222-8222-222222222222'
            if variant == 'symlink':
                saved = path.with_suffix('.private-copy')
                path.rename(saved); path.symlink_to(saved)
            read = Mock()
            with self.subTest(variant=variant):
                result = self.inspection(candidate, official=read)
                self.assertEqual(result['operationOutcome'], 'rejected')
                read.assert_not_called()

    def test_inspection_schema_rejects_override_ids_paths_and_mutation_flags(self):
        _, _, payload = self.seed_failed_receipt()
        for key in ('nodeId', 'resourceId', 'file', 'subjectId', 'dryRun', 'force', 'cookie'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                subject.validate({**payload, key: 'override'})
        self.assertTrue(subject.supports(subject.INSPECT_ACTION))
        self.assertTrue(subject.supports(subject.RECONCILE_ACTION))
        self.assertEqual(subject.request_schemas()[subject.INSPECT_ACTION]['required'], ['action', 'projectId', 'operationId'])

    def test_incomplete_original_plan_is_not_inferred_from_current_subject(self):
        path, record, payload = self.seed_failed_receipt()
        del record['sourceSubject']['descriptionSha256']
        subject._save(path, record)
        read = Mock()
        result = self.inspection(payload, official=read)
        self.assertEqual(result['operationOutcome'], 'rejected')
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_PLAN_INCOMPLETE')
        read.assert_not_called()

    def test_reconcile_does_not_overwrite_a_journal_changed_during_read(self):
        path, original, payload = self.seed_failed_receipt()
        payload['action'] = subject.RECONCILE_ACTION
        def read(p, job_id, *, invoke):
            result = self.fake_official(p, job_id, invoke=invoke)
            if p['action'] == 'node.read':
                changed = json.loads(path.read_text())
                changed['concurrentMarker'] = 'preserve'
                subject._save(path, changed)
            return result
        result = self.inspection(payload, official=read)
        after = json.loads(path.read_text())
        self.assertEqual(result['upstream']['error']['code'], 'SUBJECT_JOURNAL_CHANGED')
        self.assertFalse(result['localJournalUpdated'])
        self.assertNotIn('reconciliations', after)
        self.assertEqual(after['concurrentMarker'], 'preserve')
        self.assertEqual(after['result'], original['result'])

    def test_subject_capability_uses_scoped_shared_acceptance_records(self):
        accepted = [{'action': subject.ACTION, 'variant': 'saved_images_and_voice', 'scope': 'one import'}]
        registry = SimpleNamespace(verified=lambda action, variant=None: accepted if action == subject.ACTION else [])
        with patch.dict(sys.modules, {'acceptance_registry': registry}), patch.object(subject.web, '_load_session', return_value=SESSION):
            capabilities = subject.capabilities()['actions']
        self.assertTrue(capabilities[subject.ACTION]['serverExecutionVerified'])
        self.assertEqual(capabilities[subject.ACTION]['executionVerification'], accepted)
        self.assertFalse(capabilities[subject.INSPECT_ACTION]['serverExecutionVerified'])


if __name__ == '__main__': unittest.main()
