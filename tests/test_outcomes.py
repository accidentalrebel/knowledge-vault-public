"""The decision log: does the page get an honest account of what happened and why?

`approval_actions` says a decision happened; only this table says why. The tests that
matter are the ones about what survives - a reason recorded once is never rewritten, a
decision made before the table existed is still explained, and a hand decision with no
reason at all still appears rather than vanishing from the record.
"""
import json
import time
import unittest

import test_approvals as approval_fixtures
from reviewer import outcomes


class Fixture(unittest.TestCase):
    def setUp(self):
        self.fixture = approval_fixtures.ApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.desk = self.fixture.desk
        self.drafts = 0

    def ready(self):
        self.drafts += 1
        cid, note = self.fixture.candidate(self.drafts)
        self.fixture.digest()
        return cid, max(t['id'] for t in self.desk.pending_tickets())

    def act(self, number, action, key, channel='jev', instructions='Narrow it.'):
        return self.desk.act(number, action, instructions if action == 'revise' else '',
                             key, {'channel': channel, 'login': channel + '-user'})


class ShortReasonTests(unittest.TestCase):
    def test_an_approval_needs_no_reason(self):
        self.assertEqual(outcomes.short_reason('approve', 'All approval thresholds met: P(4)=0.7'), '')

    def test_each_refusal_reads_as_a_sentence_without_arithmetic(self):
        cases = {
            'Below approval thresholds: factual P(3-4)=0.720 (need 0.85)':
                'evidence too thin for the claim',
            'Sensitivity gate: P(levels 2-4)=0.510': 'held back as sensitive',
            'Deterministic secret scan matched: high_entropy_token':
                'a credential pattern matched',
            'Revision budget exhausted: factual P(4)=0.170': 'out of revision attempts',
        }
        for reason, expected in cases.items():
            self.assertEqual(outcomes.short_reason('reject', reason), expected)

    def test_an_already_short_reason_survives_a_second_pass(self):
        """Rows recovered from an old notice hold the short form already; shortening it
        again must not mangle them, or history would read differently from today."""
        for phrase in ('evidence too thin for the claim', 'held back as sensitive',
                       'a credential pattern matched', 'out of revision attempts'):
            self.assertEqual(outcomes.short_reason('reject', phrase), phrase)

    def test_an_unrecognized_reason_is_trimmed_rather_than_dropped(self):
        long = 'Something new went wrong: ' + 'x' * 200
        self.assertEqual(outcomes.short_reason('reject', long), 'something new went wrong')

    def test_a_blank_reason_is_blank(self):
        self.assertEqual(outcomes.short_reason('reject', None), '')


class RecordTests(Fixture):
    def test_a_recorded_reason_is_never_rewritten(self):
        cid, number = self.ready()
        outcomes.record(self.store, 'k1', number, cid, 'reject', 'the first reason')
        outcomes.record(self.store, 'k1', number, cid, 'approve', 'a different reason')
        rows = outcomes.decisions(self.store)
        with self.store.db() as db:
            stored = db.execute('SELECT reason FROM jev_outcomes WHERE key=?', ('k1',)).fetchone()
        self.assertEqual(stored['reason'], 'the first reason')
        self.assertEqual(rows, rows)

    def test_reading_works_on_a_database_that_never_saw_this_table(self):
        with self.store.db() as db:
            db.execute('DROP TABLE IF EXISTS jev_outcomes')
        self.assertEqual(outcomes.decisions(self.store), [])
        self.assertEqual(outcomes.tally(self.store)['published'], 0)


class DecisionsTests(Fixture):
    def test_a_decision_carries_its_verdict_reason_actor_and_text(self):
        cid, number = self.ready()
        self.act(number, 'reject', 'jev:one')
        outcomes.record(self.store, 'jev:one', number, cid, 'reject',
                        'Sensitivity gate: P(levels 2-4)=0.510')
        item = outcomes.decisions(self.store)[0]
        self.assertEqual((item['verdict'], item['tone'], item['by']), ('Dropped', 'drop', 'jev'))
        self.assertEqual(item['why'], 'held back as sensitive')
        self.assertIn('P(levels 2-4)', item['reason'])
        self.assertEqual(item['detail'], item['reason'], 'a full verdict is offered in detail')
        self.assertTrue(item['note'])

    def test_an_approval_reads_as_published_at_its_saved_path(self):
        _, number = self.ready()
        self.act(number, 'approve', 'dashboard:one', channel='dashboard')
        item = outcomes.decisions(self.store)[0]
        self.assertEqual((item['verdict'], item['tone']), ('Published', 'good'))
        self.assertTrue(item['path'].startswith('knowledge/'))

    def test_a_hand_decision_without_a_reason_still_appears(self):
        _, number = self.ready()
        self.act(number, 'reject', 'dashboard:two', channel='dashboard')
        item = outcomes.decisions(self.store)[0]
        self.assertEqual((item['by'], item['verdict'], item['reason'], item['why']),
                         ('dashboard', 'Dropped', '', ''))

    def test_the_outcome_names_what_happened_not_what_was_asked(self):
        """An approval that hit a changed destination published nothing. Calling it
        'Published' because the button said approve is the one lie this page must not tell."""
        (self.fixture.vault / 'Existing.md').write_text('Before\n')
        self.fixture.candidate(verdict='update', target='Existing.md')
        self.fixture.digest()
        number = max(t['id'] for t in self.desk.pending_tickets())
        (self.fixture.vault / 'Existing.md').write_text('Another writer changed this.\n')
        self.act(number, 'approve', 'dashboard:three', channel='dashboard')
        item = outcomes.decisions(self.store)[0]
        self.assertEqual(item['action'], 'approve')
        self.assertEqual((item['state'], item['verdict']), ('stale', 'Needs a look'))

    def test_newest_first(self):
        _, first = self.ready()
        self.act(first, 'reject', 'a')
        _, second = self.ready()
        self.act(second, 'reject', 'b')
        self.assertEqual([i['ticket'] for i in outcomes.decisions(self.store)], [second, first])


class BackfillTests(Fixture):
    def test_a_pre_table_decision_is_recovered_from_the_notice_it_sent(self):
        cid, number = self.ready()
        self.act(number, 'reject', 'jev:old')
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO approval_outbox VALUES (?,?,?,NULL)',
                       ('jev-outcome:jev:old',
                        'DROPPED  A title\nevidence too thin for the claim\n\nno action needed', 'sent'))
        self.assertEqual(outcomes.backfill(self.store), 1)
        item = outcomes.decisions(self.store)[0]
        self.assertEqual(item['why'], 'evidence too thin for the claim')
        self.assertEqual(item['detail'], '', 'the arithmetic was never kept, so promise none')

    def test_backfill_is_idempotent_and_leaves_fresh_rows_alone(self):
        cid, number = self.ready()
        self.act(number, 'reject', 'jev:old')
        outcomes.record(self.store, 'jev:old', number, cid, 'reject', 'the recorded reason')
        self.assertEqual(outcomes.backfill(self.store), 0)
        self.assertEqual(outcomes.decisions(self.store)[0]['reason'], 'the recorded reason')

    def test_a_decision_with_no_surviving_notice_still_gets_a_row(self):
        _, number = self.ready()
        self.act(number, 'reject', 'jev:silent')
        self.assertEqual(outcomes.backfill(self.store), 1)
        self.assertEqual(outcomes.decisions(self.store)[0]['reason'], '')

    def test_hand_decisions_are_not_invented_reasons(self):
        _, number = self.ready()
        self.act(number, 'reject', 'dashboard:x', channel='dashboard')
        self.assertEqual(outcomes.backfill(self.store), 0)


class TallyTests(Fixture):
    def test_the_headline_numbers_match_the_tickets_and_the_queue(self):
        _, number = self.ready()
        self.act(number, 'approve', 'dashboard:one', channel='dashboard')
        self.ready()
        counts = outcomes.tally(self.store)
        self.assertEqual(counts['published'], 1)
        self.assertEqual(counts['waiting'], 1)
        self.assertEqual(counts['decided_this_week'], 1)
        self.assertEqual(counts['week'], {'published': 1})

    def test_decisions_outside_the_window_are_not_called_this_week(self):
        _, number = self.ready()
        self.act(number, 'approve', 'dashboard:one', channel='dashboard')
        with self.store.db() as db:
            db.execute('UPDATE approval_actions SET created=?', (time.time() - 30 * 86400,))
        counts = outcomes.tally(self.store)
        self.assertEqual(counts['decided_this_week'], 0)
        self.assertEqual(counts['published'], 1, 'the vault still holds it')

    def test_an_empty_queue_reports_no_last_decision(self):
        self.assertIsNone(outcomes.tally(self.store)['last_decision'])


class DigestKeyTests(unittest.TestCase):
    def test_the_same_set_of_decisions_gives_the_same_key_in_any_order(self):
        self.assertEqual(outcomes.digest_key(['a', 'b']), outcomes.digest_key(['b', 'a']))

    def test_a_different_set_gives_a_different_key(self):
        self.assertNotEqual(outcomes.digest_key(['a', 'b']), outcomes.digest_key(['a', 'c']))


if __name__ == '__main__':
    unittest.main()
