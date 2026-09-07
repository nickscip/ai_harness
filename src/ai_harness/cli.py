from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Family, HarnessConfig, other_family
from .context import split_prompt_and_references
from .council import CANONICAL_ORDER
from .doctor import deep_doctor, format_doctor, shallow_doctor, slack_doctor
from .errors import AwaitingInput, HarnessError
from .git import GitRepo
from .pr import execute_review, start_review
from .progress import TerminalProgress
from .state import RunStore, controller_lock, list_runs
from .task import collected_slack_answers, execute_task, start_task


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-harness",
        description="Plan, adversarially review, revise, and implement with Claude and Codex.",
    )
    parser.add_argument("--family", choices=["claude", "codex"], default=None)
    parser.add_argument("--profile", default=None, help="Named profile from council-profiles.json")
    parser.add_argument("--timeout", type=int, default=None, help="Per-stage timeout in seconds")
    parser.add_argument("--claude-model", default=None)
    parser.add_argument("--codex-model", default=None)
    parser.add_argument("--no-publish", action="store_true", help="Render a PR review locally")
    parser.add_argument(
        "--reviewer",
        action="append",
        default=None,
        metavar="NAME",
        choices=[member.value for member in CANONICAL_ORDER],
        help="Run this council member instead of letting the review lead route (repeatable)",
    )
    parser.add_argument(
        "--all-reviewers",
        action="store_true",
        help="Run every council member and skip review-lead routing",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Stop after verified implementation without committing, pushing, or opening a PR",
    )
    parser.add_argument(
        "--slack",
        action="store_true",
        help="Ask blocking planning questions over Slack instead of pausing the run",
    )
    parser.add_argument(
        "--slack-wait",
        type=int,
        default=None,
        help="Total seconds to wait for Slack answers before falling back to a manual pause",
    )
    parser.add_argument("parts", nargs="*")
    return parser


def _config_from_args(args: argparse.Namespace) -> HarnessConfig:
    return HarnessConfig.from_env(
        family=args.family,
        timeout=args.timeout,
        claude_model=args.claude_model,
        codex_model=args.codex_model,
        profile=getattr(args, "profile", None),
        slack=True if getattr(args, "slack", False) else None,
        slack_wait=getattr(args, "slack_wait", None),
    )


def _config_from_state(
    state: dict[str, object],
    *,
    slack: bool | None = None,
    slack_wait: int | None = None,
) -> HarnessConfig:
    options = state.get("options", {})
    assert isinstance(options, dict)
    family = state["primary_family"]
    assert family in {"claude", "codex"}
    return HarnessConfig(
        primary_family=family,
        profile_name=str(options.get("profile_name", "resumed-run")),
        profile_description=str(options.get("profile_description", "Resumed run.")),
        profiles_path=str(options.get("profiles_path", "")),
        claude_model=str(options.get("claude_model", "sonnet")),
        claude_effort=str(options.get("claude_effort", "medium")),
        claude_fallback_model=str(options.get("claude_fallback_model", "")),
        codex_model=str(options.get("codex_model", "gpt-5.6-terra")),
        codex_reasoning=str(options.get("codex_reasoning", "medium")),
        stage_timeout=int(options.get("timeout", 900)),
        claude_max_budget_usd=float(options.get("claude_max_budget_usd", 8.0)),
        council_workers=int(options.get("council_workers", 3)),
        slack_enabled=bool(options.get("slack_enabled", False)) if slack is None else slack,
        slack_user=str(options.get("slack_user", "")),
        slack_wait_seconds=(
            int(options.get("slack_wait_seconds", 1800)) if slack_wait is None else slack_wait
        ),
        slack_budget_usd=float(options.get("slack_budget_usd", 5.0)),
        slack_max_clarifications=int(options.get("slack_max_clarifications", 3)),
        slack_poll_seconds=int(options.get("slack_poll_seconds", 20)),
    )


def _repo_for_state(current: GitRepo, state: dict[str, object]) -> GitRepo:
    source = Path(str(state["source_repo"])).resolve()
    return GitRepo(root=source, common_git_dir=current.common_git_dir)


def _status(repo: GitRepo, run_id: str | None) -> int:
    if run_id:
        states = [RunStore(repo.common_git_dir, run_id).load()]
    else:
        states = list_runs(repo.common_git_dir)
    if not states:
        print("No ai-harness runs for this repository.")
        return 0
    for state in states:
        completed = sum(
            1 for stage in state.get("stages", {}).values() if stage.get("status") == "completed"
        )
        print(
            f"{state['id']}  {state['kind']:<6}  {state['status']:<9}  "
            f"{completed}/{len(state.get('stages', {}))} stages  {state['updated_at']}"
        )
        if run_id:
            print(json.dumps(state, indent=2, sort_keys=True))
    return 0


def _parse_answers(state: dict[str, object], values: list[str]) -> dict[str, str] | None:
    pending = state.get("pending_input")
    # Collected Slack answers may be partial. With no explicit terminal answers, leave them in
    # progress so Slack can resume when enabled or the run can pause cleanly when disabled.
    if not values:
        return None
    carried = (
        collected_slack_answers(state, pending) if isinstance(pending, dict) else {}
    )
    if not isinstance(pending, dict):
        raise HarnessError("This run has no pending planning questions")
    questions = pending.get("questions")
    if not isinstance(questions, list):
        raise HarnessError("Pending planning questions are corrupt")
    identifiers = [str(item["id"]) for item in questions]
    # The shorthand form targets what is still outstanding, so it keeps working after Slack
    # already collected some of the answers.
    outstanding = [item for item in identifiers if item not in carried]
    answers: dict[str, str] = dict(carried)
    explicit: set[str] = set()
    for value in values:
        if "=" in value:
            identifier, answer = value.split("=", 1)
            identifier = identifier.strip()
        elif len(outstanding) == 1 and len(values) == 1:
            identifier, answer = outstanding[0], value
        else:
            raise HarnessError("Use --answer 'Q001=your answer' for each pending question")
        if identifier in explicit:
            raise HarnessError(f"Duplicate answer for {identifier}")
        explicit.add(identifier)
        # An explicit --answer overrides whatever Slack collected for the same question.
        answers[identifier] = answer.strip()
    return answers


def _resume(
    repo: GitRepo,
    run_id: str,
    answer_values: list[str],
    progress: TerminalProgress,
    timeout: int | None = None,
    slack: bool | None = None,
    slack_wait: int | None = None,
) -> int:
    store = RunStore(repo.common_git_dir, run_id, progress=progress)
    state = store.load()
    if state["status"] == "completed":
        print(f"Run {run_id} is already complete.")
        if state.get("result"):
            print(json.dumps(state["result"], indent=2, sort_keys=True))
        return 0
    if timeout is not None:
        if timeout < 1:
            raise HarnessError("--timeout must be at least one second")
        state["options"]["timeout"] = timeout
        store.save(state)
    source_repo = _repo_for_state(repo, state)
    config = _config_from_state(state, slack=slack, slack_wait=slack_wait)
    progress(f"Resuming {state['kind']} run {run_id} ({state['status']})")
    progress(_model_profile_message(config))
    answers = _parse_answers(state, answer_values)
    with controller_lock(repo.common_git_dir):
        if state["kind"] == "task":
            result = execute_task(store, source_repo, config, answers=answers)
        elif state["kind"] == "review":
            if answers:
                raise HarnessError("PR review runs do not accept planning answers")
            result = execute_review(store, source_repo, config)
        else:
            raise HarnessError(f"Run type cannot be resumed: {state['kind']}")
    print(json.dumps(result, indent=2, sort_keys=True))
    progress(f"Resumed workflow complete in {progress.elapsed_seconds:.1f}s")
    return 0


def _family_profile(config: HarnessConfig, family: Family) -> str:
    model = config.model_for(family)
    resolved_effort = (
        config.codex_reasoning if family == "codex" else config.claude_effort
    )
    effort = f", effort={resolved_effort}"
    fallback = (
        f", fallback={config.claude_fallback_model}"
        if family == "claude" and config.claude_fallback_model
        else ""
    )
    return f"{family} (model={model}{effort}{fallback})"


def _model_profile_message(config: HarnessConfig) -> str:
    critic = other_family(config.primary_family)
    return (
        f"Model profile: {config.profile_name}; "
        f"primary={_family_profile(config, config.primary_family)}, "
        f"adversary={_family_profile(config, critic)}, "
        f"per-agent timeout={config.stage_timeout}s"
    )


def _format_config(config: HarnessConfig) -> str:
    return "\n".join(
        [
            "ai-harness resolved configuration:",
            f"- profile: {config.profile_name}",
            f"- description: {config.profile_description}",
            f"- profile file: {config.profiles_path}",
            f"- primary family: {config.primary_family}",
            f"- Claude model: {config.claude_model}",
            f"- Claude effort: {config.claude_effort}",
            f"- Claude fallback model: {config.claude_fallback_model or 'none'}",
            f"- Codex model: {config.codex_model}",
            f"- Codex reasoning: {config.codex_reasoning}",
            f"- Claude max budget: ${config.claude_max_budget_usd:.2f}",
            f"- per-agent timeout: {config.stage_timeout}s",
            f"- parallel council specialists: {config.council_workers}",
            (
                "Resolution order: CLI flags > AI_HARNESS_* environment > "
                "selected profile > built-in defaults."
            ),
        ]
    )


def _cleanup(repo: GitRepo, run_id: str) -> int:
    store = RunStore(repo.common_git_dir, run_id)
    state = store.load()
    worktree_value = state.get("worktree")
    if not worktree_value:
        print(f"Run {run_id} has no retained worktree.")
        return 0
    source_repo = _repo_for_state(repo, state)
    worktree = Path(worktree_value)
    with controller_lock(repo.common_git_dir):
        source_repo.remove_worktree(worktree)
        state = store.load()
        state["worktree"] = None
        state["cleanup"] = "worktree removed; branch retained"
        store.save(state)
    print(f"Removed clean harness worktree {worktree}. The branch was retained.")
    return 0


def _dispatch(argv: list[str]) -> int:
    if argv and argv[0] == "config":
        parser = argparse.ArgumentParser(prog="ai-harness config")
        parser.add_argument("--family", choices=["claude", "codex"], default=None)
        parser.add_argument("--profile", default=None)
        parser.add_argument("--timeout", type=int, default=None)
        parser.add_argument("--claude-model", default=None)
        parser.add_argument("--codex-model", default=None)
        args = parser.parse_args(argv[1:])
        print(_format_config(_config_from_args(args)))
        return 0

    if argv and argv[0] == "doctor":
        parser = argparse.ArgumentParser(prog="ai-harness doctor")
        parser.add_argument("--deep", action="store_true")
        parser.add_argument("--profile", default=None)
        parser.add_argument("--timeout", type=int, default=None)
        parser.add_argument("--slack", action="store_true", default=None)
        args = parser.parse_args(argv[1:])
        config = HarnessConfig.from_env(
            timeout=args.timeout, profile=args.profile, slack=args.slack
        )
        tools = shallow_doctor()
        deep = deep_doctor(config) if args.deep else None
        print(format_doctor(tools, deep, slack_doctor(config)))
        return 0

    if argv and argv[0] in {"status", "resume", "cleanup"}:
        command = argv[0]
        parser = argparse.ArgumentParser(prog=f"ai-harness {command}")
        parser.add_argument("run_id", nargs="?" if command == "status" else None)
        if command == "resume":
            parser.add_argument(
                "--answer",
                action="append",
                default=[],
                help="Answer a planning question as Q001=text; repeat for multiple questions",
            )
            parser.add_argument(
                "--timeout",
                type=int,
                default=None,
                help="Override and persist the per-stage wall-clock timeout in seconds",
            )
            slack_group = parser.add_mutually_exclusive_group()
            slack_group.add_argument(
                "--slack",
                dest="slack",
                action="store_true",
                default=None,
                help="Ask outstanding planning questions over Slack",
            )
            slack_group.add_argument(
                "--no-slack",
                dest="slack",
                action="store_false",
                default=None,
                help="Answer from the terminal even if the run enabled Slack",
            )
            parser.add_argument(
                "--slack-wait",
                type=int,
                default=None,
                help="Total seconds to wait for Slack answers on this resume",
            )
        args = parser.parse_args(argv[1:])
        repo = GitRepo.discover(Path.cwd())
        if command == "status":
            return _status(repo, args.run_id)
        if command == "resume":
            return _resume(
                repo,
                args.run_id,
                args.answer,
                TerminalProgress(),
                timeout=args.timeout,
                slack=args.slack,
                slack_wait=args.slack_wait,
            )
        return _cleanup(repo, args.run_id)

    parser = _common_parser()
    args = parser.parse_args(argv)
    if not args.parts:
        parser.error("provide a task prompt or /review PR_NUMBER")
    config = _config_from_args(args)
    repo = GitRepo.discover(Path.cwd())
    progress = TerminalProgress()
    progress(_model_profile_message(config))
    with controller_lock(repo.common_git_dir):
        if args.parts[0] == "/review":
            if args.local_only:
                raise HarnessError("--local-only is only valid with an implementation task")
            if args.slack or args.slack_wait is not None:
                raise HarnessError(
                    "--slack is only valid with an implementation task; "
                    "pull request reviews do not ask blocking questions"
                )
            if args.reviewer and args.all_reviewers:
                raise HarnessError("--reviewer and --all-reviewers cannot be combined")
            council = (
                [member.value for member in CANONICAL_ORDER]
                if args.all_reviewers
                else args.reviewer
            )
            if len(args.parts) < 2:
                parser.error("/review requires a pull request number")
            try:
                number = int(args.parts[1])
            except ValueError as exc:
                raise HarnessError(f"Invalid pull request number: {args.parts[1]}") from exc
            review_parts = args.parts[2:]
            if not any(not part.startswith("@") for part in review_parts):
                review_parts = [*review_parts, "Review adversarially for bugs before they happen."]
            prompt, references = split_prompt_and_references(review_parts, Path.cwd())
            store = start_review(
                repo,
                config=config,
                number=number,
                prompt=prompt,
                references=references,
                publish=not args.no_publish,
                council=council,
                progress=progress,
            )
            result = store.load()["result"]
            print(f"Review run {store.run_id} complete.")
            print(f"Review: {result['review']}")
            if result.get("url"):
                print(f"Published: {result['url']}")
            elif result["published"]:
                print("Review was already published for this exact head and prompt.")
            else:
                print("Not published (--no-publish).")
            progress(f"PR review command complete in {progress.elapsed_seconds:.1f}s")
            return 0
        if args.no_publish:
            raise HarnessError("--no-publish is only valid with /review")
        if args.reviewer or args.all_reviewers:
            raise HarnessError("--reviewer and --all-reviewers are only valid with /review")
        prompt, references = split_prompt_and_references(args.parts, Path.cwd())
        store = start_task(
            repo,
            config=config,
            prompt=prompt,
            references=references,
            deliver=not args.local_only,
            progress=progress,
        )
        state = store.load()
        for warning in state.get("warnings", []):
            print(f"warning: {warning}", file=sys.stderr)
        result = state["result"]
        print(f"Task run {store.run_id} complete.")
        delivery = result.get("delivery")
        if delivery:
            pull = delivery["pull_request"]
            review = delivery["review"]
            print(f"Commit: {delivery['commit']}")
            print(f"Draft PR: {pull['url']}")
            if review["publication"].get("url"):
                print(f"Review: {review['publication']['url']}")
            elif review["publication"].get("idempotent"):
                print("Review: already published for this exact implementation")
            print(f"Findings: {review['findings']}")
        else:
            print(f"Worktree: {result['worktree']}")
            print(f"Branch: {result['branch']}")
            print(f"Patch artifact: {result['patch']}")
        progress(f"Task command complete in {progress.elapsed_seconds:.1f}s")
        return 0


def main() -> None:
    try:
        raise SystemExit(_dispatch(sys.argv[1:]))
    except AwaitingInput as exc:
        print(f"Task run {exc.run_id} paused for human input.")
        for question in exc.questions:
            print(f"\n{question['id']}: {question['question']}")
            print(f"Why this blocks planning: {question['why_blocking']}")
            if question["suggested_default"]:
                print(f"Suggested default: {question['suggested_default']}")
        print(
            "\nResume with one --answer 'Q001=your answer' argument per question:\n"
            f"  ai-harness resume {exc.run_id} --answer 'Q001=...'"
        )
        raise SystemExit(0) from exc
    except HarnessError as exc:
        print(f"ai-harness: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
