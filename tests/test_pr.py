from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.errors import HarnessError, ProviderError, StalePullRequest
from ai_harness.git import GitRepo
from ai_harness.pr import (
    _line_from_blob,
    create_local_review,
    execute_review,
    find_previous_review,
    parse_diff_index,
    publish_comment_review,
    publish_local_review,
    render_review_body,
    require_unchanged_pr_head,
    start_review,
    validate_pr_findings,
)
from ai_harness.process import CommandResult, run_command
from ai_harness.schema import validate_output
from ai_harness.state import RunStore, list_runs


def _finding(*, line: int, excerpt: str, identifier: str = "F001") -> dict[str, object]:
    return {
        "id": identifier,
        "severity": "high",
        "title": "Incorrect branch",
        "body": "The changed branch returns the wrong value.",
        "evidence": [
            {
                "path": "tracked.txt",
                "line": line,
                "side": "RIGHT",
                "excerpt": excerpt,
                "rationale": "This is the changed return.",
            }
        ],
        "recommendation": "Return the expected value.",
    }


def test_diff_evidence_requires_changed_line_and_exact_blob(git_repo: GitRepo) -> None:
    base = git_repo.head()
    (git_repo.root / "tracked.txt").write_text("original\nnew line\n", encoding="utf-8")
    run_command(["git", "add", "tracked.txt"], cwd=git_repo.root)
    run_command(["git", "commit", "-m", "change"], cwd=git_repo.root)
    head = git_repo.head()
    diff = git_repo.three_dot_diff(base, head, cwd=git_repo.root)
    index = parse_diff_index(diff)
    valid = {"summary": "Found one issue.", "findings": [_finding(line=2, excerpt="new line")]}
    inline, summary = validate_pr_findings(
        valid, index=index, repo=git_repo, base=base, head=head
    )
    assert [item["id"] for item in inline] == ["F001"]
    assert not summary

    invalid = {"summary": "Found one issue.", "findings": [_finding(line=1, excerpt="original")]}
    inline, summary = validate_pr_findings(
        invalid, index=index, repo=git_repo, base=base, head=head
    )
    assert not inline
    assert summary[0]["invalid_evidence"]


def test_parse_diff_index_handles_deletions_no_newline_and_unprefixed_paths() -> None:
    diff = "\n".join(
        [
            "diff --git a/old.txt b/old.txt",
            "--- old.txt",
            "+++ old.txt",
            "@@ -1,2 +1,1 @@",
            "-gone",
            " kept",
            "\\ No newline at end of file",
            "diff --git a/removed.txt b/removed.txt",
            "--- a/removed.txt",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-bye",
            "stray line outside any hunk",
        ]
    )
    index = parse_diff_index(diff)
    assert index.changed["old.txt"] == {("LEFT", 1)}
    assert index.sources["old.txt"] == {"LEFT": "old.txt", "RIGHT": "old.txt"}
    assert index.changed["removed.txt"] == {("LEFT", 1)}
    assert index.sources["removed.txt"] == {"LEFT": "removed.txt", "RIGHT": "removed.txt"}
    assert _line_from_blob("one\ntwo\n", 2) == "two"
    assert _line_from_blob("one\n", 5) is None
    assert _line_from_blob("one\n", 0) is None


def test_clean_review_body_is_always_nonempty() -> None:
    body = render_review_body(
        {"summary": "The diff is clean.", "findings": []},
        marker="<!-- ai-harness-review:key -->",
        summary_only_ids=set(),
    )
    assert body.startswith("No actionable findings.")
    assert "<!-- ai-harness-review:key -->" in body


def test_publication_retries_422_without_inline_comments(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr("ai_harness.pr.review_marker_exists", lambda *args: False)

    def fake_post(repo, name, number, payload):
        calls.append(payload.copy())
        if len(calls) == 1:
            return CommandResult(("gh",), 1, "", "HTTP 422: Validation Failed")
        return CommandResult(("gh",), 0, json.dumps({"html_url": "https://example/review"}), "")

    monkeypatch.setattr("ai_harness.pr.post_review", fake_post)
    inline = [
        {
            **_finding(line=2, excerpt="new line"),
            "valid_evidence": [
                {
                    "path": "tracked.txt",
                    "line": 2,
                    "side": "RIGHT",
                    "excerpt": "new line",
                    "rationale": "changed",
                }
            ],
        }
    ]
    result = publish_comment_review(
        repo_path=tmp_path,
        name_with_owner="owner/repo",
        number=16,
        head="a" * 40,
        marker="<!-- marker -->",
        body="review body",
        inline=inline,
        publish=True,
    )
    assert result["summary_only_fallback"] is True
    assert "comments" in calls[0]
    assert "comments" not in calls[1]
    assert calls[1]["event"] == "COMMENT"


def test_publication_failure_and_no_publish_are_reported(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ai_harness.pr.review_marker_exists", lambda *args: False)
    monkeypatch.setattr(
        "ai_harness.pr.post_review",
        lambda *args: CommandResult(("gh",), 1, "", "HTTP 500: boom"),
    )
    common = {
        "repo_path": tmp_path,
        "name_with_owner": "owner/repo",
        "number": 16,
        "head": "a" * 40,
        "marker": "<!-- marker -->",
        "body": "review body",
        "inline": [],
    }
    with pytest.raises(HarnessError, match="Could not publish COMMENT review: HTTP 500: boom"):
        publish_comment_review(publish=True, **common)
    assert publish_comment_review(publish=False, **common) == {
        "published": False,
        "reason": "--no-publish",
    }


def test_publication_marker_is_idempotent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ai_harness.pr.review_marker_exists", lambda *args: True)
    monkeypatch.setattr(
        "ai_harness.pr.post_review",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not publish")),
    )
    result = publish_comment_review(
        repo_path=tmp_path,
        name_with_owner="owner/repo",
        number=16,
        head="a" * 40,
        marker="<!-- marker -->",
        body="review body",
        inline=[],
        publish=True,
    )
    assert result == {"published": True, "idempotent": True}


def test_stale_pr_head_fails_before_publication(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "ai_harness.pr.pull_request", lambda *args: {"headRefOid": "b" * 40}
    )
    with pytest.raises(StalePullRequest, match="nothing was published"):
        require_unchanged_pr_head(tmp_path, 16, "a" * 40)


def _commit_change(git_repo: GitRepo, content: str, message: str) -> str:
    (git_repo.root / "tracked.txt").write_text(content, encoding="utf-8")
    run_command(["git", "add", "tracked.txt"], cwd=git_repo.root)
    run_command(["git", "commit", "-q", "-m", message], cwd=git_repo.root)
    return git_repo.head()


def _completed_review(
    git_repo: GitRepo, run_id: str, *, number: int, head: str, kind: str = "review", **pr_extra
) -> RunStore:
    store = RunStore.create(
        git_repo.common_git_dir,
        run_id=run_id,
        kind=kind,
        source_repo=git_repo.root,
        source_head=head,
        primary_family="claude",
        prompt="review",
        options={"pr_number": number},
    )
    store.begin_stage("review-final", "claude")
    store.complete_stage("review-final", {"summary": "Clean.", "findings": []})
    store.update(pr={"number": number, "headRefOid": head, **pr_extra}, status="completed")
    return store


def test_follow_up_review_uses_newest_completed_ancestor(git_repo: GitRepo) -> None:
    previous_head = git_repo.head()
    store = RunStore.create(
        git_repo.common_git_dir,
        run_id="20260809-000000-review-aaaaaa",
        kind="review",
        source_repo=git_repo.root,
        source_head=previous_head,
        primary_family="claude",
        prompt="review",
        options={"pr_number": 19},
    )
    store.begin_stage("review-final", "claude")
    previous_final = {"summary": "One issue.", "findings": [_finding(line=1, excerpt="original")]}
    store.complete_stage("review-final", previous_final)
    state = store.load()
    state["pr"] = {"number": 19, "headRefOid": previous_head, "headRefName": "feature"}
    state["status"] = "completed"
    store.save(state)

    (git_repo.root / "tracked.txt").write_text("updated\n", encoding="utf-8")
    run_command(["git", "add", "tracked.txt"], cwd=git_repo.root)
    run_command(["git", "commit", "-m", "update reviewed head"], cwd=git_repo.root)
    current_head = git_repo.head()

    found = find_previous_review(git_repo, number=19, head=current_head)
    assert found is not None
    found_state, found_final = found
    assert found_state["id"] == store.run_id
    assert found_final == previous_final
    assert find_previous_review(git_repo, number=20, head=current_head) is None
    assert find_previous_review(git_repo, number=19, head=current_head, head_ref="other") is None

    # Each distractor sorts ahead of the matching run and is invalid for exactly one reason, so
    # dropping either filter would return it instead. A real sibling commit keeps the ancestry
    # filter honest: the run is skipped because its head is not an ancestor, not because it
    # is missing from the repository.
    git_repo.run(["checkout", "-q", "-b", "sibling", previous_head])
    sibling_head = _commit_change(git_repo, "sibling\n", "sibling commit")
    git_repo.run(["checkout", "-q", "main"])

    _completed_review(
        git_repo, "20260809-000000-task-zzzzzz", number=19, head=previous_head, kind="task"
    )
    found = find_previous_review(git_repo, number=19, head=current_head)
    assert found is not None and found[0]["id"] == store.run_id

    _completed_review(git_repo, "20260809-000000-review-yyyyyy", number=19, head=sibling_head)
    found = find_previous_review(git_repo, number=19, head=current_head)
    assert found is not None and found[0]["id"] == store.run_id

    current_store = RunStore.create(
        git_repo.common_git_dir,
        run_id="20260809-000001-review-bbbbbb",
        kind="review",
        source_repo=git_repo.root,
        source_head=current_head,
        primary_family="claude",
        prompt="review again",
        options={"pr_number": 19},
    )
    current_store.begin_stage("review-final", "claude")
    current_store.complete_stage("review-final", {"summary": "Clean.", "findings": []})
    current_state = current_store.load()
    current_state["pr"] = {"number": 19, "headRefOid": current_head}
    current_state["status"] = "completed"
    current_store.save(current_state)
    assert find_previous_review(git_repo, number=19, head=current_head) is None


def test_follow_up_review_prefers_the_newest_of_two_eligible_ancestors(git_repo: GitRepo) -> None:
    older_head = git_repo.head()
    middle_head = _commit_change(git_repo, "middle\n", "middle")
    current_head = _commit_change(git_repo, "current\n", "current")
    _completed_review(git_repo, "20260809-000000-review-aaaaaa", number=19, head=older_head)
    newer = _completed_review(
        git_repo, "20260809-000001-review-bbbbbb", number=19, head=middle_head
    )

    found = find_previous_review(git_repo, number=19, head=current_head)

    assert found is not None
    assert found[0]["id"] == newer.run_id
    assert found[0]["pr"]["headRefOid"] == middle_head


# --- full review pipeline through fake providers ----------------------------------------------


def _metadata(base: str, head: str, number: int = 16) -> dict[str, object]:
    return {
        "number": number,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": head,
        "baseRefOid": base,
        "url": f"https://example/pr/{number}",
        "title": "Change tracked.txt",
        "body": "",
        "headRefName": "feature",
        "baseRefName": "main",
        "additions": 1,
        "deletions": 0,
        "changedFiles": 1,
    }


def _specialist_finding(
    *, excerpt: str, severity: str = "high", line: int = 2
) -> dict[str, object]:
    return {
        "severity": severity,
        "confidence": 0.9,
        "title": "Incorrect branch",
        "body": "The changed branch returns the wrong value.",
        "evidence": [
            {
                "path": "tracked.txt",
                "line": line,
                "side": "RIGHT",
                "excerpt": excerpt,
                "rationale": "This is the changed return.",
            }
        ],
        "recommendation": "Return the expected value.",
        "suggested_verification": "Assert the returned value in tests/test_tracked.py.",
    }


def _specialist_review(
    reviewer: str, findings: list[dict[str, object]], **overrides: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "reviewer": reviewer,
        "verdict": "comment" if findings else "pass",
        "summary": "Reviewed the changed branch.",
        "scope_reviewed": ["tracked.txt"],
        "residual_risks": [],
        "findings": findings,
    }
    value.update(overrides)
    return value


class _ReviewRunner:
    """Fake council: one valid finding, one whose excerpt cannot be validated."""

    prompts: list[tuple[str, str]] = []
    routing: dict[str, object] = {}
    consolidation: dict[str, object] = {}
    reviews: dict[str, dict[str, object]] = {}

    def __init__(self, config, store: RunStore):
        self.store = store

    def run(self, request):
        _ReviewRunner.prompts.append((request.stage, request.prompt))
        assert request.writable is False
        self.store.begin_stage(request.stage, request.family)
        if request.stage == "review-routing":
            value = dict(_ReviewRunner.routing)
        elif request.stage == "review-consolidation":
            value = dict(_ReviewRunner.consolidation)
        else:
            member = request.stage.removeprefix("review-")
            value = _ReviewRunner.reviews.get(member) or _specialist_review(member, [])
        try:
            value = validate_output(request.schema_name, value)
        except ProviderError as exc:
            self.store.fail_stage(request.stage, str(exc))
            raise
        self.store.complete_stage(request.stage, value)
        return value


def _default_council_responses() -> None:
    _ReviewRunner.prompts = []
    _ReviewRunner.routing = {
        "specialist_requests": [
            {
                "reviewer": "resumability_reviewer",
                "evidence_paths": ["tracked.txt"],
                "rationale": "The change rewrites persisted content.",
            }
        ],
        "focus_reviewers": ["correctness_reviewer"],
        "summary": "Added the resumability reviewer.",
    }
    _ReviewRunner.reviews = {
        "correctness_reviewer": _specialist_review(
            "correctness_reviewer", [_specialist_finding(excerpt="new line")]
        ),
        "refactorer": _specialist_review(
            "refactorer",
            [_specialist_finding(excerpt="does not match", severity="medium")],
        ),
    }
    _ReviewRunner.consolidation = {
        "accepted_groups": [
            {"source_ids": ["correctness_reviewer:1"], "rationale": "Real defect."},
            {"source_ids": ["refactorer:1"], "rationale": "Separate structural defect."},
        ],
        "dismissed_groups": [],
        "summary": "Two root causes retained.",
    }


@pytest.fixture
def review_env(git_repo: GitRepo, monkeypatch):
    """Patch every GitHub touchpoint; return (base, head) with metadata served for `head`."""
    _default_council_responses()
    base = git_repo.head()
    head = _commit_change(git_repo, "original\nnew line\n", "change")
    served = {"head": head}
    posted: list[dict[str, object]] = []
    monkeypatch.setattr("ai_harness.pr.ProviderRunner", _ReviewRunner)
    monkeypatch.setattr(
        "ai_harness.pr.pull_request", lambda repo_path, number: _metadata(base, served["head"])
    )
    monkeypatch.setattr("ai_harness.pr.repository_name", lambda repo_path: "owner/repo")
    monkeypatch.setattr("ai_harness.pr.review_marker_exists", lambda *args: False)

    def fake_post(repo_path, name, number, payload):
        posted.append(payload)
        return CommandResult(("gh",), 0, json.dumps({"html_url": "https://example/review/1"}), "")

    monkeypatch.setattr("ai_harness.pr.post_review", fake_post)
    monkeypatch.setattr(GitRepo, "fetch_pull_request_head", lambda self, number, head: None)
    return base, served, posted


def test_start_review_pipeline_validates_evidence_renders_and_publishes(
    git_repo: GitRepo, review_env, tmp_path: Path
) -> None:
    base, served, posted = review_env
    progress: list[str] = []
    notes = tmp_path / "notes.md"
    notes.write_text("Watch the second line.\n", encoding="utf-8")
    store = start_review(
        git_repo,
        config=HarnessConfig(),
        number=16,
        prompt="Look for bugs",
        references=[notes],
        publish=True,
        progress=progress.append,
    )
    state = store.load()
    assert state["status"] == "completed"
    assert state["worktree"] is None
    assert state["repository"] == "owner/repo"
    assert state["follow_up_review"] is None
    assert state["result"] == {
        "published": True,
        "review": str(store.root / "review.md"),
        "council_report": str(store.root / "council-report.md"),
        "url": "https://example/review/1",
        "reviewers": ["correctness_reviewer", "refactorer", "resumability_reviewer"],
        "findings": 2,
        "dismissed": 0,
        "summary_only": 1,
    }
    stages = [stage for stage, _ in _ReviewRunner.prompts]
    assert stages[0] == "review-routing"
    assert stages[-1] == "review-consolidation"
    assert sorted(stages[1:-1]) == [
        "review-correctness_reviewer",
        "review-refactorer",
        "review-resumability_reviewer",
    ]
    assert all("FOLLOW-UP REVIEW SCOPE" not in prompt for _, prompt in _ReviewRunner.prompts)

    # The lead's focus assignment reaches only the reviewer it named.
    focused = {
        stage
        for stage, prompt in _ReviewRunner.prompts
        if "specifically to you" in prompt
    }
    assert focused == {"review-correctness_reviewer"}

    review = (store.root / "review.md").read_text(encoding="utf-8")
    assert "### [high] F001: Incorrect branch\n" in review
    assert "F002: Incorrect branch (summary only" in review
    assert "Found by `correctness_reviewer:1`" in review
    assert state["review_marker"] in review
    report = (store.root / "council-report.md").read_text(encoding="utf-8")
    assert "`resumability_reviewer`: **pass**" in report
    assert "sources: correctness_reviewer:1" in report
    assert [comment["path"] for comment in posted[0]["comments"]] == ["tracked.txt"]
    assert posted[0]["commit_id"] == served["head"]
    assert any("Loaded PR #16" in line for line in progress)
    assert not Path(state["diff"]).parent.joinpath("worktree").exists()

    # A completed run has released its worktree, so it cannot be executed again.
    _ReviewRunner.prompts.clear()
    with pytest.raises(HarnessError, match="worktree is missing"):
        execute_review(store, git_repo, HarnessConfig())
    assert _ReviewRunner.prompts == []


def test_explicit_council_skips_routing_and_gets_its_own_marker(
    git_repo: GitRepo, review_env
) -> None:
    base, served, posted = review_env
    auto = start_review(
        git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
    )
    _ReviewRunner.prompts.clear()
    explicit = start_review(
        git_repo,
        config=HarnessConfig(),
        number=16,
        prompt="Look",
        references=[],
        publish=True,
        council=["refactorer", "correctness_reviewer"],
    )

    stages = [stage for stage, _ in _ReviewRunner.prompts]
    assert "review-routing" not in stages
    assert sorted(stages[:-1]) == ["review-correctness_reviewer", "review-refactorer"]
    assert explicit.load()["options"]["council_members"] == [
        "correctness_reviewer",
        "refactorer",
    ]
    # The requested council is part of the review identity, so this is not a repeat.
    assert explicit.load()["review_marker"] != auto.load()["review_marker"]
    assert len(posted) == 2


def test_council_specialists_are_resumable_one_stage_at_a_time(
    git_repo: GitRepo, review_env
) -> None:
    base, served, _ = review_env
    _ReviewRunner.reviews["refactorer"] = {"reviewer": "refactorer", "verdict": "nonsense"}
    with pytest.raises(ProviderError, match="refactorer"):
        start_review(
            git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
        )
    run_id = next(
        state["id"] for state in list_runs(git_repo.common_git_dir) if state["kind"] == "review"
    )
    store = RunStore(git_repo.common_git_dir, run_id)
    state = store.load()
    stages = state["stages"]
    assert stages["review-correctness_reviewer"]["status"] == "completed"
    assert stages["review-refactorer"]["status"] == "failed"
    # A specialist that began after the failure must not leave the run marked running.
    assert state["status"] == "failed"

    _default_council_responses()
    result = execute_review(store, git_repo, HarnessConfig())
    assert result["findings"] == 2
    # Only the failed specialist and the stages after it were re-run.
    assert [stage for stage, _ in _ReviewRunner.prompts] == [
        "review-refactorer",
        "review-consolidation",
    ]


def test_extended_head_gets_follow_up_scope_and_respects_no_publish(
    git_repo: GitRepo, review_env
) -> None:
    base, served, posted = review_env
    first = start_review(
        git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
    )
    served["head"] = _commit_change(git_repo, "original\nnew line\nmore\n", "extend")
    _ReviewRunner.prompts.clear()

    second = start_review(
        git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=False
    )
    state = second.load()
    follow_up = state["follow_up_review"]
    assert follow_up["previous_run_id"] == first.run_id
    assert follow_up["previous_head"] == first.load()["pr"]["headRefOid"]
    assert "more" in Path(follow_up["update_diff"]).read_text(encoding="utf-8")
    previous = json.loads(Path(follow_up["previous_review"]).read_text())
    assert previous["summary"] == "Two root causes retained."
    assert all("FOLLOW-UP REVIEW SCOPE" in prompt for _, prompt in _ReviewRunner.prompts)
    assert state["result"]["published"] is False
    assert state["result"]["url"] is None
    assert len(posted) == 1


def test_review_pipeline_error_paths(git_repo: GitRepo, review_env) -> None:
    base, served, _ = review_env
    _ReviewRunner.consolidation["accepted_groups"] = [
        {"source_ids": ["correctness_reviewer:1"], "rationale": "Real defect."}
    ]
    with pytest.raises(ProviderError, match=r"omitted findings: \['refactorer:1'\]"):
        start_review(
            git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
        )

    orphan = RunStore.create(
        git_repo.common_git_dir,
        run_id="20260906-000000-review-orphan",
        kind="review",
        source_repo=git_repo.root,
        source_head=served["head"],
        primary_family="claude",
        prompt="Look",
        options={"pr_number": 16, "publish": True},
    )
    orphan.update(pr=_metadata(base, served["head"]))
    with pytest.raises(HarnessError, match="worktree is missing"):
        execute_review(orphan, git_repo, HarnessConfig())

    closed = _metadata(base, served["head"])
    closed["state"] = "MERGED"
    with pytest.raises(HarnessError, match="is not open"), pytest.MonkeyPatch.context() as patch:
        patch.setattr("ai_harness.pr.pull_request", lambda repo_path, number: closed)
        start_review(
            git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
        )
    with pytest.raises(HarnessError, match="over the configured review limit"):
        start_review(
            git_repo,
            config=HarnessConfig(max_pr_files=0),
            number=16,
            prompt="Look",
            references=[],
            publish=True,
        )


def test_local_review_publishes_through_publish_local_review(
    git_repo: GitRepo, review_env
) -> None:
    base, served, posted = review_env
    store = create_local_review(
        git_repo,
        config=HarnessConfig(),
        base=base,
        head=served["head"],
        branch="feature",
        title="Local change",
        prompt="Review the implementation",
        references=[],
    )
    state = store.load()
    assert state["repository"] == "local"
    assert state["pr"]["headRefName"] == "feature"
    assert state["follow_up_review"] is None

    result = execute_review(store, git_repo, HarnessConfig())
    assert result["published"] is False
    assert result["findings"] == 2
    assert posted == []
    # The local review is not described to the council as pull request zero.
    assert all("#0" not in prompt for _, prompt in _ReviewRunner.prompts)

    publication = publish_local_review(
        store, repo=git_repo, name_with_owner="owner/repo", number=16
    )
    assert publication["url"] == "https://example/review/1"
    assert posted[0]["commit_id"] == served["head"]

    # A second local review of an extended head reuses the completed one as follow-up scope.
    served["head"] = _commit_change(git_repo, "original\nnew line\nmore\n", "extend")
    follow_up_store = create_local_review(
        git_repo,
        config=HarnessConfig(),
        base=base,
        head=served["head"],
        branch="feature",
        title="Local change",
        prompt="Review the implementation",
        references=[],
    )
    follow_up = follow_up_store.load()["follow_up_review"]
    assert follow_up["previous_run_id"] == store.run_id
    with pytest.raises(HarnessError, match="no validated findings"):
        publish_local_review(
            follow_up_store, repo=git_repo, name_with_owner="owner/repo", number=16
        )


def test_clean_council_skips_consolidation_and_still_reports_residual_risk(
    git_repo: GitRepo, review_env
) -> None:
    base, served, posted = review_env
    _ReviewRunner.routing = {
        "specialist_requests": [],
        "focus_reviewers": [],
        "summary": "The floor is enough.",
    }
    _ReviewRunner.reviews = {
        "refactorer": _specialist_review(
            "refactorer",
            [],
            verdict="abstain",
            residual_risks=["The rename is untested."],
        )
    }
    store = start_review(
        git_repo, config=HarnessConfig(), number=16, prompt="Look", references=[], publish=True
    )

    stages = [stage for stage, _ in _ReviewRunner.prompts]
    assert "review-consolidation" not in stages
    assert store.load()["stages"]["review-consolidation"]["family"] == "controller"
    assert store.load()["result"]["findings"] == 0
    review = (store.root / "review.md").read_text(encoding="utf-8")
    assert review.startswith("No actionable findings.")
    assert "refactorer: The rename is untested." in review
    assert posted[0]["comments"] == []


def test_a_fully_completed_council_replays_its_artifacts_without_a_model(
    git_repo: GitRepo, review_env
) -> None:
    base, served, posted = review_env
    store = create_local_review(
        git_repo,
        config=HarnessConfig(),
        base=base,
        head=served["head"],
        branch="feature",
        title="Local change",
        prompt="Review the implementation",
        references=[],
    )
    store.begin_stage("review-routing", "claude")
    store.complete_stage(
        "review-routing",
        {"specialist_requests": [], "focus_reviewers": [], "summary": "Floor only."},
    )
    for member, finding in (
        ("correctness_reviewer", [_specialist_finding(excerpt="new line")]),
        ("refactorer", []),
    ):
        store.begin_stage(f"review-{member}", "claude")
        store.complete_stage(f"review-{member}", _specialist_review(member, finding))
    store.begin_stage("review-consolidation", "claude")
    store.complete_stage(
        "review-consolidation",
        {
            "accepted_groups": [
                {"source_ids": ["correctness_reviewer:1"], "rationale": "Real defect."}
            ],
            "dismissed_groups": [],
            "summary": "One root cause.",
        },
    )
    store.begin_stage("review-final", "controller")
    store.complete_stage(
        "review-final",
        {"summary": "One root cause.", "findings": [_finding(line=2, excerpt="new line")]},
    )
    _ReviewRunner.prompts.clear()

    result = execute_review(store, git_repo, HarnessConfig())

    assert _ReviewRunner.prompts == []
    assert result["findings"] == 1
    assert result["reviewers"] == ["correctness_reviewer", "refactorer"]
