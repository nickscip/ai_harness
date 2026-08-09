from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import HarnessError, StateError
from .state import RunStore, sha256_file

MAX_CONTEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_TOTAL_BYTES = 10 * 1024 * 1024


def split_prompt_and_references(parts: Sequence[str], cwd: Path) -> tuple[str, list[Path]]:
    prompt_parts: list[str] = []
    references: list[Path] = []
    for part in parts:
        if part.startswith("@") and len(part) > 1:
            candidate = Path(part[1:]).expanduser()
            if not candidate.is_absolute():
                candidate = cwd / candidate
            candidate = candidate.resolve()
            if not candidate.exists():
                raise HarnessError(f"Referenced context does not exist: {part}")
            if not candidate.is_file():
                raise HarnessError(f"Referenced context must be a file: {part}")
            references.append(candidate)
        else:
            prompt_parts.append(part)
    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        raise HarnessError("A non-empty prompt is required")
    return prompt, references


def copy_references(store: RunStore, references: Sequence[Path]) -> list[dict[str, Any]]:
    context_dir = store.root / "context"
    context_dir.mkdir(exist_ok=True)
    manifest: list[dict[str, Any]] = []
    total = 0
    for index, source in enumerate(references, start=1):
        size = source.stat().st_size
        if size > MAX_CONTEXT_FILE_BYTES:
            raise HarnessError(f"Context file is larger than 2 MiB: {source}")
        total += size
        if total > MAX_CONTEXT_TOTAL_BYTES:
            raise HarnessError("Referenced context exceeds the 10 MiB run limit")
        suffix = source.suffix if source.suffix else ".txt"
        destination = context_dir / f"ref-{index:03d}{suffix}"
        shutil.copyfile(source, destination)
        manifest.append(
            {
                "original": str(source),
                "copy": str(destination),
                "sha256": sha256_file(destination),
                "size": size,
            }
        )
    state = store.load()
    state["context"] = manifest
    store.save(state)
    return manifest


def verify_references(state: dict[str, Any]) -> None:
    for item in state.get("context", []):
        copy = Path(item["copy"])
        if not copy.is_file():
            raise StateError(f"Context copy disappeared: {copy}")
        if sha256_file(copy) != item["sha256"]:
            raise StateError(f"Context copy changed during the run: {copy}")


def context_prompt(state: dict[str, Any]) -> str:
    items = state.get("context", [])
    if not items:
        return "No supplemental context files were supplied."
    lines = [
        "Read these supplemental context copies exactly as files "
        "(the original @ syntax was removed):"
    ]
    for item in items:
        lines.append(f"- {item['copy']} (copied from {item['original']})")
    return "\n".join(lines)
