from __future__ import annotations

import json
from pathlib import Path

from ai_harness.github import repository_name
from ai_harness.process import CommandResult


def test_gh_infers_host_from_remote_instead_of_shell_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GH_HOST", "git.enterprise.example")

    def fake_run_command(argv, *, cwd, timeout, input_text, env):
        assert "GH_HOST" not in env
        return CommandResult(tuple(argv), 0, json.dumps({"nameWithOwner": "owner/repo"}), "")

    monkeypatch.setattr("ai_harness.github.run_command", fake_run_command)

    assert repository_name(tmp_path) == "owner/repo"
