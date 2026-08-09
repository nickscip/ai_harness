from __future__ import annotations

import os
import tempfile
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .errors import HarnessError
from .process import CommandResult, run_command


@dataclass(frozen=True)
class StatusEntry:
    status: str
    path: str
    original_path: str | None = None


@dataclass(frozen=True)
class GitRepo:
    root: Path
    common_git_dir: Path

    @classmethod
    def discover(cls, cwd: Path) -> GitRepo:
        root_result = run_command(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=15
        )
        root = Path(root_result.stdout.strip()).resolve()
        common_result = run_command(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=root,
            timeout=15,
        )
        common = Path(common_result.stdout.strip())
        if not common.is_absolute():
            common = (root / common).resolve()
        return cls(root=root, common_git_dir=common.resolve())

    def run(
        self,
        argv: Iterable[str],
        *,
        cwd: Path | None = None,
        timeout: int = 60,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        return run_command(
            ["git", *argv],
            cwd=cwd or self.root,
            timeout=timeout,
            check=check,
            env=env,
        )

    def head(self) -> str:
        return self.run(["rev-parse", "HEAD"]).stdout.strip()

    def current_branch(self) -> str:
        branch = self.run(["branch", "--show-current"]).stdout.strip()
        if not branch:
            raise HarnessError("PR delivery requires the caller checkout to be on a branch")
        return branch

    def status(self, *, cwd: Path | None = None) -> list[StatusEntry]:
        result = self.run(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=cwd
        )
        pieces = result.stdout.split("\0")
        entries: list[StatusEntry] = []
        index = 0
        while index < len(pieces):
            piece = pieces[index]
            index += 1
            if not piece:
                continue
            if len(piece) < 4:
                raise HarnessError(f"Unexpected git status entry: {piece!r}")
            status = piece[:2]
            path = piece[3:]
            original: str | None = None
            if "R" in status or "C" in status:
                if index >= len(pieces):
                    raise HarnessError("Truncated rename entry from git status")
                original = pieces[index]
                index += 1
            entries.append(StatusEntry(status=status, path=path, original_path=original))
        return entries

    def create_branch_worktree(self, path: Path, branch: str, head: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise HarnessError(f"Refusing to replace existing worktree path: {path}")
        self.run(["worktree", "add", "-b", branch, str(path), head], timeout=120)

    def create_detached_worktree(self, path: Path, head: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            raise HarnessError(f"Refusing to replace existing worktree path: {path}")
        self.run(["worktree", "add", "--detach", str(path), head], timeout=120)

    def git_admin_dir(self, worktree: Path) -> Path:
        result = self.run(
            ["rev-parse", "--path-format=absolute", "--git-dir"], cwd=worktree
        )
        return Path(result.stdout.strip()).resolve()

    def remove_worktree(self, worktree: Path) -> None:
        if self.status(cwd=worktree):
            raise HarnessError(f"Refusing to remove dirty worktree: {worktree}")
        self.run(["worktree", "remove", str(worktree)], timeout=120)

    def fetch_pull_request_head(self, number: int, expected_head: str) -> None:
        result = self.run(
            ["fetch", "--no-tags", "origin", f"refs/pull/{number}/head"],
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HarnessError(f"Could not fetch refs/pull/{number}/head: {detail}")
        fetched = self.run(["rev-parse", "FETCH_HEAD"]).stdout.strip()
        if fetched != expected_head:
            raise HarnessError(
                f"Fetched PR head {fetched} does not match GitHub metadata {expected_head}"
            )

    def ensure_commit(self, oid: str, fallback_ref: str | None = None) -> None:
        present = self.run(["cat-file", "-e", f"{oid}^{{commit}}"], check=False)
        if present.returncode == 0:
            return
        fetched = self.run(["fetch", "--no-tags", "origin", oid], timeout=180, check=False)
        if fetched.returncode == 0:
            return
        if fallback_ref:
            self.run(["fetch", "--no-tags", "origin", fallback_ref], timeout=180)
        present = self.run(["cat-file", "-e", f"{oid}^{{commit}}"], check=False)
        if present.returncode != 0:
            raise HarnessError(f"Could not resolve exact commit {oid}")

    def three_dot_diff(self, base: str, head: str, *, cwd: Path) -> str:
        return self.run(
            ["diff", "--binary", "--find-renames", f"{base}...{head}", "--"],
            cwd=cwd,
            timeout=180,
        ).stdout

    def update_diff(self, previous_head: str, head: str, *, cwd: Path) -> str:
        return self.run(
            ["diff", "--binary", "--find-renames", f"{previous_head}..{head}", "--"],
            cwd=cwd,
            timeout=180,
        ).stdout

    def is_ancestor(self, previous_head: str, head: str) -> bool:
        result = self.run(
            ["merge-base", "--is-ancestor", previous_head, head],
            check=False,
        )
        return result.returncode == 0

    def commit_all(self, worktree: Path, message: str) -> str:
        self.run(["add", "-A", "--", "."], cwd=worktree)
        check = self.run(["diff", "--cached", "--check"], cwd=worktree, check=False)
        if check.returncode != 0:
            raise HarnessError(f"git diff --cached --check failed:\n{check.stdout}{check.stderr}")
        self.run(["commit", "-m", message], cwd=worktree, timeout=120)
        return self.run(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()

    def push_branch(self, worktree: Path, branch: str) -> None:
        self.run(
            ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{branch}"],
            cwd=worktree,
            timeout=180,
        )

    def implementation_patch(self, worktree: Path, scratch_dir: Path) -> str:
        scratch_dir.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix="index-", dir=scratch_dir)
        os.close(descriptor)
        index = Path(temporary)
        index.unlink()
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            self.run(["read-tree", "HEAD"], cwd=worktree, env=env)
            self.run(["add", "-A", "--", "."], cwd=worktree, env=env)
            result = self.run(
                ["diff", "--cached", "--binary", "--full-index", "HEAD", "--"],
                cwd=worktree,
                env=env,
                timeout=180,
            )
            check = self.run(
                ["diff", "--cached", "--check", "HEAD", "--"],
                cwd=worktree,
                env=env,
                check=False,
            )
            if check.returncode != 0:
                raise HarnessError(f"git diff --check failed:\n{check.stdout}{check.stderr}")
            return result.stdout
        finally:
            with suppress(FileNotFoundError):
                index.unlink()

    def show_file(self, commit: str, path: str) -> str | None:
        result = self.run(["show", f"{commit}:{path}"], check=False)
        if result.returncode != 0:
            return None
        return result.stdout


def resolve_inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise HarnessError(f"Command cwd escapes the worktree: {relative}") from exc
    return candidate
