from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import HarnessConfig, harness_worktree_path
from .context import context_prompt, copy_references, verify_references
from .council import (
    Member,
    aggregate_reviews,
    apply_lead_routing,
    apply_lead_verdict,
    clean_summary,
    deterministic_members,
    empty_lead_verdict,
    empty_routing,
    findings_manifest,
    index_findings,
    member_family,
    normalize_review,
    parse_member,
    persona,
    render_council_report,
    stage_name,
    synthesize_final,
)
from .errors import HarnessError, ProviderError, StalePullRequest
from .git import GitRepo
from .github import post_review, pull_request, repository_name, review_marker_exists
from .progress import ProgressCallback
from .prompts import lead_consolidation_prompt, lead_routing_prompt, specialist_prompt
from .providers import ProviderRequest, ProviderRunner
from .schema import require_unique_ids, validate_output
from .state import RunStore, list_runs, new_run_id

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffIndex:
    changed: dict[str, set[tuple[str, int]]]
    sources: dict[str, dict[str, str]]


def parse_diff_index(diff: str) -> DiffIndex:
    changed: dict[str, set[tuple[str, int]]] = {}
    sources: dict[str, dict[str, str]] = {}
    old_path: str | None = None
    new_path: str | None = None
    github_path: str | None = None
    old_line = 0
    new_line = 0
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("--- "):
            value = line[4:]
            old_path = None if value == "/dev/null" else _strip_diff_prefix(value, "a/")
            in_hunk = False
            continue
        if line.startswith("+++ "):
            value = line[4:]
            new_path = None if value == "/dev/null" else _strip_diff_prefix(value, "b/")
            github_path = new_path or old_path
            if github_path:
                changed.setdefault(github_path, set())
                sources[github_path] = {
                    "LEFT": old_path or github_path,
                    "RIGHT": new_path or github_path,
                }
            in_hunk = False
            continue
        match = _HUNK.match(line)
        if match:
            old_line = int(match.group(1))
            new_line = int(match.group(3))
            in_hunk = True
            continue
        if not in_hunk or github_path is None:
            continue
        if line.startswith("+"):
            changed[github_path].add(("RIGHT", new_line))
            new_line += 1
        elif line.startswith("-"):
            changed[github_path].add(("LEFT", old_line))
            old_line += 1
        elif line.startswith(" "):
            old_line += 1
            new_line += 1
        elif line.startswith("\\ No newline"):
            continue

    return DiffIndex(changed=changed, sources=sources)


def _strip_diff_prefix(value: str, prefix: str) -> str:
    if value.startswith(prefix):
        return value[len(prefix) :]
    return value


def _line_from_blob(blob: str, line_number: int) -> str | None:
    lines = blob.splitlines()
    if line_number < 1 or line_number > len(lines):
        return None
    return lines[line_number - 1]


def validate_pr_findings(
    final: dict[str, Any],
    *,
    index: DiffIndex,
    repo: GitRepo,
    base: str,
    head: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    require_unique_ids(final)
    inline: list[dict[str, Any]] = []
    summary_only: list[dict[str, Any]] = []
    blob_cache: dict[tuple[str, str], str | None] = {}
    for finding in final["findings"]:
        valid_evidence: list[dict[str, Any]] = []
        invalid_reasons: list[str] = []
        for evidence in finding["evidence"]:
            path = evidence["path"]
            side = evidence["side"]
            line = evidence["line"]
            if path not in index.changed or (side, line) not in index.changed[path]:
                invalid_reasons.append(f"{path}:{line} is not a changed {side} diff line")
                continue
            source_path = index.sources[path][side]
            commit = base if side == "LEFT" else head
            cache_key = (commit, source_path)
            if cache_key not in blob_cache:
                blob_cache[cache_key] = repo.show_file(commit, source_path)
            blob = blob_cache[cache_key]
            actual = _line_from_blob(blob, line) if blob is not None else None
            if actual is None or actual.strip() != evidence["excerpt"].strip():
                invalid_reasons.append(f"{path}:{line} excerpt does not match the exact blob")
                continue
            valid_evidence.append(evidence)
        validated = {
            **finding,
            "valid_evidence": valid_evidence,
            "invalid_evidence": invalid_reasons,
        }
        if valid_evidence:
            inline.append(validated)
        else:
            summary_only.append(validated)
    return inline, summary_only


def render_review_body(
    final: dict[str, Any],
    *,
    marker: str,
    summary_only_ids: set[str],
) -> str:
    findings = final["findings"]
    if not findings:
        return f"No actionable findings.\n\n{final['summary']}\n\n{marker}"
    parts = [final["summary"], "", "## Findings", ""]
    for finding in findings:
        location = finding["evidence"][0]
        suffix = (
            " (summary only: inline location was not validated)"
            if finding["id"] in summary_only_ids
            else ""
        )
        parts.extend(
            [
                f"### [{finding['severity']}] {finding['id']}: {finding['title']}{suffix}",
                "",
                finding["body"],
                "",
                (
                    f"Evidence: `{location['path']}:{location['line']}` "
                    f"({location['side']}) — {location['rationale']}"
                ),
                "",
                f"Recommendation: {finding['recommendation']}",
                "",
            ]
        )
    parts.append(marker)
    return "\n".join(parts)


def _inline_comment(finding: dict[str, Any]) -> dict[str, Any]:
    evidence = finding["valid_evidence"][0]
    body = (
        f"**[{finding['severity']}] {finding['id']}: {finding['title']}**\n\n"
        f"{finding['body']}\n\nRecommendation: {finding['recommendation']}"
    )
    return {
        "path": evidence["path"],
        "line": evidence["line"],
        "side": evidence["side"],
        "body": body,
    }


def _review_key(
    name: str,
    number: int,
    head: str,
    prompt: str,
    context: list[dict[str, Any]],
    council: list[str] | None,
) -> str:
    material = json.dumps(
        {
            "repo": name,
            "number": number,
            "head": head,
            "prompt": prompt,
            "context": [item["sha256"] for item in context],
            "council": council or "auto",
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(material).hexdigest()[:24]


def publish_comment_review(
    *,
    repo_path: Path,
    name_with_owner: str,
    number: int,
    head: str,
    marker: str,
    body: str,
    inline: list[dict[str, Any]],
    publish: bool,
) -> dict[str, Any]:
    if not publish:
        return {"published": False, "reason": "--no-publish"}
    if review_marker_exists(repo_path, name_with_owner, number, marker):
        return {"published": True, "idempotent": True}
    payload = {
        "commit_id": head,
        "event": "COMMENT",
        "body": body,
        "comments": [_inline_comment(item) for item in inline],
    }
    result = post_review(repo_path, name_with_owner, number, payload)
    fallback = False
    if result.returncode != 0 and (
        "422" in result.stderr or "Validation Failed" in result.stderr
    ):
        fallback = True
        payload.pop("comments", None)
        result = post_review(repo_path, name_with_owner, number, payload)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise HarnessError(f"Could not publish COMMENT review: {detail[-2000:]}")
    response = json.loads(result.stdout) if result.stdout.strip() else {}
    return {
        "published": True,
        "idempotent": False,
        "summary_only_fallback": fallback,
        "url": response.get("html_url"),
    }


def require_unchanged_pr_head(repo_path: Path, number: int, expected_head: str) -> None:
    current = pull_request(repo_path, number)
    actual = str(current.get("headRefOid"))
    if actual != expected_head:
        raise StalePullRequest(
            f"PR #{number} moved from {expected_head} to {actual}; nothing was published"
        )


def find_previous_review(
    repo: GitRepo, *, number: int, head: str, head_ref: str | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Find the newest completed review whose head is an ancestor of this update."""
    for state in list_runs(repo.common_git_dir):
        if state.get("kind") != "review" or state.get("status") != "completed":
            continue
        metadata = state.get("pr")
        if not isinstance(metadata, dict) or int(metadata.get("number") or 0) != number:
            continue
        if head_ref is not None and str(metadata.get("headRefName") or "") != head_ref:
            continue
        previous_head = str(metadata.get("headRefOid") or "")
        if previous_head == head:
            return None
        if not previous_head or not repo.is_ancestor(previous_head, head):
            continue
        previous_store = RunStore(repo.common_git_dir, str(state["id"]))
        final = previous_store.read_completed_stage("review-final")
        if final is not None:
            return state, final
    return None


def _follow_up_prompt(state: dict[str, Any]) -> str:
    follow_up = state.get("follow_up_review")
    if not isinstance(follow_up, dict):
        return ""
    return f"""

FOLLOW-UP REVIEW SCOPE
Previous reviewed head: {follow_up['previous_head']}
Previous final review: {follow_up['previous_review']}
Intervening update delta: {follow_up['update_diff']}

This PR head extends a head that already completed the full adversarial review cycle. Use the
previous final review and the intervening delta as the primary scope: verify that prior findings
were addressed, find defects introduced by the update, and inspect unchanged PR code only where
needed to validate an interaction. Do not restart a line-by-line review of unchanged code or
repeat resolved findings."""


def start_review(
    repo: GitRepo,
    *,
    config: HarnessConfig,
    number: int,
    prompt: str,
    references: Sequence[Path],
    publish: bool,
    council: Sequence[str] | None = None,
    progress: ProgressCallback | None = None,
) -> RunStore:
    selection = sorted({parse_member(name).value for name in council}) if council else None
    if progress is not None:
        progress(f"Loading pull request #{number} with gh")
    metadata = pull_request(repo.root, number)
    if metadata.get("state") != "OPEN":
        raise HarnessError(f"Pull request #{number} is not open")
    changed_files = int(metadata.get("changedFiles") or 0)
    changed_lines = int(metadata.get("additions") or 0) + int(metadata.get("deletions") or 0)
    if changed_files > config.max_pr_files or changed_lines > config.max_pr_lines:
        raise HarnessError(
            f"PR is over the configured review limit ({changed_files} files, {changed_lines} lines)"
        )
    head = str(metadata["headRefOid"])
    base = str(metadata["baseRefOid"])
    if progress is not None:
        progress(
            f"Loaded PR #{number}: {metadata.get('title', '')} "
            f"({base[:12]}...{head[:12]})"
        )
        progress("Ensuring the exact base and head commits are available")
    repo.fetch_pull_request_head(number, head)
    repo.ensure_commit(base, str(metadata.get("baseRefName") or ""))
    run_id = new_run_id("review")
    store = RunStore.create(
        repo.common_git_dir,
        run_id=run_id,
        kind="review",
        source_repo=repo.root,
        source_head=repo.head(),
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
            "publish": publish,
            "pr_number": number,
            "council_members": selection,
        },
        progress=progress,
    )
    copy_references(store, references)
    worktree = harness_worktree_path(repo.root, run_id)
    store.log(f"Creating detached review worktree {worktree}")
    repo.create_detached_worktree(worktree, head)
    store.log("Building the canonical merge-base pull-request diff")
    diff = repo.three_dot_diff(base, head, cwd=worktree)
    diff_path = store.write_text_artifact("pull-request.diff", diff)
    previous = find_previous_review(
        repo, number=number, head=head, head_ref=str(metadata.get("headRefName") or "")
    )
    follow_up_review: dict[str, Any] | None = None
    if previous is not None:
        previous_state, previous_final = previous
        previous_head = str(previous_state["pr"]["headRefOid"])
        update_diff_path = store.write_text_artifact(
            "review-update.diff", repo.update_diff(previous_head, head, cwd=worktree)
        )
        previous_review_path = store.write_text_artifact(
            "previous-review.json",
            json.dumps(previous_final, indent=2, sort_keys=True) + "\n",
        )
        follow_up_review = {
            "previous_run_id": previous_state["id"],
            "previous_head": previous_head,
            "previous_review": str(previous_review_path),
            "update_diff": str(update_diff_path),
        }
    name = repository_name(repo.root)
    state = store.load()
    key = _review_key(name, number, head, prompt, state["context"], selection)
    state.update(
        {
            "worktree": str(worktree),
            "repository": name,
            "pr": metadata,
            "review_key": key,
            "review_marker": f"<!-- ai-harness-review:{key} -->",
            "diff": str(diff_path),
            "follow_up_review": follow_up_review,
        }
    )
    store.save(state)
    store.log(f"Review artifacts: {store.root}")
    execute_review(store, repo, config)
    return store


def create_local_review(
    repo: GitRepo,
    *,
    config: HarnessConfig,
    base: str,
    head: str,
    branch: str,
    title: str,
    prompt: str,
    references: Sequence[Path],
    progress: ProgressCallback | None = None,
) -> RunStore:
    """Create a resumable review run for a local branch diff without GitHub metadata."""
    repo.ensure_commit(base)
    repo.ensure_commit(head)
    run_id = new_run_id("review")
    store = RunStore.create(
        repo.common_git_dir,
        run_id=run_id,
        kind="review",
        source_repo=repo.root,
        source_head=repo.head(),
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
            "publish": False,
            "pr_number": 0,
            "local_review": True,
        },
        progress=progress,
    )
    copy_references(store, references)
    worktree = harness_worktree_path(repo.root, run_id)
    store.log(f"Creating detached local-review worktree for {head[:12]}")
    repo.create_detached_worktree(worktree, head)
    diff_path = store.write_text_artifact(
        "pull-request.diff", repo.three_dot_diff(base, head, cwd=worktree)
    )
    metadata = {
        "number": 0,
        "state": "OPEN",
        "isDraft": True,
        "headRefOid": head,
        "baseRefOid": base,
        "title": title,
        "body": "",
        "headRefName": branch,
        "baseRefName": "",
    }
    previous = find_previous_review(repo, number=0, head=head, head_ref=branch)
    follow_up_review: dict[str, Any] | None = None
    if previous is not None:
        previous_state, previous_final = previous
        previous_head = str(previous_state["pr"]["headRefOid"])
        update_diff_path = store.write_text_artifact(
            "review-update.diff", repo.update_diff(previous_head, head, cwd=worktree)
        )
        previous_review_path = store.write_text_artifact(
            "previous-review.json",
            json.dumps(previous_final, indent=2, sort_keys=True) + "\n",
        )
        follow_up_review = {
            "previous_run_id": previous_state["id"],
            "previous_head": previous_head,
            "previous_review": str(previous_review_path),
            "update_diff": str(update_diff_path),
        }
    state = store.load()
    key = _review_key(str(repo.root), 0, head, prompt, state["context"], None)
    state.update(
        {
            "worktree": str(worktree),
            "repository": "local",
            "pr": metadata,
            "review_key": key,
            "review_marker": f"<!-- ai-harness-review:{key} -->",
            "diff": str(diff_path),
            "follow_up_review": follow_up_review,
        }
    )
    store.save(state)
    store.log(f"Local implementation-review artifacts: {store.root}")
    return store


def publish_local_review(
    review_store: RunStore,
    *,
    repo: GitRepo,
    name_with_owner: str,
    number: int,
) -> dict[str, Any]:
    state = review_store.load()
    head = str(state["pr"]["headRefOid"])
    require_unchanged_pr_head(repo.root, number, head)
    validation = review_store.read_completed_stage("evidence-validation")
    if validation is None:
        raise HarnessError("Local implementation review has no validated findings")
    body = str(validation["body"])
    inline = list(validation["inline_findings"])
    return publish_comment_review(
        repo_path=repo.root,
        name_with_owner=name_with_owner,
        number=number,
        head=head,
        marker=str(state["review_marker"]),
        body=body,
        inline=inline,
        publish=True,
    )


def _provider_context(store: RunStore, state: dict[str, Any]) -> dict[str, Any]:
    context_dir = store.root / "context"
    return {
        "cwd": Path(state["worktree"]),
        "context_dirs": tuple(
            path for path in (context_dir, store.root) if path.is_dir()
        ),
    }


def _routing(
    store: RunStore,
    runner: ProviderRunner,
    config: HarnessConfig,
    *,
    state: dict[str, Any],
    changed_paths: set[str],
    request: dict[str, Any],
) -> tuple[tuple[Member, ...], frozenset[Member], dict[str, Any]]:
    """Resolve the council. On resume this replays the artifact instead of re-deciding."""
    requested = state["options"].get("council_members")
    deterministic = deterministic_members(changed_paths)
    routing = store.read_completed_stage("review-routing")
    if requested:
        members = tuple(parse_member(name) for name in requested)
        if routing is None:
            routing = empty_routing(members, "Council selected by the caller")
            store.begin_stage("review-routing", "controller")
            store.complete_stage("review-routing", routing)
        return members, frozenset(), routing
    if routing is None:
        routing = runner.run(
            ProviderRequest(
                family=state["primary_family"],
                stage="review-routing",
                prompt=lead_routing_prompt(
                    charter=persona("review_lead"),
                    changed_paths=sorted(changed_paths),
                    deterministic=[member.value for member in deterministic],
                    **request,
                ),
                schema_name="review_routing",
                writable=False,
                timeout=config.stage_timeout,
                **_provider_context(store, state),
            )
        )
    else:
        validate_output("review_routing", routing)
    members, focus = apply_lead_routing(
        routing, deterministic=deterministic, changed_paths=changed_paths
    )
    return members, focus, routing


def _run_specialists(
    store: RunStore,
    runner: ProviderRunner,
    config: HarnessConfig,
    *,
    state: dict[str, Any],
    members: tuple[Member, ...],
    focus: frozenset[Member],
    request: dict[str, Any],
) -> dict[Member, dict[str, Any]]:
    reviews: dict[Member, dict[str, Any]] = {}
    pending: list[Member] = []
    for member in members:
        completed = store.read_completed_stage(stage_name(member))
        if completed is None:
            pending.append(member)
        else:
            reviews[member] = normalize_review(
                member, validate_output("specialist_review", completed)
            )
    if not pending:
        return reviews

    primary = state["primary_family"]
    contract = persona("reviewer-common")
    provider_context = _provider_context(store, state)

    def invoke(member: Member) -> dict[str, Any]:
        return runner.run(
            ProviderRequest(
                family=member_family(member, primary),
                stage=stage_name(member),
                prompt=specialist_prompt(
                    contract=contract,
                    charter=persona(member.value),
                    focused=member in focus,
                    **request,
                ),
                schema_name="specialist_review",
                writable=False,
                timeout=config.stage_timeout,
                **provider_context,
            )
        )

    errors: dict[str, str] = {}
    workers = min(config.council_workers, len(pending))
    store.log(f"Running {len(pending)} council specialist(s) with {workers} worker(s)")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(invoke, member): member for member in pending}
        for future in as_completed(futures):
            member = futures[future]
            try:
                reviews[member] = normalize_review(member, future.result())
            except Exception as exc:  # noqa: BLE001 - reported per member below
                errors[member.value] = str(exc)
    if errors:
        # Specialists that finished have already persisted their stage, so a resume
        # re-runs only the failures.
        store.update(status="failed")
        detail = "; ".join(f"{name}: {message}" for name, message in sorted(errors.items()))
        raise ProviderError(f"Council specialists failed — {detail}")
    return reviews


def execute_review(store: RunStore, repo: GitRepo, config: HarnessConfig) -> dict[str, Any]:
    state = store.load()
    number = int(state["options"]["pr_number"])
    publish = bool(state["options"]["publish"])
    local_review = bool(state["options"].get("local_review", False))
    metadata = state["pr"]
    base = str(metadata["baseRefOid"])
    head = str(metadata["headRefOid"])
    worktree = Path(state["worktree"]) if state.get("worktree") else None
    if worktree is None or (not worktree.is_dir() and state["status"] != "completed"):
        raise HarnessError("PR review worktree is missing")
    if worktree is None or not worktree.is_dir():
        return state.get("result", {})
    diff_path = Path(state["diff"])
    diff = diff_path.read_text(encoding="utf-8")
    index = parse_diff_index(diff)
    changed_paths = set(index.changed)
    runner = ProviderRunner(config, store)
    request = {
        "number": number,
        "title": str(metadata["title"]),
        "branch": str(metadata.get("headRefName") or ""),
        "base": base,
        "head": head,
        "diff_path": diff_path,
        "user_prompt": state["prompt"],
        "context": context_prompt(state),
        "follow_up": _follow_up_prompt(state),
    }

    members, focus, routing = _routing(
        store, runner, config, state=state, changed_paths=changed_paths, request=request
    )
    verify_references(store.load())

    reviews = _run_specialists(
        store, runner, config, state=state, members=members, focus=focus, request=request
    )
    aggregate_reviews(members, reviews)
    verify_references(store.load())

    indexed = index_findings(members, reviews)
    manifest_path = store.write_text_artifact(
        "specialist-findings.json", findings_manifest(indexed)
    )

    lead = store.read_completed_stage("review-consolidation")
    if lead is None:
        if indexed:
            lead = runner.run(
                ProviderRequest(
                    family=state["primary_family"],
                    stage="review-consolidation",
                    prompt=lead_consolidation_prompt(
                        charter=persona("review_lead"),
                        manifest_path=manifest_path,
                        council=[member.value for member in members],
                        **request,
                    ),
                    schema_name="review_lead",
                    writable=False,
                    timeout=config.stage_timeout,
                    **_provider_context(store, state),
                )
            )
        else:
            lead = empty_lead_verdict(clean_summary(members, reviews))
            store.begin_stage("review-consolidation", "controller")
            store.complete_stage("review-consolidation", lead)
    else:
        validate_output("review_lead", lead)
    groups = apply_lead_verdict(lead, indexed)
    verify_references(store.load())

    final = store.read_completed_stage("review-final")
    if final is None:
        final = validate_output("pr_review", synthesize_final(groups, summary=lead["summary"]))
        store.begin_stage("review-final", "controller")
        store.complete_stage("review-final", final)
    else:
        validate_output("pr_review", final)
    require_unique_ids(final)
    store.write_text_artifact(
        "council-report.md",
        render_council_report(
            title=request["title"],
            base=base,
            head=head,
            expected=members,
            reviews=reviews,
            routing=routing,
            deterministic=deterministic_members(changed_paths),
            lead=lead,
            groups=groups,
            indexed=indexed,
        ),
    )

    inline, summary_only = validate_pr_findings(
        final, index=index, repo=repo, base=base, head=head
    )
    body = render_review_body(
        final,
        marker=state["review_marker"],
        summary_only_ids={item["id"] for item in summary_only},
    )
    validated = {
        "body": body,
        "inline_findings": inline,
        "summary_only_findings": summary_only,
    }
    store.write_text_artifact("review.md", body + "\n")
    if store.read_completed_stage("evidence-validation") is None:
        store.begin_stage("evidence-validation", "controller")
        store.complete_stage("evidence-validation", validated)

    publication = store.read_completed_stage("publication")
    if publication is None:
        if not local_review:
            require_unchanged_pr_head(repo.root, number, head)
        store.begin_stage("publication", "controller")
        try:
            publication = publish_comment_review(
                repo_path=repo.root,
                name_with_owner=state["repository"],
                number=number,
                head=head,
                marker=state["review_marker"],
                body=body,
                inline=inline,
                publish=publish,
            )
            store.complete_stage("publication", publication)
        except Exception as exc:
            store.fail_stage("publication", str(exc))
            raise

    if repo.status(cwd=worktree):
        raise HarnessError(f"PR review unexpectedly changed its read-only worktree: {worktree}")
    store.log("Removing detached review worktree")
    repo.remove_worktree(worktree)
    final_state = store.load()
    final_state["worktree"] = None
    final_state["status"] = "completed"
    final_state["result"] = {
        "published": publication["published"],
        "review": str(store.root / "review.md"),
        "council_report": str(store.root / "council-report.md"),
        "url": publication.get("url"),
        "reviewers": [member.value for member in members],
        "findings": len(final["findings"]),
        "dismissed": len(lead["dismissed_groups"]),
        "summary_only": len(summary_only),
    }
    store.save(final_state)
    store.log(
        f"Review council complete with {len(final['findings'])} finding(s) from "
        f"{len(members)} specialist(s), {len(lead['dismissed_groups'])} dismissed; "
        f"artifact {store.root / 'review.md'}"
    )
    return final_state["result"]
