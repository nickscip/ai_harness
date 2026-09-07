from __future__ import annotations

import pytest

from ai_harness.cli import _dispatch
from ai_harness.errors import HarnessError
from ai_harness.git import GitRepo


def test_task_cli_delivers_by_default_and_local_only_opts_out(
    git_repo: GitRepo, monkeypatch, capsys
) -> None:
    delivered: list[bool] = []

    class FakeStore:
        run_id = "task-test"

        def load(self):
            return {
                "warnings": [],
                "result": {
                    "worktree": "/tmp/worktree",
                    "branch": "ai-harness/task-test",
                    "patch": "/tmp/implementation.patch",
                    "delivery": None,
                },
            }

    def fake_start_task(repo, *, config, prompt, references, deliver, progress):
        delivered.append(deliver)
        return FakeStore()

    monkeypatch.chdir(git_repo.root)
    monkeypatch.setattr("ai_harness.cli.start_task", fake_start_task)

    assert _dispatch(["Implement the next roadmap item"]) == 0
    assert _dispatch(["--local-only", "Implement the next roadmap item"]) == 0
    assert delivered == [True, False]
    captured = capsys.readouterr()
    assert "Task run task-test complete." in captured.out
    assert "[ai-harness] Model profile: codex-balanced" in captured.err
    assert "primary=codex (model=gpt-5.6-terra, effort=medium)" in captured.err
    assert "adversary=claude (model=sonnet, effort=medium)" in captured.err


def test_config_command_shows_resolved_models(monkeypatch, capsys) -> None:
    monkeypatch.setenv("AI_HARNESS_CODEX_MODEL", "codex-test-model")

    assert (
        _dispatch(
            ["config", "--profile", "claude-opus", "--claude-model", "claude-test-model"]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "profile: claude-opus" in output
    assert "primary family: claude" in output
    assert "Claude model: claude-test-model" in output
    assert "Claude effort: high" in output
    assert "Claude fallback model: sonnet" in output
    assert "Codex model: codex-test-model" in output
    assert "Codex reasoning: medium" in output
    assert "Codex fast tier: off" in output
    assert "Apply PR review feedback: off" in output


@pytest.mark.parametrize("argv", [["--family", "codex", "task"], ["config", "--family", "codex"]])
def test_the_family_flag_is_rejected_by_every_parser(argv: list[str], capsys) -> None:
    """The profile's provider is the only way to choose the primary family."""
    with pytest.raises(SystemExit) as exit_info:
        _dispatch(argv)

    assert exit_info.value.code == 2
    assert "unrecognized arguments: --family" in capsys.readouterr().err


def test_the_retired_family_variable_fails_loudly_instead_of_being_ignored(monkeypatch) -> None:
    """Silently ignoring it would change provider, credentials, and cost with no signal."""
    monkeypatch.setenv("AI_HARNESS_FAMILY", "codex")

    with pytest.raises(HarnessError, match="AI_HARNESS_FAMILY is no longer supported"):
        _dispatch(["config", "--profile", "claude-fable"])


def test_claude_fable_profile_resolves_its_codex_critic_overrides(capsys) -> None:
    assert _dispatch(["config", "--profile", "claude-fable"]) == 0

    output = capsys.readouterr().out
    assert "primary family: claude" in output
    assert "Claude model: fable" in output
    assert "Claude effort: high" in output
    assert "Codex model: gpt-5.6-sol" in output
    assert "Codex reasoning: xhigh" in output
    assert "Codex fast tier: on" in output
    assert "Apply PR review feedback: on" in output
