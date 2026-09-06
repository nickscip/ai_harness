from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

ProgressCallback = Callable[[str], None]


class TerminalProgress:
    """Write immediate, provider-neutral workflow progress to stderr."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr
        self.started = time.monotonic()

    def __call__(self, message: str) -> None:
        print(f"[ai-harness] {message}", file=self.stream, flush=True)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started


_STAGE_LABELS = {
    "worktree-bootstrap": "worktree setup",
    "plan": "repository-grounded plan",
    "plan-review": "adversarial plan review",
    "revised-plan": "review-driven plan revision",
    "command-repair": "verification-command repair",
    "preparation": "implementation preparation",
    "implementation": "implementation",
    "implementation-repair": "no-op implementation recovery",
    "implementation-capture": "implementation diff capture",
    "verification": "controller verification",
    "delivery-commit": "implementation commit",
    "implementation-review": "cross-family implementation review",
    "delivery-push": "branch push",
    "delivery-pr": "draft pull request creation",
    "delivery-review-publication": "implementation review publication",
    "review-draft": "draft code review",
    "review-critique": "adversarial review critique",
    "review-final": "final code review",
    "evidence-validation": "review evidence validation",
    "publication": "GitHub review publication",
}


def stage_label(name: str) -> str:
    if name.startswith("plan-r") and name.removeprefix("plan-r").isdigit():
        return "repository-grounded plan after human input"
    if name.startswith("clarify-r"):
        return "answer to a human follow-up question"
    return _STAGE_LABELS.get(name, name.replace("-", " "))


def stage_started_message(name: str, family: str) -> str:
    label = stage_label(name)
    if family == "controller":
        return f"Starting controller stage: {label}"
    return f"Starting agent: {label} ({family})"


def _stage_summary(name: str, value: dict[str, Any]) -> str:
    if name.startswith("plan") and isinstance(value.get("steps"), list):
        questions = value.get("questions")
        question_count = len(questions) if isinstance(questions, list) else 0
        return f"{len(value['steps'])} step(s), {question_count} question(s)"
    if name == "plan-review":
        findings = value.get("findings")
        count = len(findings) if isinstance(findings, list) else 0
        verdict = value.get("verdict", "completed")
        return f"{verdict}, {count} finding(s)"
    if name == "implementation":
        changed = value.get("changed_files")
        if isinstance(changed, list):
            return f"{len(changed)} reported changed file(s)"
    if name in {"review-draft", "review-final"}:
        findings = value.get("findings")
        if isinstance(findings, list):
            return f"{len(findings)} finding(s)"
    if name == "review-critique":
        findings = value.get("findings")
        if isinstance(findings, list):
            return f"{len(findings)} proposed finding(s)"
    if name == "implementation-capture":
        changed = value.get("changed_paths")
        if isinstance(changed, list):
            return f"{len(changed)} changed path(s) captured"
    if name == "verification":
        commands = value.get("commands")
        if isinstance(commands, list):
            return f"{len(commands)} command(s) passed"
    if name == "worktree-bootstrap":
        return "worktree-setup complete" if value.get("ran") else "worktree setup not enabled"
    if name == "delivery-commit" and isinstance(value.get("sha"), str):
        return f"commit {value['sha'][:12]}"
    if name == "delivery-push" and isinstance(value.get("branch"), str):
        return str(value["branch"])
    if name == "delivery-pr":
        return str(value.get("url") or f"PR #{value.get('number', '?')}")
    if name in {"publication", "delivery-review-publication"}:
        if value.get("idempotent"):
            return "already published"
        if value.get("url"):
            return str(value["url"])
        if value.get("published") is False:
            return "not published"
    if name == "evidence-validation":
        inline = value.get("inline_findings")
        summary = value.get("summary_only_findings")
        if isinstance(inline, list) and isinstance(summary, list):
            return f"{len(inline)} inline, {len(summary)} summary-only finding(s)"
    return ""


def stage_completed_message(
    name: str,
    family: str,
    value: dict[str, Any],
    elapsed_seconds: float | None,
) -> str:
    label = stage_label(name)
    actor = "Controller stage" if family == "controller" else "Agent"
    family_suffix = "" if family == "controller" else f" ({family})"
    elapsed = f" in {elapsed_seconds:.1f}s" if elapsed_seconds is not None else ""
    summary = _stage_summary(name, value)
    summary_suffix = f" — {summary}" if summary else ""
    return f"Completed {actor.lower()}: {label}{family_suffix}{elapsed}{summary_suffix}"
