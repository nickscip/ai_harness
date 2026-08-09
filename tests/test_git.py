from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.errors import HarnessError
from ai_harness.git import GitRepo


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

