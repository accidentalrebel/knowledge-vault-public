"""CLI for submission, bounded review, qualification, and inspection."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

from .editorial import fingerprint
from .providers import invoke
from .qualification import qualify
from .store import Store
from .worker import eligible, run


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / 'ops' / 'reviewer.json'


def load_config(path):
    config = json.loads(Path(path).read_text())
    for key in ('state', 'vault'):
        if not Path(config[key]).is_absolute():
            raise ValueError(f'{key} must be an absolute path')
    state, vault = Path(config['state']).resolve(), Path(config['vault']).resolve()
    if state.is_relative_to(vault) or vault.is_relative_to(state):
        raise ValueError('Reviewer state must be outside and separate from the indexed vault')
    if not config['source_roots'] or any(not Path(p).is_absolute() for p in config['source_roots']):
        raise ValueError('Configure absolute evidence source roots')
    for section, keys in [('publication', ('state', 'khoj_source')), ('telegram', ('spool', 'bridge_scripts'))]:
        if section in config and any(key in config[section] and not Path(config[section][key]).is_absolute()
                                     for key in keys):
            raise ValueError(f'{section} paths must be absolute')
    search = config.get('dedup')
    if search is not None:
        # The query carries a candidate's title and claim, so it must not leave the machine.
        if not search.get('khoj_url', '').startswith(('http://127.0.0.1', 'http://localhost')):
            raise ValueError('Duplicate search must query Khoj on loopback')
        if not Path(search.get('manifest', '')).is_absolute():
            raise ValueError('The sync manifest path must be absolute')
    if 'dashboard' in config:
        board = config['dashboard']
        if board.get('host') != '127.0.0.1' or not isinstance(board.get('port'), int) or not 1024 <= board['port'] <= 65535:
            raise ValueError('The dashboard must bind a loopback host and an unprivileged port')
        if not board.get('public_origin', '').startswith('https://') or board['public_origin'].endswith('/'):
            raise ValueError('The dashboard public origin must be an exact https origin without a trailing slash')
        if not board.get('allowed_login', '').strip():
            raise ValueError('Configure the Tailscale login allowed to act on tickets')
        if not Path(board.get('secret', '')).is_absolute():
            raise ValueError('The dashboard action secret must be an absolute path')
        if Path(board['secret']).resolve().is_relative_to(vault):
            raise ValueError('The dashboard action secret must be outside the indexed vault')
    if 'publication' in config and Path(config['publication']['state']).resolve().is_relative_to(vault):
        raise ValueError('Publication state must be outside the indexed vault')
    if 'spool' in config.get('telegram', {}) and Path(config['telegram']['spool']).resolve().is_relative_to(vault):
        raise ValueError('Telegram spool must be outside the indexed vault')
    order = ['claude', 'codex', 'glm', 'ollama']
    names = [p['name'] for p in config['providers']]
    if any(n not in order for n in names) or len(names) != len(set(names)):
        raise ValueError('Unsupported provider; only personal Claude, Codex, GLM and Ollama are supported')
    if names != sorted(names, key=order.index):
        raise ValueError('Provider order must be Claude, Codex, GLM, Ollama')
    profiles = {'claude': '.claude', 'codex': '.codex', 'glm': '.claude-glm'}
    for p in config['providers']:
        if p['kind'] != p['name'] or not isinstance(p['enabled'], bool) or not p['model']:
            raise ValueError('Invalid provider settings')
        if p['name'] in profiles:
            if Path(p['profile']).name != profiles[p['name']] or not Path(p['profile']).is_absolute():
                raise ValueError('Use the personal profile; work profiles are not permitted')
            if not Path(p['executable']).is_absolute():
                raise ValueError('Provider executable must be an absolute path')
    return config


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('submit', help='Queue one evidenced candidate per completed task').add_argument('file')
    commands.add_parser('run', help='Review one candidate when hourly and rolling-day limits permit it')
    commands.add_parser('status', help='Inspect queue and provider eligibility')
    commands.add_parser('show', help='Show a candidate, snapshots and saved decision').add_argument('id')
    commands.add_parser('qualify', help='Run six editorial examples; consumes provider allowance').add_argument('provider')
    commands.add_parser('ticket', help='Inspect a frozen Telegram draft without credentials').add_argument('number', type=int)
    commands.add_parser('reopen', help='Return a finished candidate to the queue and supersede its tickets').add_argument('id')
    commands.add_parser('telegram-tick', help='Announce one newly prepared draft and deliver pending outcomes')
    commands.add_parser('dashboard', help='Serve the private approval dashboard on the configured loopback port')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        store = Store(config['state'])
        if args.command == 'submit':
            path = Path(args.file)
            if path.stat().st_size > 32_000:
                raise ValueError('Candidate JSON exceeds 32000 bytes')
            result = store.submit(json.loads(path.read_text()), config['source_roots'])
        elif args.command == 'run':
            result = run(store, config, invoke)
        elif args.command == 'show':
            result = store.get(args.id)
        elif args.command == 'reopen':
            result = store.reopen(args.id)
        elif args.command == 'qualify':
            result = qualify(store, config, args.provider, invoke)
        elif args.command == 'ticket':
            from .approvals import inspect_ticket
            result = inspect_ticket(store, args.number)
        elif args.command in ('telegram-tick', 'dashboard'):
            if any(section not in config for section in ('publication', 'dashboard', 'telegram')):
                raise ValueError('Configure publication, dashboard and telegram before starting the approval service')
            from .approvals import ApprovalDesk
            from .publication import Publisher
            from .telegram import TelegramTransport
            from .telegram_runner import tick
            transport = TelegramTransport(os.environ.get('TELEGRAM_BOT_TOKEN', ''), os.environ.get('TELEGRAM_CHAT_ID', '0'))
            publisher = Publisher(config)
            try:
                desk = ApprovalDesk(store, config, transport, publisher)
                if args.command == 'telegram-tick':
                    result = tick(desk, config)
                else:
                    from .dashboard import serve
                    return serve(desk, config)
            except publisher.error as exc:
                raise ValueError(str(exc)) from None
        else:
            result = store.status()
            result['eligible_providers'] = [p['name'] for p in eligible(store, config['providers'], time.time())]
            result['qualification_current'] = {
                p['name']: (store.get_meta('qualified:' + p['name']) or {}).get('fingerprint') == fingerprint(p)
                for p in config['providers']}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
