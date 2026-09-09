from __future__ import annotations

import json
import subprocess
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import HarnessError
from .process import CommandResult, controller_env, run_command


@cache
def _origin_host(repo: Path) -> str:
    """The host in this repository's origin URL, or "" when it cannot be read.

    Read directly rather than through run_command: this is a local read-only query,
    not a controller subprocess, and it must not consume a gh call's turn.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    url = result.stdout.strip()
    if "://" in url:
        return urlsplit(url).hostname or ""
    return url.rpartition("@")[2].partition(":")[0]


def _github_env(repo: Path) -> dict[str, str]:
    environment = controller_env()
    # A shell-wide GH_HOST can point at a different GitHub Enterprise instance, and
    # `gh api` with an explicit repos/ path never infers the host from origin the way
    # `gh repo view` does. Pin it to this repository's own origin host.
    host = _origin_host(repo)
    if host:
        environment["GH_HOST"] = host
    else:
        environment.pop("GH_HOST", None)
    return environment


def _gh_json(
    argv: list[str], *, repo: Path, timeout: int = 120, input_text: str | None = None
) -> Any:
    result = run_command(
        ["gh", *argv],
        cwd=repo,
        timeout=timeout,
        input_text=input_text,
        env=_github_env(repo),
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"gh returned invalid JSON for {' '.join(argv)}") from exc


def repository_name(repo: Path) -> str:
    value = _gh_json(["repo", "view", "--json", "nameWithOwner"], repo=repo)
    name = value.get("nameWithOwner") if isinstance(value, dict) else None
    if not isinstance(name, str) or "/" not in name:
        raise HarnessError("Could not resolve the GitHub repository name")
    return name


def pull_request(repo: Path, number: int) -> dict[str, Any]:
    fields = ",".join(
        [
            "number",
            "state",
            "isDraft",
            "headRefOid",
            "baseRefOid",
            "url",
            "title",
            "body",
            "headRefName",
            "baseRefName",
            "additions",
            "deletions",
            "changedFiles",
        ]
    )
    value = _gh_json(["pr", "view", str(number), "--json", fields], repo=repo)
    if not isinstance(value, dict):
        raise HarnessError(f"Could not load pull request #{number}")
    return value


def open_pull_request_for_head(repo: Path, branch: str) -> dict[str, Any] | None:
    value = _gh_json(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            branch,
            "--json",
            "number,url,isDraft,headRefOid,headRefName,baseRefName,title",
        ],
        repo=repo,
    )
    if not isinstance(value, list) or not value:
        return None
    first = value[0]
    return first if isinstance(first, dict) else None


def create_draft_pull_request(
    repo: Path,
    *,
    name_with_owner: str,
    title: str,
    head: str,
    base: str,
    body: str,
) -> dict[str, Any]:
    value = _gh_json(
        [
            "api",
            "--method",
            "POST",
            f"repos/{name_with_owner}/pulls",
            "--input",
            "-",
        ],
        repo=repo,
        timeout=120,
        input_text=json.dumps(
            {"title": title, "head": head, "base": base, "body": body, "draft": True}
        ),
    )
    if not isinstance(value, dict) or not isinstance(value.get("number"), int):
        raise HarnessError("GitHub did not return the created draft pull request")
    return value


def review_marker_exists(repo: Path, name_with_owner: str, number: int, marker: str) -> bool:
    value = _gh_json(
        [
            "api",
            "--paginate",
            "--slurp",
            f"repos/{name_with_owner}/pulls/{number}/reviews?per_page=100",
        ],
        repo=repo,
    )
    pages = value if isinstance(value, list) else []
    for page in pages:
        if not isinstance(page, list):
            continue
        for review in page:
            if isinstance(review, dict) and marker in str(review.get("body", "")):
                return True
    return False


def post_review(
    repo: Path,
    name_with_owner: str,
    number: int,
    payload: dict[str, Any],
) -> CommandResult:
    return run_command(
        [
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{name_with_owner}/pulls/{number}/reviews",
            "--input",
            "-",
        ],
        cwd=repo,
        timeout=120,
        input_text=json.dumps(payload),
        env=_github_env(repo),
        check=False,
    )
