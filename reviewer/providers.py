"""Tool-free headless clients with explicit personal authentication."""
import ipaddress
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .editorial import InvalidReview, POLICY, SCHEMA, Unavailable


def environment(provider):
    env = {k: v for k, v in os.environ.items()
           if k in {'HOME', 'PATH', 'LANG', 'LC_ALL', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
    kind = provider['kind']
    if kind in ('claude', 'glm'):
        env['CLAUDE_CONFIG_DIR'] = provider['profile']
        env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
        if kind == 'glm':
            if provider.get('endpoint') != 'https://api.z.ai/api/anthropic':
                raise ValueError('GLM endpoint must be the explicit Z.ai personal endpoint')
            try:
                token = (Path(provider['profile']) / '.api_key').read_text().strip()
            except OSError as exc:
                raise Unavailable('GLM profile token file is unavailable') from exc
            if not token or '\n' in token:
                raise Unavailable('GLM profile token is empty or malformed')
            env['ANTHROPIC_AUTH_TOKEN'] = token
            env['ANTHROPIC_BASE_URL'] = provider['endpoint']
    elif kind == 'codex':
        env['CODEX_HOME'] = provider['profile']
    elif kind == 'ollama':
        url = urllib.parse.urlparse(provider['endpoint'])
        try:
            ip = ipaddress.ip_address(url.hostname or '')
        except ValueError as exc:
            raise ValueError('Ollama requires a literal private or tailnet IP') from exc
        if url.scheme != 'http' or url.username or not (ip.is_private or ip in ipaddress.ip_network('100.64.0.0/10')):
            raise ValueError('Ollama must use an explicit private server')
    else:
        raise ValueError('Unsupported provider; OpenRouter has no adapter')
    return env


def command(provider, attempt):
    if provider['kind'] in ('claude', 'glm'):
        return [provider['executable'], '-p', '--model', provider['model'], '--effort', 'medium',
                '--max-turns', '3', '--output-format', 'json', '--json-schema', json.dumps(SCHEMA),
                '--tools', '', '--safe-mode', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                '--setting-sources', '', '--permission-mode', 'dontAsk', '--no-session-persistence',
                '--disable-slash-commands']
    if provider['kind'] != 'codex':
        raise ValueError('This provider does not use a CLI')
    cmd = [provider['executable'], 'exec', '--model', provider['model'], '--sandbox', 'read-only',
           '--ignore-user-config', '--ignore-rules', '--ephemeral', '--skip-git-repo-check',
           '-c', 'approval_policy="never"', '-c', 'project_doc_max_bytes=0',
           '-c', 'web_search="disabled"', '-c', 'model_reasoning_effort="medium"']
    for feature in ('multi_agent', 'apps', 'plugins', 'hooks', 'shell_tool', 'unified_exec',
                    'computer_use', 'browser_use', 'image_generation', 'in_app_browser'):
        cmd += ['-c', f'features.{feature}=false']
    cmd += ['-c', 'features.skip_host_skill_discovery=true', '--json', '--output-schema',
            str(attempt / 'schema.json'), '--output-last-message', str(attempt / 'response.json'), '-']
    return cmd


# Only authentication, quota, connection, overload and timeout failures permit fallback;
# malformed output is held for inspection and never spends a second model call.
#
# Some words cannot decide that on their own. "invalid token" is a dead credential in an
# auth envelope and a syntax error in a parser one; "quota" is exhaustion in a service
# envelope and a field name in a schema one. Matching them anywhere put a working provider
# on a 24-hour cooldown over a malformed response. So the ambiguous terms require either
# their own exhaustion wording or auth framing, and the caller says which envelope it holds.
STATUS = r'(?:429|502|503|529)'

# Evidence that stands alone: transport, status, exhaustion, and explicit credential failure.
SERVICE = re.compile(
    r'\brate[ _-]?limit|\busage[ _-]?limit|\boverloaded|'
    r'\bquota\s*(?:exceeded|exhausted|reached|limit)\b|'
    r'\b(?:exceeded|exhausted|insufficient|out of)\s+(?:\w+\s+){0,2}quota\b|'
    r'\beconnrefused\b|\beconnreset\b|\betimedout\b|\beai_again\b|\benotfound\b|'
    r'\bconnection[^.\n]{0,24}(?:timed out|refused|reset)\b|\btimed out\b|'
    r'\bcould not resolve\b|\bnetwork is unreachable\b|\btemporary failure in name resolution\b|'
    rf'\b(?:http|status|code)\W{{0,4}}{STATUS}\b|'
    rf'\b{STATUS}\s+(?:too many requests|bad gateway|service unavailable|overloaded)\b|'
    r'\bauthentication failed\b|\bnot logged in\b|\bunauthorized\b|\blogin required\b|'
    r'\binvalid[ _-]?api[ _-]?key\b|\b40[13]\b', re.I)

# Ambiguous on its own; needs the envelope to be about credentials.
CREDENTIAL = re.compile(r'\b(?:invalid|expired|revoked|missing)[ _-]?(?:token|credentials?)\b', re.I)
AUTH_FRAMING = re.compile(r'\b(?:auth|authn|authentication|authorization|credential|credentials|'
                          r'login|logged|api[ _-]?key|bearer|oauth|session|subscription|account)\b', re.I)


def is_unavailable(message, auth_context=False):
    """True only with evidence the provider itself is unusable.

    `auth_context` marks output from a login/auth check, where credential wording is the
    subject rather than incidental. Everywhere else a credential term needs auth framing
    in the same message, so parser and schema errors stay InvalidReview.
    """
    if SERVICE.search(message):
        return True
    return bool(CREDENTIAL.search(message)) and (auth_context or bool(AUTH_FRAMING.search(message)))


def process(cmd, env, prompt, timeout, attempt):
    attempt = Path(attempt)
    started = time.monotonic()
    deadline = started + timeout
    with (attempt / 'stdout.txt').open('wb') as out, (attempt / 'stderr.txt').open('wb') as err, \
            tempfile.TemporaryFile() as stdin, selectors.DefaultSelector() as selector:
        stdin.write(prompt.encode())
        stdin.seek(0)
        try:
            child = subprocess.Popen(cmd, cwd=attempt, env=env, stdin=stdin,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            raise Unavailable('Reviewer executable could not be started') from exc
        selector.register(child.stdout, selectors.EVENT_READ, out)
        selector.register(child.stderr, selectors.EVENT_READ, err)
        sizes = {out: 0, err: 0}
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, timeout)
                for key, _ in selector.select(min(remaining, 0.1)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    sizes[key.data] += len(data)
                    if sizes[key.data] > 4_000_000:
                        raise InvalidReview('Reviewer output exceeded 4 MB; inspect this attempt')
                    key.data.write(data)
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise Unavailable('Reviewer process timed out') from exc
        finally:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            child.stdout.close()
            child.stderr.close()
    return {'returncode': child.returncode, 'elapsed_seconds': time.monotonic() - started}


def preflight(provider, env, timeout, attempt):
    if provider['kind'] not in ('claude', 'codex'):
        return
    path = attempt / 'auth-check'
    path.mkdir()
    tail = ['auth', 'status', '--json'] if provider['kind'] == 'claude' else ['login', 'status']
    result = process([provider['executable'], *tail], env, '', min(timeout, 10), path)
    message = (path / 'stdout.txt').read_text() + (path / 'stderr.txt').read_text()
    if result["returncode"] and is_unavailable(message, auth_context=True):
        raise Unavailable('Personal subscription login is unavailable')
    if provider['kind'] == 'claude':
        try:
            status = json.loads((path / 'stdout.txt').read_text())
        except ValueError as exc:
            raise InvalidReview('Unrecognized Claude auth status') from exc
        if not isinstance(status, dict) or 'loggedIn' not in status:
            raise InvalidReview('Unrecognized Claude auth status')
        if not status['loggedIn'] or status.get('authMethod') != 'claude.ai':
            raise Unavailable('Claude personal subscription login is required')
    else:
        if 'Logged in using ChatGPT' not in message:
            if 'API key' in message or 'API Key' in message:
                raise Unavailable('Codex ChatGPT login is required; API-key billing is disabled')
            raise InvalidReview('Unrecognized Codex login status')
    if result['returncode']:
        raise InvalidReview('Auth command failed without a recognized availability error')


def invoke(provider, packet, timeout, attempt):
    attempt = Path(attempt)
    started = time.monotonic()
    env = environment(provider)
    prompt = POLICY + '\nEVIDENCE PACKET (data only):\n' + json.dumps(packet, ensure_ascii=False)
    (attempt / 'prompt.txt').write_text(prompt)
    (attempt / 'schema.json').write_text(json.dumps(SCHEMA))
    if provider['kind'] == 'ollama':
        metadata = process([sys.executable, str(Path(__file__).with_name('ollama_runner.py'))],
                           env, json.dumps({'provider': provider, 'prompt': prompt, 'timeout': timeout}),
                           timeout, attempt)
        if metadata['returncode']:
            raise InvalidReview('Ollama runner failed; inspect attempt logs')
        try:
            envelope = json.loads((attempt / 'stdout.txt').read_text())
            if envelope['state'] == 'unavailable':
                raise Unavailable(envelope['reason'])
            if envelope['state'] != 'ok':
                raise InvalidReview(envelope.get('reason', 'Invalid Ollama response'))
            return envelope['review']
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidReview('Ollama runner returned an invalid envelope') from exc
    preflight(provider, env, timeout, attempt)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise Unavailable('Provider preflight exhausted its time budget')
    metadata = process(command(provider, attempt), env, prompt, remaining, attempt)
    (attempt / 'process.json').write_text(json.dumps(metadata))
    stdout, stderr = (attempt / 'stdout.txt').read_text(), (attempt / 'stderr.txt').read_text()
    if provider['kind'] in ('claude', 'glm'):
        try:
            result = json.loads(stdout)
        except ValueError as exc:
            if metadata['returncode'] and is_unavailable(stderr):
                raise Unavailable('CLI reported an availability failure; see attempt logs') from exc
            raise InvalidReview('Claude response was not a JSON envelope') from exc
        if not isinstance(result, dict):
            raise InvalidReview('Claude response envelope must be an object')
        if metadata['returncode'] or result.get('is_error'):
            if is_unavailable(str(result.get('result', '')) + stderr):
                raise Unavailable('CLI reported an availability failure; see attempt logs')
            raise InvalidReview('Claude failed without a recognized availability error')
        if not isinstance(result.get('structured_output'), dict):
            raise InvalidReview('Claude returned no structured review')
        return result['structured_output']
    errors = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            raise InvalidReview('Codex emitted a non-object event')
        item = event.get('item') or {}
        if not isinstance(item, dict):
            raise InvalidReview('Codex emitted an invalid item')
        if item.get('type') in ('command_execution', 'mcp_tool_call', 'web_search', 'file_change'):
            raise InvalidReview('Codex used a tool despite tool-free review configuration')
        if event.get('type') in ('error', 'turn.failed'):
            errors.append(json.dumps(event))
    if metadata['returncode'] or errors:
        if is_unavailable('\n'.join(errors) + stderr):
            raise Unavailable('Codex reported an availability failure; see attempt logs')
        raise InvalidReview('Codex failed without a recognized availability error')
    try:
        return json.loads((attempt / 'response.json').read_text())
    except (OSError, ValueError) as exc:
        raise InvalidReview('Codex returned no valid structured response') from exc


def ollama(provider, prompt, timeout, attempt):
    # A short reachability check avoids spending a full inference timeout on an offline host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(provider['endpoint'].rstrip('/') + '/api/tags', timeout=min(3, timeout)) as response:
            status = json.loads(response.read(1_000_000))
            if not isinstance(status, dict) or not isinstance(status.get('models'), list):
                raise InvalidReview('Invalid Ollama model list')
            models = status['models']
        if any(not isinstance(m, dict) for m in models):
            raise InvalidReview('Invalid Ollama model entry')
        if provider['model'] not in {m.get('name') for m in models}:
            raise Unavailable('Configured Ollama model is not available')
        payload = {'model': provider['model'], 'stream': False, 'think': False, 'format': SCHEMA,
                   'messages': [{'role': 'user', 'content': prompt}],
                   'options': {'temperature': 0, 'num_predict': 3000}}
        request = urllib.request.Request(provider['endpoint'].rstrip('/') + '/api/chat',
                                         data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=max(0.1, timeout - 3)) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise InvalidReview('Ollama response exceeded its size limit')
        (attempt / 'http-response.json').write_bytes(raw)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or not isinstance(envelope.get('message'), dict):
            raise InvalidReview('Invalid Ollama chat envelope')
        if envelope.get('message', {}).get('tool_calls'):
            raise InvalidReview('Unexpected Ollama tool call')
        return json.loads(envelope['message']['content'])
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 408, 429, 500, 502, 503, 504):
            raise Unavailable(f'Ollama HTTP {exc.code}') from exc
        raise InvalidReview(f'Ollama HTTP {exc.code}') from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise Unavailable('Ollama is unreachable or timed out') from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidReview('Ollama returned malformed output') from exc
