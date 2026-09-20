import importlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from reviewer.store import Store


# These tests construct a real Publisher, so they need the khoj-lab checkout that
# provides khoj_mcp. Point KHOJ_SRC at yours; without it they error at import.
KHOJ_SRC = os.environ.get('KHOJ_SRC', '/home/you/development/khoj-lab/src')
DASHBOARD = 'https://your-host.your-tailnet.ts.net/knowledge-review/'
ACTOR = {'channel': 'dashboard', 'login': 'you@github'}


class TelegramDouble:
    chat_id = 123
    bot_id = 456

    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, text, silent=False):
        if self.fail:
            raise OSError('Connection closed before acknowledgement')
        self.messages.append({'text': text, 'silent': silent})
        return {'message_id': 100 + len(self.messages), 'date': int(time.time())}

    def document(self, name, data, caption, silent=False):
        result = self.send(caption, silent)
        self.messages[-1].update(name=name, data=data)
        return result


class CountingPublisher:
    """Wraps the real publisher so tests can prove a snapshot is written once."""

    def __init__(self, publisher):
        self._publisher = publisher
        self.error = publisher.error
        self.write_count = 0

    def read(self, target):
        return self._publisher.read(target)

    def publish(self, snapshot):
        self.write_count += 1
        return self._publisher.publish(snapshot)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        try:
            self.a = importlib.import_module('reviewer.approvals')
            self.p = importlib.import_module('reviewer.publication')
        except ModuleNotFoundError:
            self.fail('Approval tickets and publication are not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        (self.vault / '.knowledge-vault').write_text('personal-knowledge-v1\n')
        self.source = self.root / 'source.md'
        self.source.write_text('One daemon owns polling; consumers read its spool.\n')
        self.store = Store(self.root / 'state')
        self.config = {'vault': str(self.vault), 'source_roots': [str(self.root)],
                       'publication': {'khoj_source': KHOJ_SRC,
                                       'state': str(self.root / 'vault-state')},
                       'dashboard': {'host': '127.0.0.1', 'port': 8902,
                                     'public_origin': 'https://your-host.your-tailnet.ts.net',
                                     'base_path': '/knowledge-review',
                                     'allowed_login': ACTOR['login']}}
        self.transport = TelegramDouble()
        self.publisher = CountingPublisher(self.p.Publisher(self.config))
        self.desk = self.a.ApprovalDesk(self.store, self.config, self.transport, self.publisher)
        self.keys = 0

    def key(self):
        self.keys += 1
        return f'dashboard:nonce-{self.keys}'

    def candidate(self, number=1, verdict='accept', target='', note=None):
        note = note or '# Shared spool\nOne daemon owns polling; consumers read its spool.\n'
        candidate = {'task_key': f'task-{number}', 'title': f'Shared spool {number}', 'category': 'lesson',
                     'claim': f'One daemon owns polling, case {number}.',
                     'why_useful': 'Reuse its spool for new consumers.',
                     'sources': [{'path': str(self.source), 'start': 1, 'end': 1}]}
        cid = self.store.submit(candidate, self.config['source_roots'])['id']
        decision = {'id': cid, 'verdict': verdict, 'reason': 'A verified reusable polling constraint.',
                    'target': target, 'note': note,
                    'evidence': [{'source': 's1', 'quote': 'One daemon owns polling; consumers read its spool.'}]}
        self.store.mark([cid], 'accepted' if verdict == 'accept' else 'updated', decision)
        return cid, note

    def digest(self):
        return self.desk.digest(DASHBOARD)

    def act(self, number, action, instructions='', action_key=None, actor=None):
        return self.desk.act(number, action, instructions, action_key or self.key(), actor or ACTOR)

    # --- intake and notification -------------------------------------------------

    def test_empty_digest_makes_no_network_call(self):
        self.assertEqual(self.digest()['state'], 'empty')
        self.assertEqual(self.transport.messages, [])

    def test_notification_is_a_short_link_without_the_draft_text(self):
        _, note = self.candidate()
        self.digest()
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 1)
        text = self.transport.messages[0]['text']
        self.assertIn(DASHBOARD, text)
        self.assertNotIn(note.strip(), text)
        self.assertNotIn('approve 1', text)
        self.assertLessEqual(len(text), 400)
        self.assertNotIn('data', self.transport.messages[0])

    def test_one_ticket_per_digest_and_no_reminders(self):
        self.candidate(1)
        self.candidate(2)
        self.assertEqual(self.digest()['tickets'], [1])
        self.assertEqual(self.digest()['tickets'], [2])
        self.assertEqual(self.digest()['state'], 'empty')
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 2)
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 2)

    def test_a_failed_notification_does_not_block_dashboard_approval(self):
        cid, note = self.candidate()
        self.transport.fail = True
        self.digest()
        self.desk.flush_responses()
        self.assertEqual(self.transport.messages, [])
        self.assertEqual(self.desk.ticket(1)['state'], 'ready')
        self.transport.fail = False
        self.assertEqual(self.act(1, 'approve')['state'], 'published')
        self.assertEqual((self.vault / self.desk.ticket(1)['snapshot']['target']).read_text(), note)

    def test_pending_tickets_are_listed_oldest_first_with_exact_content(self):
        self.assertEqual(self.desk.pending_tickets(), [])
        _, first = self.candidate(1)
        self.digest()
        self.candidate(2, note='# Second\nAnother reusable constraint.\n')
        self.digest()
        pending = self.desk.pending_tickets()
        self.assertEqual([t['id'] for t in pending], [1, 2])
        self.assertEqual(pending[0]['snapshot']['note'], first)
        self.assertEqual(pending[0]['snapshot']['sources'][0]['path'], str(self.source))
        self.assertTrue(all(t['fingerprint'] for t in pending))
        self.assertNotEqual(pending[0]['fingerprint'], pending[1]['fingerprint'])
        self.act(1, 'reject')
        self.assertEqual([t['id'] for t in self.desk.pending_tickets()], [2])

    def test_a_ticket_and_its_announcement_commit_together(self):
        self.candidate()
        self.digest()
        with self.store.db() as db:
            rows = db.execute("SELECT key FROM approval_outbox WHERE key='ticket-link:1'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_a_ready_ticket_missing_its_announcement_is_restored_once(self):
        self.candidate()
        self.digest()
        with self.store.db() as db:                      # simulate a stop between the two writes
            db.execute("DELETE FROM approval_outbox WHERE key='ticket-link:1'")
        self.assertEqual(self.digest(), {'state': 'empty', 'restored': [1]})
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 1)
        self.assertIn(DASHBOARD, self.transport.messages[0]['text'])
        self.assertEqual(self.digest(), {'state': 'empty'})
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 1)

    def test_a_stop_between_the_ticket_and_its_announcement_rolls_back_both(self):
        self.candidate()
        original = self.desk._link_text
        self.desk._link_text = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.digest()
        self.desk._link_text = original
        with self.store.db() as db:                      # neither half may survive alone
            tickets = db.execute('SELECT COUNT(*) FROM approval_tickets').fetchone()[0]
            notices = db.execute('SELECT COUNT(*) FROM approval_outbox').fetchone()[0]
        self.assertEqual((tickets, notices), (0, 0))
        self.assertEqual(self.digest()['tickets'], [1])  # and the candidate is still announceable
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 1)

    def test_a_sent_announcement_is_never_recreated(self):
        self.candidate()
        self.digest()
        self.desk.flush_responses()
        self.assertEqual(self.digest(), {'state': 'empty'})
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), 1)

    def test_an_action_outcome_and_its_notice_commit_together(self):
        self.candidate()
        self.digest()
        self.act(1, 'approve', action_key='dashboard:atomic')
        with self.store.db() as db:
            action = db.execute("SELECT state FROM approval_actions WHERE key='dashboard:atomic'").fetchone()
            notice = db.execute("SELECT text FROM approval_outbox WHERE key='action:dashboard:atomic'").fetchone()
        self.assertEqual(action['state'], 'done')
        self.assertIn('Published draft 1', notice['text'])

    def test_the_retry_generation_counts_only_recovered_interruptions(self):
        self.candidate()
        self.digest()
        self.assertEqual(self.desk.action_generation(1), 0)
        original = self.desk._revision_plan
        self.desk._revision_plan = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.act(1, 'revise', 'Shorter.', action_key='dashboard:torn')
        self.desk._revision_plan = original
        self.desk.recover()
        self.assertEqual(self.desk.action_generation(1), 1)
        self.act(1, 'revise', 'Shorter.')
        self.assertEqual(self.desk.action_generation(1), 1)

    # --- approval ----------------------------------------------------------------

    def test_approval_writes_only_the_exact_snapshot_and_is_idempotent(self):
        cid, note = self.candidate()
        self.digest()
        result = self.act(1, 'approve', action_key='dashboard:nonce-1')
        self.assertEqual(result['state'], 'published')
        target = self.desk.ticket(1)['snapshot']['target']
        self.assertEqual((self.vault / target).read_text(), note)
        self.assertEqual(self.store.get(cid)['state'], 'published')
        self.assertEqual(self.act(1, 'approve', action_key='dashboard:nonce-1')['state'], 'published')
        self.assertEqual(self.act(1, 'approve', action_key='dashboard:nonce-2')['state'], 'published')
        self.assertEqual(self.publisher.write_count, 1)
        self.assertEqual(len(list(self.vault.rglob('*.md'))), 1)

    def test_update_uses_the_revision_checked_store_and_retains_a_backup(self):
        before = '# Shared spool\nOne daemon owns polling.\n'
        (self.vault / 'Existing.md').write_text(before)
        _, note = self.candidate(verdict='update', target='Existing.md')
        self.digest()
        self.assertEqual(self.desk.ticket(1)['snapshot']['before'], before)
        self.act(1, 'approve')
        self.assertEqual((self.vault / 'Existing.md').read_text(), note)
        backups = list((self.root / 'vault-state/backups').glob('*.bak'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), before)

    def test_a_changed_destination_requeues_review_instead_of_publishing(self):
        (self.vault / 'Existing.md').write_text('Before\n')
        cid, _ = self.candidate(verdict='update', target='Existing.md')
        self.digest()
        (self.vault / 'Existing.md').write_text('Another writer changed this.\n')
        result = self.act(1, 'approve')
        self.assertEqual(result['state'], 'stale')
        self.assertEqual((self.vault / 'Existing.md').read_text(), 'Another writer changed this.\n')
        self.assertEqual(self.desk.ticket(1)['state'], 'stale')
        self.assertEqual(self.store.get(cid)['state'], 'pending')
        self.assertEqual(self.publisher.write_count, 0)

    def test_changed_evidence_requeues_review_instead_of_publishing(self):
        cid, _ = self.candidate()
        self.digest()
        self.source.write_text('The architecture changed.\n')
        self.assertEqual(self.act(1, 'approve')['state'], 'stale')
        self.assertEqual(list(self.vault.rglob('*.md')), [])
        self.assertEqual(self.desk.ticket(1)['state'], 'stale')
        self.assertEqual(self.store.get(cid)['state'], 'pending')

    def test_approval_of_an_unknown_or_terminal_ticket_changes_nothing(self):
        cid, _ = self.candidate()
        self.digest()
        with self.assertRaises(ValueError):
            self.act(99, 'approve')
        self.act(1, 'reject')
        self.assertEqual(self.act(1, 'approve')['state'], 'rejected')
        self.assertEqual(list(self.vault.rglob('*.md')), [])
        self.assertEqual(self.publisher.write_count, 0)

    def test_an_unsupported_action_is_refused(self):
        self.candidate()
        self.digest()
        for action in ('show', 'publish', ''):
            with self.assertRaises(ValueError):
                self.act(1, action)
        self.assertEqual(self.desk.ticket(1)['state'], 'ready')

    def test_an_action_requires_a_durable_key(self):
        self.candidate()
        self.digest()
        for bad in ('', None, 'x' * 201):
            with self.assertRaises(ValueError):
                self.desk.act(1, 'approve', '', bad, ACTOR)
        self.assertEqual(list(self.vault.rglob('*.md')), [])

    def test_no_action_consumes_a_model_allowance(self):
        providers = importlib.import_module('reviewer.providers')
        original = providers.invoke
        providers.invoke = lambda *args, **kwargs: self.fail('An approval action invoked a model')
        self.addCleanup(setattr, providers, 'invoke', original)
        self.candidate()
        self.digest()
        self.act(1, 'approve')
        self.assertEqual(self.store.status()['review_budget']['used_24h'], 0)

    # --- rejection and revision --------------------------------------------------

    def test_rejection_is_terminal_and_writes_nothing(self):
        cid, _ = self.candidate()
        self.digest()
        self.assertEqual(self.act(1, 'reject')['state'], 'rejected')
        self.assertEqual(self.store.get(cid)['state'], 'declined')
        self.assertEqual(list(self.vault.rglob('*.md')), [])
        self.assertEqual(self.desk.pending_tickets(), [])

    def test_revision_invalidates_the_ticket_and_requeues_the_candidate(self):
        cid, note = self.candidate()
        self.digest()
        self.assertEqual(self.act(1, 'revise', 'shorten this to one paragraph')['state'], 'revised')
        self.assertEqual(self.store.get(cid)['state'], 'pending')
        request = self.store.get_meta('revision:' + cid)
        self.assertEqual(request['instructions'], 'shorten this to one paragraph')
        self.assertEqual(request['previous_note'], note)
        self.assertEqual(request['actor']['login'], ACTOR['login'])
        self.assertEqual(self.act(1, 'approve')['state'], 'revised')
        self.assertEqual(list(self.vault.rglob('*.md')), [])
        decision = {'id': cid, 'verdict': 'accept', 'target': '', 'note': 'A shorter note.\n',
                    'reason': 'Shortened.', 'evidence': [{'source': 's1', 'quote': self.source.read_text().strip()}]}
        self.store.mark([cid], 'accepted', decision)
        self.digest()
        self.assertEqual(self.desk.ticket(2)['snapshot']['note'], 'A shorter note.\n')
        self.assertEqual([t['id'] for t in self.desk.pending_tickets()], [2])

    def test_revision_requires_nonblank_bounded_instructions(self):
        self.candidate()
        self.digest()
        for bad in ('', '   ', 'x' * 2001):
            with self.assertRaises(ValueError):
                self.act(1, 'revise', bad)
        self.assertEqual(self.desk.ticket(1)['state'], 'ready')

    def test_revision_can_drop_only_an_originally_blank_uncited_legacy_source(self):
        cid, _ = self.candidate()
        candidate = self.store.get(cid)
        blank = dict(candidate['sources'][0], id='s2', text='\n', start=2, end=2)
        candidate['sources'].append(blank)
        candidate['candidate']['sources'].append({k: blank[k] for k in ('path', 'start', 'end')})
        with self.store.db() as db:
            db.execute('UPDATE candidates SET payload=? WHERE id=?',
                       (json.dumps({k: candidate[k] for k in ('candidate', 'sources')}), cid))
        self.digest()
        self.act(1, 'revise', 'Shorter')
        revised = self.store.get(cid)
        self.assertEqual(revised['state'], 'pending')
        self.assertEqual(len(revised['sources']), 1)

    def test_failed_revision_review_sends_one_result_without_reminders(self):
        cid, _ = self.candidate()
        self.digest()
        self.act(1, 'revise', 'Add an unverified guarantee.')
        self.store.mark([cid], 'uncertain', {'reason': 'The evidence does not establish that guarantee.'})
        self.desk.notify_revision_results()
        self.desk.flush_responses()
        count = len(self.transport.messages)
        self.assertIn('uncertain', self.transport.messages[-1]['text'])
        self.assertIn('does not establish', self.transport.messages[-1]['text'])
        self.desk.notify_revision_results()
        self.desk.flush_responses()
        self.assertEqual(len(self.transport.messages), count)

    # --- interrupted writes ------------------------------------------------------

    def test_crash_after_the_source_save_recovers_without_repeating_the_write(self):
        cid, note = self.candidate()
        self.digest()
        original = self.publisher.publish
        def interrupted(snapshot):
            original(snapshot)
            raise KeyboardInterrupt()
        self.publisher.publish = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.act(1, 'approve')
        self.desk.recover()
        self.assertEqual(self.publisher.write_count, 1)
        self.assertEqual(self.desk.ticket(1)['state'], 'published')
        self.assertEqual(self.store.get(cid)['state'], 'published')
        self.assertEqual((self.vault / self.desk.ticket(1)['snapshot']['target']).read_text(), note)

    def test_a_saved_note_with_a_failed_verification_is_reconciled_before_any_further_action(self):
        cid, note = self.candidate()
        self.digest()
        inner = self.publisher._publisher
        original, calls = inner.read, []
        def fail_verification(target):
            calls.append(target)
            if len(calls) >= 2:
                raise OSError('Temporary read failure after save')
            return original(target)
        inner.read = fail_verification
        self.assertEqual(self.act(1, 'approve')['state'], 'publish_unknown')
        inner.read = original
        self.assertEqual(self.act(1, 'reject')['state'], 'published')
        self.assertEqual(self.store.get(cid)['state'], 'published')
        self.assertEqual((self.vault / self.desk.ticket(1)['snapshot']['target']).read_text(), note)
        self.desk.flush_responses()
        self.assertIn('published', self.transport.messages[-1]['text'])

    def test_a_confirmed_conflict_requeues_review_instead_of_stranding_the_ticket(self):
        # The backend refuses a revision-checked edit before writing. That is a definite
        # no-write, so the draft is merely stale; treating it as unresolved would strand it.
        before = '# Shared spool\nThe published wording.\n'
        (self.vault / 'Existing.md').write_text(before)
        cid, _ = self.candidate(verdict='update', target='Existing.md')
        self.digest()
        conflict = self.publisher.error('revision_conflict', 'the note changed since it was read')
        self.publisher.publish = lambda view: (_ for _ in ()).throw(conflict)
        result = self.act(1, 'approve')
        self.assertEqual(result['state'], 'stale')
        self.assertNotIn('could not be confirmed', result['notice'])
        self.assertEqual(self.desk.ticket(1)['state'], 'stale')
        self.assertEqual(self.store.get(cid)['state'], 'pending')     # back in the review queue
        self.assertEqual((self.vault / 'Existing.md').read_text(), before)
        self.assertEqual(self.desk.pending_tickets(), [])

    def test_an_uncertain_write_is_still_held_for_reconciliation(self):
        cid, _ = self.candidate()
        self.digest()
        uncertain = self.publisher.error('write_failed', 'could not create the note')
        self.publisher.publish = lambda view: (_ for _ in ()).throw(uncertain)
        self.assertEqual(self.act(1, 'approve')['state'], 'publish_unknown')
        self.assertEqual(self.desk.ticket(1)['state'], 'publish_unknown')
        self.assertEqual(self.store.get(cid)['state'], 'accepted')

    def test_an_unresolved_save_result_cannot_be_rejected_or_revised(self):
        cid, _ = self.candidate()
        self.digest()
        self.publisher.publish = lambda view: (_ for _ in ()).throw(OSError('write status unknown'))
        self.assertEqual(self.act(1, 'approve')['state'], 'publish_unknown')
        for action, instructions in (('reject', ''), ('revise', 'Shorter')):
            self.assertEqual(self.act(1, action, instructions)['state'], 'publish_unknown')
        self.assertEqual(self.desk.ticket(1)['state'], 'publish_unknown')
        self.assertEqual(self.store.get(cid)['state'], 'accepted')

    def test_an_interrupted_action_is_not_silently_repeated_after_recovery(self):
        cid, _ = self.candidate()
        self.digest()
        original = self.desk._revision_plan
        self.desk._revision_plan = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.act(1, 'revise', 'Make it shorter.', action_key='dashboard:interrupted')
        self.desk._revision_plan = original
        self.desk.recover()
        self.assertEqual(self.store.get(cid)['state'], 'accepted')
        self.assertEqual(self.desk.ticket(1)['state'], 'ready')
        self.assertEqual(self.desk.act(1, 'revise', 'Make it shorter.', 'dashboard:interrupted', ACTOR)['state'],
                         'interrupted')
        self.assertEqual(self.act(1, 'revise', 'Make it shorter.')['state'], 'revised')
        self.assertEqual(self.store.get_meta('revision:' + cid)['instructions'], 'Make it shorter.')

    def test_a_terminal_ticket_with_an_unfinished_action_is_not_called_interrupted(self):
        # The state a split transaction could leave behind: the decision took effect but its
        # outcome was never recorded. Recovery must report what happened, not deny it.
        for state, expected in (('rejected', 'rejected'), ('revised', 'revised'), ('published', 'published')):
            self.candidate(number=hash(state) % 1000)
            self.digest()
            number = self.desk.pending_tickets()[-1]['id']
            key = f'dashboard:torn-{state}'
            with self.store.db() as db:
                db.execute('UPDATE approval_tickets SET state=? WHERE id=?', (state, number))
                db.execute('INSERT INTO approval_actions VALUES (?,?,?,?,?,?,NULL,?)',
                           (key, number, 'reject', '', json.dumps(ACTOR), 'running', time.time()))
            self.desk.recover()
            with self.store.db() as db:
                row = db.execute('SELECT state,result FROM approval_actions WHERE key=?', (key,)).fetchone()
            outcome = json.loads(row['result'])
            self.assertEqual(row['state'], 'done', state)
            self.assertEqual(outcome['state'], expected, state)
            self.assertNotIn('interrupted', outcome['notice'], state)
            self.assertEqual(self.desk.action_generation(number), 0, state)

    def test_a_reconciled_publication_replays_as_published_not_unresolved(self):
        cid, note = self.candidate()
        self.digest()
        inner = self.publisher._publisher
        original, calls = inner.read, []
        def fail_verification(target):
            calls.append(target)
            if len(calls) >= 2:
                raise OSError('Temporary read failure after save')
            return original(target)
        inner.read = fail_verification
        self.assertEqual(self.act(1, 'approve', action_key='dashboard:recon')['state'], 'publish_unknown')
        inner.read = original
        self.desk.recover()
        self.assertEqual(self.desk.ticket(1)['state'], 'published')
        replay = self.desk.act(1, 'approve', '', 'dashboard:recon', ACTOR)
        self.assertEqual(replay['state'], 'published')
        self.assertEqual(self.store.get(cid)['state'], 'published')

    def test_a_legacy_delivery_state_ticket_becomes_decidable_again(self):
        cid, note = self.candidate()
        self.digest()
        for legacy in ('prepared', 'delivering', 'delivery_unknown'):
            with self.store.db() as db:
                db.execute('UPDATE approval_tickets SET state=? WHERE id=1', (legacy,))
            self.assertEqual(self.desk.pending_tickets(), [], legacy)
            self.assertEqual(self.digest()['state'], 'empty', legacy)   # no replacement is offered
            self.desk.recover()
            self.assertEqual([t['id'] for t in self.desk.pending_tickets()], [1], legacy)
        self.assertEqual(self.act(1, 'approve')['state'], 'published')
        self.assertEqual((self.vault / self.desk.ticket(1)['snapshot']['target']).read_text(), note)

    # --- retired Telegram command path -------------------------------------------

    def test_the_desk_no_longer_accepts_telegram_approval_commands(self):
        self.candidate()
        self.digest()
        for name in ('ingest', 'handle', 'process_commands'):
            self.assertFalse(hasattr(self.desk, name), f'{name} still exposes a Telegram command path')
        self.assertEqual(list(self.vault.rglob('*.md')), [])

    def test_historical_telegram_command_rows_remain_inspectable(self):
        with self.store.db() as db:
            db.execute('INSERT INTO approval_commands VALUES (?,?,?,?,?,?,?)',
                       ('123:9', 1, 'approve', '', '{}', 'done', time.time()))
        self.candidate()
        self.digest()
        self.act(1, 'approve')
        with self.store.db() as db:
            row = db.execute("SELECT * FROM approval_commands WHERE key='123:9'").fetchone()
        self.assertEqual(row['state'], 'done')

    def test_ticket_inspection_still_returns_the_frozen_snapshot(self):
        _, note = self.candidate()
        self.digest()
        self.assertEqual(self.a.inspect_ticket(self.store, 1)['snapshot']['note'], note)
        with self.assertRaises(ValueError):
            self.a.inspect_ticket(self.store, 99)


if __name__ == '__main__':
    unittest.main()


class EveryMessageCarriesTheLink(ApprovalTests):
    """A notification exists to be glanced at and then left. Whatever it says, the next
    thing the reader wants is the page that explains it, so the link is added when the
    message is sent rather than by each thing that writes one."""

    def sent(self):
        self.desk.flush_responses()
        return [m['text'] for m in self.transport.messages]

    def test_a_decision_notice_ends_with_the_dashboard_link(self):
        self.candidate()
        self.digest()
        self.desk.act(1, 'reject', '', self.key(), ACTOR)
        text = self.sent()[-1]
        self.assertTrue(text.endswith(DASHBOARD), text)
        self.assertIn('Declined draft 1', text)

    def test_a_notice_that_already_names_the_page_is_not_given_a_second_link(self):
        self.desk._notify('custom', 'Something happened. See %s' % DASHBOARD)
        self.assertEqual(self.sent()[-1].count(DASHBOARD), 1)

    def test_a_revision_result_carries_it_too(self):
        """This one is written by a different path entirely, which is the reason for
        appending centrally: a message type added later cannot arrive without a way back."""
        cid, _ = self.candidate()
        self.digest()
        self.desk.act(1, 'revise', 'Narrow it.', self.key(), ACTOR)
        self.store.mark([cid], 'uncertain', {'reason': 'Evidence no longer supports it.'})
        self.desk.notify_revision_results()
        results = [t for t in self.sent() if t.startswith('Revision of draft')]
        self.assertTrue(results)
        self.assertTrue(results[0].endswith(DASHBOARD))

    def test_no_dashboard_configured_still_sends_the_message(self):
        desk = self.a.ApprovalDesk(self.store, {k: v for k, v in self.config.items()
                                                if k != 'dashboard'}, self.transport, self.publisher)
        desk._notify('plain', 'A message with nowhere to point.')
        desk.flush_responses()
        self.assertEqual(self.transport.messages[-1]['text'], 'A message with nowhere to point.')
