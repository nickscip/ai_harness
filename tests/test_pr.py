from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_harness.errors import StalePullRequest
from ai_harness.git import GitRepo
from ai_harness.pr import (
    find_previous_review,
    parse_diff_index,
    publish_comment_review,
    render_review_body,
    require_unchanged_pr_head,
    validate_pr_findings,
)
from ai_harness.process import CommandResult, run_command
from ai_harness.state import RunStore


def _finding(*, line: int, excerpt: str) -> dict[str, object]:
    return {
        "id": "F001",
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
    state["pr"] = {"number": 19, "headRefOid": previous_head}
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
