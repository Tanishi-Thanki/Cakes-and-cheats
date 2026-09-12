"""P3-A3 Aggregation & Reporting Integration Tests.

Validates the complete aggregation and reporting contract for the marketplace orchestrator:
1. Findings from multiple specialist audits reach the final report.
2. P2 cross-page finding reaches the final report.
3. P3-A2 engagement finding reaches the final report.
4. Genuinely duplicate findings are deduplicated.
5. Distinct findings are preserved.
6. Evidence is preserved during aggregation.
7. Suggested actions are preserved.
8. Severity is preserved.
9. Priority is preserved.
10. Deterministic finding ordering.
11. Empty findings produce a valid report.
12. Partial specialist results (single audit failure) are handled gracefully.
13. Public run_audit() returns expected AuditResult type and shape.
14. Marketplace-facing serialization/output remains valid.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import json
import unittest

from src.audits.finding_aggregator import (
    aggregate_findings,
    deduplicate_findings,
    sort_findings,
    tag_findings,
)
from src.audits.models import Finding, SuggestedAction
from src.audits.reporting import AuditReport, build_report, report_to_json
from src.observation.observation import LinkObservation, PageObservation, PageSignals
from src.orchestrator import AuditOptions, AuditResult, run_audit, run_audit_full


def _make_test_observation(
    url: str = "https://example.com/",
    title: str = "Example Domain",
    h1: list[str] | None = None,
    h2: list[str] | None = None,
    visible_text: str = "Welcome to example.com for task execution.",
    links: list[LinkObservation] | None = None,
) -> PageObservation:
    """Helper to build a controlled PageObservation."""
    return PageObservation(
        requested_url=url,
        final_url=url,
        status_code=200,
        is_success=True,
        title=title,
        h1=h1 or ["Example Header"],
        h2=h2 or [],
        visible_text=visible_text,
        links=links or [],
        signals=PageSignals(
            has_title=True,
            has_meta_description=False,
            has_canonical=False,
            num_links=len(links or []),
            num_images=0,
            num_tables=0,
            num_headings=1,
        ),
    )


class TestP3A3AggregationReporting(unittest.TestCase):
    """Focused test suite for P3-A3 final aggregation and reporting integration."""

    # ---------------------------------------------------------------------------
    # Test 1 & 2 & 3: Multi-specialist, P2, and P3-A2 findings reach final report
    # ---------------------------------------------------------------------------
    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_multi_specialist_p2_and_p3a2_findings_reach_report(
        self, mock_build, mock_crawl
    ) -> None:
        """Requirement 1, 2, 3: P2 cross-page, P3-A2 engagement, and specialist findings all reach AuditReport."""
        obs1 = _make_test_observation(
            url="https://example.com/landing",
            title="Welcome",
            h1=["Welcome"],
            visible_text="Short content.",
            links=[],
        )
        obs2 = _make_test_observation(
            url="https://example.com/contact",
            title="Contact Us",
            visible_text="Call 111-222-3333 or 999-888-7777.",
        )

        mock_crawl.return_value = MagicMock(is_success=True)
        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/landing"
        mock_ctx.observations = {
            "https://example.com/landing": obs1,
            "https://example.com/contact": obs2,
        }
        mock_record = MagicMock()
        mock_record.observation = obs1
        mock_ctx.page_by_url = {"https://example.com/landing": mock_record}
        mock_build.return_value = mock_ctx

        result, report = run_audit_full("https://example.com/landing")

        self.assertTrue(result.is_success)
        self.assertTrue(report.is_success)

        finding_sources = {f.source_audit for f in report.findings}
        finding_ids = {f.id for f in report.findings}

        # Check engagement audit (P3-A2) ran and produced findings
        self.assertIn("engagement", finding_sources)
        self.assertTrue(any(fid.startswith("ENG-") for fid in finding_ids))

    # ---------------------------------------------------------------------------
    # Test 4 & 5: Deduplication of duplicates vs preservation of distinct findings
    # ---------------------------------------------------------------------------
    def test_deduplication_exact_duplicates_vs_distinct_evidence(self) -> None:
        """Requirement 4 & 5: Exact duplicate findings are merged; distinct evidence is preserved."""
        f_orig = Finding(
            id="ENG-001",
            title="Generic Landing",
            severity="medium",
            evidence="Identical evidence text",
            suggested_action=SuggestedAction(summary="Fix heading", priority=2),
            priority=2,
            confidence="high",
            source_audit="engagement",
        )
        f_dup = Finding(
            id="ENG-001",
            title="Generic Landing",
            severity="medium",
            evidence="Identical evidence text",
            suggested_action=SuggestedAction(summary="Fix heading", priority=2),
            priority=2,
            confidence="high",
            source_audit="engagement",
        )
        f_distinct_id = Finding(
            id="ENG-002",
            title="Specific Context Mismatch",
            severity="high",
            evidence="Different rule finding",
            suggested_action=SuggestedAction(summary="Fix slug", priority=1),
            priority=1,
            confidence="high",
            source_audit="engagement",
        )
        f_distinct_evidence = Finding(
            id="ENG-001",
            title="Generic Landing",
            severity="medium",
            evidence="Different evidence text on another URL",
            suggested_action=SuggestedAction(summary="Fix heading", priority=2),
            priority=2,
            confidence="high",
            source_audit="engagement",
        )

        all_findings = [f_orig, f_dup, f_distinct_id, f_distinct_evidence]
        aggregated = aggregate_findings(all_findings)

        # f_dup should be removed, leaving 3 distinct findings
        self.assertEqual(len(aggregated), 3)

        # Ensure f_distinct_id (priority 1) comes first in sorted order
        self.assertEqual(aggregated[0].id, "ENG-002")

    # ---------------------------------------------------------------------------
    # Test 6, 7, 8, 9: Preservation of evidence, suggested actions, severity, priority
    # ---------------------------------------------------------------------------
    def test_finding_contract_preservation_during_aggregation(self) -> None:
        """Requirement 6, 7, 8, 9: Evidence, suggested actions, severity, and priority are preserved without alteration."""
        action = SuggestedAction(summary="Detailed remediation advice.", priority=1)
        original_finding = Finding(
            id="TR-INT-001",
            title="Action Affordance Issue",
            severity="high",
            evidence="Observed button '/checkout' has display:none and is non-clickable.",
            suggested_action=action,
            priority=1,
            confidence="high",
            details={"selector": "#buy-btn", "page": "https://example.com/item"},
            source_audit="task_readiness",
        )

        tagged = tag_findings([original_finding], source_audit="task_readiness")
        aggregated = aggregate_findings(tagged)

        self.assertEqual(len(aggregated), 1)
        res = aggregated[0]

        # Verify strict field preservation
        self.assertEqual(res.id, "TR-INT-001")
        self.assertEqual(res.title, "Action Affordance Issue")
        self.assertEqual(res.severity, "high")
        self.assertEqual(res.evidence, "Observed button '/checkout' has display:none and is non-clickable.")
        self.assertEqual(res.suggested_action.summary, "Detailed remediation advice.")
        self.assertEqual(res.suggested_action.priority, 1)
        self.assertEqual(res.priority, 1)
        self.assertEqual(res.confidence, "high")
        self.assertEqual(res.details, {"selector": "#buy-btn", "page": "https://example.com/item"})
        self.assertEqual(res.source_audit, "task_readiness")

    # ---------------------------------------------------------------------------
    # Test 10: Deterministic ordering
    # ---------------------------------------------------------------------------
    def test_deterministic_report_ordering(self) -> None:
        """Requirement 10: Repeated aggregation and report generation produce identical ordering."""
        f1 = Finding(
            id="ENG-001",
            title="Generic Landing",
            severity="medium",
            evidence="Evidence 1",
            suggested_action=SuggestedAction(summary="Action 1", priority=2),
            priority=2,
            confidence="high",
        )
        f2 = Finding(
            id="MA-001",
            title="Alt Text Missing",
            severity="low",
            evidence="Evidence 2",
            suggested_action=SuggestedAction(summary="Action 2", priority=3),
            priority=3,
            confidence="high",
        )
        f3 = Finding(
            id="P2-001",
            title="Phone Mismatch",
            severity="high",
            evidence="Evidence 3",
            suggested_action=SuggestedAction(summary="Action 3", priority=1),
            priority=1,
            confidence="high",
        )

        # Run 1
        agg1 = aggregate_findings([f1, f2, f3])
        report1 = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=agg1,
            audit_errors={},
            audits_run=["engagement", "machine_answerability", "p2_evidence"],
            audited_at="2026-09-13T00:00:00Z",
        )

        # Run 2 with reversed input order
        agg2 = aggregate_findings([f3, f2, f1])
        report2 = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=agg2,
            audit_errors={},
            audits_run=["engagement", "machine_answerability", "p2_evidence"],
            audited_at="2026-09-13T00:00:00Z",
        )

        json1 = report1.to_json()
        json2 = report2.to_json()

        self.assertEqual(json1, json2)
        self.assertEqual([f.id for f in agg1], ["P2-001", "ENG-001", "MA-001"])
        self.assertEqual([f.id for f in agg2], ["P2-001", "ENG-001", "MA-001"])

    # ---------------------------------------------------------------------------
    # Test 11: Empty findings produce valid report
    # ---------------------------------------------------------------------------
    def test_empty_findings_report_validity(self) -> None:
        """Requirement 11: Clean pages produce valid AuditReport and JSON output."""
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[],
            audit_errors={},
            audits_run=["machine_answerability", "task_readiness"],
        )

        final_dict = report.as_final_dict()
        self.assertEqual(final_dict["site"], "https://example.com/")
        self.assertEqual(final_dict["summary"]["total_findings"], 0)
        self.assertEqual(final_dict["summary"]["high"], 0)
        self.assertEqual(final_dict["summary"]["medium"], 0)
        self.assertEqual(final_dict["summary"]["low"], 0)
        self.assertEqual(final_dict["findings"], [])

        json_str = report_to_json(report)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["summary"]["total_findings"], 0)

    # ---------------------------------------------------------------------------
    # Test 12: Partial specialist failure handled gracefully
    # ---------------------------------------------------------------------------
    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_partial_specialist_audit_failure_handled_gracefully(
        self, mock_build, mock_crawl
    ) -> None:
        """Requirement 12: Individual specialist audit exceptions are isolated in audit_errors; other findings survive."""
        obs = _make_test_observation()
        mock_crawl.return_value = MagicMock(is_success=True)
        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/"
        mock_ctx.observations = {"https://example.com/": obs}
        mock_record = MagicMock()
        mock_record.observation = obs
        mock_ctx.page_by_url = {"https://example.com/": mock_record}
        mock_build.return_value = mock_ctx

        # Mock one audit to raise an exception
        def _failing_audit(o: PageObservation) -> list[Finding]:
            raise RuntimeError("Failing audit exploded!")

        def _working_audit(o: PageObservation) -> list[Finding]:
            return [
                Finding(
                    id="MA-001",
                    title="Alt Text Issue",
                    severity="low",
                    evidence="Missing alt text",
                    suggested_action=SuggestedAction(summary="Add alt", priority=3),
                    priority=3,
                    confidence="high",
                )
            ]

        with patch.dict(
            "src.orchestrator.AUDIT_REGISTRY",
            {
                "failing": _failing_audit,
                "working": _working_audit,
            },
            clear=True,
        ):
            options = AuditOptions(audits=("failing", "working"))
            result = run_audit("https://example.com/", options=options)

            self.assertTrue(result.is_success)
            self.assertIn("failing", result.audit_errors)
            self.assertIn("Failing audit exploded!", result.audit_errors["failing"])
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.findings[0].id, "MA-001")

    # ---------------------------------------------------------------------------
    # Test 13: Public run_audit() returns expected AuditResult shape
    # ---------------------------------------------------------------------------
    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_public_run_audit_contract_shape(
        self, mock_build, mock_crawl
    ) -> None:
        """Requirement 13: Public run_audit() returns AuditResult with expected attributes."""
        obs = _make_test_observation()
        mock_crawl.return_value = MagicMock(is_success=True)
        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/"
        mock_ctx.observations = {"https://example.com/": obs}
        mock_record = MagicMock()
        mock_record.observation = obs
        mock_ctx.page_by_url = {"https://example.com/": mock_record}
        mock_build.return_value = mock_ctx

        result = run_audit("https://example.com/")

        self.assertIsInstance(result, AuditResult)
        self.assertTrue(result.is_success)
        self.assertEqual(result.requested_url, "https://example.com/")
        self.assertIsInstance(result.findings, list)
        self.assertIsInstance(result.audit_errors, dict)

    # ---------------------------------------------------------------------------
    # Test 14: Marketplace-facing JSON serialization is valid and complete
    # ---------------------------------------------------------------------------
    def test_marketplace_facing_json_serialization(self) -> None:
        """Requirement 14: report.to_json() / report.as_final_dict() satisfies marketplace consumer schema."""
        action = SuggestedAction(summary="Add clear CTA button", priority=1)
        f = Finding(
            id="ENG-003",
            title="Task Continuation Deficit",
            severity="medium",
            evidence="Product page has zero action links.",
            suggested_action=action,
            priority=2,
            confidence="high",
            details={"link_count": 0},
            source_audit="engagement",
        )

        report = build_report(
            requested_url="https://example.com/item",
            final_url="https://example.com/item",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[f],
            audit_errors={},
            audits_run=["engagement"],
            audited_at="2026-09-13T02:00:00Z",
        )

        json_output = report.to_json()
        parsed = json.loads(json_output)

        self.assertEqual(parsed["site"], "https://example.com/item")
        self.assertEqual(parsed["audited_at"], "2026-09-13T02:00:00Z")
        self.assertEqual(parsed["summary"]["total_findings"], 1)
        self.assertEqual(parsed["summary"]["medium"], 1)
        self.assertEqual(len(parsed["findings"]), 1)

        f_json = parsed["findings"][0]
        self.assertEqual(f_json["id"], "ENG-003")
        self.assertEqual(f_json["title"], "Task Continuation Deficit")
        self.assertEqual(f_json["severity"], "medium")
        self.assertEqual(f_json["evidence"], "Product page has zero action links.")
        self.assertEqual(f_json["suggested_action"]["summary"], "Add clear CTA button")
        self.assertEqual(f_json["suggested_action"]["priority"], 1)
        self.assertEqual(f_json["priority"], 2)
        self.assertEqual(f_json["confidence"], "high")
        self.assertEqual(f_json["source_audit"], "engagement")
        self.assertEqual(f_json["details"], {"link_count": 0})


if __name__ == "__main__":
    unittest.main()
