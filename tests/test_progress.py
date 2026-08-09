from __future__ import annotations

from io import StringIO

from ai_harness.progress import TerminalProgress, stage_completed_message


def test_terminal_progress_is_prefixed_and_immediately_line_oriented() -> None:
    stream = StringIO()
    progress = TerminalProgress(stream)

    progress("Starting agent: repository-grounded plan (claude)")

    assert stream.getvalue() == (
        "[ai-harness] Starting agent: repository-grounded plan (claude)\n"
    )


def test_stage_completion_includes_elapsed_time_and_result_summary() -> None:
    message = stage_completed_message(
        "plan-review",
        "codex",
        {"verdict": "revise", "findings": [{"id": "R001"}]},
        12.34,
    )

    assert message == (
        "Completed agent: adversarial plan review (codex) in 12.3s — "
        "revise, 1 finding(s)"
    )
