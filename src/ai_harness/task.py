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
    task_review_feedback_prompt,
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
)
from .state import RunStore, new_run_id, sha256_bytes

WORKTREE_SETUP_TARGET = "worktree-setup"
_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
_WORKTREE_SETUP_MARKER = b"# ai-harness: worktree-setup"
_MAX_WORKTREE_SETUP_MARKER_BYTES = 1_000_000


def _worktree_setup_enabled(makefile: Path) -> bool:
    scanned = 0
    try:
        with makefile.open("rb") as handle:
            while True:
                line = handle.readline(_MAX_WORKTREE_SETUP_MARKER_BYTES - scanned + 1)
                if not line:
                    return False
                scanned += len(line)
                if scanned > _MAX_WORKTREE_SETUP_MARKER_BYTES:
                    raise HarnessError(
                        "Could not inspect worktree setup opt-in: opt-in scan exceeds "
                        f"{_MAX_WORKTREE_SETUP_MARKER_BYTES} bytes"
                    )
                logical_line = line[:-1] if line.endswith(b"\n") else line
                logical_line = (
                    logical_line[:-1] if logical_line.endswith(b"\r") else logical_line
                )
                if logical_line == _WORKTREE_SETUP_MARKER:
                    return True
    except OSError as exc:
        raise HarnessError(f"Could not inspect worktree setup opt-in: {exc}") from exc


def worktree_setup_command(worktree: Path, source_repo: Path) -> list[str] | None:
    """Return the explicitly enabled worktree bootstrap command without evaluating Make.

    A fresh worktree has no ignored build inputs — no virtualenv, no node_modules, no
    `.env` — so verification commands fail until the project installs them itself.
    """
    candidates = (worktree / name for name in _MAKEFILE_NAMES)
    makefile = next((path for path in candidates if path.is_file()), None)
    if makefile is None:
        return None
    if not _worktree_setup_enabled(makefile):
        return None
    return ["make", WORKTREE_SETUP_TARGET, f"ENV_SOURCE={source_repo / '.env'}"]


def _bootstrap_worktree(
    store: RunStore,
    repo: GitRepo,
    *,
    worktree: Path,
    source_repo: Path,
    timeout: int,
) -> dict[str, Any]:
    bootstrap = store.read_completed_stage("worktree-bootstrap")
    if bootstrap is not None:
        return bootstrap
    store.begin_stage("worktree-bootstrap", "controller")
    try:
        argv = worktree_setup_command(worktree, source_repo)
        if argv is None:
            bootstrap = {"ran": False, "commands": []}
        else:
            command_results = execute_planned_commands(
                [
                    {
                        "argv": argv,
                        "cwd": ".",
                        "purpose": "Install the ignored build inputs this worktree needs.",
                        "timeout_seconds": timeout,
                    }
                ],
                worktree=worktree,
                preparation=True,
                progress=store.progress,
            )
            changed = repo.status(cwd=worktree)
            if changed:
                raise HarnessError(
                    "Worktree setup changed the worktree: "
                    + ", ".join(item.path for item in changed)
                )
            bootstrap = {"ran": True, "commands": command_results}
        store.complete_stage("worktree-bootstrap", bootstrap)
        return bootstrap
    except Exception as exc:
        store.fail_stage("worktree-bootstrap", str(exc))
        raise


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
            "codex_fast": config.codex_fast,
            "apply_review": config.apply_review,
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


def _bound_slack_progress(
    state: dict[str, Any],
    *,
    stage: str,
    round_number: int,
    questions: list,
) -> dict[str, Any] | None:
    progress = state.get("slack_progress")
    if not isinstance(progress, dict):
        return None
    binding = _slack_binding(state, stage, round_number, questions)
    if {key: progress.get(key) for key in binding} != binding:
        return None
    return progress


def collected_slack_answers(
    state: dict[str, Any], pending: dict[str, Any]
) -> dict[str, str]:
    """Return nonblank Slack answers only when progress matches the exact pending plan."""
    questions = pending.get("questions")
    stage = pending.get("stage")
    round_number = pending.get("round")
    if not isinstance(questions, list) or not isinstance(stage, str) or not isinstance(
        round_number, int
    ):
        return {}
    progress = _bound_slack_progress(
        state,
        stage=stage,
        round_number=round_number,
        questions=questions,
    )
    if progress is None or not isinstance(progress.get("answers"), dict):
        return {}
    return {
        str(key): str(value)
        for key, value in progress["answers"].items()
        if str(value).strip()
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
    ignored = int(state.get("slack_ignored_messages", 0))
    binding = _slack_binding(state, stage, round_number, questions)
    progress = _bound_slack_progress(
        state,
        stage=stage,
        round_number=round_number,
        questions=questions,
    )
    if progress is None:
        if isinstance(state.get("slack_progress"), dict):
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
            "clarification_attempts": 0,
            "pending_clarification": None,
            "send_in_flight": False,
        }

    if "clarification_attempts" not in progress:
        # PR-head progress counted a clarification before sending it. Recover the number of
        # confirmed deliveries from receipts newer than the active question, while keeping the old
        # count as the attempt index so an existing stage artifact is never reused for new text.
        legacy_attempts = max(0, int(progress.get("clarifications", 0)))
        receipt_ts = str(progress.get("receipt_ts", ""))
        outbound = [str(value) for value in progress.get("outbound_ts", [])]
        confirmed = min(
            legacy_attempts,
            sum(parse_ts(value) > parse_ts(receipt_ts) for value in outbound),
        )
        latest_known = max((parse_ts(value) for value in outbound), default=parse_ts(receipt_ts))
        cursor_advanced = parse_ts(str(progress.get("cursor_ts", ""))) > latest_known
        progress["clarifications"] = confirmed
        progress["clarification_attempts"] = legacy_attempts
        progress["pending_clarification"] = None
        if legacy_attempts > confirmed:
            legacy_stage = (
                f"clarify-r{round_number}-{progress.get('question_id', '')}-{legacy_attempts}"
            )
            if store.read_completed_stage(legacy_stage) is not None:
                progress["pending_clarification"] = {
                    "attempt": legacy_attempts,
                    "text": "",
                }
        progress["send_in_flight"] = legacy_attempts > confirmed or cursor_advanced
    else:
        progress.setdefault("pending_clarification", None)
        progress.setdefault("send_in_flight", False)
    if progress["send_in_flight"]:
        # The transport may have posted before its process returned malformed output or died. Its
        # timestamp is unknowable, so never poll the old receipt again. A fresh question receipt
        # advances the cursor past that possible self-message before any read occurs.
        store.log("Recovering from an uncertain Slack send with a fresh question receipt")
        progress["channel_id"] = ""
        progress["receipt_ts"] = ""
        progress["cursor_ts"] = ""
        progress["send_in_flight"] = False

    # A resume is a new wait invocation. Honor its configured --slack-wait window even when the
    # previous invocation persisted an expired deadline with the same question receipt.
    progress["deadline_epoch"] = time.time() + config.slack_wait_seconds

    def save() -> None:
        current = store.load()
        current["slack_progress"] = progress
        current["slack_ignored_messages"] = ignored
        store.save(current)

    def deliver_pending_clarification(
        question: dict[str, Any], identifier: str, receipt: Receipt
    ) -> None:
        pending = progress.get("pending_clarification")
        if not isinstance(pending, dict):
            return
        attempt = int(pending["attempt"])
        clarify_stage = f"clarify-r{round_number}-{identifier}-{attempt}"
        clarification = store.read_completed_stage(clarify_stage)
        if clarification is None:
            clarification_text = str(pending.get("text", ""))
            if not clarification_text:
                raise HarnessError(
                    f"Pending Slack clarification has no completed artifact: {clarify_stage}"
                )
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
                        clarification_text,
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

        progress["send_in_flight"] = True
        save()
        sent = channel.post_reply(receipt, str(clarification["answer"]))
        progress["clarifications"] = int(progress["clarifications"]) + 1
        progress["outbound_ts"] = [*progress["outbound_ts"], sent.message_ts]
        if parse_ts(sent.message_ts) > parse_ts(str(progress["cursor_ts"])):
            progress["cursor_ts"] = sent.message_ts
        progress["pending_clarification"] = None
        progress["send_in_flight"] = False
        save()
        channel.set_outbound(progress["outbound_ts"])

    save()
    if ignored >= MAX_IGNORED_SLACK_MESSAGES:
        store.log("Too many unusable Slack messages; falling back to manual input")
        return None

    for index, question in enumerate(questions):
        identifier = str(question["id"])
        if identifier in progress["answers"]:
            continue
        position = f"question {index + 1} of {len(questions)}"

        if progress["question_id"] != identifier or not progress["receipt_ts"]:
            if progress["question_id"] != identifier:
                progress["question_id"] = identifier
                progress["clarifications"] = 0
                progress["clarification_attempts"] = 0
                progress["pending_clarification"] = None
            progress["send_in_flight"] = True
            save()
            receipt = channel.post_question(
                _question_message(question, run_id=store.run_id, position=position)
            )
            progress["channel_id"] = receipt.channel_id
            progress["receipt_ts"] = receipt.message_ts
            progress["outbound_ts"] = [*progress["outbound_ts"], receipt.message_ts]
            progress["cursor_ts"] = receipt.message_ts
            progress["send_in_flight"] = False
            save()
            store.log(f"Asked {identifier} on Slack; waiting for a reply")
        else:
            receipt = Receipt(
                channel_id=str(progress["channel_id"]),
                message_ts=str(progress["receipt_ts"]),
            )
            store.log(f"Resuming the Slack wait for {identifier}")

        channel.set_outbound(progress["outbound_ts"])
        deliver_pending_clarification(question, identifier, receipt)
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
                target_sender=config.slack_user.strip(),
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
            routed = classify_reply(message.text)

            if not routed.text.strip():
                ignored += 1
                store.log("Ignoring a Slack message: routed reply is empty")
                save()
                if ignored >= MAX_IGNORED_SLACK_MESSAGES:
                    store.log("Too many unusable Slack messages; falling back to manual input")
                    return None
                continue

            if routed.routing is Routing.CANCEL:
                store.log("Slack conversation cancelled; falling back to manual input")
                return None

            if routed.routing is Routing.ANSWER:
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
                progress["send_in_flight"] = True
                save()
                sent = channel.post_reply(receipt, notice)
                progress["outbound_ts"] = [*progress["outbound_ts"], sent.message_ts]
                progress["send_in_flight"] = False
                save()
                store.log("Slack clarification limit reached; falling back to manual input")
                return None

            clarification_attempt = int(progress["clarification_attempts"]) + 1
            progress["clarification_attempts"] = clarification_attempt
            progress["pending_clarification"] = {
                "attempt": clarification_attempt,
                "text": routed.text,
            }
            save()
            deliver_pending_clarification(question, identifier, receipt)

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
            # Slack calls persist spend and conversation progress while this function waits.
            # Merge the resolved answers into the latest state instead of overwriting those writes
            # with the snapshot loaded before `_slack_answers` ran.
            state = store.load()
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
    revised: dict[str, Any],
    implementation: dict[str, Any],
    verification: dict[str, Any],
    context: str,
    context_dirs: tuple[Path, ...],
    runner: ProviderRunner,
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

    feedback: dict[str, Any] | None = None
    if config.apply_review and int(review["findings"]) > 0:
        feedback = _apply_review_feedback(
            store=store,
            repo=repo,
            config=config,
            runner=runner,
            worktree=worktree,
            branch=branch,
            revised=revised,
            review=review,
            context=context,
            context_dirs=context_dirs,
        )

    return {
        "commit": commit["sha"],
        "pull_request": pull,
        "review": {**review, "publication": publication},
        "feedback": feedback,
    }


def _apply_review_feedback(
    *,
    store: RunStore,
    repo: GitRepo,
    config: HarnessConfig,
    runner: ProviderRunner,
    worktree: Path,
    branch: str,
    revised: dict[str, Any],
    review: dict[str, Any],
    context: str,
    context_dirs: tuple[Path, ...],
) -> dict[str, Any]:
    """Have the primary family apply the published review's findings and update the pull request."""
    state = store.load()
    review_store = RunStore(repo.common_git_dir, str(review["run_id"]), progress=store.progress)

    applied = store.read_completed_stage("review-feedback")
    if applied is None:
        applied = runner.run(
            ProviderRequest(
                family=state["primary_family"],
                stage="review-feedback",
                cwd=worktree,
                prompt=task_review_feedback_prompt(
                    state["prompt"],
                    Path(str(review["review"])),
                    review_store.root / "review-final.json",
                    context,
                ),
                schema_name="implementation",
                writable=True,
                timeout=config.stage_timeout,
                context_dirs=(*context_dirs, review_store.root),
                git_admin_dir=repo.git_admin_dir(worktree),
            )
        )
    else:
        validate_output("implementation", applied)
    verify_references(store.load())

    delivered = store.read_completed_stage("review-feedback-delivery")
    if delivered is None:
        store.begin_stage("review-feedback-delivery", "controller")
        try:
            changed = [item.path for item in repo.status(cwd=worktree)]
            if not changed:
                # Every finding was argued down rather than fixed. The notes are the record.
                delivered = {"changed_paths": [], "commit": "", "commands": []}
            else:
                command_results = execute_planned_commands(
                    revised["verification_commands"],
                    worktree=worktree,
                    preparation=False,
                    progress=store.progress,
                )
                commit_sha = repo.commit_all(
                    worktree, "ai-harness: apply pull request review feedback"
                )
                repo.push_branch(worktree, branch)
                delivered = {
                    "changed_paths": changed,
                    "commit": commit_sha,
                    "commands": command_results,
                }
            store.complete_stage("review-feedback-delivery", delivered)
        except Exception as exc:
            store.fail_stage("review-feedback-delivery", str(exc))
            raise

    return {"summary": applied["summary"], "notes": applied["notes"], **delivered}


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

    _bootstrap_worktree(
        store,
        repo,
        worktree=worktree,
        source_repo=Path(str(state["source_repo"])),
        timeout=config.stage_timeout,
    )

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
            revised=revised,
            implementation=implementation,
            verification=verification,
            context=context,
            context_dirs=context_dirs,
            runner=runner,
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
