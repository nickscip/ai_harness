from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ai_harness.config import HarnessConfig
from ai_harness.errors import ProviderError
from ai_harness.git import GitRepo
from ai_harness.process import CommandResult, sanitized_model_env
from ai_harness.providers import (
    ProviderRequest,
    ProviderRunner,
    build_claude_argv,
    build_codex_argv,
    find_claude,
    find_codex,
)
from ai_harness.state import RunStore


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


def test_writable_codex_requires_git_admin_dir(tmp_path: Path) -> None:
    request = ProviderRequest(
        family="codex",
        stage="probe",
        cwd=tmp_path,
        prompt="probe",
        schema_name="doctor",
        writable=True,
        timeout=30,
    )
    with pytest.raises(ProviderError, match="linked worktree Git directory"):
        build_codex_argv(request, HarnessConfig(), Path("/bin/codex"), tmp_path / "out.json")


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


# --- discovery and real round trips through fake executables ---------------------------------

CLAUDE_HELP = "--safe-mode --json-schema --permission-mode --effort --fallback-model"
CODEX_HELP = "--output-schema --ignore-user-config --add-dir"


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_tools(root: Path, *, claude_help: str = CLAUDE_HELP, codex_help: str = CODEX_HELP):
    """Shell stand-ins: answer help probes, otherwise emit the sibling `*.out` payload."""
    claude = _executable(
        root / "claude",
        "#!/bin/sh\n"
        f'if [ "$1" = "--help" ]; then echo "{claude_help}"; exit 0; fi\n'
        'cat "$(dirname "$0")/claude.out"\n',
    )
    codex = _executable(
        root / "codex",
        "#!/bin/sh\n"
        f'if [ "$1" = "exec" ] && [ "$2" = "--help" ]; then echo "{codex_help}"; exit 0; fi\n'
        'while [ $# -gt 0 ]; do\n'
        '  if [ "$1" = "-o" ]; then cp "$(dirname "$0")/codex.out" "$2"; exit 0; fi\n'
        "  shift\n"
        "done\n"
        "exit 3\n",
    )
    return claude, codex


@pytest.fixture
def isolated_discovery(tmp_path: Path, monkeypatch, clear_provider_caches):
    """Keep a real claude/codex install on the developer machine out of discovery."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AI_HARNESS_CLAUDE", raising=False)
    monkeypatch.delenv("AI_HARNESS_CODEX", raising=False)
    monkeypatch.setattr("ai_harness.providers.shutil.which", lambda command: None)
    return tmp_path


def test_find_tools_use_env_override_and_reject_incompatible_candidates(
    isolated_discovery: Path, monkeypatch
) -> None:
    good = isolated_discovery / "good"
    claude, codex = _fake_tools(good)
    monkeypatch.setenv("AI_HARNESS_CLAUDE", str(claude))
    monkeypatch.setenv("AI_HARNESS_CODEX", str(codex))
    # `which` returning the same binary must dedupe rather than probe twice.
    monkeypatch.setattr("ai_harness.providers.shutil.which", lambda command: str(good / command))
    assert find_claude() == claude.resolve()
    assert find_codex() == codex.resolve()

    find_claude.cache_clear()
    find_codex.cache_clear()
    monkeypatch.setattr("ai_harness.providers.shutil.which", lambda command: None)
    old = isolated_discovery / "old"
    old_claude, old_codex = _fake_tools(old, claude_help="--print", codex_help="--json")
    monkeypatch.setenv("AI_HARNESS_CLAUDE", str(old_claude))
    monkeypatch.setenv("AI_HARNESS_CODEX", str(old_codex))
    with pytest.raises(ProviderError, match=r"missing --safe-mode"):
        find_claude()
    with pytest.raises(ProviderError, match=r"missing --output-schema"):
        find_codex()

    find_claude.cache_clear()
    find_codex.cache_clear()
    garbage = isolated_discovery / "garbage"
    garbage.mkdir()
    for name in ("claude", "codex"):
        path = garbage / name
        path.write_bytes(b"\x00not an executable format\x00")
        path.chmod(0o755)
    monkeypatch.setenv("AI_HARNESS_CLAUDE", str(garbage / "claude"))
    monkeypatch.setenv("AI_HARNESS_CODEX", str(garbage / "codex"))
    with pytest.raises(ProviderError, match="No compatible Claude Code executable"):
        find_claude()
    with pytest.raises(ProviderError, match="none found"):
        find_codex()


def _store(git_repo: GitRepo) -> RunStore:
    return RunStore.create(
        git_repo.common_git_dir,
        run_id="20260906-000000-doctor-abc123",
        kind="doctor",
        source_repo=git_repo.root,
        source_head=git_repo.head(),
        primary_family="claude",
        prompt="doctor",
        options={},
    )


def test_provider_runner_round_trips_both_families(
    git_repo: GitRepo, isolated_discovery: Path, monkeypatch
) -> None:
    claude, codex = _fake_tools(isolated_discovery / "bin")
    monkeypatch.setenv("AI_HARNESS_CLAUDE", str(claude))
    monkeypatch.setenv("AI_HARNESS_CODEX", str(codex))
    (claude.parent / "claude.out").write_text(
        json.dumps(
            {
                "is_error": False,
                "structured_output": {"provider": "claude", "ok": True, "message": "hi"},
            }
        ),
        encoding="utf-8",
    )
    (codex.parent / "codex.out").write_text(
        json.dumps({"provider": "codex", "ok": True, "message": "hi"}), encoding="utf-8"
    )
    store = _store(git_repo)
    runner = ProviderRunner(HarnessConfig(), store)
    assert (runner.claude_deny_bin / "gh").exists()
    assert (runner.codex_deny_bin / "security").exists()
    assert not (runner.claude_deny_bin / "security").exists()

    for family in ("claude", "codex"):
        request = ProviderRequest(
            family=family,  # type: ignore[arg-type]
            stage=f"probe-{family}",
            cwd=git_repo.root,
            prompt="probe",
            schema_name="doctor",
            writable=False,
            timeout=30,
        )
        assert runner.run(request)["provider"] == family
        assert (store.root / f"logs/probe-{family}-{family}.stdout").exists()
        assert store.load()["stages"][f"probe-{family}"]["status"] == "completed"
    assert json.loads((store.root / ".probe-codex-codex-result.json").read_text())["ok"] is True


def _fake_run(stdout: str = "", returncode: int = 0, codex_output: str | None = None):
    def fake_run_command(argv, *, cwd, timeout, env, check):
        if codex_output is not None and "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(codex_output, encoding="utf-8")
        return CommandResult(tuple(argv), returncode, stdout, "stderr detail")

    return fake_run_command


@pytest.mark.parametrize(
    ("family", "run", "match"),
    [
        ("claude", _fake_run(returncode=2), r"Claude exited 2: stderr detail"),
        ("claude", _fake_run(json.dumps({"is_error": True, "result": "boom"})), "boom"),
        ("claude", _fake_run(json.dumps({"result": "[]"})), "did not contain a structured"),
        ("claude", _fake_run("not json"), r"claude stage probe failed"),
        (
            "claude",
            _fake_run(json.dumps({"structured_output": {"provider": "claude"}})),
            "Invalid doctor output",
        ),
        ("codex", _fake_run(returncode=1), r"Codex exited 1"),
        ("codex", _fake_run(), "did not write its final structured output"),
        ("codex", _fake_run(codex_output="[]"), "was not an object"),
    ],
)
def test_provider_runner_failure_matrix(
    git_repo: GitRepo, monkeypatch, family: str, run, match: str
) -> None:
    monkeypatch.setattr("ai_harness.providers.find_claude", lambda: Path("/bin/false"))
    monkeypatch.setattr("ai_harness.providers.find_codex", lambda: Path("/bin/false"))
    monkeypatch.setattr("ai_harness.providers.run_command", run)
    store = _store(git_repo)
    runner = ProviderRunner(HarnessConfig(), store)
    request = ProviderRequest(
        family=family,  # type: ignore[arg-type]
        stage="probe",
        cwd=git_repo.root,
        prompt="probe",
        schema_name="doctor",
        writable=False,
        timeout=30,
    )
    with pytest.raises(ProviderError, match=match):
        runner.run(request)
    stage = store.load()["stages"]["probe"]
    assert stage["status"] == "failed"
    assert stage["error"]


def test_provider_runner_accepts_json_encoded_result_string(git_repo: GitRepo, monkeypatch) -> None:
    monkeypatch.setattr("ai_harness.providers.find_claude", lambda: Path("/bin/false"))
    payload = {"provider": "claude", "ok": True, "message": "from result string"}
    monkeypatch.setattr(
        "ai_harness.providers.run_command", _fake_run(json.dumps({"result": json.dumps(payload)}))
    )
    runner = ProviderRunner(HarnessConfig(), _store(git_repo))
    request = ProviderRequest(
        family="claude",
        stage="probe",
        cwd=git_repo.root,
        prompt="probe",
        schema_name="doctor",
        writable=False,
        timeout=30,
    )
    assert runner.run(request) == payload
