"""Transactional candidate state; nothing here writes the knowledge vault."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path



# Text source files an excerpt may be cited from. Omitting a shell or .mjs file does not
# stop an agent citing the repo - it makes it re-ground the same claim in weaker README
# prose, which is exactly the support the gate scores. Extensions stay an allowlist so a
# binary or an unexpected format is still refused.
EVIDENCE_SUFFIXES = {
    '.md', '.txt', '.rst', '.py', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx',
    '.rs', '.go', '.gd', '.sh', '.bash', '.zsh', '.rb', '.java', '.kt', '.c',
    '.h', '.cpp', '.hpp', '.cs', '.php', '.lua', '.sql', '.toml', '.yaml',
    '.yml', '.json', '.ini', '.cfg', '.conf', '.env_example', '.service',
    '.timer', '.tf', '.gradle', '.swift', '.m', '.pl', '.r', '.jl',
    '.tsv', '.csv', '.mk', '.gitignore', '.dockerfile', '.tmpl', '.xml',
}

def snapshot(ref, roots):
    path = Path(ref['path'])
    resolved = path.resolve(strict=True)
    if not path.is_absolute() or not any(resolved.is_relative_to(Path(r).resolve()) for r in roots):
        raise ValueError('Evidence must be an absolute path within a configured source root')
    if any(p.startswith('.') for p in path.parts[1:]) or any(p.startswith('.') for p in resolved.parts[1:]):
        raise ValueError('Hidden paths are not evidence sources')
    if resolved.suffix not in EVIDENCE_SUFFIXES:
        raise ValueError('Unsupported evidence file type')
    if resolved.stat().st_size > 2_000_000:
        raise ValueError('Evidence file exceeds 2 MB')
    raw = resolved.read_bytes()
    lines = raw.decode('utf-8').splitlines(keepends=True)
    start, end = ref.get('start'), ref.get('end')
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
        raise ValueError('Evidence line range is outside the file')
    excerpt = ''.join(lines[start - 1:end])
    if not excerpt.strip():
        raise ValueError('Evidence excerpt is blank')
    if len(excerpt.encode()) > 8000:
        raise ValueError('Evidence excerpt exceeds 8000 bytes')
    return {'path': str(resolved), 'start': start, 'end': end,
            'sha256': hashlib.sha256(raw).hexdigest(), 'text': excerpt}


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, task_key TEXT UNIQUE, fingerprint TEXT UNIQUE,
                    payload TEXT NOT NULL, state TEXT NOT NULL, decision TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, provider TEXT, state TEXT, path TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS review_reservations (
                    id TEXT PRIMARY KEY, created REAL NOT NULL,
                    units INTEGER NOT NULL CHECK(units BETWEEN 1 AND 3), candidates TEXT NOT NULL);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'queue.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get_meta(self, key):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def set_meta(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, json.dumps(value)))

    def submit(self, candidate, roots):
        fields = {'task_key', 'title', 'category', 'claim', 'why_useful', 'sources'}
        if not isinstance(candidate, dict) or set(candidate) != fields:
            raise ValueError('Candidate fields must be task_key, title, category, claim, why_useful, sources')
        for key, limit in [('task_key', 200), ('title', 200), ('claim', 2000), ('why_useful', 1000)]:
            if not isinstance(candidate[key], str) or not 1 <= len(candidate[key].strip()) <= limit:
                raise ValueError(f'{key} must be nonempty and at most {limit} characters')
        if candidate['category'] not in ('project', 'service', 'decision', 'lesson'):
            raise ValueError('Unknown candidate category')
        if not isinstance(candidate['sources'], list) or not 1 <= len(candidate['sources']) <= 3:
            raise ValueError('Supply one to three evidence references')
        refs = []
        for ref in candidate['sources']:
            if not isinstance(ref, dict) or set(ref) != {'path', 'start', 'end'}:
                raise ValueError('Source reference requires path, start, end')
            refs.append(snapshot(ref, roots))
        fingerprint = hashlib.sha256(json.dumps({k: v for k, v in candidate.items()
                                                 if k != 'task_key'}, sort_keys=True).encode()).hexdigest()
        payload = {'candidate': candidate, 'sources': [dict(r, id=f's{i+1}') for i, r in enumerate(refs)]}
        cid = uuid.uuid4().hex[:16]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM candidates WHERE task_key=? OR fingerprint=?',
                                  (candidate['task_key'], fingerprint)).fetchone()
            if existing:
                return {'id': existing['id'], 'deduplicated': True}
            if db.execute("SELECT COUNT(*) FROM candidates WHERE state='pending'").fetchone()[0] >= 100:
                raise ValueError('Inbox has 100 pending candidates; inspect its backlog before adding more')
            db.execute('INSERT INTO candidates VALUES (?, ?, ?, ?, ?, NULL, ?)',
                       (cid, candidate['task_key'], fingerprint, json.dumps(payload), 'pending', time.time()))
        return {'id': cid, 'deduplicated': False}

    def get(self, cid):
        with self.db() as db:
            row = db.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
        if not row:
            raise ValueError('Unknown candidate ID')
        return {'id': row['id'], **json.loads(row['payload']), 'state': row['state'],
                'decision': json.loads(row['decision']) if row['decision'] else None}

    def pending(self, limit=3):
        with self.db() as db:
            ids = db.execute("SELECT id FROM candidates WHERE state='pending' ORDER BY created LIMIT ?", (limit,)).fetchall()
        return [self.get(r['id']) for r in ids]

    def reopen(self, cid):
        """Put a finished candidate back in the queue for another review.

        Superseding its terminal tickets is the part that is easy to miss: the digest will
        not raise a new ticket while an earlier one sits in a state other than revised,
        stale or superseded, so a candidate reopened without this silently re-reviews,
        reaches 'accepted', and then waits forever for a ticket that never comes.
        """
        with self.db() as db:
            row = db.execute('SELECT state FROM candidates WHERE id=?', (cid,)).fetchone()
            if row is None:
                raise ValueError('Unknown candidate')
            if row['state'] == 'pending':
                return {'id': cid, 'state': 'pending', 'reopened': False, 'superseded': 0}
            has_tickets = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_tickets'").fetchone()
            superseded = db.execute("""UPDATE approval_tickets SET state='superseded'
                WHERE candidate=? AND state NOT IN ('revised','stale','superseded')""",
                (cid,)).rowcount if has_tickets else 0
            db.execute("UPDATE candidates SET state='pending',decision=NULL WHERE id=?", (cid,))
        return {'id': cid, 'state': 'pending', 'reopened': True,
                'previous_state': row['state'], 'superseded': superseded}

    def mark(self, ids, state, detail=None):
        with self.db() as db:
            db.executemany('UPDATE candidates SET state=?, decision=? WHERE id=?',
                           [(state, json.dumps(detail) if detail else None, cid) for cid in ids])

    def status(self):
        from .budget import status as budget_status
        with self.db() as db:
            candidates = dict(db.execute('SELECT state, COUNT(*) FROM candidates GROUP BY state').fetchall())
            attempts = dict(db.execute('SELECT state, COUNT(*) FROM attempts GROUP BY state').fetchall())
            items = [dict(r) for r in db.execute('SELECT id, task_key, state, created FROM candidates ORDER BY created')]
            meta = {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM meta')}
            has_tickets = db.execute("SELECT 1 FROM sqlite_master WHERE name='approval_tickets' AND type='table'").fetchone()
            approvals = dict(db.execute('SELECT state,COUNT(*) FROM approval_tickets GROUP BY state').fetchall()) if has_tickets else {}
        return {'candidates': candidates, 'items': items, 'attempts': attempts, 'metadata': meta,
                'review_budget': budget_status(self, time.time()), 'approvals': approvals,
                'publication': 'Exact-content human approval is required before a source write.'}

    def start_attempt(self, provider, ids):
        aid = uuid.uuid4().hex
        path = self.root / 'attempts' / aid
        path.mkdir(parents=True, mode=0o700)
        with self.db() as db:
            db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?, ?)',
                       (aid, provider, 'running', str(path), time.time()))
            db.executemany("UPDATE candidates SET state='reviewing' WHERE id=?", [(cid,) for cid in ids])
        return aid, path

    def finish_attempt(self, aid, state):
        with self.db() as db:
            db.execute('UPDATE attempts SET state=? WHERE id=?', (state, aid))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE candidates SET state='held_interrupted' WHERE state='reviewing'")
            db.execute("UPDATE attempts SET state='interrupted' WHERE state='running'")

    def complete(self, aid, decisions):
        states = {'accept': 'accepted', 'update': 'updated', 'reject': 'rejected', 'uncertain': 'uncertain'}
        with self.db() as db:
            for decision in decisions:
                db.execute('UPDATE candidates SET state=?, decision=? WHERE id=?',
                           (states[decision['verdict']], json.dumps(decision), decision['id']))
            db.execute("UPDATE attempts SET state='completed' WHERE id=?", (aid,))
