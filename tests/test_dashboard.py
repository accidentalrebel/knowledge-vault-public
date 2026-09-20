import importlib
import json
import re
import unittest
from urllib.parse import urlencode

import test_approvals as approval_fixtures


ORIGIN = 'https://your-host.your-tailnet.ts.net'
LOGIN = approval_fixtures.ACTOR['login']
BASE = '/knowledge-review'
FORM = 'application/x-www-form-urlencoded'


class DashboardTests(unittest.TestCase):
    def setUp(self):
        try:
            self.d = importlib.import_module('reviewer.dashboard')
        except ModuleNotFoundError:
            self.fail('The private review dashboard is not implemented')
        self.fixture = approval_fixtures.ApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app = self.d.DashboardApp(self.fixture.desk, LOGIN, ORIGIN, BASE, secret=b'a' * 32)

    # --- helpers ------------------------------------------------------------------

    def get(self, path='/', headers=None):
        return self.app.handle('GET', path, {'Tailscale-User-Login': LOGIN, **(headers or {})}, b'')

    def review(self, query=''):
        """The full-card page. '/' is the overview and deliberately summarizes."""
        return self.get('/review' + query)

    def post(self, path, fields, headers=None, body=None):
        head = {'Tailscale-User-Login': LOGIN, 'Origin': ORIGIN, 'Content-Type': FORM}
        head.update(headers or {})
        return self.app.handle('POST', path, head, body if body is not None else urlencode(fields).encode())

    def token(self, number, action):
        return self.app.action_token(self.fixture.desk.ticket(number), action)

    def ready(self, **kwargs):
        cid, note = self.fixture.candidate(**kwargs)
        self.fixture.digest()
        return cid, note

    def vault_files(self):
        return sorted(p.name for p in self.fixture.vault.rglob('*.md'))

    # --- reading ------------------------------------------------------------------

    def test_identity_is_required_even_on_loopback(self):
        self.ready()
        for headers in ({}, {'Tailscale-User-Login': 'someone-else@github'},
                        {'Tailscale-User-Login': ''}, {'Tailscale-User-Login': LOGIN.upper() + 'x'}):
            response = self.app.handle('GET', '/', headers, b'')
            self.assertEqual(response.status, 403, headers)
            self.assertNotIn('Shared spool', response.body)

    def test_empty_queue_renders_a_clear_empty_state(self):
        response = self.get()
        self.assertEqual(response.status, 200)
        self.assertIn('No drafts are waiting', response.body)

    def test_pending_ticket_shows_destination_exact_text_reason_and_provenance(self):
        _, note = self.ready()
        body = self.review().body
        self.assertIn('knowledge/Lessons/Shared spool 1.md', body)
        self.assertIn(note.strip().splitlines()[-1], body)
        self.assertIn('A verified reusable polling constraint.', body)
        self.assertIn(str(self.fixture.source), body)
        self.assertIn('lines 1-1', body)
        self.assertIn('One daemon owns polling; consumers read its spool.', body)

    def test_update_tickets_show_the_exact_current_text(self):
        before = '# Shared spool\nThe currently published wording.\n'
        (self.fixture.vault / 'Existing.md').write_text(before)
        self.ready(verdict='update', target='Existing.md')
        body = self.review().body
        self.assertIn('The currently published wording.', body)
        self.assertIn('knowledge/Existing.md', body)

    def test_every_untrusted_field_is_escaped(self):
        self.fixture.source.write_text('<script>alert("evidence")</script>\n')
        self.fixture.candidate(note='<script>alert("note")</script>\n')
        cid = self.fixture.store.status()['items'][0]['id']
        candidate = self.fixture.store.get(cid)
        candidate['decision']['reason'] = '<img src=x onerror="alert(1)">'
        candidate['candidate']['title'] = 'Spool <b>one</b>'
        self.fixture.store.mark([cid], 'accepted', candidate['decision'])
        with self.fixture.store.db() as db:
            db.execute('UPDATE candidates SET payload=? WHERE id=?',
                       (json.dumps({k: candidate[k] for k in ('candidate', 'sources')}), cid))
        self.fixture.digest()
        body = self.review().body
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('&lt;img src=x onerror=', body)
        self.assertNotIn('<script>alert', body)
        self.assertNotIn('onerror="alert(1)"', body)

    def test_the_page_loads_no_remote_assets_and_sets_a_restrictive_policy(self):
        self.ready()
        for response in (self.get(), self.review(), self.get('/published')):
            self.assertNotIn('<script', response.body)
            for value in re.findall(r'(?:href|src|action)="([^"]*)"', response.body):
                self.assertTrue(value.startswith(BASE), value)
        policy = response.headers['Content-Security-Policy']
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn('unsafe-inline', policy)
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')

    def test_local_stylesheet_is_served_from_the_same_origin(self):
        response = self.get(BASE + '/style.css')
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers['Content-Type'].startswith('text/css'))

    def test_the_mounted_path_prefix_and_the_bare_path_render_the_same_page(self):
        self.ready()
        self.assertEqual(self.get(BASE + '/review').body, self.review().body)
        self.assertEqual(self.get(BASE + '/').body, self.get('/').body)
        self.assertEqual(self.get(BASE).status, 200)

    def test_an_unknown_path_is_not_found(self):
        self.assertEqual(self.get('/nope').status, 404)
        self.assertEqual(self.get(BASE + '/ticket/1').status, 404)

    # --- mutation authorization ---------------------------------------------------

    def test_a_get_cannot_mutate_a_ticket(self):
        self.ready()
        for path in ('/ticket/1/approve', BASE + '/ticket/1/reject', '/ticket/1/revise'):
            response = self.get(path)
            self.assertEqual(response.status, 405, path)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.desk.ticket(1)['state'], 'ready')

    def test_a_post_without_the_trusted_identity_is_refused(self):
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        for headers in ({'Tailscale-User-Login': None}, {'Tailscale-User-Login': 'attacker@github'}):
            clean = {k: v for k, v in headers.items() if v is not None}
            response = self.app.handle('POST', '/ticket/1/approve',
                                       {'Origin': ORIGIN, 'Content-Type': FORM, **clean}, urlencode(fields).encode())
            self.assertEqual(response.status, 403)
        self.assertEqual(self.vault_files(), [])

    def test_a_post_from_another_origin_is_refused(self):
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        for origin in ('https://evil.example', 'http://' + ORIGIN[len('https://'):], ORIGIN + '.evil.example', ''):
            response = self.post('/ticket/1/approve', fields, {'Origin': origin})
            self.assertEqual(response.status, 403, origin)
        response = self.app.handle('POST', '/ticket/1/approve',
                                   {'Tailscale-User-Login': LOGIN, 'Content-Type': FORM}, urlencode(fields).encode())
        self.assertEqual(response.status, 403)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.publisher.write_count, 0)

    def test_a_missing_or_altered_action_token_is_refused(self):
        self.ready()
        good = self.token(1, 'approve')
        for fields in ({}, {'token': ''}, {'token': good[:-1] + ('0' if good[-1] != '0' else '1')},
                       {'token': self.token(1, 'reject')}, {'token': 'x' * 64}):
            self.assertEqual(self.post('/ticket/1/approve', fields).status, 403, fields)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.desk.ticket(1)['state'], 'ready')

    def test_a_token_minted_with_another_secret_is_refused(self):
        self.ready()
        other = self.d.DashboardApp(self.fixture.desk, LOGIN, ORIGIN, BASE, secret=b'b' * 32)
        stolen = other.action_token(self.fixture.desk.ticket(1), 'approve')
        self.assertEqual(self.post('/ticket/1/approve', {'token': stolen}).status, 403)
        self.assertEqual(self.vault_files(), [])

    def test_a_wrong_content_type_or_oversized_body_is_refused(self):
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        self.assertEqual(self.post('/ticket/1/approve', fields, {'Content-Type': 'application/json'}).status, 415)
        self.assertEqual(self.post('/ticket/1/approve', fields, body=b'x' * 9000).status, 413)
        self.assertEqual(self.vault_files(), [])

    def test_an_unknown_ticket_or_action_is_not_found(self):
        self.ready()
        self.assertEqual(self.post('/ticket/99/approve', {'token': 'x'}).status, 404)
        self.assertEqual(self.post('/ticket/1/publish', {'token': 'x'}).status, 404)
        self.assertEqual(self.post('/ticket/abc/approve', {'token': 'x'}).status, 404)

    def test_the_dashboard_refuses_to_overlap_the_hourly_reviewer(self):
        worker = importlib.import_module('reviewer.worker')
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        with worker.worker_lock(self.fixture.desk.store) as acquired:
            self.assertTrue(acquired)
            response = self.post('/ticket/1/approve', fields)
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers['Retry-After'], '30')
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.desk.ticket(1)['state'], 'ready')

    # --- mutation outcomes ---------------------------------------------------------

    def test_approval_publishes_the_exact_snapshot_and_reports_it_back(self):
        _, note = self.ready()
        response = self.post('/ticket/1/approve', {'token': self.token(1, 'approve')})
        self.assertEqual(response.status, 303)
        self.assertTrue(response.headers['Location'].startswith(BASE + '/?done='))
        self.assertEqual((self.fixture.vault / 'Lessons/Shared spool 1.md').read_text(), note)
        page = self.get(response.headers['Location'])
        self.assertIn('Published draft 1', page.body)
        self.assertIn('No drafts are waiting', page.body)

    def test_a_duplicate_post_publishes_once(self):
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        first = self.post('/ticket/1/approve', fields)
        second = self.post('/ticket/1/approve', fields)
        self.assertEqual((first.status, second.status), (303, 303))
        self.assertEqual(first.headers['Location'], second.headers['Location'])
        self.assertEqual(self.fixture.publisher.write_count, 1)
        self.assertEqual(len(self.vault_files()), 1)

    def test_a_form_rendered_token_is_the_one_the_dashboard_accepts(self):
        self.ready()
        body = self.review().body
        token = re.search(r'name="token" value="([0-9a-f]{64})"', body)[1]
        self.assertEqual(self.post('/ticket/1/approve', {'token': token}).status, 303)
        self.assertEqual(len(self.vault_files()), 1)

    def test_rejection_removes_the_ticket_without_writing(self):
        cid, _ = self.ready()
        response = self.post('/ticket/1/reject', {'token': self.token(1, 'reject')})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.store.get(cid)['state'], 'declined')
        page = self.get(response.headers['Location'])
        self.assertIn('Declined draft 1', page.body)

    def test_revision_requires_bounded_nonblank_instructions(self):
        self.ready()
        token = self.token(1, 'revise')
        for instructions in ('', '   ', 'x' * 2001):
            response = self.post('/ticket/1/revise', {'token': token, 'instructions': instructions})
            self.assertEqual(response.status, 400, repr(instructions[:10]))
        self.assertEqual(self.fixture.desk.ticket(1)['state'], 'ready')

    def test_revision_requeues_the_candidate_for_a_fresh_review(self):
        cid, _ = self.ready()
        response = self.post('/ticket/1/revise',
                             {'token': self.token(1, 'revise'), 'instructions': 'Shorten it to one paragraph.'})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.fixture.store.get(cid)['state'], 'pending')
        request = self.fixture.store.get_meta('revision:' + cid)
        self.assertEqual(request['instructions'], 'Shorten it to one paragraph.')
        self.assertEqual(request['actor'], {'channel': 'dashboard', 'login': LOGIN})
        self.assertEqual(self.vault_files(), [])
        self.assertIn('no longer approvable', self.get(response.headers['Location']).body)

    def test_a_stale_destination_reports_the_requeue_without_writing(self):
        (self.fixture.vault / 'Existing.md').write_text('Before\n')
        cid, _ = self.ready(verdict='update', target='Existing.md')
        token = self.token(1, 'approve')
        (self.fixture.vault / 'Existing.md').write_text('Another writer changed this.\n')
        response = self.post('/ticket/1/approve', {'token': token})
        self.assertEqual(response.status, 303)
        self.assertEqual((self.fixture.vault / 'Existing.md').read_text(), 'Another writer changed this.\n')
        self.assertEqual(self.fixture.store.get(cid)['state'], 'pending')
        self.assertIn('was not published', self.get(response.headers['Location']).body)

    def test_an_interrupted_decision_can_be_retried_from_a_reloaded_page(self):
        cid, _ = self.ready()
        desk = self.fixture.desk
        original = desk._revision_plan
        desk._revision_plan = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.post('/ticket/1/revise', {'token': self.token(1, 'revise'), 'instructions': 'Shorten it.'})
        desk._revision_plan = original
        desk.recover()
        self.assertEqual(desk.ticket(1)['state'], 'ready')
        # the reloaded page must carry a token that actually completes the decision
        body = self.review().body
        token = re.search(r'action="[^"]*/ticket/1/revise"[^>]*>'
                          r'<input type="hidden" name="token" value="([0-9a-f]{64})"', body)[1]
        response = self.post('/ticket/1/revise', {'token': token, 'instructions': 'Shorten it.'})
        self.assertEqual(response.status, 303)
        self.assertEqual(self.fixture.store.get(cid)['state'], 'pending')
        self.assertIn('no longer approvable', self.get(response.headers['Location']).body)
        self.assertEqual(self.vault_files(), [])

    def test_a_stale_form_from_before_a_recovery_cannot_act(self):
        self.ready()
        desk = self.fixture.desk
        stale = self.token(1, 'approve')
        original = desk._revision_plan
        desk._revision_plan = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.post('/ticket/1/revise', {'token': self.token(1, 'revise'), 'instructions': 'Shorten it.'})
        desk._revision_plan = original
        desk.recover()
        self.assertEqual(self.post('/ticket/1/approve', {'token': stale}).status, 403)
        self.assertEqual(self.vault_files(), [])
        self.assertEqual(self.fixture.publisher.write_count, 0)

    def test_a_double_tap_within_one_generation_still_acts_once(self):
        self.ready()
        fields = {'token': self.token(1, 'approve')}
        first, second = self.post('/ticket/1/approve', fields), self.post('/ticket/1/approve', fields)
        self.assertEqual((first.status, second.status), (303, 303))
        self.assertEqual(first.headers['Location'], second.headers['Location'])
        self.assertEqual(self.fixture.publisher.write_count, 1)

    def test_terminal_feedback_is_escaped_and_an_unknown_receipt_is_ignored(self):
        self.assertNotIn('notice', self.get('/?done=' + 'z' * 40).body)
        self.assertNotIn('notice', self.get('/?done=<script>alert(1)</script>').body)
        with self.fixture.store.db() as db:
            db.execute("INSERT INTO approval_actions VALUES (?,?,?,?,?,?,?,?)",
                       ('dashboard:receipt', 1, 'reject', '', '{}', 'done',
                        json.dumps({'state': 'rejected', 'ticket': 1,
                                    'notice': 'Declined <script>alert(1)</script>.'}), 0))
        body = self.get('/?done=dashboard:receipt').body
        self.assertIn('Declined &lt;script&gt;alert(1)&lt;/script&gt;.', body)
        self.assertNotIn('<script>', body)


if __name__ == '__main__':
    unittest.main()


class PublishedFeedTests(DashboardTests):
    """With Jev deciding, the review queue is normally empty and this feed is the only
    place the published notes are visible."""

    def publish_one(self):
        self.ready()
        return self.post('/ticket/1/approve', {'token': self.token(1, 'approve')})

    def test_empty_feed_says_so(self):
        response = self.get('/published')
        self.assertEqual(response.status, 200)
        self.assertIn('Nothing has been published yet', response.body)

    def test_published_note_appears_with_its_text(self):
        self.publish_one()
        body = self.get('/published').body
        ticket = self.fixture.desk.ticket(1)
        self.assertIn(ticket['snapshot']['title'][:30], body)
        self.assertIn('Exact published text', body)

    def test_feed_records_which_channel_decided(self):
        self.publish_one()
        self.assertIn('decided by dashboard', self.get('/published').body)

    def test_feed_is_read_only(self):
        self.publish_one()
        body = self.get('/published').body
        self.assertNotIn('<form', body)
        self.assertNotIn('<button', body)

    def test_feed_rejects_posts(self):
        self.assertEqual(self.post('/published', {}).status, 405)

    def test_feed_requires_the_configured_login(self):
        response = self.app.handle('GET', BASE + '/published', {'Tailscale-User-Login': 'someone@else'}, b'')
        self.assertEqual(response.status, 403)

    def test_index_links_to_the_feed(self):
        self.assertIn('/published', self.get('/').body)


class OverviewTests(DashboardTests):
    """The glance page. It must answer 'anything happening?' before anything is scrolled,
    and it must never become a second place that can act on a draft."""

    def decide(self, action='approve', reason=''):
        """Take one draft to a terminal state, optionally as the gate did, with a reason."""
        cid, _ = self.ready()
        number = max(t['id'] for t in self.fixture.desk.pending_tickets())
        response = self.post(f'/ticket/{number}/{action}',
                             {'token': self.token(number, action),
                              'instructions': 'Narrow it.' if action == 'revise' else ''})
        if reason:
            with self.fixture.store.db() as db:
                db.execute('UPDATE approval_actions SET actor=? WHERE ticket=?',
                           (json.dumps({'channel': 'jev', 'login': 'jev-auto'}), number))
            outcomes = importlib.import_module('reviewer.outcomes')
            key = [r['key'] for r in self.actions() if r['ticket'] == number][0]
            outcomes.record(self.fixture.store, key, number, cid, action, reason)
        return number, response

    def actions(self):
        with self.fixture.store.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM approval_actions')]

    # --- bottom line up front -------------------------------------------------------

    def test_the_numbers_come_before_the_detail(self):
        self.decide()
        body = self.get().body
        self.assertLess(body.index('class="figures"'), body.index('Recent decisions'))
        self.assertIn('notes published', body)
        self.assertIn('waiting on you', body)

    def test_the_first_line_says_when_and_whether_anything_is_pending(self):
        self.assertIn('Nothing decided yet. No drafts are waiting.', self.get().body)
        self.ready()
        self.assertIn('1 draft is waiting on you.', self.get().body)

    def test_a_published_note_reads_as_published_with_its_destination(self):
        self.decide()
        body = self.get().body
        self.assertIn('Published', body)
        self.assertIn('Today', body)
        # The title is already the filename; only the folder earns a line beside it.
        summary = body[body.index('Recent decisions'):body.index('<details>')]
        self.assertIn('Lessons &middot;', summary)
        self.assertNotIn('knowledge/Lessons/Shared spool 1.md', summary)
        self.assertIn('knowledge/Lessons/Shared spool 1.md', body)

    def test_a_dropped_note_leads_with_the_plain_reason_not_the_arithmetic(self):
        self.decide('reject', reason='Below approval thresholds: factual P(3-4)=0.720 (need 0.85)')
        body = self.get().body
        self.assertIn('Dropped', body)
        self.assertIn('evidence too thin for the claim', body)
        self.assertLess(body.index('evidence too thin'), body.index('P(3-4)'))

    def test_the_full_verdict_and_exact_text_stay_one_click_away(self):
        _, _ = self.decide('reject', reason='Sensitivity gate: P(levels 2-4)=0.510')
        body = self.get().body
        self.assertIn('<details>', body)
        self.assertIn('P(levels 2-4)=0.510', body)
        self.assertIn('One daemon owns polling', body)

    def test_the_gate_is_named_in_words(self):
        self.decide('reject', reason='Sensitivity gate: P(levels 2-4)=0.510')
        self.assertIn('by the gate', self.get().body)

    # --- the overview never acts -----------------------------------------------------

    def test_the_overview_carries_no_controls(self):
        self.decide()
        self.ready()
        body = self.get().body
        self.assertNotIn('<form', body)
        self.assertNotIn('<button', body)
        self.assertNotIn('name="token"', body)

    def test_a_pending_draft_is_named_and_linked_but_not_decidable_here(self):
        self.ready()
        body = self.get().body
        self.assertIn('Waiting on you', body)
        self.assertIn('Shared spool 1', body)
        self.assertIn(BASE + '/review', body)
        self.assertNotIn(self.fixture.desk.ticket(1)['snapshot']['note'], body)

    def test_a_held_candidate_is_named_rather_than_left_invisible(self):
        cid, _ = self.ready()
        self.assertNotIn('held and will not be reviewed', self.get().body)
        self.fixture.store.mark([cid], 'held_stale', {'reason': 'Evidence changed'})
        body = self.get().body
        self.assertIn('1 candidate is held and will not be reviewed', body)
        self.assertIn('reviewer reopen', body)

    def test_a_paused_reviewer_is_named_above_the_numbers(self):
        """Nine hours of a blocked reviewer once read as a quiet day. The numbers alone
        cannot tell those apart, so the banner sits above them."""
        worker = importlib.import_module('reviewer.worker')
        self.assertNotIn('Review is paused', self.get().body)
        worker.note_block(self.fixture.store, "No module named 'httpx'")
        body = self.get().body
        self.assertIn('Review is paused', body)
        self.assertIn("No module named &#x27;httpx&#x27;", body)
        self.assertLess(body.index('Review is paused'), body.index('class="figures"'))
        worker.clear_block(self.fixture.store)
        self.assertNotIn('Review is paused', self.get().body)

    def test_the_overview_rejects_posts(self):
        self.assertEqual(self.post('/', {}).status, 405)
        self.assertEqual(self.post('/review', {}).status, 405)

    def test_untrusted_text_is_escaped_in_the_feed(self):
        self.decide('reject', reason='<img src=x onerror="alert(1)">')
        body = self.get().body
        self.assertIn('&lt;img src=x onerror=', body)
        self.assertNotIn('onerror="alert(1)"', body)
