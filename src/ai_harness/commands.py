from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .git import resolve_inside
from .process import controller_env, run_command
from .progress import ProgressCallback

_ALLOWED_TOOLS = {
    "bun",
    "cargo",
    "go",
    "make",
    "mypy",
    "npm",
    "npx",
    "pnpm",
    "pytest",
    "python",
    "python3",
    "ruff",
    "tox",
    "uv",
    "yarn",
}
_ALLOWED_GIT_VERIFY = {"diff", "status", "grep", "show"}
_ALLOWED_GIT_PREPARE = {"submodule", "lfs"}
_FORBIDDEN_ARGUMENTS = {"-c", "--eval", "-e"}
_COREPACK_MANAGERS = {"pnpm", "yarn"}
_INTERPRETER_FALLBACKS = {"python": "python3"}


def validate_planned_command(command: dict[str, Any], *, preparation: bool) -> list[str]:
    argv = list(command["argv"])
    executable = Path(argv[0]).name
    if any("\x00" in item or "\n" in item or "\r" in item for item in argv):
        raise HarnessError(f"Rejected command with control characters: {argv!r}")
    if executable in {"sh", "bash", "zsh", "fish", "sudo", "env", "xargs", "rm"}:
        raise HarnessError(f"Rejected shell or destructive executable: {executable}")
    if executable in {"python", "python3"} and any(
        item in _FORBIDDEN_ARGUMENTS for item in argv[1:]
    ):
        raise HarnessError(f"Rejected eval-style command arguments: {argv!r}")
    if executable == "git":
        allowed = _ALLOWED_GIT_PREPARE if preparation else _ALLOWED_GIT_VERIFY
        if len(argv) < 2 or argv[1] not in allowed:
            raise HarnessError(f"Rejected git command outside controller allowlist: {argv!r}")
    elif executable not in _ALLOWED_TOOLS:
        raise HarnessError(f"Rejected executable outside controller allowlist: {executable}")
    return argv


def validate_plan_commands(plan: dict[str, Any]) -> None:
    """Validate every controller command before implementation can begin."""
    for command in plan["preparation_commands"]:
        validate_planned_command(command, preparation=True)
    for command in plan["verification_commands"]:
        validate_planned_command(command, preparation=False)


def resolve_controller_argv(argv: list[str], env: dict[str, str]) -> list[str]:
    """Fall back to Corepack or python3 when the requested executable is unavailable."""
    path = env.get("PATH")
    if shutil.which(argv[0], path=path):
        return argv
    executable = Path(argv[0]).name
    if executable in _COREPACK_MANAGERS:
        corepack = shutil.which("corepack", path=path)
        if corepack:
            return [corepack, executable, *argv[1:]]
    interpreter = _INTERPRETER_FALLBACKS.get(executable)
    if interpreter and shutil.which(interpreter, path=path):
        return [interpreter, *argv[1:]]
    return argv


def prepare_controller_command(
    argv: list[str], env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """Resolve an executable without letting Corepack edit the target repository."""
    resolved = resolve_controller_argv(argv, env)
    if resolved != argv and Path(resolved[0]).name == "corepack":
        env = {**env, "COREPACK_ENABLE_AUTO_PIN": "0"}
    return resolved, env


def execute_planned_commands(
    commands: list[dict[str, Any]],
    *,
    worktree: Path,
    preparation: bool,
    progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    kind = "preparation" if preparation else "verification"
    for index, command in enumerate(commands, start=1):
        argv = validate_planned_command(command, preparation=preparation)
        cwd = resolve_inside(worktree, command["cwd"] or ".")
        if not cwd.is_dir():
            raise HarnessError(f"Planned command cwd does not exist: {cwd}")
        env = controller_env()
        argv, env = prepare_controller_command(argv, env)
        rendered = " ".join(argv)
        if progress is not None:
            progress(
                f"Running {kind} command {index}/{len(commands)}: {rendered} "
                f"({command['purpose']})"
            )
        started = time.monotonic()
        result = run_command(
            argv,
            cwd=cwd,
            timeout=command["timeout_seconds"],
            env=env,
            check=False,
        )
        record = {
            "argv": argv,
            "cwd": str(cwd),
            "purpose": command["purpose"],
            "returncode": result.returncode,
            "stdout": result.stdout[-20_000:],
            "stderr": result.stderr[-20_000:],
        }
        results.append(record)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise HarnessError(
                f"Planned command failed ({result.returncode}): {' '.join(argv)}\n{detail[-2000:]}"
            )
        if progress is not None:
            progress(
                f"Completed {kind} command {index}/{len(commands)} in "
                f"{time.monotonic() - started:.1f}s: {rendered}"
            )
    return results
