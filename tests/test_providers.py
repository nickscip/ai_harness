from __future__ import annotations

import json
from pathlib import Path

from ai_harness.config import HarnessConfig
from ai_harness.process import sanitized_model_env
from ai_harness.providers import ProviderRequest, build_claude_argv, build_codex_argv


def _request(tmp_path: Path, *, family: str, writable: bool) -> ProviderRequest:
    git_admin = tmp_path / "outside" / "gitdir" if writable else None
    return ProviderRequest(
        family=family,  # type: ignore[arg-type]
        stage="probe",
        cwd=tmp_path,
        prompt="probe",
        schema_name="doctor",
        writable=writable,
        timeout=30,
        context_dirs=(tmp_path / "context",),
        git_admin_dir=git_admin,
    )


def test_claude_argv_is_hook_free_structured_and_noninteractive(tmp_path: Path) -> None:
    request = _request(tmp_path, family="claude", writable=True)
    argv = build_claude_argv(request, HarnessConfig(), Path("/bin/claude"))
    assert "--safe-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--no-session-persistence" in argv
    assert "--strict-mcp-config" in argv
    assert "Edit" in argv[argv.index("--allowedTools") + 1]
    assert "pytest" not in argv[argv.index("--allowedTools") + 1]
    provider_schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert "$schema" not in provider_schema


def test_claude_argv_uses_controller_timeout_without_a_turn_cap(tmp_path: Path) -> None:
    request = _request(tmp_path, family="claude", writable=False)
    argv = build_claude_argv(request, HarnessConfig(), Path("/bin/claude"))
    assert "--max-turns" not in argv


def test_claude_argv_applies_profile_effort_and_fallback(tmp_path: Path) -> None:
    request = _request(tmp_path, family="claude", writable=False)
    config = HarnessConfig(
        claude_model="opus",
        claude_effort="high",
        claude_fallback_model="sonnet",
    )

    argv = build_claude_argv(request, config, Path("/bin/claude"))

    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--fallback-model") + 1] == "sonnet"


def test_codex_argv_pins_model_reasoning_sandbox_and_gitdir(tmp_path: Path) -> None:
    request = _request(tmp_path, family="codex", writable=True)
    output = tmp_path / "result.json"
    argv = build_codex_argv(request, HarnessConfig(), Path("/bin/codex"), output)
    joined = " ".join(argv)
    assert "-a never exec" in joined
    assert "--ignore-user-config" in argv
    assert "workspace-write" in argv
    assert str(request.git_admin_dir) in argv
    assert 'model_reasoning_effort="medium"' in argv
    assert "allow_login_shell=false" in argv
    assert "sandbox_workspace_write.network_access=false" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    # Only tiers a model advertises are accepted, so a non-fast run omits the flag entirely.
    assert not any("service_tier" in item for item in argv)


def test_codex_argv_requests_the_priority_tier_when_the_critic_is_fast(tmp_path: Path) -> None:
    request = _request(tmp_path, family="codex", writable=False)
    config = HarnessConfig(codex_model="gpt-5.6-sol", codex_reasoning="xhigh", codex_fast=True)

    argv = build_codex_argv(request, config, Path("/bin/codex"), tmp_path / "result.json")

    assert argv[argv.index("-m") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="xhigh"' in argv
    assert 'service_tier="priority"' in argv


def test_model_environment_strips_tokens_and_prepends_deny_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("EXAMPLE_SECRET", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    deny = tmp_path / "deny"
    env = sanitized_model_env(deny)
    assert "GH_TOKEN" not in env
    assert "EXAMPLE_SECRET" not in env
    assert env["PATH"].startswith(f"{deny}:")
    assert env["GIT_SSH_COMMAND"] == str(deny / "ssh")
