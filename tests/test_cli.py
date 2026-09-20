import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'vault').mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'state': str(self.root / 'state'),
                                          'vault': str(self.root / 'vault'),
                                          'source_roots': [str(self.root)], 'providers': []}))

    def cli(self, *args):
        return subprocess.run([sys.executable, '-m', 'reviewer', '--config', str(self.config), *args],
                              cwd=ROOT, capture_output=True, text=True)

    def test_submit_show_status_and_idle_run_without_vault_mutation(self):
        run = self.cli('run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['state'], 'empty')
        source = self.root / 'architecture.md'
        source.write_text('One daemon owns polling; consumers share its spool.\n')
        candidate = self.root / 'candidate.json'
        candidate.write_text(json.dumps({'task_key': 'task-abc', 'title': 'Shared spool',
                                         'category': 'service', 'claim': 'One daemon owns polling.',
                                         'why_useful': 'Reuse this service for later integrations.',
                                         'sources': [{'path': str(source), 'start': 1, 'end': 1}]}))
        submitted = self.cli('submit', str(candidate))
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        cid = json.loads(submitted.stdout)['id']
        self.assertEqual(json.loads(self.cli('show', cid).stdout)['state'], 'pending')
        self.assertEqual(json.loads(self.cli('status').stdout)['candidates'], {'pending': 1})
        self.assertEqual(json.loads(self.cli('run').stdout)['state'], 'no_provider')
        self.assertEqual(list((self.root / 'vault').iterdir()), [])

    def test_inbox_cannot_be_inside_the_indexed_vault(self):
        config = json.loads(self.config.read_text())
        config['state'] = str(self.root / 'vault' / 'inbox')
        self.config.write_text(json.dumps(config))
        run = self.cli('run')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('outside', run.stderr)

    def test_openrouter_cannot_be_enabled_by_config(self):
        config = json.loads(self.config.read_text())
        config['providers'] = [{'name': 'openrouter', 'kind': 'openrouter', 'enabled': True}]
        self.config.write_text(json.dumps(config))
        run = self.cli('run')
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('Unsupported provider', run.stderr)

    def test_ticket_inspection_requires_no_telegram_credentials(self):
        import test_approvals
        fixture = test_approvals.ApprovalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, note = fixture.candidate()
        fixture.digest()
        config = json.loads(self.config.read_text())
        config['state'] = str(fixture.store.root)
        self.config.write_text(json.dumps(config))
        result = self.cli('ticket', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['snapshot']['note'], note)
        self.assertEqual(json.loads(self.cli('status').stdout)['approvals'], {'ready': 1})

    def test_tick_configuration_fails_without_secrets_or_network(self):
        result = self.cli('telegram-tick')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Configure publication, dashboard and telegram', result.stderr)

    def test_dashboard_config_must_be_loopback_private_and_outside_the_vault(self):
        good = {'host': '127.0.0.1', 'port': 8902,
                'public_origin': 'https://your-host.your-tailnet.ts.net',
                'base_path': '/knowledge-review', 'allowed_login': 'you@github',
                'secret': str(self.root / 'dashboard.secret')}
        bad = [({'host': '0.0.0.0'}, 'loopback'), ({'port': 80}, 'loopback'),
               ({'public_origin': 'http://your-host.your-tailnet.ts.net'}, 'https origin'),
               ({'public_origin': 'https://your-host.your-tailnet.ts.net/'}, 'https origin'),
               ({'allowed_login': ''}, 'Tailscale login'),
               ({'secret': 'dashboard.secret'}, 'absolute path'),
               ({'secret': str(self.root / 'vault' / 'secret')}, 'outside the indexed vault')]
        for override, expected in bad:
            config = json.loads(self.config.read_text())
            config['dashboard'] = {**good, **override}
            self.config.write_text(json.dumps(config))
            result = self.cli('status')
            self.assertNotEqual(result.returncode, 0, override)
            self.assertIn(expected, result.stderr, override)
        config = json.loads(self.config.read_text())
        config['dashboard'] = good
        self.config.write_text(json.dumps(config))
        self.assertEqual(self.cli('status').returncode, 0)

    def test_dashboard_command_requires_the_full_approval_configuration(self):
        result = self.cli('dashboard')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Configure publication, dashboard and telegram', result.stderr)

    def test_publication_state_cannot_be_indexed(self):
        config = json.loads(self.config.read_text())
        config['publication'] = {'state': str(self.root / 'vault' / 'state'), 'khoj_source': '/tmp/src'}
        self.config.write_text(json.dumps(config))
        result = self.cli('status')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Publication state must be outside', result.stderr)

    def test_empty_tick_uses_existing_telegram_credential_names_without_network(self):
        config = json.loads(self.config.read_text())
        config['publication'] = {'state': str(self.root / 'publication-state'),
                                 'khoj_source': os.environ.get('KHOJ_SRC', '/home/you/development/khoj-lab/src')}
        spool = self.root / 'inbox.jsonl'
        spool.write_text('')
        config['telegram'] = {'spool': str(spool),
                              'bridge_scripts': '/home/you/development/telegram-agent-bridge/scripts'}
        config['dashboard'] = {'host': '127.0.0.1', 'port': 8902,
                               'public_origin': 'https://your-host.your-tailnet.ts.net',
                               'base_path': '/knowledge-review',
                               'allowed_login': 'you@github',
                               'secret': str(self.root / 'dashboard.secret')}
        (self.root / 'vault/.knowledge-vault').write_text('personal-knowledge-v1\n')
        self.config.write_text(json.dumps(config))
        env = dict(os.environ, TELEGRAM_BOT_TOKEN='456:fake_test_token', TELEGRAM_CHAT_ID='123', TOKEN='', CHAT_ID='0')
        result = subprocess.run([sys.executable, '-m', 'reviewer', '--config', str(self.config), 'telegram-tick'],
                                cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'state': 'checked', 'digest': {'state': 'empty'}})


if __name__ == '__main__':
    unittest.main()
