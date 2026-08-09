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
