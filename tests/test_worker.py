import hashlib
import importlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path


class WorkerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.store_module = importlib.import_module('reviewer.store')
            self.worker = importlib.import_module('reviewer.worker')
            self.editorial = importlib.import_module('reviewer.editorial')
        except ModuleNotFoundError:
            self.fail('The durable candidate reviewer has not been implemented')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.source = self.root / 'source.md'
        self.source.write_text('One daemon owns polling.\nOther consumers read its shared spool.\n')
        self.store = self.store_module.Store(self.root / 'state')
        self.config = {'vault': str(self.vault), 'source_roots': [str(self.root)],
                       'providers': [{'name': name, 'kind': 'claude', 'model': 'test',
                                      'profile': str(self.root), 'enabled': True}
                                     for name in ['claude', 'codex', 'glm']]}
        for provider in self.config['providers']:
            self.store.set_meta('qualified:' + provider['name'],
                                {'fingerprint': self.editorial.fingerprint(provider), 'passed': True})

    def submit(self, n=1):
        return self.store.submit({'task_key': f'task-{n}', 'title': f'Polling ownership {n}',
                                  'category': 'service', 'claim': f'One daemon owns polling, case {n}.',
                                  'why_useful': 'A new integration can reuse the shared spool.',
                                  'sources': [{'path': str(self.source), 'start': 1, 'end': 2}]},
                                 self.config['source_roots'])['id']

    def response(self, packet, verdict='reject'):
        return {'decisions': [{'id': c['id'], 'verdict': verdict,
                               'reason': 'Evidence supports the useful bounded claim.' if verdict == 'accept'
                                         else 'No useful addition to the existing record.',
                               'target': '', 'note': '# Polling\nOne daemon owns polling.' if verdict == 'accept' else '',
                               'evidence': [{'source': 's1', 'quote': 'One daemon owns polling.'}]
                                           if verdict == 'accept' else []}
                              for c in packet['candidates']]}

    def never_call(self, *args):
        self.fail('A provider was called when no review should run')

    def test_empty_queue_never_calls_a_model(self):
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'empty')

    def test_revision_request_reaches_reviewer_with_evidence_under_daily_cap(self):
        cid = self.submit()
        request = {'instructions': 'Shorten the explanation.', 'previous_note': 'A long prior draft.',
                   'ticket': 12, 'message': 900, 'requested_at': 1}
        self.store.set_meta('revision:' + cid, request)
        def invoke(provider, packet, timeout, path):
            self.assertEqual(packet['candidates'][0]['revision_request'], request)
            self.assertIn('One daemon owns polling.', packet['candidates'][0]['sources'][0]['text'])
            return self.response(packet, 'accept')
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'reviewed')
        self.submit(2)
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'hourly_limit')

    def test_one_submission_per_task_and_exact_content_deduplication(self):
        first = self.submit()
        self.assertEqual(first, self.submit())
        payload = self.store.get(first)['candidate']
        payload['task_key'] = 'different-task'
        self.assertEqual(self.store.submit(payload, self.config['source_roots'])['id'], first)
        self.assertEqual(self.store.status()['candidates'], {'pending': 1})

    def test_submission_refuses_evidence_outside_roots_or_beyond_file(self):
        cid = self.submit()
        payload = self.store.get(cid)['candidate']
        payload['task_key'] = 'bad-path'
        payload['sources'][0]['path'] = '/etc/passwd'
        with self.assertRaises(ValueError):
            self.store.submit(payload, self.config['source_roots'])
        payload['sources'][0] = {'path': str(self.source), 'start': 1, 'end': 200}
        with self.assertRaises(ValueError):
            self.store.submit(payload, self.config['source_roots'])

    def test_blank_evidence_excerpt_is_rejected_at_intake(self):
        cid = self.submit()
        payload = self.store.get(cid)['candidate']
        payload['task_key'] = 'blank-evidence'
        self.source.write_text('\n\n')
        with self.assertRaises(ValueError):
            self.store.submit(payload, self.config['source_roots'])

    def test_one_per_hour_and_rolling_day_limit_survive_restart_and_expire_individually(self):
        """Pins the mechanism at a capacity of three; the shipped value is asserted
        separately by ShippedCapacity so raising it cannot silently pass here."""
        for n in range(4):
            self.submit(n)
        seen = []
        def invoke(provider, packet, timeout, path):
            seen.append(len(packet['candidates']))
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 180)
            return self.response(packet)
        base = 2_000_000_000
        capacity = patch('reviewer.budget.CAPACITY', 3)
        capacity.start()
        self.addCleanup(capacity.stop)
        self.config['review'] = {'batch': 1}
        with patch('time.time', return_value=base):
            self.worker.run(self.store, self.config, invoke)
        self.assertEqual(seen, [1])
        reopened = self.store_module.Store(self.store.root)
        with patch('time.time', return_value=base + 3599):
            self.assertEqual(self.worker.run(reopened, self.config, self.never_call)['state'], 'hourly_limit')
        for elapsed in (3600, 7200):
            with patch('time.time', return_value=base + elapsed):
                self.assertEqual(self.worker.run(reopened, self.config, invoke)['count'], 1)
        self.assertEqual(seen, [1, 1, 1])
        with patch('time.time', return_value=base + 86399):
            result = self.worker.run(reopened, self.config, self.never_call)
            self.assertEqual(result['state'], 'daily_limit')
            self.assertEqual(result['next_run_after'], base + 86400)
        with patch('time.time', return_value=base + 86400):
            self.assertEqual(self.worker.run(reopened, self.config, invoke)['count'], 1)

    def test_availability_failure_falls_back_once_and_persists_cooldown(self):
        self.submit()
        seen = []
        def invoke(provider, packet, timeout, path):
            seen.append(provider['name'])
            if provider['name'] == 'claude':
                raise self.editorial.Unavailable('quota')
            return self.response(packet)
        result = self.worker.run(self.store, self.config, invoke)
        self.assertEqual(result['state'], 'reviewed')
        self.assertEqual(seen, ['claude', 'codex'])
        self.assertEqual(self.store.status()['attempts'], {'unavailable': 1, 'completed': 1})
        self.assertIsNotNone(self.store.get_meta('cooldown:claude'))

    def test_uncertain_is_terminal_and_does_not_trigger_fallback(self):
        cid = self.submit()
        def invoke(provider, packet, timeout, path):
            self.assertEqual(provider['name'], 'claude')
            return self.response(packet, 'uncertain')
        self.worker.run(self.store, self.config, invoke)
        self.assertEqual(self.store.get(cid)['state'], 'uncertain')
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'empty')

    def test_invalid_candidate_is_held_without_fallback_and_next_candidate_stays_pending(self):
        self.config['review'] = {'batch': 1}
        for n in range(2):
            self.submit(n)
        def invoke(provider, packet, timeout, path):
            self.assertEqual(provider['name'], 'claude')
            result = self.response(packet, 'accept')
            result['decisions'][0]['evidence'][0]['quote'] = 'A fabricated quotation from a source.'
            return result
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_invalid')
        self.assertEqual(self.store.status()['candidates'], {'held_invalid': 1, 'pending': 1})

    def legacy_reservation(self, base, count=1, missing=False):
        ids = [self.submit(i) for i in range(count)]
        self.store.set_meta('next_run_after', base + 86400)
        with patch('time.time', return_value=base + 1):
            aid, path = self.store.start_attempt('claude', ids)
        if not missing:
            packet = {'candidates': [{'id': cid} for cid in ids]}
            (path / 'packet.json').write_text(json.dumps(packet))
        self.store.mark(ids, 'rejected', {'reason': 'Historical review'})
        self.store.finish_attempt(aid, 'completed')
        return ids

    def test_legacy_single_candidate_counts_once_and_does_not_block_the_whole_day(self):
        base = 2_000_000_000
        self.legacy_reservation(base)
        self.submit(10)
        with patch('time.time', return_value=base + 3600):
            result = self.worker.run(self.store, self.config, lambda p, packet, t, path: self.response(packet))
            self.assertEqual(result['state'], 'reviewed')
            self.assertEqual(self.store.status()['review_budget']['used_24h'], 2)
        reopened = self.store_module.Store(self.store.root)
        with patch('time.time', return_value=base + 7200):
            self.assertEqual(reopened.status()['review_budget']['used_24h'], 2)

    def test_legacy_full_batch_and_missing_packet_preserve_conservative_daily_cap(self):
        base = 2_000_000_000
        capacity = patch('reviewer.budget.CAPACITY', 3)
        capacity.start()
        self.addCleanup(capacity.stop)
        for count, missing in ((3, False), (1, True)):
            with self.subTest(count=count, missing=missing):
                self.store = self.store_module.Store(self.root / f'state-{count}')
                self.legacy_reservation(base, count, missing)
                self.submit(10)
                with patch('time.time', return_value=base + 3600):
                    result = self.worker.run(self.store, self.config, self.never_call)
                self.assertEqual(result['state'], 'daily_limit')
                self.assertEqual(result['next_run_after'], base + 86400)

    def test_fallback_chain_reserves_one_unit_and_invalid_review_keeps_it(self):
        self.submit()
        def invoke(provider, packet, timeout, path):
            if provider['name'] == 'claude':
                raise self.editorial.Unavailable('quota')
            return {'decisions': []}
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_invalid')
        self.assertEqual(self.store.status()['review_budget']['used_24h'], 1)

    def test_crash_reservation_survives_without_refunding_an_unknown_model_call(self):
        self.submit()
        def crash(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.worker.run(self.store, self.config, crash)
        self.assertEqual(self.store_module.Store(self.store.root).status()['review_budget']['used_24h'], 1)

    def test_empty_and_unqualified_checks_do_not_consume_allowance(self):
        self.worker.run(self.store, self.config, self.never_call)
        self.submit()
        self.config['providers'] = []
        self.worker.run(self.store, self.config, self.never_call)
        self.assertEqual(self.store.status()['review_budget']['used_24h'], 0)

    def test_legacy_fallback_and_qualification_packets_do_not_multiply_usage(self):
        base = 2_000_000_000
        ids = self.legacy_reservation(base)
        with patch('time.time', return_value=base + 10):
            aid, path = self.store.start_attempt('codex', [])
            (path / 'packet.json').write_text(json.dumps({'candidates': [{'id': cid} for cid in ids]}))
            self.store.finish_attempt(aid, 'completed')
            aid, path = self.store.start_attempt('claude', [])
            examples = ['useful', 'unsupported', 'duplicate', 'misleading', 'addition', 'conflict']
            (path / 'packet.json').write_text(json.dumps({'candidates': [{'id': cid} for cid in examples]}))
            self.store.finish_attempt(aid, 'unavailable')
        with patch('time.time', return_value=base + 3600):
            self.assertEqual(self.store.status()['review_budget']['used_24h'], 1)

    def test_legacy_inconsistent_fallback_packets_reserve_the_whole_ceiling(self):
        from reviewer import budget
        base = 2_000_000_000
        self.legacy_reservation(base)
        extra = self.submit(2)
        with patch('time.time', return_value=base + 10):
            aid, path = self.store.start_attempt('codex', [extra])
            (path / 'packet.json').write_text(json.dumps({'candidates': [{'id': extra}]}))
            self.store.finish_attempt(aid, 'completed')
        with patch('time.time', return_value=base + 3600):
            self.assertEqual(self.store.status()['review_budget']['used_24h'], budget.CAPACITY)

    def test_two_reservations_racing_for_one_hour_are_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from reviewer.budget import reserve
        cid = self.submit()
        other = self.store_module.Store(self.store.root)
        barrier = Barrier(2)
        def claim(store):
            barrier.wait()
            return reserve(store, cid, 2_000_000_000)['state']
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(claim, self.store)
            b = pool.submit(claim, other)
            self.assertEqual(sorted([a.result(), b.result()]), ['hourly_limit', 'reserved'])

    def test_crash_between_reservation_and_attempt_keeps_hourly_guard(self):
        cid = self.submit()
        with patch.object(self.store, 'start_attempt', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.run(self.store, self.config, self.never_call)
        self.assertEqual(self.store.get(cid)['state'], 'pending')
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'hourly_limit')

    def test_duplicate_or_unknown_decision_ids_are_held(self):
        self.submit()
        def invoke(provider, packet, timeout, path):
            result = self.response(packet)
            result['decisions'].append(dict(result['decisions'][0]))
            return result
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_invalid')

    def test_changed_evidence_is_held_before_any_model_call(self):
        cid = self.submit()
        self.source.write_text('There is now a different owner.\n')
        self.worker.run(self.store, self.config, self.never_call)
        self.assertEqual(self.store.get(cid)['state'], 'held_stale')

    def test_evidence_changed_during_review_is_held_without_fallback(self):
        cid = self.submit()
        def invoke(provider, packet, timeout, path):
            self.assertEqual(provider['name'], 'claude')
            self.source.write_text('Polling ownership changed during the review.\n')
            return self.response(packet, 'accept')
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_stale')
        self.assertEqual(self.store.get(cid)['state'], 'held_stale')

    def test_evidence_changed_during_failed_call_prevents_fallback(self):
        self.submit()
        seen = []
        def invoke(provider, packet, timeout, path):
            seen.append(provider['name'])
            self.source.write_text('Polling ownership changed during the failed call.\n')
            raise self.editorial.Unavailable('quota')
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_stale')
        self.assertEqual(seen, ['claude'])
        self.assertEqual(self.store.status()['attempts'], {'unavailable_stale': 1})

    def test_vault_changed_during_review_is_held_without_fallback(self):
        cid = self.submit()
        def invoke(provider, packet, timeout, path):
            self.assertEqual(provider['name'], 'claude')
            (self.vault / 'new-duplicate.md').write_text('One daemon owns polling.\n')
            return self.response(packet, 'accept')
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_stale')
        self.assertEqual(self.store.get(cid)['state'], 'held_stale')

    def test_total_deadline_leaves_untried_providers_alone(self):
        from unittest.mock import patch
        self.submit()
        seen = []
        def unavailable(provider, packet, timeout, path):
            seen.append((provider['name'], timeout))
            raise self.editorial.Unavailable('quota')
        with patch('reviewer.worker.time.monotonic', side_effect=[0, 0, 599, 601]):
            result = self.worker.run(self.store, self.config, unavailable)
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(seen, [('claude', 180), ('codex', 1)])

    def test_crash_is_recorded_and_recovery_holds_inflight_candidates(self):
        cid = self.submit()
        def crash(*args):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.worker.run(self.store, self.config, crash)
        reopened = self.store_module.Store(self.store.root)
        self.worker.run(reopened, self.config, self.never_call)
        self.assertEqual(reopened.get(cid)['state'], 'held_interrupted')

    def test_accept_is_a_saved_draft_and_does_not_write_vault(self):
        cid = self.submit()
        self.worker.run(self.store, self.config,
                        lambda provider, packet, timeout, path: self.response(packet, 'accept'))
        self.assertEqual(list(self.vault.iterdir()), [])
        record = self.store.get(cid)
        self.assertEqual(record['state'], 'accepted')
        self.assertIn('One daemon', record['decision']['note'])

    def test_a_protocol_error_is_held_without_fallback_or_cooldown(self):
        # End to end: a malformed response mentioning an ambiguous word must stop the review
        # for inspection, not cool the provider down and spend a second model call.
        self.submit()
        calls = []
        def malformed(provider, packet, timeout, attempt):
            calls.append(provider['name'])
            raise self.editorial.InvalidReview('JSON parse error: invalid token at position 7')
        result = self.worker.run(self.store, self.config, malformed)
        self.assertEqual(result['state'], 'held_invalid')
        self.assertEqual(calls, [self.config['providers'][0]['name']])
        self.assertIsNone(self.store.get_meta('cooldown:' + calls[0]))

    def test_unqualified_providers_are_skipped_without_calls(self):
        self.submit()
        for p in self.config['providers']:
            p['model'] = 'changed-model'
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'no_provider')

    def test_missing_or_oversized_vault_is_not_treated_as_no_duplicates(self):
        self.submit()
        self.vault.rmdir()
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'context_blocked')
        self.vault.mkdir()
        (self.vault / 'huge.md').write_text('a' * 130_000)
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'context_blocked')

    def test_concurrent_worker_exits_without_calling_provider(self):
        import fcntl
        self.submit()
        with (self.store.root / 'worker.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'], 'busy')


class PacingConfigTests(WorkerTests):
    """Batch size and spacing are config knobs so a backlog can be drained without a code
    edit. Both default to the shipped values when the config says nothing."""

    def test_batch_reviews_several_candidates_in_one_call(self):
        for n in range(5):
            self.submit(n)
        self.config['review'] = {'batch': 5}
        seen = []
        def invoke(provider, packet, timeout, path):
            seen.append(len(packet['candidates']))
            return self.response(packet)
        self.worker.run(self.store, self.config, invoke)
        self.assertEqual(seen, [5])

    def test_batch_defaults_to_the_shipped_size(self):
        for n in range(9):
            self.submit(n)
        seen = []
        def invoke(provider, packet, timeout, path):
            seen.append(len(packet['candidates']))
            return self.response(packet)
        self.worker.run(self.store, self.config, invoke)
        self.assertEqual(seen, [self.worker.BATCH])

    def test_interval_shortens_the_wait_between_runs(self):
        for n in range(4):
            self.submit(n)
        self.config['review'] = {'batch': 1, 'interval_seconds': 5}
        base = 2_000_000_000
        def invoke(provider, packet, timeout, path):
            return self.response(packet)
        with patch('time.time', return_value=base):
            self.worker.run(self.store, self.config, invoke)
        with patch('time.time', return_value=base + 2):
            self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'hourly_limit')
        with patch('time.time', return_value=base + 6):
            self.assertEqual(self.worker.run(self.store, self.config, invoke)['count'], 1)

    def test_default_interval_is_still_an_hour(self):
        for n in range(2):
            self.submit(n)
        self.config['review'] = {'batch': 1}
        base = 2_000_000_000
        def invoke(provider, packet, timeout, path):
            return self.response(packet)
        with patch('time.time', return_value=base):
            self.worker.run(self.store, self.config, invoke)
        with patch('time.time', return_value=base + 3599):
            self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'hourly_limit')


class BatchIsolationTests(WorkerTests):
    """A fabricated quotation in one decision used to hold every candidate sharing the
    call. Two bad quotes cost twelve good candidates on the first real backlog."""

    def bad_quote(self, packet, victim_index):
        result = self.response(packet, 'accept')
        result['decisions'][victim_index]['evidence'][0]['quote'] = 'A fabricated quotation.'
        return result

    def test_one_bad_decision_holds_only_its_own_candidate(self):
        ids = [self.submit(n) for n in range(4)]
        self.config['review'] = {'batch': 4}
        calls = []
        def invoke(provider, packet, timeout, path):
            calls.append(len(packet['candidates']))
            if len(packet['candidates']) > 1:
                return self.bad_quote(packet, 2)
            if packet['candidates'][0]['id'] == ids[2]:
                return self.bad_quote(packet, 0)
            return self.response(packet)
        result = self.worker.run(self.store, self.config, invoke)
        self.assertEqual(result['state'], 'reviewed_individually')
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['held_invalid'], 1)
        self.assertEqual(self.store.get(ids[2])['state'], 'held_invalid')
        for good in (ids[0], ids[1], ids[3]):
            self.assertNotEqual(self.store.get(good)['state'], 'held_invalid')
        self.assertEqual(calls, [4, 1, 1, 1, 1])

    def test_a_single_candidate_batch_still_holds_directly(self):
        cid = self.submit()
        self.config['review'] = {'batch': 1}
        def invoke(provider, packet, timeout, path):
            return self.bad_quote(packet, 0)
        result = self.worker.run(self.store, self.config, invoke)
        self.assertEqual(result['state'], 'held_invalid')
        self.assertEqual(self.store.get(cid)['state'], 'held_invalid')

    def test_retry_spends_no_extra_reservation(self):
        for n in range(3):
            self.submit(n)
        self.config['review'] = {'batch': 3}
        def invoke(provider, packet, timeout, path):
            if len(packet['candidates']) > 1:
                return self.bad_quote(packet, 0)
            return self.response(packet)
        before = self.store.status()['review_budget']['used_24h']
        self.worker.run(self.store, self.config, invoke)
        after = self.store.status()['review_budget']['used_24h']
        self.assertEqual(after - before, 1)


class ShippedCapacity(unittest.TestCase):
    """The shipped daily ceiling is a policy decision, asserted apart from the mechanism."""

    def test_daily_ceiling_does_not_bind_before_the_hourly_reservation(self):
        from reviewer import budget
        self.assertGreaterEqual(budget.CAPACITY, budget.DAY // budget.HOUR)


if __name__ == '__main__':
    unittest.main()


class SearchDedupTests(unittest.TestCase):
    """The worker asking Khoj which notes matter, instead of sending every note.

    The guarantee at stake is that an already-covered candidate cannot reach the vault, so
    these pin the two ways search could weaken it: answering "nothing found" when it is
    actually down, and missing a note that changed while the model was thinking.

    Deliberately not a subclass of WorkerTests: those tests assert the whole-vault
    behavior this replaces, including a size ceiling that search removes.
    """

    submit = WorkerTests.submit
    response = WorkerTests.response
    never_call = WorkerTests.never_call

    def setUp(self):
        WorkerTests.setUp(self)
        self.dedup = importlib.import_module('reviewer.dedup')
        self.manifest = self.root / 'sync-manifest.json'
        self.config['dedup'] = {'manifest': str(self.manifest), 'namespace': 'knowledge'}
        self.hits, self.failing = [], False
        outer = self

        def search(settings, query):
            if outer.failing:
                raise OSError('connection refused')
            return outer.hits[:settings['neighbours']]

        original = self.dedup.search_hits
        self.dedup.search_hits = search
        self.addCleanup(lambda: setattr(self.dedup, 'search_hits', original))

    def note(self, name, text, indexed=True):
        (self.vault / name).write_text(text)
        files = json.loads(self.manifest.read_text())['files'] if self.manifest.exists() else {}
        files[name] = {'digest': hashlib.sha256(text.encode()).hexdigest(), 'indexed': indexed}
        self.manifest.write_text(json.dumps({'files': files}))

    def hit(self, name, distance=0.05):
        return {'path': 'knowledge/' + name, 'distance': distance}

    def test_a_search_outage_pauses_review_instead_of_publishing(self):
        self.submit()
        self.note('Existing.md', '# Existing\nOne daemon owns polling.\n')
        self.failing = True
        result = self.worker.run(self.store, self.config, self.never_call)
        self.assertEqual(result['state'], 'context_blocked')
        self.assertIn('Khoj', result['detail'])

    def test_a_pause_is_recorded_so_something_can_say_so(self):
        """A paused reviewer publishes nothing and announces nothing, which is exactly
        what a quiet week looks like. It has to leave a mark."""
        self.submit()
        self.failing = True
        self.worker.run(self.store, self.config, self.never_call)
        blocked = self.store.get_meta(self.worker.BLOCK_KEY)
        self.assertIn('Khoj', blocked['detail'])
        self.assertGreater(blocked['since'], 0)

    def test_the_first_time_it_broke_is_kept_across_repeated_failures(self):
        self.submit()
        self.failing = True
        self.worker.run(self.store, self.config, self.never_call)
        first = self.store.get_meta(self.worker.BLOCK_KEY)['since']
        self.worker.run(self.store, self.config, self.never_call)
        self.assertEqual(self.store.get_meta(self.worker.BLOCK_KEY)['since'], first)

    def test_a_working_search_clears_the_pause(self):
        self.submit()
        self.note('Near.md', '# Near\nOne daemon owns polling.\n')
        self.failing = True
        self.worker.run(self.store, self.config, self.never_call)
        self.assertIsNotNone(self.store.get_meta(self.worker.BLOCK_KEY))
        self.failing = False
        self.hits = [self.hit('Near.md')]
        self.store.set_meta('next_review_after', 0)
        self.worker.run(self.store, self.config, lambda p, packet, t, path: self.response(packet))
        self.assertIsNone(self.store.get_meta(self.worker.BLOCK_KEY))

    def test_the_packet_lists_every_note_but_carries_only_the_neighbours(self):
        self.submit()
        self.note('Near.md', '# Near\nOne daemon owns polling.\n')
        self.note('Far.md', '# Far\nSomething unrelated entirely.\n')
        self.hits = [self.hit('Near.md')]
        seen = {}
        def invoke(provider, packet, timeout, path):
            seen.update(packet)
            return self.response(packet)
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'reviewed')
        self.assertEqual(sorted(n['path'] for n in seen['vault_listing']), ['Far.md', 'Near.md'])
        self.assertEqual([n['path'] for n in seen['existing_notes']], ['Near.md'])

    def test_a_note_the_index_has_not_caught_up_with_is_still_compared(self):
        self.submit()
        self.note('Fresh.md', '# Fresh\nOne daemon owns polling.\n', indexed=False)
        self.hits = []
        seen = {}
        def invoke(provider, packet, timeout, path):
            seen.update(packet)
            return self.response(packet)
        self.worker.run(self.store, self.config, invoke)
        self.assertEqual([n['path'] for n in seen['existing_notes']], ['Fresh.md'])

    def test_a_note_absent_from_the_packet_still_makes_the_review_stale(self):
        """The whole-vault packet caught any vault change because it contained everything.
        A subset cannot, so the check compares digests taken from disk instead - otherwise
        a note published mid-review would silently escape the duplicate comparison."""
        cid = self.submit()
        self.note('Near.md', '# Near\nOne daemon owns polling.\n')
        self.hits = [self.hit('Near.md')]
        def invoke(provider, packet, timeout, path):
            self.assertNotIn('Surprise.md', [n['path'] for n in packet['existing_notes']])
            (self.vault / 'Surprise.md').write_text('One daemon owns polling.\n')
            return self.response(packet, 'accept')
        self.assertEqual(self.worker.run(self.store, self.config, invoke)['state'], 'held_stale')
        self.assertEqual(self.store.get(cid)['state'], 'held_stale')
        self.assertEqual(list(p.name for p in self.vault.iterdir() if p.name == 'accepted.md'), [])

    def test_no_provider_is_called_when_every_candidate_is_already_stale(self):
        """Search costs a round trip; a batch with nothing reviewable should not pay it."""
        self.submit()
        self.source.write_text('The evidence moved on.\n')
        self.failing = True
        self.assertEqual(self.worker.run(self.store, self.config, self.never_call)['state'],
                         'held_stale')
