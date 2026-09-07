from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .errors import HarnessError

Provider = Literal["claude", "codex"]
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
_PROFILE_KEYS = {
    "description",
    "provider",
    "model",
    "fallback_model",
    "effort",
    "timeout_seconds",
    "critic_model",
    "critic_effort",
    "critic_fast",
    "apply_review",
}


@dataclass(frozen=True)
class HarnessProfile:
    name: str
    description: str
    provider: Provider
    model: str
    effort: str
    timeout_seconds: int
    fallback_model: str = ""
    # The critic is always the other family, so these fill that family's slots.
    critic_model: str = ""
    critic_effort: str = ""
    critic_fast: bool = False
    apply_review: bool = False


@dataclass(frozen=True)
class ProfileCatalog:
    path: Path
    default_profile: str
    profiles: dict[str, HarnessProfile]

    def select(self, name: str | None = None) -> HarnessProfile:
        selected = name or self.default_profile
        try:
            return self.profiles[selected]
        except KeyError as exc:
            available = ", ".join(sorted(self.profiles))
            raise HarnessError(
                f"Unknown harness profile {selected!r}; available profiles: {available}"
            ) from exc


def default_profiles_path() -> Path:
    override = os.getenv("AI_HARNESS_PROFILES")
    if override:
        return Path(override).expanduser().resolve()
    source_path = Path(__file__).resolve().parents[2] / "council-profiles.json"
    if source_path.is_file():
        return source_path
    return Path(__file__).with_name("council-profiles.json")


def _required_string(value: dict[str, Any], key: str, profile: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise HarnessError(f"Profile {profile!r} requires a non-empty {key}")
    return item.strip()


def _profile(name: str, value: Any) -> HarnessProfile:
    if not isinstance(value, dict):
        raise HarnessError(f"Profile {name!r} must be an object")
    unknown = set(value) - _PROFILE_KEYS
    if unknown:
        raise HarnessError(f"Profile {name!r} has unknown fields: {sorted(unknown)}")
    provider = _required_string(value, "provider", name)
    if provider not in {"claude", "codex"}:
        raise HarnessError(f"Profile {name!r} has unsupported provider {provider!r}")
    effort = _required_string(value, "effort", name)
    if effort not in _EFFORTS:
        raise HarnessError(f"Profile {name!r} has unsupported effort {effort!r}")
    if provider == "codex" and effort == "max":
        raise HarnessError(f"Profile {name!r} uses max effort, which Codex does not support")
    timeout = value.get("timeout_seconds")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 1800:
        raise HarnessError(f"Profile {name!r} timeout_seconds must be between 1 and 1800")
    fallback = value.get("fallback_model", "")
    if not isinstance(fallback, str):
        raise HarnessError(f"Profile {name!r} fallback_model must be a string")
    if provider != "claude" and fallback:
        raise HarnessError(f"Profile {name!r} can only use fallback_model with Claude")
    critic = "codex" if provider == "claude" else "claude"
    critic_model = value.get("critic_model", "")
    if not isinstance(critic_model, str):
        raise HarnessError(f"Profile {name!r} critic_model must be a string")
    critic_effort = value.get("critic_effort", "")
    if not isinstance(critic_effort, str):
        raise HarnessError(f"Profile {name!r} critic_effort must be a string")
    critic_effort = critic_effort.strip()
    if critic_effort and critic_effort not in _EFFORTS:
        raise HarnessError(f"Profile {name!r} has unsupported critic_effort {critic_effort!r}")
    if critic == "codex" and critic_effort == "max":
        raise HarnessError(
            f"Profile {name!r} uses max critic_effort, which Codex does not support"
        )
    critic_fast = value.get("critic_fast", False)
    if not isinstance(critic_fast, bool):
        raise HarnessError(f"Profile {name!r} critic_fast must be a boolean")
    if critic_fast and critic != "codex":
        raise HarnessError(f"Profile {name!r} can only use critic_fast with a Codex critic")
    apply_review = value.get("apply_review", False)
    if not isinstance(apply_review, bool):
        raise HarnessError(f"Profile {name!r} apply_review must be a boolean")
    return HarnessProfile(
        name=name,
        description=_required_string(value, "description", name),
        provider=provider,
        model=_required_string(value, "model", name),
        fallback_model=fallback.strip(),
        effort=effort,
        timeout_seconds=timeout,
        critic_model=critic_model.strip(),
        critic_effort=critic_effort,
        critic_fast=critic_fast,
        apply_review=apply_review,
    )


def load_profile_catalog(path: Path | None = None) -> ProfileCatalog:
    resolved = (path or default_profiles_path()).resolve()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HarnessError(f"Harness profile file does not exist: {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise HarnessError(f"Harness profile file is invalid JSON: {resolved}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise HarnessError("Harness profile file requires schema_version 1")
    default = raw.get("default_profile")
    values = raw.get("profiles")
    if not isinstance(default, str) or not default:
        raise HarnessError("Harness profile file requires default_profile")
    if not isinstance(values, dict) or not values:
        raise HarnessError("Harness profile file requires a non-empty profiles object")
    profiles = {str(name): _profile(str(name), value) for name, value in values.items()}
    if default not in profiles:
        raise HarnessError(f"Default harness profile {default!r} does not exist")
    return ProfileCatalog(path=resolved, default_profile=default, profiles=profiles)
