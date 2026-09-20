"""Small editorial smoke test shared by every configured provider."""
import json
import time

from .editorial import InvalidReview, Unavailable, fingerprint, validate_review
from .worker import worker_lock


def examples():
    source = ('Mercury bridge design, verified 2026-09-15: one inbox daemon owns polling. '
              'Additional consumers must read the shared spool; concurrent pollers caused HTTP 409. '
              'This ownership constraint applies to every integration using this bridge.')
    cases = [
        ('useful', 'service', 'Mercury polling ownership',
         'Reuse the shared spool when adding a Mercury integration because a single daemon owns polling.',
         source),
        ('unsupported', 'lesson', 'First fixed upstream release',
         'Upstream release 9.9 is confirmed to fix the missing-document defect.',
         'The local test reproduced missing documents on release 1.42.10. No later upstream release was tested.'),
        ('duplicate', 'service', 'Cobalt bridge reuse',
         'Cobalt consumers read the shared spool instead of starting another poller.',
         'Cobalt architecture: one daemon polls; consumers read the shared spool.'),
        ('misleading', 'decision', 'Storage migration approved',
         'The user decided to migrate the storage service to PostgreSQL.',
         'Proposal dated 2026-09-14: consider PostgreSQL for storage. The user has not reviewed or approved this proposal.'),
        ('addition', 'service', 'Cobalt spool fan-out guarantee',
         'Each Cobalt consumer has an independent spool cursor, so one consumer cannot consume another consumer\'s events.',
         'Cobalt architecture, verified 2026-09-15: one daemon polls; consumers read the shared spool. '
         'Each consumer stores an independent read cursor. Reading does not remove events from the spool. '
         'This enables independent integrations without event stealing.'),
        ('conflict', 'decision', 'Quasar polling owner',
         'Service A is the current designated owner of Quasar polling.',
         'Two current records from the same responsible maintainer, both dated 2026-09-15, conflict: '
         'record one assigns Quasar polling to service A; record two assigns it to service B. '
         'Neither record supersedes the other and no tie-breaking authority is available.'),
    ]
    return {'candidates': [{'id': cid, 'category': category, 'title': title, 'claim': claim,
                            'why_useful': 'A future integration needs the established constraints.',
                            'sources': [{'id': 's1', 'path': f'qualification/{cid}.md',
                                         'start': 1, 'end': 1, 'text': text}]}
                           for cid, category, title, claim, text in cases],
            # The real packet names every note that exists and supplies the text of only
            # the relevant ones, so qualification must present that shape or it is
            # certifying a provider against a contract the reviewer does not use.
            'vault_listing': [{'path': 'Services/Cobalt.md', 'title': 'Cobalt'},
                              {'path': 'Services/Quasar.md', 'title': 'Quasar'},
                              {'path': 'Decisions/Storage engine.md', 'title': 'Storage engine'}],
            'existing_notes': [{'path': 'Services/Cobalt.md',
                                'text': '# Cobalt\nOne daemon polls; consumers read the shared spool.\n'
                                        'Source: Cobalt architecture, verified 2026-09-15.'}]}


def qualify(store, config, name, invoke):
    provider = next((p for p in config['providers'] if p['name'] == name), None)
    if provider is None:
        raise ValueError('Unknown provider')
    with worker_lock(store) as acquired:
        if not acquired:
            return {'state': 'busy', 'passed': False}
        now = time.time()
        if now < (store.get_meta('qualification_after:' + name) or 0):
            return {'state': 'daily_limit', 'passed': False}
        store.set_meta('qualification_after:' + name, now + 86400)
        result = {'passed': False, 'fingerprint': fingerprint(provider), 'checked_at': now,
                  'state': 'interrupted'}
        store.set_meta('qualified:' + name, result)
        aid, path = store.start_attempt(name, [])
        result['attempt'] = str(path)
        packet = examples()
        (path / 'packet.json').write_text(json.dumps(packet, indent=2))
        try:
            response = invoke(provider, packet, 180, path)
            decisions = validate_review(response, packet)
            expected = {'useful': {'accept'}, 'unsupported': {'reject', 'uncertain'},
                        'duplicate': {'reject'}, 'misleading': {'reject', 'uncertain'},
                        'addition': {'update'}, 'conflict': {'uncertain'}}
            actual = {d['id']: d['verdict'] for d in decisions}
            result['verdicts'] = actual
            result['passed'] = all(actual[cid] in allowed for cid, allowed in expected.items())
            result['state'] = 'qualified' if result['passed'] else 'failed_editorial_check'
            (path / 'decisions.json').write_text(json.dumps(response, indent=2))
        except Unavailable as exc:
            result.update(state='unavailable', reason=str(exc))
            store.set_meta('cooldown:' + name, now + 86400)
        except (InvalidReview, ValueError, TypeError, KeyError) as exc:
            result.update(state='invalid', reason=str(exc))
        store.set_meta('qualified:' + name, result)
        store.finish_attempt(aid, result['state'])
        (path / 'qualification.json').write_text(json.dumps(result, indent=2))
        return result
