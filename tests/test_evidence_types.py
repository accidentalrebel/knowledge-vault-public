"""Evidence file types a candidate may cite.

Three of six backfill agents hit the original allowlist on .sh, .mjs and .tsv. None of
them gave up: each re-grounded the same claim in README prose instead of the code that
actually established it, which weakens precisely the factual support the gate scores.
"""
import tempfile
import unittest
from pathlib import Path

from reviewer.store import EVIDENCE_SUFFIXES, snapshot


class EvidenceTypeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def cite(self, name):
        path = self.root / name
        path.write_text('alpha\nbravo\ncharlie\n')
        return snapshot({'path': str(path), 'start': 1, 'end': 2}, [str(self.root)])

    def test_shell_scripts_are_citable(self):
        self.assertIn('alpha', self.cite('deploy.sh')['text'])

    def test_ecmascript_module_is_citable(self):
        self.assertIn('alpha', self.cite('lint.mjs')['text'])

    def test_tabular_data_is_citable(self):
        self.assertIn('alpha', self.cite('decisions.tsv')['text'])

    def test_systemd_unit_is_citable(self):
        self.assertIn('alpha', self.cite('worker.service')['text'])

    def test_binary_and_unknown_types_are_still_refused(self):
        for name in ('image.png', 'archive.tar', 'blob.bin', 'notes.docx'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.cite(name)

    def test_allowlist_covers_every_language_in_the_source_roots(self):
        for suffix in ('.py', '.sh', '.js', '.mjs', '.ts', '.go', '.rs', '.md', '.toml'):
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, EVIDENCE_SUFFIXES)


if __name__ == '__main__':
    unittest.main()
