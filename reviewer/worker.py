"""One bounded hourly review; routing failures and editorial decisions stay separate."""
import fcntl
import json
import time
from contextlib import contextmanager

from . import dedup
from .editorial import InvalidReview, Unavailable, fingerprint, validate_review
from .store import snapshot
from . import budget


# Candidates judged in one call. Each decision is a full rewritten note, roughly 870
# output tokens, so the cap is generation time against the per-provider timeout below,
# not input size: 42 candidates measured ~28k tokens in and ~36k out. Six fits inside
# 180s with room to spare, and is the packet size every provider is already qualified on.
BATCH = 6


@contextmanager
def worker_lock(store):
    with (store.root / 'worker.lock').open('a') as lock:
        acquired = False
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                fcntl.flock(lock, fcntl.LOCK_UN)


def eligible(store, providers, now):
    selected = []
    for provider in providers:
        q = store.get_meta('qualified:' + provider['name']) or {}
        cooldown = store.get_meta('cooldown:' + provider['name']) or 0
        if (provider.get('enabled', False) and q.get('passed')
                and q.get('fingerprint') == fingerprint(provider) and cooldown <= now):
            selected.append(provider)
    return selected


# A paused reviewer announces nothing and publishes nothing, so without a recorded state
# it is indistinguishable from a quiet week. It cost nine hours of silence once.
BLOCK_KEY = 'review_blocked'


def note_block(store, detail):
    """Records why review is paused, keeping the time it first happened."""
    existing = store.get_meta(BLOCK_KEY) or {}
    if existing.get('detail') != detail:
        store.set_meta(BLOCK_KEY, {'detail': detail, 'since': time.time()})
    return {'state': 'context_blocked', 'detail': detail}


def clear_block(store):
    if store.get_meta(BLOCK_KEY):
        store.set_meta(BLOCK_KEY, None)


def hold_changed_packet(store, config, packet, aid, path, attempt_state='stale', marks=None):
    """A review is only as good as the vault it compared against.

    `marks` is the path-to-digest map taken when the packet was built. Comparing it to the
    vault now catches a note added, removed or edited while the model was thinking -
    including one the packet never carried, which a comparison of the supplied notes alone
    would miss.
    """
    try:
        if marks is not None and dedup.fingerprints(dedup.notes_on_disk(config['vault'])) != marks:
            raise ValueError('Vault changed during review; duplicate comparison is stale')
        for candidate in packet['candidates']:
            for source in candidate['sources']:
                ref = {k: source[k] for k in ('path', 'start', 'end')}
                if snapshot(ref, config['source_roots'])['sha256'] != source['sha256']:
                    raise ValueError('Source changed during review')
    except (OSError, ValueError) as exc:
        detail = {'reason': str(exc), 'attempt': str(path)}
        store.mark([c['id'] for c in packet['candidates']], 'held_stale', detail)
        store.finish_attempt(aid, attempt_state)
        return {'state': 'held_stale', **detail}
    return None



def retry_individually(store, config, provider, invoke, current, context, deadline, batch_failure):
    """One bad quotation in a batched review used to hold every candidate sharing the call.
    The validation is right - it is what stops a reviewer inventing evidence - so the batch
    is re-judged one candidate at a time and only the candidate that actually fails is held.

    This spends no extra reservation: the run already holds one, and the retry stays inside
    the same deadline.
    """
    reviewed, held = [], []
    for candidate in current:
        if time.monotonic() >= deadline:
            store.mark([candidate['id']], 'pending')
            continue
        packet = {'candidates': [candidate], 'vault_listing': context['listing'],
                  'existing_notes': context['notes']}
        aid, path = store.start_attempt(provider['name'], [candidate['id']])
        (path / 'packet.json').write_text(json.dumps(packet, indent=2))
        try:
            result = invoke(provider, packet, min(180, deadline - time.monotonic()), path)
            decisions = validate_review(result, packet)
        except Unavailable as exc:
            (path / 'failure.json').write_text(json.dumps({'type': 'unavailable', 'reason': str(exc)}))
            store.finish_attempt(aid, 'unavailable')
            store.mark([candidate['id']], 'pending')
            continue
        except (InvalidReview, ValueError, TypeError, KeyError) as exc:
            detail = {'reason': str(exc), 'attempt': str(path)}
            store.finish_attempt(aid, 'invalid')
            (path / 'failure.json').write_text(json.dumps(detail))
            store.mark([candidate['id']], 'held_invalid', detail)
            held.append(candidate['id'])
            continue
        (path / 'decisions.json').write_text(json.dumps(result, indent=2))
        if hold_changed_packet(store, config, packet, aid, path, marks=context['fingerprints']):
            continue
        store.complete(aid, decisions)
        reviewed.append(candidate['id'])
    return {'state': 'reviewed_individually', 'provider': provider['name'],
            'count': len(reviewed), 'held_invalid': len(held),
            'batch_failure': batch_failure['reason']}

def run(store, config, invoke):
    with worker_lock(store) as acquired:
        if not acquired:
            return {'state': 'busy'}
        store.recover()
        pacing = config.get('review') or {}
        candidates = store.pending(limit=pacing.get('batch', BATCH))
        if not candidates:
            return {'state': 'empty'}
        now = time.time()
        allowance = budget.status(store, now, pacing.get('interval_seconds'))
        if allowance['state'] != 'available':
            return allowance
        providers = eligible(store, config['providers'], now)
        if not providers:
            return {'state': 'no_provider', 'detail': 'Candidates remain queued; inspect qualification and cooldown status'}
        current = []
        for candidate in candidates:
            try:
                for source in candidate['sources']:
                    ref = {k: source[k] for k in ('path', 'start', 'end')}
                    if snapshot(ref, config['source_roots'])['sha256'] != source['sha256']:
                        raise ValueError('Source changed since submission')
                item = {'id': candidate['id'], **candidate['candidate'], 'sources': candidate['sources']}
                request = store.get_meta('revision:' + candidate['id'])
                if request:
                    item['revision_request'] = request
                current.append(item)
            except (OSError, ValueError) as exc:
                store.mark([candidate['id']], 'held_stale', {'reason': str(exc)})
        if not current:
            return {'state': 'held_stale'}
        try:
            context = dedup.for_review(config, current)
        except (OSError, ValueError, dedup.SearchUnavailable) as exc:
            return note_block(store, str(exc))
        clear_block(store)
        packet = {'candidates': current, 'vault_listing': context['listing'],
                  'existing_notes': context['notes']}
        ids = [c['id'] for c in current]
        reservation = budget.reserve(store, ids[0], time.time(), pacing.get('interval_seconds'))
        if reservation['state'] != 'reserved':
            return reservation
        deadline = time.monotonic() + 600
        for provider in providers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            aid, path = store.start_attempt(provider['name'], ids)
            (path / 'packet.json').write_text(json.dumps(packet, indent=2))
            try:
                result = invoke(provider, packet, min(180, remaining), path)
                decisions = validate_review(result, packet)
            except Unavailable as exc:
                (path / 'failure.json').write_text(json.dumps({'type': 'unavailable', 'reason': str(exc)}))
                store.set_meta('cooldown:' + provider['name'], time.time() + 86400)
                store.finish_attempt(aid, 'unavailable')
                stale = hold_changed_packet(store, config, packet, aid, path, 'unavailable_stale',
                                            marks=context['fingerprints'])
                if stale:
                    return stale
                store.mark(ids, 'pending')
                continue
            except (InvalidReview, ValueError, TypeError, KeyError) as exc:
                detail = {'reason': str(exc), 'attempt': str(path)}
                store.finish_attempt(aid, 'invalid')
                (path / 'failure.json').write_text(json.dumps(detail))
                if len(current) > 1:
                    return retry_individually(store, config, provider, invoke, current,
                                              context, deadline, detail)
                store.mark(ids, 'held_invalid', detail)
                return {'state': 'held_invalid', **detail}
            (path / 'decisions.json').write_text(json.dumps(result, indent=2))
            stale = hold_changed_packet(store, config, packet, aid, path,
                                        marks=context['fingerprints'])
            if stale:
                return stale
            store.complete(aid, decisions)
            return {'state': 'reviewed', 'provider': provider['name'], 'count': len(ids), 'attempt': str(path)}
        return {'state': 'waiting', 'detail': 'All eligible providers unavailable; queued within provider cooldowns and review allowance'}
