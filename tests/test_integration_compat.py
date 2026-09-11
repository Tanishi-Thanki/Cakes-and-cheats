"""Person 3 integration-hardening regression tests.

Prove that Person 1 (crawl/render) and Person 2 (evidence/consistency) audit
modules can plug into the existing architecture with zero structural changes:

1. A full Finding with all contract fields (severity, priority, confidence,
   evidence, suggested_action, source_audit, nested details) survives the chain:

       audit -> Finding -> tag_findings (aggregation) -> AuditReport -> JSON

2. Nesting Evidence/Consistency-style structured details ({entity, attribute,
   value, sources, classification}) round-trips byte-for-byte into the final
   report JSON.
3. Declarative multi-page source references ([{url, role}, ...]) serialize
   unchanged — no cross-page crawling involved.
4. An unfamiliar severity string never crashes aggregation or reporting; it is
   preserved on the finding but not counted in the summary buckets.
5. An audit returning [] is handled cleanly.
6. All raise on one optional audit yet the report, aggregation, and JSON
   serialization still succeed; audit_errors records the failure.
7. The documented registration pattern
       AUDIT_REGISTRY["name"] = lambda obs: audit_function(obs)
   runs end-to-end through the orchestrator's isolated audit slot.
"""

from __future__ import annotations

import json
import unittest

from src.audits.finding_aggregator import (
    deduplicate_findings,
    sort_findings,
    tag_findings,
)
from src.audits.models import Finding, SuggestedAction
from src.audits.reporting import build_report, report_to_json
from src.observation.observation import PageObservation


def _build_obs() -> PageObservation:
    """Minimum valid PageObservation for the isolated audit slot."""
    return PageObservation(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        is_success=True,
    )


def _evidence_finding(
    source_audit: str = "evidence_consistency",
    details: dict | None = None,
) -> Finding:
    """Realistic Evidence/Consistency-style finding (Person 2 surface)."""
    return Finding(
        id="EVID-001",
        title="Entity attribute mismatch",
        severity="high",
        evidence="Structured data and visible text disagree about the attribute.",
        suggested_action=SuggestedAction(
            summary="Reconcile the attribute across all sources.",
            priority=1,
        ),
        priority=1,
        confidence="high",
        details=(
            details
            if details is not None
            else {
                "entity": "ACME Widgets",
                "attribute": "price",
                "value": "149.99",
                "sources": [
                    {
                        "url": "https://example.com/product/acme-widgets",
                        "role": "primary",
                    },
                    {
                        "url": "https://cdn.example.com/catalog.csv",
                        "role": "corroborating",
                    },
                ],
                "classification": "MATCH",
            }
        ),
        source_audit=source_audit,
    )


def _final_report(findings: list[Finding], *, audit_errors: dict | None = None, audits_run: list[str] | None = None) -> dict:
    """Run the aggregation -> report -> JSON chain and return the parsed dict."""
    report = build_report(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        is_success=True,
        error_message=None,
        findings=findings,
        audit_errors=audit_errors or {},
        audits_run=audits_run or [],
    )
    return json.loads(report_to_json(report))


class TestFullFindingContract(unittest.TestCase):
    """A complete Finding with all contract fields survives the whole chain."""

    def test_all_contract_keys_present_in_final_json(self) -> None:
        raw = [_evidence_finding()]
        tagged = tag_findings(raw, "evidence_consistency")

        final = _final_report(tagged, audits_run=["evidence_consistency"])

        self.assertEqual(final["summary"]["total_findings"], 1)
        self.assertEqual(final["summary"]["high"], 1)
        f = final["findings"][0]
        self.assertEqual(
            sorted(f.keys()),
            sorted(
                [
                    "id",
                    "title",
                    "severity",
                    "evidence",
                    "suggested_action",
                    "priority",
                    "confidence",
                    "source_audit",
                    "details",
                ]
            ),
        )
        self.assertEqual(f["id"], "EVID-001")
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["priority"], 1)
        self.assertEqual(f["confidence"], "high")
        self.assertEqual(f["evidence"], raw[0].evidence)
        self.assertEqual(f["suggested_action"]["summary"], "Reconcile the attribute across all sources.")
        self.assertEqual(f["source_audit"], "evidence_consistency")
        # source_audit is injected by aggregation, not by the audit itself
        self.assertEqual(raw[0].source_audit, "evidence_consistency")


class TestEvidenceStyleDetailsSurvive(unittest.TestCase):
    """Nested Evidence/Consistency details round-trip into final JSON."""

    def test_nested_details_preserved_through_json(self) -> None:
        raw = [_evidence_finding()]
        tagged = tag_findings(raw, "evidence_consistency")

        final = _final_report(tagged, audits_run=["evidence_consistency"])

        details = final["findings"][0]["details"]
        self.assertEqual(details["entity"], "ACME Widgets")
        self.assertEqual(details["attribute"], "price")
        self.assertEqual(details["value"], "149.99")
        self.assertEqual(details["classification"], "MATCH")
        self.assertEqual(
            details["sources"],
            [
                {"url": "https://example.com/product/acme-widgets", "role": "primary"},
                {"url": "https://cdn.example.com/catalog.csv", "role": "corroborating"},
            ],
        )


class TestMultiPageDeclarativeReferences(unittest.TestCase):
    """Declarative cross-page references serialize without schema changes."""

    def test_page_source_references_unchanged(self) -> None:
        details = {
            "sources": [
                {"url": "https://example.com/", "role": "primary"},
                {"url": "https://docs.example.com/guide", "role": "corroborating"},
                {"url": "https://status.example.com/", "role": "context"},
            ]
        }
        tagged = tag_findings([_evidence_finding(details=details)], "evidence_consistency")

        final = _final_report(tagged, audits_run=["evidence_consistency"])

        self.assertEqual(final["findings"][0]["details"]["sources"], details["sources"])


class TestUnknownSeverityTolerance(unittest.TestCase):
    """An unfamiliar severity never breaks aggregation or reporting."""

    def test_unfamiliar_severity_survives_full_chain(self) -> None:
        f = Finding(
            id="XTRA-001",
            title="Familiar-looking rule from a future audit",
            severity="catastrophic",  # type: ignore[arg-type]
            evidence="Observed signal X.",
            suggested_action=None,
            priority=2,
            confidence="medium",
            details={},
            source_audit="future_audit",
        )
        tagged = tag_findings([f], "future_audit")

        final = _final_report(tagged, audits_run=["future_audit"])

        self.assertEqual(final["summary"]["total_findings"], 1)
        self.assertNotIn("catastrophic", final["summary"])
        self.assertEqual(final["summary"]["high"], 0)
        self.assertEqual(final["findings"][0]["severity"], "catastrophic")


class TestEmptyAuditResult(unittest.TestCase):
    """An audit returning [] flows through aggregation and reporting cleanly."""

    def test_empty_findings_from_audit(self) -> None:
        tagged = tag_findings([], "future_audit")
        self.assertEqual(tagged, [])

        final = _final_report(tagged, audits_run=["future_audit"])

        self.assertEqual(final["findings"], [])
        self.assertEqual(final["summary"]["total_findings"], 0)
        self.assertNotIn("audit_errors", final)


class TestFailureIsolation(unittest.TestCase):
    """A failing optional audit is isolated; report + JSON still succeed."""

    def test_failing_audit_does_not_block_report_or_json(self) -> None:
        from src.orchestrator import _run_audit_safe

        obs = _build_obs()
        audit_errors: dict[str, str] = {}

        def broken_future_audit(obs: PageObservation) -> list[Finding]:  # noqa: ARG001
            raise RuntimeError("boom in evidence_consistency")

        failed = _run_audit_safe(broken_future_audit, obs, "evidence_consistency", audit_errors)
        tagged = tag_findings(
            deduplicate_findings(
                sort_findings(
                    _run_audit_safe(lambda o: [_evidence_finding()], obs, "task_readiness", audit_errors)
                )
            ),
            "task_readiness",
        )

        self.assertEqual(failed, [])
        self.assertEqual(list(audit_errors.keys()), ["evidence_consistency"])
        self.assertEqual(len(tagged), 1)

        final = _final_report(
            tagged,
            audit_errors=audit_errors,
            audits_run=["task_readiness", "evidence_consistency"],
        )

        self.assertIn("audit_errors", final)
        self.assertEqual(final["audit_errors"]["evidence_consistency"], "boom in evidence_consistency")
        self.assertEqual(final["summary"]["total_findings"], 1)
        self.assertEqual(final["findings"][0]["id"], "EVID-001")
        self.assertEqual(final["findings"][0]["source_audit"], "task_readiness")


class TestRegistrationPattern(unittest.TestCase):
    """The documented lambda registration pattern works end-to-end."""

    def test_lambda_registration_runs_through_isolated_slot(self) -> None:
        from src.orchestrator import (
            AUDIT_REGISTRY,
            _ensure_optional_audits_registered,
            _run_audit_safe,
        )

        _ensure_optional_audits_registered()

        def audit_probe(obs: PageObservation) -> list[Finding]:  # noqa: ARG001
            return [_evidence_finding(source_audit="probe")]

        AUDIT_REGISTRY["probe_audit"] = lambda obs: audit_probe(obs)
        try:
            self.assertTrue(callable(AUDIT_REGISTRY["probe_audit"]))
            audit_errors: dict[str, str] = {}
            findings = _run_audit_safe(AUDIT_REGISTRY["probe_audit"], _build_obs(), "probe_audit", audit_errors)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].id, "EVID-001")
            self.assertEqual(audit_errors, {})
        finally:
            del AUDIT_REGISTRY["probe_audit"]
            self.assertNotIn("probe_audit", AUDIT_REGISTRY)


if __name__ == "__main__":
    unittest.main()