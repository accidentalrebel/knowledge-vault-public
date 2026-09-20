"""Returning a finished candidate to the queue.

Reopening by hand left three candidates permanently stuck: they re-reviewed, reached
'accepted', and then waited forever because the digest will not raise a new ticket while
an earlier one sits in a terminal state.
"""
import json
import tempfile
import unittest
from pathlib import Path

from reviewer import store as store_module


class ReopenTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sources = self.root / 'src'
        self.sources.mkdir()
        self.file = self.sources / 'note.md'
        self.file.write_text('alpha\nbravo\ncharlie\n')
        self.store = store_module.Store(self.root / 'state')

    def submit(self, key='p/s/t'):
        return self.store.submit({
            'task_key': key, 'title': 'A title', 'category': 'lesson',
            'claim': 'A claim that is long enough to be accepted by the intake contract.',
            'why_useful': 'Because a future task would otherwise repeat the investigation.',
            'sources': [{'path': str(self.file), 'start': 1, 'end': 2}],
        }, [str(self.sources)])['id']

    def ticket(self, cid, state):
        with self.store.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS approval_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT NOT NULL,
                snapshot TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                message_ids TEXT NOT NULL DEFAULT '[]', delivered_at INTEGER,
                approval_key TEXT, result TEXT)""")
            db.execute('INSERT INTO approval_tickets (candidate,snapshot,state,created) VALUES (?,?,?,?)',
                       (cid, json.dumps({'title': 'A title'}), state, 0))

    def test_reopen_works_before_any_desk_has_created_tickets(self):
        cid = self.submit()
        self.store.mark([cid], 'declined')
        self.assertEqual(self.store.reopen(cid)['superseded'], 0)
        self.assertEqual(self.store.get(cid)['state'], 'pending')

    def test_reopen_supersedes_a_rejected_ticket(self):
        cid = self.submit()
        self.store.mark([cid], 'declined')
        self.ticket(cid, 'rejected')
        result = self.store.reopen(cid)
        self.assertTrue(result['reopened'])
        self.assertEqual(result['superseded'], 1)
        self.assertEqual(self.store.get(cid)['state'], 'pending')
        with self.store.db() as db:
            state = db.execute('SELECT state FROM approval_tickets WHERE candidate=?', (cid,)).fetchone()['state']
        self.assertEqual(state, 'superseded')

    def test_reopened_candidate_is_visible_to_the_digest_query(self):
        cid = self.submit()
        self.store.mark([cid], 'declined')
        self.ticket(cid, 'rejected')
        self.store.reopen(cid)
        self.store.mark([cid], 'accepted')
        with self.store.db() as db:
            rows = db.execute("""SELECT id FROM candidates c WHERE state IN ('accepted','updated')
                AND NOT EXISTS (SELECT 1 FROM approval_tickets t WHERE t.candidate=c.id
                  AND t.state NOT IN ('revised','stale','superseded'))""").fetchall()
        self.assertEqual([r['id'] for r in rows], [cid])

    def test_revised_tickets_are_left_alone(self):
        cid = self.submit()
        self.store.mark([cid], 'held_invalid')
        self.ticket(cid, 'revised')
        self.assertEqual(self.store.reopen(cid)['superseded'], 0)

    def test_reopening_a_pending_candidate_changes_nothing(self):
        cid = self.submit()
        result = self.store.reopen(cid)
        self.assertFalse(result['reopened'])
        self.assertEqual(result['superseded'], 0)

    def test_unknown_candidate_is_an_error(self):
        with self.assertRaises(ValueError):
            self.store.reopen('does-not-exist')

    def test_reopen_clears_the_previous_decision(self):
        cid = self.submit()
        self.store.mark([cid], 'declined', {'reason': 'sensitivity'})
        self.store.reopen(cid)
        self.assertIsNone(self.store.get(cid).get('decision'))


if __name__ == '__main__':
    unittest.main()
