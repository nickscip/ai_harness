from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_harness.errors import HarnessError
from ai_harness.github import (
    create_draft_pull_request,
    open_pull_request_for_head,
    post_review,
    pull_request,
    repository_name,
    review_marker_exists,
)
from ai_harness.process import CommandResult


def test_gh_infers_host_from_remote_instead_of_shell_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GH_HOST", "git.enterprise.example")

    def fake_run_command(argv, *, cwd, timeout, input_text, env):
        assert "GH_HOST" not in env
        return CommandResult(tuple(argv), 0, json.dumps({"nameWithOwner": "owner/repo"}), "")

    monkeypatch.setattr("ai_harness.github.run_command", fake_run_command)

    assert repository_name(tmp_path) == "owner/repo"


def _fake_gh(monkeypatch, responses: list[object]) -> list[dict[str, object]]:
    """Feed canned gh outputs in order; record every call's argv and kwargs."""
    calls: list[dict[str, object]] = []

    def fake_run_command(argv, *, cwd, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        payload = responses.pop(0)
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return CommandResult(tuple(argv), 0, stdout, "")

    monkeypatch.setattr("ai_harness.github.run_command", fake_run_command)
    return calls


def test_gh_json_and_repository_name_reject_bad_payloads(tmp_path: Path, monkeypatch) -> None:
    _fake_gh(monkeypatch, ["not json", {"nameWithOwner": "no-slash"}, ["list"]])
    with pytest.raises(HarnessError, match="invalid JSON"):
        repository_name(tmp_path)
    with pytest.raises(HarnessError, match="repository name"):
        repository_name(tmp_path)
    with pytest.raises(HarnessError, match="repository name"):
        repository_name(tmp_path)


def test_pull_request_and_open_pr_lookup(tmp_path: Path, monkeypatch) -> None:
    calls = _fake_gh(
        monkeypatch,
        [{"number": 16, "state": "OPEN"}, [], [], [{"number": 1}], [1]],
    )
    assert pull_request(tmp_path, 16)["state"] == "OPEN"
    assert calls[0]["argv"][:3] == ["gh", "pr", "view"]
    with pytest.raises(HarnessError, match="Could not load pull request #16"):
        pull_request(tmp_path, 16)
    assert open_pull_request_for_head(tmp_path, "feature") is None
    assert open_pull_request_for_head(tmp_path, "feature") == {"number": 1}
    assert open_pull_request_for_head(tmp_path, "feature") is None
    assert "feature" in calls[-1]["argv"]


def test_create_draft_pr_posts_draft_payload(tmp_path: Path, monkeypatch) -> None:
    calls = _fake_gh(monkeypatch, [{"number": 7, "url": "https://example/pr/7"}, {"number": "x"}])
    created = create_draft_pull_request(
        tmp_path,
        name_with_owner="owner/repo",
        title="Title",
        head="feature",
        base="main",
        body="Body",
    )
    assert created["number"] == 7
    sent = json.loads(str(calls[0]["input_text"]))
    assert sent == {
        "title": "Title", "head": "feature", "base": "main", "body": "Body", "draft": True
    }
    assert "repos/owner/repo/pulls" in calls[0]["argv"]
    with pytest.raises(HarnessError, match="draft pull request"):
        create_draft_pull_request(
            tmp_path, name_with_owner="owner/repo", title="t", head="h", base="b", body=""
        )


def test_review_marker_exists_scans_paginated_pages(tmp_path: Path, monkeypatch) -> None:
    _fake_gh(
        monkeypatch,
        [
            [[{"body": "first"}], [{"body": "see <!-- m -->"}]],
            [[{"body": "x"}], "junk", [7]],
            {},
        ],
    )
    assert review_marker_exists(tmp_path, "owner/repo", 1, "<!-- m -->") is True
    assert review_marker_exists(tmp_path, "owner/repo", 1, "<!-- m -->") is False
    assert review_marker_exists(tmp_path, "owner/repo", 1, "<!-- m -->") is False


def test_post_review_does_not_raise_on_nonzero(tmp_path: Path, monkeypatch) -> None:
    def fake_run_command(argv, *, cwd, timeout, input_text, env, check):
        assert check is False
        assert json.loads(input_text) == {"event": "COMMENT"}
        return CommandResult(tuple(argv), 1, "", "HTTP 422")

    monkeypatch.setattr("ai_harness.github.run_command", fake_run_command)
    result = post_review(tmp_path, "owner/repo", 3, {"event": "COMMENT"})
    assert result.returncode == 1
