"""Safeguards that make an unattended gate safe to run.

Jev is stochastic, so an unchanged note re-evaluated enough times will eventually draw a
favourable score. These tests pin the four properties that prevent that: one decision per
evaluation input, a revision budget that survives ticket replacement, termination when a
revision changes nothing, and approval bound to the exact scored content.
"""
import sqlite3
import unittest

from reviewer.jev import (DEFAULT_POLICY, Ledger, content_hash, evaluation_key,
                          gate, build_state)


SNAPSHOT = {
    'title': 'A supported lesson',
    'note': '# A supported lesson\n\nThe body that would be committed.',
    'sources': [{'id': 's1', 'path': 'docs/x.md', 'start': 1, 'end': 2,
                 'sha256': 'a' * 64, 'text': 'evidence for the lesson'}],
}


def approving_answers():
    strong = {str(i): 0.0 for i in range(5)}
    strong.update({'3': 0.35, '4': 0.63})
    clean = {str(i): 0.0 for i in range(5)}
    clean['0'] = 1.0
    return {'factual_support': {'probabilities': strong},
            'durability': {'probabilities': strong},
            'reference_value': {'probabilities': strong},
            'sensitive_information': {'probabilities': clean}}


class FakeService:
    """Counts calls so a cache hit is provable rather than assumed."""

    def __init__(self, answers=None):
        self.calls = 0
        self._answers = answers or approving_answers()

    def __call__(self, state, api_key=None, timeout=30):
        self.calls += 1
        return {'model': 'jev-test', 'answers': self._answers, 'usage': {}}


class Key(unittest.TestCase):
    def test_same_state_gives_same_key(self):
        a = build_state(SNAPSHOT, 'why')
        b = build_state(dict(SNAPSHOT), 'why')
        self.assertEqual(evaluation_key(a), evaluation_key(b))

    def test_changed_note_changes_key(self):
        a = build_state(SNAPSHOT, 'why')
        altered = dict(SNAPSHOT, note=SNAPSHOT['note'] + ' extra claim')
        self.assertNotEqual(evaluation_key(a), evaluation_key(build_state(altered, 'why')))

    def test_changed_evidence_changes_key(self):
        a = build_state(SNAPSHOT, 'why')
        altered = dict(SNAPSHOT, sources=[dict(SNAPSHOT['sources'][0], text='different evidence')])
        self.assertNotEqual(evaluation_key(a), evaluation_key(build_state(altered, 'why')))

    def test_key_is_order_independent(self):
        state = build_state(SNAPSHOT, 'why')
        reordered = {'sources': state['sources'], 'submission': state['submission']}
        self.assertEqual(evaluation_key(state), evaluation_key(reordered))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.ledger = Ledger(self.db)

    def test_roundtrip(self):
        self.ledger.remember('k1', 'cand', {'answers': {}})
        self.assertEqual(self.ledger.cached('k1'), {'answers': {}})

    def test_unknown_key_is_none(self):
        self.assertIsNone(self.ledger.cached('nope'))

    def test_revisions_start_at_zero(self):
        self.assertEqual(self.ledger.revisions('cand'), 0)

    def test_revisions_accumulate_per_candidate(self):
        self.ledger.record_revision('cand', 'hash-a')
        self.ledger.record_revision('cand', 'hash-b')
        self.assertEqual(self.ledger.revisions('cand'), 2)
        self.assertEqual(self.ledger.revisions('other'), 0)


class OneDecisionPerInput(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.ledger = Ledger(self.db)
        self.service = FakeService()

    def test_second_evaluation_of_same_input_reuses_the_first(self):
        first = gate(SNAPSHOT, 'why', self.ledger, 'cand', service=self.service)
        second = gate(SNAPSHOT, 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(self.service.calls, 1, 'unchanged input must not be re-drawn')
        self.assertEqual(first.action, second.action)

    def test_changed_note_is_evaluated_again(self):
        gate(SNAPSHOT, 'why', self.ledger, 'cand', service=self.service)
        revised = dict(SNAPSHOT, note=SNAPSHOT['note'] + '\n\nNarrowed further.')
        gate(revised, 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(self.service.calls, 2)


class RevisionBudget(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.ledger = Ledger(self.db)
        weak = {str(i): 0.0 for i in range(5)}
        weak.update({'1': 0.5, '2': 0.4})
        strong = {str(i): 0.0 for i in range(5)}
        strong.update({'3': 0.35, '4': 0.63})
        clean = {str(i): 0.0 for i in range(5)}
        clean['0'] = 1.0
        self.service = FakeService({'factual_support': {'probabilities': weak},
                                    'durability': {'probabilities': strong},
                                    'reference_value': {'probabilities': strong},
                                    'sensitive_information': {'probabilities': clean}})

    def _note(self, n):
        return dict(SNAPSHOT, note=SNAPSHOT['note'] + ('\n\nrevision %d' % n))

    def test_budget_survives_ticket_replacement(self):
        # Each pass is a different ticket for the same candidate.
        first = gate(self._note(1), 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(first.action, 'revise')
        second = gate(self._note(2), 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(second.action, 'revise')
        third = gate(self._note(3), 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(third.action, 'reject', 'a new ticket must not reset the budget')

    def test_other_candidate_keeps_its_own_budget(self):
        gate(self._note(1), 'why', self.ledger, 'cand', service=self.service)
        gate(self._note(2), 'why', self.ledger, 'cand', service=self.service)
        fresh = gate(self._note(1), 'why', self.ledger, 'other', service=self.service)
        self.assertEqual(fresh.action, 'revise')


class NoProgress(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.ledger = Ledger(self.db)
        weak = {str(i): 0.0 for i in range(5)}
        weak.update({'1': 0.5, '2': 0.4})
        strong = {str(i): 0.0 for i in range(5)}
        strong.update({'3': 0.35, '4': 0.63})
        clean = {str(i): 0.0 for i in range(5)}
        clean['0'] = 1.0
        self.service = FakeService({'factual_support': {'probabilities': weak},
                                    'durability': {'probabilities': strong},
                                    'reference_value': {'probabilities': strong},
                                    'sensitive_information': {'probabilities': clean}})

    def test_reviser_returning_identical_content_terminates(self):
        first = gate(SNAPSHOT, 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(first.action, 'revise')
        again = gate(SNAPSHOT, 'why', self.ledger, 'cand', service=self.service)
        self.assertEqual(again.action, 'reject')
        self.assertIn('no_progress', again.reason)


class ContentBinding(unittest.TestCase):
    def test_hash_covers_note_and_evidence(self):
        base = content_hash(SNAPSHOT)
        self.assertEqual(base, content_hash(dict(SNAPSHOT)))
        self.assertNotEqual(base, content_hash(dict(SNAPSHOT, note='different body')))
        moved = dict(SNAPSHOT, sources=[dict(SNAPSHOT['sources'][0], text='tampered evidence')])
        self.assertNotEqual(base, content_hash(moved))

    def test_gate_reports_the_hash_it_scored(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        result = gate(SNAPSHOT, 'why', Ledger(db), 'cand', service=FakeService())
        self.assertEqual(result.content_hash, content_hash(SNAPSHOT))


class SecretsShortCircuit(unittest.TestCase):
    def test_credential_in_evidence_rejects_without_calling_the_service(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        service = FakeService()
        poisoned = dict(SNAPSHOT, sources=[dict(SNAPSHOT['sources'][0],
                        text='AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE')])
        result = gate(poisoned, 'why', Ledger(db), 'cand', service=service)
        self.assertEqual(result.action, 'reject')
        self.assertEqual(service.calls, 0, 'a credential must never leave the machine')


if __name__ == '__main__':
    unittest.main()
