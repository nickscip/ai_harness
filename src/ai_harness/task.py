from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .commands import execute_planned_commands, validate_plan_commands
from .config import HarnessConfig, harness_worktree_path, other_family
from .context import context_prompt, copy_references, verify_references
from .errors import AwaitingInput, HarnessError
from .git import GitRepo
from .github import (
    create_draft_pull_request,
    open_pull_request_for_head,
    repository_name,
)
from .pr import create_local_review, execute_review, publish_local_review
from .progress import ProgressCallback
from .prompts import (
    task_command_repair_prompt,
    task_implementation_prompt,
    task_implementation_repair_prompt,
    task_plan_answer_prompt,
    task_plan_clarify_prompt,
    task_plan_prompt,
    task_review_prompt,
    task_revise_prompt,
)
from .providers import ProviderRequest, ProviderRunner
from .schema import validate_output, validate_plan, validate_plan_review, validate_revised_plan
from .slack import (
    ChannelBudget,
    ChannelError,
    QuestionChannel,
    Receipt,
    Routing,
    build_question_channel,
    classify_reply,
    parse_ts,
    reject_reason,
    truncate_reply,
)
from .state import RunStore, new_run_id, sha256_bytes


def start_task(
    repo: GitRepo,
    *,
    config: HarnessConfig,
    prompt: str,
    references: Sequence[Path],
    deliver: bool = False,
    progress: ProgressCallback | None = None,
) -> RunStore:
    run_id = new_run_id("task")
    source_head = repo.head()
    base_branch = repo.current_branch() if deliver else None
    store = RunStore.create(
        repo.common_git_dir,
        run_id=run_id,
        kind="task",
        source_repo=repo.root,
        source_head=source_head,
        primary_family=config.primary_family,
        prompt=prompt,
        options={
            "timeout": config.stage_timeout,
            "profile_name": config.profile_name,
            "profile_description": config.profile_description,
            "profiles_path": config.profiles_path,
            "claude_model": config.claude_model,
            "claude_effort": config.claude_effort,
            "claude_fallback_model": config.claude_fallback_model,
            "codex_model": config.codex_model,
            "codex_reasoning": config.codex_reasoning,
            "claude_max_budget_usd": config.claude_max_budget_usd,
            "deliver": deliver,
            "slack_enabled": config.slack_enabled,
            "slack_user": config.slack_user,
            "slack_wait_seconds": config.slack_wait_seconds,
            "slack_budget_usd": config.slack_budget_usd,
            "slack_max_clarifications": config.slack_max_clarifications,
            "slack_poll_seconds": config.slack_poll_seconds,
        },
        progress=progress,
    )
    store.log(
        f"Created task run {run_id} from {base_branch or 'detached/local-only'} "
        f"at {source_head[:12]}"
    )
    copy_references(store, references)
    dirty = repo.status()
    warnings: list[str] = []
    if dirty:
        paths = [item.path for item in dirty]
        warnings.append(
            "The caller checkout is dirty. The harness used HEAD and excluded these local paths: "
            + ", ".join(paths)
        )
        store.log(f"Excluding {len(dirty)} dirty caller-checkout path(s)")
    branch = f"ai-harness/{run_id}"
    worktree = harness_worktree_path(repo.root, run_id)
    store.log(f"Creating implementation worktree {worktree}")
    repo.create_branch_worktree(worktree, branch, source_head)
    store.log(f"Created branch {branch}")
    state = store.load()
    state.update(
        {
            "branch": branch,
            "base_branch": base_branch,
            "worktree": str(worktree),
            "warnings": warnings,
            "caller_dirty_paths": [item.path for item in dirty],
            "repository_features": {
                "submodules": (repo.root / ".gitmodules").is_file(),
                "lfs_attributes": (repo.root / ".gitattributes").is_file()
                and "filter=lfs" in (repo.root / ".gitattributes").read_text(
                    encoding="utf-8", errors="ignore"
                ),
            },
            "human_answers": [],
        }
    )
    store.save(state)
    execute_task(store, repo, config)
    return store


MAX_IGNORED_SLACK_MESSAGES = 10


def _question_fingerprint(questions: list[dict[str, Any]]) -> str:
    return sha256_bytes(json.dumps(questions, sort_keys=True).encode("utf-8"))


def _question_message(question: dict[str, Any], *, run_id: str, position: str) -> str:
    default = question.get("suggested_default") or ""
    lines = [
        f"*ai-harness needs a decision* ({position})",
        f"`[ai-harness {run_id} {question['id']}]`",
        "",
        question["question"],
        "",
        f"_Why this blocks planning:_ {question['why_blocking']}",
    ]
    if default.strip():
        lines.append(f"_Suggested default:_ {default}")
    lines.extend(
        [
            "",
            "Reply with your decision. Prefix `q:` to ask me something first, "
            "`a:` to force a reply to count as the answer, or send `cancel` to "
            "answer from the terminal instead.",
        ]
    )
    return "\n".join(lines)


def _slack_binding(state: dict[str, Any], stage: str, round_number: int, questions: list) -> dict:
    """Bind progress to the exact pending question set.

    A later planning round may legitimately reuse Q001, and answer validation matches on ID alone,
    so progress carrying a stale answer must never be applied to a different question.
    """
    stage_record = state.get("stages", {}).get(stage, {})
    return {
        "round": round_number,
        "stage": stage,
        "artifact_sha256": stage_record.get("sha256", ""),
        "question_hash": _question_fingerprint(questions),
    }


def _slack_answers(
    *,
    store: RunStore,
    runner: ProviderRunner,
    config: HarnessConfig,
    channel: QuestionChannel,
    primary: str,
    worktree: Path,
    context: str,
    context_dirs: tuple[Path, ...],
    questions: list[dict[str, Any]],
    stage: str,
    round_number: int,
) -> dict[str, str] | None:
    """Run the Slack conversation for one pending question set.

    Returns a complete answer dict, or None to fall back to the manual `resume --answer` path.
    Every send and every accepted reply is persisted before the next network call, so a crash
    resumes at the unanswered question rather than re-asking or losing an answer.
    """
    state = store.load()
    binding = _slack_binding(state, stage, round_number, questions)
    progress = state.get("slack_progress")
    if not isinstance(progress, dict) or {k: progress.get(k) for k in binding} != binding:
        if isinstance(progress, dict):
            store.log("Discarding Slack progress bound to a different planning question set")
        progress = {
            **binding,
            "channel_id": "",
            "question_id": "",
            "receipt_ts": "",
            "outbound_ts": [],
            "cursor_ts": "",
            "answers": {},
            "deadline_epoch": time.time() + config.slack_wait_seconds,
            "clarifications": 0,
        }

    def save() -> None:
        current = store.load()
        current["slack_progress"] = progress
        store.save(current)

    save()

    for index, question in enumerate(questions):
        identifier = str(question["id"])
        if identifier in progress["answers"]:
            continue
        position = f"question {index + 1} of {len(questions)}"

        if progress["question_id"] != identifier or not progress["receipt_ts"]:
            receipt = channel.post_question(
                _question_message(question, run_id=store.run_id, position=position)
            )
            progress["question_id"] = identifier
            progress["channel_id"] = receipt.channel_id
            progress["receipt_ts"] = receipt.message_ts
            progress["outbound_ts"] = [*progress["outbound_ts"], receipt.message_ts]
            progress["cursor_ts"] = receipt.message_ts
            progress["clarifications"] = 0
            save()
            store.log(f"Asked {identifier} on Slack; waiting for a reply")
        else:
            receipt = Receipt(
                channel_id=str(progress["channel_id"]),
                message_ts=str(progress["receipt_ts"]),
            )
            store.log(f"Resuming the Slack wait for {identifier}")

        channel.set_outbound(progress["outbound_ts"])
        ignored = 0
        while True:
            remaining = float(progress["deadline_epoch"]) - time.time()
            if remaining <= 0:
                store.log("Slack wait budget expired before an answer arrived")
                return None
            message = channel.wait_for_reply(
                receipt,
                after_ts=str(progress["cursor_ts"]),
                deadline=time.monotonic() + remaining,
            )
            if message is None:
                store.log("Slack wait ended without a reply")
                return None

            reason = reject_reason(
                message,
                receipt=receipt,
                cursor_ts=str(progress["cursor_ts"]),
                outbound_ts=progress["outbound_ts"],
                target_sender=config.slack_user,
            )
            if reason is not None:
                ignored += 1
                store.log(f"Ignoring a Slack message: {reason}")
                if parse_ts(message.message_ts) > parse_ts(str(progress["cursor_ts"])):
                    progress["cursor_ts"] = message.message_ts
                    save()
                if ignored >= MAX_IGNORED_SLACK_MESSAGES:
                    store.log("Too many unusable Slack messages; falling back to manual input")
                    return None
                continue

            progress["cursor_ts"] = message.message_ts
            save()
            routed = classify_reply(truncate_reply(message.text))

            if routed.routing is Routing.CANCEL:
                store.log("Slack conversation cancelled; falling back to manual input")
                return None

            if routed.routing is Routing.ANSWER:
                if not routed.text.strip():
                    continue
                progress["answers"] = {**progress["answers"], identifier: routed.text}
                progress["question_id"] = ""
                progress["receipt_ts"] = ""
                save()
                store.log(f"Collected the Slack answer for {identifier}")
                break

            if progress["clarifications"] >= config.slack_max_clarifications:
                notice = (
                    "That's my clarification limit for this question. Reply with your decision, "
                    "or answer from the terminal with `ai-harness resume`."
                )
                sent = channel.post_reply(receipt, notice)
                progress["outbound_ts"] = [*progress["outbound_ts"], sent.message_ts]
                save()
                store.log("Slack clarification limit reached; falling back to manual input")
                return None

            progress["clarifications"] = int(progress["clarifications"]) + 1
            save()
            clarify_stage = f"clarify-r{round_number}-{identifier}-{progress['clarifications']}"
            clarification = store.read_completed_stage(clarify_stage)
            if clarification is None:
                clarification = runner.run(
                    ProviderRequest(
                        family=primary,
                        stage=clarify_stage,
                        cwd=worktree,
                        prompt=task_plan_clarify_prompt(
                            str(store.load()["prompt"]),
                            store.root / f"{stage}.json",
                            {
                                "id": identifier,
                                "question": str(question["question"]),
                                "why_blocking": str(question["why_blocking"]),
                                "suggested_default": str(question.get("suggested_default", "")),
                            },
                            routed.text,
                            context,
                        ),
                        schema_name="clarification",
                        writable=False,
                        timeout=config.stage_timeout,
                        context_dirs=(*context_dirs, store.root),
                    )
                )
            else:
                validate_output("clarification", clarification)

            sent = channel.post_reply(receipt, str(clarification["answer"]))
            progress["outbound_ts"] = [*progress["outbound_ts"], sent.message_ts]
            if parse_ts(sent.message_ts) > parse_ts(str(progress["cursor_ts"])):
                progress["cursor_ts"] = sent.message_ts
            save()
            channel.set_outbound(progress["outbound_ts"])

    return {str(key): str(value) for key, value in progress["answers"].items()}


def _resolve_plan(
    *,
    store: RunStore,
    runner: ProviderRunner,
    config: HarnessConfig,
    channel: QuestionChannel | None,
    primary: str,
    worktree: Path,
    context: str,
    context_dirs: tuple[Path, ...],
    timeout: int,
    answers: dict[str, str] | None,
) -> tuple[dict[str, Any], Path]:
    while True:
        state = store.load()
        pending = state.get("pending_input")
        if pending:
            questions = pending["questions"]
            if answers is None and channel is not None:
                try:
                    answers = _slack_answers(
                        store=store,
                        runner=runner,
                        config=config,
                        channel=channel,
                        primary=primary,
                        worktree=worktree,
                        context=context,
                        context_dirs=context_dirs,
                        questions=questions,
                        stage=str(pending["stage"]),
                        round_number=int(pending["round"]),
                    )
                except ChannelError as exc:
                    # Every channel failure class degrades to the documented manual path.
                    store.log(f"Slack question channel unavailable: {exc}")
                    answers = None
            if answers is None:
                store.log(f"Planning is paused for {len(questions)} human answer(s)")
                raise AwaitingInput(store.run_id, questions)
            expected = {item["id"] for item in questions}
            provided = set(answers)
            missing = sorted(expected - provided)
            extra = sorted(provided - expected)
            blank = sorted(key for key, answer in answers.items() if not answer.strip())
            if missing or extra or blank:
                raise HarnessError(
                    "Answers do not match pending questions "
                    f"(missing={missing}, extra={extra}, blank={blank})"
                )
            history = list(state.get("human_answers", []))
            history.append(
                {
                    "round": pending["round"],
                    "questions": questions,
                    "answers": answers,
                }
            )
            state.update(
                {
                    "status": "running",
                    "pending_input": None,
                    "human_answers": history,
                    "planning_round": int(pending["round"]) + 1,
                    "planning_base_artifact": pending["artifact"],
                    "planning_answers": answers,
                    # Progress is cleared with the answers it produced, so a later round that
                    # reuses a question ID can never inherit this round's reply.
                    "slack_progress": None,
                }
            )
            store.save(state)
            answers = None
        elif answers:
            raise HarnessError("This run has no pending planning questions")

        state = store.load()
        active_stage = state.get("active_plan_stage")
        if active_stage:
            active = store.read_completed_stage(active_stage)
            if active is None:
                raise HarnessError(f"Active planning artifact is incomplete: {active_stage}")
            validate_output("plan", active)
            validate_plan(active)
            return active, store.root / f"{active_stage}.json"

        round_number = int(state.get("planning_round", 1))
        stage = "plan" if round_number == 1 else f"plan-r{round_number}"
        plan = store.read_completed_stage(stage)
        if plan is None:
            if round_number == 1:
                prompt = task_plan_prompt(state["prompt"], context)
                stage_context_dirs = context_dirs
            else:
                base_artifact = state.get("planning_base_artifact")
                planning_answers = state.get("planning_answers")
                if not base_artifact or not isinstance(planning_answers, dict):
                    raise HarnessError("Answered planning state is incomplete")
                prompt = task_plan_answer_prompt(
                    state["prompt"],
                    store.root / base_artifact,
                    planning_answers,
                    context,
                )
                stage_context_dirs = (*context_dirs, store.root)
            plan = runner.run(
                ProviderRequest(
                    family=primary,
                    stage=stage,
                    cwd=worktree,
                    prompt=prompt,
                    schema_name="plan",
                    writable=False,
                    timeout=timeout,
                    context_dirs=stage_context_dirs,
                )
            )
        else:
            validate_output("plan", plan)
        validate_plan(plan)
        verify_references(store.load())

        questions = plan["questions"]
        if questions:
            state = store.load()
            state.update(
                {
                    "status": "awaiting_input",
                    "planning_round": round_number,
                    "pending_input": {
                        "round": round_number,
                        "stage": stage,
                        "artifact": f"{stage}.json",
                        "questions": questions,
                    },
                }
            )
            store.save(state)
            store.log(f"Planning requested {len(questions)} blocking human answer(s); pausing")
            # Loop back so the pending branch above is the one place that either resolves the
            # questions over Slack or raises AwaitingInput. One answer-application path.
            continue

        state = store.load()
        state.update(
            {
                "status": "running",
                "pending_input": None,
                "active_plan_stage": stage,
                "active_plan_artifact": f"{stage}.json",
            }
        )
        store.save(state)
        return plan, store.root / f"{stage}.json"


def _delivery_title(summary: str) -> str:
    compact = " ".join(summary.split()).strip("#*- ")
    return (compact or "Implement reviewed task")[:80].rstrip()


def _delivery_body(
    *,
    task_prompt: str,
    implementation: dict[str, Any],
    verification: dict[str, Any],
    run_id: str,
) -> str:
    commands = verification.get("commands", [])
    checks = (
        "\n".join(f"- `{' '.join(item['argv'])}`: passed" for item in commands)
        or "- Controller verification completed; the plan specified no commands."
    )
    return (
        "## Summary\n"
        f"{implementation['summary']}\n\n"
        "## Task\n"
        f"{task_prompt}\n\n"
        "## Verification\n"
        f"{checks}\n\n"
        f"Generated by ai-harness run `{run_id}` after adversarial plan review."
    )


def _deliver_task(
    *,
    store: RunStore,
    repo: GitRepo,
    config: HarnessConfig,
    worktree: Path,
    implementation: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    state = store.load()
    branch = str(state["branch"])
    base_branch = str(state.get("base_branch") or "")
    if not base_branch:
        raise HarnessError("Task delivery has no base branch")

    commit = store.read_completed_stage("delivery-commit")
    if commit is None:
        store.begin_stage("delivery-commit", "controller")
        try:
            if repo.status(cwd=worktree):
                commit_sha = repo.commit_all(
                    worktree, f"ai-harness: {_delivery_title(implementation['summary'])}"
                )
            else:
                commit_sha = repo.run(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
                if commit_sha == state["source_head"]:
                    raise HarnessError("No implementation commit exists to deliver")
            commit = {"sha": commit_sha}
            store.complete_stage("delivery-commit", commit)
        except Exception as exc:
            store.fail_stage("delivery-commit", str(exc))
            raise

    review = store.read_completed_stage("implementation-review")
    if review is None:
        store.begin_stage("implementation-review", "controller")
        try:
            current = store.load()
            review_run_id = current.get("implementation_review_run_id")
            if review_run_id:
                review_store = RunStore(
                    repo.common_git_dir,
                    str(review_run_id),
                    progress=store.progress,
                )
            else:
                references = [Path(item["copy"]) for item in current.get("context", [])]
                review_store = create_local_review(
                    repo,
                    config=config,
                    base=str(current["source_head"]),
                    head=str(commit["sha"]),
                    branch=branch,
                    title=_delivery_title(implementation["summary"]),
                    prompt=(
                        "Review the implementation produced for this task. "
                        "Don't overengineer, but be adversarial to find bugs "
                        "before they happen.\n\n"
                        + str(current["prompt"])
                    ),
                    references=references,
                    progress=store.progress,
                )
                current = store.load()
                current["implementation_review_run_id"] = review_store.run_id
                store.save(current)
            review_result = execute_review(review_store, repo, config)
            final_review = review_store.read_completed_stage("review-final")
            if final_review is None:
                raise HarnessError("Implementation review did not produce final findings")
            review = {
                "run_id": review_store.run_id,
                "result": review_result,
                "findings": len(final_review["findings"]),
                "review": str(review_store.root / "review.md"),
            }
            store.complete_stage("implementation-review", review)
        except Exception as exc:
            store.fail_stage("implementation-review", str(exc))
            raise

    pushed = store.read_completed_stage("delivery-push")
    if pushed is None:
        store.begin_stage("delivery-push", "controller")
        try:
            repo.push_branch(worktree, branch)
            pushed = {"branch": branch, "sha": commit["sha"]}
            store.complete_stage("delivery-push", pushed)
        except Exception as exc:
            store.fail_stage("delivery-push", str(exc))
            raise

    pull = store.read_completed_stage("delivery-pr")
    if pull is None:
        store.begin_stage("delivery-pr", "controller")
        try:
            name = repository_name(repo.root)
            existing = open_pull_request_for_head(repo.root, branch)
            created = existing or create_draft_pull_request(
                repo.root,
                name_with_owner=name,
                title=_delivery_title(implementation["summary"]),
                head=branch,
                base=base_branch,
                body=_delivery_body(
                    task_prompt=str(state["prompt"]),
                    implementation=implementation,
                    verification=verification,
                    run_id=store.run_id,
                ),
            )
            pull = {
                "number": int(created["number"]),
                "url": created.get("html_url") or created.get("url"),
                "repository": name,
                "draft": bool(created.get("isDraft", created.get("draft", True))),
            }
            store.complete_stage("delivery-pr", pull)
        except Exception as exc:
            store.fail_stage("delivery-pr", str(exc))
            raise

    publication = store.read_completed_stage("delivery-review-publication")
    if publication is None:
        store.begin_stage("delivery-review-publication", "controller")
        try:
            review_store = RunStore(
                repo.common_git_dir,
                str(review["run_id"]),
                progress=store.progress,
            )
            publication = publish_local_review(
                review_store,
                repo=repo,
                name_with_owner=str(pull["repository"]),
                number=int(pull["number"]),
            )
            store.complete_stage("delivery-review-publication", publication)
        except Exception as exc:
            store.fail_stage("delivery-review-publication", str(exc))
            raise

    return {
        "commit": commit["sha"],
        "pull_request": pull,
        "review": {**review, "publication": publication},
    }


def execute_task(
    store: RunStore,
    repo: GitRepo,
    config: HarnessConfig,
    *,
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    state = store.load()
    if state.get("status") == "completed" and isinstance(state.get("result"), dict):
        return state["result"]
    worktree_value = state.get("worktree")
    if not worktree_value:
        raise HarnessError("Task run has no worktree")
    worktree = Path(worktree_value)
    if not worktree.is_dir():
        raise HarnessError(f"Task worktree is missing: {worktree}")
    if repo.head() != state["source_head"] and Path(state["source_repo"]) == repo.root:
        # Caller HEAD moving is harmless; the run remains pinned to source_head.
        pass
    runner = ProviderRunner(config, store)
    primary = state["primary_family"]
    critic = other_family(primary)
    context = context_prompt(state)
    context_dir = store.root / "context"
    context_dirs = (context_dir,) if context_dir.is_dir() else ()
    git_admin = repo.git_admin_dir(worktree)

    def record_spend(total: float) -> None:
        current = store.load()
        current["slack_spent_usd"] = total
        store.save(current)

    # Spend is tracked run-wide and outside slack_progress: `--max-budget-usd` is a per-process
    # cap, and discarding progress for a new question set must not also reset the allowance.
    budget = ChannelBudget(
        limit_usd=config.slack_budget_usd,
        spent_usd=float(state.get("slack_spent_usd", 0.0)),
        on_spend=record_spend,
    )
    try:
        channel = build_question_channel(config, store, budget=budget)
    except ChannelError as exc:
        store.log(f"Slack question channel unavailable: {exc}")
        channel = None

    plan, plan_path = _resolve_plan(
        store=store,
        runner=runner,
        config=config,
        channel=channel,
        primary=primary,
        worktree=worktree,
        context=context,
        context_dirs=context_dirs,
        timeout=config.stage_timeout,
        answers=answers,
    )

    review = store.read_completed_stage("plan-review")
    if review is None:
        review = runner.run(
            ProviderRequest(
                family=critic,
                stage="plan-review",
                cwd=worktree,
                prompt=task_review_prompt(state["prompt"], plan_path, context),
                schema_name="plan_review",
                writable=False,
                timeout=config.stage_timeout,
                context_dirs=(*context_dirs, store.root),
            )
        )
    else:
        validate_output("plan_review", review)
    validate_plan_review(review)
    verify_references(store.load())

    revised = store.read_completed_stage("revised-plan")
    if revised is None:
        revised = runner.run(
            ProviderRequest(
                family=primary,
                stage="revised-plan",
                cwd=worktree,
                prompt=task_revise_prompt(
                    state["prompt"],
                    plan_path,
                    store.root / "plan-review.json",
                    context,
                ),
                schema_name="revised_plan",
                writable=False,
                timeout=config.stage_timeout,
                context_dirs=(*context_dirs, store.root),
            )
        )
    else:
        validate_output("revised_plan", revised)
    validate_revised_plan(review, revised)
    verify_references(store.load())

    revised_path = store.root / "revised-plan.json"
    try:
        validate_plan_commands(revised)
    except HarnessError as validation_error:
        repaired = store.read_completed_stage("command-repair")
        if repaired is None:
            repaired = runner.run(
                ProviderRequest(
                    family=primary,
                    stage="command-repair",
                    cwd=worktree,
                    prompt=task_command_repair_prompt(
                        state["prompt"],
                        revised_path,
                        str(validation_error),
                        context,
                    ),
                    schema_name="revised_plan",
                    writable=False,
                    timeout=config.stage_timeout,
                    context_dirs=(*context_dirs, store.root),
                )
            )
        else:
            validate_output("revised_plan", repaired)
        validate_revised_plan(review, repaired)
        validate_plan_commands(repaired)
        verify_references(store.load())
        revised = repaired
        revised_path = store.root / "command-repair.json"

    preparation = store.read_completed_stage("preparation")
    if preparation is None:
        store.begin_stage("preparation", "controller")
        try:
            if repo.status(cwd=worktree):
                raise HarnessError("Task worktree changed before preparation")
            command_results = execute_planned_commands(
                revised["preparation_commands"],
                worktree=worktree,
                preparation=True,
                progress=store.progress,
            )
            changed = repo.status(cwd=worktree)
            if changed:
                raise HarnessError(
                    "Preparation commands changed the worktree: "
                    + ", ".join(item.path for item in changed)
                )
            preparation = {"commands": command_results}
            store.complete_stage("preparation", preparation)
        except Exception as exc:
            store.fail_stage("preparation", str(exc))
            raise

    implementation = store.read_completed_stage("implementation")
    if implementation is None:
        implementation = runner.run(
            ProviderRequest(
                family=primary,
                stage="implementation",
                cwd=worktree,
                prompt=task_implementation_prompt(
                    state["prompt"],
                    revised_path,
                    store.root / "plan-review.json",
                    context,
                ),
                schema_name="implementation",
                writable=True,
                timeout=config.stage_timeout,
                context_dirs=(*context_dirs, store.root),
                git_admin_dir=git_admin,
            )
        )
    else:
        validate_output("implementation", implementation)
    verify_references(store.load())

    implementation_status = repo.status(cwd=worktree)
    if not implementation_status or not implementation["changed_files"]:
        repaired_implementation = store.read_completed_stage("implementation-repair")
        if repaired_implementation is None:
            repaired_implementation = runner.run(
                ProviderRequest(
                    family=primary,
                    stage="implementation-repair",
                    cwd=worktree,
                    prompt=task_implementation_repair_prompt(
                        state["prompt"],
                        revised_path,
                        store.root / "plan-review.json",
                        store.root / "implementation.json",
                        context,
                    ),
                    schema_name="implementation",
                    writable=True,
                    timeout=config.stage_timeout,
                    context_dirs=(*context_dirs, store.root),
                    git_admin_dir=git_admin,
                )
            )
        else:
            validate_output("implementation", repaired_implementation)
        implementation = repaired_implementation
        verify_references(store.load())

    capture = store.read_completed_stage("implementation-capture")
    capture_status = repo.status(cwd=worktree)
    captured_patch = repo.implementation_patch(worktree, store.root) if capture_status else ""
    capture_is_stale = bool(
        capture is not None
        and store.read_completed_stage("delivery-commit") is None
        and (
            capture.get("changed_paths") != [item.path for item in capture_status]
            or Path(str(capture["patch"])).read_text(encoding="utf-8") != captured_patch
        )
    )
    if capture is None or capture_is_stale:
        store.begin_stage("implementation-capture", "controller")
        try:
            if not capture_status:
                raise HarnessError("Implementation produced no tracked or untracked changes")
            if not captured_patch:
                raise HarnessError("Could not serialize implementation as a Git patch")
            patch_path = store.write_text_artifact("implementation.patch", captured_patch)
            capture = {
                "changed_paths": [item.path for item in capture_status],
                "patch": str(patch_path),
            }
            store.complete_stage("implementation-capture", capture)
        except Exception as exc:
            store.fail_stage("implementation-capture", str(exc))
            raise

    verification = store.read_completed_stage("verification")
    if verification is None:
        store.begin_stage("verification", "controller")
        try:
            command_results = execute_planned_commands(
                revised["verification_commands"],
                worktree=worktree,
                preparation=False,
                progress=store.progress,
            )
            verification = {
                "diff_check": "passed",
                "commands": command_results,
                "changed_paths": capture["changed_paths"],
                "patch": capture["patch"],
            }
            store.complete_stage("verification", verification)
        except Exception as exc:
            store.fail_stage("verification", str(exc))
            raise

    delivery: dict[str, Any] | None = None
    if bool(store.load()["options"].get("deliver", False)):
        delivery = _deliver_task(
            store=store,
            repo=repo,
            config=config,
            worktree=worktree,
            implementation=implementation,
            verification=verification,
        )

    final_state = store.load()
    final_state["status"] = "completed"
    final_state["result"] = {
        "worktree": str(worktree),
        "branch": final_state["branch"],
        "changed_paths": capture["changed_paths"],
        "patch": capture["patch"],
        "delivery": delivery,
    }
    store.save(final_state)
    if delivery:
        store.log(
            f"Task workflow complete: draft PR {delivery['pull_request']['url']} "
            f"with {delivery['review']['findings']} review finding(s)"
        )
    else:
        store.log(f"Local-only task workflow complete in {worktree}")
    return final_state["result"]
