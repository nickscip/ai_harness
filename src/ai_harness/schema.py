from __future__ import annotations

import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import jsonschema

from .errors import ProviderError


def load_schema(name: str) -> dict[str, Any]:
    resource = files("ai_harness").joinpath("schemas", f"{name}.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def schema_path(name: str) -> Path:
    resource = files("ai_harness").joinpath("schemas", f"{name}.json")
    return Path(str(resource))


def claude_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt the canonical schema to Claude's currently accepted dialect."""
    adapted = copy.deepcopy(schema)
    adapted.pop("$schema", None)
    return adapted


def validate_output(name: str, value: Any) -> dict[str, Any]:
    schema = load_schema(name)
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path) or "<root>"
        raise ProviderError(f"Invalid {name} output at {location}: {exc.message}") from exc
    if not isinstance(value, dict):
        raise ProviderError(f"Invalid {name} output: expected an object")
    return value


def require_unique_ids(value: dict[str, Any], collection: str = "findings") -> None:
    items = value.get(collection, [])
    identifiers = [item["id"] for item in items]
    if len(identifiers) != len(set(identifiers)):
        raise ProviderError(f"Duplicate stable IDs in {collection}")


def validate_plan(plan: dict[str, Any]) -> None:
    require_unique_ids(plan, "steps")
    require_unique_ids(plan, "questions")


def validate_plan_review(review: dict[str, Any]) -> None:
    require_unique_ids(review)
    finding_ids = {item["id"] for item in review["findings"]}
    required = review["required_changes"]
    if len(required) != len(set(required)):
        raise ProviderError("Duplicate IDs in required_changes")
    unknown = set(required) - finding_ids
    if unknown:
        raise ProviderError(f"required_changes refers to unknown findings: {sorted(unknown)}")


def validate_revised_plan(review: dict[str, Any], revised: dict[str, Any]) -> None:
    finding_ids = {item["id"] for item in review["findings"]}
    resolutions = revised["review_resolutions"]
    resolved_ids = [item["finding_id"] for item in resolutions]
    if len(resolved_ids) != len(set(resolved_ids)):
        raise ProviderError("Duplicate finding dispositions in revised plan")
    if set(resolved_ids) != finding_ids:
        missing = sorted(finding_ids - set(resolved_ids))
        extra = sorted(set(resolved_ids) - finding_ids)
        raise ProviderError(
            f"Revised plan dispositions are incomplete (missing={missing}, extra={extra})"
        )
    step_ids = {item["id"] for item in revised["steps"]}
    if len(step_ids) != len(revised["steps"]):
        raise ProviderError("Duplicate step IDs in revised plan")
    for resolution in resolutions:
        unknown = set(resolution["affected_steps"]) - step_ids
        if unknown:
            raise ProviderError(
                f"Resolution {resolution['finding_id']} refers to unknown steps: {sorted(unknown)}"
            )
