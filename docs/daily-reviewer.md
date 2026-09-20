# Hourly candidate review

The reviewer saves evidence-backed decisions about possible knowledge-base additions. Accepted and updated notes are **drafts**, available through `show`. An automatic gate then decides each draft - publish, revise or drop - and the [review dashboard](telegram-approval.md) informs you of the outcome rather than asking you to approve it. The model-review worker itself never writes to `vault/`.

## Operation

The systemd user timer checks about an hour after the previous check finishes. The worker reviews **one candidate per hour**. A rolling 24-hour ceiling of 24 reservations sits above that, so the hourly reservation is what actually governs the rate; the ceiling was lowered to three while a human approved each draft by hand and was raised when the automatic gate took that over. Each accepted draft becomes a ticket at the next minute-based check and is decided in the same run. One Telegram message per run reports how many decisions landed and links to the dashboard; the decisions themselves, with their reasons, are on the [dashboard overview](telegram-approval.md). Empty queues and checks before the next permitted time make no model calls.

The queue records a reservation before calling a model. A provider fallback chain counts once; a later retry or requested revision uses another reservation. Rejected, uncertain, invalid and interrupted reviews keep their reservation. Empty, stale-source and no-provider checks consume none. `status.review_budget` shows used and remaining capacity and the next permitted run.

Existing daily-batch usage is carried into the new allowance from its saved timestamp and candidate packet. If that packet is missing or inconsistent, migration conservatively counts all three slots until the old reservation expires. Qualifications are separate maintenance checks and do not consume candidate slots.

The fallback order is personal Claude → personal Codex → personal GLM → Ollama. Each configured provider must first pass the same six-case editorial smoke test. A different model, changed provider configuration, or changed reviewer code invalidates that qualification. This small test is evidence of basic suitability, not proof of general review accuracy. Actual model names and usage are retained in private attempt logs when the provider reports them; aliases may change upstream without a local configuration change.

Only authentication, quota, connection, overload, and timeout failures permit fallback. A valid rejection or uncertain verdict finishes the review. Malformed output is held for inspection; it does not get another provider. Unavailable providers receive a 24-hour cooldown. There is no reliable quota preflight: subscription calls consume the same allowance used for ordinary work. OpenRouter has no adapter and cannot be enabled in the config.

A review has a 600-second total deadline and each provider gets at most 180 seconds. The service has a 630-second hard stop and kills its process group. Claude gets at most three turns and no tools; Codex's execution tools, hooks, plugins, apps, MCP config, user instructions, and web access are disabled. The environment selects the personal profile and drops inherited API-billing credentials. GLM explicitly loads its personal `.api_key` without putting the token in arguments or logs. Ollama uses the configured private/tailnet endpoint and only qualifies if its selected model is reachable and passes.

## Submit a candidate

The current capture policy was installed on 2026-09-18. A completing agent submits up to three candidates, one per distinct finding, from evidence it already gathered, on its own judgment and without asking. Most substantial tasks produce at least one; submitting is cheap and the review stage is the gatekeeper. Queue submission is not vault publication: the candidate lands unindexed, and an automatic gate decides the resulting draft. Reload existing agent sessions to pick up the current skill and standing policy.

Save this shape to a local JSON file, replacing the task key, text, source path, and line range with real evidence:

```json
{
  "task_key": "project/session-id/completed-task",
  "title": "Short descriptive subject",
  "category": "service",
  "claim": "A concise supported fact or decision, including its important qualifiers.",
  "why_useful": "The future task this helps and what re-discovery it avoids.",
  "sources": [
    {"path": "/home/you/development/project/docs/architecture.md", "start": 10, "end": 18}
  ]
}
```

Categories: `project`, `service`, `decision`, `lesson`. A candidate needs one to three existing source excerpts. Each candidate needs its own `task_key`: reusing one returns the earlier candidate instead of creating another, so several findings submitted under one key silently collapse into the first. Evidence must be a text source file from the allowlist in `reviewer/store.py` (`EVIDENCE_SUFFIXES`), which covers prose, config and the common languages; binaries and unknown formats are refused. The queue snapshots their text, paths, line ranges, and full-file hashes. It rejects out-of-root, hidden, unsupported, oversized, or invalid-line-range sources. Prefer retained project paths; temporary worktree paths can disappear before review. After a merge, cite the matching passage in the retained checkout. This is a bounded intake contract, not a secret detector: the author must exclude secrets and private material unrelated to the task.

```bash
python3 /home/you/development/knowledge-vault/ops/knowledge-review submit /absolute/path/candidate.json
python3 /home/you/development/knowledge-vault/ops/knowledge-review status
python3 /home/you/development/knowledge-vault/ops/knowledge-review show CANDIDATE_ID
```

The inbox is `.state/reviewer/queue.sqlite3`, outside the Khoj-indexed `vault/`. Exact duplicate candidates return the earlier ID. Reusing a task key returns the earlier candidate instead of creating another. At 100 pending candidates, new intake stops until the backlog is inspected. This is not a global guarantee that agents choose truthful task keys; the capture instructions must use a stable task identity.

## Inspect results and failures

`status` shows each candidate ID and state, attempt counts, the next permissible run, provider cooldowns, and current qualification. `show` returns the original candidate, frozen evidence, and exact stored decision. Accepted and updated drafts can be inspected there and presented for the normal vault-write approval workflow.

| State | Meaning | Next action |
| --- | --- | --- |
| `pending` | Awaiting an eligible review | Let the scheduler run; check eligibility if it stays queued. |
| `accepted` / `updated` | Saved proposed note | Expect a dashboard ticket and a Telegram link after the next check, or inspect with `show`. |
| `published` | Human-approved source text was saved | Read the source directly; normal sync handles search. |
| `declined` | The draft was declined at the approval stage, by the gate or by you on the dashboard. The deciding actor is recorded on the action, not in this state name. | Leave it recorded; no reminders. |
| `rejected` | No worthwhile supported addition | Leave the decision recorded; no automatic resubmission. |
| `uncertain` | Evidence or authority needs clarification | Investigate only if the task merits it. |
| `held_stale` | A source changed or disappeared | Inspect the evidence and candidate before undertaking a new follow-up task. |
| `held_invalid` | Provider output broke the review contract | Inspect the named attempt; fix the adapter or prompt before another deliberate attempt. |
| `held_interrupted` | A previous process died during review | Inspect its attempt; the worker will not silently pay for it again. |

Held and finished candidates are not routinely requeued. A dashboard request for changes, or a stale draft discovered before publication, explicitly reopens review using refreshed evidence. Revisions remain subject to the rolling 24-hour allowance. Other intentional follow-up tasks with new evidence may submit a new candidate and distinct task key. Keep the old record for context; no automatic rejection purge runs.

Private attempt directories contain packets, prompts, raw provider output, failure information, and structured decisions. Files may contain sensitive project excerpts, so keep `.state/` out of Git and external sync. Auth status may include account identity; credentials themselves are never deliberately logged.

For duplicate comparison the worker sends an inventory of every note — path and title, read from disk — plus the full text of only the notes a candidate could collide with, chosen by a Khoj search on its title and claim. Measured on the live vault, that is 15 notes and 31,712 bytes where the whole vault was 44 notes and 75,096 bytes, and it stays bounded as the vault grows instead of scaling with it.

Three properties keep the guarantee as strong as sending everything. The inventory comes from the filesystem, so no note can be missing merely because the index has not seen it. Notes the sync manifest reports as unindexed, or indexed under a stale digest, are supplied in full regardless, because search cannot find them. And if Khoj cannot answer, review pauses with `context_blocked`; it does not assume there are no matches.

Recall was measured before this replaced the whole-vault packet: querying each of the 44 notes by its own title alone returned it at rank 1 in 44 of 44 cases, and every candidate previously rejected as a duplicate received the note it duplicated as its nearest supplied note. Remove the `dedup` section from `ops/reviewer.json` to fall back to the whole-vault packet and its 128,000-byte ceiling.

A review is checked against the vault again when it returns: the path-to-digest map taken when the packet was built is compared to the vault as it stands, so a note added, removed or edited while the model was thinking makes the comparison stale even when that note was never in the packet. The code validates cited quotations and update targets. Whether every claim is actually supported remains the model's editorial judgment, followed by human publication review. The prompt targets 80–180 words for new notes plus concise provenance, and one or two sentences of review explanation. Existing supported content is preserved when an update needs more space; drafts are never truncated after review.

Before review and after each provider attempt, changed source files or vault contents are held. Saved decisions describe their recorded snapshot: after review, inspect current sources again before publishing. Current commands/configuration/status remain authoritative in the repository. Khoj synchronization remains on its existing interval; the queue is never indexed.

## Provider qualification

These commands consume personal usage allowance. They are explicit maintenance operations, separate from the daily candidate budget, capped at one attempt per provider per 24 hours:

```bash
python3 /home/you/development/knowledge-vault/ops/knowledge-review qualify claude
python3 /home/you/development/knowledge-vault/ops/knowledge-review qualify codex
python3 /home/you/development/knowledge-vault/ops/knowledge-review qualify glm
python3 /home/you/development/knowledge-vault/ops/knowledge-review qualify ollama
```

Qualification tests a useful new capability, an unsupported fixed-release claim, a duplicate, an unapproved proposal incorrectly described as a decision, a supported update to an existing note, and conflicting records that require an uncertain verdict. It uses synthetic evidence and no production note writes. Unreachable providers stay ineligible; no automatic paid fallback follows a qualification test.

## Scheduler and verification

```bash
systemctl --user status knowledge-vault-review.timer
systemctl --user list-timers knowledge-vault-review.timer
journalctl --user -u knowledge-vault-review.service -n 20 --no-pager
systemctl --user start knowledge-vault-review.service
systemctl --user disable --now knowledge-vault-review.timer
```

Three units cooperate: `knowledge-vault-review.timer` runs the hourly model review, `knowledge-vault-telegram.timer` turns accepted candidates into tickets and sends the dashboard link each minute, and `knowledge-vault-dashboard.service` serves the approval page continuously on `127.0.0.1:8902`. All three share one nonblocking worker lock, so none of them overlaps another.

An explicit service start still respects both review limits. Stop the timer with the last command to pause scheduling. The timer starts checking after the user manager starts and schedules the next check one hour after each completed check. This avoids calendar jitter skipping a run because the previous start was slightly late. This installer does not change login lingering or machine power settings.

From the repository, run `python3 -m unittest discover -s tests -v`. Tests use temporary sources and SQLite state, plus real short child processes for subprocess behavior. They do not call external models.

Two dependencies live outside this repository and are not vendored:

- The approval, dashboard, Telegram and some CLI tests construct a real `Publisher`, which imports `khoj_mcp` from the checkout named by `publication.khoj_source` in `ops/reviewer.json`. Without that checkout those tests error at import. This is the same backend the service publishes through, so the tests exercise real locking, backups and revision checks rather than a stand-in.
- `tests/test_instructions.py` asserts the deployed instruction files for the installed caller skills. It reads `KNOWLEDGE_SKILLS_DIR`, defaulting to `~/.claude-shared/skills`, and skips with an explanatory message when those files are absent rather than failing a clean checkout. CLI switches follow installed local help; Ollama's structured `/api/chat` request follows its [official API documentation](https://docs.ollama.com/api/chat).
