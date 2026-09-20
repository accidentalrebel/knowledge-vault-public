"""Immutable review tickets and durable, channel-neutral approval actions."""
import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath

from .store import snapshot as source_snapshot


ACTIONS = ('approve', 'reject', 'revise')
# Backend codes raised before any bytes are written. A refusal is a definite no-write,
# so the ticket is merely stale; only a torn or unverifiable write stays unresolved.
CONFIRMED_NO_WRITE = ('revision_conflict', 'already_exists', 'not_found',
                      'ambiguous_edit', 'invalid_argument')
TERMINAL = ('published', 'rejected', 'revised', 'stale', 'superseded')
UNRESOLVED = ('publishing', 'publish_unknown')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate_fingerprint(candidate):
    return digest({key: candidate[key] for key in ('id', 'candidate', 'sources', 'decision')})


def ticket_fingerprint(ticket):
    """Binds an action to one ticket number and the exact text that was displayed."""
    return digest({'id': ticket['id'], 'snapshot': ticket['snapshot']})


def dashboard_url(config):
    section = config['dashboard']
    base = section.get('base_path', '/knowledge-review').strip('/')
    return section['public_origin'].rstrip('/') + '/' + base + '/'


def inspect_ticket(store, number):
    with store.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_tickets'").fetchone()
        row = db.execute('SELECT * FROM approval_tickets WHERE id=?', (number,)).fetchone() if exists else None
    if row is None:
        raise ValueError('Unknown draft number')
    result = dict(row)
    for key in ('snapshot', 'message_ids', 'result'):
        result[key] = json.loads(result[key]) if result[key] else None
    return result


class ApprovalDesk:
    def __init__(self, store, config, transport, publisher):
        self.store, self.config, self.transport, self.publisher = store, config, transport, publisher
        if int(transport.chat_id) <= 0:
            raise ValueError('Knowledge approvals require a positive private Telegram chat ID')
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS approval_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT NOT NULL,
                    snapshot TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                    message_ids TEXT NOT NULL DEFAULT '[]', delivered_at INTEGER,
                    approval_key TEXT, result TEXT);
                CREATE TABLE IF NOT EXISTS approval_commands (
                    key TEXT PRIMARY KEY, ticket INTEGER NOT NULL, action TEXT NOT NULL,
                    instructions TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_actions (
                    key TEXT PRIMARY KEY, ticket INTEGER NOT NULL, action TEXT NOT NULL,
                    instructions TEXT NOT NULL, actor TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS approval_outbox (
                    key TEXT PRIMARY KEY, text TEXT NOT NULL, state TEXT NOT NULL, message_id INTEGER);
            ''')

    def ticket(self, number):
        return inspect_ticket(self.store, number)

    def pending_tickets(self):
        """Frozen views of every ticket still awaiting a human decision, oldest first."""
        with self.store.db() as db:
            rows = db.execute("SELECT id FROM approval_tickets WHERE state='ready' ORDER BY created, id").fetchall()
        views = []
        for row in rows:
            ticket = self.ticket(row['id'])
            views.append(dict(ticket, fingerprint=ticket_fingerprint(ticket)))
        return views

    def action_generation(self, number):
        """Counts recovered interruptions on a ticket.

        An interrupted action is recorded under its own key and replays forever, which is
        what keeps a double submit from acting twice. A caller that derives its action key
        from ticket state alone could therefore never retry the decision it was told to
        retry. Folding this count into that derivation gives a reloaded page a fresh key
        while an already-rendered form keeps replaying its own outcome.
        """
        with self.store.db() as db:
            return db.execute("SELECT COUNT(*) FROM approval_actions WHERE ticket=? AND state='done' "
                              "AND json_extract(result,'$.state')='interrupted'", (number,)).fetchone()[0]

    def set_ticket(self, number, state, result=None):
        with self.store.db() as db:
            db.execute('UPDATE approval_tickets SET state=?,result=? WHERE id=?',
                       (state, json.dumps(result) if result is not None else None, number))

    def _sources_current(self, sources):
        for source in sources:
            path = Path(source['path']).resolve(strict=True)
            if not any(path.is_relative_to(Path(root).resolve()) for root in self.config['source_roots']):
                raise ValueError('Evidence left its configured source root')
            if path.stat().st_size > 2_000_000 or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
                raise ValueError('Evidence changed since review')

    def _make_snapshot(self, candidate):
        decision = candidate['decision']
        if not isinstance(decision, dict) or decision.get('verdict') not in ('accept', 'update'):
            raise ValueError('This candidate has no accepted review')
        self._sources_current(candidate['sources'])
        if decision['verdict'] == 'update':
            target = decision['target']
        else:
            folder = {'project': 'Projects', 'service': 'Services', 'decision': 'Decisions', 'lesson': 'Lessons'}[
                candidate['candidate']['category']]
            title = re.sub(r'[^\w -]+', '', candidate['candidate']['title'], flags=re.UNICODE).strip()[:100]
            if not title:
                raise ValueError('Draft title has no usable filename')
            target = f'{folder}/{title}.md'
        path = PurePosixPath(target)
        if path.is_absolute() or any(part.startswith('.') for part in path.parts) or path.suffix.lower() != '.md':
            raise ValueError('Invalid destination path')
        existing = self.publisher.read(target)
        if decision['verdict'] == 'accept' and existing is not None:
            raise ValueError('The proposed destination already exists')
        if decision['verdict'] == 'update' and existing is None:
            raise ValueError('The update destination no longer exists')
        if existing and existing['content'] == decision['note']:
            raise ValueError('The destination already contains the proposed text')
        return {'candidate_id': candidate['id'], 'candidate_fingerprint': candidate_fingerprint(candidate),
                'title': candidate['candidate']['title'], 'target': target, 'note': decision['note'],
                'before': existing['content'] if existing else None,
                'expected_revision': existing['revision'] if existing else None,
                'reason': decision['reason'], 'sources': candidate['sources']}

    def _fresh(self, ticket):
        view = ticket['snapshot']
        current = self.store.get(ticket['candidate'])
        if current['state'] not in ('accepted', 'updated') or candidate_fingerprint(current) != view['candidate_fingerprint']:
            raise ValueError('The reviewed candidate changed')
        self._sources_current(view['sources'])
        destination = self.publisher.read(view['target'])
        revision = destination['revision'] if destination else None
        if revision != view['expected_revision']:
            raise ValueError('The destination changed')

    def _create_ticket(self, cid, url):
        """A ticket is approvable as soon as it exists; the dashboard reads it directly.

        The row and its announcement commit together. Splitting them would let a stop
        between the two leave a ready ticket that no later digest re-announces, so the
        promised link would never arrive.
        """
        view = self._make_snapshot(self.store.get(cid))
        with self.store.db() as db:
            cursor = db.execute('INSERT INTO approval_tickets(candidate,snapshot,state,created) VALUES (?,?,?,?)',
                                (cid, json.dumps(view), 'ready', time.time()))
            number = cursor.lastrowid
            waiting = db.execute("SELECT COUNT(*) FROM approval_tickets WHERE state='ready'").fetchone()[0]
            if self._announces_links():
                db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)',
                           ('ticket-link:' + str(number), self._link_text(number, waiting, url), 'pending'))
            return number

    def _notify(self, key, text):
        with self.store.db() as db:
            db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)', (key, text, 'pending'))

    def _announces_links(self):
        """With Jev deciding, a 'go and decide' prompt is wrong and a second message per
        note is noise; the channel announces the outcome instead."""
        return not ((self.config.get('jev') or {}).get('enabled'))

    def _link_text(self, number, waiting, url):
        return (f'Knowledge review: draft {number} is ready ({waiting} waiting).\n'
                f'Open {url} to read the exact text and decide.\nReplies here change nothing.')

    def _restore_announcements(self, url):
        """Re-creates an announcement lost before this atomicity fix, or by a torn write."""
        if not self._announces_links():
            return []
        with self.store.db() as db:
            waiting = db.execute("SELECT COUNT(*) FROM approval_tickets WHERE state='ready'").fetchone()[0]
            missing = db.execute("SELECT id FROM approval_tickets t WHERE t.state='ready' AND NOT EXISTS "
                                 "(SELECT 1 FROM approval_outbox o WHERE o.key='ticket-link:'||t.id)").fetchall()
            for row in missing:
                db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)',
                           ('ticket-link:' + str(row['id']), self._link_text(row['id'], waiting, url), 'pending'))
        return [row['id'] for row in missing]

    def digest(self, dashboard_url):
        """Prepare at most one ticket per run and announce it with a link only."""
        with self.store.db() as db:
            db.execute("DELETE FROM meta WHERE key='next_digest_after'")
            rows = db.execute("""SELECT id FROM candidates c WHERE state IN ('accepted','updated')
                AND NOT EXISTS (SELECT 1 FROM approval_tickets t WHERE t.candidate=c.id
                  AND t.state NOT IN ('revised','stale','superseded')) ORDER BY created LIMIT 1""").fetchall()
        numbers = []
        for row in rows:
            try:
                numbers.append(self._create_ticket(row['id'], dashboard_url))
            except (OSError, ValueError, self.publisher.error) as exc:
                self._queue_revision(row['id'], 'Recheck the proposed note and current destination before presenting it. '
                                     + str(exc), previous_note=self.store.get(row['id'])['decision']['note'])
        restored = self._restore_announcements(dashboard_url)
        if not numbers:
            return {'state': 'empty', 'restored': restored} if restored else {'state': 'empty'}
        return {'state': 'announced', 'tickets': numbers}

    def _revision_plan(self, cid, instructions, previous_note='', ticket=None, actor=None):
        """Reads and validates only. Writes happen in _apply_revision so a caller can
        commit the revision together with its ticket state and action outcome."""
        candidate = self.store.get(cid)
        request = {'instructions': instructions, 'previous_note': previous_note,
                   'ticket': ticket, 'actor': actor, 'requested_at': time.time()}
        cited = {item['source'] for item in (candidate.get('decision') or {}).get('evidence', [])}
        # Early pilot records could contain an unused blank excerpt. Never discard
        # a previously nonblank or cited source merely because it now fails validation.
        refs = [ref for ref, old in zip(candidate['candidate']['sources'], candidate['sources'])
                if old['text'].strip() or old['id'] in cited]
        state, failure = 'pending', None
        try:
            sources = [dict(source_snapshot(ref, self.config['source_roots']), id=f's{i+1}')
                       for i, ref in enumerate(refs)]
            if not sources:
                raise ValueError('No usable evidence remains')
        except (OSError, ValueError) as exc:
            state, failure = 'held_stale', str(exc)
            sources, refs = candidate['sources'], candidate['candidate']['sources']
        payload = {'candidate': dict(candidate['candidate'], sources=refs), 'sources': sources}
        notice = (f'The source references need repair before another review: {failure}' if failure else
                  'Revision queued for the next eligible hourly review.')
        return {'cid': cid, 'state': state, 'payload': payload, 'failure': failure,
                'request': request, 'notice': notice}

    def _apply_revision(self, db, plan, ticket=None, ticket_state='revised'):
        db.execute('UPDATE candidates SET state=?,payload=?,decision=? WHERE id=?',
                   (plan['state'], json.dumps(plan['payload']),
                    json.dumps({'reason': plan['failure']}) if plan['failure'] else None, plan['cid']))
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                   ('revision:' + plan['cid'], json.dumps(plan['request'])))
        if ticket is not None:
            db.execute('UPDATE approval_tickets SET state=? WHERE id=?', (ticket_state, ticket))
        return plan['notice']

    def _queue_revision(self, cid, instructions, previous_note='', ticket=None, actor=None, ticket_state='revised'):
        plan = self._revision_plan(cid, instructions, previous_note, ticket, actor)
        with self.store.db() as db:
            return self._apply_revision(db, plan, ticket, ticket_state)

    def _published(self, ticket, result):
        with self.store.db() as db:
            db.execute("UPDATE approval_tickets SET state='published',result=? WHERE id=?",
                       (json.dumps(result), ticket['id']))
            db.execute("UPDATE candidates SET state='published' WHERE id=?", (ticket['candidate'],))

    def _reconcile_publication(self, ticket):
        if ticket['state'] not in UNRESOLVED or not ticket['approval_key']:
            return ticket
        try:
            actual = self.publisher.read(ticket['snapshot']['target'])
        except (OSError, ValueError, self.publisher.error):
            actual = None
        if actual and actual['content'] == ticket['snapshot']['note']:
            notice = (f'Draft {ticket["id"]} was already saved to '
                      f'knowledge/{ticket["snapshot"]["target"]} and is published.')
            with self.store.db() as db:
                db.execute("UPDATE approval_tickets SET state='published',result=? WHERE id=?",
                           (json.dumps({'revision': actual['revision'], 'recovered': True}), ticket['id']))
                db.execute("UPDATE candidates SET state='published' WHERE id=?", (ticket['candidate'],))
                db.execute("UPDATE approval_outbox SET state='superseded' WHERE key=? AND state='pending'",
                           ('action:' + ticket['approval_key'],))
                self._record(db, ticket['approval_key'], ticket['id'], 'published', notice)
                db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)',
                           ('reconciled:' + str(ticket['id']), notice, 'pending'))
            return self.ticket(ticket['id'])
        return ticket

    def _record(self, db, key, number, state, notice):
        """Writes an action outcome and its notice inside the caller's transaction, so a
        terminal ticket transition can never commit without the outcome that explains it."""
        outcome = {'state': state, 'ticket': number, 'notice': notice}
        db.execute("UPDATE approval_actions SET state='done',result=? WHERE key=?", (json.dumps(outcome), key))
        if not self._acted_by_gate(db, key):
            db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)',
                       ('action:' + key, notice, 'pending'))
        return outcome

    def _acted_by_gate(self, db, key):
        """The gate announces its own outcome, so this generic notice would be a second
        message per note saying the same thing in worse words. A person acting on the
        dashboard still gets one, because nothing else confirms their click."""
        row = db.execute('SELECT actor FROM approval_actions WHERE key=?', (key,)).fetchone()
        try:
            return (json.loads(row['actor']) or {}).get('channel') == 'jev'
        except (TypeError, ValueError, KeyError):
            return False

    def _result(self, key, number, state, notice):
        with self.store.db() as db:
            return self._record(db, key, number, state, notice)

    def _stale(self, ticket, key, actor, reason):
        """A confirmed no-write: requeue the candidate and record the outcome in one commit."""
        plan = self._revision_plan(ticket['candidate'], 'The draft or destination changed. Recheck all '
                                   'evidence and preserve current destination content.',
                                   ticket['snapshot']['note'], ticket['id'], actor)
        with self.store.db() as db:
            notice = self._apply_revision(db, plan, ticket['id'], 'stale')
            return self._record(db, key, ticket['id'], 'stale', f'{reason} {notice}')

    def _approve(self, ticket, key, actor):
        number, view = ticket['id'], ticket['snapshot']
        try:
            self._fresh(ticket)
        except (OSError, ValueError, self.publisher.error):
            return self._stale(ticket, key, actor,
                               f'Draft {number} changed since it was prepared and was not published.')
        with self.store.db() as db:
            db.execute("UPDATE approval_tickets SET state='publishing',approval_key=? WHERE id=?", (key, number))
        try:
            result = self.publisher.publish(view)
        except self.publisher.error as exc:
            if getattr(exc, 'code', None) in CONFIRMED_NO_WRITE:
                # The backend refused before writing, so the destination is untouched and
                # this is an ordinary conflict, not an outcome anyone needs to reconcile.
                return self._stale(ticket, key, actor,
                                   f'Draft {number} conflicted with the current destination and was not written.')
            self.set_ticket(number, 'publish_unknown', {'reason': str(exc)})
            return self._result(key, number, 'publish_unknown',
                                f'Draft {number} could not be confirmed as published. It will not be retried '
                                f'automatically; inspect the destination before a fresh review.')
        except (OSError, ValueError) as exc:
            self.set_ticket(number, 'publish_unknown', {'reason': str(exc)})
            return self._result(key, number, 'publish_unknown',
                                f'Draft {number} could not be confirmed as published. It will not be retried '
                                f'automatically; inspect the destination before a fresh review.')
        notice = (f'Published draft {number} to knowledge/{view["target"]}. The exact source text is '
                  'saved; search indexing follows the normal sync interval.')
        with self.store.db() as db:
            db.execute("UPDATE approval_tickets SET state='published',result=? WHERE id=?",
                       (json.dumps(result), number))
            db.execute("UPDATE candidates SET state='published' WHERE id=?", (ticket['candidate'],))
            return self._record(db, key, number, 'published', notice)

    def act(self, number, action, instructions, action_key, actor):
        """One durable, idempotent decision on one frozen ticket, from any channel."""
        if action not in ACTIONS:
            raise ValueError('Supported actions are approve, reject and revise')
        if not isinstance(action_key, str) or not 1 <= len(action_key) <= 200:
            raise ValueError('An action requires a durable key of 1 to 200 characters')
        if not isinstance(actor, dict) or not isinstance(actor.get('login'), str) or not actor['login'].strip():
            raise ValueError('An action requires an identified actor')
        instructions = instructions or ''
        if action == 'revise':
            instructions = instructions.strip()
            if not 1 <= len(instructions) <= 2000:
                raise ValueError('Revision instructions must be nonblank and at most 2000 characters')
        ticket = self._reconcile_publication(self.ticket(number))
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM approval_actions WHERE key=?', (action_key,)).fetchone()
            if existing is None:
                db.execute('INSERT INTO approval_actions VALUES (?,?,?,?,?,?,NULL,?)',
                           (action_key, number, action, instructions, json.dumps(actor), 'running', time.time()))
            else:
                existing = dict(existing)
        if existing is not None:
            if existing['state'] == 'done' and existing['result']:
                return json.loads(existing['result'])
            return {'state': 'in_progress', 'ticket': existing['ticket'],
                    'notice': f'That action on draft {existing["ticket"]} is already running; '
                              'reload the page before trying again.'}
        state = ticket['state']
        if state in TERMINAL:
            return self._result(action_key, number, state,
                                f'Draft {number} is {state}; that number no longer permits changes.')
        if state in UNRESOLVED:
            return self._result(action_key, number, state,
                                f'Draft {number} has an unresolved save result. No further change was made. '
                                f'Inspect the destination before requesting a fresh review.')
        if state != 'ready':
            return self._result(action_key, number, state, f'Draft {number} is not ready for a decision.')
        if action == 'approve':
            return self._approve(ticket, action_key, actor)
        if action == 'reject':
            with self.store.db() as db:
                db.execute("UPDATE approval_tickets SET state='rejected' WHERE id=?", (number,))
                db.execute("UPDATE candidates SET state='declined' WHERE id=?", (ticket['candidate'],))
                return self._record(db, action_key, number, 'rejected',
                                    f'Declined draft {number}. It will not be published or announced again.')
        plan = self._revision_plan(ticket['candidate'], instructions, ticket['snapshot']['note'], number, actor)
        with self.store.db() as db:
            notice = self._apply_revision(db, plan, number)
            return self._record(db, action_key, number, 'revised',
                                f'Draft {number} is no longer approvable. {notice}')

    def _with_link(self, text):
        """Every message ends with the dashboard link.

        The point of a notification here is to be glanceable and then get out of the way;
        whatever it says, the next thing the reader wants is the page that explains it.
        Doing it at the point of sending rather than in each message builder means a new
        kind of notice cannot be added later that quietly arrives without a way back.
        """
        try:
            url = dashboard_url(self.config)
        except (KeyError, TypeError, AttributeError):
            return text
        return text if url in text else '%s\n\n%s' % (text.rstrip(), url)

    def flush_responses(self, limit=10):
        with self.store.db() as db:
            pending = [dict(r) for r in db.execute("SELECT * FROM approval_outbox WHERE state='pending' LIMIT ?", (limit,))]
        for item in pending:
            with self.store.db() as db:
                db.execute("UPDATE approval_outbox SET state='sending' WHERE key=?", (item['key'],))
            try:
                message = self.transport.send(self._with_link(item['text']), silent=True)
                mid = message['message_id']
            except (OSError, ValueError, KeyError):
                with self.store.db() as db:
                    db.execute("UPDATE approval_outbox SET state='delivery_unknown' WHERE key=?", (item['key'],))
                continue
            with self.store.db() as db:
                db.execute("UPDATE approval_outbox SET state='sent',message_id=? WHERE key=?", (mid, item['key']))

    def notify_revision_results(self):
        with self.store.db() as db:
            rows = db.execute("SELECT c.id,c.state,c.decision,m.value FROM candidates c "
                              "JOIN meta m ON m.key='revision:'||c.id WHERE c.state IN "
                              "('rejected','uncertain','held_stale','held_invalid','held_interrupted')").fetchall()
            for row in rows:
                request = json.loads(row['value'])
                if request['ticket'] is None:
                    continue
                reason = (json.loads(row['decision']) if row['decision'] else {}).get('reason', 'Inspect the saved attempt.')
                text = f'Revision of draft {request["ticket"]}: {row["state"]}. {reason[:2000]} No new draft was published.'
                db.execute('INSERT OR IGNORE INTO approval_outbox VALUES (?,?,?,NULL)',
                           ('revision-result:' + row['id'] + ':' + digest(request), text, 'pending'))

    def recover(self):
        with self.store.db() as db:
            db.execute("UPDATE approval_outbox SET state='delivery_unknown' WHERE state='sending'")
            # Tickets frozen by the retired Telegram delivery states are complete rows whose
            # only uncertainty was delivery. The dashboard reads them directly, so delivery no
            # longer gates a decision and they are simply approvable. Without this they would
            # be invisible to the dashboard, refused a replacement by digest, and stranded.
            db.execute("UPDATE approval_tickets SET state='ready' "
                       "WHERE state IN ('prepared','delivering','delivery_unknown')")
            uncertain = [r['id'] for r in db.execute("SELECT id FROM approval_tickets WHERE state IN "
                                                     "('publishing','publish_unknown')")]
        for number in uncertain:
            self._reconcile_publication(self.ticket(number))
        with self.store.db() as db:
            running = [dict(r) for r in db.execute("SELECT * FROM approval_actions WHERE state='running'")]
        for record in running:
            number = record['ticket']
            try:
                ticket = self.ticket(number)
            except ValueError:
                self._result(record['key'], number, 'unknown', f'Unknown knowledge draft {number}.')
                continue
            if ticket['state'] in TERMINAL:
                # The transition committed with its own outcome; do not call it interrupted.
                self._result(record['key'], number, ticket['state'],
                             f'Draft {number} is {ticket["state"]}; that decision already took effect.')
                continue
            if ticket['state'] == 'publishing' and ticket['approval_key'] == record['key']:
                self.set_ticket(number, 'publish_unknown')
                self._result(record['key'], number, 'publish_unknown',
                             f'Draft {number} has an unresolved save result after an interruption. Inspect the '
                             'destination before a fresh review.')
                continue
            self._result(record['key'], number, 'interrupted',
                         f'The {record["action"]} of draft {number} was interrupted and was not repeated. '
                         'Reload the dashboard and decide again.')
