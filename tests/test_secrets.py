"""The deterministic secret gate. A hit is unappealable, so false negatives are the
danger and false positives merely cost a revision.

Fixtures below are synthetic and assembled at runtime where a literal would be
structurally valid. A detector's own test suite is full of secret-shaped strings,
so writing a real-looking token into this file trips every scanner that reads the
repository - including this one - over a string that was never a credential.
"""
import base64
import json
import unittest

from reviewer.secrets import scan, scan_state


def jwt(subject='1234567890', signature='not-a-real-signature'):
    """A structurally valid JWT built from parts, so no token literal is stored."""
    def segment(payload):
        raw = json.dumps(payload, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return '.'.join((segment({'alg': 'HS256'}), segment({'sub': subject}), signature))


class DetectsRealSecrets(unittest.TestCase):
    def test_aws_access_key(self):
        self.assertTrue(scan('AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE'))

    def test_github_token(self):
        self.assertTrue(scan('use ghp_016C7f4a9B2d8E3f5A6b7C8d9E0f1A2b3C4d5E'))

    def test_anthropic_style_key(self):
        self.assertTrue(scan('ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789'))

    def test_private_key_block(self):
        self.assertTrue(scan('-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n'))

    def test_jwt(self):
        self.assertTrue(scan(jwt()))

    def test_bearer_with_real_looking_value(self):
        self.assertTrue(scan('Authorization=Bearer ' + jwt('a')))

    def test_password_in_connection_string(self):
        self.assertTrue(scan('postgres://admin:hunter2hunter2hunter2@db.internal:5432/app'))

    def test_glm_shaped_key_in_assignment(self):
        # The shape actually found in a real shell profile: hex32 dot alnum16.
        self.assertTrue(scan('export GLM_API_KEY="0123456789abcdef0123456789abcdef.AbCdEfGhIjKlMnOp"'))


class AllowsLegitimateVaultContent(unittest.TestCase):
    def test_placeholder_bearer_is_not_a_secret(self):
        # This exact string appeared in the keyring evidence packet and must pass.
        self.assertEqual(scan("a header such as Authorization=Bearer ... is not a usable secret"), [])

    def test_sha256_digest_is_not_a_secret(self):
        # Every published note cites source digests; flagging these fails the whole vault.
        self.assertEqual(
            scan('Source: README.md (sha256 `9d0d59b4564c0466696031cea39f43ec689aaccc2088044652db62f71c3c2dc3`)'),
            [])

    def test_git_revision_hash_is_not_a_secret(self):
        self.assertEqual(scan('revision da21d05575a18a985f99881108956ece95b5b4ccd2eee0d99968525e4176d712'), [])

    def test_redacted_and_env_references_are_not_secrets(self):
        self.assertEqual(scan('token: <MASKED>'), [])
        self.assertEqual(scan('export KEY="$OPENROUTER_API_KEY"'), [])
        self.assertEqual(scan('set API_KEY=YOUR_KEY_HERE'), [])

    def test_discussion_of_credentials_is_not_a_secret(self):
        self.assertEqual(
            scan('the keychain plugin reads secrets through the Secret Service on D-Bus, '
                 'so the daemon waits for an unlock prompt'), [])

    def test_kebab_case_identifiers_are_not_secrets(self):
        # Repository and file names are long and reasonably high-entropy, but they are
        # ordinary identifiers; flagging them rejected two real published notes.
        self.assertEqual(scan('the log-sim-scenario-generation-skill-kit repository'), [])
        self.assertEqual(scan('output/discovery-t9999-checkpoint-approved.md line 144'), [])
        self.assertEqual(scan('snake_case_module_name_that_is_long_enough'), [])

    def test_structureless_random_token_is_still_caught(self):
        self.assertTrue(scan('xK9mP2vLq7ZnR4tYw8BcE1dF5gH3jS6aQ4wZ'))

    def test_local_paths_are_not_secrets(self):
        self.assertEqual(scan('/home/you/development/knowledge-vault/.state/reviewer'), [])


class IdentifierExclusionIsNarrow(unittest.TestCase):
    """The kebab-case exclusion must suppress only the entropy heuristic. Every other
    detector has to keep firing on hyphenated values. Credentials below are synthetic."""

    def test_hyphenated_value_in_credential_assignment_still_fires(self):
        self.assertTrue(scan('API_KEY=alpha-bravo-charlie-delta-echo-foxtrot'))

    def test_hyphenated_password_assignment_still_fires(self):
        self.assertTrue(scan('password: correct-horse-battery-staple'))

    def test_hyphenated_vendor_prefix_still_fires(self):
        self.assertTrue(scan('glpat-abcd-efgh-ijkl-mnop-qrst'))
        self.assertTrue(scan('token=xoxb-1111111111-abcdefghij-klmnop'))

    def test_private_key_header_still_fires(self):
        self.assertTrue(scan('-----BEGIN OPENSSH PRIVATE KEY-----'))

    def test_hyphenated_bare_identifier_is_still_allowed(self):
        self.assertEqual(scan('alpha-bravo-charlie-delta-echo-foxtrot'), [])


class ScansWholePacket(unittest.TestCase):
    def test_finds_secret_in_a_source_excerpt(self):
        state = {'submission': {'title': 'x', 'claim': 'clean body', 'why_useful': 'clean'},
                 'sources': [{'id': 's1', 'path': 'a.md', 'excerpt': 'clean'},
                             {'id': 's2', 'path': 'b.md', 'excerpt': 'AWS_SECRET=AKIAIOSFODNN7EXAMPLE'}]}
        self.assertTrue(scan_state(state))

    def test_clean_packet_passes(self):
        state = {'submission': {'title': 'Khoj retention', 'claim': '# Khoj\n\nAcknowledgement is not retention.',
                                'why_useful': 'avoids a repeat investigation'},
                 'sources': [{'id': 's1', 'path': 'docs/results.md', 'excerpt': 'forty files from sixty acknowledgements'}]}
        self.assertEqual(scan_state(state), [])

    def test_findings_do_not_echo_the_secret(self):
        findings = scan('AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE')
        self.assertTrue(findings)
        for finding in findings:
            self.assertNotIn('AKIAIOSFODNN7EXAMPLE', repr(finding))


class CamelCaseIdentifierTests(unittest.TestCase):
    """API error codes are long, letters-only and score above the entropy floor. The
    third false positive of this shape: kebab-case names, then unquoted call expressions,
    now words joined by capitalisation. Each one hard-rejected a real candidate."""

    def test_api_error_code_is_not_a_secret(self):
        self.assertEqual(scan('InputImageSensitiveContentDetected'), [])

    def test_long_camel_case_run_is_not_a_secret(self):
        self.assertEqual(scan('InputImageSensitiveContentDetectedPrivacyInformation'), [])

    def test_camel_case_inside_prose_is_not_a_secret(self):
        body = 'The backend refused it with the error `InputImageSensitiveContentDetected` on upload.'
        self.assertEqual(scan(body), [])

    def test_mixed_alphabet_token_is_still_caught(self):
        self.assertTrue(scan('sk9Xk2mQ7fPl3vRt8wZa1bNcAAAAbbbbCCCC1234'))

    def test_vendor_prefixed_token_is_still_caught(self):
        self.assertTrue(scan('AKIAIOSFODNN7EXAMPLEAKIAIOSFODNN7EXAMPLE'))


class CodeExpressionTests(unittest.TestCase):
    """Real candidates cite source files, so an assignment of a function result to a
    variable named auth/token/key must not hard-reject the packet. Found by scanning a
    real backfill candidate that cited a project's connectors.py."""

    def test_unquoted_call_expression_is_not_a_credential(self):
        self.assertEqual(scan('auth = read_json(config["identity"])'), [])

    def test_unquoted_method_call_is_not_a_credential(self):
        self.assertEqual(scan('token = self.get_token(request)'), [])

    def test_unquoted_subscript_is_not_a_credential(self):
        self.assertEqual(scan('api_key = settings[environment]'), [])

    def test_unquoted_env_style_secret_is_still_caught(self):
        self.assertTrue(scan('API_KEY=sk9Xk2mQ7fPl3vRt8wZa1bNc'))

    def test_quoted_value_with_punctuation_is_still_scanned(self):
        self.assertTrue(scan('password = "p(ssw0rd!longenough123"'))


if __name__ == '__main__':
    unittest.main()


class DigestRunTests(unittest.TestCase):
    """Notes quote digests constantly - source sha256s, revisions, content hashes. A
    62-character hex run, a digest quoted mid-sentence, missed the exact-length check and
    hard-rejected a real note. Fourth false positive of this family."""

    DIGEST = '08e24e1dd853dc4e4bd5095b76a70bd56bbadce9c6cea3605ee53ce509e3c1'

    def test_non_canonical_length_digest_in_prose(self):
        self.assertEqual(scan('Editing-contract digest in full: %s as supplied' % self.DIGEST), [])

    def test_canonical_sha256_still_passes(self):
        self.assertEqual(scan('sha256 %sab' % self.DIGEST), [])

    def test_hex_assigned_to_a_credential_name_is_still_caught(self):
        """The assignment rule keeps its own naming context, so hex keys stay covered."""
        self.assertTrue(scan('api_key = %s' % self.DIGEST))

    def test_mixed_alphabet_token_is_still_caught(self):
        self.assertTrue(scan('sk9Xk2mQ7fPl3vRt8wZa1bNcAAAAbbbbCCCC1234'))

    def test_short_hex_is_not_excused(self):
        self.assertFalse(scan('deadbeef'))


class TaskKeySegmentTests(unittest.TestCase):
    """The scanner reads provenance too, and the reviewer quotes the task key in the note.
    A 44-character slug whose first segment was 17 letters long exceeded MAX_SEGMENT and
    dropped two security lessons. Fifth false positive of this family."""

    def test_long_word_segment_in_a_task_key(self):
        self.assertEqual(scan('task `backfill/2026-09-18/x/bypasspermissions-required-for-headless-bash`'), [])

    def test_slug_with_several_long_words(self):
        self.assertEqual(scan('allowed-tools-inert-under-bypasspermissions'), [])

    def test_segments_carrying_digits_keep_the_length_cap(self):
        self.assertTrue(scan('key-9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f'))

    def test_earlier_kebab_regressions_stay_fixed(self):
        for name in ('log-sim-scenario-generation-skill-kit', 'discovery-t9999-checkpoint-approved'):
            with self.subTest(name=name):
                self.assertEqual(scan(name), [])
