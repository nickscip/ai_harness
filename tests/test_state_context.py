from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.context import copy_references, split_prompt_and_references, verify_references
from ai_harness.errors import StateError
from ai_harness.state import RunStore


def _store(tmp_path: Path) -> RunStore:
    common = tmp_path / ".git"
    common.mkdir()
    return RunStore.create(
        common,
        run_id="test-run",
        kind="task",
        source_repo=tmp_path,
        source_head="a" * 40,
        primary_family="claude",
        prompt="do it",
        options={},
    )


def test_at_references_are_removed_copied_and_hash_checked(tmp_path: Path) -> None:
    reference = tmp_path / "SKILL.md"
    reference.write_text("instructions\n", encoding="utf-8")
    prompt, references = split_prompt_and_references(
        ["refactor", "@SKILL.md", "carefully"], tmp_path
    )
    assert prompt == "refactor carefully"
    store = _store(tmp_path)
    manifest = copy_references(store, references)
    assert "@" not in Path(manifest[0]["copy"]).name
    verify_references(store.load())
    Path(manifest[0]["copy"]).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(StateError, match="changed"):
        verify_references(store.load())


def test_completed_stage_checksum_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    artifact = store.complete_stage("plan", {"ok": True})
    assert store.read_completed_stage("plan") == {"ok": True}
    artifact.write_text("{}\n", encoding="utf-8")
    with pytest.raises(StateError, match="Checksum"):
        store.read_completed_stage("plan")


def test_run_store_reports_stage_start_completion_and_summary(tmp_path: Path) -> None:
    messages: list[str] = []
    common = tmp_path / ".git"
    common.mkdir()
    store = RunStore.create(
        common,
        run_id="progress-run",
        kind="task",
        source_repo=tmp_path,
        source_head="a" * 40,
        primary_family="claude",
        prompt="do it",
        options={},
        progress=messages.append,
    )

    store.begin_stage("plan-review", "codex")
    store.complete_stage(
        "plan-review",
        {"verdict": "approve", "findings": [], "required_changes": []},
    )

    assert messages[0] == "Starting agent: adversarial plan review (codex)"
    assert messages[1].startswith(
        "Completed agent: adversarial plan review (codex) in "
    )
    assert messages[1].endswith("— approve, 0 finding(s)")
