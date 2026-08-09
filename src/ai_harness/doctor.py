from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import jsonschema

from .config import HarnessConfig
from .errors import HarnessError
from .process import run_command
from .providers import ProviderRequest, ProviderRunner, find_claude, find_codex
from .state import RunStore, new_run_id


def _version(executable: Path) -> str:
    result = run_command([str(executable), "--version"], cwd=Path.cwd(), check=False)
    return (result.stdout.strip() or result.stderr.strip()).splitlines()[0]


def shallow_doctor() -> dict[str, Any]:
    claude = find_claude()
    codex = find_codex()
    tools: dict[str, Any] = {
        "claude": {"path": str(claude), "version": _version(claude)},
        "codex": {"path": str(codex), "version": _version(codex)},
        "jsonschema": {"version": getattr(jsonschema, "__version__", "installed")},
    }
    for command in ("git", "gh", "uv"):
        path = shutil.which(command)
        if not path:
            raise HarnessError(f"Required executable is missing: {command}")
        tools[command] = {"path": path, "version": _version(Path(path))}
    return tools


def deep_doctor(config: HarnessConfig) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ai-harness-doctor-") as temporary:
        repo = Path(temporary) / "repo"
        repo.mkdir()
        (repo / "probe.txt").write_text("doctor-probe\n", encoding="utf-8")
        run_command(["git", "init", "-b", "main"], cwd=repo)
        run_command(["git", "config", "user.name", "AI Harness Doctor"], cwd=repo)
        run_command(["git", "config", "user.email", "doctor@example.invalid"], cwd=repo)
        run_command(["git", "add", "probe.txt"], cwd=repo)
        run_command(["git", "commit", "-m", "probe"], cwd=repo)
        common = Path(
            run_command(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo
            ).stdout.strip()
        )
        store = RunStore.create(
            common,
            run_id=new_run_id("doctor"),
            kind="doctor",
            source_repo=repo,
            source_head=run_command(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip(),
            primary_family=config.primary_family,
            prompt="doctor",
            options={},
        )
        runner = ProviderRunner(config, store)
        results: dict[str, Any] = {}
        for family in ("claude", "codex"):
            results[family] = runner.run(
                ProviderRequest(
                    family=family,
                    stage=f"doctor-{family}",
                    cwd=repo,
                    prompt=(
                        "Read probe.txt. Return structured output with provider set to "
                        f"{family}, ok true, and a short message confirming doctor-probe. "
                        "Do not modify files or run network commands."
                    ),
                    schema_name="doctor",
                    writable=False,
                    timeout=config.stage_timeout,
                )
            )
        return results


def format_doctor(tools: dict[str, Any], deep: dict[str, Any] | None) -> str:
    lines = ["ai-harness doctor: PASS"]
    for name, value in tools.items():
        lines.append(f"- {name}: {value['version']} ({value.get('path', 'runtime')})")
    if deep is not None:
        lines.append("- real structured-output round trips: claude PASS, codex PASS")
    return "\n".join(lines)

