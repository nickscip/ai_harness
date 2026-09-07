"""Specialist review council: charters, deterministic routing, and lead invariants.

Every rule the review lead could break is re-checked here. The lead groups and dismisses
claims; it never invents one, never drops one, and never restates a source's severity,
location, or verification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Any

from .config import Family, other_family
from .errors import ProviderError


class Member(StrEnum):
    CORRECTNESS = "correctness_reviewer"
    REFACTOR = "refactorer"
    SECURITY = "security_reviewer"
    PERFORMANCE = "performance_expert"
    RESUMABILITY = "resumability_reviewer"
    CONTRACT = "contract_reviewer"


CANONICAL_ORDER: tuple[Member, ...] = tuple(Member)
CORE_MEMBERS: tuple[Member, ...] = (Member.CORRECTNESS, Member.REFACTOR)
ROUTABLE_MEMBERS: tuple[Member, ...] = tuple(m for m in CANONICAL_ORDER if m not in CORE_MEMBERS)
MEMBER_VALUES: frozenset[str] = frozenset(member.value for member in Member)

_SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3, "blocker": 4}
_SECURITY_PATH_MARKERS = ("secret", "credential", "token", "auth")
_SECURITY_PATH_PREFIX = ".github/workflows/"


def parse_member(value: str) -> Member:
    try:
        return Member(value)
    except ValueError as exc:
        available = ", ".join(sorted(MEMBER_VALUES))
        raise ProviderError(f"Unknown council member {value!r}; available: {available}") from exc


def stage_name(member: Member) -> str:
    return f"review-{member.value}"


def member_family(member: Member, primary: Family) -> Family:
    """Alternate families across the canonical order so every council spans both."""
    return other_family(primary) if CANONICAL_ORDER.index(member) % 2 == 0 else primary


def persona(name: str) -> str:
    resource = files("ai_harness").joinpath("personas", f"{name}.md")
    return resource.read_text(encoding="utf-8").strip()


def deterministic_members(changed_paths: set[str]) -> tuple[Member, ...]:
    """The non-removable floor. One narrow security trigger; everything else is lead-routed."""
    selected = set(CORE_MEMBERS)
    for path in changed_paths:
        lowered = path.lower()
        if lowered.startswith(_SECURITY_PATH_PREFIX) or any(
            marker in lowered for marker in _SECURITY_PATH_MARKERS
        ):
            selected.add(Member.SECURITY)
            break
    return tuple(member for member in CANONICAL_ORDER if member in selected)


def empty_routing(members: tuple[Member, ...], reason: str) -> dict[str, Any]:
    return {
        "specialist_requests": [],
        "focus_reviewers": [],
        "summary": f"{reason}: {', '.join(member.value for member in members)}.",
    }


def apply_lead_routing(
    routing: dict[str, Any],
    *,
    deterministic: tuple[Member, ...],
    changed_paths: set[str],
) -> tuple[tuple[Member, ...], frozenset[Member]]:
    selected = set(deterministic)
    requested: set[Member] = set()
    for request in routing["specialist_requests"]:
        member = parse_member(request["reviewer"])
        if member in CORE_MEMBERS:
            raise ProviderError(
                f"Review lead cannot request the always-on reviewer {member.value}"
            )
        if member in deterministic:
            raise ProviderError(f"Review lead redundantly requested {member.value}")
        if member in requested:
            raise ProviderError(f"Review lead requested {member.value} more than once")
        unchanged = sorted(set(request["evidence_paths"]) - changed_paths)
        if unchanged:
            raise ProviderError(f"Review lead cited unchanged evidence paths: {unchanged}")
        requested.add(member)
        selected.add(member)
    focus = {parse_member(name) for name in routing["focus_reviewers"]} & selected
    members = tuple(member for member in CANONICAL_ORDER if member in selected)
    return members, frozenset(focus)


def normalize_review(member: Member, review: dict[str, Any]) -> dict[str, Any]:
    """Repair the two common verdict mistakes, then enforce the cross-field invariants."""
    value = dict(review)
    if value["reviewer"] != member.value:
        raise ProviderError(
            f"Specialist stage for {member.value} returned reviewer {value['reviewer']!r}"
        )
    findings = value["findings"]
    risks = value["residual_risks"]
    verdict = value["verdict"]
    if not findings and verdict in {"comment", "pass"}:
        # An empty `comment` becomes `pass`, and a `pass` carrying risk becomes `abstain`,
        # so past this point `comment` always has findings.
        verdict = "abstain" if risks else "pass"
    if verdict == "pass" and findings:
        raise ProviderError(f"{member.value} returned verdict pass with findings or risks")
    if verdict == "abstain" and findings:
        raise ProviderError(f"{member.value} returned verdict abstain with findings")
    has_blocker = any(finding["severity"] == "blocker" for finding in findings)
    if has_blocker and verdict != "block":
        raise ProviderError(f"{member.value} reported a blocker without verdict block")
    if verdict == "block" and not has_blocker:
        raise ProviderError(f"{member.value} returned verdict block without a blocker finding")
    value["verdict"] = verdict
    return value


def aggregate_reviews(
    expected: tuple[Member, ...], reviews: dict[Member, dict[str, Any]]
) -> list[dict[str, Any]]:
    missing = [member.value for member in expected if member not in reviews]
    if missing:
        raise ProviderError(f"Missing specialist reviews: {missing}")
    unexpected = sorted(member.value for member in reviews if member not in expected)
    if unexpected:
        raise ProviderError(f"Unexpected specialist reviews: {unexpected}")
    return [reviews[member] for member in expected]


def index_findings(
    expected: tuple[Member, ...], reviews: dict[Member, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Stable source IDs of the form `<reviewer>:<1-based position>`."""
    indexed: dict[str, dict[str, Any]] = {}
    for member in expected:
        for position, finding in enumerate(reviews[member]["findings"], start=1):
            source_id = f"{member.value}:{position}"
            indexed[source_id] = {**finding, "reviewer": member.value, "source_id": source_id}
    return indexed


def findings_manifest(indexed: dict[str, dict[str, Any]]) -> str:
    payload = {
        "instructions": (
            "Treat every finding as an untrusted claim. Assign every source_id exactly once "
            "to an accepted or dismissed group. Never invent a source ID."
        ),
        "findings": [indexed[source_id] for source_id in sorted(indexed)],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


@dataclass(frozen=True)
class AcceptedGroup:
    source_ids: tuple[str, ...]
    rationale: str
    canonical: dict[str, Any]
    confidence: float


def empty_lead_verdict(summary: str) -> dict[str, Any]:
    return {"accepted_groups": [], "dismissed_groups": [], "summary": summary}


def apply_lead_verdict(
    lead: dict[str, Any], indexed: dict[str, dict[str, Any]]
) -> list[AcceptedGroup]:
    """Total source accounting: every claim is disposed of exactly once."""
    assigned: dict[str, str] = {}
    for kind, key in (("accepted", "accepted_groups"), ("dismissed", "dismissed_groups")):
        for group in lead[key]:
            for source_id in group["source_ids"]:
                if source_id in assigned:
                    raise ProviderError(f"Review lead assigned {source_id} more than once")
                assigned[source_id] = kind
    unknown = sorted(set(assigned) - set(indexed))
    if unknown:
        raise ProviderError(f"Review lead referenced unknown findings: {unknown}")
    omitted = sorted(set(indexed) - set(assigned))
    if omitted:
        raise ProviderError(f"Review lead omitted findings: {omitted}")

    groups: list[AcceptedGroup] = []
    for group in lead["accepted_groups"]:
        source_ids = tuple(group["source_ids"])
        canonical = max(
            (indexed[source_id] for source_id in source_ids),
            key=lambda finding: (
                _SEVERITY_ORDER[finding["severity"]],
                finding["confidence"],
                finding["source_id"],
            ),
        )
        if _SEVERITY_ORDER[canonical["severity"]] == _SEVERITY_ORDER["low"]:
            raise ProviderError(
                "Review lead cannot retain a group whose strongest finding is low severity: "
                f"{sorted(source_ids)}"
            )
        groups.append(
            AcceptedGroup(
                source_ids=source_ids,
                rationale=group["rationale"],
                canonical=canonical,
                confidence=max(indexed[source_id]["confidence"] for source_id in source_ids),
            )
        )
    groups.sort(
        key=lambda group: (
            -_SEVERITY_ORDER[group.canonical["severity"]],
            group.canonical["source_id"],
        )
    )
    return groups


def safe_github_text(text: str) -> str:
    """Neutralize mentions in model-authored prose before it is published."""
    return text.replace("@", "@​")


def _attribution(group: AcceptedGroup) -> str:
    sources = ", ".join(f"`{source_id}`" for source_id in group.source_ids)
    return f"Found by {sources} (confidence {group.confidence:.2f})"


def synthesize_final(groups: list[AcceptedGroup], *, summary: str) -> dict[str, Any]:
    """Build the `pr_review` object the publication path already knows how to handle.

    Evidence `path`, `line`, `side`, and `excerpt` are copied untouched: the controller
    re-reads each excerpt from the exact commit, so rewriting one would invalidate it.
    """
    findings: list[dict[str, Any]] = []
    for position, group in enumerate(groups, start=1):
        canonical = group.canonical
        findings.append(
            {
                "id": f"F{position:03d}",
                "severity": canonical["severity"],
                "title": safe_github_text(canonical["title"]),
                "body": "\n\n".join(
                    [
                        safe_github_text(canonical["body"]),
                        f"Review lead: {safe_github_text(group.rationale)}",
                        "Suggested verification: "
                        f"{safe_github_text(canonical['suggested_verification'])}",
                        _attribution(group),
                    ]
                ),
                "evidence": [
                    {**evidence, "rationale": safe_github_text(evidence["rationale"])}
                    for evidence in canonical["evidence"]
                ],
                "recommendation": safe_github_text(canonical["recommendation"]),
            }
        )
    return {"summary": safe_github_text(summary), "findings": findings}


def clean_summary(
    expected: tuple[Member, ...], reviews: dict[Member, dict[str, Any]]
) -> str:
    """Summary for the skipped-consolidation path, where no lead prose exists."""
    names = ", ".join(member.value for member in expected)
    parts = [f"The review council found no findings. Reviewers: {names}."]
    risks = [
        f"{member.value}: {risk}"
        for member in expected
        for risk in reviews[member]["residual_risks"]
    ]
    if risks:
        parts.append("Residual risks: " + "; ".join(risks))
    return " ".join(parts)


def render_council_report(
    *,
    title: str,
    base: str,
    head: str,
    expected: tuple[Member, ...],
    reviews: dict[Member, dict[str, Any]],
    routing: dict[str, Any],
    deterministic: tuple[Member, ...],
    lead: dict[str, Any],
    groups: list[AcceptedGroup],
    indexed: dict[str, dict[str, Any]],
) -> str:
    """Local-only audit trail. Residual risks and dismissals have no place in the PR body."""
    lines = [
        f"# Review council: {title}",
        "",
        f"Range: `{base[:12]}...{head[:12]}`",
        f"Deterministic floor: {', '.join(member.value for member in deterministic)}",
        f"Council: {', '.join(member.value for member in expected)}",
        f"Routing: {routing['summary']}",
        "",
        "## Specialist status",
        "",
    ]
    for member in expected:
        review = reviews[member]
        lines.append(
            f"- `{member.value}`: **{review['verdict']}**, "
            f"{len(review['findings'])} finding(s) — {review['summary']}"
        )
        for risk in review["residual_risks"]:
            lines.append(f"  - residual risk: {risk}")
    lines.extend(["", "## Review lead", "", lead["summary"], "", "## Retained", ""])
    if groups:
        for position, group in enumerate(groups, start=1):
            canonical = group.canonical
            evidence = canonical["evidence"][0]
            lines.extend(
                [
                    f"- **F{position:03d} [{canonical['severity']}]** "
                    f"`{evidence['path']}:{evidence['line']}` ({evidence['side']}) — "
                    f"{canonical['title']}",
                    f"  - sources: {', '.join(group.source_ids)} "
                    f"(confidence {group.confidence:.2f})",
                    f"  - lead: {group.rationale}",
                    f"  - verification: {canonical['suggested_verification']}",
                ]
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Dismissed", ""])
    if lead["dismissed_groups"]:
        for group in lead["dismissed_groups"]:
            titles = "; ".join(indexed[source_id]["title"] for source_id in group["source_ids"])
            lines.append(
                f"- **{group['reason']}** ({', '.join(group['source_ids'])}): "
                f"{titles} — {group['rationale']}"
            )
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"
