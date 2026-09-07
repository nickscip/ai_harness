from __future__ import annotations

from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.doctor import deep_doctor, format_doctor, shallow_doctor, slack_doctor
from ai_harness.errors import HarnessError
from ai_harness.process import CommandResult


def _version_command(stderr_for: str = ""):
    def fake_run_command(argv, *, cwd, timeout=60, check=True, **kwargs):
        name = Path(argv[0]).name
        if name == stderr_for:
            return CommandResult(tuple(argv), 1, "", f"{name} 9.9.9 (stderr)\nmore")
        return CommandResult(tuple(argv), 0, f"{name} 1.2.3\nextra line\n", "")

    return fake_run_command


def test_shallow_doctor_reports_versions_and_requires_git_gh_uv(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("ai_harness.doctor.find_claude", lambda: tmp_path / "claude")
    monkeypatch.setattr("ai_harness.doctor.find_codex", lambda: tmp_path / "codex")
    monkeypatch.setattr("ai_harness.doctor.run_command", _version_command(stderr_for="gh"))
    monkeypatch.setattr("ai_harness.doctor.shutil.which", lambda command: f"/usr/bin/{command}")

    tools = shallow_doctor()

    assert tools["claude"] == {"path": str(tmp_path / "claude"), "version": "claude 1.2.3"}
    assert tools["gh"]["version"] == "gh 9.9.9 (stderr)"
    assert tools["uv"]["path"] == "/usr/bin/uv"
    assert tools["jsonschema"]["version"]

    monkeypatch.setattr(
        "ai_harness.doctor.shutil.which", lambda command: None if command == "gh" else "/usr/bin/x"
    )
    with pytest.raises(HarnessError, match="missing: gh"):
        shallow_doctor()


def test_slack_doctor_disabled_missing_user_no_connector_and_ready(
    tmp_path: Path, monkeypatch
) -> None:
    assert slack_doctor(HarnessConfig())["status"] == "disabled"

    outputs: list[str] = []
    monkeypatch.setattr("ai_harness.doctor.find_claude", lambda: tmp_path / "claude")
    monkeypatch.setattr(
        "ai_harness.doctor.run_command",
        lambda argv, *, cwd, timeout, check: CommandResult(tuple(argv), 0, outputs.pop(0), ""),
    )

    outputs.append("nothing here\n")
    report = slack_doctor(HarnessConfig(slack_enabled=True, slack_user=""))
    assert report["status"] == "unavailable"
    assert report["connector"] == ""
    assert len(report["problems"]) == 2

    outputs.append("slack: https://mcp.slack.example (HTTP) - ✓ Connected\n")
    report = slack_doctor(HarnessConfig(slack_enabled=True, slack_user="U123"))
    assert report["status"] == "ready"
    assert report["connector"].startswith("slack:")
    assert report["problems"] == []


def test_deep_doctor_round_trips_both_families_with_fake_runner(monkeypatch) -> None:
    seen: list[tuple[str, str, bool]] = []

    class FakeRunner:
        def __init__(self, config, store):
            self.store = store

        def run(self, request):
            seen.append((request.family, request.stage, request.writable))
            assert (request.cwd / "probe.txt").read_text(encoding="utf-8") == "doctor-probe\n"
            self.store.begin_stage(request.stage, request.family)
            value = {"provider": request.family, "ok": True, "message": "doctor-probe"}
            self.store.complete_stage(request.stage, value)
            return value

    monkeypatch.setattr("ai_harness.doctor.ProviderRunner", FakeRunner)
    results = deep_doctor(HarnessConfig())
    assert set(results) == {"claude", "codex"}
    assert results["codex"]["provider"] == "codex"
    assert seen == [("claude", "doctor-claude", False), ("codex", "doctor-codex", False)]


def test_format_doctor_lists_tools_deep_and_slack() -> None:
    text = format_doctor(
        {"git": {"version": "git 2.50", "path": "/usr/bin/git"}, "jsonschema": {"version": "4"}},
        {"claude": {}, "codex": {}},
        {"status": "unavailable", "connector": "slack: ✓ Connected", "problems": ["no user"]},
    )
    assert text.splitlines() == [
        "ai-harness doctor: PASS",
        "- git: git 2.50 (/usr/bin/git)",
        "- jsonschema: 4 (runtime)",
        "- real structured-output round trips: claude PASS, codex PASS",
        "- Slack question channel: unavailable",
        "  slack: ✓ Connected",
        "  problem: no user",
    ]
    assert format_doctor({}, None) == "ai-harness doctor: PASS"
