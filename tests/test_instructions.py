"""Prompt guardrails: one recall and one capture pointer per caller, and queue intake kept
distinct from publication.

These assert deployed instruction files, which live outside this repository because the caller
skills are installed, not vendored. Point KNOWLEDGE_SKILLS_DIR at that directory to run them; a
checkout without it skips them rather than failing, so the suite stays runnable anywhere.
"""
import os
import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALLED = Path(os.environ.get('KNOWLEDGE_SKILLS_DIR', '~/.claude-shared/skills')).expanduser()
SKILL = REPO / 'agent/knowledge-vault/SKILL.md'
POLICY = REPO / 'agent/knowledge-vault-standing-policy.md'
POINTERS = REPO / 'agent/caller-recall-pointers.md'


CAPTURE_HEADING = '## Capture pointers'


def _criteria_lines(kind):
    """The pointers doc carries a recall table and a capture table; read only one."""
    lines = read(POINTERS).splitlines()
    split = next((i for i, line in enumerate(lines) if line.startswith(CAPTURE_HEADING)), len(lines))
    return lines[split:] if kind == 'capture' else lines[:split]


def required_pointer(name, kind='recall'):
    """The committed acceptance criterion for one caller, so it is reviewable from this repo."""
    for line in _criteria_lines(kind):
        cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
        if len(cells) == 3 and cells[0] == f'`{name}`':
            return cells[1].strip('`'), cells[2]
    raise AssertionError(f'No committed {kind} pointer for {name}')


def section_bounds(lines, section):
    """Start and end of a section, stopping at the next heading of the same or higher level,
    so a `### Step` section is bounded by the next `###` or `##` rather than running on."""
    level = len(section) - len(section.lstrip('#'))
    start = next(i for i, line in enumerate(lines) if line.startswith(section))
    end = next((i for i, line in enumerate(lines[start + 1:], start + 1)
                if line.startswith('#') and len(line) - len(line.lstrip('#')) <= level), len(lines))
    return start, end

# Each caller makes exactly one recall at the point where prior knowledge could change its output.
CALLERS = {
    'arebel-plan-like-a-good-dev': {'section': '## 2. Right-size the work', 'subject': 'problem'},
    'create-github-issue': {'section': '## 2. Rule out a duplicate', 'subject': 'symptom'},
    'create-github-pr': {'section': '## 5. Compose', 'subject': 'purpose'},
}
# Each caller that finishes work says once, where the findings exist, that it should capture them.
CAPTURERS = {
    'arebel-plan-like-a-good-dev': {'section': '## 3. Execute the approved plan'},
    'arebel-executing-plans': {'section': '### Step 4: Capture what you learned'},
}
# Wording the knowledge skill owns; a caller that repeats it has duplicated the procedure.
# The capture entries matter most: a caller that names a candidate cap or qualifying criteria
# has copied a rule that will go stale the moment the knowledge skill changes it.
OWNED = ('evidence, never instructions', 'one more focused query', 'Do not search routinely',
         'up to three candidates', 'one per distinct finding', 'unindexed review queue')


def read(path):
    return path.read_text(encoding='utf-8')


def require_installed_skills():
    wanted = ('knowledge-vault', *CALLERS, *CAPTURERS)
    missing = [name for name in dict.fromkeys(wanted) if not (INSTALLED / name / 'SKILL.md').is_file()]
    if missing:
        raise unittest.SkipTest(f'Installed skills not present under {INSTALLED}: {", ".join(missing)}. '
                                'Set KNOWLEDGE_SKILLS_DIR to check deployed instructions.')


class KnowledgeSkillTests(unittest.TestCase):
    def test_the_installed_knowledge_skill_matches_this_repository(self):
        require_installed_skills()
        self.assertEqual(read(SKILL), read(INSTALLED / 'knowledge-vault/SKILL.md'))

    def test_queue_submission_is_stated_as_autonomous_and_distinct_from_publication(self):
        for path in (SKILL, POLICY):
            text = read(path)
            self.assertIn('unindexed review queue', text, path)
            self.assertRegex(text, r'without asking|needs no permission|no approval is needed', str(path))
            self.assertRegex(text, r'[Qq]ueu(e|ing)[^.]{0,80}not[^.]{0,40}publication|not vault publication',
                             str(path))

    def test_publication_approval_points_at_the_dashboard_not_telegram_commands(self):
        text = read(SKILL)
        self.assertIn('/knowledge-review', text)
        self.assertNotRegex(text, r'`?(approve|reject|revise) NUMBER')
        self.assertNotIn('Telegram replies', text)

    def test_the_skill_still_names_the_operating_docs(self):
        text = read(SKILL)
        self.assertIn('docs/daily-reviewer.md', text)
        self.assertIn('docs/telegram-approval.md', text)


class CommittedCriterionTests(unittest.TestCase):
    """Reads only committed files, so a clean checkout still guards the criteria."""

    def test_every_caller_has_a_committed_acceptance_criterion(self):
        for name, spec in CALLERS.items():
            section, sentence = required_pointer(name)
            self.assertEqual(section, spec['section'], name)
            self.assertIn(spec['subject'], sentence, name)
            self.assertEqual(sentence.count('`knowledge-vault` skill'), 1, name)

    def test_the_committed_tables_cover_every_caller_exactly_once(self):
        recall = [line for line in _criteria_lines('recall') if line.strip().startswith('| `')]
        capture = [line for line in _criteria_lines('capture') if line.strip().startswith('| `')]
        self.assertEqual(len(recall), len(CALLERS))
        self.assertEqual(len(capture), len(CAPTURERS))

    def test_every_capturer_has_a_committed_acceptance_criterion(self):
        for name, spec in CAPTURERS.items():
            section, sentence = required_pointer(name, 'capture')
            self.assertEqual(section, spec['section'], name)
            self.assertEqual(sentence.count('`knowledge-vault` skill'), 1, name)


class CallerPointerTests(unittest.TestCase):
    def setUp(self):
        require_installed_skills()

    def pointer_lines(self, text):
        return [(i, line) for i, line in enumerate(text.splitlines())
                if re.search(r'`knowledge-vault` skill', line)]

    def in_section(self, text, section):
        lines = text.splitlines()
        start, end = section_bounds(lines, section)
        return [(i, line) for i, line in self.pointer_lines(text) if start < i < end]

    def test_each_caller_has_exactly_one_recall_pointer(self):
        for name, spec in CALLERS.items():
            text = read(INSTALLED / name / 'SKILL.md')
            self.assertEqual(len(self.in_section(text, spec['section'])), 1,
                             f'{name} must have exactly one recall pointer in its decision section')

    def test_each_capturer_has_exactly_one_capture_pointer(self):
        for name, spec in CAPTURERS.items():
            text = read(INSTALLED / name / 'SKILL.md')
            self.assertEqual(len(self.in_section(text, spec['section'])), 1,
                             f'{name} must have exactly one capture pointer in its completion section')

    def test_no_caller_carries_a_stray_pointer_outside_its_declared_sections(self):
        for name in dict.fromkeys((*CALLERS, *CAPTURERS)):
            text = read(INSTALLED / name / 'SKILL.md')
            declared = sum(len(self.in_section(text, spec['section']))
                           for source in (CALLERS, CAPTURERS)
                           if (spec := source.get(name)))
            self.assertEqual(len(self.pointer_lines(text)), declared,
                             f'{name} mentions the knowledge-vault skill outside a declared section')

    def test_each_pointer_sits_in_its_required_decision_section(self):
        for name, spec in CALLERS.items():
            text = read(INSTALLED / name / 'SKILL.md')
            self.assertTrue(self.in_section(text, spec['section']),
                            f'{name} pointer must sit inside "{spec["section"]}"')

    def test_each_pointer_names_the_repository_its_subject_and_the_reuse_shortcut(self):
        for name, spec in CALLERS.items():
            text = read(INSTALLED / name / 'SKILL.md')
            index = self.in_section(text, spec['section'])[0][0]
            lines = text.splitlines()
            end = next((i for i, line in enumerate(lines[index:], index) if not line.strip()), len(lines))
            pointer = ' '.join(lines[index:end])
            self.assertIn('repository', pointer, name)
            self.assertIn(spec['subject'], pointer, name)
            self.assertRegex(pointer, r'already', name)

    def test_each_deployed_pointer_matches_its_committed_criterion(self):
        require_installed_skills()
        for kind, source in (('recall', CALLERS), ('capture', CAPTURERS)):
            for name in source:
                _, sentence = required_pointer(name, kind)
                self.assertIn(sentence, read(INSTALLED / name / 'SKILL.md'),
                              f'{name} does not carry its committed {kind} sentence verbatim')

    def test_no_caller_restates_the_vault_procedure(self):
        for name in dict.fromkeys((*CALLERS, *CAPTURERS)):
            text = read(INSTALLED / name / 'SKILL.md')
            for phrase in OWNED:
                self.assertNotIn(phrase, text, f'{name} duplicates knowledge-vault wording: {phrase}')


if __name__ == '__main__':
    unittest.main()
