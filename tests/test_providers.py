import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class ProviderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.p = importlib.import_module('reviewer.providers')
            self.q = importlib.import_module('reviewer.qualification')
        except ModuleNotFoundError:
            self.fail('Personal provider adapters and qualification are not implemented')
        from reviewer.store import Store
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / 'state')

    def test_personal_claude_environment_drops_inherited_billing_keys(self):
        provider = {'name': 'claude', 'kind': 'claude', 'profile': str(self.root), 'model': 'fable'}
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'wrong-account', 'OPENAI_API_KEY': 'wrong-account',
                                    'ANTHROPIC_BASE_URL': 'https://wrong.example', 'CODEX_HOME': '/wrong'}):
            env = self.p.environment(provider)
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('ANTHROPIC_BASE_URL', env)
        self.assertNotIn('CODEX_HOME', env)
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.root))

    def test_glm_uses_only_explicit_profile_token_and_endpoint(self):
        (self.root / '.api_key').write_text('fixture-token\n')
        provider = {'name': 'glm', 'kind': 'glm', 'profile': str(self.root), 'model': 'glm-5.3',
                    'endpoint': 'https://api.z.ai/api/anthropic'}
        env = self.p.environment(provider)
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'fixture-token')
        self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.z.ai/api/anthropic')
        (self.root / '.api_key').unlink()
        with self.assertRaises(self.p.Unavailable):
            self.p.environment(provider)

    def test_claude_command_disables_tools_hooks_and_mcp(self):
        cmd = self.p.command({'kind': 'claude', 'executable': '/usr/bin/claude', 'model': 'fable'}, self.root)
        self.assertEqual(cmd[cmd.index('--tools') + 1], '')
        self.assertIn('--safe-mode', cmd)
        self.assertIn('--strict-mcp-config', cmd)
        self.assertIn('--json-schema', cmd)
        self.assertEqual(cmd[cmd.index('--max-turns') + 1], '3')

    def test_codex_command_uses_subscription_home_and_no_user_tools(self):
        cmd = self.p.command({'kind': 'codex', 'executable': '/usr/bin/codex', 'model': 'gpt-6-astra'}, self.root)
        self.assertIn('--ignore-user-config', cmd)
        self.assertIn('--ignore-rules', cmd)
        self.assertIn('features.shell_tool=false', cmd)
        self.assertIn('features.apps=false', cmd)
        self.assertIn('--output-schema', cmd)

    def test_unknown_errors_are_not_classified_as_retryable(self):
        for message in ['HTTP 429 rate limit', 'usage limit reached', 'ECONNREFUSED',
                        'authentication failed', 'overloaded_error', 'connection timed out']:
            self.assertTrue(self.p.is_unavailable(message), message)
        for message in ['unknown flag --schema', 'invalid JSON', 'out of turns', 'segmentation fault']:
            self.assertFalse(self.p.is_unavailable(message), message)

    def test_availability_evidence_is_recognized_in_its_usual_forms(self):
        for message in ['429 Too Many Requests', '503 Service Unavailable', 'status: 502',
                        'http 529', 'rate-limit exceeded', 'quota exceeded', 'Overloaded',
                        'not logged in', 'unauthorized', 'invalid api key',
                        'ECONNRESET', 'EAI_AGAIN', 'connection was refused by the host',
                        'could not resolve host', 'network is unreachable']:
            self.assertTrue(self.p.is_unavailable(message), message)

    def test_ambiguous_words_need_their_envelope_to_be_about_credentials_or_exhaustion(self):
        # "invalid token" is a dead credential in an auth envelope and a syntax error in a
        # parser one; "quota" is exhaustion in a service envelope and a field name in a schema.
        for message in ['JSON parse error: invalid token at position 7',
                        'Schema validation failed: unexpected property "quota"',
                        'missing token field in response object',
                        'invalid credentials field in the decision object'.replace('credentials field', 'verdict field')]:
            self.assertFalse(self.p.is_unavailable(message), message)
        for message in ['Authentication error: invalid token', 'HTTP 401 invalid token',
                        'credential expired token', 'quota exceeded', 'exhausted monthly quota']:
            self.assertTrue(self.p.is_unavailable(message), message)
        # a login/auth check is itself the credential envelope, so bare wording counts there
        self.assertTrue(self.p.is_unavailable('invalid token', auth_context=True))
        self.assertFalse(self.p.is_unavailable('invalid token'))

    def test_protocol_errors_never_claim_the_provider_is_unavailable(self):
        # Malformed output must be held for inspection, not spend a second model call and a
        # 24-hour cooldown. A status number or the word "token" inside ordinary parser text
        # is not evidence that the provider is down.
        for message in ['Invalid JSON: unexpected token at position 7',
                        'JSON parsing failed at column 429',
                        'schema validation failed: 502 fields checked',
                        'response did not match schema at line 503',
                        'Unexpected end of JSON input',
                        'invalid verdict token in decision 4',
                        'decision 429 is not one of the submitted candidates',
                        'expected 503 characters, got 12']:
            self.assertFalse(self.p.is_unavailable(message), message)

    def test_auth_command_protocol_error_is_not_an_availability_failure(self):
        exe = self.root / 'fake-cli'
        exe.write_text('#!/usr/bin/python3\nimport sys\nprint("unknown flag --json",file=sys.stderr)\nsys.exit(2)\n')
        exe.chmod(0o700)
        provider = {'name': 'claude', 'kind': 'claude', 'executable': str(exe),
                    'model': 'fable', 'profile': str(self.root)}
        with self.assertRaises(self.p.InvalidReview):
            self.p.preflight(provider, {}, 3, self.root)

    def test_a_failed_envelope_naming_a_parser_token_is_held_not_retried(self):
        # The end-to-end path: authenticated CLI, failed envelope whose text merely mentions
        # an ambiguous word. It must be InvalidReview, so the worker holds it rather than
        # cooling the provider down and paying for a second model call.
        exe = self.root / 'fake-cli'
        exe.write_text('#!/usr/bin/python3\nimport sys,json\n'
                       'print(json.dumps({"loggedIn":True,"authMethod":"claude.ai"}) if "auth" in sys.argv '
                       'else json.dumps({"is_error":True,'
                       '"result":"JSON parse error: invalid token at position 7"}))\n'
                       'sys.exit(0 if "auth" in sys.argv else 1)\n')
        exe.chmod(0o700)
        provider = {'name': 'claude', 'kind': 'claude', 'executable': str(exe),
                    'model': 'fable', 'profile': str(self.root)}
        with self.assertRaises(self.p.InvalidReview):
            self.p.invoke(provider, self.q.examples(), 5, self.root)

    def test_a_failed_envelope_with_real_availability_evidence_still_falls_back(self):
        exe = self.root / 'fake-cli'
        exe.write_text('#!/usr/bin/python3\nimport sys,json\n'
                       'print(json.dumps({"loggedIn":True,"authMethod":"claude.ai"}) if "auth" in sys.argv '
                       'else json.dumps({"is_error":True,"result":"429 Too Many Requests"}))\n'
                       'sys.exit(0 if "auth" in sys.argv else 1)\n')
        exe.chmod(0o700)
        provider = {'name': 'claude', 'kind': 'claude', 'executable': str(exe),
                    'model': 'fable', 'profile': str(self.root)}
        with self.assertRaises(self.p.Unavailable):
            self.p.invoke(provider, self.q.examples(), 5, self.root)

    def test_non_object_claude_envelope_is_held_as_invalid(self):
        exe = self.root / 'fake-cli'
        exe.write_text('#!/usr/bin/python3\nimport sys,json\n'
                       'print(json.dumps({"loggedIn":True,"authMethod":"claude.ai"}) '
                       'if "auth" in sys.argv else "[]")\n')
        exe.chmod(0o700)
        provider = {'name': 'claude', 'kind': 'claude', 'executable': str(exe),
                    'model': 'fable', 'profile': str(self.root)}
        with self.assertRaises(self.p.InvalidReview):
            self.p.invoke(provider, self.q.examples(), 3, self.root)

    def test_real_child_timeout_kills_descendant_process(self):
        code = ('import subprocess,sys,time,pathlib\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                'pathlib.Path("child.pid").write_text(str(p.pid))\n'
                'time.sleep(60)\n')
        start = time.monotonic()
        with self.assertRaises(self.p.Unavailable):
            self.p.process([sys.executable, '-c', code], {}, '', 0.5, self.root)
        self.assertLess(time.monotonic() - start, 4)
        pid = int((self.root / 'child.pid').read_text())
        proc = Path(f'/proc/{pid}/stat')
        # A killed child may briefly remain as a zombie until its new parent reaps it.
        self.assertTrue(not proc.exists() or proc.read_text().split()[2] == 'Z')

    def test_real_child_output_is_persisted_and_parsed(self):
        result = self.p.process([sys.executable, '-c', 'import json;print(json.dumps({"ok":True}))'],
                                {}, '', 3, self.root)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(json.loads((self.root / 'stdout.txt').read_text()), {'ok': True})

    def test_existing_profile_log_does_not_trigger_output_limit(self):
        (self.root / 'profile.log').write_bytes(b'x' * 4_100_000)
        result = self.p.process([sys.executable, '-c',
                                 'with open("profile.log","a") as f: f.write("updated")'],
                                {}, '', 3, self.root)
        self.assertEqual(result['returncode'], 0)
        self.assertTrue((self.root / 'profile.log').read_bytes().endswith(b'updated'))

    def test_child_output_limit_holds_a_run_without_exhausting_disk(self):
        with self.assertRaises(self.p.InvalidReview):
            self.p.process([sys.executable, '-c', 'import sys;sys.stdout.write("x"*5000000)'],
                           {}, '', 3, self.root)
        self.assertLessEqual((self.root / 'stdout.txt').stat().st_size, 4_000_000)

    def test_accept_everything_fails_qualification(self):
        provider = {'name': 'claude', 'kind': 'claude', 'model': 'test', 'enabled': True}
        def invoke(provider, packet, timeout, path):
            return {'decisions': [{'id': c['id'], 'verdict': 'accept', 'reason': 'Accept it.',
                                   'target': '', 'note': 'A proposed note.',
                                   'evidence': [{'source': 's1', 'quote': c['sources'][0]['text'][:30]}]}
                                  for c in packet['candidates']]}
        result = self.q.qualify(self.store, {'providers': [provider]}, 'claude', invoke)
        self.assertFalse(result['passed'])
        self.assertFalse(self.store.get_meta('qualified:claude')['passed'])

    def test_rejecting_all_updates_and_uncertainty_fails_qualification(self):
        provider = {'name': 'claude', 'kind': 'claude', 'model': 'test', 'enabled': True}
        def invoke(provider, packet, timeout, path):
            return {'decisions': [{'id': c['id'], 'verdict': 'accept' if c['id'] == 'useful' else 'reject',
                                   'reason': 'A decision.', 'target': '',
                                   'note': 'A sourced proposed note.' if c['id'] == 'useful' else '',
                                   'evidence': [{'source': 's1', 'quote': c['sources'][0]['text'][:30]}]
                                               if c['id'] == 'useful' else []}
                                  for c in packet['candidates']]}
        result = self.q.qualify(self.store, {'providers': [provider]}, 'claude', invoke)
        self.assertFalse(result['passed'])

    def test_qualification_cannot_be_repeated_until_next_day(self):
        provider = {'name': 'claude', 'kind': 'claude', 'model': 'test', 'enabled': True}
        def unavailable(*args):
            raise self.p.Unavailable('quota')
        self.q.qualify(self.store, {'providers': [provider]}, 'claude', unavailable)
        def never(*args):
            self.fail('Qualification called a model twice on one day')
        self.assertEqual(self.q.qualify(self.store, {'providers': [provider]}, 'claude', never)['state'], 'daily_limit')

    def test_ollama_must_use_an_explicit_private_server(self):
        with self.assertRaises(ValueError):
            self.p.environment({'kind': 'ollama', 'endpoint': 'https://openrouter.ai/api/v1'})

    def test_slow_trickling_ollama_response_obeys_total_timeout(self):
        import http.server
        import threading
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                body = b'{"models":[{"name":"test"}]}'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body = json.dumps({'message': {'content': '{"decisions":[]}'}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                try:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        provider = {'name': 'ollama', 'kind': 'ollama', 'model': 'test',
                    'endpoint': f'http://127.0.0.1:{server.server_port}'}
        started = time.monotonic()
        with self.assertRaises(self.p.Unavailable):
            self.p.invoke(provider, self.q.examples(), 0.4, self.root)
        self.assertLess(time.monotonic() - started, 0.9)


if __name__ == '__main__':
    unittest.main()
