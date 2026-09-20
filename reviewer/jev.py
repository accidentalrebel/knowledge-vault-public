"""Autonomous approval gate backed by TypeSafe's Jev (System One).

Jev judges the exact artifact that will be committed - the reviewer-narrowed proposed
note - against the evidence packet, and issues the approve/reject/revise action a human
used to issue from the dashboard.

The thresholds in DEFAULT_POLICY are a conservative starting recommendation, not values
validated against a labelled corpus; the queue held 8 candidates and no rejects when this
was written. They are deliberately biased toward false revise/reject over false accept,
and they live in config so production statistics can move them without a code change.

Ordering matters: the deterministic secret scan in `reviewer.secrets` runs over the whole
packet before any request leaves the machine, so a real credential in an evidence excerpt
is never sent to an external service.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections import namedtuple
from contextlib import contextmanager
from pathlib import Path

from . import secrets

ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
MODEL = 'jev-latest'
QUESTIONS_PATH = Path(__file__).with_name('jev_questions.json')

DEFAULT_POLICY = {
    'approve': {
        'factual_support': {'p4': 0.25, 'p3plus': 0.85},
        'durability': {'p3plus': 0.80},
        'reference_value': {'p3plus': 0.80},
    },
    # Any of these being met blocks approval. Level 1 is ordinary local provenance and
    # must not gate; level 4 is a directly usable secret.
    'sensitivity_block': {'p4': 0.01, 'p3plus': 0.05, 'p2plus': 0.35},
    'revise': {'durability': {'p3plus': 0.80}, 'reference_value': {'p3plus': 0.80}},
    'max_revisions': 2,
}


class JevUnavailable(RuntimeError):
    """The service could not be reached. The caller must not treat this as a verdict."""


def mass(probabilities, levels):
    """Probability mass on the given rubric levels. Routing uses mass, never the
    expected score, because two very different distributions share a mean."""
    return sum(float(probabilities.get(str(level), 0.0)) for level in levels)


def questions():
    with QUESTIONS_PATH.open() as handle:
        return json.load(handle)


def build_state(snapshot, why_useful=''):
    """The packet Jev judges: the exact note that would be committed, plus its evidence.

    `snapshot['note']` is the reviewer-narrowed body, which is what the human approved;
    scoring the original candidate instead would judge a claim nobody is publishing.
    Source digests are omitted - they are provenance, add no semantic evidence, and are
    high-entropy noise in the request.
    """
    return {
        'submission': {
            'title': snapshot.get('title', ''),
            'claim': snapshot.get('note', ''),
            'why_useful': why_useful or '',
        },
        'sources': [
            {key: source[key] for key in ('id', 'path', 'start', 'end') if key in source}
            | {'excerpt': source.get('text', '')}
            for source in snapshot.get('sources', [])
        ],
    }


def key_file():
    return Path(os.environ.get('TYPESAFE_KEY_FILE') or Path.home() / '.config' / 'typesafe.key')


def _key_from_file():
    """A systemd user service never sources the shell profile, so the environment export
    that serves interactive shells is invisible here. Read the same file .bashrc reads
    rather than depending on how the gate happened to be invoked."""
    try:
        return key_file().read_text().strip() or None
    except OSError:
        return None


def evaluate(state, api_key=None, timeout=30):
    """One request carrying all four questions; they are independent over one state."""
    key = api_key or os.environ.get('TYPESAFE_API_KEY') or _key_from_file()
    if not key:
        raise JevUnavailable('No TypeSafe credential in TYPESAFE_API_KEY or %s' % key_file())
    payload = {'state': state, 'model': MODEL, 'questions': questions()}
    request = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'},
        method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise JevUnavailable('HTTP %d from TypeSafe' % exc.code) from exc
    except Exception as exc:
        raise JevUnavailable('TypeSafe request failed: %s' % exc) from exc
    if 'answers' not in body:
        raise JevUnavailable('TypeSafe response carried no answers')
    return body


def _probs(answers, question):
    answer = answers.get(question) or {}
    return answer.get('probabilities') or {}


def decide(answers, revision_count, policy=None):
    """Return (action, reason, instructions).

    Fail-closed: anything that is not clearly approvable is revised while revision budget
    remains, and rejected once it is gone. Uncertainty costs an extra editorial pass or a
    missed note; it never causes an unsafe publication.
    """
    policy = policy or DEFAULT_POLICY
    budget_left = revision_count < policy['max_revisions']

    sensitive = _probs(answers, 'sensitive_information')
    block = policy['sensitivity_block']
    sensitivity_hits = [
        ('P(level 4)=%.3f' % mass(sensitive, (4,)), mass(sensitive, (4,)) >= block['p4']),
        ('P(levels 3-4)=%.3f' % mass(sensitive, (3, 4)), mass(sensitive, (3, 4)) >= block['p3plus']),
        ('P(levels 2-4)=%.3f' % mass(sensitive, (2, 3, 4)), mass(sensitive, (2, 3, 4)) >= block['p2plus']),
    ]
    triggered = [text for text, hit in sensitivity_hits if hit]
    if triggered:
        reason = 'Sensitivity gate: ' + ', '.join(triggered)
        if budget_left:
            return 'revise', reason, (
                'Remove or generalise the sensitive material before this note can be published. '
                'Keep only information that is safe in a personal knowledge vault: no internal '
                'operational detail, confidential data, or credential-bearing text. ' + reason)
        return 'reject', reason + '; revision budget exhausted', ''

    factual = _probs(answers, 'factual_support')
    durability = _probs(answers, 'durability')
    reference = _probs(answers, 'reference_value')
    wanted = policy['approve']

    checks = {
        'factual P(4)': (mass(factual, (4,)), wanted['factual_support']['p4']),
        'factual P(3-4)': (mass(factual, (3, 4)), wanted['factual_support']['p3plus']),
        'durability P(3-4)': (mass(durability, (3, 4)), wanted['durability']['p3plus']),
        'reference P(3-4)': (mass(reference, (3, 4)), wanted['reference_value']['p3plus']),
    }
    failed = {name: (got, need) for name, (got, need) in checks.items() if got < need}
    if not failed:
        return 'approve', 'All approval thresholds met: ' + ', '.join(
            '%s=%.3f' % (name, got) for name, (got, _) in checks.items()), ''

    summary = ', '.join('%s=%.3f (need %.2f)' % (name, got, need)
                        for name, (got, need) in sorted(failed.items()))
    salvageable = (mass(durability, (3, 4)) >= policy['revise']['durability']['p3plus']
                   and mass(reference, (3, 4)) >= policy['revise']['reference_value']['p3plus'])
    if salvageable and budget_left:
        return 'revise', 'Below approval thresholds: ' + summary, (
            'Narrow this note to exactly what the supplied evidence excerpts support. '
            'Remove or scope any assertion, measurement, version, or generalisation that the '
            'evidence does not establish, and keep the lesson that does survive. '
            'Failing checks: ' + summary)
    if not salvageable:
        return 'reject', 'Insufficient durability or reference value: ' + summary, ''
    return 'reject', 'Revision budget exhausted: ' + summary, ''


Result = namedtuple('Result', 'action reason instructions content_hash answers')


def policy_from_config(config):
    """Thresholds are operational settings, not code. Anything the config omits keeps
    its shipped default, so a partial block cannot silently disable a gate."""
    section = (config or {}).get('jev') or {}
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    for group in ('approve', 'revise'):
        for question, checks in (section.get(group) or {}).items():
            policy[group].setdefault(question, {}).update(checks)
    policy['sensitivity_block'].update(section.get('sensitivity_block') or {})
    if 'max_revisions' in section:
        policy['max_revisions'] = section['max_revisions']
    return policy


def enabled(config):
    return bool(((config or {}).get('jev') or {}).get('enabled'))


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def evaluation_key(state, model=MODEL):
    """Identity of one evaluation: the state, the frozen questions and the model.

    Jev is stochastic, so an unchanged note re-evaluated enough times would eventually
    draw a favourable score. Persisting one decision per key removes that path entirely;
    it buys operational consistency, not determinism or accuracy.
    """
    return hashlib.sha256(_canonical(
        {'state': state, 'questions': questions(), 'model': model}).encode()).hexdigest()


def content_hash(snapshot):
    """Binds an approval to the exact note and evidence that were scored, so a note
    edited after scoring - or evidence swapped beneath it - invalidates the approval."""
    return hashlib.sha256(_canonical({
        'note': snapshot.get('note', ''),
        'title': snapshot.get('title', ''),
        'sources': [{'id': s.get('id'), 'path': s.get('path'), 'text': s.get('text', '')}
                    for s in snapshot.get('sources', [])],
    }).encode()).hexdigest()


class Ledger:
    """Durable record of evaluations and revision budget, keyed by candidate.

    Accepts a live connection (tests) or a Store. With a Store each operation opens its
    own short transaction, so no transaction is ever held open across a network call.
    """

    SCHEMA = ('CREATE TABLE IF NOT EXISTS jev_evaluations ('
              'key TEXT PRIMARY KEY, candidate TEXT, response TEXT, created REAL)',
              'CREATE TABLE IF NOT EXISTS jev_revisions ('
              'id INTEGER PRIMARY KEY AUTOINCREMENT, candidate TEXT NOT NULL, '
              'content TEXT NOT NULL, created REAL)')

    def __init__(self, db=None, store=None):
        if db is None and store is None:
            raise ValueError('Ledger needs a connection or a store')
        self._db, self._store = db, store
        with self._conn() as conn:
            for statement in self.SCHEMA:
                conn.execute(statement)
            self._commit(conn)

    @contextmanager
    def _conn(self):
        if self._db is not None:
            yield self._db
        else:
            with self._store.db() as conn:
                yield conn

    def _commit(self, conn):
        if self._db is not None:
            conn.commit()

    def cached(self, key):
        with self._conn() as conn:
            row = conn.execute('SELECT response FROM jev_evaluations WHERE key=?', (key,)).fetchone()
        return json.loads(row['response']) if row else None

    def remember(self, key, candidate, response):
        with self._conn() as conn:
            conn.execute('INSERT OR REPLACE INTO jev_evaluations VALUES (?,?,?,?)',
                         (key, candidate, json.dumps(response), time.time()))
            self._commit(conn)

    def revisions(self, candidate):
        with self._conn() as conn:
            row = conn.execute('SELECT COUNT(*) n FROM jev_revisions WHERE candidate=?',
                               (candidate,)).fetchone()
        return row['n'] if row else 0

    def seen_content(self, candidate, content):
        with self._conn() as conn:
            row = conn.execute('SELECT 1 FROM jev_revisions WHERE candidate=? AND content=?',
                               (candidate, content)).fetchone()
        return row is not None

    def record_revision(self, candidate, content):
        with self._conn() as conn:
            conn.execute('INSERT INTO jev_revisions (candidate, content, created) VALUES (?,?,?)',
                         (candidate, content, time.time()))
            self._commit(conn)


def gate(snapshot, why_useful, ledger, candidate_id, policy=None, service=None, api_key=None):
    """The whole autonomous decision for one ticket.

    Order is deliberate: the deterministic scan runs before anything leaves the machine,
    so a credential in an evidence excerpt is never sent to an external service.
    """
    policy = policy or DEFAULT_POLICY
    service = service or evaluate
    state = build_state(snapshot, why_useful=why_useful)
    fingerprint = content_hash(snapshot)

    hits = secrets.scan_state(state)
    if hits:
        rules = sorted({hit['rule'] for hit in hits})
        return Result('reject', 'Deterministic secret scan matched: ' + ', '.join(rules),
                      '', fingerprint, None)

    if ledger.seen_content(candidate_id, fingerprint):
        return Result('reject', 'revision_no_progress: the reviser returned content already '
                      'scored for this candidate', '', fingerprint, None)

    key = evaluation_key(state)
    body = ledger.cached(key)
    if body is None:
        body = service(state, api_key=api_key)
        ledger.remember(key, candidate_id, body)

    answers = body['answers']
    action, reason, instructions = decide(answers, ledger.revisions(candidate_id), policy)
    if action == 'revise':
        ledger.record_revision(candidate_id, fingerprint)
    return Result(action, reason, instructions, fingerprint, answers)
