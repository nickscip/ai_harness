from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import CommandError


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 60,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> CommandResult:
    command = [str(item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(
            f"Command timed out after {timeout}s: {command[0]}",
            argv=command,
            stderr=(exc.stderr or "") if isinstance(exc.stderr, str) else "",
        ) from exc
    except OSError as exc:
        raise CommandError(f"Could not execute {command[0]}: {exc}", argv=command) from exc

    result = CommandResult(
        argv=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if len(detail) > 2_000:
            detail = detail[-2_000:]
        raise CommandError(
            f"Command failed ({result.returncode}): {command[0]}\n{detail}",
            argv=command,
            stderr=result.stderr,
        )
    return result


_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
_EXACT_SECRETS = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITLAB_TOKEN",
    "SSH_AUTH_SOCK",
}


def sanitized_model_env(deny_bin: Path) -> dict[str, str]:
    """Best-effort subprocess hygiene; this is not same-user credential isolation."""
    clean: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if key in _EXACT_SECRETS:
            continue
        if upper.endswith("_KEY") or any(marker in upper for marker in _SECRET_MARKERS):
            continue
        clean[key] = value
    clean["PATH"] = f"{deny_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    clean["GIT_SSH_COMMAND"] = str(deny_bin / "ssh")
    clean["GIT_ASKPASS"] = "/usr/bin/false"
    clean["GIT_TERMINAL_PROMPT"] = "0"
    clean["GIT_OPTIONAL_LOCKS"] = "0"
    clean["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return clean


def controller_env() -> dict[str, str]:
    clean = dict(os.environ)
    for key in _EXACT_SECRETS:
        clean.pop(key, None)
    clean["GIT_TERMINAL_PROMPT"] = "0"
    return clean
