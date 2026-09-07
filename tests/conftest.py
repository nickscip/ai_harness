from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.git import GitRepo
from ai_harness.process import run_command
from ai_harness.providers import find_claude, find_codex


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    root = tmp_path / "repo"
    root.mkdir()
    run_command(["git", "init", "-b", "main"], cwd=root)
    run_command(["git", "config", "user.name", "Harness Tests"], cwd=root)
    run_command(["git", "config", "user.email", "tests@example.invalid"], cwd=root)
    (root / "tracked.txt").write_text("original\n", encoding="utf-8")
    run_command(["git", "add", "tracked.txt"], cwd=root)
    run_command(["git", "commit", "-m", "initial"], cwd=root)
    return GitRepo.discover(root)


@pytest.fixture
def origin(git_repo: GitRepo, tmp_path: Path) -> Path:
    """A bare `origin` for git_repo plus a second clone at `<origin>/../other` for making
    commits the fixture repo does not have yet."""
    bare = tmp_path / "origin.git"
    run_command(["git", "clone", "--bare", "--quiet", str(git_repo.root), str(bare)], cwd=tmp_path)
    run_command(["git", "remote", "add", "origin", str(bare)], cwd=git_repo.root)
    other = tmp_path / "other"
    run_command(["git", "clone", "--quiet", str(bare), str(other)], cwd=tmp_path)
    run_command(["git", "config", "user.name", "Other"], cwd=other)
    run_command(["git", "config", "user.email", "other@example.invalid"], cwd=other)
    return bare


@pytest.fixture
def clear_provider_caches():
    find_claude.cache_clear()
    find_codex.cache_clear()
    yield
    find_claude.cache_clear()
    find_codex.cache_clear()
