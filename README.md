# ai-harness

`ai-harness` is a small, standalone controller for one adversarial cycle between Claude Code and
Codex CLI. It works from any trusted local Git repository without installing files into that repo.

For implementation tasks it:

1. asks the primary model family for a repository-grounded plan;
2. asks the other family to adversarially review that plan;
3. returns the review to the primary family for a disposition-complete revision;
4. implements the revised plan in a persistent linked worktree;
5. runs the revised plan's verification commands itself;
6. commits the verified diff and convenes the review council on it locally;
7. pushes the harness branch and opens a draft pull request; and
8. publishes the already-validated implementation review on that exact PR head.

That entire path is one workflow started by one command. A genuinely blocking planning question is
the only intentional human pause; answering it resumes the same run at the exact stopped stage.
With `--slack` that pause becomes a direct-message conversation the run continues from on its own.

For pull requests it convenes a **review council**. It creates a detached worktree at the exact PR
head, reviews the exact three-dot diff, asks a review lead which specialties the diff actually
needs, runs those specialists in parallel against separate charters, and asks the lead to group and
filter what they found. By default it publishes one GitHub `COMMENT` review with validated inline
locations plus a summary.

## Install

Requires Python 3.11+, `uv`, Git, GitHub CLI, Claude Code with safe-mode/structured-output support,
and Codex CLI with `exec --output-schema` and sandbox support.

Install the command once in editable mode so changes to this checkout are immediately available:

```sh
uv tool install --editable "$HOME/Developer/Personal/ai_harness"
```

Then validate local CLI compatibility without spending model allowance:

```sh
ai-harness doctor
```

`doctor --deep` performs a real structured-output call to both providers and consumes allowance:

```sh
ai-harness doctor --deep
```

If multiple Claude installations exist, the harness selects the first one that actually exposes the
required flags. Override discovery with `AI_HARNESS_CLAUDE` or `AI_HARNESS_CODEX`.

## Progress and model configuration

Every task and PR-review workflow writes immediate progress to stderr with an `[ai-harness]` prefix.
The first line shows the resolved primary family, both model names, Codex reasoning level, and the
per-agent timeout. Subsequent lines announce every model handoff and controller stage at start and
completion, including elapsed time and useful result counts. Verification commands, worktree setup,
artifacts, GitHub publication, pauses, and final completion are also reported. Provider output stays
captured in the run artifacts rather than being mixed into the terminal log.

Model stages are bounded by wall-clock time, not tool-turn count. The selected profile's
`timeout_seconds` applies to each model invocation; `--timeout 900` sets a 15-minute per-stage cap.
Claude may use as many tool round trips as it needs inside that window, subject to the separate
configured spend limit.

Inspect the resolved configuration without making a model call:

```sh
ai-harness config
ai-harness config --profile claude-opus
```

The root [`council-profiles.json`](council-profiles.json) file is the editable source of truth for
named profiles. Select one per run with `--profile NAME`, or set `AI_HARNESS_PROFILE`. The profile's
`provider` is the primary family, and there is no way to override it independently: swapping which
family plans and implements means selecting a different profile. A profile also chooses that
family's model and effort, the timeout, and an optional Claude fallback model. The other family
participates as the adversary using its configured counterpart model.

A profile may also pin the adversarial family instead of accepting its defaults, and opt into a
final feedback pass:

| Profile field | Effect |
|---|---|
| `critic_model` | Model the adversarial family uses; defaults to that family's counterpart |
| `critic_effort` | Reasoning effort for the adversarial family |
| `critic_fast` | Request Codex's `priority` (fast) speed tier for adversarial stages |
| `apply_review` | After publishing the implementation review, have the primary family apply its findings and update the pull request |

Configuration resolves in this order: command-line flags, `AI_HARNESS_*` environment variables,
the selected profile, then built-in library defaults. The common settings are:

| Setting | CLI flag | Environment variable | Default |
|---|---|---|---|
| Named profile | `--profile` | `AI_HARNESS_PROFILE` | `codex-balanced` |
| Claude model | `--claude-model` | `AI_HARNESS_CLAUDE_MODEL` | `sonnet` |
| Codex model | `--codex-model` | `AI_HARNESS_CODEX_MODEL` | `gpt-5.6-terra` |
| Codex reasoning | — | `AI_HARNESS_CODEX_REASONING` | `medium` |
| Codex fast speed tier | — | `AI_HARNESS_CODEX_FAST` | selected profile |
| Apply PR review feedback | — | `AI_HARNESS_APPLY_REVIEW` | selected profile |
| Per-agent timeout | `--timeout` | `AI_HARNESS_TIMEOUT` | selected profile |
| Parallel council specialists | — | `AI_HARNESS_COUNCIL_WORKERS` | `3` |
| Explicit council member | `--reviewer` | — | review lead routes |
| Every council member | `--all-reviewers` | — | review lead routes |
| Ask planning questions on Slack | `--slack` | `AI_HARNESS_SLACK` | off |
| Slack user to ask | — | `AI_HARNESS_SLACK_USER` | none |
| Total Slack wait per question set | `--slack-wait` | `AI_HARNESS_SLACK_WAIT` | `1800` |
| Run-wide Slack spend cap | — | `AI_HARNESS_SLACK_BUDGET_USD` | `5.00` |
| Clarification turns per question | — | `AI_HARNESS_SLACK_MAX_CLARIFICATIONS` | `3` |
| Minimum Slack poll interval | — | `AI_HARNESS_SLACK_POLL_SECONDS` | `20` |

For example, select Codex as the primary planner/implementer and preview the resolution:

```sh
ai-harness config --profile codex-deep
```

## Task usage

From any target repository, one line runs planning, adversarial plan review, revision,
implementation, verification, commit, adversarial implementation review, push, draft-PR creation,
and review publication:

```sh
ai-harness "Add bounded retries to the upload worker and test the exhausted path"
ai-harness --profile codex-deep --timeout 1200 "Refactor the parser without changing its API"
```

The default `codex-balanced` profile uses Codex `gpt-5.6-terra` with medium reasoning as the primary
family and Claude Sonnet as the adversarial family. Swap the complete primary profile with
`--profile`, or override individual values with model flags and `AI_HARNESS_*` variables.

The `claude-fable` profile runs the same workflow with Claude Fable at high effort as the primary
family and Codex `gpt-5.6-sol` at `xhigh` on the fast speed tier as the adversary. It sets
`apply_review`, so the run does not stop at the published review: Fable applies the findings,
the plan's verification commands re-run, and the fix lands as a second commit on the same branch,
updating the draft pull request in place. Findings Fable argues down are recorded in that stage's
`notes` rather than acted on.

```sh
ai-harness --profile claude-fable "Add bounded retries to the upload worker"
```

An `@path` argument is copied to a neutral, checksummed run artifact and explicitly given to each
stage. This prevents either CLI from interpreting the original `@` token itself:

```sh
ai-harness "Refactor this narrowly" @.claude/skills/refactor/SKILL.md
```

The caller checkout may be dirty. The harness warns, lists excluded paths, and creates its worktree
from `HEAD`; it never mixes uncommitted caller changes into the run. The caller must be on a named
branch, and the repository must have a pushable `origin` plus working `gh` authentication. A
successful run leaves a clean, committed `ai-harness/<run-id>` branch in a retained worktree at:

```text
<repo-parent>/.ai-harness-worktrees/<repo>/<run-id>
```

Repositories that need ignored build inputs before planning can explicitly enable a GNU Make
bootstrap. Put the exact marker in the top-level `GNUmakefile`, `makefile`, or `Makefile`; the target
itself may be declared there or in an included file:

```make
# ai-harness: worktree-setup

worktree-setup:
	# install dependencies or copy ignored inputs
```

The harness passes `ENV_SOURCE=<caller-checkout>/.env` and runs the target under the configured
per-stage timeout. It never evaluates Make syntax while checking for the opt-in marker.

The branch is pushed, its draft PR URL is printed, and the council's implementation review is
published against the exact commit. It is the same council described under
[Pull request usage](#pull-request-usage), on the local branch diff instead of a PR.

To stop after verified implementation and leave the changes uncommitted locally, opt out
explicitly:

```sh
ai-harness --local-only "Experiment with the parser without opening a PR"
```

## Pull request usage

```sh
ai-harness /review 16 @.claude/skills/refactor/SKILL.md \
  "Don't overengineer, but be adversarial to find bugs before they happen"
```

Publishing is the default. To render and validate the final review locally:

```sh
ai-harness --no-publish /review 16 "Look for latent correctness bugs"
```

### The review council

Six specialists share one shared review contract and one output schema, and each has its own
charter:

| Member | Owns |
|---|---|
| `correctness_reviewer` | the premise of the change, root cause versus symptom, test integrity |
| `refactorer` | structure, duplication, YAGNI, misleading abstractions, changed prose |
| `security_reviewer` | secrets, credential and `PATH` boundaries, sandbox arguments, injection |
| `performance_expert` | async and threaded paths, deadlines, fan-out, measured budgets |
| `resumability_reviewer` | resume keys, idempotency, atomic writes, locks, recovery, cleanup |
| `contract_reviewer` | schemas, APIs, migrations, config, and the tables that document them |

`correctness_reviewer` and `refactorer` always run and cannot be removed. One narrow deterministic
trigger adds `security_reviewer` when the diff touches `.github/workflows/` or a path naming a
secret, credential, token, or auth. Everything else is decided by the **review lead**, which reads
the diff and the changed-path list and may request at most three further specialists, each citing
paths that are actually in the diff. It can add members; it can never remove one.

Specialists run in parallel — `AI_HARNESS_COUNCIL_WORKERS=1` makes them sequential — with families
alternating across the roster, so every council contains both Claude and Codex. The review lead runs
on the primary family; the members that land on the adversarial family use that family's model and
effort, including a profile's `critic_model` and `critic_effort`. Each returns at most five
findings, each with a severity, a confidence, an exact changed-line location, and a verification
that would falsify it. A specialty that does not apply abstains rather than reaching for something
to say.

The lead then consolidates. It receives the findings as a manifest of untrusted claims identified
as `<reviewer>:<position>`, and must dispose of every one exactly once — into an accepted group or
a dismissed group with one of `trivial`, `speculative`, `yagni`, `unsupported`, or `out_of_scope`.
The controller rejects a duplicated, invented, or omitted claim, refuses a group whose strongest
member is only `low` severity, and selects each retained group's severity, location, and
verification from its strongest source. The lead groups and explains; it cannot rewrite the
evidence. Published findings are numbered `F001` onward by the controller, not by a model.

To skip routing and run a specific council:

```sh
ai-harness /review 16 --reviewer security_reviewer --reviewer resumability_reviewer
ai-harness /review 16 --all-reviewers
```

Explicit selection is a full override, not an addition: it skips the review lead's routing pass and
runs exactly the members named, so `--reviewer performance_expert` alone runs a one-member council
with no correctness reviewer. The always-on floor governs automatic routing.

The requested council is part of the review identity, so an explicitly selected council publishes
separately from an automatically routed one at the same head.

Automatic routing is one identity. The review marker covers the repository, PR number, head, prompt,
and context checksums plus the *requested* council, not the roster the lead happened to pick — so
re-running the same `/review` command posts nothing the second time even if the lead selects
different specialists and they find something new. That is deliberate: the marker exists to stop a
repeated command from accumulating near-duplicate reviews, and a model's routing choice is not a
stable identity to key publication on. The second run's findings are still written to `review.md`
and `council-report.md`, and the run reports that it did not publish. To get a second published
opinion at the same head, name the council explicitly with `--reviewer`, which changes the identity.

### Publication

Only open PRs under the configured size limits are accepted. Fork heads are fetched through
`refs/pull/<number>/head`. Immediately before publication, the controller rechecks the PR head. It
always sends a non-empty body (`No actionable findings.` for a clean review), uses `COMMENT` rather
than approve/request-changes, and retries a GitHub 422 once as summary-only. An invisible,
provider-neutral marker makes resume and repeated identical reviews idempotent. Mentions in
model-authored text are neutralized so a quoted `@team` cannot notify anyone.

Every finding's cited line is re-read from the exact commit and its excerpt compared to that
line, ignoring only leading and trailing whitespace.
A finding whose evidence does not survive that check is published as a summary note without an
inline location rather than attached to the wrong line.

Dismissed findings never reach the pull request. They, along with every specialist's verdict and
residual risks, are written to `council-report.md` beside the run's other artifacts.

When a PR head advances after a completed review, the next review automatically uses the newest
reviewed ancestor, its final findings, and the intervening commit delta as its primary scope. This
keeps fix verification adversarial without redoing a line-by-line review of unchanged PR code;
the full diff remains available for interaction checks and final inline-location validation.

## Runs and recovery

State and artifacts are written atomically under the target repository's common Git directory:

```text
.git/ai-harness/runs/<run-id>/
```

Each completed stage has a checksum. Commit, review, push, PR creation, and publication are also
separate resumable controller stages, even though they form one user-facing workflow. Each council
specialist is its own stage too, so a resume re-runs only the specialist that failed and replays the
routing decision rather than making it again. A timeout,
malformed model response, failed verification, GitHub failure, or stale PR head stops the run
without discarding completed work:

```sh
ai-harness status
ai-harness status <run-id>
ai-harness resume <run-id>
ai-harness resume <run-id> --timeout 900
```

If an implementer reports success but leaves no Git changes, the harness makes one explicit no-op
recovery call before failing closed. Verification commands also use Corepack automatically when a
plan names `pnpm` or `yarn` but that package manager is not directly installed. Corepack manifest
auto-pinning is disabled so controller verification cannot silently edit the target repository.

If the planning model finds a genuinely blocking product or scope decision, the run enters
`awaiting_input` before the adversarial reviewer or implementer is called. Questions are surfaced with
stable IDs and the worktree remains intact. Resume with one answer per question:

```sh
ai-harness resume <run-id> \
  --answer 'Q001=Use the existing JSON response format' \
  --answer 'Q002=Keep backward compatibility for one release'
```

For a single pending question, `--answer 'your answer'` is accepted as shorthand. The primary planner
receives the answers in a fresh structured call, rewrites the plan, and may pause again only for a new
blocking ambiguity. Answer history is stored with the run artifacts.

## Answering planning questions on Slack

`--slack` turns that pause into a direct-message conversation instead of a stopped run. The
controller asks one question at a time, waits for your reply, and feeds it into the same answer
machinery the manual path uses, so the run continues without a second `ai-harness resume`:

```sh
export AI_HARNESS_SLACK_USER=U0123456789
ai-harness --slack "Add bounded retries to the upload worker and test the exhausted path"
```

Replies are routed by an explicit prefix, and the message itself states the convention:

| Reply | Meaning |
|---|---|
| `q: why not JSON` or `?why not JSON` | ask the planner first; its answer is sent back and the same question keeps waiting |
| `a: use JSON, okay?` | force the reply to count as the answer |
| `cancel` | stop using Slack and pause for `ai-harness resume` instead |
| anything else | ends with `?` is treated as a question, otherwise as the answer |

The bare heuristic misreads both `Use JSON, okay?` and `why not JSON`, which is why the prefixes
exist. Clarification turns are capped per question.

Slack is an accelerator, never a requirement. A wait that runs out, a revoked connector, a missing
scope, a rate limit, a malformed reply, or an exhausted allowance all fall back to the normal
`awaiting_input` pause. Answers already collected are kept, so only the outstanding questions need
`--answer`, and `--no-slack` forces the terminal path on a resume:

```sh
ai-harness resume <run-id> --answer 'Q002=Keep backward compatibility'
ai-harness resume <run-id> --no-slack
```

Check the channel before relying on it. Sending and reading direct messages are separate Slack
permissions, so a connected server is necessary but not sufficient — the first real run proves the
read path:

```sh
ai-harness doctor --slack
```

Cost is worth knowing up front. There is no Slack token: the controller reaches Slack through the
Claude CLI's authenticated connector, and enabling connectors loads every configured MCP server's
tool schemas into the prompt, which is the bulk of the price. Measured: about $0.64 for a single
send or read, and about $0.96 for a wait that polled six times, so cost rises with the number of
polls rather than staying flat.

A wait therefore stretches its poll interval rather than adding round trips, capping any one call
at twenty polls. A full 30-minute wait polls every 90 seconds and costs roughly $1.90 in total
instead of the $6 it would cost as ninety polls. Budget about $2.50 for a question answered on the
first reply, plus roughly $1.50 per clarification turn. `AI_HARNESS_SLACK_BUDGET_USD` is a run-wide
cap the controller enforces across processes, since `--max-budget-usd` only bounds one of them; a
call that would exceed the run cap is never launched and the run falls back to the manual pause.

Raising `AI_HARNESS_SLACK_POLL_SECONDS` lowers cost and slows how quickly a reply is noticed.

Two other things to know. The controller lock is held for the whole wait, so `resume`, `cleanup`,
and new runs in the same repository block until you answer or the wait budget expires; `status` is
unaffected. And the feature applies to task planning only — pull request reviews do not ask
blocking questions, and `--slack` is rejected with `/review`.

Task worktrees intentionally remain after success. Delivered worktrees are clean because their
changes have been committed and pushed; `--local-only` worktrees remain dirty by design. Cleanup
removes only a clean, harness-owned worktree and retains its branch, refusing a dirty worktree so
implementation changes cannot be discarded accidentally:

```sh
ai-harness cleanup <run-id>
```

## Verification and security boundary

Model claims are not treated as test results. Plans return commands as argv arrays; the controller
rejects shells, eval flags, destructive Git operations, and unknown executables, then runs allowed
preparation/verification commands without a shell. Completion also requires a host-observed Git
status change, a binary patch built with a temporary index (including untracked and empty files), and
`git diff --check` over that same temporary index.

Claude runs with safe mode, `dontAsk`, an empty MCP configuration, no session persistence, an explicit
tool allowlist, and no repository test execution. Codex runs with ignored user config, explicit model
and reasoning, approval `never`, disabled login shells, disabled workspace network access, and either
read-only or workspace-write sandboxing. Writable Codex stages receive only the linked worktree's Git
administrative directory as an extra writable root. Both receive a sanitized environment and deny
PATH entries for GitHub, SSH, and network clients. Claude's allowlist blocks credential-client shell
commands while still allowing its parent process to refresh saved macOS authentication; Codex also
shadows the macOS credential client in its model-command PATH.

This is defense in depth, not same-user isolation. A local process may still reach ambient keychains
or explicitly addressed executables through OS facilities the CLIs do not sandbox. Use the harness
only with repositories and referenced files you trust. The controller itself intentionally uses your
normal `gh` credentials to read and publish PR reviews.

### The Slack question channel widens this boundary

`--slack` is off by default because it is a real widening, not a cosmetic one. When it is on, the
Slack process is the single Claude invocation in this harness that can reach the network.

What contains it: it is a separate process from every planning and implementation agent, launched
from a fresh empty temporary directory, given no repository access and no `--add-dir`, so the run
artifacts holding your plan, task prompt, and referenced context files are out of its reach. It
cannot use `--safe-mode`, because that flag is precisely what disables the connector, so hooks,
skills, settings sources, and memory are each disabled explicitly instead. Its tool allowlist is
`sleep`, one Slack send tool, and one Slack read tool.

What does not contain it, stated plainly:

- The set of MCP servers cannot be narrowed. Enabling the Slack connector enables every connector
  configured for that Claude installation. The tool allowlist is what keeps the others unusable;
  that is an allowlist, not isolation.
- Reply provenance is reported by a language model. The controller validates every field in Python
  against a recorded send receipt, a strictly monotonic cursor, the set of timestamps this run
  itself posted, the thread, and the configured sender, and it caps and JSON-quotes reply text
  before it reaches any prompt. That makes a stale message, an unrelated DM, a concurrent run's
  traffic, or the harness's own clarification unable to become an answer. It raises the bar against
  a fabricated message; it does not make fabrication impossible.
- Sender identity alone proves nothing here. The connector posts as the authenticated user, so in a
  self-DM the harness's own messages carry the same sender id as your replies. The outbound
  timestamp record is the discriminator that actually works.
- Separating two concurrent runs rests on the controller lock, not on provenance. The outbound
  timestamp record is per-run, and an unthreaded reply intended for another run would satisfy every
  provenance check. `controller_lock` allows one controller per repository at a time, which is what
  makes that unreachable; the run identifier printed in each message is for your benefit, not a
  validated field. Do not weaken that lock while this feature is enabled.
- Clarification answers are repository-derived by design. The planner reads the worktree to answer
  your follow-up, and that answer is sent to Slack. That is the intended feature, and it means
  repository content leaves the machine when you ask a follow-up question.

## Development

Normal tests use fake providers and temporary Git repositories; they never call a model or publish to
GitHub:

```sh
uv run --group dev pytest --cov
uv run --group dev ruff check .
```

Coverage must stay at or above 90% (`fail_under` in `pyproject.toml`). CI runs both commands on every
pull request, and the `test` check is required before merging to `main`.
