from __future__ import annotations

from typing import Any


class HarnessError(RuntimeError):
    """An expected, user-facing harness failure."""


class AwaitingInput(HarnessError):
    """A task is paused until the user answers planning questions."""

    def __init__(self, run_id: str, questions: list[dict[str, Any]]):
        super().__init__(f"Run {run_id} is awaiting human input")
        self.run_id = run_id
        self.questions = questions


class CommandError(HarnessError):
    def __init__(self, message: str, *, argv: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.argv = argv or []
        self.stderr = stderr


class ProviderError(HarnessError):
    """A model provider failed or returned invalid output."""


class StateError(HarnessError):
    """Persisted run state is missing, corrupt, or inconsistent."""


class StalePullRequest(HarnessError):
    """The pull request head changed during review."""
