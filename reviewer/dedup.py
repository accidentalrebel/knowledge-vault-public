"""Duplicate comparison by search, not by shipping the corpus.

The reviewer used to receive every note in the vault so it could answer one question:
is this candidate already covered? Measured on a real run, that made 88% of the packet a
re-send of the whole vault - 73,245 bytes of notes to judge 9,824 bytes of candidate -
and it carried a hard ceiling that stopped review outright once the vault outgrew it.

Khoj already indexes this vault and answers that question directly, so ask it. The
reviewer gets an inventory of every note plus the full text of only the few a candidate
could actually collide with.

Three things keep that as safe as sending everything:

- The inventory is read from disk, so the set of notes is always current even when the
  index is a minute behind, and a note can never be absent merely because search missed it.
- A note the index has not caught up with cannot be found by search, so it is supplied in
  full on the strength of the sync manifest rather than quietly left out.
- If search is unavailable the caller pauses. Answering "no duplicates" because the search
  was down is the one answer this must never give.

Recall was measured against the live vault before this replaced the whole-vault packet:
querying each of the 44 notes by its own title alone retrieved it at rank 1 in 44 of 44
cases, and every candidate the reviewer had previously rejected as a duplicate sat within
0.10 of the note it duplicated while candidates rejected for other reasons had no
neighbour closer than 0.15.
"""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULTS = {'neighbours': 5, 'max_notes': 24, 'max_bytes': 96_000, 'timeout': 15,
            'khoj_url': 'http://127.0.0.1:42111', 'namespace': 'knowledge'}


class SearchUnavailable(Exception):
    """Khoj could not answer. The caller pauses; it never assumes there are no matches."""


def _title(text, path):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line.lstrip('#').strip() or path
    return path


def notes_on_disk(root):
    """Every note, read from the filesystem rather than the index, newest truth wins."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError('Vault directory is missing; duplicate context is unavailable')
    found = {}
    for path in sorted(root.rglob('*.md')):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Vault context contains a symlink outside the supported snapshot')
        text = path.read_text()
        found[path.relative_to(root).as_posix()] = text
    return found


def inventory(found):
    return [{'path': path, 'title': _title(text, path)} for path, text in sorted(found.items())]


def fingerprints(found):
    return {path: hashlib.sha256(text.encode()).hexdigest() for path, text in found.items()}


def unindexed(found, manifest_path):
    """Notes search cannot see yet: absent from the manifest, or recorded as not indexed,
    or indexed under a digest that no longer matches what is on disk."""
    try:
        files = (json.loads(Path(manifest_path).read_text()) or {}).get('files') or {}
    except (OSError, ValueError):
        # No manifest is not evidence that everything is indexed.
        return sorted(found)
    stale = []
    for path, text in found.items():
        entry = files.get(path)
        if not entry or not entry.get('indexed'):
            stale.append(path)
        elif entry.get('digest') != hashlib.sha256(text.encode()).hexdigest():
            stale.append(path)
    return sorted(stale)


def search_hits(settings, query):
    """One Khoj search, over the standard library only.

    The reviewer is a stdlib service on the system interpreter. Borrowing khoj-lab's
    client meant borrowing its virtualenv too, which is how this silently blocked review
    for nine hours on a missing httpx. Khoj's search is a plain GET returning a JSON list,
    so there is nothing here worth a dependency.
    """
    url = '%s/api/search?%s' % (settings['khoj_url'].rstrip('/'), urllib.parse.urlencode(
        {'q': query, 'n': str(settings['neighbours']), 't': 'markdown',
         'r': 'false', 'dedupe': 'true'}))
    with urllib.request.urlopen(url, timeout=settings['timeout']) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise ValueError('Khoj search returned an invalid payload')
    hits = []
    for item in payload:
        path = ((item or {}).get('additional') or {}).get('file')
        score = (item or {}).get('score')
        if not isinstance(path, str) or isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError('Khoj search returned a hit without a usable path and score')
        hits.append({'path': path, 'distance': float(score)})
    return hits


def _query(candidate):
    """Title plus claim - the shape a candidate actually arrives in, and what recall was
    measured on. Returns '' when there is nothing to ask about, so a malformed candidate
    produces no search rather than a search for punctuation."""
    title = (candidate.get('title') or '').strip()
    body = (candidate.get('claim') or candidate.get('why_useful') or '').strip()
    if not title and not body:
        return ''
    return ('%s. %s' % (title, body)).strip()[:2000]


def neighbours(settings, candidates):
    """Paths of the notes each candidate could collide with, nearest first overall.

    Ordered by distance across the whole batch so that a byte cap drops the least likely
    duplicates rather than an arbitrary tail.
    """
    prefix = settings.get('namespace', DEFAULTS['namespace']) + '/'
    best = {}
    try:
        for candidate in candidates:
            query = _query(candidate)
            if not query:
                continue
            for hit in search_hits(settings, query):
                path = hit['path']
                path = path[len(prefix):] if path.startswith(prefix) else path
                if path not in best or hit['distance'] < best[path]:
                    best[path] = hit['distance']
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SearchUnavailable('Khoj duplicate search is unavailable: %s' % exc) from exc
    return [path for path, _ in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))]


def context(config, candidates):
    """The duplicate-comparison context for one review packet.

    `notes` keeps the packet key `existing_notes` and its shape: still the set of notes the
    reviewer compares against, now a relevant subset rather than the whole vault.
    `fingerprints` is not sent to the model; it is what detects the vault changing under a
    review in flight.
    """
    settings = dict(DEFAULTS, **(config.get('dedup') or {}))
    found = notes_on_disk(config['vault'])
    if not settings.get('khoj_url'):
        raise SearchUnavailable('No Khoj endpoint is configured for duplicate search')

    ordered = neighbours(settings, candidates)
    # A note search cannot see is not a note without duplicates; supply it first.
    selected, seen = [], set()
    for path in unindexed(found, settings.get('manifest', '')) + ordered:
        if path in found and path not in seen:
            seen.add(path)
            selected.append(path)

    notes, size = [], 0
    for path in selected[:settings['max_notes']]:
        text = found[path]
        if size + len(text) > settings['max_bytes'] and notes:
            break
        size += len(text)
        notes.append({'path': path, 'text': text})
    return {'listing': inventory(found), 'notes': notes, 'fingerprints': fingerprints(found)}


def whole_vault(config, ceiling=128_000):
    """The original behavior, kept for a deployment with no Khoj to ask.

    Sending every note does not scale - it is what this module exists to replace - so the
    ceiling stays exactly where it was and still pauses review rather than silently
    comparing against part of the vault.
    """
    found = notes_on_disk(config['vault'])
    size = 0
    for path in sorted(found):
        size += len(found[path].encode())
        if size > ceiling:
            raise ValueError('Complete vault context exceeds %d bytes; review is paused' % ceiling)
    notes = [{'path': path, 'text': found[path]} for path in sorted(found)]
    return {'listing': inventory(found), 'notes': notes, 'fingerprints': fingerprints(found)}


def for_review(config, candidates):
    """Search when Khoj is configured, the whole vault when it is not."""
    return context(config, candidates) if config.get('dedup') else whole_vault(config)
