"""Duplicate comparison by search.

The whole point of this module is a guarantee: a candidate that is already covered must
not reach the vault a second time. Sending every note made that guarantee trivially true
and did not scale. These tests are about the ways asking a search engine instead could
quietly weaken it - an index that is a minute behind, a search that is down, a byte cap
that drops the wrong note - and pin that none of them answer "no duplicates".
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from reviewer import dedup


class FakeSearch:
    """Stands in for one Khoj GET. Returns hits in the shape the real endpoint does."""

    def __init__(self, hits, fail=False):
        self.hits, self.fail, self.queries = hits, fail, []

    def __call__(self, settings, query):
        self.queries.append((query, settings['neighbours']))
        if self.fail:
            raise OSError('connection refused')
        return self.hits.get(query, [])[:settings['neighbours']]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        (self.vault / 'Lessons').mkdir(parents=True)
        self.manifest = self.root / 'sync-manifest.json'
        self.written = {}

    def note(self, name, text):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.written[name] = text
        return name

    def index(self, *names, indexed=True):
        files = {n: {'digest': hashlib.sha256(self.written[n].encode()).hexdigest(),
                     'indexed': indexed} for n in names}
        self.manifest.write_text(json.dumps({'files': files, 'root': str(self.vault), 'version': 1}))

    def config(self, **dedup_settings):
        settings = {'khoj_source': '/unused', 'manifest': str(self.manifest), 'namespace': 'knowledge'}
        settings.update(dedup_settings)
        return {'vault': str(self.vault), 'dedup': settings}

    def wire(self, hits, fail=False):
        self.retrieval = FakeSearch(hits, fail)
        original = dedup.search_hits
        dedup.search_hits = self.retrieval
        self.addCleanup(lambda: setattr(dedup, 'search_hits', original))

    def candidate(self, title, claim=''):
        return {'id': 'c1', 'title': title, 'claim': claim}


class InventoryTests(Fixture):
    def test_every_note_is_listed_from_disk_whatever_the_index_says(self):
        """The listing is the set of notes that exist. An index a minute behind must never
        be able to make a note invisible to the reviewer."""
        self.note('Lessons/A.md', '# Alpha\nbody\n')
        self.note('Lessons/B.md', '# Beta\nbody\n')
        self.index('Lessons/A.md')  # B is not in the manifest at all
        self.wire({})
        result = dedup.context(self.config(), [self.candidate('anything')])
        self.assertEqual([n['path'] for n in result['listing']], ['Lessons/A.md', 'Lessons/B.md'])
        self.assertEqual([n['title'] for n in result['listing']], ['Alpha', 'Beta'])

    def test_a_title_falls_back_to_the_path_when_the_note_has_none(self):
        self.note('Lessons/Bare.md', '')
        self.index()
        self.wire({})
        listing = dedup.context(self.config(), [self.candidate('x')])['listing']
        self.assertEqual(listing[0]['title'], 'Lessons/Bare.md')

    def test_a_symlink_out_of_the_vault_is_refused(self):
        outside = self.root / 'outside.md'
        outside.write_text('# Elsewhere\n')
        (self.vault / 'Link.md').symlink_to(outside)
        with self.assertRaises(ValueError):
            dedup.notes_on_disk(self.vault)

    def test_a_missing_vault_is_not_an_empty_vault(self):
        with self.assertRaises(ValueError):
            dedup.notes_on_disk(self.root / 'nope')


class UnindexedTests(Fixture):
    def test_a_note_search_cannot_see_is_supplied_in_full(self):
        """This is the staleness hole. Khoj indexes on a timer, so a note published a
        moment ago is invisible to search; omitting it would let its duplicate through."""
        self.note('Lessons/Indexed.md', '# Indexed\nold\n')
        self.note('Lessons/Fresh.md', '# Fresh\njust published\n')
        self.index('Lessons/Indexed.md')
        self.wire({})  # search returns nothing at all
        result = dedup.context(self.config(), [self.candidate('unrelated')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/Fresh.md'])

    def test_a_note_edited_since_indexing_counts_as_unindexed(self):
        self.note('Lessons/A.md', '# A\noriginal\n')
        self.index('Lessons/A.md')
        (self.vault / 'Lessons/A.md').write_text('# A\nedited since the index saw it\n')
        self.wire({})
        result = dedup.context(self.config(), [self.candidate('x')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md'])

    def test_no_manifest_treats_every_note_as_unseen(self):
        """Absent evidence of indexing is not evidence of indexing."""
        self.note('Lessons/A.md', '# A\n')
        self.note('Lessons/B.md', '# B\n')
        self.wire({})
        result = dedup.context(self.config(manifest=str(self.root / 'absent.json')),
                               [self.candidate('x')])
        self.assertEqual(sorted(n['path'] for n in result['notes']),
                         ['Lessons/A.md', 'Lessons/B.md'])

    def test_an_unreadable_manifest_is_treated_the_same_way(self):
        self.note('Lessons/A.md', '# A\n')
        self.manifest.write_text('{ not json')
        self.wire({})
        result = dedup.context(self.config(), [self.candidate('x')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md'])


class SearchTests(Fixture):
    def setUp(self):
        super().setUp()
        for name in 'ABCDE':
            self.note(f'Lessons/{name}.md', f'# {name}\ntext for {name}\n')
        self.index(*[f'Lessons/{n}.md' for n in 'ABCDE'])

    def hit(self, name, distance):
        return {'path': f'knowledge/Lessons/{name}.md', 'heading': None,
                'excerpt': '', 'distance': distance, 'rerank_score': None, 'truncated': False}

    def test_only_the_neighbours_of_a_candidate_are_supplied(self):
        self.wire({'Spool. how spools work': [self.hit('B', 0.04), self.hit('D', 0.11)]})
        result = dedup.context(self.config(), [self.candidate('Spool', 'how spools work')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/B.md', 'Lessons/D.md'])
        self.assertEqual(len(result['listing']), 5, 'all five still listed')

    def test_the_namespace_prefix_is_stripped_to_a_vault_relative_path(self):
        self.wire({'T.': [self.hit('A', 0.05)]})
        result = dedup.context(self.config(), [self.candidate('T', '')])
        self.assertEqual(result['notes'][0]['path'], 'Lessons/A.md')

    def test_a_hit_that_is_no_longer_on_disk_is_ignored(self):
        self.wire({'T.': [{'path': 'knowledge/Lessons/Deleted.md', 'distance': 0.01,
                            'heading': None, 'excerpt': '', 'rerank_score': None, 'truncated': False}]})
        self.assertEqual(dedup.context(self.config(), [self.candidate('T', '')])['notes'], [])

    def test_neighbours_of_every_candidate_are_pooled_and_deduplicated(self):
        self.wire({'One.': [self.hit('A', 0.05), self.hit('B', 0.09)],
                   'Two.': [self.hit('B', 0.02), self.hit('C', 0.07)]})
        result = dedup.context(self.config(), [self.candidate('One', ''), self.candidate('Two', '')])
        paths = [n['path'] for n in result['notes']]
        self.assertEqual(len(paths), len(set(paths)), 'a shared neighbour is sent once')
        self.assertEqual(sorted(paths), ['Lessons/A.md', 'Lessons/B.md', 'Lessons/C.md'])

    def test_a_cap_drops_the_least_likely_duplicate_not_an_arbitrary_one(self):
        """Ordering by distance is what makes truncation safe: the notes a candidate is
        most likely to duplicate survive the cut."""
        self.wire({'T.': [self.hit('E', 0.30), self.hit('A', 0.02), self.hit('C', 0.15)]})
        result = dedup.context(self.config(max_notes=2), [self.candidate('T', '')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md', 'Lessons/C.md'])

    def test_the_byte_cap_always_supplies_at_least_one_note(self):
        self.wire({'T.': [self.hit('A', 0.02), self.hit('B', 0.03)]})
        result = dedup.context(self.config(max_bytes=1), [self.candidate('T', '')])
        self.assertEqual(len(result['notes']), 1)

    def test_a_candidate_with_no_searchable_text_is_skipped_not_guessed_at(self):
        self.wire({})
        dedup.context(self.config(), [{'id': 'c', 'title': '', 'claim': ''}])
        self.assertEqual(self.retrieval.queries, [])

    def test_why_useful_is_used_when_a_candidate_has_no_claim(self):
        self.wire({'T. because it recurs': [self.hit('A', 0.05)]})
        result = dedup.context(self.config(), [{'id': 'c', 'title': 'T',
                                               'why_useful': 'because it recurs'}])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md'])

    def test_a_transport_failure_surfaces_as_an_outage_not_a_crash(self):
        self.wire({}, fail=True)
        with self.assertRaises(dedup.SearchUnavailable):
            dedup.context(self.config(), [self.candidate('T')])


class FailClosedTests(Fixture):
    def test_a_search_outage_is_never_reported_as_no_duplicates(self):
        self.note('Lessons/A.md', '# A\n')
        self.index('Lessons/A.md')
        self.wire({}, fail=True)
        with self.assertRaises(dedup.SearchUnavailable):
            dedup.context(self.config(), [self.candidate('T')])

    def test_an_unconfigured_endpoint_is_an_outage_not_an_empty_vault(self):
        self.note('Lessons/A.md', '# A\n')
        self.index('Lessons/A.md')
        config = {'vault': str(self.vault), 'dedup': {'manifest': str(self.manifest),
                                                     'khoj_url': ''}}
        with self.assertRaises(dedup.SearchUnavailable):
            dedup.context(config, [self.candidate('T')])


class WholeVaultFallbackTests(Fixture):
    def test_without_a_dedup_section_the_whole_vault_is_still_sent(self):
        self.note('Lessons/A.md', '# A\nbody\n')
        self.note('Lessons/B.md', '# B\nbody\n')
        result = dedup.for_review({'vault': str(self.vault)}, [self.candidate('T')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md', 'Lessons/B.md'])

    def test_the_old_ceiling_still_pauses_rather_than_comparing_against_part(self):
        self.note('Lessons/Huge.md', 'a' * 130_000)
        with self.assertRaises(ValueError):
            dedup.for_review({'vault': str(self.vault)}, [self.candidate('T')])

    def test_a_configured_dedup_section_selects_search(self):
        self.note('Lessons/A.md', '# A\n')
        self.note('Lessons/B.md', '# B\n')
        self.index('Lessons/A.md', 'Lessons/B.md')
        self.wire({'T.': [{'path': 'knowledge/Lessons/A.md', 'distance': 0.05, 'heading': None,
                            'excerpt': '', 'rerank_score': None, 'truncated': False}]})
        result = dedup.for_review(self.config(), [self.candidate('T', '')])
        self.assertEqual([n['path'] for n in result['notes']], ['Lessons/A.md'])


class FingerprintTests(Fixture):
    def test_fingerprints_cover_every_note_and_change_with_its_text(self):
        self.note('Lessons/A.md', '# A\noriginal\n')
        before = dedup.fingerprints(dedup.notes_on_disk(self.vault))
        (self.vault / 'Lessons/A.md').write_text('# A\nedited\n')
        after = dedup.fingerprints(dedup.notes_on_disk(self.vault))
        self.assertEqual(set(before), {'Lessons/A.md'})
        self.assertNotEqual(before, after)

    def test_a_note_added_anywhere_changes_the_fingerprints(self):
        self.note('Lessons/A.md', '# A\n')
        before = dedup.fingerprints(dedup.notes_on_disk(self.vault))
        self.note('Lessons/New.md', '# New\n')
        self.assertNotEqual(before, dedup.fingerprints(dedup.notes_on_disk(self.vault)))


if __name__ == '__main__':
    unittest.main()


class SearchHitsTests(unittest.TestCase):
    """The request itself, now that it is hand-rolled on urllib instead of a client
    library. A malformed payload must be an outage, never an empty result set."""

    def respond(self, payload, capture=None):
        import io, json as js, urllib.request
        from contextlib import contextmanager

        @contextmanager
        def urlopen(url, timeout=None):
            if capture is not None:
                capture.append(url)
            yield io.BytesIO(js.dumps(payload).encode())

        original = urllib.request.urlopen
        urllib.request.urlopen = urlopen
        self.addCleanup(lambda: setattr(urllib.request, 'urlopen', original))

    def settings(self):
        return dict(dedup.DEFAULTS)

    def test_a_hit_becomes_a_path_and_a_distance(self):
        self.respond([{'additional': {'file': 'knowledge/Lessons/A.md'}, 'score': 0.07,
                       'entry': 'x', 'corpus-id': '1'}])
        self.assertEqual(dedup.search_hits(self.settings(), 'q'),
                         [{'path': 'knowledge/Lessons/A.md', 'distance': 0.07}])

    def test_the_query_and_limit_reach_the_endpoint(self):
        seen = []
        self.respond([], seen)
        dedup.search_hits(dict(self.settings(), neighbours=3), 'polling ownership')
        self.assertIn('q=polling+ownership', seen[0])
        self.assertIn('n=3', seen[0])
        self.assertTrue(seen[0].startswith('http://127.0.0.1:42111/api/search?'))

    def test_a_payload_that_is_not_a_list_is_refused(self):
        self.respond({'error': 'nope'})
        with self.assertRaises(ValueError):
            dedup.search_hits(self.settings(), 'q')

    def test_a_hit_without_a_usable_path_or_score_is_refused(self):
        for payload in ([{'additional': {}, 'score': 0.1}],
                        [{'additional': {'file': 'a.md'}}],
                        [{'additional': {'file': 'a.md'}, 'score': True}],
                        [None]):
            self.respond(payload)
            with self.assertRaises(ValueError):
                dedup.search_hits(self.settings(), 'q')
