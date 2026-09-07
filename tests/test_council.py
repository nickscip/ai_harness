from __future__ import annotations

import json

import pytest

from ai_harness.council import (
    CANONICAL_ORDER,
    CORE_MEMBERS,
    Member,
    aggregate_reviews,
    apply_lead_routing,
    apply_lead_verdict,
    clean_summary,
    deterministic_members,
    empty_lead_verdict,
    empty_routing,
    findings_manifest,
    index_findings,
    member_family,
    normalize_review,
    parse_member,
    persona,
    render_council_report,
    safe_github_text,
    stage_name,
    synthesize_final,
)
from ai_harness.errors import ProviderError
from ai_harness.schema import load_schema, validate_output


def _finding(
    *, severity: str = "high", confidence: float = 0.8, title: str = "Defect", line: int = 2
) -> dict[str, object]:
    return {
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "body": "Body.",
        "evidence": [
            {
                "path": "src/app.py",
                "line": line,
                "side": "RIGHT",
                "excerpt": "    return value",
                "rationale": "Reached from the changed branch.",
            }
        ],
        "recommendation": "Fix it.",
        "suggested_verification": "Run the regression test.",
    }


def _review(reviewer: str, findings: list[dict[str, object]], **overrides) -> dict[str, object]:
    value = {
        "reviewer": reviewer,
        "verdict": "comment" if findings else "pass",
        "summary": "Reviewed.",
        "scope_reviewed": ["src/app.py"],
        "residual_risks": [],
        "findings": findings,
    }
    value.update(overrides)
    return value


def _routing(requests: list[dict[str, object]], focus: list[str] | None = None):
    return {
        "specialist_requests": requests,
        "focus_reviewers": focus or [],
        "summary": "Routed.",
    }


def test_every_member_has_a_charter_and_a_schema_slot() -> None:
    enum_values = [member.value for member in CANONICAL_ORDER]
    assert load_schema("specialist_review")["properties"]["reviewer"]["enum"] == enum_values
    routable = load_schema("review_routing")["properties"]["specialist_requests"]["items"]
    assert routable["properties"]["reviewer"]["enum"] == [
        member.value for member in CANONICAL_ORDER if member not in CORE_MEMBERS
    ]
    for member in CANONICAL_ORDER:
        assert persona(member.value).startswith("You are ")
    assert persona("reviewer-common").startswith("# Specialist review contract")
    assert persona("review_lead").startswith("You are the review lead.")


def test_council_spans_both_families_and_names_stages_stably() -> None:
    families = {member: member_family(member, "claude") for member in CANONICAL_ORDER}
    assert set(families.values()) == {"claude", "codex"}
    assert families[Member.CORRECTNESS] == "codex"
    assert families[Member.REFACTOR] == "claude"
    assert member_family(Member.CORRECTNESS, "codex") == "claude"
    assert stage_name(Member.SECURITY) == "review-security_reviewer"
    with pytest.raises(ProviderError, match="Unknown council member"):
        parse_member("lending_expert")


def test_deterministic_floor_is_two_members_plus_a_narrow_security_trigger() -> None:
    assert deterministic_members({"README.md"}) == CORE_MEMBERS
    assert Member.SECURITY in deterministic_members({".github/workflows/ci.yml"})
    assert Member.SECURITY in deterministic_members({"app/Auth/Session.py"})
    assert Member.SECURITY in deterministic_members({"src/token_store.py"})
    assert Member.PERFORMANCE not in deterministic_members({"src/pool.py"})


def test_lead_may_add_members_but_never_remove_or_fabricate_them() -> None:
    changed = {"src/state.py", "src/app.py"}
    members, focus = apply_lead_routing(
        _routing(
            [
                {
                    "reviewer": "resumability_reviewer",
                    "evidence_paths": ["src/state.py"],
                    "rationale": "Resume keys change.",
                }
            ],
            focus=["correctness_reviewer", "security_reviewer"],
        ),
        deterministic=CORE_MEMBERS,
        changed_paths=changed,
    )
    assert members == (*CORE_MEMBERS, Member.RESUMABILITY)
    # A focus assignment for a member that is not on the council is dropped, not honored.
    assert focus == frozenset({Member.CORRECTNESS})

    with pytest.raises(ProviderError, match="always-on reviewer refactorer"):
        apply_lead_routing(
            _routing(
                [
                    {
                        "reviewer": "refactorer",
                        "evidence_paths": ["src/app.py"],
                        "rationale": "x",
                    }
                ]
            ),
            deterministic=CORE_MEMBERS,
            changed_paths=changed,
        )
    with pytest.raises(ProviderError, match="redundantly requested security_reviewer"):
        apply_lead_routing(
            _routing(
                [
                    {
                        "reviewer": "security_reviewer",
                        "evidence_paths": ["src/app.py"],
                        "rationale": "x",
                    }
                ]
            ),
            deterministic=(*CORE_MEMBERS, Member.SECURITY),
            changed_paths=changed,
        )
    request = {
        "reviewer": "contract_reviewer",
        "evidence_paths": ["src/app.py"],
        "rationale": "x",
    }
    with pytest.raises(ProviderError, match="more than once"):
        apply_lead_routing(
            _routing([request, dict(request)]),
            deterministic=CORE_MEMBERS,
            changed_paths=changed,
        )
    with pytest.raises(ProviderError, match=r"unchanged evidence paths: \['docs/nope.md'\]"):
        apply_lead_routing(
            _routing([{**request, "evidence_paths": ["docs/nope.md"]}]),
            deterministic=CORE_MEMBERS,
            changed_paths=changed,
        )


def test_explicit_selection_records_a_routing_artifact_without_a_model() -> None:
    routing = empty_routing(CORE_MEMBERS, "Council selected by the caller")
    validate_output("review_routing", routing)
    assert routing["specialist_requests"] == []
    assert "correctness_reviewer" in routing["summary"]


def test_verdict_repairs_are_applied_before_the_invariants() -> None:
    # comment with nothing to say becomes pass; pass with a residual risk becomes abstain.
    assert normalize_review(Member.REFACTOR, _review("refactorer", []))["verdict"] == "pass"
    empty_comment = _review("refactorer", [], verdict="comment")
    assert normalize_review(Member.REFACTOR, empty_comment)["verdict"] == "pass"
    risky = _review("refactorer", [], verdict="pass", residual_risks=["Unclear ownership."])
    assert normalize_review(Member.REFACTOR, risky)["verdict"] == "abstain"

    with pytest.raises(ProviderError, match="verdict pass with findings"):
        normalize_review(Member.REFACTOR, _review("refactorer", [_finding()], verdict="pass"))

    blocker = _review("refactorer", [_finding(severity="blocker")])
    with pytest.raises(ProviderError, match="blocker without verdict block"):
        normalize_review(Member.REFACTOR, blocker)
    with pytest.raises(ProviderError, match="verdict block without a blocker"):
        normalize_review(
            Member.REFACTOR, _review("refactorer", [_finding()], verdict="block")
        )
    with pytest.raises(ProviderError, match="verdict abstain with findings"):
        normalize_review(
            Member.REFACTOR, _review("refactorer", [_finding()], verdict="abstain")
        )
    with pytest.raises(ProviderError, match="returned reviewer 'refactorer'"):
        normalize_review(Member.SECURITY, _review("refactorer", []))


def test_aggregation_fails_closed_on_a_missing_or_extra_specialist() -> None:
    reviews = {Member.CORRECTNESS: _review("correctness_reviewer", [])}
    with pytest.raises(ProviderError, match=r"Missing specialist reviews: \['refactorer'\]"):
        aggregate_reviews(CORE_MEMBERS, reviews)
    with pytest.raises(ProviderError, match=r"Unexpected specialist reviews: \['refactorer'\]"):
        aggregate_reviews(
            (Member.CORRECTNESS,),
            {**reviews, Member.REFACTOR: _review("refactorer", [])},
        )


def _indexed() -> dict[str, dict[str, object]]:
    reviews = {
        Member.CORRECTNESS: _review(
            "correctness_reviewer",
            [_finding(title="Root cause", confidence=0.6), _finding(severity="low")],
        ),
        Member.REFACTOR: _review("refactorer", [_finding(severity="medium", confidence=0.95)]),
    }
    return index_findings(CORE_MEMBERS, reviews)


def test_source_ids_are_positional_and_the_manifest_marks_them_untrusted() -> None:
    indexed = _indexed()
    assert sorted(indexed) == [
        "correctness_reviewer:1",
        "correctness_reviewer:2",
        "refactorer:1",
    ]
    assert indexed["refactorer:1"]["reviewer"] == "refactorer"
    manifest = json.loads(findings_manifest(indexed))
    assert "untrusted claim" in manifest["instructions"]
    assert len(manifest["findings"]) == 3


def test_lead_must_dispose_of_every_source_exactly_once() -> None:
    indexed = _indexed()
    with pytest.raises(ProviderError, match="assigned correctness_reviewer:1 more than once"):
        apply_lead_verdict(
            {
                "accepted_groups": [
                    {"source_ids": ["correctness_reviewer:1"], "rationale": "a"},
                    {"source_ids": ["correctness_reviewer:1"], "rationale": "b"},
                ],
                "dismissed_groups": [],
                "summary": "s",
            },
            indexed,
        )
    with pytest.raises(ProviderError, match=r"unknown findings: \['refactorer:4'\]"):
        apply_lead_verdict(
            {
                "accepted_groups": [{"source_ids": ["refactorer:4"], "rationale": "a"}],
                "dismissed_groups": [],
                "summary": "s",
            },
            indexed,
        )
    with pytest.raises(ProviderError, match="omitted findings"):
        apply_lead_verdict(
            {
                "accepted_groups": [{"source_ids": ["correctness_reviewer:1"], "rationale": "a"}],
                "dismissed_groups": [],
                "summary": "s",
            },
            indexed,
        )
    with pytest.raises(ProviderError, match="low severity"):
        apply_lead_verdict(
            {
                "accepted_groups": [{"source_ids": ["correctness_reviewer:2"], "rationale": "a"}],
                "dismissed_groups": [
                    {
                        "source_ids": ["correctness_reviewer:1", "refactorer:1"],
                        "reason": "yagni",
                        "rationale": "b",
                    }
                ],
                "summary": "s",
            },
            indexed,
        )


def test_grouping_keeps_the_strongest_source_and_orders_by_severity() -> None:
    indexed = _indexed()
    lead = {
        "accepted_groups": [
            {
                "source_ids": ["refactorer:1"],
                "rationale": "Structural duplicate.",
            },
            {
                "source_ids": ["correctness_reviewer:1", "correctness_reviewer:2"],
                "rationale": "One root cause reported twice.",
            },
        ],
        "dismissed_groups": [],
        "summary": "Two root causes.",
    }
    groups = apply_lead_verdict(lead, indexed)
    # High outranks medium regardless of the order the lead listed the groups in.
    assert [group.canonical["severity"] for group in groups] == ["high", "medium"]
    # The `low` sibling is grouped in but the canonical source is the `high` one.
    assert groups[0].canonical["source_id"] == "correctness_reviewer:1"
    assert groups[0].confidence == pytest.approx(0.8)

    final = validate_output("pr_review", synthesize_final(groups, summary=lead["summary"]))
    assert [finding["id"] for finding in final["findings"]] == ["F001", "F002"]
    assert final["findings"][0]["severity"] == "high"
    assert "Found by `correctness_reviewer:1`, `correctness_reviewer:2`" in (
        final["findings"][0]["body"]
    )
    assert "Run the regression test." in final["findings"][0]["body"]


def test_published_text_is_neutered_but_evidence_excerpts_are_left_exact() -> None:
    assert safe_github_text("ping @org/team") != "ping @org/team"
    assert "@" not in safe_github_text("@here").replace("@​", "")
    finding = _finding()
    finding["title"] = "@org/team owns this"
    finding["evidence"][0]["excerpt"] = "    call(@decorator)"
    finding["evidence"][0]["rationale"] = "cc @org/team"
    indexed = index_findings(
        (Member.REFACTOR,), {Member.REFACTOR: _review("refactorer", [finding])}
    )
    groups = apply_lead_verdict(
        {
            "accepted_groups": [{"source_ids": ["refactorer:1"], "rationale": "@org/team"}],
            "dismissed_groups": [],
            "summary": "@org/team",
        },
        indexed,
    )
    final = synthesize_final(groups, summary="@org/team")
    evidence = final["findings"][0]["evidence"][0]
    assert evidence["excerpt"] == "    call(@decorator)"
    assert "@​" in evidence["rationale"]
    assert "@​" in final["findings"][0]["title"]
    assert "@​" in final["summary"]


def test_clean_council_summary_carries_residual_risks_when_no_lead_runs() -> None:
    reviews = {
        Member.CORRECTNESS: _review("correctness_reviewer", []),
        Member.REFACTOR: _review(
            "refactorer", [], verdict="abstain", residual_risks=["Untested migration path."]
        ),
    }
    summary = clean_summary(CORE_MEMBERS, reviews)
    assert "no findings" in summary
    assert "refactorer: Untested migration path." in summary
    lead = empty_lead_verdict(summary)
    validate_output("review_lead", lead)
    assert apply_lead_verdict(lead, {}) == []


def test_council_report_records_dismissals_that_never_reach_the_pull_request() -> None:
    indexed = _indexed()
    lead = {
        "accepted_groups": [{"source_ids": ["correctness_reviewer:1"], "rationale": "Real."}],
        "dismissed_groups": [
            {
                "source_ids": ["correctness_reviewer:2", "refactorer:1"],
                "reason": "yagni",
                "rationale": "No current requirement.",
            }
        ],
        "summary": "One root cause.",
    }
    groups = apply_lead_verdict(lead, indexed)
    reviews = {
        Member.CORRECTNESS: _review(
            "correctness_reviewer",
            [_finding(title="Root cause", confidence=0.6), _finding(severity="low")],
        ),
        Member.REFACTOR: _review(
            "refactorer",
            [_finding(severity="medium", confidence=0.95)],
            residual_risks=["Unclear ownership."],
        ),
    }
    report = render_council_report(
        title="Change app.py",
        base="b" * 40,
        head="h" * 40,
        expected=CORE_MEMBERS,
        reviews=reviews,
        routing=_routing([]),
        deterministic=CORE_MEMBERS,
        lead=lead,
        groups=groups,
        indexed=indexed,
    )
    assert "**yagni** (correctness_reviewer:2, refactorer:1)" in report
    assert "residual risk: Unclear ownership." in report
    assert "**F001 [high]** `src/app.py:2` (RIGHT)" in report

    empty = render_council_report(
        title="Clean",
        base="b" * 40,
        head="h" * 40,
        expected=CORE_MEMBERS,
        reviews={member: _review(member.value, []) for member in CORE_MEMBERS},
        routing=_routing([]),
        deterministic=CORE_MEMBERS,
        lead=empty_lead_verdict("Nothing found."),
        groups=[],
        indexed={},
    )
    assert empty.count("- none") == 2
