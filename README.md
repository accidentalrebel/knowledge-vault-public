# Knowledge vault — an agent-written notebook with an automatic editor

> **Redacted snapshot, not a maintained project.**
> Taken from a private repository at commit `f0538dc` on 2026-09-20. Paths, hostnames and
> logins are placeholders. The notes the system produced are **not** included — only the
> machinery that produces them. Issues and pull requests are not watched. It is published
> because someone might find the design useful, not because it is a product.

Agents I run across a dozen repositories occasionally learn something worth keeping — a
measured constraint, a settled decision, a failure mode that cost an afternoon. This is
where that goes, and more importantly, this is what stops it filling with slop.

Notes live in an Obsidian vault. [Khoj](https://github.com/khoj-ai/khoj) indexes it so
agents can search it semantically. Everything in between is the interesting part.

## The pipeline

```
agent submits a candidate  →  hourly editorial review  →  a frozen ticket
                                                              ↓
                              published to the vault  ←  the gate decides
```

**Submission is cheap and unattended.** An agent finishing a task may submit one candidate
with the evidence it already gathered. Submitting is not publishing; it joins an unindexed
queue.

**An hourly worker reviews a batch.** A model — Claude, with Codex and GLM as fallbacks —
judges each candidate against the evidence, narrows the claim to what that evidence
actually supports, and writes a draft. It never writes to the vault.

**The gate decides.** Each accepted draft is frozen into an immutable ticket, then judged
by [TypeSafe's Jev](https://typesafe.ai) against four questions. This replaced me clicking
approve on a phone, which is a job nobody does well at the fiftieth draft.

**Publication goes through Khoj's own store** — revision checks, cooperating locks,
backups, and a read-back that verifies the exact saved bytes.

## The gate

`reviewer/jev_questions.json` holds four questions, each scored on a five-level rubric:

| question | asks |
|---|---|
| `factual_support` | do the cited excerpts actually support the claim, at the scope stated? |
| `durability` | will this still be useful after the task that produced it? |
| `reference_value` | could a future reader use this without reconstructing the conversation? |
| `sensitive_information` | what is the most sensitive thing anywhere in the packet? |

Routing uses **probability mass, never the expected score**. A note publishes only when
`factual_support` puts ≥0.25 on level 4 *and* ≥0.85 on levels 3–4, with durability and
reference value each ≥0.80 on levels 3–4. Sensitivity blocks at ≥0.01 on level 4, ≥0.05 on
3–4, or ≥0.35 on 2–4. A draft that misses gets sent back to be narrowed, twice, then
dropped.

Two design notes that took a while to get right:

**Credentials are not the model's job.** A deterministic scanner (`reviewer/secrets.py`)
runs over the whole packet *before* anything leaves the machine. Its false positives were
instructive: a CamelCase API error string, a hex digest quoted mid-sentence, and once, the
system feeding its own task key into its own entropy detector.

**The sensitivity rubric started wrong.** Level 2 originally read as "any internal
operational detail", which classified me describing my own laptop as a disclosure and
blocked every security-operations lesson worth keeping. It now means *another party's*
confidential operations, with the instruction: judge whether disclosing the material would
cause harm, not whether its subject matter is security-related.

**It fails closed.** If the gate is unreachable, the ticket waits. Nothing publishes on an
outage.

## Duplicate detection

The reviewer used to receive every note in the vault so it could answer "is this already
covered?" — measured at 88% of the packet, resent hourly, with a hard size ceiling that
stopped review once the vault outgrew it.

Khoj already indexes the vault, so `reviewer/dedup.py` asks it. The packet now carries an
inventory of every note plus the full text of only the ones a candidate could collide
with. Three things keep that as strong as sending everything: the inventory is read from
disk so the index being behind cannot hide a note; notes the sync manifest reports as
unindexed are supplied in full because search cannot see them; and a search outage pauses
review rather than reporting no duplicates.

## Layout

```
reviewer/     the engine — gate, questions, scanner, dedup, publication, dashboard
ops/          systemd units, config, a CLI for inspecting the queue
agent/        the skill agents use to submit, and the policy they follow
docs/         how the hourly review, capture, and dashboard reporting work
tests/        521 tests
```

## Running the tests

```bash
KHOJ_SRC=/path/to/khoj-lab/src python3 -m pytest -q
```

The approval, dashboard and CLI tests construct a real publisher, so they need a
[khoj-lab](https://github.com/khoj-ai/khoj) checkout providing `khoj_mcp`; point
`KHOJ_SRC` at yours. Tests comparing installed agent skills skip unless
`KNOWLEDGE_SKILLS_DIR` points at a directory containing them. Everything else is standard
library only — the reviewer runs on the system interpreter by design.

## What was redacted

Home paths, a Tailscale hostname and login, one LAN address, and a handful of private
project names that appeared in test fixtures and examples. `ops/reviewer.json` is an
example, not a runnable configuration.

## License

MIT. See [LICENSE](LICENSE). No warranty and no support — the license says so, and so do I.
