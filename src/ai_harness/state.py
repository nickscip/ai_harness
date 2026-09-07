from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import StateError
from .progress import (
    ProgressCallback,
    stage_completed_message,
    stage_label,
    stage_started_message,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_run_id(kind: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{kind}-{secrets.token_hex(3)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


class RunStore:
    def __init__(
        self,
        common_git_dir: Path,
        run_id: str,
        progress: ProgressCallback | None = None,
    ):
        self.common_git_dir = common_git_dir.resolve()
        self.run_id = run_id
        self.root = self.common_git_dir / "ai-harness" / "runs" / run_id
        self.state_path = self.root / "run.json"
        self.progress = progress
        self._stage_started: dict[str, float] = {}
        # Council specialists run in a thread pool and share one store, so every
        # load-modify-save below is a critical section.
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        common_git_dir: Path,
        *,
        run_id: str,
        kind: str,
        source_repo: Path,
        source_head: str,
        primary_family: str,
        prompt: str,
        options: dict[str, Any],
        progress: ProgressCallback | None = None,
    ) -> RunStore:
        store = cls(common_git_dir, run_id, progress=progress)
        if store.root.exists():
            raise StateError(f"Run already exists: {run_id}")
        store.root.mkdir(parents=True)
        now = utc_now()
        store.save(
            {
                "version": 1,
                "id": run_id,
                "kind": kind,
                "status": "running",
                "source_repo": str(source_repo.resolve()),
                "source_head": source_head,
                "primary_family": primary_family,
                "prompt": prompt,
                "options": options,
                "worktree": None,
                "branch": None,
                "context": [],
                "stages": {},
                "warnings": [],
                "created_at": now,
                "updated_at": now,
            }
        )
        return store

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StateError(f"Unknown run: {self.run_id}") from exc
        except json.JSONDecodeError as exc:
            raise StateError(f"Corrupt run state: {self.state_path}") from exc
        if value.get("id") != self.run_id or value.get("version") != 1:
            raise StateError(f"Invalid run state: {self.state_path}")
        return value

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        encoded = json.dumps(state, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        _atomic_write(self.state_path, encoded)

    def update(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            state = self.load()
            state.update(values)
            self.save(state)
            return state

    def begin_stage(self, name: str, family: str) -> None:
        with self._lock:
            state = self.load()
            stages = state["stages"]
            previous = stages.get(name, {})
            stages[name] = {
                **previous,
                "name": name,
                "family": family,
                "status": "running",
                "started_at": utc_now(),
                "error": None,
            }
            state["status"] = "running"
            self.save(state)
            self._stage_started[name] = time.monotonic()
        self.log(stage_started_message(name, family))

    def complete_stage(self, name: str, value: dict[str, Any]) -> Path:
        artifact = self.root / f"{name}.json"
        payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        _atomic_write(artifact, payload)
        with self._lock:
            state = self.load()
            stage = state["stages"].setdefault(name, {"name": name})
            stage.update(
                {
                    "status": "completed",
                    "artifact": artifact.name,
                    "sha256": sha256_bytes(payload),
                    "completed_at": utc_now(),
                    "error": None,
                }
            )
            self.save(state)
            started = self._stage_started.pop(name, None)
        elapsed = time.monotonic() - started if started is not None else None
        self.log(stage_completed_message(name, str(stage.get("family", "")), value, elapsed))
        return artifact

    def fail_stage(self, name: str, error: str) -> None:
        with self._lock:
            state = self.load()
            stage = state["stages"].setdefault(name, {"name": name})
            stage.update({"status": "failed", "error": error, "failed_at": utc_now()})
            state["status"] = "failed"
            self.save(state)
            started = self._stage_started.pop(name, None)
        elapsed = f" after {time.monotonic() - started:.1f}s" if started is not None else ""
        self.log(f"Failed stage: {stage_label(name)}{elapsed} — {error}")

    def read_completed_stage(self, name: str) -> dict[str, Any] | None:
        state = self.load()
        stage = state["stages"].get(name)
        if not stage or stage.get("status") != "completed":
            return None
        artifact_name = stage.get("artifact")
        if not artifact_name:
            raise StateError(f"Completed stage {name} has no artifact")
        artifact = self.root / artifact_name
        try:
            payload = artifact.read_bytes()
        except FileNotFoundError as exc:
            raise StateError(f"Missing artifact for completed stage {name}") from exc
        if sha256_bytes(payload) != stage.get("sha256"):
            raise StateError(f"Checksum mismatch for stage {name}")
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StateError(f"Invalid JSON artifact for stage {name}") from exc
        if not isinstance(value, dict):
            raise StateError(f"Stage artifact is not an object: {name}")
        return value

    def write_text_artifact(self, name: str, text: str) -> Path:
        path = self.root / name
        _atomic_write(path, text.encode("utf-8"))
        return path

    def log(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def list_runs(common_git_dir: Path) -> list[dict[str, Any]]:
    root = common_git_dir.resolve() / "ai-harness" / "runs"
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/run.json"), reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            runs.append(value)
    return runs


@contextmanager
def controller_lock(common_git_dir: Path) -> Iterator[None]:
    lock_path = common_git_dir.resolve() / "ai-harness" / "controller.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateError("Another ai-harness controller is active for this repository") from exc
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()
