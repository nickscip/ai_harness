from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.errors import HarnessError, StalePullRequest
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
from ai_harness.state import RunStore


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
    # A different head ref, a non-review run, and an unrelated head are all skipped.
    assert find_previous_review(git_repo, number=19, head=current_head, head_ref="other") is None
    _completed_review(
        git_repo, "20260809-000000-task-zzzzzz", number=19, head="f" * 40, kind="task"
    )
    _completed_review(git_repo, "20260809-000000-review-yyyyyy", number=19, head="f" * 40)
    assert find_previous_review(git_repo, number=19, head=current_head) is not None

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


# --- full review pipeline through fake providers ----------------------------------------------


def _commit_change(git_repo: GitRepo, content: str, message: str) -> str:
    (git_repo.root / "tracked.txt").write_text(content, encoding="utf-8")
    run_command(["git", "add", "tracked.txt"], cwd=git_repo.root)
    run_command(["git", "commit", "-q", "-m", message], cwd=git_repo.root)
    return git_repo.head()


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


class _ReviewRunner:
    """Fake provider: valid F001, invalid-excerpt F002; never touches the read-only worktree."""

    prompts: list[tuple[str, str]] = []
    critique: dict[str, object] = {}

    def __init__(self, config, store: RunStore):
        self.store = store

    def run(self, request):
        _ReviewRunner.prompts.append((request.stage, request.prompt))
        assert request.writable is False
        self.store.begin_stage(request.stage, request.family)
        if request.stage == "review-critique":
            value = dict(_ReviewRunner.critique)
        else:
            value = {
                "summary": "Two findings.",
                "findings": [
                    _finding(line=2, excerpt="new line"),
                    _finding(line=2, excerpt="does not match", identifier="F002"),
                ],
            }
        self.store.complete_stage(request.stage, value)
        return value


@pytest.fixture
def review_env(git_repo: GitRepo, monkeypatch):
    """Patch every GitHub touchpoint; return (base, head) with metadata served for `head`."""
    _ReviewRunner.prompts = []
    _ReviewRunner.critique = {"summary": "Keep both.", "findings": [], "rejected_finding_ids": []}
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
        "url": "https://example/review/1",
        "findings": 2,
        "summary_only": 1,
    }
    assert [stage for stage, _ in _ReviewRunner.prompts] == [
        "review-draft",
        "review-critique",
        "review-final",
    ]
    assert all("FOLLOW-UP REVIEW SCOPE" not in prompt for _, prompt in _ReviewRunner.prompts)
    review = (store.root / "review.md").read_text(encoding="utf-8")
    assert "### [high] F001: Incorrect branch\n" in review
    assert "F002: Incorrect branch (summary only" in review
    assert state["review_marker"] in review
    assert [comment["path"] for comment in posted[0]["comments"]] == ["tracked.txt"]
    assert posted[0]["commit_id"] == served["head"]
    assert any("Loaded PR #16" in line for line in progress)
    assert not Path(state["diff"]).parent.joinpath("worktree").exists()

    # A completed run has released its worktree, so it cannot be executed again.
    _ReviewRunner.prompts.clear()
    with pytest.raises(HarnessError, match="worktree is missing"):
        execute_review(store, git_repo, HarnessConfig())
    assert _ReviewRunner.prompts == []


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
    assert json.loads(Path(follow_up["previous_review"]).read_text())["summary"] == "Two findings."
    assert all("FOLLOW-UP REVIEW SCOPE" in prompt for _, prompt in _ReviewRunner.prompts)
    assert state["result"]["published"] is False
    assert state["result"]["url"] is None
    assert len(posted) == 1


def test_review_pipeline_error_paths(git_repo: GitRepo, review_env) -> None:
    base, served, _ = review_env
    _ReviewRunner.critique["rejected_finding_ids"] = ["F999"]
    with pytest.raises(HarnessError, match=r"unknown finding IDs: \['F999'\]"):
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
