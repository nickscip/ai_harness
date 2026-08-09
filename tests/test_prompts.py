from __future__ import annotations

from pathlib import Path

from ai_harness.prompts import pr_draft_prompt, task_plan_prompt


def test_planner_is_told_to_record_commands_without_executing_them() -> None:
    prompt = task_plan_prompt("Fix the bug", "No context")
    assert "source inspection only" in prompt
    assert "Do not execute repository scripts, tests, package managers, Docker" in prompt
    assert "Record those argv commands in" in prompt
    assert "Do not keep retrying a denied tool" in prompt


def test_follow_up_pr_review_prioritizes_the_update_delta() -> None:
    follow_up = """
FOLLOW-UP REVIEW SCOPE
Previous reviewed head: abc
Intervening update delta: /tmp/update.diff
Do not restart a line-by-line review of unchanged code.
"""
    prompt = pr_draft_prompt(
        19,
        "Fix bug",
        "base",
        "head",
        Path("/tmp/full.diff"),
        "Be adversarial",
        "No context",
        follow_up,
    )
    assert "Intervening update delta: /tmp/update.diff" in prompt
    assert "Do not restart a line-by-line review" in prompt
