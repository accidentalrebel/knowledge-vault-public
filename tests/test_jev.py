"""Jev approval gate: state construction and the fail-closed routing policy.

Thresholds here are the recommended starting policy, not values validated against a
labelled corpus. They live in config; these tests pin the decision logic, not the numbers.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

from reviewer import jev
from reviewer.jev import DEFAULT_POLICY, build_state, decide, mass


def dist(**levels):
    probs = {str(i): 0.0 for i in range(5)}
    probs.update({str(k): v for k, v in levels.items()})
    return probs


def answers(factual, durability, reference, sensitive=None):
    sensitive = sensitive if sensitive is not None else dist(**{'0': 1.0})
    return {
        'factual_support': {'type': 'score', 'probabilities': factual},
        'durability': {'type': 'score', 'probabilities': durability},
        'reference_value': {'type': 'score', 'probabilities': reference},
        'sensitive_information': {'type': 'score', 'probabilities': sensitive},
    }


STRONG = dist(**{'3': 0.35, '4': 0.62})   # P4 .62, P3+ .97
WEAK = dist(**{'1': 0.5, '2': 0.4})       # P3+ .10
MID = dist(**{'2': 0.22, '3': 0.57, '4': 0.21})  # the keyring published note


class Mass(unittest.TestCase):
    def test_sums_requested_levels(self):
        self.assertAlmostEqual(mass(STRONG, (3, 4)), 0.97)
        self.assertAlmostEqual(mass(STRONG, (4,)), 0.62)

    def test_missing_levels_count_as_zero(self):
        self.assertAlmostEqual(mass({'4': 1.0}, (3, 4)), 1.0)


# Boundary behaviour is a property of the policy, not of whichever numbers ship today.
STRICT = {
    'approve': {'factual_support': {'p4': 0.60, 'p3plus': 0.90},
                'durability': {'p3plus': 0.90}, 'reference_value': {'p3plus': 0.90}},
    'sensitivity_block': DEFAULT_POLICY['sensitivity_block'],
    'revise': DEFAULT_POLICY['revise'],
    'max_revisions': DEFAULT_POLICY['max_revisions'],
}


class ApprovePath(unittest.TestCase):
    def test_all_thresholds_met_approves(self):
        action, _, _ = decide(answers(STRONG, STRONG, STRONG), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'approve')

    def test_factual_p4_just_below_threshold_does_not_approve(self):
        factual = dist(**{'3': 0.41, '4': 0.59})  # P4 .59 < .60, P3+ passes
        action, _, _ = decide(answers(factual, STRONG, STRONG), 0, STRICT)
        self.assertNotEqual(action, 'approve')

    def test_factual_p3plus_below_threshold_does_not_approve(self):
        factual = dist(**{'2': 0.12, '4': 0.88})  # P4 passes, P3+ .88 < .90
        action, _, _ = decide(answers(factual, STRONG, STRONG), 0, STRICT)
        self.assertNotEqual(action, 'approve')

    def test_low_durability_blocks_approval(self):
        action, _, _ = decide(answers(STRONG, WEAK, STRONG), 0, DEFAULT_POLICY)
        self.assertNotEqual(action, 'approve')


class SensitivityGate(unittest.TestCase):
    def test_any_meaningful_level_four_mass_blocks_approval(self):
        action, reason, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'0': 0.98, '4': 0.02})), 0, DEFAULT_POLICY)
        self.assertNotEqual(action, 'approve')
        self.assertIn('sensitiv', reason.lower())

    def test_level_three_plus_mass_blocks_approval(self):
        action, _, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'0': 0.9, '3': 0.1})), 0, DEFAULT_POLICY)
        self.assertNotEqual(action, 'approve')

    def test_level_two_plus_majority_blocks_approval(self):
        action, _, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'1': 0.4, '2': 0.6})), 0, DEFAULT_POLICY)
        self.assertNotEqual(action, 'approve')

    def test_provenance_metadata_alone_still_approves(self):
        # Level 1 is ordinary local paths and repo names; it must not block.
        action, _, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'0': 0.3, '1': 0.7})), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'approve')

    def test_sensitive_with_budget_revises_rather_than_rejects(self):
        action, _, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'0': 0.9, '3': 0.1})), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'revise')

    def test_sensitive_without_budget_rejects(self):
        action, _, _ = decide(
            answers(STRONG, STRONG, STRONG, sensitive=dist(**{'0': 0.9, '3': 0.1})), 2, DEFAULT_POLICY)
        self.assertEqual(action, 'reject')


class RevisePath(unittest.TestCase):
    def test_valuable_but_unsupported_is_revised(self):
        # The handoff's key pattern: high value and durability, weak evidence.
        action, _, instructions = decide(answers(WEAK, STRONG, STRONG), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'revise')
        self.assertTrue(instructions.strip())

    def test_ambiguous_keyring_shaped_case_is_revised_not_rejected(self):
        action, _, _ = decide(answers(MID, STRONG, STRONG), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'revise')

    def test_revision_budget_exhausted_rejects(self):
        action, _, _ = decide(answers(WEAK, STRONG, STRONG), 2, DEFAULT_POLICY)
        self.assertEqual(action, 'reject')

    def test_last_revision_still_allowed_at_one(self):
        action, _, _ = decide(answers(WEAK, STRONG, STRONG), 1, DEFAULT_POLICY)
        self.assertEqual(action, 'revise')

    def test_low_value_is_rejected_not_revised(self):
        action, _, _ = decide(answers(WEAK, WEAK, WEAK), 0, DEFAULT_POLICY)
        self.assertEqual(action, 'reject')

    def test_revise_instructions_mention_the_failing_dimension(self):
        _, _, instructions = decide(answers(WEAK, STRONG, STRONG), 0, DEFAULT_POLICY)
        self.assertIn('evidence', instructions.lower())


class BuildState(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            'title': 'Khoj upload acknowledgements do not prove note retention',
            'note': '# Khoj upload acknowledgements do not prove note retention\n\nBody text.',
            'sources': [{'id': 's1', 'path': '/x/docs/results.md', 'start': 128, 'end': 128,
                         'sha256': 'a' * 64, 'text': 'forty files from sixty acknowledgements'}],
        }

    def test_claim_is_the_exact_note_that_will_be_committed(self):
        state = build_state(self.snapshot, why_useful='avoids repeating an investigation')
        self.assertEqual(state['submission']['claim'], self.snapshot['note'])

    def test_title_and_why_useful_carried_through(self):
        state = build_state(self.snapshot, why_useful='avoids repeating an investigation')
        self.assertEqual(state['submission']['title'], self.snapshot['title'])
        self.assertEqual(state['submission']['why_useful'], 'avoids repeating an investigation')

    def test_source_text_becomes_excerpt(self):
        state = build_state(self.snapshot, why_useful='')
        source = state['sources'][0]
        self.assertEqual(source['excerpt'], 'forty files from sixty acknowledgements')
        self.assertEqual(source['id'], 's1')
        self.assertEqual(source['start'], 128)

    def test_digest_is_not_sent_to_the_model(self):
        # sha256 adds no semantic evidence and is high-entropy noise in the packet.
        state = build_state(self.snapshot, why_useful='')
        self.assertNotIn('sha256', state['sources'][0])


if __name__ == '__main__':
    unittest.main()


class ShippedThresholds(unittest.TestCase):
    """Pins the relaxed values chosen from the negative-mutation measurements, so a
    change to them is a deliberate edit rather than a silent drift."""

    def test_factual_thresholds(self):
        self.assertEqual(DEFAULT_POLICY['approve']['factual_support'], {'p4': 0.25, 'p3plus': 0.85})

    def test_value_thresholds(self):
        self.assertEqual(DEFAULT_POLICY['approve']['durability']['p3plus'], 0.80)
        self.assertEqual(DEFAULT_POLICY['approve']['reference_value']['p3plus'], 0.80)

    def test_credential_thresholds_were_never_relaxed(self):
        """A published credential is not reversible, so these two may only ever fall."""
        self.assertLessEqual(DEFAULT_POLICY['sensitivity_block']['p4'], 0.01)
        self.assertLessEqual(DEFAULT_POLICY['sensitivity_block']['p3plus'], 0.05)

    def test_level_two_block_was_not_loosened(self):
        """Level 2 was re-scoped from any internal operational detail to another party's
        confidential operations. The threshold tracks that narrower meaning and must not
        drift upward past the value it had under the broader one."""
        self.assertLessEqual(DEFAULT_POLICY['sensitivity_block']['p2plus'], 0.50)
        self.assertEqual(DEFAULT_POLICY['sensitivity_block']['p2plus'], 0.35)

    def test_revision_cap(self):
        self.assertEqual(DEFAULT_POLICY['max_revisions'], 2)


class ConfigLoading(unittest.TestCase):
    def test_missing_block_keeps_defaults(self):
        from reviewer.jev import policy_from_config
        self.assertEqual(policy_from_config({}), DEFAULT_POLICY)

    def test_partial_override_keeps_other_gates(self):
        from reviewer.jev import policy_from_config
        policy = policy_from_config({'jev': {'approve': {'factual_support': {'p4': 0.4}}}})
        self.assertEqual(policy['approve']['factual_support']['p4'], 0.4)
        self.assertEqual(policy['approve']['factual_support']['p3plus'], 0.85)
        self.assertEqual(policy['sensitivity_block'], DEFAULT_POLICY['sensitivity_block'])

    def test_disabled_by_default(self):
        from reviewer.jev import enabled
        self.assertFalse(enabled({}))
        self.assertFalse(enabled({'jev': {'enabled': False}}))
        self.assertTrue(enabled({'jev': {'enabled': True}}))


class CredentialDiscoveryTests(unittest.TestCase):
    """A systemd user service never sources the shell profile, so the gate deferred every
    ticket with 'TYPESAFE_API_KEY is not set' while the key sat readable on disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'typesafe.key')

    def test_key_file_is_used_when_the_environment_is_empty(self):
        with open(self.path, 'w') as handle:
            handle.write('sk-from-file\n')
        with patch.dict(os.environ, {'TYPESAFE_KEY_FILE': self.path}, clear=False):
            os.environ.pop('TYPESAFE_API_KEY', None)
            self.assertEqual(jev._key_from_file(), 'sk-from-file')

    def test_missing_key_file_is_not_an_error(self):
        with patch.dict(os.environ, {'TYPESAFE_KEY_FILE': self.path}, clear=False):
            self.assertIsNone(jev._key_from_file())

    def test_blank_key_file_is_not_a_credential(self):
        with open(self.path, 'w') as handle:
            handle.write('   \n')
        with patch.dict(os.environ, {'TYPESAFE_KEY_FILE': self.path}, clear=False):
            self.assertIsNone(jev._key_from_file())

    def test_unavailable_names_both_places_it_looked(self):
        with patch.dict(os.environ, {'TYPESAFE_KEY_FILE': self.path}, clear=False):
            os.environ.pop('TYPESAFE_API_KEY', None)
            with self.assertRaises(jev.JevUnavailable) as caught:
                jev.evaluate({'submission': {}}, timeout=1)
            self.assertIn(self.path, str(caught.exception))


class SensitivityQuestionTests(unittest.TestCase):
    """Level 2 used to read 'internal operational information ... access-control
    arrangements', which classified the operator describing their own machine as a
    disclosure. Six security-operations lessons were blocked by it, including the one
    that diagnosed why the gate itself had failed. Level 2 now means another party's
    confidential operations; credentials remain the deterministic scanner's job."""

    def setUp(self):
        self.sensitivity = jev.questions()['sensitive_information']

    def test_describing_your_own_systems_is_not_a_disclosure(self):
        self.assertIn('operator', self.sensitivity['criteria'][1])

    def test_level_two_is_about_another_party(self):
        self.assertIn('other than the operator', self.sensitivity['criteria'][2])

    def test_security_subject_matter_alone_does_not_elevate(self):
        text = self.sensitivity['instructions']
        self.assertIn('not whether its subject matter is', text)
        for topic in ('keyrings', 'authentication', 'permissions', 'encryption'):
            self.assertIn(topic, text)

    def test_usable_credentials_remain_the_top_level(self):
        top = self.sensitivity['criteria'][4]
        for material in ('passwords', 'API keys', 'private keys', 'session cookies'):
            self.assertIn(material, top)

    def test_confidential_third_party_data_stays_above_the_block(self):
        self.assertIn('customer', self.sensitivity['criteria'][3])

    def test_shipped_block_matches_the_narrower_level_two(self):
        block = DEFAULT_POLICY['sensitivity_block']
        self.assertEqual(block['p2plus'], 0.35)
        self.assertEqual(block['p3plus'], 0.05)
        self.assertEqual(block['p4'], 0.01)
