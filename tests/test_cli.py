from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ai_harness.cli import _dispatch, _parse_answers, main
from ai_harness.errors import AwaitingInput, HarnessError
from ai_harness.git import GitRepo
from ai_harness.state import RunStore


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
    assert "parallel council specialists: 3" in output

    monkeypatch.setenv("AI_HARNESS_COUNCIL_WORKERS", "1")
    assert _dispatch(["config"]) == 0
    assert "parallel council specialists: 1" in capsys.readouterr().out

    monkeypatch.setenv("AI_HARNESS_COUNCIL_WORKERS", "7")
    with pytest.raises(HarnessError, match="must be between 1 and 6, not 7"):
        _dispatch(["config"])


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


def _question(identifier: str = "Q001") -> dict[str, object]:
    return {
        "id": identifier,
        "question": "JSON or text?",
        "why_blocking": "Changes the file format.",
        "suggested_default": "",
    }


def _store(git_repo: GitRepo, run_id: str, kind: str, **state_updates: object) -> RunStore:
    store = RunStore.create(
        git_repo.common_git_dir,
        run_id=run_id,
        kind=kind,
        source_repo=git_repo.root,
        source_head=git_repo.head(),
        primary_family="claude",
        prompt="prompt",
        options={"pr_number": 16, "publish": True},
    )
    if state_updates:
        store.update(**state_updates)
    return store


def test_status_lists_all_runs_and_dumps_one(git_repo: GitRepo, monkeypatch, capsys) -> None:
    monkeypatch.chdir(git_repo.root)
    assert _dispatch(["status"]) == 0
    assert "No ai-harness runs" in capsys.readouterr().out

    _store(git_repo, "20260906-000001-task-aaaaaa", "task")
    review = _store(git_repo, "20260906-000002-review-bbbbbb", "review")
    review.begin_stage("review-correctness_reviewer", "claude")
    review.complete_stage(
        "review-correctness_reviewer",
        {
            "reviewer": "correctness_reviewer",
            "verdict": "pass",
            "summary": "x",
            "scope_reviewed": [],
            "residual_risks": [],
            "findings": [],
        },
    )

    assert _dispatch(["status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("20260906-000002-review-bbbbbb  review  running    1/1 stages")
    assert lines[1].startswith("20260906-000001-task-aaaaaa  task    running    0/0 stages")

    assert _dispatch(["status", review.run_id]) == 0
    output = capsys.readouterr().out
    assert json.loads(output.split("\n", 1)[1])["id"] == review.run_id


def test_resume_dispatches_task_and_review_kinds(git_repo: GitRepo, monkeypatch, capsys) -> None:
    monkeypatch.chdir(git_repo.root)
    seen: list[tuple[object, ...]] = []

    def fake_execute_task(store, repo, config, *, answers):
        seen.append(("task", store.run_id, repo.root, config, answers))
        return {"ok": True}

    def fake_execute_review(store, repo, config):
        seen.append(("review", store.run_id, repo.root, config, None))
        return {"reviewed": True}

    monkeypatch.setattr("ai_harness.cli.execute_task", fake_execute_task)
    monkeypatch.setattr("ai_harness.cli.execute_review", fake_execute_review)

    pending = {"questions": [_question()], "stage": "plan", "round": 1}
    task = _store(git_repo, "20260906-000001-task-aaaaaa", "task", pending_input=pending)
    assert (
        _dispatch(
            [
                "resume",
                task.run_id,
                "--answer",
                "Q001=yes",
                "--timeout",
                "30",
                "--no-slack",
                "--slack-wait",
                "5",
            ]
        )
        == 0
    )
    kind, run_id, root, config, answers = seen.pop()
    assert (kind, run_id, root) == ("task", task.run_id, git_repo.root)
    assert answers == {"Q001": "yes"}
    assert config.stage_timeout == 30
    assert config.slack_enabled is False
    assert config.slack_wait_seconds == 5
    assert task.load()["options"]["timeout"] == 30
    assert '"ok": true' in capsys.readouterr().out

    assert _dispatch(["resume", task.run_id, "--answer", "shorthand"]) == 0
    assert seen.pop()[4] == {"Q001": "shorthand"}

    review = _store(git_repo, "20260906-000002-review-bbbbbb", "review")
    assert _dispatch(["resume", review.run_id]) == 0
    assert seen.pop()[:2] == ("review", review.run_id)
    assert '"reviewed": true' in capsys.readouterr().out

    done = _store(
        git_repo,
        "20260906-000003-task-cccccc",
        "task",
        status="completed",
        result={"branch": "done"},
    )
    assert _dispatch(["resume", done.run_id]) == 0
    output = capsys.readouterr().out
    assert f"Run {done.run_id} is already complete." in output
    assert '"branch": "done"' in output
    assert not seen


def test_resume_rejects_bad_timeout_review_answers_and_unknown_kinds(
    git_repo: GitRepo, monkeypatch
) -> None:
    monkeypatch.chdir(git_repo.root)
    monkeypatch.setattr("ai_harness.cli.execute_task", lambda *a, **k: {})
    monkeypatch.setattr("ai_harness.cli.execute_review", lambda *a, **k: {})
    pending = {"questions": [_question()], "stage": "plan", "round": 1}
    task = _store(git_repo, "20260906-000001-task-aaaaaa", "task", pending_input=pending)
    with pytest.raises(HarnessError, match="at least one second"):
        _dispatch(["resume", task.run_id, "--timeout", "0"])

    review = _store(git_repo, "20260906-000002-review-bbbbbb", "review", pending_input=pending)
    with pytest.raises(HarnessError, match="do not accept planning answers"):
        _dispatch(["resume", review.run_id, "--answer", "Q001=yes"])

    doctor = _store(git_repo, "20260906-000003-doctor-cccccc", "doctor")
    with pytest.raises(HarnessError, match="cannot be resumed: doctor"):
        _dispatch(["resume", doctor.run_id])


def test_parse_answers_error_branches() -> None:
    assert _parse_answers({}, []) is None
    with pytest.raises(HarnessError, match="no pending planning questions"):
        _parse_answers({}, ["Q001=yes"])
    with pytest.raises(HarnessError, match="corrupt"):
        _parse_answers({"pending_input": {"questions": "bad"}}, ["Q001=yes"])
    two = {"pending_input": {"questions": [_question("Q001"), _question("Q002")]}}
    with pytest.raises(HarnessError, match="for each pending question"):
        _parse_answers(two, ["yes"])
    with pytest.raises(HarnessError, match="Duplicate answer for Q001"):
        _parse_answers(two, ["Q001=a", "Q001=b"])
    assert _parse_answers(two, ["Q002 = b ", "Q001=a"]) == {"Q001": "a", "Q002": "b"}


def test_cleanup_removes_retained_worktree_once(
    git_repo: GitRepo, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(git_repo.root)
    worktree = tmp_path / "retained"
    git_repo.create_detached_worktree(worktree, git_repo.head())
    task = _store(git_repo, "20260906-000001-task-aaaaaa", "task", worktree=str(worktree))

    assert _dispatch(["cleanup", task.run_id]) == 0
    assert f"Removed clean harness worktree {worktree}" in capsys.readouterr().out
    assert not worktree.exists()
    state = task.load()
    assert state["worktree"] is None
    assert state["cleanup"] == "worktree removed; branch retained"

    assert _dispatch(["cleanup", task.run_id]) == 0
    assert "has no retained worktree" in capsys.readouterr().out


def test_doctor_command_formats_shallow_deep_and_slack(monkeypatch, capsys) -> None:
    configs = []

    def fake_deep(config):
        configs.append(config)
        return {"claude": {}, "codex": {}}

    monkeypatch.setattr(
        "ai_harness.cli.shallow_doctor",
        lambda: {"git": {"version": "git 2.50", "path": "/usr/bin/git"}},
    )
    monkeypatch.setattr("ai_harness.cli.deep_doctor", fake_deep)
    monkeypatch.setattr(
        "ai_harness.cli.slack_doctor",
        lambda config: {"status": "unavailable", "connector": "", "problems": ["no user"]},
    )

    assert _dispatch(["doctor", "--deep", "--slack", "--timeout", "12"]) == 0
    output = capsys.readouterr().out
    assert "- git: git 2.50 (/usr/bin/git)" in output
    assert "real structured-output round trips" in output
    assert "Slack question channel: unavailable" in output
    assert "problem: no user" in output
    assert configs[0].slack_enabled is True
    assert configs[0].stage_timeout == 12

    assert _dispatch(["doctor"]) == 0
    assert "round trips" not in capsys.readouterr().out


def test_review_command_reports_publication_outcome(git_repo: GitRepo, monkeypatch, capsys) -> None:
    monkeypatch.chdir(git_repo.root)
    calls: list[dict[str, object]] = []
    results: list[dict[str, object]] = []

    class FakeStore:
        run_id = "review-test"

        def load(self):
            return {"result": results.pop(0)}

    def fake_start_review(
        repo, *, config, number, prompt, references, publish, council, progress
    ):
        calls.append(
            {"number": number, "prompt": prompt, "publish": publish, "council": council}
        )
        return FakeStore()

    monkeypatch.setattr("ai_harness.cli.start_review", fake_start_review)

    results.append({"review": "/r/review.md", "url": "https://example/review", "published": True})
    assert _dispatch(["/review", "16"]) == 0
    output = capsys.readouterr().out
    assert "Review run review-test complete." in output
    assert "Published: https://example/review" in output
    assert calls[-1] == {
        "number": 16,
        "prompt": "Review adversarially for bugs before they happen.",
        "publish": True,
        "council": None,
    }

    results.append({"review": "/r/review.md", "url": None, "published": True})
    assert _dispatch(["/review", "16", "Focus", "on", "auth"]) == 0
    assert "already published for this exact head" in capsys.readouterr().out
    assert calls[-1]["prompt"] == "Focus on auth"

    results.append({"review": "/r/review.md", "url": None, "published": False})
    assert _dispatch(["--no-publish", "/review", "16"]) == 0
    assert "Not published (--no-publish)." in capsys.readouterr().out
    assert calls[-1]["publish"] is False

    results.append({"review": "/r/review.md", "url": None, "published": False})
    assert _dispatch(["--no-publish", "--all-reviewers", "/review", "16"]) == 0
    capsys.readouterr()
    assert calls[-1]["council"] == [
        "correctness_reviewer",
        "refactorer",
        "security_reviewer",
        "performance_expert",
        "resumability_reviewer",
        "contract_reviewer",
    ]

    results.append({"review": "/r/review.md", "url": None, "published": False})
    assert _dispatch(["--no-publish", "--reviewer", "security_reviewer", "/review", "16"]) == 0
    capsys.readouterr()
    assert calls[-1]["council"] == ["security_reviewer"]

    with pytest.raises(HarnessError, match="cannot be combined"):
        _dispatch(["--all-reviewers", "--reviewer", "refactorer", "/review", "16"])
    with pytest.raises(HarnessError, match="only valid with /review"):
        _dispatch(["--all-reviewers", "Implement", "it"])
    with pytest.raises(SystemExit):
        _dispatch(["--reviewer", "nope", "/review", "16"])

    with pytest.raises(HarnessError, match="Invalid pull request number: abc"):
        _dispatch(["/review", "abc"])
    with pytest.raises(HarnessError, match="--local-only is only valid"):
        _dispatch(["--local-only", "/review", "16"])
    with pytest.raises(HarnessError, match="--slack is only valid"):
        _dispatch(["--slack", "/review", "16"])
    with pytest.raises(HarnessError, match="--no-publish is only valid with /review"):
        _dispatch(["--no-publish", "Implement", "it"])
    with pytest.raises(SystemExit):
        _dispatch(["/review"])
    with pytest.raises(SystemExit):
        _dispatch([])


def test_task_command_prints_delivery_summary_and_warnings(
    git_repo: GitRepo, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(git_repo.root)
    publications: list[dict[str, object]] = []

    class FakeStore:
        run_id = "task-test"

        def load(self):
            return {
                "warnings": ["verification skipped"],
                "result": {
                    "delivery": {
                        "commit": "abc123",
                        "pull_request": {"url": "https://example/pr/1"},
                        "review": {"publication": publications.pop(0), "findings": 2},
                    }
                },
            }

    monkeypatch.setattr("ai_harness.cli.start_task", lambda repo, **kwargs: FakeStore())

    publications.append({"url": "https://example/review"})
    assert _dispatch(["Implement", "it"]) == 0
    captured = capsys.readouterr()
    assert "warning: verification skipped" in captured.err
    assert "Commit: abc123" in captured.out
    assert "Draft PR: https://example/pr/1" in captured.out
    assert "Review: https://example/review" in captured.out
    assert "Findings: 2" in captured.out

    publications.append({"idempotent": True})
    assert _dispatch(["Implement", "it"]) == 0
    assert "Review: already published for this exact implementation" in capsys.readouterr().out


def test_main_maps_awaiting_input_and_harness_error_to_exit_codes(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["ai-harness", "anything"])
    question = {**_question(), "suggested_default": "text"}

    def awaiting(argv):
        raise AwaitingInput("run-1", [question])

    monkeypatch.setattr("ai_harness.cli._dispatch", awaiting)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "Task run run-1 paused for human input." in output
    assert "Q001: JSON or text?" in output
    assert "Suggested default: text" in output
    assert "ai-harness resume run-1 --answer 'Q001=...'" in output

    def failing(argv):
        raise HarnessError("boom")

    monkeypatch.setattr("ai_harness.cli._dispatch", failing)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "ai-harness: boom" in capsys.readouterr().err

    monkeypatch.setattr("ai_harness.cli._dispatch", lambda argv: 0)
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
