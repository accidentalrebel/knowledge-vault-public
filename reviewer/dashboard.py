"""Private review dashboard: server-rendered, identity-gated, token-bound actions.

Binds loopback only. Tailscale Serve terminates HTTPS on the tailnet and supplies
the `Tailscale-User-Login` identity header; nothing else may reach this port.
"""
import hashlib
import hmac
import html
import json
import time
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import outcomes
from .worker import BLOCK_KEY
from .approvals import ACTIONS, ticket_fingerprint
from .worker import worker_lock


MAX_BODY = 8192
FORM_TYPE = 'application/x-www-form-urlencoded'
POLICY = ("default-src 'none'; style-src 'self'; form-action 'self'; "
          "base-uri 'none'; frame-ancestors 'none'; img-src 'none'")
STYLE = '''
:root { color-scheme: light dark; --line: rgba(128,128,128,.32); --dim: rgba(128,128,128,.9);
        --good: #2f8f4e; --wait: #b07d1a; --drop: #8a8a8a; --warn: #c0392b; }
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 1.5rem 1.1rem 5rem; max-width: 46rem; line-height: 1.5;
       font-family: ui-sans-serif, system-ui, -apple-system, sans-serif; }
h1 { font-size: 1.45rem; margin: 0 0 .15rem; letter-spacing: -.01em; }
.asof { margin: 0 0 1.4rem; color: var(--dim); font-size: .9rem; }
.notice { border-left: 3px solid currentColor; padding: .6rem .9rem; margin: 0 0 1.4rem;
          background: rgba(128,128,128,.1); border-radius: 0 .3rem .3rem 0; font-size: .92rem; }
.notice.stop { border-color: var(--warn); color: var(--warn); }

/* Bottom line, literally up front: the four numbers that answer "anything happening?" */
.figures { display: grid; grid-template-columns: repeat(auto-fit, minmax(7.5rem, 1fr));
           gap: .7rem; list-style: none; margin: 0 0 2.2rem; padding: 0; }
.figures li { border: 1px solid var(--line); border-radius: .5rem; padding: .7rem .8rem; }
.figures b { display: block; font-size: 1.7rem; font-weight: 600; line-height: 1.1;
             font-variant-numeric: tabular-nums; }
.figures span { font-size: .78rem; color: var(--dim); }
.figures .zero b { color: var(--dim); font-weight: 400; }

h2 { font-size: .75rem; letter-spacing: .1em; text-transform: uppercase; color: var(--dim);
     margin: 2.2rem 0 .7rem; padding-bottom: .4rem; border-bottom: 1px solid var(--line); }
.day { font-size: .78rem; letter-spacing: .06em; text-transform: uppercase; color: var(--dim);
       margin: 1.6rem 0 .5rem; }
.day:first-of-type { margin-top: .8rem; }

/* One decision, one block: verdict, then what it was about, then why, then where. */
.row { border-left: 3px solid var(--drop); padding: .1rem 0 .1rem .85rem; margin: 0 0 1.4rem; }
.row.good { border-color: var(--good); }
.row.wait { border-color: var(--wait); }
.row.warn { border-color: var(--warn); }
.verdict { font-size: .72rem; font-weight: 700; letter-spacing: .09em; text-transform: uppercase;
           margin: 0 0 .15rem; color: var(--drop); }
.good .verdict { color: var(--good); }
.wait .verdict { color: var(--wait); }
.warn .verdict { color: var(--warn); }
.row h3 { font-size: 1rem; font-weight: 600; margin: 0 0 .2rem; line-height: 1.35; }
.why { margin: 0 0 .25rem; font-size: .92rem; }
.where { margin: 0; font-size: .78rem; color: var(--dim); word-break: break-word; }
details { margin-top: .5rem; }
summary { font-size: .78rem; color: var(--dim); cursor: pointer; }
pre { white-space: pre-wrap; word-break: break-word; background: rgba(128,128,128,.12);
      padding: .7rem; border-radius: .35rem; font-size: .82rem; margin: .4rem 0 0; }
.empty { color: var(--dim); margin: .8rem 0 0; }
code { font-size: .88em; background: rgba(128,128,128,.18); padding: .05rem .3rem;
       border-radius: .2rem; }
.more { margin: 2.5rem 0 0; font-size: .85rem; }
a { color: inherit; }
@media (prefers-color-scheme: dark) {
  :root { --good: #5cc97e; --wait: #e0a83a; --drop: #9a9a9a; --warn: #f0736a; }
}

/* The review page keeps the full card: exact text and evidence, never summarized. */
.ticket { border: 1px solid var(--line); border-radius: .5rem; padding: 1rem; margin: 1.5rem 0; }
.ticket h2 { font-size: 1.05rem; margin: 0 0 .3rem; text-transform: none; letter-spacing: 0;
             color: inherit; border: 0; padding: 0; }
.meta { font-size: .85rem; color: var(--dim); margin: .2rem 0 .8rem; word-break: break-all; }
h4 { font-size: .75rem; letter-spacing: .07em; text-transform: uppercase; color: var(--dim);
     margin: 1rem 0 .3rem; }
.source { font-size: .8rem; margin: .5rem 0; word-break: break-all; }
form { display: inline-block; margin: .4rem .5rem 0 0; }
textarea { width: 100%; min-height: 4rem; font: inherit; font-size: .9rem; }
button { font: inherit; padding: .4rem .9rem; border-radius: .35rem; cursor: pointer; }
.revise { display: block; margin-top: 1rem; }
'''


class Response:
    def __init__(self, status, body='', headers=None, content_type='text/html; charset=utf-8'):
        self.status, self.body = status, body
        self.headers = {'Content-Type': content_type, 'Cache-Control': 'no-store',
                        'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                        'Content-Security-Policy': POLICY}
        self.headers.update(headers or {})


def esc(value):
    return html.escape('' if value is None else str(value), quote=True)


def ago(seconds):
    """Elapsed time in the coarsest unit that is still true, for a line read at a glance."""
    seconds = max(0, int(seconds))
    for size, unit in ((86400, 'day'), (3600, 'hour'), (60, 'minute')):
        if seconds >= size:
            count = seconds // size
            return '%d %s%s ago' % (count, unit, '' if count == 1 else 's')
    return 'just now'


def folder(path):
    """'knowledge/Lessons/A long title.md' -> 'Lessons'. Falls back to the whole path."""
    parts = [part for part in (path or '').split('/') if part]
    return parts[-2] if len(parts) > 1 else (path or '')


def day_label(when, now):
    today = time.localtime(now).tm_yday
    day = time.localtime(when)
    if day.tm_yday == today and day.tm_year == time.localtime(now).tm_year:
        return 'Today'
    if (now - when) < 172800 and day.tm_yday == today - 1:
        return 'Yesterday'
    return time.strftime('%A %-d %B', day)


def read_secret(path):
    secret = Path(path).read_bytes().strip()
    if len(secret) < 32:
        raise ValueError('The dashboard action secret must be at least 32 bytes')
    return secret


class DashboardApp:
    def __init__(self, desk, allowed_login, public_origin, base_path='/knowledge-review', secret=b''):
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError('The dashboard needs a 32-byte or longer action secret')
        if not allowed_login or not public_origin.startswith('https://') or public_origin.endswith('/'):
            raise ValueError('Configure an allowed login and an exact https public origin')
        self.desk = desk
        self.allowed_login = allowed_login
        self.public_origin = public_origin
        self.base = '/' + base_path.strip('/')
        self.secret = secret

    # --- action tokens -------------------------------------------------------------

    def action_token(self, ticket, action, generation=None):
        """Authorizes one action on exactly the snapshot and retry generation displayed."""
        if generation is None:
            generation = self.desk.action_generation(ticket['id'])
        message = f'{ticket["id"]}:{action}:{ticket_fingerprint(ticket)}:{generation}'.encode()
        return hmac.new(self.secret, message, hashlib.sha256).hexdigest()

    def _token_ok(self, ticket, action, supplied, generation=None):
        expected = self.action_token(ticket, action, generation)
        return isinstance(supplied, str) and hmac.compare_digest(expected, supplied)

    # --- request handling ----------------------------------------------------------

    def handle(self, method, path, headers, body):
        lookup = {key.lower(): value for key, value in headers.items()}
        if lookup.get('tailscale-user-login', '') != self.allowed_login:
            return Response(403, self._page('Not authorized',
                                            '<p>This page is private to its configured Tailscale user.</p>'))
        split = urlsplit(path)
        route = split.path
        if route.startswith(self.base):
            route = route[len(self.base):] or '/'
        if not route.startswith('/'):
            route = '/' + route
        if route == '/style.css':
            return Response(200, STYLE, content_type='text/css; charset=utf-8')
        if route in ('/published', '/review'):
            if method != 'GET':
                return Response(405, self._page('Not allowed', '<p>This page only reads.</p>'),
                                {'Allow': 'GET'})
            done = (parse_qs(split.query).get('done') or [''])[0]
            return self._published() if route == '/published' else self._review(done)
        if route == '/':
            if method != 'GET':
                return Response(405, self._page('Not allowed', '<p>Use the buttons on the review page.</p>'),
                                {'Allow': 'GET'})
            done = (parse_qs(split.query).get('done') or [''])[0]
            return self._overview(done)
        parts = route.strip('/').split('/')
        if len(parts) != 3 or parts[0] != 'ticket' or parts[2] not in ACTIONS:
            return Response(404, self._page('Not found', '<p>No such review page.</p>'))
        if method != 'POST':
            return Response(405, self._page('Not allowed', '<p>Decisions are submitted with the page buttons.</p>'),
                            {'Allow': 'POST'})
        return self._mutate(parts[1], parts[2], lookup, body)

    def _mutate(self, number, action, headers, body):
        if headers.get('origin', '') != self.public_origin:
            return Response(403, self._page('Not authorized', '<p>This request did not come from the review page.</p>'))
        if headers.get('content-type', '').split(';')[0].strip() != FORM_TYPE:
            return Response(415, self._page('Unsupported', '<p>Submit the review form itself.</p>'))
        if len(body) > MAX_BODY:
            return Response(413, self._page('Too large', '<p>That submission was too large to accept.</p>'))
        if not number.isdigit():
            return Response(404, self._page('Not found', '<p>No such draft.</p>'))
        try:
            ticket = self.desk.ticket(int(number))
        except ValueError:
            return Response(404, self._page('Not found', '<p>No such draft.</p>'))
        fields = parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True)
        generation = self.desk.action_generation(ticket['id'])
        if not self._token_ok(ticket, action, (fields.get('token') or [''])[0], generation):
            return Response(403, self._page('Not authorized',
                                            '<p>That decision is not bound to this draft. Reload and try again.</p>'))
        instructions = (fields.get('instructions') or [''])[0]
        if action == 'revise' and not 1 <= len(instructions.strip()) <= 2000:
            return Response(400, self._page(
                'Instructions needed',
                '<p>Revision instructions must be nonblank and at most 2000 characters.</p>'
                f'<p><a href="{esc(self.base)}/">Back to the review page</a></p>'))
        key = 'dashboard:' + hashlib.sha256(self.action_token(ticket, action, generation).encode()).hexdigest()
        actor = {'channel': 'dashboard', 'login': self.allowed_login}
        with worker_lock(self.desk.store) as acquired:
            if not acquired:
                return Response(503, self._page('Busy', '<p>The hourly reviewer is running. Try again shortly.</p>'),
                                {'Retry-After': '30'})
            self.desk.act(int(number), action, instructions, key, actor)
        return Response(303, '', {'Location': f'{self.base}/?done={key}'})

    # --- rendering ------------------------------------------------------------------

    def _page(self, title, inner, subtitle=''):
        return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{esc(title)}</title>'
                f'<link rel="stylesheet" href="{esc(self.base)}/style.css"></head><body>'
                f'<h1>{esc(title)}</h1>'
                f'{f"<p class=\"asof\">{esc(subtitle)}</p>" if subtitle else ""}'
                f'{inner}</body></html>\n')

    def _receipt(self, key):
        if not key:
            return ''
        with self.desk.store.db() as db:
            row = db.execute("SELECT result FROM approval_actions WHERE key=? AND state='done'", (key,)).fetchone()
        if not row or not row['result']:
            return ''
        return f'<p class="notice">{esc(json.loads(row["result"])["notice"])}</p>'

    def _review(self, done=''):
        """Every draft still awaiting a decision, in full: exact text and every source.

        The gate normally empties this within a minute of a draft appearing, so this page
        is the exception path - what to read when the gate deferred or was turned off.
        """
        tickets = self.desk.pending_tickets()
        parts = [self._receipt(done)]
        if not tickets:
            parts.append('<p class="empty">No drafts are waiting.</p>')
        for ticket in tickets:
            parts.append(self._card(ticket))
        parts.append(f'<p class="more"><a href="{esc(self.base)}/">Back to the overview</a></p>')
        return Response(200, self._page('Knowledge review', ''.join(parts)))

    # --- overview --------------------------------------------------------------------

    def _overview(self, done=''):
        """The glance page: the numbers, then anything wanting a person, then what happened.

        Read top to bottom it answers three questions in order - is the vault growing, is
        anything stuck on me, and what did the gate decide - so a reader who stops after
        the first line has still learned the useful part.
        """
        now = time.time()
        counts = outcomes.tally(self.desk.store, now=now)
        parts = [self._receipt(done), self._stalled(now), self._figures(counts),
                 self._held(counts), self._waiting(), '<h2>Recent decisions</h2>', self._feed(now)]
        parts.append(f'<p class="more"><a href="{esc(self.base)}/published">'
                     'Everything published, with its exact text</a></p>')
        return Response(200, self._page('Knowledge vault', ''.join(parts), subtitle=self._asof(counts, now)))

    def _asof(self, counts, now):
        last = counts['last_decision']
        decided = f'Last decision {ago(now - last)}.' if last else 'Nothing decided yet.'
        waiting = counts['waiting']
        if not waiting:
            return decided + ' No drafts are waiting.'
        return decided + (' 1 draft is waiting on you.' if waiting == 1
                          else f' {waiting} drafts are waiting on you.')

    def _figures(self, counts):
        cells = ((counts['published'], 'notes published'),
                 (counts['decided_this_week'], 'decided this week'),
                 (counts['queued'], 'waiting for review'),
                 (counts['waiting'], 'waiting on you'))
        items = ''.join(f'<li{" class=\"zero\"" if not value else ""}>'
                        f'<b>{value}</b><span>{esc(label)}</span></li>' for value, label in cells)
        return f'<ul class="figures">{items}</ul>'

    def _stalled(self, now):
        """A paused reviewer looks exactly like a quiet week, so say which it is.

        Above the numbers rather than below them: while this is showing, the numbers are
        not standing still because nothing happened.
        """
        blocked = self.desk.store.get_meta(BLOCK_KEY)
        if not blocked:
            return ''
        since = blocked.get('since')
        when = (' for %s' % ago(now - since).removesuffix(' ago')) if since else ''
        return (f'<p class="notice stop">Review is paused{esc(when)}. '
                f'{esc(blocked.get("detail", "Cause unrecorded."))} '
                'Nothing is lost and nothing will publish until it clears.</p>')

    def _held(self, counts):
        """Held candidates are the one thing that is stuck and otherwise says nothing.

        They are not queued, not decided and not announced, so without a line here they
        are invisible until someone thinks to query the database.
        """
        held = counts['held']
        if not held:
            return ''
        subject = '1 candidate is' if held == 1 else f'{held} candidates are'
        return (f'<p class="notice">{subject} held and will not be reviewed again on their '
                f'own. Reopen one with <code>reviewer reopen &lt;id&gt;</code>.</p>')

    def _waiting(self):
        """Only rendered when it is true, so an empty section never costs a glance."""
        tickets = self.desk.pending_tickets()
        if not tickets:
            return ''
        rows = ''.join(
            f'<article class="row warn"><p class="verdict">Needs your decision</p>'
            f'<h3>{esc(ticket["snapshot"].get("title", ""))}</h3>'
            f'<p class="where">knowledge/{esc(ticket["snapshot"].get("target", ""))} '
            f'&middot; draft {ticket["id"]}</p></article>' for ticket in tickets)
        return (f'<h2>Waiting on you</h2>{rows}'
                f'<p class="more"><a href="{esc(self.base)}/review">'
                f'Read the exact text and decide</a></p>')

    def _feed(self, now, limit=40):
        decisions = outcomes.decisions(self.desk.store, limit)
        if not decisions:
            return '<p class="empty">Nothing has been decided yet.</p>'
        parts, heading = [], None
        for item in decisions:
            day = day_label(item['created'], now)
            if day != heading:
                heading, _ = day, parts.append(f'<p class="day">{esc(day)}</p>')
            parts.append(self._decision(item))
        return ''.join(parts)

    def _decision(self, item):
        """Verdict, title, why, then where - and nothing else above the fold.

        The destination is the title again with a folder and an extension around it, so
        only the folder earns a line here; the exact path goes with the exact text.
        """
        when = time.strftime('%H:%M', time.localtime(item['created']))
        by = 'the gate' if item['by'] == 'jev' else esc(item['by'])
        rows = [f'<article class="row {item["tone"]}">',
                f'<p class="verdict">{esc(item["verdict"])}</p>',
                f'<h3>{esc(item["title"])}</h3>']
        if item['why']:
            rows.append(f'<p class="why">{esc(item["why"])}</p>')
        rows.append(f'<p class="where">{esc(folder(item["path"]))} &middot; {when} &middot; by {by}</p>')
        detail = item['detail']
        rows.append(f'<details><summary>The exact text{" and the full verdict" if detail else ""}'
                    f'</summary><p class="where">{esc(item["path"])}</p>'
                    f'{f"<pre>{esc(detail)}</pre>" if detail else ""}'
                    f'<pre>{esc(item["note"])}</pre></details>')
        return ''.join(rows) + '</article>'

    def _published(self, limit=500):
        """Every note that reached the vault, newest first, with the text as saved.

        The overview shows the last few decisions of every kind; this is the archive
        behind it, and the only page that lists the whole vault. It publishes nothing and
        offers no controls; removing a note is a vault edit, not a dashboard action.
        """
        with self.desk.store.db() as db:
            rows = db.execute(
                "SELECT t.id, t.snapshot, t.result, t.created, a.actor FROM approval_tickets t "
                "LEFT JOIN approval_actions a ON a.ticket = t.id AND a.action = 'approve' "
                "AND a.state = 'done' WHERE t.state = 'published' "
                "ORDER BY t.created DESC, t.id DESC LIMIT ?", (limit,)).fetchall()
        parts = [f'<p class="more"><a href="{esc(self.base)}/">Back to the overview</a></p>']
        if not rows:
            parts.append('<p class="empty">Nothing has been published yet.</p>')
        for row in rows:
            view = json.loads(row['snapshot'])
            result = json.loads(row['result']) if row['result'] else {}
            actor = json.loads(row['actor']) if row['actor'] else {}
            who = esc(actor.get('channel') or 'unknown')
            when = time.strftime('%Y-%m-%d %H:%M', time.localtime(row['created'] or 0))
            path = esc(result.get('path') or ('knowledge/' + (view.get('target') or '')))
            parts.append(
                f'<article class="ticket"><h2>{esc(view.get("title", ""))}</h2>'
                f'<p class="meta">{path}<br>published {esc(when)} &middot; decided by {who}</p>'
                f'<details><summary>Exact published text</summary>'
                f'<pre>{esc(view.get("note", ""))}</pre></details></article>')
        return Response(200, self._page('Published notes', ''.join(parts),
                                        subtitle=f'{len(rows)} in the vault, newest first.'))

    def _card(self, ticket):
        view, number = ticket['snapshot'], ticket['id']
        generation = self.desk.action_generation(number)
        rows = [f'<article class="ticket"><h2>Draft {number}: {esc(view["title"])}</h2>',
                f'<p class="meta">Destination: knowledge/{esc(view["target"])}</p>',
                f'<h4>Review notes and limitations</h4><pre>{esc(view["reason"])}</pre>']
        if view['before'] is not None:
            rows.append(f'<h4>Exact current text</h4><pre>{esc(view["before"])}</pre>')
        rows.append(f'<h4>Exact proposed text</h4><pre>{esc(view["note"])}</pre>')
        rows.append('<h4>Evidence</h4>')
        for source in view['sources']:
            rows.append(f'<div class="source">{esc(source["path"])} '
                        f'lines {esc(source["start"])}-{esc(source["end"])}'
                        f'<pre>{esc(source["text"])}</pre></div>')
        for action, label in (('approve', 'Approve and publish'), ('reject', 'Decline')):
            rows.append(self._form(ticket, action, f'<button type="submit">{label}</button>', generation=generation))
        rows.append(self._form(ticket, 'revise',
                               '<label>What should change?<textarea name="instructions" maxlength="2000" '
                               'required rows="3"></textarea></label>'
                               '<button type="submit">Request changes</button>',
                               css=' class="revise"', generation=generation))
        return ''.join(rows) + '</article>'

    def _form(self, ticket, action, inner, css='', generation=None):
        return (f'<form method="post" action="{esc(self.base)}/ticket/{ticket["id"]}/{action}"{css}>'
                f'<input type="hidden" name="token" value="{self.action_token(ticket, action, generation)}">'
                f'{inner}</form>')


def serve(desk, config):
    """Runs the dashboard on the configured loopback address until stopped."""
    board = config['dashboard']
    app = DashboardApp(desk, board['allowed_login'], board['public_origin'],
                       board.get('base_path', '/knowledge-review'), read_secret(board['secret']))
    # Decisions made before the log existed are explained only by the messages they sent.
    # Recovering them once per start is idempotent and is the difference between a page
    # that accounts for the whole vault and one that starts from today.
    outcomes.backfill(desk.store)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'knowledge-review'
        sys_version = ''

        def _respond(self, method):
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = MAX_BODY + 1
            body = self.rfile.read(min(length, MAX_BODY + 1)) if length > 0 else b''
            response = app.handle(method, self.path, dict(self.headers.items()), body)
            payload = response.body.encode()
            self.send_response(response.status)
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self._respond('GET')

        def do_POST(self):
            self._respond('POST')

        def log_message(self, fmt, *args):
            pass

    os.umask(0o077)
    server = ThreadingHTTPServer((board['host'], board['port']), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
