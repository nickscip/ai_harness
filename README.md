# ai-harness

`ai-harness` is a small, standalone controller for one adversarial cycle between Claude Code and
Codex CLI. It works from any trusted local Git repository without installing files into that repo.

For implementation tasks it:

1. asks the primary model family for a repository-grounded plan;
2. asks the other family to adversarially review that plan;
3. returns the review to the primary family for a disposition-complete revision;
4. implements the revised plan in a persistent linked worktree;
5. runs the revised plan's verification commands itself;
6. commits the verified diff and runs a cross-family adversarial implementation review locally;
7. pushes the harness branch and opens a draft pull request; and
8. publishes the already-validated implementation review on that exact PR head.

That entire path is one workflow started by one command. A genuinely blocking planning question is
the only intentional human pause; answering it resumes the same run at the exact stopped stage.

For pull requests it creates a detached worktree at the exact PR head, reviews the exact three-dot
diff, has the other family critique the draft, and asks the primary family for the final review. By
default it publishes one GitHub `COMMENT` review with validated inline locations plus a summary.

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
named profiles. Select one per run with `--profile NAME`, or set `AI_HARNESS_PROFILE`. A profile
chooses the primary family, that family's model and effort, the timeout, and an optional Claude
fallback model. The other family still participates as the adversary using its configured
counterpart model.

Configuration resolves in this order: command-line flags, `AI_HARNESS_*` environment variables,
the selected profile, then built-in library defaults. The common settings are:

| Setting | CLI flag | Environment variable | Default |
|---|---|---|---|
| Named profile | `--profile` | `AI_HARNESS_PROFILE` | `codex-balanced` |
| Primary family | `--family` | `AI_HARNESS_FAMILY` | selected profile |
| Claude model | `--claude-model` | `AI_HARNESS_CLAUDE_MODEL` | `sonnet` |
| Codex model | `--codex-model` | `AI_HARNESS_CODEX_MODEL` | `gpt-5.6-terra` |
| Codex reasoning | — | `AI_HARNESS_CODEX_REASONING` | `medium` |
| Per-agent timeout | `--timeout` | `AI_HARNESS_TIMEOUT` | selected profile |

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
ai-harness --family codex --timeout 1200 "Refactor the parser without changing its API"
```

The default `codex-balanced` profile uses Codex `gpt-5.6-terra` with medium reasoning as the primary
family and Claude Sonnet as the adversarial family. Swap the complete primary profile with
`--profile`, or override individual values with model flags and `AI_HARNESS_*` variables.

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

The branch is pushed, its draft PR URL is printed, and the cross-family implementation review is
published against the exact commit. To stop after verified implementation and leave the changes
uncommitted locally, opt out explicitly:

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

Only open PRs under the configured size limits are accepted. Fork heads are fetched through
`refs/pull/<number>/head`. Immediately before publication, the controller rechecks the PR head. It
always sends a non-empty body (`No actionable findings.` for a clean review), uses `COMMENT` rather
than approve/request-changes, and retries a GitHub 422 once as summary-only. An invisible,
provider-neutral marker makes resume and repeated identical reviews idempotent.

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
separate resumable controller stages, even though they form one user-facing workflow. A timeout,
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

## Development

Normal tests use fake providers and temporary Git repositories; they never call a model or publish to
GitHub:

```sh
uv run --group dev pytest
uv run --group dev ruff check .
```
