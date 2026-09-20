"""Deterministic secret detection for the autonomous approval gate.

This runs before the note packet is sent to TypeSafe and again before the git commit.
A hit is unappealable: no model score may override it. Jev's `sensitive_information`
score is a semantic backstop for judgement calls, not a credential detector.

Design note: published notes routinely cite source digests and git revisions, so a
blanket high-entropy rule would fail every note in the vault. Pure hexadecimal of
digest length is therefore excluded, and the entropy rule only considers tokens that
cannot be a digest.
"""
import math
import re

# Values that look like secrets but are explicitly stand-ins. The keyring evidence
# packet contained "Authorization=Bearer ..." and must not be gated on it.
PLACEHOLDER = re.compile(
    r'^(?:\.{3}|\*+|x{3,}|<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|'
    r'(?:your|my|the)[_-]?\w*|example\w*|placeholder\w*|redacted|masked|changeme|'
    r'test[_-]?\w*|dummy\w*|fake\w*|none|null|nil|todo)$',
    re.I)

HEX = re.compile(r'^[0-9a-f]+$', re.I)

# High-confidence vendor prefixes. Presence of the pattern is the finding.
PREFIXED = (
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('github_token', re.compile(r'\b gh[pousr]_[A-Za-z0-9]{36,} \b'.replace(' ', ''))),
    ('anthropic_key', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai_key', re.compile(r'\bsk-(?!ant-)[A-Za-z0-9]{32,}')),
    ('slack_token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}')),
    ('google_key', re.compile(r'\bAIza[A-Za-z0-9_-]{35}\b')),
    ('gitlab_token', re.compile(r'\bglpat-[A-Za-z0-9_-]{20,}')),
    ('npm_token', re.compile(r'\bnpm_[A-Za-z0-9]{36}\b')),
    ('private_key_block', re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')),
    ('connection_string_password', re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s:/@]+:([^\s:/@]{8,})@')),
)

# A credential-bearing assignment: a secret-ish name, then a value that is neither a
# placeholder nor a digest.
ASSIGNMENT = re.compile(
    r'\b(?P<name>[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|secret|password|passwd|pwd|token|'
    r'credential|auth)[A-Za-z0-9_.-]*)\s*[:=]\s*(?P<quote>["\']?)(?P<value>[^\s"\',;]{8,})(?P=quote)',
    re.I)

MIN_ENTROPY_LENGTH = 32
MIN_ENTROPY = 3.5
# No dot or slash: keeps filesystem paths and dotted version strings out of the rule.
TOKEN = re.compile(r'\b[A-Za-z0-9_+=-]{%d,}\b' % MIN_ENTROPY_LENGTH)

# kebab- and snake-case names built from short word-like segments. Repository names and
# filenames are long and reasonably high-entropy, but they are identifiers, not secrets.
IDENTIFIER = re.compile(r'^[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+$')
MAX_SEGMENT = 12

# Words joined by case rather than by a separator. An API error code such as
# InputImageSensitiveContentDetected is 34 characters at entropy 3.70 and hard-rejected a
# real candidate. A value that is letters only and alternates capital-then-lowercase the
# whole way is a name; tokens of this length are drawn from a mixed alphabet.
CAMEL = re.compile(r'^[A-Za-z][a-z]*(?:[A-Z][a-z]+)+[A-Z]?$')

# An unquoted assignment value carrying code punctuation is an expression, not a literal
# secret: `auth = read_json(config[...])` is source, not a credential. Quoted values are
# still scanned, so a real password containing punctuation is not excused by this.
CODEY = re.compile(r'[()\[\]{}<>\\|&`]')


def _segment(part):
    """A segment is a word if it is letters only, whatever its length: task keys carry
    runs like 'bypasspermissions' that exceed any sensible word limit. The length cap
    still applies once digits appear, so `key-9f8a7b6c5d4e3f2a1b0c9d8e7f` stays a secret.
    """
    return part.isalpha() or len(part) <= MAX_SEGMENT


def _identifier(value):
    if CAMEL.match(value):
        return True
    if not IDENTIFIER.match(value):
        return False
    return all(_segment(segment) for segment in re.split(r'[-_]', value))


def entropy(value):
    if not value:
        return 0.0
    return -sum((n / len(value)) * math.log2(n / len(value))
                for n in (value.count(c) for c in set(value)))


def _hex_run(value):
    """A long hex-only run with no credential keyword near it is a hash, not a secret.
    Notes quote digests constantly - source sha256s, revisions, content hashes - and a
    non-canonical length (62 here, from a digest quoted mid-sentence) slipped past the
    exact-length check and hard-rejected a real note. This is deliberately not used by the
    assignment rule: `api_key = <hex>` still gets flagged, because some keys are hex.
    """
    return len(value) >= 32 and bool(HEX.match(value))


def _is_digest(value):
    """sha1/sha256/md5 hex and git revisions are evidence provenance, not secrets."""
    return bool(HEX.match(value)) and len(value) in (32, 40, 64, 128)


def _placeholder(value):
    return bool(PLACEHOLDER.match(value.strip('"\'')))


def _finding(rule, value):
    """Findings are logged and announced, so they must never carry the secret itself."""
    return {'rule': rule, 'length': len(value), 'hint': value[:2] + '…' if len(value) > 4 else '…'}


def scan(text):
    """Return a list of findings for one string. Empty list means clean."""
    if not isinstance(text, str) or not text:
        return []
    findings = []
    for rule, pattern in PREFIXED:
        for match in pattern.finditer(text):
            value = match.group(1) if pattern.groups else match.group(0)
            if _placeholder(value):
                continue
            findings.append(_finding(rule, value))
    for match in ASSIGNMENT.finditer(text):
        value = match.group('value')
        if _placeholder(value) or _is_digest(value):
            continue
        if value.startswith('$'):
            continue
        if not match.group('quote') and CODEY.search(value):
            continue
        if len(value) >= 16 or entropy(value) >= MIN_ENTROPY:
            findings.append(_finding('credential_assignment', value))
    for match in TOKEN.finditer(text):
        value = match.group(0)
        if _hex_run(value) or _is_digest(value) or _placeholder(value) or _identifier(value):
            continue
        if entropy(value) >= MIN_ENTROPY:
            findings.append(_finding('high_entropy_token', value))
    return findings


def scan_state(state):
    """Scan every string anywhere in the packet, including source excerpts and paths."""
    findings = []

    def walk(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, path + '.' + str(key))
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, path + '[%d]' % index)
        elif isinstance(node, str):
            for finding in scan(node):
                findings.append(dict(finding, where=path.lstrip('.')))

    walk(state, '')
    return findings
