# Fixtures use synthetic identities and material; no user account data.
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str((Path(__file__).resolve().parents[2] / "src" / "jimeng_mcp" / "_bridge")))
import personal_actions as personal

SOURCE = '10000000000001'
TARGET = '10000000000002'
CREATED = '10000000000003'
SESSION = {'cookie': 'sessionid=private-test', 'userAgent': 'Test Agent', 'headers': {}, 'actionHeaders': {}}


def voice(vid, title=''):
    return {'source': 'generated', 'audio_info': {'vid': vid, 'audio_url': 'https://v6-artist.vlabvod.com/test?expires=1',
            'duration': 4.56, 'duration_ms': 4560, 'format': 'mp3', 'title': title}}


def subject(sid, name, voices=None):
    return {'subject_id': sid, 'data_id': '1234', 'subject_control': {'enabled': True, 'editable': True, 'deletable': True},
            'content': {'name': name, 'description': 'Original description',
                        'main_image': {'image_uri': 'tos-cn-i-test/saved-image', 'width': 100, 'height': 120,
                                       'image_url': 'https://p3.example/image?expires=1',
                                       'aigc_image': {'item_id': 9000000000000000000, 'item_id_str': '9000000000000000123'}},
                        'auxiliary_images': [{'image_uri': 'tos-cn-i-test/aux-image', 'width': 90, 'height': 120}],
                        **({'voice_list': voices} if voices is not None else {})}}


class PersonalActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DREAMINA_CANVAS_STATE_DIR': self.tmp.name, 'JIMENG_READ_ONLY': '0'})
        env.start()
        self.addCleanup(env.stop)
        self.records = {SOURCE: subject(SOURCE, 'Source', [voice('saved-one')]), TARGET: subject(TARGET, 'Test target')}
        self.calls = []
        self.next_version = 2000
        self.source_before = copy.deepcopy(self.records[SOURCE])
        self.update = {'action': personal.UPDATE, 'subjectId': TARGET, 'expectedDataId': '1234', 'description': 'Edited', 'dryRun': False}
        self.create = {'action': personal.CREATE, 'sourceSubjectId': SOURCE, 'expectedSourceDataId': '1234', 'name': 'New copy', 'dryRun': False}
        self.bind = {'action': personal.VOICE_CREATE, 'subjectId': TARGET, 'expectedDataId': '1234',
                     'sourceSubjectId': SOURCE, 'expectedSourceDataId': '1234', 'sourceVoiceIndex': 0, 'dryRun': False}

    def fake_lookup(self, payload, base, session):
        self.assertEqual(session, SESSION)
        if payload['subjectId'] not in self.records:
            return {'upstream': {'ok': False, 'error': {'code': 'WEB_SUBJECT_NOT_FOUND'}}}
        return {'upstream': {'ok': True, 'data': {'subject': copy.deepcopy(self.records[payload['subjectId']])}}}

    def fake_send(self, path, body, headers):
        self.calls.append((path, copy.deepcopy(body)))
        self.assertEqual(headers['Cookie'], SESSION['cookie'])
        self.assertNotIn('expected_data_id', body)
        self.assertNotIn('data_id', body)
        self.assertNotIn('generate_voice', path)
        self.assertNotIn('delete', path)
        sid = CREATED if path.endswith('/create') else body['subject_id']
        result = copy.deepcopy(self.records.get(sid, subject(sid, '')))
        result['content'] = copy.deepcopy(body['content'])
        result['content']['main_image']['image_url'] = 'https://p3.example/refresh?expires=2'
        result['content']['main_image']['format'] = ''
        aigc = result['content']['main_image'].get('aigc_image')
        if aigc:
            aigc['item_id'] = 9000000000000000000
        self.next_version += 1
        result['data_id'] = str(self.next_version)
        self.records[sid] = result
        return 200, json.dumps({'ret': '0', 'data': result}).encode()

    def run_action(self, payload, job='test', send=None):
        with patch.object(personal.web, '_load_session', return_value=SESSION), \
             patch.object(personal.web, '_subject_lookup', side_effect=self.fake_lookup), \
             patch.object(personal.web, '_send', side_effect=send or self.fake_send):
            return personal.execute(payload, job)

    def test_default_dry_run_has_no_network_or_state(self):
        p = {k: v for k, v in self.create.items() if k != 'dryRun'}
        with patch.object(personal.web, '_load_session') as load, patch.object(personal, '_begin') as begin:
            result = personal.execute(p, 'job')
        load.assert_not_called()
        begin.assert_not_called()
        self.assertTrue(result['dryRun'])
        self.assertFalse(result['workspaceMutationAttempted'])
        self.assertFalse(result['generationSubmitted'])

    def test_schema_rejects_arbitrary_urls_ids_body_and_noop_update(self):
        for key in ['url', 'imageUri', 'vid', 'headers', 'cookie', 'content', 'workspaceId']:
            with self.subTest(key=key), self.assertRaises(ValueError):
                personal.validate({**self.update, key: 'untrusted'})
        with self.assertRaises(ValueError):
            personal.validate({k: v for k, v in self.update.items() if k != 'description'})
        with self.assertRaises(ValueError):
            personal.validate({**self.create, 'name': '😀' * 11})
        with self.assertRaises(ValueError):
            personal.validate({**self.create, 'name': '   '})
        with patch.dict(os.environ, {'JIMENG_READ_ONLY': '1'}), self.assertRaises(ValueError):
            personal.validate(self.update)
        self.assertFalse(personal.supports('personal.subject.archive'))

    def test_copy_create_preserves_source_and_defaults_to_no_voices(self):
        result = self.run_action(self.create)
        self.assertTrue(result['persistenceVerified'])
        self.assertEqual(result['subjectId'], CREATED)
        self.assertNotIn('voice_list', self.calls[0][1]['content'])
        self.assertEqual(self.records[SOURCE], self.source_before)
        self.assertEqual(self.calls[0][1]['content']['main_image']['aigc_image'], {'item_id_str': '9000000000000000123'})
        self.assertEqual(self.calls[0][0], '/mweb/v1/dreamina_subject/create')
        self.assertFalse(result['upstreamConditionalVersioning'])

    def test_copy_create_can_explicitly_include_saved_voices(self):
        result = self.run_action({**self.create, 'includeVoices': True, 'includeAuxiliary': False})
        self.assertTrue(result['persistenceVerified'])
        self.assertNotIn('auxiliary_images', self.calls[0][1]['content'])
        self.assertEqual(self.calls[0][1]['content']['voice_list'], self.source_before['content']['voice_list'])

    def test_update_changes_only_text_and_preserves_bindings(self):
        self.records[TARGET]['content']['voice_list'] = [voice('existing')]
        before = copy.deepcopy(self.records[TARGET])
        result = self.run_action(self.update)
        self.assertTrue(result['persistenceVerified'])
        content = self.calls[0][1]['content']
        self.assertEqual(content['description'], 'Edited')
        self.assertEqual(content['voice_list'], before['content']['voice_list'])
        self.assertEqual(content['auxiliary_images'], before['content']['auxiliary_images'])
        self.assertEqual(set(self.calls[0][1]), {'subject_id', 'content'})

    def test_target_and_source_versions_checked_before_write(self):
        for n, payload in enumerate([{**self.update, 'expectedDataId': 'stale'}, {**self.bind, 'expectedSourceDataId': '4321'}]):
            if n == 0:
                payload['expectedDataId'] = '4321'
            result = self.run_action(payload, 'conflict' + str(n))
            self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_VERSION_CONFLICT')
        self.assertEqual(self.calls, [])

    def test_target_must_be_explicitly_editable(self):
        self.records[TARGET]['subject_control'] = {}
        result = self.run_action(self.update)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_NOT_EDITABLE')
        self.assertEqual(self.calls, [])

    def test_add_saved_voice_uses_official_conversion_and_explicit_selection(self):
        result = self.run_action({**self.bind, 'select': True})
        self.assertTrue(result['persistenceVerified'])
        content = self.calls[0][1]['content']
        selected = content['voice_list'][0]
        self.assertEqual(selected['audio_info']['url'], selected['audio_info']['audio_url'])
        self.assertEqual(set(selected['audio_info']), {'vid', 'audio_url', 'url', 'duration', 'title'})
        self.assertEqual(content['selected_voice'], selected)
        self.assertEqual(self.records[SOURCE], self.source_before)
        self.assertEqual(result['voiceOperationScope'], 'binding_only')

    def test_duplicate_voice_does_not_submit(self):
        self.records[TARGET]['content']['voice_list'] = [voice('saved-one')]
        result = self.run_action(self.bind)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_VOICE_DUPLICATE')
        self.assertEqual(self.calls, [])

    def test_replacement_moves_explicit_selected_binding(self):
        old = voice('old-voice')
        self.records[TARGET]['content'].update(voice_list=[old], selected_voice=old)
        result = self.run_action({**self.bind, 'action': personal.VOICE_UPDATE, 'voiceIndex': 0})
        self.assertTrue(result['persistenceVerified'])
        content = self.calls[0][1]['content']
        self.assertEqual(content['selected_voice']['audio_info']['vid'], 'saved-one')
        self.assertEqual(len(content['voice_list']), 1)

    def test_archive_detaches_only_target_binding_and_preserves_private_before(self):
        old, kept = voice('old-voice'), voice('kept-voice')
        self.records[TARGET]['content'].update(voice_list=[old, kept], selected_voice=old)
        p = {'action': personal.VOICE_ARCHIVE, 'subjectId': TARGET, 'expectedDataId': '1234', 'voiceIndex': 0, 'dryRun': False}
        result = self.run_action(p)
        self.assertTrue(result['persistenceVerified'])
        content = self.calls[0][1]['content']
        self.assertEqual(content['voice_list'], [kept])
        self.assertNotIn('selected_voice', content)
        self.assertFalse(result['mediaDeleted'])
        journal_path = next((Path(self.tmp.name) / 'personal-edits').glob('*.json'))
        journal = json.loads(journal_path.read_text())
        self.assertEqual(journal['beforeSubject']['content']['voice_list'], [old, kept])
        self.assertEqual(journal_path.stat().st_mode & 0o777, 0o600)

    def test_archive_other_voice_keeps_selected_voice(self):
        old, kept = voice('old-voice'), voice('kept-voice')
        self.records[TARGET]['content'].update(voice_list=[old, kept], selected_voice=kept)
        p = {'action': personal.VOICE_ARCHIVE, 'subjectId': TARGET, 'expectedDataId': '1234', 'voiceIndex': 0, 'dryRun': False}
        result = self.run_action(p)
        self.assertTrue(result['persistenceVerified'])
        self.assertEqual(self.calls[0][1]['content']['selected_voice'], kept)

    def test_uncertain_write_cannot_be_retried_with_same_job(self):
        def unavailable(*args):
            self.calls.append('attempt')
            raise TimeoutError('sessionid=private-test')
        first = self.run_action(self.update, send=unavailable)
        self.assertEqual(first['upstream']['error']['code'], 'PERSONAL_WRITE_UNCERTAIN')
        second = self.run_action(self.update)
        self.assertEqual(second['upstream']['error']['code'], 'PERSONAL_RECONCILIATION_REQUIRED')
        self.assertEqual(self.calls, ['attempt'])
        self.assertNotIn(SESSION['cookie'], json.dumps(first))

    def test_completed_job_replay_does_not_write_or_require_auth(self):
        first = self.run_action(self.update)
        with patch.object(personal.web, '_load_session') as load:
            replay = personal.execute(self.update, 'test')
        self.assertTrue(replay['replayedOriginalReceipt'])
        self.assertEqual(replay['dataId'], first['dataId'])
        self.assertEqual(len(self.calls), 1)
        load.assert_not_called()

    def test_job_identity_conflict_does_not_write(self):
        self.run_action(self.update)
        result = self.run_action({**self.update, 'description': 'other'})
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_IDENTITY_CONFLICT')
        self.assertEqual(len(self.calls), 1)

    def test_wrong_receipt_subject_fails_closed(self):
        def wrong(*args):
            status, raw = self.fake_send(*args)
            envelope = json.loads(raw)
            envelope['data']['subject_id'] = SOURCE
            return status, json.dumps(envelope).encode()
        result = self.run_action(self.update, send=wrong)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_RECEIPT_MISMATCH')
        self.assertTrue(result['workspaceMutationAttempted'])

    def test_wrong_receipt_content_fails_closed(self):
        def wrong(*args):
            status, raw = self.fake_send(*args)
            envelope = json.loads(raw)
            envelope['data']['content']['description'] = 'Unexpected overwrite'
            return status, json.dumps(envelope).encode()
        result = self.run_action(self.update, send=wrong)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_RECEIPT_MISMATCH')

    def test_later_concurrent_edit_detected_by_readback(self):
        def changed(*args):
            result = self.fake_send(*args)
            self.records[TARGET]['data_id'] = '3000'
            return result
        result = self.run_action(self.update, send=changed)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_READBACK_MISMATCH')

    def test_unknown_content_field_or_video_subject_prevents_replacement(self):
        self.records[TARGET]['content']['unrecognized'] = 'future platform field'
        result = self.run_action(self.update, 'unknown')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_CONTENT_CONTRACT')
        del self.records[TARGET]['content']['unrecognized']
        self.records[TARGET]['content']['video_info'] = {'vid': 'a-video'}
        result = self.run_action(self.update, 'video')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_IMAGE_SUBJECT_REQUIRED')
        self.assertEqual(self.calls, [])

    def test_derived_caption_is_not_submitted_but_preserved_in_before_state(self):
        self.records[TARGET]['content']['caption'] = 'Platform-derived image caption'
        result = self.run_action(self.update)
        self.assertTrue(result['persistenceVerified'])
        self.assertNotIn('caption', self.calls[0][1]['content'])
        journal = json.loads(next((Path(self.tmp.name) / 'personal-edits').glob('*.json')).read_text())
        self.assertEqual(journal['beforeSubject']['content']['caption'], 'Platform-derived image caption')

    def test_private_journal_symlink_is_rejected(self):
        root = Path(self.tmp.name) / 'personal-edits'
        root.symlink_to(self.tmp.name, target_is_directory=True)
        result = self.run_action(self.update)
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_STATE_INVALID')
        self.assertEqual(self.calls, [])

    def test_invalid_index_and_source_contract_fail_before_submit(self):
        result = self.run_action({**self.bind, 'sourceVoiceIndex': 2}, 'bad-index')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_VOICE_NOT_FOUND')
        self.records[SOURCE]['content']['voice_list'][0]['source'] = 'unknown'
        result = self.run_action(self.bind, 'bad-source')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_VOICE_CONTRACT')
        self.assertEqual(self.calls, [])

    def delete_payload(self):
        return {'action': personal.DELETE, 'subjectId': TARGET, 'expectedDataId': '1234',
                'confirmPermanentDelete': True, 'dryRun': False}

    def test_delete_default_is_local_plan_and_requires_explicit_live_confirmation(self):
        p = {'action': personal.DELETE, 'subjectId': TARGET, 'expectedDataId': '1234'}
        with patch.object(personal.web, '_load_session') as load:
            result = personal.execute(p, 'plan')
        load.assert_not_called()
        self.assertTrue(result['dryRun'])
        self.assertEqual(result['requestPlan']['body'], {'subject_id': TARGET})
        self.assertEqual(result['requestPlan']['path'], '/mweb/v1/dreamina_subject/delete')
        for flag in (None, False):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                personal.validate({**p, 'dryRun': False, **({'confirmPermanentDelete': flag} if flag is not None else {})})

    def test_delete_needs_current_version_and_deletable_permission(self):
        p = self.delete_payload()
        result = self.run_action({**p, 'expectedDataId': '4321'}, 'delete-version')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_VERSION_CONFLICT')
        self.records[TARGET]['subject_control']['deletable'] = False
        result = self.run_action(p, 'delete-permission')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_NOT_DELETABLE')
        self.assertEqual(self.calls, [])

    def test_delete_requires_ack_and_complete_list_absence(self):
        def deleted(path, body, headers):
            self.calls.append((path, body))
            self.assertEqual(path, '/mweb/v1/dreamina_subject/delete')
            self.assertEqual(body, {'subject_id': TARGET})
            del self.records[TARGET]
            return 200, b'{"ret":"0"}'
        result = self.run_action(self.delete_payload(), send=deleted)
        self.assertTrue(result['deletionVerified'])
        self.assertFalse(result['restoreSupported'])
        self.assertEqual(result['deletedSubjectId'], TARGET)
        self.assertEqual(len(self.calls), 1)
        replay = self.run_action(self.delete_payload())
        self.assertTrue(replay['replayedOriginalReceipt'])
        self.assertEqual(len(self.calls), 1)

    def test_delete_ack_without_absence_is_uncertain(self):
        result = self.run_action(self.delete_payload(), send=lambda *args: (200, b'{"ret":"0","data":{}}'))
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_DELETE_READBACK_UNCERTAIN')
        self.assertTrue(result['workspaceMutationAttempted'])
        second = self.run_action(self.delete_payload())
        self.assertEqual(second['upstream']['error']['code'], 'PERSONAL_RECONCILIATION_REQUIRED')

    def test_delete_page_limit_or_network_error_is_not_absence(self):
        for index, code in enumerate(('WEB_LOOKUP_LIMIT', 'WEB_TRANSPORT_FAILED', 'WEB_PAGINATION_INVALID')):
            target = str(int(TARGET) + index)
            current = copy.deepcopy(self.records[TARGET])
            current['subject_id'] = target
            before = {'upstream': {'ok': True, 'data': {'subject': current}}}
            failed = {'upstream': {'ok': False, 'error': {'code': code}}}
            with patch.object(personal.web, '_load_session', return_value=SESSION), \
                 patch.object(personal.web, '_subject_lookup', side_effect=[before, failed]), \
                 patch.object(personal.web, '_send', return_value=(200, b'{"ret":"0"}')):
                result = personal.execute({**self.delete_payload(), 'subjectId': target}, 'absent-' + str(index))
            self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_DELETE_READBACK_UNCERTAIN')

    def test_delete_rejected_receipt_does_not_claim_deletion(self):
        result = self.run_action(self.delete_payload(), send=lambda *args: (403, b'{"ret":"1001","errmsg":"sessionid=private-test"}'))
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_WRITE_REJECTED')
        self.assertNotIn('deletionVerified', result)
        self.assertNotIn(SESSION['cookie'], json.dumps(result))

    def test_uncertain_create_blocks_new_job_even_when_remote_create_happened(self):
        def lost_response(*args):
            self.fake_send(*args)
            raise TimeoutError('Response lost after remote creation')
        first = self.run_action(self.create, 'uncertain-create-a', send=lost_response)
        self.assertEqual(first['upstream']['error']['code'], 'PERSONAL_WRITE_UNCERTAIN')
        second = self.run_action(self.create, 'uncertain-create-b')
        self.assertEqual(second['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.records[SOURCE]['data_id'], '1234')
        self.assertEqual(len(list((Path(self.tmp.name) / 'personal-edits').glob('pending-*.json'))), 1)

    def test_uncertain_create_fingerprint_normalizes_defaults_and_ignores_expected_version(self):
        def lost_response(*args):
            self.fake_send(*args)
            raise TimeoutError('lost')
        self.run_action(self.create, 'first', send=lost_response)
        alternate = {**self.create, 'includeAuxiliary': True, 'includeVoices': False, 'expectedSourceDataId': '5678'}
        result = self.run_action(alternate, 'new-job')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_target_write_blocks_different_action_and_changed_payload(self):
        def lost_response(*args):
            self.fake_send(*args)
            raise TimeoutError('lost')
        self.run_action(self.update, 'first', send=lost_response)
        p = {**self.delete_payload(), 'expectedDataId': self.records[TARGET]['data_id']}
        result = self.run_action(p, 'new-delete-job')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        result = self.run_action({**self.update, 'description': 'different', 'expectedDataId': self.records[TARGET]['data_id']}, 'new-update-job')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        self.assertEqual(len(self.calls), 1)

    def test_preflight_failure_does_not_create_cross_job_pending_guard(self):
        first = self.run_action({**self.update, 'expectedDataId': '5678'}, 'bad-version')
        self.assertEqual(first['upstream']['error']['code'], 'PERSONAL_VERSION_CONFLICT')
        self.assertEqual(list((Path(self.tmp.name) / 'personal-edits').glob('pending-*.json')), [])
        second = self.run_action(self.update, 'correct-version')
        self.assertTrue(second['persistenceVerified'])

    def test_successful_readback_releases_guard_for_next_explicit_edit(self):
        first = self.run_action(self.update, 'first')
        self.assertEqual(list((Path(self.tmp.name) / 'personal-edits').glob('pending-*.json')), [])
        second = self.run_action({**self.update, 'description': 'another explicit edit', 'expectedDataId': first['dataId']}, 'second')
        self.assertTrue(second['persistenceVerified'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(list((Path(self.tmp.name) / 'personal-edits').glob('pending-*.json')), [])

    def test_pending_guard_write_failure_cleans_only_own_unsubmitted_guard(self):
        real_dump = personal.json.dump
        def fail_guard_dump(record, *args, **kwargs):
            if isinstance(record, dict) and 'pendingIntentId' in record:
                raise OSError('synthetic guard storage failure')
            return real_dump(record, *args, **kwargs)
        with patch.object(personal.json, 'dump', side_effect=fail_guard_dump):
            first = self.run_action(self.create, 'storage-failed')
        self.assertFalse(first['workspaceMutationAttempted'])
        self.assertEqual(first['upstream']['error']['code'], 'PERSONAL_PREFLIGHT_FAILED')
        self.assertEqual(self.calls, [])
        self.assertEqual(list((Path(self.tmp.name) / 'personal-edits').glob('pending-*.json')), [])
        second = self.run_action(self.create, 'new-storage-recovered-job')
        self.assertTrue(second['persistenceVerified'])
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_create_cannot_bypass_guard_by_making_inherited_description_explicit(self):
        def lost_response(*args):
            self.fake_send(*args)
            raise TimeoutError('Response lost after remote create')
        self.run_action(self.create, 'inherited-description', send=lost_response)
        equivalent = {**self.create, 'description': self.records[SOURCE]['content']['description']}
        result = self.run_action(equivalent, 'explicit-same-description')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        result = self.run_action({**equivalent, 'includeVoices': True, 'includeAuxiliary': False}, 'changed-options')
        self.assertEqual(result['upstream']['error']['code'], 'PERSONAL_PENDING_WRITE')
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
