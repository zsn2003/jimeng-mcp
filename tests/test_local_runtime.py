import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from jimeng_mcp.config import configure_environment, save_settings, read_settings, DEFAULTS
from jimeng_mcp.runtime import LocalRuntime


class Adapter:
    def validate(self, payload):
        pass
    def capabilities(self):
        return {'actions': ['account.status'], 'readOnly': True}
    def studio_catalog(self):
        return {'operations': []}
    def execute(self, payload, job_id):
        return {'action': payload['action'], 'upstream': {'ok': True, 'data': {}}, 'generationSubmitted': False}


class LocalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {}, clear=False); self.env.start()
        self.root = configure_environment(Path(self.temp.name) / 'private')
        self.runtime = LocalRuntime(self.root, adapter_override=Adapter())
    def tearDown(self):
        self.runtime.close(); self.env.stop(); self.temp.cleanup()
    def call(self, method, params=None, id=1):
        return self.runtime.dispatch({'jsonrpc': '2.0', 'id': id, 'method': method, 'params': params or {}})
    def initialize(self):
        response = self.call('initialize', {'protocolVersion': '2025-06-18', 'clientInfo': {'name': 'test', 'version': '1'}, 'capabilities': {}})
        self.assertEqual(response['result']['serverInfo']['name'], 'jimeng-mcp')
        self.runtime.dispatch({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    def test_protocol_requires_handshake_and_annotations_do_not_alias(self):
        self.assertEqual(self.call('tools/list')['error']['code'], -32002)
        self.initialize()
        tools = {x['name']: x for x in self.call('tools/list')['result']['tools']}
        self.assertEqual(len(tools), 10)
        self.assertTrue(tools['capabilities']['annotations']['readOnlyHint'])
        self.assertTrue(tools['capabilities']['annotations']['openWorldHint'])
        self.assertFalse(tools['artifact_manifest']['annotations']['readOnlyHint'])
        self.assertTrue(tools['submit_job']['annotations']['destructiveHint'])
    def test_persisted_job_and_idempotency_survive_restart(self):
        self.initialize()
        args = {'name': 'submit_job', 'arguments': {'idempotencyKey': 'local-read-v1', 'payload': {'action': 'account.status'}}}
        first = self.call('tools/call', args)['result']['structuredContent']['data']
        for _ in range(100):
            job = self.runtime.service.get_job(first['id'])
            if job['status'] == 'COMPLETED': break
            time.sleep(.01)
        self.assertEqual(job['status'], 'COMPLETED')
        self.runtime.close()
        self.runtime = LocalRuntime(self.root, adapter_override=Adapter())
        self.initialize()
        replay = self.call('tools/call', args)['result']['structuredContent']
        self.assertEqual(replay['data']['id'], first['id'])
        self.assertTrue(replay['idempotentReplayed'])
    def test_second_client_cannot_recover_active_database(self):
        with self.assertRaisesRegex(RuntimeError, 'active bridge process'):
            LocalRuntime(self.root, adapter_override=Adapter())
    def test_artifact_export_returns_verified_local_file(self):
        raw = b'local artifact test'; source = self.root / 'source.mp3'; source.write_bytes(raw)
        self.runtime.service.adapter.artifact_descriptor = lambda p: {'path': str(source), 'filename': 'source.mp3', 'byteSize': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'format': 'mp3'}
        self.initialize()
        request = {'name': 'artifact_manifest', 'arguments': {'projectId': '00000000-0000-4000-8000-000000000001', 'artifactId': '00000000-0000-4000-8000-000000000002'}}
        result = self.call('tools/call', request)['result']
        self.assertFalse(result['isError'])
        data = result['structuredContent']['data']; exported = Path(data['localPath'])
        self.assertEqual(exported.read_bytes(), raw)
        self.assertEqual(exported.parent, self.root / 'exports')
        self.assertNotIn('downloadRoute', data)
        self.assertFalse(data['requiresBearerAuth'])
        self.assertEqual(exported.stat().st_mode & 0o777, 0o600)
    def test_local_routes_cannot_reach_arbitrary_urls(self):
        code, value, _ = self.runtime.rpc.backend.request('GET', 'https://example.invalid/private')
        self.assertEqual(code, 400)
        self.assertEqual(value['error']['code'], 'INVALID_LOCAL_ROUTE')


class ConfigTests(unittest.TestCase):
    def test_settings_isolate_host_policy_and_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'JIMENG_MAX_CREDITS': 'unlimited', 'JIMENG_POLICY_STATE_DIR': '/unrelated', 'JIMENG_PROJECT_POLICY_FILE': '/unrelated.json'}):
            root = configure_environment(Path(tmp) / 'profile')
            self.assertEqual(os.environ['JIMENG_MAX_CREDITS'], '0')
            self.assertEqual(os.environ['JIMENG_POLICY_STATE_DIR'], str(root / 'policy'))
            self.assertNotIn('JIMENG_PROJECT_POLICY_FILE', os.environ)
            values = {**DEFAULTS, 'max_credits': 20}; save_settings(root, values)
            self.assertEqual(read_settings(root)['max_credits'], 20)
            configure_environment(root)
            self.assertEqual(os.environ['JIMENG_MAX_CREDITS'], '20')
            with self.assertRaises(ValueError): save_settings(root, {**values, 'max_credits': 100001})
    def test_existing_public_directory_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'public'; root.mkdir(mode=0o755)
            with self.assertRaises(ValueError): configure_environment(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
    def test_symlink_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / 'link'; link.symlink_to(tmp, target_is_directory=True)
            with self.assertRaises(ValueError): configure_environment(link)


class StdioProcessTests(unittest.TestCase):
    def test_clean_process_speaks_json_lines_without_http(self):
        root = Path(__file__).resolve().parents[1]
        messages = [
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'integration-test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'action_schema','arguments':{'action':'personal.subject.delete'}}},
            {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'capabilities','arguments':{}}}]
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PYTHONPATH=str(root / 'src'))
            result = subprocess.run([sys.executable,'-m','jimeng_mcp','--data-dir',str(Path(tmp)/'state'),'serve'], input='\n'.join(json.dumps(x) for x in messages)+'\n', text=True, capture_output=True, timeout=30, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r['id'] for r in rows], [1,2,3,4])
            self.assertEqual(len(rows[1]['result']['tools']),10)
            cap = rows[3]['result']['structuredContent']['data']
            self.assertFalse(cap['localRuntime']['networkListener'])
            self.assertEqual(len(cap['backend']['actions']),122)
            self.assertEqual(cap['backend']['liveCreditCeiling'],0)
            self.assertEqual(cap['backend']['studioCatalog']['kind'],'coverage_and_acceptance')
            self.assertEqual(result.stderr, '')


if __name__ == '__main__': unittest.main()
