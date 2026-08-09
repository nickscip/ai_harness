from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import HarnessError
from .profiles import load_profile_catalog

Family = Literal["claude", "codex"]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class HarnessConfig:
    primary_family: Family = "claude"
    profile_name: str = "built-in"
    profile_description: str = "Built-in library defaults."
    profiles_path: str = ""
    claude_model: str = "sonnet"
    claude_effort: str = "medium"
    claude_fallback_model: str = ""
    codex_model: str = "gpt-5.6-terra"
    codex_reasoning: str = "medium"
    stage_timeout: int = 900
    claude_max_budget_usd: float = 8.0
    max_pr_files: int = 300
    max_pr_lines: int = 100_000
    slack_enabled: bool = False
    slack_user: str = ""
    slack_wait_seconds: int = 1_800
    slack_budget_usd: float = 5.0
    slack_max_clarifications: int = 3
    slack_poll_seconds: int = 20

    @classmethod
    def from_env(
        cls,
        *,
        family: Family | None = None,
        timeout: int | None = None,
        claude_model: str | None = None,
        codex_model: str | None = None,
        profile: str | None = None,
        slack: bool | None = None,
        slack_wait: int | None = None,
    ) -> HarnessConfig:
        catalog = load_profile_catalog()
        selected = catalog.select(profile or os.getenv("AI_HARNESS_PROFILE"))
        profile_claude_model = selected.model if selected.provider == "claude" else "sonnet"
        profile_codex_model = (
            selected.model if selected.provider == "codex" else "gpt-5.6-terra"
        )
        profile_claude_effort = selected.effort if selected.provider == "claude" else "medium"
        profile_codex_effort = selected.effort if selected.provider == "codex" else "medium"
        resolved_family = family or os.getenv("AI_HARNESS_FAMILY", selected.provider)
        if resolved_family not in {"claude", "codex"}:
            raise ValueError(f"Unsupported family: {resolved_family}")
        claude_effort = os.getenv("AI_HARNESS_CLAUDE_EFFORT", profile_claude_effort)
        codex_effort = os.getenv("AI_HARNESS_CODEX_REASONING", profile_codex_effort)
        allowed_efforts = {"low", "medium", "high", "xhigh", "max"}
        if claude_effort not in allowed_efforts:
            raise HarnessError(f"Unsupported Claude effort: {claude_effort}")
        if codex_effort not in allowed_efforts - {"max"}:
            raise HarnessError(f"Unsupported Codex reasoning effort: {codex_effort}")
        resolved_timeout = (
            timeout
            if timeout is not None
            else int(os.getenv("AI_HARNESS_TIMEOUT", str(selected.timeout_seconds)))
        )
        return cls(
            primary_family=resolved_family,
            profile_name=selected.name,
            profile_description=selected.description,
            profiles_path=str(catalog.path),
            claude_model=claude_model
            or os.getenv("AI_HARNESS_CLAUDE_MODEL", profile_claude_model),
            claude_effort=claude_effort,
            claude_fallback_model=os.getenv(
                "AI_HARNESS_CLAUDE_FALLBACK_MODEL",
                selected.fallback_model if selected.provider == "claude" else "",
            ),
            codex_model=codex_model
            or os.getenv("AI_HARNESS_CODEX_MODEL", profile_codex_model),
            codex_reasoning=codex_effort,
            stage_timeout=resolved_timeout,
            claude_max_budget_usd=float(
                os.getenv("AI_HARNESS_CLAUDE_MAX_BUDGET_USD", "8")
            ),
            max_pr_files=int(os.getenv("AI_HARNESS_MAX_PR_FILES", "300")),
            max_pr_lines=int(os.getenv("AI_HARNESS_MAX_PR_LINES", "100000")),
            slack_enabled=(
                slack if slack is not None else _env_flag("AI_HARNESS_SLACK", False)
            ),
            slack_user=os.getenv("AI_HARNESS_SLACK_USER", ""),
            slack_wait_seconds=(
                slack_wait
                if slack_wait is not None
                else int(os.getenv("AI_HARNESS_SLACK_WAIT", "1800"))
            ),
            slack_budget_usd=float(os.getenv("AI_HARNESS_SLACK_BUDGET_USD", "5")),
            slack_max_clarifications=int(
                os.getenv("AI_HARNESS_SLACK_MAX_CLARIFICATIONS", "3")
            ),
            slack_poll_seconds=int(os.getenv("AI_HARNESS_SLACK_POLL_SECONDS", "20")),
        )

    def model_for(self, family: Family) -> str:
        return self.claude_model if family == "claude" else self.codex_model


def other_family(family: Family) -> Family:
    return "codex" if family == "claude" else "claude"


def harness_worktree_path(repo_root: Path, run_id: str) -> Path:
    return repo_root.parent / ".ai-harness-worktrees" / repo_root.name / run_id
