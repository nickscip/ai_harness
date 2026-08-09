from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.git import GitRepo
from ai_harness.process import run_command


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

