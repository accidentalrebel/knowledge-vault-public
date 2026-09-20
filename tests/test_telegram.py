import importlib
import io
import json
import unittest
import urllib.error

import test_approvals as approval_fixtures


class TelegramTests(unittest.TestCase):
    def setUp(self):
        try:
            self.t = importlib.import_module('reviewer.telegram')
            self.runner = importlib.import_module('reviewer.telegram_runner')
            self.a = importlib.import_module('reviewer.approvals')
        except ModuleNotFoundError:
            self.fail('Telegram transport and the notification runner are not implemented')
        self.fixture = approval_fixtures.ApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.spool = self.fixture.root / 'inbox.jsonl'
        self.spool.write_text('')
        self.fixture.config['telegram'] = {
            'spool': str(self.spool),
            'bridge_scripts': '/home/you/development/telegram-agent-bridge/scripts'}

    def tick(self):
        return self.runner.tick(self.fixture.desk, self.fixture.config)

    def append(self, text):
        message = {'message_id': 9001, 'date': 1, 'text': text,
                   'from': {'id': 123, 'is_bot': False}, 'chat': {'id': 123, 'type': 'private'}}
        with self.spool.open('a') as stream:
            stream.write(json.dumps({'update_id': 1, 'message': message}) + '\n')

    # --- notification-only tick ---------------------------------------------------

    def test_dashboard_url_is_built_from_the_configured_origin_and_path(self):
        self.assertEqual(self.a.dashboard_url(self.fixture.config), approval_fixtures.DASHBOARD)

    def test_idle_tick_sends_nothing(self):
        self.assertEqual(self.tick(), {'state': 'checked', 'digest': {'state': 'empty'}})
        self.assertEqual(self.fixture.transport.messages, [])

    def test_tick_announces_one_new_ticket_with_only_a_link(self):
        f = self.fixture
        _, note = f.candidate(1)
        f.candidate(2)
        result = self.tick()
        self.assertEqual(result['digest']['tickets'], [1])
        self.assertEqual(len(f.transport.messages), 1)
        text = f.transport.messages[0]['text']
        self.assertIn(approval_fixtures.DASHBOARD, text)
        self.assertNotIn(note.strip(), text)
        self.tick()
        self.assertEqual([t['id'] for t in f.desk.pending_tickets()], [1, 2])
        self.assertEqual(len(f.transport.messages), 2)

    def test_a_telegram_approval_message_no_longer_publishes_anything(self):
        f = self.fixture
        f.candidate()
        self.tick()
        for text in ('approve 1', 'reject 1', 'revise 1: shorter', 'show 1'):
            self.append(text)
        self.tick()
        self.assertEqual(list(f.vault.rglob('*.md')), [])
        self.assertEqual(f.desk.ticket(1)['state'], 'ready')
        with f.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM approval_commands').fetchone()[0], 0)
        self.assertIsNone(f.store.get_meta('telegram_cursor'))

    def test_tick_delivers_publication_and_revision_outcomes(self):
        f = self.fixture
        cid, _ = f.candidate()
        self.tick()
        f.act(1, 'revise', 'Make it shorter.')
        f.store.mark([cid], 'uncertain', {'reason': 'The evidence does not establish that.'})
        self.tick()
        self.assertIn('uncertain', f.transport.messages[-1]['text'])

    def test_tick_does_not_overlap_the_reviewer(self):
        f = self.fixture
        worker = importlib.import_module('reviewer.worker')
        f.candidate()
        with worker.worker_lock(f.store) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.tick(), {'state': 'busy'})
        self.assertEqual(f.transport.messages, [])

    def test_tick_recovers_an_interrupted_publication_without_repeating_it(self):
        f = self.fixture
        cid, note = f.candidate()
        self.tick()
        original = f.publisher.publish
        def interrupted(snapshot):
            original(snapshot)
            raise KeyboardInterrupt()
        f.publisher.publish = interrupted
        with self.assertRaises(KeyboardInterrupt):
            f.act(1, 'approve')
        f.publisher.publish = original
        self.tick()
        self.assertEqual(f.publisher.write_count, 1)
        self.assertEqual(f.desk.ticket(1)['state'], 'published')

    # --- transport ----------------------------------------------------------------

    def test_http_send_is_plain_exact_text_and_records_confirmed_message_id(self):
        calls = []
        def open_request(request, timeout):
            calls.append((request, timeout))
            return io.BytesIO(json.dumps({'ok': True, 'result': {
                'message_id': 7, 'date': 1234, 'chat': {'id': 123}}}).encode())
        transport = self.t.TelegramTransport('456:fixture-secret', '123', open_request)
        result = transport.send('Exact <text> & markdown **here**\n', silent=True)
        self.assertEqual(result['message_id'], 7)
        request, timeout = calls[0]
        self.assertTrue(request.full_url.endswith('/sendMessage'))
        payload = json.loads(request.data)
        self.assertEqual(payload['text'], 'Exact <text> & markdown **here**\n')
        self.assertNotIn('parse_mode', payload)
        self.assertTrue(payload['disable_notification'])
        self.assertTrue(payload['link_preview_options']['is_disabled'])
        self.assertLessEqual(timeout, 15)

    def test_document_upload_contains_exact_bytes(self):
        calls = []
        def open_request(request, timeout):
            calls.append(request)
            return io.BytesIO(b'{"ok":true,"result":{"message_id":8,"date":1234,"chat":{"id":123}}}')
        transport = self.t.TelegramTransport('456:fixture-secret', '123', open_request)
        content = '# Before\nαβ\n# After\nExact **new** text\n'.encode()
        transport.document('draft-1.md', content, 'Read this before approving.')
        self.assertTrue(calls[0].full_url.endswith('/sendDocument'))
        self.assertIn(content, calls[0].data)

    def test_network_error_does_not_expose_token_and_is_delivery_unknown(self):
        def fail(request, timeout):
            raise urllib.error.URLError(request.full_url)
        transport = self.t.TelegramTransport('456:fixture-secret', '123', fail)
        with self.assertRaises(OSError) as caught:
            transport.send('test')
        self.assertNotIn('fixture-secret', str(caught.exception))

    def test_malformed_or_wrong_recipient_ack_is_not_success(self):
        for data in [b'[]', b'not json', b'{"ok":true}',
                     b'{"ok":true,"result":{"message_id":8,"date":1234,"chat":{"id":999}}}']:
            transport = self.t.TelegramTransport('456:fixture-secret', '123',
                                                  lambda request, timeout, data=data: io.BytesIO(data))
            with self.assertRaises(OSError):
                transport.send('test')


if __name__ == '__main__':
    unittest.main()
