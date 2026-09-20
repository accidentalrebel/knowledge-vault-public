"""Autonomous approval channel: Jev issues the decision a person used to issue.

This is a third caller of the same channel-neutral `ApprovalDesk.act` the dashboard uses,
not a separate publication path. Going through `act` keeps the existing idempotency
(an interrupted action replays rather than acting twice) and records who decided, so
`approval_actions` shows `jev` rather than a human login.

The caller is expected to hold the worker lock; `telegram_runner.tick` already does.
"""
import hashlib

from . import jev, outcomes

ACTOR = {'channel': 'jev', 'login': 'jev-auto'}


def action_key(number, result, generation=0):
    """Stable per (ticket, decision, content, recovery generation).

    Derived from the scored content rather than the clock, so a retry of the same
    decision replays its recorded outcome instead of acting a second time.
    """
    material = '%s:%s:%s:%s' % (number, result.action, result.content_hash, generation)
    return 'jev:' + hashlib.sha256(material.encode()).hexdigest()


def _why_useful(store, candidate_id):
    try:
        return (store.get(candidate_id).get('candidate') or {}).get('why_useful', '')
    except ValueError:
        return ''


# One message per run, not one per note. The phone is a doorbell: it says how many
# decisions landed and where to read them. Everything a reader might want next - the
# titles, the reasons, the exact text - is on the page the link opens, formatted for
# reading rather than squeezed into a notification.
COUNTED = (('published', 'published'), ('revised', 'sent back'), ('rejected', 'dropped'))


def _digest_text(_config, decided):
    tallies = {}
    for item in decided:
        tallies[item['state']] = tallies.get(item['state'], 0) + 1
    parts = ['%s %s' % (tallies[state], word) for state, word in COUNTED if tallies.get(state)]
    other = sum(count for state, count in tallies.items()
                if state not in {state for state, _ in COUNTED})
    if other:
        parts.append('%s needing a look' % other)
    headline = 'Knowledge vault \u00b7 %s decided' % len(decided)
    body = ', '.join(parts) if parts else 'no change'
    # The link is appended to every outgoing message when it is sent, not here.
    return '%s\n%s' % (headline, body)


def tick(desk, config, service=None, ledger=None):
    """Decide every ready ticket. Returns a summary; raises nothing for one bad ticket."""
    if not jev.enabled(config):
        return {'state': 'disabled'}
    policy = jev.policy_from_config(config)
    ledger = ledger or jev.Ledger(store=desk.store)
    decided, deferred, keys = [], [], []
    for ticket in desk.pending_tickets():
        number = ticket['id']
        snapshot = ticket['snapshot']
        try:
            result = jev.gate(snapshot, _why_useful(desk.store, ticket['candidate']),
                              ledger, ticket['candidate'], policy, service=service)
        except jev.JevUnavailable as exc:
            # No verdict was produced. Leaving the ticket ready is the fail-closed
            # outcome: the next run retries, and nothing publishes on an outage.
            deferred.append({'ticket': number, 'reason': str(exc)})
            continue
        key = action_key(number, result, desk.action_generation(number))
        record = desk.act(number, result.action, result.instructions, key, ACTOR)
        # The reason exists only here. Store it before announcing, so the page the
        # message points at can always explain a decision the message only counts.
        outcomes.record(desk.store, key, number, ticket['candidate'], result.action, result.reason)
        keys.append(key)
        decided.append({'ticket': number, 'action': result.action, 'reason': result.reason,
                        'state': (record or {}).get('state')
                                 or outcomes.ACTION_STATE[result.action]})
    if decided:
        desk._notify(outcomes.digest_key(keys), _digest_text(config, decided))
    return {'state': 'decided', 'decided': decided, 'deferred': deferred}
