"""The decision log the dashboard reads.

`approval_actions` records that a decision happened and what it did to the ticket; it
never kept *why*, because the reason lives in the gate's verdict and used to exist only
in the Telegram message that carried it. This module is that missing column: one row per
gate decision, written in the run that made it, plus the read the dashboard renders.

Nothing here decides or publishes anything. Every function is safe to call on a database
that predates it.
"""
import hashlib
import json
import time


SCHEMA = ('CREATE TABLE IF NOT EXISTS jev_outcomes ('
          'key TEXT PRIMARY KEY, ticket INTEGER NOT NULL, candidate TEXT NOT NULL, '
          'action TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL)')

# How a finished action reads on the page. Keyed by the recorded *outcome*, not the
# requested action, so a ticket that went stale under an approval is not called published.
VERDICT = {'published': 'Published', 'revised': 'Sent back', 'rejected': 'Dropped',
           'stale': 'Needs a look', 'publish_unknown': 'Needs a look',
           'interrupted': 'Needs a look'}
TONE = {'Published': 'good', 'Sent back': 'wait', 'Dropped': 'drop', 'Needs a look': 'warn'}

# What a decision does to a ticket when it succeeds. Used to name an outcome the caller
# has not yet read back from the desk.
ACTION_STATE = {'approve': 'published', 'revise': 'revised', 'reject': 'rejected'}

# The marks the gate used in its per-draft Telegram messages, for reading decisions made
# before this table existed back out of those messages.
LEGACY_MARK = {'PUBLISHED': 'published', 'NARROWING': 'revised', 'DROPPED': 'rejected'}


def ensure(db):
    db.execute(SCHEMA)


PHRASES = (('sensitivity gate', 'held back as sensitive'),
           ('deterministic secret scan', 'a credential pattern matched'),
           ('below approval thresholds', 'evidence too thin for the claim'),
           ('revision budget exhausted', 'out of revision attempts'),
           ('no further revision', 'out of revision attempts'))


def short_reason(action, reason):
    """The stored reason is a threshold dump. Keep its shape, drop the arithmetic.

    Returns '' for an approval: 'published' already says everything a reader needs.
    Matching is case-insensitive on purpose - a reason recovered from an old notice was
    already lowercased once, and history should not read differently from today.
    """
    reason = (reason or '').strip()
    if action == 'approve' or not reason:
        return ''
    lowered = reason.lower()
    for prefix, phrase in PHRASES:
        if lowered.startswith(prefix):
            return phrase
    return reason.split(':')[0][:80].lower()


def record(store, key, ticket, candidate, action, reason):
    """Insert-or-ignore, so replaying a decision never rewrites the reason it gave."""
    with store.db() as db:
        ensure(db)
        db.execute('INSERT OR IGNORE INTO jev_outcomes VALUES (?,?,?,?,?,?)',
                   (key, ticket, candidate, action, reason or '', time.time()))


def backfill(store):
    """Recover pre-table decisions from the notices the gate actually sent.

    Those messages are the only surviving record of the reason, and their second line is
    the same short phrase `short_reason` produces today. Recovering the short form rather
    than inventing a threshold dump keeps the page honest about what is known.
    """
    recovered = 0
    with store.db() as db:
        ensure(db)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='approval_outbox'").fetchone():
            return 0
        rows = db.execute(
            "SELECT a.key, a.ticket, a.action, a.created, t.candidate, o.text "
            "FROM approval_actions a JOIN approval_tickets t ON t.id = a.ticket "
            "LEFT JOIN approval_outbox o ON o.key = 'jev-outcome:' || a.key "
            "WHERE a.state='done' AND json_extract(a.actor,'$.channel')='jev' "
            "AND a.key NOT IN (SELECT key FROM jev_outcomes)").fetchall()
        for row in rows:
            lines = (row['text'] or '').splitlines()
            reason = lines[1].strip() if len(lines) > 1 else ''
            db.execute('INSERT OR IGNORE INTO jev_outcomes VALUES (?,?,?,?,?,?)',
                       (row['key'], row['ticket'], row['candidate'], row['action'],
                        reason, row['created']))
            recovered += 1
    return recovered


def _outcome_state(result, action):
    try:
        return (json.loads(result) or {}).get('state') or action
    except (TypeError, ValueError):
        return action


def decisions(store, limit=60):
    """Every finished decision, newest first, with everything the page shows.

    Spined on `approval_actions` rather than on this module's own table so a decision made
    from the dashboard by hand appears beside the gate's, with its reason simply absent.
    """
    with store.db() as db:
        ensure(db)
        rows = db.execute(
            "SELECT a.key, a.ticket, a.action, a.actor, a.created, a.result, "
            "       o.reason, t.snapshot, t.result AS saved, t.candidate "
            "FROM approval_actions a JOIN approval_tickets t ON t.id = a.ticket "
            "LEFT JOIN jev_outcomes o ON o.key = a.key "
            "WHERE a.state='done' ORDER BY a.created DESC, a.rowid DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for row in rows:
        view = json.loads(row['snapshot'])
        saved = json.loads(row['saved']) if row['saved'] else {}
        actor = json.loads(row['actor']) if row['actor'] else {}
        state = _outcome_state(row['result'], row['action'])
        verdict = VERDICT.get(state, 'Needs a look')
        out.append({
            'ticket': row['ticket'], 'candidate': row['candidate'], 'action': row['action'],
            'state': state, 'verdict': verdict, 'tone': TONE.get(verdict, 'warn'),
            'reason': row['reason'] or '', 'why': short_reason(row['action'], row['reason']),
            # A full verdict names its thresholds and so always carries a colon. A reason
            # recovered from an old notice is only the phrase, and repeating it under
            # 'the full reason' would promise detail that was never kept.
            'detail': row['reason'] if ':' in (row['reason'] or '') else '',
            'by': actor.get('channel') or 'unknown', 'created': row['created'] or 0,
            'title': view.get('title') or '', 'note': view.get('note') or '',
            'path': saved.get('path') or ('knowledge/' + (view.get('target') or '')),
        })
    return out


def tally(store, window=7 * 86400, now=None):
    """The numbers that lead the page: the vault, the queue, and the last week."""
    now = time.time() if now is None else now
    with store.db() as db:
        ensure(db)
        tickets = dict(db.execute('SELECT state, COUNT(*) FROM approval_tickets '
                                  'GROUP BY state').fetchall())
        candidates = dict(db.execute('SELECT state, COUNT(*) FROM candidates '
                                     'GROUP BY state').fetchall())
        recent = dict(db.execute(
            "SELECT json_extract(result,'$.state') s, COUNT(*) FROM approval_actions "
            "WHERE state='done' AND created >= ? GROUP BY s", (now - window,)).fetchall())
        last = db.execute("SELECT MAX(created) FROM approval_actions "
                          "WHERE state='done'").fetchone()[0]
    return {'published': tickets.get('published', 0),
            'waiting': tickets.get('ready', 0),
            'queued': candidates.get('pending', 0),
            'held': sum(candidates.get(state, 0) for state in
                        ('held_stale', 'held_invalid', 'held_interrupted', 'uncertain')),
            'week': {key: recent.get(key, 0) for key in
                     ('published', 'revised', 'rejected') if recent.get(key)},
            'decided_this_week': sum(recent.values()),
            'last_decision': last}


def digest_key(keys):
    """Stable over the exact set of decisions announced, so a retry never sends twice."""
    material = '\n'.join(sorted(keys)).encode()
    return 'jev-digest:' + hashlib.sha256(material).hexdigest()
