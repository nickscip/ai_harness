from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_harness.errors import HarnessError
from ai_harness.profiles import load_profile_catalog


def test_root_catalog_selects_default_and_swappable_profiles() -> None:
    catalog = load_profile_catalog()

    assert catalog.path.name == "council-profiles.json"
    assert catalog.select().name == "codex-balanced"
    deep = catalog.select("codex-deep")
    assert deep.provider == "codex"
    assert deep.model == "gpt-5.6-sol"
    assert deep.effort == "high"
    opus = catalog.select("claude-opus")
    assert opus.provider == "claude"
    assert opus.fallback_model == "sonnet"
    assert opus.timeout_seconds == 900


def test_catalog_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "profiles.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "default_profile": "broken",
                "profiles": {
                    "broken": {
                        "description": "broken",
                        "provider": "codex",
                        "model": "model",
                        "effort": "medium",
                        "timeout_seconds": 600,
                        "typo": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(HarnessError, match="unknown fields"):
        load_profile_catalog(path)


def test_claude_fable_pairs_a_fable_primary_with_a_fast_xhigh_codex_critic() -> None:
    profile = load_profile_catalog().select("claude-fable")

    assert (profile.provider, profile.model, profile.effort) == ("claude", "fable", "high")
    assert profile.critic_model == "gpt-5.6-sol"
    assert profile.critic_effort == "xhigh"
    assert profile.critic_fast is True
    assert profile.apply_review is True


def _catalog_with(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "profiles.json"
    profile: dict[str, object] = {
        "description": "under test",
        "provider": "claude",
        "model": "fable",
        "effort": "high",
        "timeout_seconds": 600,
    }
    profile.update(overrides)
    path.write_text(
        json.dumps(
            {"schema_version": 1, "default_profile": "subject", "profiles": {"subject": profile}}
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"critic_model": 5}, "critic_model must be a string"),
        ({"critic_effort": ["xhigh"]}, "critic_effort must be a string"),
        ({"critic_effort": "turbo"}, "unsupported critic_effort"),
        ({"critic_effort": "max"}, "max critic_effort"),
        ({"critic_fast": "yes"}, "critic_fast must be a boolean"),
        ({"provider": "codex", "model": "gpt-5.6-terra", "critic_fast": True}, "Codex critic"),
        ({"apply_review": 1}, "apply_review must be a boolean"),
    ],
)
def test_catalog_rejects_malformed_critic_overrides(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(HarnessError, match=message):
        load_profile_catalog(_catalog_with(tmp_path, **overrides))
