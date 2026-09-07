from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.errors import HarnessError
from ai_harness.git import GitRepo, resolve_inside
from ai_harness.process import run_command


def test_binary_patch_includes_tracked_untracked_binary_and_empty_files(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    (git_repo.root / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (git_repo.root / "new.txt").write_text("new\n", encoding="utf-8")
    (git_repo.root / "empty.txt").write_bytes(b"")
    (git_repo.root / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    patch = git_repo.implementation_patch(git_repo.root, tmp_path)
    assert "tracked.txt" in patch
    assert "new.txt" in patch
    assert "empty.txt" in patch
    assert "blob.bin" in patch
    assert "new file mode" in patch
    assert {entry.path for entry in git_repo.status()} >= {"new.txt", "empty.txt", "blob.bin"}


def test_linked_worktree_git_admin_is_external_and_cleanup_refuses_dirty(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    git_repo.create_branch_worktree(worktree, "test-worktree", git_repo.head())
    admin = git_repo.git_admin_dir(worktree)
    assert admin != worktree / ".git"
    assert git_repo.common_git_dir in admin.parents
    (worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(HarnessError, match="dirty"):
        git_repo.remove_worktree(worktree)


def _commit_in(other: Path, name: str, content: str) -> str:
    (other / name).write_text(content, encoding="utf-8")
    run_command(["git", "add", name], cwd=other)
    run_command(["git", "commit", "-q", "-m", f"add {name}"], cwd=other)
    return run_command(["git", "rev-parse", "HEAD"], cwd=other).stdout.strip()


def test_fetch_pull_request_head_and_ensure_commit_use_origin(
    git_repo: GitRepo, origin: Path
) -> None:
    other = origin.parent / "other"
    pr_head = _commit_in(other, "pr.txt", "pr\n")
    run_command(["git", "push", "-q", "origin", "HEAD:refs/pull/7/head"], cwd=other)
    feature_head = _commit_in(other, "feature.txt", "feature\n")
    run_command(["git", "push", "-q", "origin", "HEAD:refs/heads/feature"], cwd=other)

    git_repo.fetch_pull_request_head(7, pr_head)
    with pytest.raises(HarnessError, match="does not match"):
        git_repo.fetch_pull_request_head(7, "0" * 40)
    with pytest.raises(HarnessError, match="Could not fetch refs/pull/99/head"):
        git_repo.fetch_pull_request_head(99, pr_head)

    git_repo.ensure_commit(pr_head)
    git_repo.ensure_commit(feature_head)
    assert git_repo.run(["cat-file", "-t", feature_head]).stdout.strip() == "commit"
    with pytest.raises(HarnessError, match="Could not resolve exact commit"):
        git_repo.ensure_commit("1" * 40, "feature")


def test_push_branch_publishes_worktree_head(
    git_repo: GitRepo, origin: Path, tmp_path: Path
) -> None:
    worktree = tmp_path / "push-worktree"
    git_repo.create_branch_worktree(worktree, "pushed", git_repo.head())
    (worktree / "pushed.txt").write_text("pushed\n", encoding="utf-8")
    head = git_repo.commit_all(worktree, "pushed commit")
    git_repo.push_branch(worktree, "pushed")
    remote = run_command(["git", "rev-parse", "refs/heads/pushed"], cwd=origin).stdout.strip()
    assert remote == head


def test_current_branch_requires_attached_head(git_repo: GitRepo) -> None:
    assert git_repo.current_branch() == "main"
    git_repo.run(["checkout", "-q", "--detach"])
    with pytest.raises(HarnessError, match="on a branch"):
        git_repo.current_branch()


def test_status_parses_renames(git_repo: GitRepo) -> None:
    git_repo.run(["mv", "tracked.txt", "renamed.txt"])
    entries = git_repo.status()
    assert len(entries) == 1
    assert entries[0].path == "renamed.txt"
    assert entries[0].original_path == "tracked.txt"
    assert "R" in entries[0].status


def test_worktree_creation_refuses_existing_path(git_repo: GitRepo, tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(HarnessError, match="existing worktree path"):
        git_repo.create_branch_worktree(existing, "branch", git_repo.head())
    with pytest.raises(HarnessError, match="existing worktree path"):
        git_repo.create_detached_worktree(existing, git_repo.head())


def test_whitespace_errors_fail_commit_and_patch(git_repo: GitRepo, tmp_path: Path) -> None:
    worktree = tmp_path / "ws"
    git_repo.create_detached_worktree(worktree, git_repo.head())
    (worktree / "bad.txt").write_text("trailing \n", encoding="utf-8")
    with pytest.raises(HarnessError, match="--check failed"):
        git_repo.implementation_patch(worktree, tmp_path / "scratch")
    with pytest.raises(HarnessError, match="--cached --check failed"):
        git_repo.commit_all(worktree, "bad")


def test_show_file_missing_returns_none_and_resolve_inside_rejects_escape(
    git_repo: GitRepo,
) -> None:
    assert git_repo.show_file(git_repo.head(), "tracked.txt") == "original\n"
    assert git_repo.show_file(git_repo.head(), "missing.txt") is None
    assert resolve_inside(git_repo.root, "sub/dir") == git_repo.root / "sub" / "dir"
    with pytest.raises(HarnessError, match="escapes the worktree"):
        resolve_inside(git_repo.root, "../outside")
