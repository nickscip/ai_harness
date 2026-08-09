from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .process import CommandResult, controller_env, run_command


def _github_env() -> dict[str, str]:
    environment = controller_env()
    # A shell-wide GH_HOST can point at a different GitHub Enterprise instance.
    # Let gh infer the correct host from this repository's origin instead.
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
        env=_github_env(),
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
        env=_github_env(),
        check=False,
    )
