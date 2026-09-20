# Caller vault pointers

The planning, issue and pull-request skills each make one vault recall at the point where prior
cross-project knowledge could change their output. Those skills are installed, not vendored, so
their files live outside this repository. The exact sentence each must carry is committed here so
the requirement is reviewable from this repository alone, and `tests/test_instructions.py` asserts
the deployed files against it.

Keep each pointer to one sentence. The `knowledge-vault` skill owns query limits, evidence handling
and candidate capture; a caller that restates any of that has duplicated the procedure. That applies
to capture pointers too: a caller says *when* to capture, never how many candidates or what qualifies.

| Skill | Section | Required sentence |
| --- | --- | --- |
| `arebel-plan-like-a-good-dev` | `## 2. Right-size the work` | Before finalizing a plan for an existing project, use the `knowledge-vault` skill once on the repository plus the task's problem, unless this task or its handoff already has a vault result. |
| `create-github-issue` | `## 2. Rule out a duplicate` | Before the duplicate search, use the `knowledge-vault` skill once on the repository plus the observed symptom, unless this task already has a vault result. |
| `create-github-pr` | `## 5. Compose` | Before composing, use the `knowledge-vault` skill once on the repository plus this change's purpose, unless this task already has a vault result. |

## Capture pointers

A caller that learns something durable while working says so once, at the point where the work is
finished and the findings exist. The recall pointer and the capture pointer are separate sentences
in separate sections; a caller may carry one of each and no more.

| Skill | Section | Required sentence |
| --- | --- | --- |
| `arebel-plan-like-a-good-dev` | `## 3. Execute the approved plan` | After verification, use the `knowledge-vault` skill once to capture what planning and execution taught about this repository, unless the task taught nothing reusable. |
| `arebel-executing-plans` | `### Step 4: Capture what you learned` | After wrap-up is resolved, use the `knowledge-vault` skill once to capture what execution taught, unless nothing nonobvious surfaced. |
