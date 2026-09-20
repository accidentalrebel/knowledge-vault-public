"""The autonomous channel: does Jev's decision actually reach the approval desk, and
does an outage leave the ticket alone rather than publishing it?"""
import sqlite3
import unittest

from reviewer import jev, jev_channel


SNAPSHOT = {'title': 'A supported lesson', 'note': '# A supported lesson\n\nBody.',
            'sources': [{'id': 's1', 'path': 'docs/x.md', 'start': 1, 'end': 2,
                         'sha256': 'a' * 64, 'text': 'evidence for the lesson'}]}


def answers(kind='approve'):
    strong = {str(i): 0.0 for i in range(5)}
    strong.update({'3': 0.35, '4': 0.63})
    weak = {str(i): 0.0 for i in range(5)}
    weak.update({'1': 0.5, '2': 0.4})
    clean = {str(i): 0.0 for i in range(5)}
    clean['0'] = 1.0
    factual = strong if kind == 'approve' else weak
    return {'factual_support': {'probabilities': factual},
            'durability': {'probabilities': strong},
            'reference_value': {'probabilities': strong},
            'sensitive_information': {'probabilities': clean}}


class FakeStore:
    def __init__(self, db):
        self._db = db

    def db(self):
        from contextlib import nullcontext
        return nullcontext(self._db)

    def get(self, cid):
        return {'id': cid, 'candidate': {'why_useful': 'because it is reusable'}, 'sources': []}


class FakeDesk:
    def __init__(self, store, tickets):
        self.store = store
        self._tickets = tickets
        self.acted = []
        self.notices = []

    def pending_tickets(self):
        return list(self._tickets)

    def action_generation(self, number):
        return 0

    def act(self, number, action, instructions, key, actor):
        self.acted.append({'number': number, 'action': action, 'key': key, 'actor': actor,
                           'instructions': instructions})
        self._tickets = [t for t in self._tickets if t['id'] != number]

    def _notify(self, key, text):
        self.notices.append({'key': key, 'text': text})


def service(kind='approve'):
    def call(state, api_key=None, timeout=30):
        return {'model': 'jev-test', 'answers': answers(kind), 'usage': {}}
    return call


def build(tickets=None, enabled=True):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    store = FakeStore(db)
    tickets = tickets if tickets is not None else [
        {'id': 7, 'candidate': 'cand-1', 'snapshot': SNAPSHOT}]
    desk = FakeDesk(store, tickets)
    config = {'jev': {'enabled': enabled}}
    return desk, config, jev.Ledger(db)


class Disabled(unittest.TestCase):
    def test_disabled_gate_does_nothing(self):
        desk, config, ledger = build(enabled=False)
        result = jev_channel.tick(desk, config, service=service(), ledger=ledger)
        self.assertEqual(result['state'], 'disabled')
        self.assertEqual(desk.acted, [])


class Decides(unittest.TestCase):
    def test_approval_reaches_the_desk_as_the_jev_actor(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        self.assertEqual(len(desk.acted), 1)
        acted = desk.acted[0]
        self.assertEqual(acted['action'], 'approve')
        self.assertEqual(acted['actor'], {'channel': 'jev', 'login': 'jev-auto'})
        self.assertTrue(acted['key'].startswith('jev:'))

    def test_revision_carries_instructions(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('revise'), ledger=ledger)
        self.assertEqual(desk.acted[0]['action'], 'revise')
        self.assertTrue(desk.acted[0]['instructions'].strip())

    def test_outcome_is_announced_as_information_not_a_request(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        text = desk.notices[0]['text']
        # A report, never a request: 'decided' is fine, 'decide' as an instruction is not.
        for ask in ('approve', 'reject', 'please', 'decide ', 'review this', 'waiting on you'):
            self.assertNotIn(ask, text.lower())

    def test_one_message_covers_the_whole_run(self):
        tickets = [{'id': n, 'candidate': 'cand-%d' % n, 'snapshot': SNAPSHOT} for n in (7, 8, 9)]
        desk, config, ledger = build(tickets)
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        self.assertEqual(len(desk.acted), 3)
        self.assertEqual(len(desk.notices), 1, 'three decisions, one notification')
        self.assertIn('3 decided', desk.notices[0]['text'])
        self.assertIn('3 published', desk.notices[0]['text'])

    def test_the_digest_carries_counts_not_content(self):
        """The link is appended by the desk when the message is sent, so what this owns
        is keeping draft text out of a notification."""
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        text = desk.notices[0]['text']
        self.assertNotIn(SNAPSHOT['title'], text)
        self.assertNotIn(SNAPSHOT['note'], text)
        self.assertLessEqual(len(text), 200)

    def test_a_missing_dashboard_still_announces(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        self.assertIn('1 published', desk.notices[0]['text'])

    def test_the_same_run_of_decisions_is_never_announced_twice(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        first = desk.notices[0]['key']
        desk._tickets = [{'id': 7, 'candidate': 'cand-1', 'snapshot': SNAPSHOT}]
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        self.assertEqual(desk.notices[1]['key'], first, 'the key is the set of decisions')

    def test_nothing_decided_sends_nothing(self):
        desk, config, ledger = build(tickets=[])
        jev_channel.tick(desk, config, service=service('approve'), ledger=ledger)
        self.assertEqual(desk.notices, [])

    def test_notice_does_not_carry_raw_threshold_arithmetic(self):
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('revise'), ledger=ledger)
        text = desk.notices[0]['text']
        for noise in ('P(3-4)', 'P(4)', 'need 0.', 'threshold'):
            self.assertNotIn(noise, text)

    def test_the_reason_is_kept_where_the_dashboard_can_read_it(self):
        from reviewer import outcomes
        desk, config, ledger = build()
        jev_channel.tick(desk, config, service=service('revise'), ledger=ledger)
        with desk.store.db() as db:
            row = db.execute('SELECT * FROM jev_outcomes').fetchone()
        self.assertEqual((row['ticket'], row['action']), (7, 'revise'))
        self.assertIn('P(', row['reason'], 'the full verdict survives, not just the summary')
        self.assertEqual(outcomes.short_reason('revise', row['reason']),
                         'evidence too thin for the claim')


class OutageIsFailClosed(unittest.TestCase):
    def test_service_failure_defers_instead_of_publishing(self):
        desk, config, ledger = build()

        def broken(state, api_key=None, timeout=30):
            raise jev.JevUnavailable('HTTP 503 from TypeSafe')

        result = jev_channel.tick(desk, config, service=broken, ledger=ledger)
        self.assertEqual(desk.acted, [], 'an outage must never publish')
        self.assertEqual(len(result['deferred']), 1)
        self.assertEqual(desk.pending_tickets()[0]['id'], 7, 'ticket stays ready for a retry')


class ActionKey(unittest.TestCase):
    def _result(self, action='approve', content='hash-a'):
        return jev.Result(action, 'reason', '', content, None)

    def test_same_decision_and_content_gives_the_same_key(self):
        self.assertEqual(jev_channel.action_key(1, self._result()),
                         jev_channel.action_key(1, self._result()))

    def test_different_action_gives_a_different_key(self):
        self.assertNotEqual(jev_channel.action_key(1, self._result('approve')),
                            jev_channel.action_key(1, self._result('reject')))

    def test_different_content_gives_a_different_key(self):
        self.assertNotEqual(jev_channel.action_key(1, self._result(content='hash-a')),
                            jev_channel.action_key(1, self._result(content='hash-b')))

    def test_recovery_generation_changes_the_key(self):
        self.assertNotEqual(jev_channel.action_key(1, self._result(), 0),
                            jev_channel.action_key(1, self._result(), 1))


if __name__ == '__main__':
    unittest.main()


class BlockedReviewIsAnnounced(unittest.TestCase):
    """The failure that looks like nothing happening. It ran for nine hours unnoticed
    because a paused reviewer publishes nothing, and silence is also what success at an
    empty queue looks like."""

    def build_desk(self):
        desk, config, _ = build(tickets=[])
        desk.store._db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
        return desk

    def set_block(self, desk, detail, since=1.0):
        import json as js
        desk.store._db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                               ('review_blocked', js.dumps({'detail': detail, 'since': since})))

    def test_a_pause_is_announced_with_its_cause(self):
        from reviewer import telegram_runner
        desk = self.build_desk()
        desk.store.get_meta = lambda key: {'detail': "No module named 'httpx'", 'since': 1.0}
        detail = telegram_runner.announce_block(desk)
        self.assertEqual(detail, "No module named 'httpx'")
        self.assertIn("No module named 'httpx'", desk.notices[0]['text'])
        self.assertIn('paused', desk.notices[0]['text'])

    def test_the_same_cause_is_not_announced_every_minute(self):
        from reviewer import telegram_runner
        desk = self.build_desk()
        desk.store.get_meta = lambda key: {'detail': 'the same cause', 'since': 1.0}
        telegram_runner.announce_block(desk)
        telegram_runner.announce_block(desk)
        self.assertEqual(desk.notices[0]['key'], desk.notices[1]['key'],
                         'one key per cause, so the outbox sends it once')

    def test_a_different_cause_gets_its_own_announcement(self):
        from reviewer import telegram_runner
        desk = self.build_desk()
        desk.store.get_meta = lambda key: {'detail': 'first cause', 'since': 1.0}
        telegram_runner.announce_block(desk)
        desk.store.get_meta = lambda key: {'detail': 'second cause', 'since': 1.0}
        telegram_runner.announce_block(desk)
        self.assertNotEqual(desk.notices[0]['key'], desk.notices[1]['key'])

    def test_nothing_is_said_when_review_is_running(self):
        from reviewer import telegram_runner
        desk = self.build_desk()
        desk.store.get_meta = lambda key: None
        self.assertIsNone(telegram_runner.announce_block(desk))
        self.assertEqual(desk.notices, [])
