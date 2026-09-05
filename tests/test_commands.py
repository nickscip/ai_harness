from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ai_harness.commands import (
    prepare_controller_command,
    resolve_controller_argv,
    validate_plan_commands,
    validate_planned_command,
)
from ai_harness.errors import CommandError, HarnessError
from ai_harness.process import run_command


def _command(argv: list[str]) -> dict[str, object]:
    return {"argv": argv, "cwd": ".", "timeout_seconds": 10, "purpose": "test"}


@pytest.mark.parametrize("executable", ["bash", "sh", "rm", "sudo"])
def test_command_policy_rejects_shells_and_destructive_tools(executable: str) -> None:
    with pytest.raises(HarnessError, match="Rejected"):
        validate_planned_command(_command([executable, "anything"]), preparation=False)


def test_command_policy_limits_git_subcommands() -> None:
    with pytest.raises(HarnessError, match="allowlist"):
        validate_planned_command(_command(["git", "push"]), preparation=False)
    assert validate_planned_command(_command(["git", "diff", "--check"]), preparation=False)
    assert validate_planned_command(_command(["pytest", "-c", "pyproject.toml"]), preparation=False)
    with pytest.raises(HarnessError, match="eval-style"):
        validate_planned_command(_command(["python", "-c", "print(1)"]), preparation=False)


def test_plan_command_policy_validates_before_execution() -> None:
    plan = {
        "preparation_commands": [],
        "verification_commands": [_command(["bash", "-lc", "pytest\nruff check ."])],
    }
    with pytest.raises(HarnessError, match="control characters"):
        validate_plan_commands(plan)


def test_subprocess_timeout_is_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="timed out"):
        run_command(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            cwd=tmp_path,
            timeout=1,
        )


def test_missing_pnpm_uses_corepack(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(command: str, *, path: str | None = None) -> str | None:
        assert path == "/controller/bin"
        return "/controller/bin/corepack" if command == "corepack" else None

    monkeypatch.setattr("ai_harness.commands.shutil.which", fake_which)

    assert resolve_controller_argv(
        ["pnpm", "install", "--frozen-lockfile"], {"PATH": "/controller/bin"}
    ) == [
        "/controller/bin/corepack",
        "pnpm",
        "install",
        "--frozen-lockfile",
    ]


def test_corepack_fallback_disables_manifest_auto_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(command: str, *, path: str | None = None) -> str | None:
        return "/controller/bin/corepack" if command == "corepack" else None

    monkeypatch.setattr("ai_harness.commands.shutil.which", fake_which)

    argv, env = prepare_controller_command(
        ["pnpm", "test"], {"PATH": "/controller/bin", "COREPACK_ENABLE_AUTO_PIN": "1"}
    )

    assert argv == ["/controller/bin/corepack", "pnpm", "test"]
    assert env["COREPACK_ENABLE_AUTO_PIN"] == "0"


def test_missing_python_falls_back_to_python3(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(command: str, *, path: str | None = None) -> str | None:
        return "/controller/bin/python3" if command == "python3" else None

    monkeypatch.setattr("ai_harness.commands.shutil.which", fake_which)

    assert resolve_controller_argv(
        ["python", "-m", "unittest", "discover"], {"PATH": "/controller/bin"}
    ) == ["python3", "-m", "unittest", "discover"]


def test_missing_python3_does_not_fall_back_to_python(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_which(command: str, *, path: str | None = None) -> str | None:
        return "/controller/bin/python" if command == "python" else None

    monkeypatch.setattr("ai_harness.commands.shutil.which", fake_which)

    assert resolve_controller_argv(["python3", "-m", "pytest"], {"PATH": "/controller/bin"}) == [
        "python3",
        "-m",
        "pytest",
    ]


def test_missing_interpreter_without_fallback_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_harness.commands.shutil.which", lambda command, *, path=None: None)

    assert resolve_controller_argv(["python", "-m", "unittest"], {"PATH": "/controller/bin"}) == [
        "python",
        "-m",
        "unittest",
    ]


def test_available_package_manager_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ai_harness.commands.shutil.which",
        lambda command, *, path=None: f"/controller/bin/{command}",
    )

    assert resolve_controller_argv(["pnpm", "test"], {"PATH": "/controller/bin"}) == [
        "pnpm",
        "test",
    ]
