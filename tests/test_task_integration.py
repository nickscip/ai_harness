from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.errors import AwaitingInput, HarnessError
from ai_harness.git import GitRepo
from ai_harness.process import run_command
from ai_harness.state import RunStore, list_runs
from ai_harness.task import execute_task, start_task, worktree_setup_command


def _plan() -> dict[str, object]:
    return {
        "summary": "Add a file.",
        "assumptions": [],
        "questions": [],
        "preparation_commands": [],
        "steps": [
            {
                "id": "P001",
                "title": "Add file",
                "description": "Add result.txt.",
                "files": ["result.txt"],
                "verification": ["Inspect the file"],
            }
        ],
        "verification_commands": [],
        "risks": [],
    }


def _revised_plan() -> dict[str, object]:
    plan = _plan()
    plan.pop("questions")
    return {**plan, "review_resolutions": []}


@pytest.mark.parametrize(
    ("primary", "expected"),
    [
        (
            "claude",
            [
                ("plan", "claude"),
                ("plan-review", "codex"),
                ("revised-plan", "claude"),
                ("implementation", "claude"),
            ],
        ),
        (
            "codex",
            [
                ("plan", "codex"),
                ("plan-review", "claude"),
                ("revised-plan", "codex"),
                ("implementation", "codex"),
            ],
        ),
    ],
)
def test_fake_provider_task_pipeline_is_ordered_resumable_and_uses_head(
    git_repo: GitRepo, monkeypatch, primary: str, expected: list[tuple[str, str]]
) -> None:
    calls: list[tuple[str, str]] = []
    (git_repo.root / "caller-only.txt").write_text("dirty\n", encoding="utf-8")

    class FakeRunner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            calls.append((request.stage, request.family))
            self.store.begin_stage(request.stage, request.family)
            if request.stage == "plan":
                value = _plan()
            elif request.stage == "plan-review":
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = _revised_plan()
            else:
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implemented.",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    monkeypatch.setattr("ai_harness.task.ProviderRunner", FakeRunner)
    config = HarnessConfig(primary_family=primary)  # type: ignore[arg-type]
    store = start_task(
        git_repo,
        config=config,
        prompt="Add result.txt",
        references=[],
    )
    state = store.load()
    assert calls == expected
    assert state["status"] == "completed"
    assert state["caller_dirty_paths"] == ["caller-only.txt"]
    worktree = Path(state["result"]["worktree"])
    assert (worktree / "result.txt").read_text(encoding="utf-8") == "implemented\n"
    assert not (worktree / "caller-only.txt").exists()
    assert "result.txt" in Path(state["result"]["patch"]).read_text(encoding="utf-8")

    calls.clear()
    execute_task(store, git_repo, config)
    assert calls == []


def test_planning_questions_pause_before_review_and_answers_resume_planning(
    git_repo: GitRepo, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []
    question = {
        "id": "Q001",
        "question": "Should the output be JSON or text?",
        "why_blocking": "The public file format changes the implementation.",
        "suggested_default": "JSON",
    }

    class QuestionRunner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            calls.append((request.stage, request.family))
            self.store.begin_stage(request.stage, request.family)
            if request.stage == "plan":
                value = {**_plan(), "questions": [question]}
            elif request.stage == "plan-r2":
                assert '"Q001": "Use JSON"' in request.prompt
                value = _plan()
            elif request.stage == "plan-review":
                assert "plan-r2.json" in request.prompt
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = _revised_plan()
            else:
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implemented.",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    monkeypatch.setattr("ai_harness.task.ProviderRunner", QuestionRunner)
    config = HarnessConfig()
    with pytest.raises(AwaitingInput) as paused:
        start_task(
            git_repo,
            config=config,
            prompt="Add result.txt",
            references=[],
        )
    assert calls == [("plan", "claude")]
    state = list_runs(git_repo.common_git_dir)[0]
    assert state["status"] == "awaiting_input"
    assert state["pending_input"]["questions"] == [question]
    assert paused.value.questions == [question]
    store = RunStore(git_repo.common_git_dir, state["id"])

    calls.clear()
    with pytest.raises(AwaitingInput):
        execute_task(store, git_repo, config)
    assert calls == []
    with pytest.raises(HarnessError, match="missing"):
        execute_task(store, git_repo, config, answers={"Q999": "wrong"})

    result = execute_task(store, git_repo, config, answers={"Q001": "Use JSON"})
    assert [stage for stage, _family in calls] == [
        "plan-r2",
        "plan-review",
        "revised-plan",
        "implementation",
    ]
    assert Path(result["worktree"], "result.txt").is_file()
    final = store.load()
    assert final["status"] == "completed"
    assert final["human_answers"][0]["answers"] == {"Q001": "Use JSON"}


def test_invalid_revised_plan_commands_get_one_repair_before_implementation(
    git_repo: GitRepo, monkeypatch
) -> None:
    calls: list[str] = []

    class RepairRunner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            calls.append(request.stage)
            self.store.begin_stage(request.stage, request.family)
            if request.stage == "plan":
                value = _plan()
            elif request.stage == "plan-review":
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = {
                    **_revised_plan(),
                    "verification_commands": [
                        {
                            "argv": ["bash", "-lc", "pytest\nruff check ."],
                            "cwd": ".",
                            "timeout_seconds": 10,
                            "purpose": "invalid compound check",
                        }
                    ],
                }
            elif request.stage == "command-repair":
                assert "Rejected command with control characters" in request.prompt
                value = _revised_plan()
            else:
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implemented.",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    monkeypatch.setattr("ai_harness.task.ProviderRunner", RepairRunner)
    store = start_task(
        git_repo,
        config=HarnessConfig(),
        prompt="Add result.txt",
        references=[],
    )
    assert calls == [
        "plan",
        "plan-review",
        "revised-plan",
        "command-repair",
        "implementation",
    ]
    assert store.load()["status"] == "completed"


def test_noop_implementation_gets_one_recovery_attempt(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class NoopRunner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            calls.append(request.stage)
            self.store.begin_stage(request.stage, request.family)
            if request.stage == "plan":
                value = _plan()
            elif request.stage == "plan-review":
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = _revised_plan()
            elif request.stage == "implementation":
                value = {
                    "summary": "Blocked without making edits.",
                    "changed_files": [],
                    "verification_requested": [],
                    "notes": ["No edits made."],
                }
            else:
                assert request.stage == "implementation-repair"
                assert "single no-op recovery attempt" in request.prompt
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implemented after recovery.",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    monkeypatch.setattr("ai_harness.task.ProviderRunner", NoopRunner)
    store = start_task(
        git_repo,
        config=HarnessConfig(),
        prompt="Add result.txt",
        references=[],
    )

    assert calls == [
        "plan",
        "plan-review",
        "revised-plan",
        "implementation",
        "implementation-repair",
    ]
    assert store.load()["status"] == "completed"
    assert Path(store.load()["result"]["worktree"], "result.txt").read_text() == "implemented\n"


def test_delivery_commits_reviews_pushes_opens_draft_pr_and_publishes(
    git_repo: GitRepo, monkeypatch
) -> None:
    source_head = git_repo.head()
    pushed: list[tuple[str, str]] = []
    created_prs: list[dict[str, object]] = []
    published: list[tuple[str, int]] = []

    class DeliveryRunner:
        def __init__(self, config, store: RunStore):
            self.store = store

        def run(self, request):
            self.store.begin_stage(request.stage, request.family)
            if request.stage == "plan":
                value = _plan()
            elif request.stage == "plan-review":
                value = {
                    "verdict": "approve",
                    "summary": "No defects.",
                    "findings": [],
                    "required_changes": [],
                }
            elif request.stage == "revised-plan":
                value = _revised_plan()
            else:
                (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
                value = {
                    "summary": "Implement the result file",
                    "changed_files": ["result.txt"],
                    "verification_requested": [],
                    "notes": [],
                }
            self.store.complete_stage(request.stage, value)
            return value

    def fake_create_local_review(
        repo,
        *,
        config,
        base,
        head,
        branch,
        title,
        prompt,
        references,
        progress,
    ):
        review_store = RunStore.create(
            repo.common_git_dir,
            run_id="20260809-000000-review-delivery",
            kind="review",
            source_repo=repo.root,
            source_head=head,
            primary_family=config.primary_family,
            prompt=prompt,
            options={"local_review": True, "publish": False, "pr_number": 0},
        )
        review_store.begin_stage("review-final", config.primary_family)
        review_store.complete_stage(
            "review-final", {"summary": "No actionable findings.", "findings": []}
        )
        review_store.begin_stage("evidence-validation", "controller")
        review_store.complete_stage(
            "evidence-validation",
            {
                "body": "No actionable findings.",
                "inline_findings": [],
                "summary_only_findings": [],
            },
        )
        review_store.write_text_artifact("review.md", "No actionable findings.\n")
        review_store.update(
            status="completed",
            result={"published": False, "findings": 0},
            pr={"headRefOid": head},
        )
        return review_store

    def fake_push(self, worktree, branch):
        pushed.append((str(worktree), branch))

    def fake_create_pr(repo, **kwargs):
        created_prs.append(kwargs)
        return {
            "number": 12,
            "url": "https://api.github.com/repos/owner/repo/pulls/12",
            "html_url": "https://example.invalid/owner/repo/pull/12",
            "draft": True,
        }

    def fake_publish(review_store, *, repo, name_with_owner, number):
        published.append((name_with_owner, number))
        return {"published": True, "url": "https://example.invalid/review/34"}

    monkeypatch.setattr("ai_harness.task.ProviderRunner", DeliveryRunner)
    monkeypatch.setattr("ai_harness.task.create_local_review", fake_create_local_review)
    monkeypatch.setattr(
        "ai_harness.task.execute_review",
        lambda review_store, repo, config: review_store.load()["result"],
    )
    monkeypatch.setattr("ai_harness.task.publish_local_review", fake_publish)
    monkeypatch.setattr("ai_harness.task.repository_name", lambda repo: "owner/repo")
    monkeypatch.setattr("ai_harness.task.open_pull_request_for_head", lambda repo, branch: None)
    monkeypatch.setattr("ai_harness.task.create_draft_pull_request", fake_create_pr)
    monkeypatch.setattr(GitRepo, "push_branch", fake_push)

    store = start_task(
        git_repo,
        config=HarnessConfig(),
        prompt="Add result.txt",
        references=[],
        deliver=True,
    )

    state = store.load()
    result = state["result"]
    worktree = Path(result["worktree"])
    assert state["status"] == "completed"
    assert result["delivery"]["commit"] != source_head
    assert result["delivery"]["pull_request"] == {
        "number": 12,
        "url": "https://example.invalid/owner/repo/pull/12",
        "repository": "owner/repo",
        "draft": True,
    }
    assert result["delivery"]["review"]["findings"] == 0
    assert git_repo.status(cwd=worktree) == []
    assert git_repo.head() == source_head
    assert pushed == [(str(worktree), state["branch"])]
    assert created_prs[0]["head"] == state["branch"]
    assert created_prs[0]["base"] == "main"
    assert published == [("owner/repo", 12)]

    monkeypatch.setattr(
        GitRepo,
        "push_branch",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not push twice")),
    )
    execute_task(store, git_repo, HarnessConfig())


class _PlanOnlyRunner:
    """Drive the pipeline without providers so bootstrap behaviour is what fails or passes."""

    def __init__(self, config, store: RunStore):
        self.store = store

    def run(self, request):
        self.store.begin_stage(request.stage, request.family)
        if request.stage == "plan":
            value = _plan()
        elif request.stage == "plan-review":
            value = {
                "verdict": "approve",
                "summary": "No defects.",
                "findings": [],
                "required_changes": [],
            }
        elif request.stage == "revised-plan":
            value = _revised_plan()
        else:
            (request.cwd / "result.txt").write_text("implemented\n", encoding="utf-8")
            value = {
                "summary": "Implemented.",
                "changed_files": ["result.txt"],
                "verification_requested": [],
                "notes": [],
            }
        self.store.complete_stage(request.stage, value)
        return value


def _commit_makefile(git_repo: GitRepo, recipe: str, *, gitignore: str = "") -> None:
    (git_repo.root / "Makefile").write_text(
        f"ENV_SOURCE ?= ../../ai/.env\n\nworktree-setup:\n\t{recipe}\n", encoding="utf-8"
    )
    paths = ["Makefile"]
    if gitignore:
        (git_repo.root / ".gitignore").write_text(gitignore, encoding="utf-8")
        paths.append(".gitignore")
    run_command(["git", "add", *paths], cwd=git_repo.root)
    run_command(["git", "commit", "-m", "add makefile"], cwd=git_repo.root)


def test_worktree_setup_command_is_opt_in_per_project(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    source = tmp_path / "source"

    assert worktree_setup_command(worktree, source) is None

    (worktree / "Makefile").write_text("install:\n\techo hi\n", encoding="utf-8")
    assert worktree_setup_command(worktree, source) is None

    (worktree / "Makefile").write_text(
        "worktree-setup: worktree-copy-env install\n", encoding="utf-8"
    )
    assert worktree_setup_command(worktree, source) == [
        "make",
        "worktree-setup",
        f"ENV_SOURCE={source / '.env'}",
    ]


def test_worktree_setup_runs_before_planning_and_installs_ignored_inputs(
    git_repo: GitRepo, monkeypatch
) -> None:
    (git_repo.root / ".env").write_text("SECRET=from-source\n", encoding="utf-8")
    _commit_makefile(git_repo, 'cp "$(ENV_SOURCE)" .', gitignore=".env\n")
    monkeypatch.setattr("ai_harness.task.ProviderRunner", _PlanOnlyRunner)

    store = start_task(
        git_repo,
        config=HarnessConfig(),
        prompt="Add result.txt",
        references=[],
    )

    state = store.load()
    worktree = Path(state["result"]["worktree"])
    bootstrap = store.read_completed_stage("worktree-bootstrap")
    assert bootstrap is not None and bootstrap["ran"] is True
    assert (worktree / ".env").read_text(encoding="utf-8") == "SECRET=from-source\n"
    assert [item.path for item in git_repo.status(cwd=worktree)] == ["result.txt"]
    assert state["status"] == "completed"


def test_worktree_setup_that_dirties_tracked_files_fails_the_run(
    git_repo: GitRepo, monkeypatch
) -> None:
    _commit_makefile(git_repo, "echo setup-touched-a-tracked-file > tracked.txt")
    monkeypatch.setattr("ai_harness.task.ProviderRunner", _PlanOnlyRunner)

    with pytest.raises(HarnessError, match="Worktree setup changed the worktree: tracked.txt"):
        start_task(
            git_repo,
            config=HarnessConfig(),
            prompt="Add result.txt",
            references=[],
        )


def test_missing_makefile_records_a_skipped_bootstrap_stage(
    git_repo: GitRepo, monkeypatch
) -> None:
    monkeypatch.setattr("ai_harness.task.ProviderRunner", _PlanOnlyRunner)

    store = start_task(
        git_repo,
        config=HarnessConfig(),
        prompt="Add result.txt",
        references=[],
    )

    bootstrap = store.read_completed_stage("worktree-bootstrap")
    assert bootstrap == {"ran": False, "commands": []}
