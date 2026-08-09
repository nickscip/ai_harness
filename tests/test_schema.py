from __future__ import annotations

from importlib.resources import files

import jsonschema
import pytest

from ai_harness.errors import ProviderError
from ai_harness.schema import (
    load_schema,
    validate_plan,
    validate_plan_review,
    validate_revised_plan,
)


def test_all_packaged_schemas_are_valid_draft_2020_12() -> None:
    directory = files("ai_harness").joinpath("schemas")
    names = [
        item.name.removesuffix(".json")
        for item in directory.iterdir()
        if item.name.endswith(".json")
    ]
    assert names
    for name in names:
        jsonschema.Draft202012Validator.check_schema(load_schema(name))


def test_review_dispositions_must_be_complete() -> None:
    review = {
        "findings": [
            {"id": "R001"},
            {"id": "R002"},
        ],
        "required_changes": ["R001"],
    }
    validate_plan_review(review)
    revised = {
        "steps": [{"id": "P001"}],
        "review_resolutions": [
            {
                "finding_id": "R001",
                "disposition": "fixed",
                "explanation": "fixed",
                "affected_steps": ["P001"],
            }
        ],
    }
    with pytest.raises(ProviderError, match="incomplete"):
        validate_revised_plan(review, revised)


def test_review_rejects_unknown_required_change() -> None:
    review = {"findings": [{"id": "R001"}], "required_changes": ["R999"]}
    with pytest.raises(ProviderError, match="unknown"):
        validate_plan_review(review)


def test_plan_question_ids_must_be_unique() -> None:
    plan = {
        "steps": [{"id": "P001"}],
        "questions": [{"id": "Q001"}, {"id": "Q001"}],
    }
    with pytest.raises(ProviderError, match="Duplicate"):
        validate_plan(plan)
