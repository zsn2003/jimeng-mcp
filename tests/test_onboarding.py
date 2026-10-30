"""Isolated onboarding checks. No real login, logout, browser or generation."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.response
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from jimeng_mcp import onboarding as onboard

SECRET = 'synthetic-secret-not-a-live-credential'
DEVICE = 'synthetic-device-reference'
VERIFY = 'https://jimeng.jianying.com/example-device-authorization'


def process(stdout='', code=0, stderr=''):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=code)


def canvas(data, ok=True):
    return process(json.dumps({'ok': ok, 'data': data}))


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'private'
        self.root.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {
            'JIMENG_MCP_DATA_DIR': str(self.root),
            'JIMENG_WEB_SESSION_FILE': str(self.root / 'web-session.json'),
            'DREAMINA_CANVAS_STATE_DIR': str(self.root / 'canvas'),
            'DREAMINA_CANVAS_PROFILE': 'jimeng-mcp',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        platform_patch = patch.object(onboard, '_platform', return_value='linux_amd64')
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        self.parser = argparse.ArgumentParser()
        onboard.configure_parser(self.parser.add_subparsers(dest='command', required=True))

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = onboard.run(self.parser.parse_args(argv))
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertNotIn(DEVICE, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def pending(self, provider='canvas'):
        onboard._write_private(onboard._pending(provider), json.dumps({
            'version': 1, 'provider': provider, 'device_code': DEVICE, 'createdAt': 1}).encode(), replace=True)

    def web_source(self, mode=0o600):
        path = Path(self.tmp.name) / 'provided-session.json'
        path.write_text(json.dumps({'enabled': True, 'cookie': SECRET, 'userAgent': 'Synthetic Test', 'headers': {}, 'actionHeaders': {}}))
        path.chmod(mode)
        return path

    def test_parser_defaults_alias_and_no_force_login(self):
        p = self.parser.parse_args(['install-cli'])
        self.assertEqual(p.provider, 'all')
        p = self.parser.parse_args(['auth', 'wait', 'plugin'])
        self.assertEqual(p.auth_action, 'wait')
        self.assertEqual(p.timeout, 120)
        self.assertEqual(self.parser.parse_args(['auth', 'status']).provider, 'all')

    def test_canvas_login_outputs_only_user_challenge_and_private_reference(self):
        response = canvas({'challenge': {'deviceCode': DEVICE, 'userCode': 'ABCD-EFGH', 'verificationUri': VERIFY},
                           'accessToken': SECRET, 'refreshToken': SECRET})
        with patch.object(onboard, '_invoke', return_value=response) as invoke:
            code, out, err = self.run_cli(['auth', 'login', 'canvas'])
        self.assertEqual(code, 0, err)
        invoke.assert_called_once_with('canvas', ['auth', 'login'])
        shown = json.loads(out)
        self.assertEqual(shown['verification_url'], VERIFY)
        self.assertFalse(shown['browserOpened'])
        file = onboard._pending('canvas')
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(file.read_text())['device_code'], DEVICE)
        self.assertNotIn(SECRET, file.read_text())

    def test_plugin_plain_text_headless_challenge_keeps_device_code_private(self):
        response = process('verification_uri: ' + VERIFY + '\nuser_code: ABCD\ndevice_code: ' + DEVICE + '\naccess_token: ' + SECRET + '\n')
        with patch.object(onboard, '_invoke', return_value=response) as invoke:
            code, out, err = self.run_cli(['auth', 'login', 'plugin'])
        self.assertEqual(code, 0, err)
        invoke.assert_called_once_with('plugin', ['login', '--headless'])
        self.assertEqual(json.loads(out)['user_code'], 'ABCD')
        self.assertTrue(onboard._pending('plugin').is_file())

    def test_plugin_json_challenge_is_supported_without_exposing_tokens(self):
        response = process(json.dumps({'verification_uri': VERIFY, 'user_code': 'ABCD', 'device_code': DEVICE, 'access_token': SECRET}))
        with patch.object(onboard, '_invoke', return_value=response):
            code, out, err = self.run_cli(['auth', 'login', 'plugin'])
        self.assertEqual(code, 0, err)
        self.assertNotIn('access_token', out)

    def test_reused_canvas_login_does_not_force_clear_existing_auth(self):
        self.pending()
        with patch.object(onboard, '_invoke', return_value=canvas({'state': {'loggedIn': True}})) as invoke:
            code, out, err = self.run_cli(['auth', 'login', 'canvas'])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)['reusedExistingLogin'])
        self.assertFalse(onboard._pending('canvas').exists())
        self.assertNotIn('--force', invoke.call_args.args[1])

    def test_unrecognized_plugin_login_requires_read_only_confirmation(self):
        with patch.object(onboard, '_invoke', side_effect=[process(SECRET), process('not logged in', code=1)]) as invoke:
            code, out, err = self.run_cli(['auth', 'login', 'plugin'])
        self.assertEqual(code, 1)
        self.assertEqual(invoke.call_args.args[1], ['user_credit'])
        self.assertEqual(out, '')
        self.assertFalse(onboard._pending('plugin').exists())

    def test_incomplete_or_unsafe_challenge_is_not_persisted(self):
        for uri in ('http://example.test/login', 'https://user:pass@example.test/login', 'javascript:alert(1)'):
            with self.subTest(uri=uri), patch.object(onboard, '_invoke', return_value=canvas({'challenge': {
                    'deviceCode': DEVICE, 'userCode': 'ABCD', 'verificationUri': uri}})):
                code, out, err = self.run_cli(['auth', 'login', 'canvas'])
            self.assertEqual(code, 1)
            self.assertFalse(onboard._pending('canvas').exists())

    def test_canvas_wait_uses_private_reference_and_removes_it_after_success(self):
        self.pending()
        with patch.object(onboard, '_invoke', return_value=canvas({'state': {'loggedIn': True, 'accessToken': SECRET}})) as invoke:
            code, out, err = self.run_cli(['auth', 'wait', 'canvas', '--timeout', '30'])
        self.assertEqual(code, 0, err)
        invoke.assert_called_once_with('canvas', ['auth', 'wait', '--device-code', DEVICE, '--timeout', '30s'], timeout=45)
        self.assertFalse(onboard._pending('canvas').exists())
        self.assertTrue(json.loads(out)['loggedIn'])

    def test_plugin_complete_uses_checklogin_then_independent_account_read(self):
        self.pending('plugin')
        with patch.object(onboard, '_invoke', side_effect=[process('Login successful ' + SECRET), process('{"total_credit":123}')]) as invoke:
            code, out, err = self.run_cli(['auth', 'complete', 'plugin', '--timeout', '30'])
        self.assertEqual(code, 0, err)
        self.assertEqual(invoke.call_args_list[0].args, ('plugin', ['login', 'checklogin', '--device_code=' + DEVICE, '--poll=30']))
        self.assertEqual(invoke.call_args_list[1].args, ('plugin', ['user_credit']))
        self.assertFalse(onboard._pending('plugin').exists())

    def test_wait_failure_preserves_pending_and_never_exposes_error_output(self):
        self.pending()
        with patch.object(onboard, '_invoke', return_value=process(json.dumps({'ok': False, 'error': SECRET}), code=1)):
            code, out, err = self.run_cli(['auth', 'complete', 'canvas'])
        self.assertEqual(code, 1)
        self.assertTrue(onboard._pending('canvas').exists())
        self.assertEqual(out, '')

    def test_wait_requires_bounded_timeout_and_existing_pending(self):
        with patch.object(onboard, '_invoke') as invoke:
            code, out, err = self.run_cli(['auth', 'complete', 'canvas', '--timeout', '601'])
            self.assertEqual(code, 1)
            code, out, err = self.run_cli(['auth', 'complete', 'canvas'])
        self.assertEqual(code, 1)
        invoke.assert_not_called()

    def test_status_filters_all_account_details_and_credentials(self):
        with patch.object(onboard, '_invoke', return_value=canvas({'loggedIn': True, 'accessToken': SECRET, 'userId': 'private-user-id'})):
            code, out, err = self.run_cli(['auth', 'status', 'canvas'])
        self.assertEqual(code, 0)
        self.assertNotIn('private-user-id', out)
        self.assertTrue(json.loads(out)['authentication'][0]['loggedIn'])
        with patch.object(onboard, '_invoke', return_value=process('{"ret":"0","total_credit":321,"access_token":"' + SECRET + '"}')):
            code, out, err = self.run_cli(['auth', 'status', 'plugin'])
        self.assertEqual(code, 0)
        self.assertNotIn('321', out)
        self.assertTrue(json.loads(out)['authentication'][0]['loggedIn'])

    def test_web_status_distinguishes_configured_from_authenticated(self):
        code, out, err = self.run_cli(['auth', 'status', 'web'])
        self.assertFalse(json.loads(out)['authentication'][0]['configured'])
        target = self.root / 'web-session.json'
        target.write_text('{}')
        target.chmod(0o600)
        with patch.object(onboard, '_validate_web_file'):
            code, out, err = self.run_cli(['auth', 'status', 'web'])
        state = json.loads(out)['authentication'][0]
        self.assertTrue(state['configured'])
        self.assertFalse(state['serverLoginVerified'])

    def test_logout_only_runs_explicit_provider_contract(self):
        self.pending('canvas')
        with patch.object(onboard, '_invoke', return_value=canvas({'loggedOut': True})) as invoke:
            code, out, err = self.run_cli(['auth', 'logout', 'canvas'])
        self.assertEqual(code, 0)
        invoke.assert_called_once_with('canvas', ['auth', 'logout'])
        self.pending('plugin')
        with patch.object(onboard, '_invoke', return_value=process('Logged out')) as invoke:
            code, out, err = self.run_cli(['auth', 'logout', 'plugin'])
        self.assertEqual(code, 0)
        invoke.assert_called_once_with('plugin', ['logout'])
        self.assertEqual(json.loads(out)['scope'], 'official_cli_shared_login_store')

    def test_web_import_validates_private_temporary_then_atomic_destination(self):
        source = self.web_source()
        def validate(path):
            self.assertNotEqual(path, source)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent, self.root)
            self.assertEqual(json.loads(path.read_text())['cookie'], SECRET)
        with patch.object(onboard, '_validate_web_file', side_effect=validate):
            code, out, err = self.run_cli(['auth', 'import-web', str(source)])
        self.assertEqual(code, 0, err)
        target = self.root / 'web-session.json'
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertEqual(list(self.root.glob('.web-import-*')), [])
        self.assertFalse(json.loads(out)['serverLoginVerified'])

    def test_web_import_rejects_public_source_and_symlink(self):
        source = self.web_source(0o644)
        with patch.object(onboard, '_validate_web_file') as validate:
            code, out, err = self.run_cli(['auth', 'import-web', str(source)])
        self.assertEqual(code, 1)
        validate.assert_not_called()
        source.chmod(0o600)
        link = Path(self.tmp.name) / 'linked.json'
        link.symlink_to(source)
        code, out, err = self.run_cli(['auth', 'import-web', str(link)])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / 'web-session.json').exists())

    def test_web_import_failure_preserves_existing_and_cleans_temporary(self):
        source = self.web_source()
        target = self.root / 'web-session.json'
        target.write_text('{"original":true}')
        target.chmod(0o600)
        with patch.object(onboard, '_validate_web_file', side_effect=onboard.OnboardingError('WEB_SESSION_INVALID', 'invalid')):
            code, out, err = self.run_cli(['auth', 'import-web', str(source), '--replace'])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(), '{"original":true}')
        self.assertEqual(list(self.root.glob('.web-import-*')), [])

    def test_web_import_requires_replace_and_rejects_external_session_path(self):
        source = self.web_source()
        target = self.root / 'web-session.json'
        target.write_text('{}')
        target.chmod(0o600)
        with patch.object(onboard, '_validate_web_file') as validate:
            code, out, err = self.run_cli(['auth', 'import-web', str(source)])
        self.assertEqual(code, 1)
        validate.assert_not_called()
        with patch.dict(os.environ, {'JIMENG_WEB_SESSION_FILE': str(Path(self.tmp.name) / 'outside.json')}):
            code, out, err = self.run_cli(['auth', 'import-web', str(source), '--replace'])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_text(), '{}')

    def test_bridge_validator_restores_environment_on_success_and_error(self):
        class SessionError(Exception):
            pass
        temporary = self.web_source()
        def check():
            self.assertEqual(os.environ['JIMENG_WEB_SESSION_FILE'], str(temporary))
        fake = SimpleNamespace(SessionError=SessionError, _load_session=check)
        original = os.environ['JIMENG_WEB_SESSION_FILE']
        with patch('jimeng_mcp.bridge_loader.load_bridge'), patch.object(onboard.importlib, 'import_module', return_value=fake):
            onboard._validate_web_file(temporary)
            self.assertEqual(os.environ['JIMENG_WEB_SESSION_FILE'], original)
            fake._load_session = Mock(side_effect=SessionError(SECRET))
            with self.assertRaises(onboard.OnboardingError) as error:
                onboard._validate_web_file(temporary)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(os.environ['JIMENG_WEB_SESSION_FILE'], original)

    def test_subprocess_argv_disables_interaction_and_preserves_home(self):
        executable = self.root / 'fake-canvas'
        executable.write_bytes(b'not executed')
        executable.chmod(0o700)
        original_home = os.environ.get('HOME')
        with patch.dict(os.environ, {'DREAMINA_CANVAS_BIN': str(executable)}), \
             patch.object(onboard.subprocess, 'run', return_value=canvas({'loggedIn': False})) as run:
            onboard._invoke('canvas', ['auth', 'status'])
        argv = run.call_args.args[0]
        self.assertEqual(argv, [str(executable), 'auth', 'status', '--format', 'json', '--non-interactive', '--profile', 'jimeng-mcp'])
        self.assertEqual(run.call_args.kwargs['env'].get('HOME'), original_home)
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_subprocess_timeout_does_not_echo_sensitive_argv_or_output(self):
        executable = self.root / 'fake-canvas'
        executable.write_bytes(b'not executed')
        executable.chmod(0o700)
        error = subprocess.TimeoutExpired([str(executable), SECRET], 1, output=SECRET)
        with patch.dict(os.environ, {'DREAMINA_CANVAS_BIN': str(executable)}), \
             patch.object(onboard.subprocess, 'run', side_effect=error):
            code, out, err = self.run_cli(['auth', 'login', 'canvas'])
        self.assertEqual(code, 1)
        self.assertIn('CLI_TIMEOUT', err)

    def test_download_checks_pinned_sha_without_running_installer(self):
        payload = b'verified official binary fixture'
        response = io.BytesIO(payload)
        response.status = 200
        opener = SimpleNamespace(open=Mock(return_value=response))
        target = self.root / 'download'
        with patch.object(onboard.urllib.request, 'build_opener', return_value=opener), patch.object(onboard.subprocess, 'run') as execute:
            size = onboard._download(onboard.CANVAS_BASE + 'dreamina-canvas_linux_amd64', target, hashlib.sha256(payload).hexdigest())
        self.assertEqual(size, len(payload))
        self.assertEqual(target.read_bytes(), payload)
        execute.assert_not_called()
        self.assertIsNone(onboard._PinnedDownloadRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_canvas_redirect_allows_only_one_exact_get_to_pinned_cdn(self):
        for platform_name in onboard.HASHES['canvas']:
            filename = 'dreamina-canvas_' + platform_name
            origin = onboard.CANVAS_BASE + filename
            destination = onboard.CANVAS_CDN_BASE + filename
            handler = onboard._PinnedDownloadRedirect()
            request = onboard.urllib.request.Request(origin, headers={'Authorization': SECRET, 'Cookie': SECRET})
            redirected = handler.redirect_request(request, None, 302, '', {}, destination)
            self.assertEqual(redirected.full_url, destination)
            self.assertEqual(redirected.get_method(), 'GET')
            self.assertIsNone(redirected.data)
            self.assertIsNone(redirected.get_header('Authorization'))
            self.assertIsNone(redirected.get_header('Cookie'))
            self.assertIsNone(handler.redirect_request(request, None, 302, '', {}, destination))
            self.assertIsNone(handler.redirect_request(redirected, None, 302, '', {}, destination))

    def test_redirect_rejects_other_hosts_paths_files_methods_and_statuses(self):
        filename = 'dreamina-canvas_linux_amd64'
        origin = onboard.CANVAS_BASE + filename
        destination = onboard.CANVAS_CDN_BASE + filename
        invalid = [
            (origin, destination.replace('https://', 'http://'), 302, None),
            (origin, destination + '?token=unexpected', 302, None),
            (origin, destination + '#fragment', 302, None),
            (origin, destination.replace('lf3-lv-buz.vlabstatic.com', 'other.example'), 302, None),
            (origin, destination.replace('1_0_0_53', '1_0_0_54'), 302, None),
            (origin, onboard.CANVAS_CDN_BASE + 'dreamina-canvas_darwin_arm64', 302, None),
            (origin + '?query=1', destination, 302, None),
            (onboard.PLUGIN_BASE + 'dreamina_cli_linux_amd64', destination, 302, None),
            (onboard.CANVAS_CDN_BASE + filename, destination, 302, None),
            (origin, destination, 301, None),
            (origin, destination, 307, None),
            (origin, destination, 302, b'body'),
        ]
        for source, target, code, data in invalid:
            with self.subTest(source=source, target=target, code=code):
                request = onboard.urllib.request.Request(source, data=data)
                self.assertIsNone(onboard._PinnedDownloadRedirect().redirect_request(request, None, code, '', {}, target))

    def test_download_follows_offline_canvas_302_and_still_checks_sha(self):
        filename = 'dreamina-canvas_linux_amd64'
        origin, destination = onboard.CANVAS_BASE + filename, onboard.CANVAS_CDN_BASE + filename
        payload = b'official artifact bytes after the allowed redirect'
        requests = []
        class OfflineHTTPS(onboard.urllib.request.HTTPSHandler):
            def https_open(self, request):
                requests.append(request.full_url)
                headers = Message()
                if request.full_url == origin:
                    headers['Location'] = destination
                    response = urllib.response.addinfourl(io.BytesIO(), headers, origin, 302)
                    response.msg = 'Found'
                    return response
                if request.full_url == destination:
                    response = urllib.response.addinfourl(io.BytesIO(payload), headers, destination, 200)
                    response.msg = 'OK'
                    return response
                raise AssertionError('Unexpected network destination')
        original_build = onboard.urllib.request.build_opener
        def build(handler):
            return original_build(OfflineHTTPS(), handler)
        with patch.object(onboard.urllib.request, 'build_opener', side_effect=build):
            target = self.root / 'redirected-download'
            size = onboard._download(origin, target, hashlib.sha256(payload).hexdigest())
            self.assertEqual(size, len(payload))
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(requests, [origin, destination])
            with self.assertRaises(onboard.OnboardingError) as error:
                onboard._download(origin, target, '0' * 64)
            self.assertEqual(error.exception.code, 'CHECKSUM_MISMATCH')

    def test_download_checksum_mismatch_fails_closed(self):
        response = io.BytesIO(b'wrong bytes')
        response.status = 200
        with patch.object(onboard.urllib.request, 'build_opener', return_value=SimpleNamespace(open=lambda *a, **k: response)):
            with self.assertRaises(onboard.OnboardingError) as error:
                onboard._download(onboard.CANVAS_BASE + 'dreamina-canvas_linux_amd64', self.root / 'download', '0' * 64)
        self.assertEqual(error.exception.code, 'CHECKSUM_MISMATCH')

    def test_installer_uses_fixed_official_urls_private_paths_and_hashes(self):
        seen = []
        def download(url, path, expected):
            seen.append((url, expected))
            path.write_bytes(b'fixture official binary')
            return len(path.read_bytes())
        with patch.object(onboard, '_download', side_effect=download), patch.object(onboard.subprocess, 'run') as execute:
            code, out, err = self.run_cli(['install-cli', 'all'])
        self.assertEqual(code, 0, err)
        execute.assert_not_called()
        self.assertEqual(seen, [(onboard.CANVAS_BASE + 'dreamina-canvas_linux_amd64', onboard.HASHES['canvas']['linux_amd64']),
                               (onboard.PLUGIN_BASE + 'dreamina_cli_linux_amd64', onboard.HASHES['plugin']['linux_amd64'])])
        for filename in ('dreamina-canvas', 'dreamina'):
            self.assertEqual((self.root / 'bin' / filename).stat().st_mode & 0o777, 0o700)
        self.assertEqual(list((self.root / 'bin').glob('.cli-download-*')), [])
        self.assertFalse(json.loads(out)['homeOrShellConfigurationModified'])

    def test_installer_rejects_cross_platform_and_existing_file_without_replace(self):
        with patch.object(onboard, '_download') as download:
            code, out, err = self.run_cli(['install-cli', 'canvas', '--platform', 'darwin_arm64'])
        self.assertEqual(code, 1)
        download.assert_not_called()
        directory = self.root / 'bin'
        directory.mkdir(mode=0o700)
        target = directory / 'dreamina-canvas'
        target.write_bytes(b'prior binary')
        target.chmod(0o700)
        with patch.object(onboard, '_download') as download:
            code, out, err = self.run_cli(['install-cli', 'canvas'])
        self.assertEqual(code, 1)
        download.assert_not_called()
        self.assertEqual(target.read_bytes(), b'prior binary')

    def test_failed_replacement_preserves_installed_binary_and_cleans_copy(self):
        directory = self.root / 'bin'
        directory.mkdir(mode=0o700)
        target = directory / 'dreamina-canvas'
        target.write_bytes(b'prior binary')
        target.chmod(0o700)
        with patch.object(onboard, '_download', side_effect=onboard.OnboardingError('CHECKSUM_MISMATCH', 'mismatch')):
            code, out, err = self.run_cli(['install-cli', 'canvas', '--replace'])
        self.assertEqual(code, 1)
        self.assertEqual(target.read_bytes(), b'prior binary')
        self.assertEqual(list(directory.glob('.cli-download-*')), [])

    def test_existing_verified_executable_is_not_downloaded_again(self):
        directory = self.root / 'bin'
        directory.mkdir(mode=0o700)
        target = directory / 'dreamina-canvas'
        payload = b'verified installed fixture'
        target.write_bytes(payload)
        target.chmod(0o700)
        with patch.dict(onboard.HASHES['canvas'], {'linux_amd64': hashlib.sha256(payload).hexdigest()}), \
             patch.object(onboard, '_download') as download:
            code, out, err = self.run_cli(['install-cli', 'canvas'])
        self.assertEqual(code, 0, err)
        download.assert_not_called()
        self.assertTrue(json.loads(out)['installed'][0]['alreadyInstalled'])

    def test_doctor_default_reads_versions_without_auth_or_generation(self):
        def invoke(provider, args, **kwargs):
            self.assertEqual(args, ['version'])
            return canvas({'version': onboard.CANVAS_VERSION, 'token': SECRET}) if provider == 'canvas' else process(json.dumps({'version': onboard.PLUGIN_VERSION, 'token': SECRET}))
        with patch.object(onboard, '_binary', return_value=self.root / 'dummy'), patch.object(onboard, '_invoke', side_effect=invoke) as invoke_mock:
            code, out, err = self.run_cli(['doctor'])
        self.assertEqual(code, 0, err)
        self.assertEqual(invoke_mock.call_count, 2)
        state = json.loads(out)
        self.assertTrue(state['ready'])
        self.assertFalse(state['authChecked'])
        self.assertFalse(state['optionalWebSession']['serverLoginVerified'])

    def test_doctor_flags_unreviewed_versions_without_printing_raw_output(self):
        with patch.object(onboard, '_binary', return_value=self.root / 'dummy'), \
             patch.object(onboard, '_invoke', side_effect=[canvas({'version': 'old-version'}), process('{"version":"1.4.22"}')]):
            code, out, err = self.run_cli(['doctor'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['cli']['canvas']['matchesTestedVersion'])

    def test_private_directory_and_binary_symlinks_fail_closed(self):
        self.root.chmod(0o755)
        code, out, err = self.run_cli(['doctor'])
        self.assertEqual(code, 1)
        self.root.chmod(0o700)
        directory = self.root / 'bin'
        directory.mkdir(mode=0o700)
        outside = Path(self.tmp.name) / 'outside'
        outside.write_bytes(b'do not modify')
        (directory / 'dreamina-canvas').symlink_to(outside)
        with patch.object(onboard, '_download') as download:
            code, out, err = self.run_cli(['install-cli', 'canvas', '--replace'])
        self.assertEqual(code, 1)
        download.assert_not_called()
        self.assertEqual(outside.read_bytes(), b'do not modify')

    def test_main_entry_web_import_uses_real_fixed_schema_without_network(self):
        source = self.web_source()
        directory = Path(self.tmp.name) / 'integration-private'
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        base = [sys.executable, '-m', 'jimeng_mcp', '--data-dir', str(directory), 'auth', 'import-web', str(source)]
        imported = subprocess.run(base, capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertNotIn(SECRET, imported.stdout + imported.stderr)
        saved = (directory / 'web-session.json').read_bytes()
        self.assertEqual(json.loads(saved)['cookie'], SECRET)
        self.assertEqual((directory / 'web-session.json').stat().st_mode & 0o777, 0o600)
        invalid = json.loads(source.read_text())
        invalid['headers'] = {'Authorization': SECRET}
        source.write_text(json.dumps(invalid))
        rejected = subprocess.run(base + ['--replace'], capture_output=True, text=True, env=env, timeout=20)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn(SECRET, rejected.stdout + rejected.stderr)
        self.assertEqual((directory / 'web-session.json').read_bytes(), saved)
        invalid['headers'] = {}
        invalid['validUntil'] = 1
        source.write_text(json.dumps(invalid))
        expired = subprocess.run(base + ['--replace'], capture_output=True, text=True, env=env, timeout=20)
        self.assertNotEqual(expired.returncode, 0)
        self.assertNotIn(SECRET, expired.stdout + expired.stderr)
        self.assertEqual((directory / 'web-session.json').read_bytes(), saved)


if __name__ == '__main__':
    unittest.main()
