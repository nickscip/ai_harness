from __future__ import annotations

from io import StringIO

from ai_harness.progress import TerminalProgress, stage_completed_message, stage_label


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


def test_specialist_stage_labels_do_not_swallow_the_lead_or_final_stages() -> None:
    assert stage_label("review-security_reviewer") == "security review"
    assert stage_label("review-performance_expert") == "performance review"
    assert stage_label("review-refactorer") == "refactorer review"
    assert stage_label("review-routing") == "review council routing"
    assert stage_label("review-consolidation") == "review council consolidation"
    assert stage_label("review-final") == "consolidated council review"


def test_council_stage_summaries_report_verdicts_and_lead_filtering() -> None:
    assert stage_completed_message(
        "review-contract_reviewer", "codex", {"verdict": "abstain", "findings": []}, 1.0
    ) == "Completed agent: contract review (codex) in 1.0s — abstain, 0 finding(s)"
    assert stage_completed_message(
        "review-routing", "claude", {"specialist_requests": [{}, {}]}, None
    ) == "Completed agent: review council routing (claude) — 2 specialist(s) requested"
    assert stage_completed_message(
        "review-consolidation",
        "claude",
        {"accepted_groups": [{}], "dismissed_groups": [{}, {}]},
        None,
    ) == "Completed agent: review council consolidation (claude) — 1 retained, 2 dismissed"
