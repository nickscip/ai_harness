from __future__ import annotations

import json
import os
import shutil
import stat
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import Family, HarnessConfig
from .errors import CommandError, ProviderError
from .process import run_command, sanitized_model_env
from .schema import claude_schema, load_schema, schema_path, validate_output
from .state import RunStore

DENIED_EXECUTABLES = (
    "gh",
    "ssh",
    "scp",
    "sftp",
    "curl",
    "wget",
    "nc",
    "ncat",
    "netcat",
    "osascript",
    "open",
)
CODEX_ONLY_DENIED_EXECUTABLES = ("security",)


@dataclass(frozen=True)
class ProviderRequest:
    family: Family
    stage: str
    cwd: Path
    prompt: str
    schema_name: str
    writable: bool
    timeout: int
    context_dirs: tuple[Path, ...] = ()
    git_admin_dir: Path | None = None


def _candidate_paths(env_name: str, command: str) -> list[Path]:
    candidates: list[Path] = []
    explicit = os.getenv(env_name)
    if explicit:
        candidates.append(Path(explicit).expanduser())
    resolved = shutil.which(command)
    if resolved:
        candidates.append(Path(resolved))
    candidates.append(Path.home() / ".local" / "bin" / command)
    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        normalized = candidate.expanduser().resolve()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(normalized)
    return unique


def _help_text(path: Path) -> str:
    try:
        result = run_command([str(path), "--help"], cwd=Path.cwd(), timeout=10, check=False)
    except CommandError:
        return ""
    return f"{result.stdout}\n{result.stderr}"


@lru_cache(maxsize=1)
def find_claude() -> Path:
    required = (
        "--safe-mode",
        "--json-schema",
        "--permission-mode",
        "--effort",
        "--fallback-model",
    )
    unsuitable: list[str] = []
    for candidate in _candidate_paths("AI_HARNESS_CLAUDE", "claude"):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        help_text = _help_text(candidate)
        missing = [flag for flag in required if flag not in help_text]
        if not missing:
            return candidate
        unsuitable.append(f"{candidate} (missing {', '.join(missing)})")
    detail = "; ".join(unsuitable) if unsuitable else "none found"
    raise ProviderError(
        "No compatible Claude Code executable. Set AI_HARNESS_CLAUDE to a binary "
        f"with safe mode and structured output support. Candidates: {detail}"
    )


@lru_cache(maxsize=1)
def find_codex() -> Path:
    required = ("--output-schema", "--ignore-user-config", "--add-dir")
    unsuitable: list[str] = []
    for candidate in _candidate_paths("AI_HARNESS_CODEX", "codex"):
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        try:
            result = run_command(
                [str(candidate), "exec", "--help"], cwd=Path.cwd(), timeout=10, check=False
            )
        except CommandError:
            continue
        help_text = f"{result.stdout}\n{result.stderr}"
        missing = [flag for flag in required if flag not in help_text]
        if not missing:
            return candidate
        unsuitable.append(f"{candidate} (missing {', '.join(missing)})")
    detail = "; ".join(unsuitable) if unsuitable else "none found"
    raise ProviderError(
        "No compatible Codex CLI executable. Set AI_HARNESS_CODEX to a binary "
        f"with exec structured output and sandbox support. Candidates: {detail}"
    )


def create_deny_bin(
    store: RunStore,
    *,
    name: str = "deny-bin",
    extra: tuple[str, ...] = (),
) -> Path:
    deny_bin = store.root / name
    deny_bin.mkdir(exist_ok=True)
    script = "#!/bin/sh\nexit 97\n"
    for executable in (*DENIED_EXECUTABLES, *extra):
        path = deny_bin / executable
        path.write_text(script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return deny_bin


def build_claude_argv(
    request: ProviderRequest,
    config: HarnessConfig,
    executable: Path,
) -> list[str]:
    canonical = load_schema(request.schema_name)
    tools = ["Read", "Glob", "Grep", "Bash"]
    allowed = [
        "Read",
        "Glob",
        "Grep",
        "Bash(git --no-optional-locks status:*)",
        "Bash(git --no-optional-locks diff:*)",
        "Bash(git --no-optional-locks show:*)",
        "Bash(git --no-optional-locks log:*)",
        "Bash(git --no-optional-locks rev-parse:*)",
        "Bash(rg:*)",
    ]
    if request.writable:
        tools.extend(["Edit", "Write"])
        allowed.extend(["Edit", "Write"])
    argv = [
        str(executable),
        "--safe-mode",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--settings",
        '{"disableAllHooks":true,"autoMemoryEnabled":false}',
        "--permission-mode",
        "dontAsk",
        "--tools",
        ",".join(tools),
        "--allowedTools",
        ",".join(allowed),
        "--effort",
        config.claude_effort,
    ]
    if config.claude_fallback_model:
        argv.extend(["--fallback-model", config.claude_fallback_model])
    for directory in request.context_dirs:
        argv.extend(["--add-dir", str(directory)])
    argv.extend(
        [
            "--json-schema",
            json.dumps(claude_schema(canonical), separators=(",", ":")),
            "--max-budget-usd",
            str(config.claude_max_budget_usd),
            "--no-session-persistence",
            "--model",
            config.model_for("claude"),
            "--output-format",
            "json",
            "-p",
            request.prompt,
        ]
    )
    return argv


def build_codex_argv(
    request: ProviderRequest,
    config: HarnessConfig,
    executable: Path,
    output_file: Path,
) -> list[str]:
    argv = [
        str(executable),
        "-a",
        "never",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-s",
        "workspace-write" if request.writable else "read-only",
        "-C",
        str(request.cwd),
    ]
    if request.writable:
        if request.git_admin_dir is None:
            raise ProviderError("A writable Codex run requires the linked worktree Git directory")
        argv.extend(["--add-dir", str(request.git_admin_dir)])
    argv.extend(
        [
            "--output-schema",
            str(schema_path(request.schema_name)),
            "-o",
            str(output_file),
            "-m",
            config.model_for("codex"),
            "-c",
            f'model_reasoning_effort="{config.codex_reasoning}"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "allow_login_shell=false",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-c",
            "web_search=\"disabled\"",
            "-c",
            "features.apps=false",
            "-c",
            "features.hooks=false",
            "-c",
            "mcp_servers={}",
            "--color",
            "never",
            request.prompt,
        ]
    )
    return argv


class ProviderRunner:
    def __init__(self, config: HarnessConfig, store: RunStore):
        self.config = config
        self.store = store
        # Claude's parent process invokes macOS `security` while refreshing its
        # saved login. Its Bash tool cannot invoke that command because of the
        # explicit allowedTools patterns. Codex has no equivalent Bash allowlist,
        # so its PATH also shadows `security`.
        self.claude_deny_bin = create_deny_bin(store, name="deny-bin-claude")
        self.codex_deny_bin = create_deny_bin(
            store,
            name="deny-bin-codex",
            extra=CODEX_ONLY_DENIED_EXECUTABLES,
        )

    def run(self, request: ProviderRequest) -> dict[str, Any]:
        self.store.begin_stage(request.stage, request.family)
        try:
            if request.family == "claude":
                value = self._run_claude(request)
            else:
                value = self._run_codex(request)
            value = validate_output(request.schema_name, value)
            self.store.complete_stage(request.stage, value)
            return value
        except (CommandError, ProviderError, OSError, json.JSONDecodeError) as exc:
            self.store.fail_stage(request.stage, str(exc))
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"{request.family} stage {request.stage} failed: {exc}") from exc

    def _run_claude(self, request: ProviderRequest) -> dict[str, Any]:
        executable = find_claude()
        argv = build_claude_argv(request, self.config, executable)
        result = run_command(
            argv,
            cwd=request.cwd,
            timeout=request.timeout,
            env=sanitized_model_env(self.claude_deny_bin),
            check=False,
        )
        self.store.write_text_artifact(f"logs/{request.stage}-claude.stdout", result.stdout)
        self.store.write_text_artifact(f"logs/{request.stage}-claude.stderr", result.stderr)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ProviderError(f"Claude exited {result.returncode}: {detail[-2000:]}")
        outer = json.loads(result.stdout)
        if outer.get("is_error"):
            raise ProviderError(f"Claude reported an error: {outer.get('result', 'unknown error')}")
        structured = outer.get("structured_output")
        if isinstance(structured, dict):
            return structured
        result_text = outer.get("result")
        if isinstance(result_text, str):
            decoded = json.loads(result_text)
            if isinstance(decoded, dict):
                return decoded
        raise ProviderError("Claude output did not contain a structured object")

    def _run_codex(self, request: ProviderRequest) -> dict[str, Any]:
        executable = find_codex()
        output_file = self.store.root / f".{request.stage}-codex-result.json"
        with suppress(FileNotFoundError):
            output_file.unlink()
        argv = build_codex_argv(request, self.config, executable, output_file)
        result = run_command(
            argv,
            cwd=request.cwd,
            timeout=request.timeout,
            env=sanitized_model_env(self.codex_deny_bin),
            check=False,
        )
        self.store.write_text_artifact(f"logs/{request.stage}-codex.stdout", result.stdout)
        self.store.write_text_artifact(f"logs/{request.stage}-codex.stderr", result.stderr)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ProviderError(f"Codex exited {result.returncode}: {detail[-2000:]}")
        try:
            decoded = json.loads(output_file.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProviderError("Codex did not write its final structured output") from exc
        if not isinstance(decoded, dict):
            raise ProviderError("Codex final output was not an object")
        return decoded
