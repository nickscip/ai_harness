from __future__ import annotations

import pytest

from ai_harness.cli import _dispatch
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
def test_the_primary_family_cannot_be_overridden_independently_of_the_profile(
    argv: list[str], monkeypatch, capsys
) -> None:
    """The profile's provider is the only way to choose the primary family."""
    monkeypatch.setenv("AI_HARNESS_FAMILY", "codex")

    with pytest.raises(SystemExit) as exit_info:
        _dispatch(argv)
    assert exit_info.value.code == 2
    assert "unrecognized arguments: --family" in capsys.readouterr().err

    # The retired environment variable is inert rather than a silent back door.
    assert _dispatch(["config", "--profile", "claude-fable"]) == 0
    assert "primary family: claude" in capsys.readouterr().out


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
