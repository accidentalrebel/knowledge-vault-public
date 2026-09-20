# Read what the vault decided, on the private dashboard

The hourly reviewer turns candidates into drafts. Each new draft becomes an immutable
numbered ticket, and the automatic gate decides it - publish, narrow, or drop - usually
within the same minute. Telegram sends **one message per run**: how many decisions landed
and a link. It carries no draft text, no titles and no reasons, because everything worth
reading is on the page the link opens. Replies to Telegram change nothing.

## Open the dashboard

<https://your-host.your-tailnet.ts.net/knowledge-review/>

Reachable from your tailnet only, as the Tailscale user `you@github`. The service itself binds `127.0.0.1:8902`; Tailscale Serve terminates HTTPS and supplies the `Tailscale-User-Login` identity header. A request without that header, or with another user's login, gets 403 — including a request made directly to the loopback port.

Three pages, all server-rendered, all escaped, no scripts, no remote assets, `default-src 'none'`.

### `/` — the overview

The glance page, read top to bottom:

1. **One line** saying when the last decision landed and whether anything waits on you.
2. **Four numbers**: notes published, decided this week, waiting for review, waiting on you.
3. **A held line**, only when candidates are stuck — they are neither queued nor decided, so nothing else would ever mention them.
4. **Waiting on you**, only when the gate deferred or is off. Titles and destinations, with a link to the full cards; you cannot decide from here.
5. **Recent decisions**, newest first, grouped by day. Each one leads with its verdict — *Published*, *Sent back*, *Dropped*, *Needs a look* — then the title, then one plain sentence of why, then the destination, time and who decided. The exact text and the full threshold verdict are behind a disclosure, one click away.

The verdict names what happened, not what was asked: an approval that hit a changed destination reads *Needs a look*, never *Published*.

### `/review` — the drafts still awaiting a decision

The full card for each: destination, reviewer notes, the exact current text when the draft edits an existing note, the exact proposed text, every evidence file with its line range and excerpt, and the three buttons. Normally empty. This is the page to read when the gate deferred because TypeSafe was unreachable, or when the gate is turned off.

### `/published` — the archive

Every note in the vault, newest first, with the text exactly as saved. Read-only; removing a note is a vault edit, not a dashboard action.

## Where the reasons come from

`approval_actions` records that a decision happened and what it did to the ticket. It never kept *why* — that lives in the gate's verdict, and before the dashboard existed it survived only inside the Telegram message that carried it. `jev_outcomes` is that missing column: one row per gate decision, written in the run that made it.

Decisions made before that table existed are recovered once, at service start, from the notices they actually sent. Those notices kept only the short phrase, so those rows offer no "full verdict" disclosure rather than inventing one. A decision made by hand on the dashboard has no reason at all and simply shows none.

## Decide

| Button | Effect |
| --- | --- |
| Approve and publish | Recheck the candidate, evidence hashes and destination revision, then save only the exact displayed text. |
| Decline | Terminal for that draft; no publication, no reminder. |
| Request changes | Invalidate that ticket and queue your instructions for the next eligible hourly review. |

An approval authorizes one ticket number bound to one frozen snapshot. Each button carries an HMAC action token over the ticket number, the action, and the snapshot fingerprint; a request with a missing, altered, or other-ticket token is refused. Mutations require `POST`, the exact `Origin` `https://your-host.your-tailnet.ts.net`, and a form body of at most 8192 bytes. A `GET` on an action path returns 405 and changes nothing. Requested changes need nonblank instructions of at most 2000 characters.

After a decision the page redirects to the overview and shows the outcome for that action. Submitting the same form twice replays the recorded outcome instead of acting again, so a double tap or a refresh cannot publish twice.

Decisions make no model calls. Revisions share the existing rolling 24-hour reservation ceiling and the same provider fallback order. The reviewer receives your request, the prior draft, refreshed evidence, and current vault context. Any resulting draft needs fresh approval. If review rejects or holds the requested change, the service sends one Telegram result explaining why.

## Save and failure behavior

- A ticket freezes the text, destination, source hashes, and the destination revision it expects. Changed sources or a changed destination requeue the candidate for review and require a new approval; the ticket becomes `stale` and nothing is written.
- Publication uses Khoj's existing `VaultStore` source backend, cooperating locks, revision checks and backups. It verifies the saved text. Obsidian edits that bypass those locks retain the backend's existing simultaneous-save limitation.
- Source save and search indexing are separate. The normal Khoj sync interval remains about a minute after a cycle finishes; failures can delay search further. An agent given the path can read the saved source immediately.
- Every decision is recorded under a durable action key before it runs. A duplicate request returns the stored outcome. An action interrupted mid-flight is reported as interrupted and is never silently repeated; decide again from a reloaded page.
- An uncertain save stays `publish_unknown` and blocks further decisions on that ticket. The next action reconciles it against the destination first, so a note that really was saved is reported as published rather than declared rejected.
- A failed Telegram notification does not block anything. The decision already happened and the dashboard already shows it; the undelivered message is marked `delivery_unknown` and is not resent automatically.
- One run's decisions are announced under a key derived from the exact set of action keys in it, so a retried run replays that key instead of sending a second message.
- The dashboard and the hourly reviewer share the existing nonblocking worker lock. A decision submitted while the reviewer runs returns 503 with `Retry-After: 30` and changes nothing.

## Inspect and operate

```bash
python3 /home/you/development/knowledge-vault/ops/knowledge-review status
python3 /home/you/development/knowledge-vault/ops/knowledge-review ticket 12
systemctl --user status knowledge-vault-dashboard.service
journalctl --user -u knowledge-vault-dashboard.service -n 20 --no-pager
systemctl --user restart knowledge-vault-dashboard.service
systemctl --user status knowledge-vault-telegram.timer
journalctl --user -u knowledge-vault-telegram.service -n 20 --no-pager
tailscale serve status
```

`ticket` reads the frozen content and lifecycle without Telegram credentials. SQLite state lives in `.state/reviewer/queue.sqlite3`; keep it and the attempt logs private and outside the indexed vault. `approval_tickets` holds ticket lifecycle, `approval_actions` holds each recorded decision and its outcome, `jev_outcomes` holds the reason behind each gate decision, and `approval_outbox` holds outbound notifications. `approval_commands` retains the retired Telegram command history for inspection and is no longer written.

The dashboard's HMAC secret lives in `.state/dashboard.secret`, mode 0600, outside the vault. Replacing it invalidates the tokens on any page a browser still has open; reload the page and decide again. The Telegram API token stays in the existing `~/.claude-shared/telegram.env`; systemd sources it without putting it in command arguments or logs.

The dashboard unit runs with `UMask=0077`, `NoNewPrivileges=true`, `ProtectSystem=strict`, `ProtectHome=read-only`, `RestrictSUIDSGID=true`, `LockPersonality=true`, `RestrictAddressFamilies=AF_INET AF_UNIX`, and write access only to `.state/` and `vault/`. It has no write access to the agent profile directories, so it cannot consume a provider allowance. `PrivateDevices=true` is deliberately absent: a systemd **user** unit cannot apply it and fails to start with `218/CAPABILITIES`.

Sleeping or offline machines cannot serve the dashboard or run the timer; the user manager resumes both when it runs again. This workflow adds no power or login-lingering configuration.
