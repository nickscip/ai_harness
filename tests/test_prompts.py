from __future__ import annotations

from pathlib import Path

from ai_harness.prompts import (
    lead_routing_prompt,
    specialist_prompt,
    task_plan_prompt,
)


def test_planner_is_told_to_record_commands_without_executing_them() -> None:
    prompt = task_plan_prompt("Fix the bug", "No context")
    assert "source inspection only" in prompt
    assert "Do not execute repository scripts, tests, package managers, Docker" in prompt
    assert "Record those argv commands in" in prompt
    assert "Do not keep retrying a denied tool" in prompt


def test_follow_up_specialist_review_prioritizes_the_update_delta() -> None:
    follow_up = """
FOLLOW-UP REVIEW SCOPE
Previous reviewed head: abc
Intervening update delta: /tmp/update.diff
Do not restart a line-by-line review of unchanged code.
"""
    prompt = specialist_prompt(
        contract="CONTRACT",
        charter="CHARTER",
        number=19,
        title="Fix bug",
        branch="fix",
        base="base",
        head="head",
        diff_path=Path("/tmp/full.diff"),
        user_prompt="Be adversarial",
        context="No context",
        follow_up=follow_up,
    )

    assert "Intervening update delta: /tmp/update.diff" in prompt
    assert "Do not restart a line-by-line review" in prompt
    assert "CONTRACT" in prompt
    assert "CHARTER" in prompt
    assert 'Reviewing pull request #19: "Fix bug"' in prompt
    assert "assigned the caller's review direction specifically to you" not in prompt


def test_focused_specialist_is_told_the_caller_direction_is_assigned_to_it() -> None:
    prompt = specialist_prompt(
        contract="CONTRACT",
        charter="CHARTER",
        number=19,
        title="Fix bug",
        branch="fix",
        base="base",
        head="head",
        diff_path=Path("/tmp/full.diff"),
        user_prompt="Watch the retry path",
        context="No context",
        focused=True,
    )

    assert "assigned the caller's review direction specifically to you" in prompt


def test_local_implementation_review_is_not_described_as_pull_request_zero() -> None:
    prompt = specialist_prompt(
        contract="CONTRACT",
        charter="CHARTER",
        number=0,
        title="Add retries",
        branch="ai-harness/add-retries",
        base="base",
        head="head",
        diff_path=Path("/tmp/full.diff"),
        user_prompt="Review it",
        context="No context",
    )

    assert "#0" not in prompt
    assert 'local branch "ai-harness/add-retries"' in prompt


def test_routing_prompt_names_the_floor_and_the_changed_paths() -> None:
    prompt = lead_routing_prompt(
        charter="LEAD",
        number=19,
        title="Fix bug",
        branch="fix",
        base="base",
        head="head",
        diff_path=Path("/tmp/full.diff"),
        changed_paths=["src/state.py", "src/pr.py"],
        deterministic=["correctness_reviewer", "refactorer"],
        user_prompt="Be adversarial",
        context="No context",
    )

    assert "MODE: routing." in prompt
    assert "correctness_reviewer" in prompt
    assert "src/state.py" in prompt
