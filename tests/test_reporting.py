"""Unit tests for the reporting layer (src/audits/reporting.py).

Tests cover the 11 required cases for the final report schema:

1.  Report with zero findings
2.  Report with one finding
3.  Report with multiple severities
4.  Correct severity counts
5.  Finding ordering preserved
6.  Evidence preserved
7.  Suggested action preserved
8.  Confidence / details preserved where available
9.  Structured failure handling
10. Deterministic serialization
11. Invalid / incomplete finding handling
"""

from __future__ import annotations

import json
import unittest

from src.audits.models import Finding, SuggestedAction
from src.audits.reporting import (
    AuditReport,
    _finding_to_dict,
    _json_safe,
    build_report,
    report_to_json,
)


# ---------------------------------------------------------------------------
# Shared factories
# ---------------------------------------------------------------------------

def _make_finding(
    rule_id: str = "MA-001",
    title: str = "Image alt text issue",
    severity: str = "medium",
    evidence: str = "Page contains 3 images lacking descriptive alt text.",
    priority: int = 2,
    confidence: str = "medium",
    source_audit: str = "machine_answerability",
) -> Finding:
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Add descriptive alt text.", priority=priority),
        priority=priority,
        confidence=confidence,  # type: ignore[arg-type]
        source_audit=source_audit,
    )


def _build_report(
    findings: list[Finding] | None = None,
    *,
    is_success: bool = True,
    error_message: str | None = None,
    audit_errors: dict[str, str] | None = None,
    audited_at: str | None = None,
) -> AuditReport:
    return build_report(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200 if is_success else 500,
        is_success=is_success,
        error_message=error_message,
        findings=findings or [],
        audit_errors=audit_errors or {},
        audits_run=["machine_answerability", "task_readiness"],
        audited_at=audited_at,
    )


# ===========================================================================
# 1. Report with zero findings
# ===========================================================================

class TestZeroFindings(unittest.TestCase):
    """Clean website produces a valid report with empty findings list."""

    def test_zero_findings_report_structure(self) -> None:
        report = _build_report(findings=[])
        d = report.as_final_dict()

        self.assertEqual(d["site"], "https://example.com/")
        self.assertIsNone(d["audited_at"])
        self.assertEqual(d["summary"]["total_findings"], 0)
        self.assertEqual(d["summary"]["high"], 0)
        self.assertEqual(d["summary"]["medium"], 0)
        self.assertEqual(d["summary"]["low"], 0)
        self.assertNotIn("critical", d["summary"])
        self.assertEqual(d["findings"], [])

    def test_zero_findings_json_valid(self) -> None:
        report = _build_report(findings=[])
        json_str = report.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed["summary"]["total_findings"], 0)
        self.assertEqual(parsed["findings"], [])


# ===========================================================================
# 2. Report with one finding
# ===========================================================================

class TestOneFinding(unittest.TestCase):
    """Report with exactly one finding produces correct structure."""

    def test_one_finding(self) -> None:
        f = _make_finding(rule_id="TR-DATA-001", severity="high", priority=1)
        report = _build_report(findings=[f])
        d = report.as_final_dict()

        self.assertEqual(d["summary"]["total_findings"], 1)
        self.assertEqual(d["summary"]["high"], 1)
        self.assertEqual(len(d["findings"]), 1)
        self.assertEqual(d["findings"][0]["id"], "TR-DATA-001")


# ===========================================================================
# 3. Report with multiple severities
# ===========================================================================

class TestMultipleSeverities(unittest.TestCase):
    """Findings across all severity levels are counted correctly."""

    def test_all_severities_present(self) -> None:
        findings = [
            _make_finding("C-001", severity="critical", priority=1),  # type: ignore[arg-type]
            _make_finding("H-001", severity="high", priority=1),
            _make_finding("M-001", severity="medium", priority=2),
            _make_finding("L-001", severity="low", priority=3),
        ]
        report = _build_report(findings=findings)
        d = report.as_final_dict()

        self.assertEqual(d["summary"]["total_findings"], 4)
        self.assertEqual(d["summary"]["critical"], 1)
        self.assertEqual(d["summary"]["high"], 1)
        self.assertEqual(d["summary"]["medium"], 1)
        self.assertEqual(d["summary"]["low"], 1)


# ===========================================================================
# 4. Correct severity counts
# ===========================================================================

class TestSeverityCounts(unittest.TestCase):
    """Severity counts in the summary match actual finding severities."""

    def test_counts_match_actual_severities(self) -> None:
        findings = [
            _make_finding("A", severity="medium"),
            _make_finding("B", severity="medium"),
            _make_finding("C", severity="low"),
        ]
        report = _build_report(findings=findings)
        d = report.as_final_dict()

        self.assertEqual(d["summary"]["total_findings"], 3)
        self.assertEqual(d["summary"]["medium"], 2)
        self.assertEqual(d["summary"]["low"], 1)
        self.assertEqual(d["summary"]["high"], 0)

    def test_critical_only_in_summary_when_present(self) -> None:
        findings = [_make_finding("H", severity="high")]
        report = _build_report(findings=findings)
        d = report.as_final_dict()
        self.assertNotIn("critical", d["summary"])

    def test_critical_appears_only_when_present(self) -> None:
        findings = [_make_finding("C", severity="critical")]  # type: ignore[arg-type]
        report = _build_report(findings=findings)
        d = report.as_final_dict()
        self.assertEqual(d["summary"]["critical"], 1)
        self.assertEqual(d["summary"]["high"], 0)


# ===========================================================================
# 5. Finding ordering preserved
# ===========================================================================

class TestFindingOrdering(unittest.TestCase):
    """Final report preserves the orchestrator's finding order."""

    def test_order_matches_input(self) -> None:
        findings = [
            _make_finding("P1-A", severity="critical", priority=1),  # type: ignore[arg-type]
            _make_finding("P2-B", severity="high", priority=2),
            _make_finding("P2-C", severity="medium", priority=2),
        ]
        report = _build_report(findings=findings)
        d = report.as_final_dict()

        self.assertEqual(d["findings"][0]["id"], "P1-A")
        self.assertEqual(d["findings"][1]["id"], "P2-B")
        self.assertEqual(d["findings"][2]["id"], "P2-C")


# ===========================================================================
# 6. Evidence preserved
# ===========================================================================

class TestEvidencePreservation(unittest.TestCase):
    """Finding evidence is never truncated, rephrased, or collapsed."""

    def test_exact_evidence_string_preserved(self) -> None:
        long_evidence = (
            "Page contains 3 image(s) lacking descriptive alt text "
            "while visible text is minimal (12 words) with no tables. "
            "If key product, specification, pricing, or decision information "
            "is conveyed primarily through images without equivalent text, "
            "automated systems may have less reliable access to that information."
        )
        f = _make_finding("MA-IMG-001", evidence=long_evidence)
        report = _build_report(findings=[f])
        d = report.as_final_dict()

        self.assertEqual(d["findings"][0]["evidence"], long_evidence)

    def test_evidence_roundtrips_through_json(self) -> None:
        evidence = "Unicode evidence: ₹9,999 vs $120.00 — no match."
        f = _make_finding("MA-REP-001", evidence=evidence)
        report = _build_report(findings=[f])
        parsed = json.loads(report.to_json())
        self.assertEqual(parsed["findings"][0]["evidence"], evidence)


# ===========================================================================
# 7. Suggested action preserved
# ===========================================================================

class TestSuggestedActionPreservation(unittest.TestCase):
    """Suggested action summary and priority are preserved."""

    def test_action_summary_and_priority(self) -> None:
        action = SuggestedAction(
            summary="Reconcile pricing between structured data and visible text.",
            priority=1,
        )
        f = Finding(
            id="MA-REP-001",
            title="Conflicting representations",
            severity="high",
            evidence="Price mismatch.",
            suggested_action=action,
            priority=1,
            confidence="high",
            source_audit="machine_answerability",
        )
        report = _build_report(findings=[f])
        d = report.as_final_dict()

        sa = d["findings"][0]["suggested_action"]
        self.assertEqual(sa["summary"], "Reconcile pricing between structured data and visible text.")
        self.assertEqual(sa["priority"], 1)


# ===========================================================================
# 8. Confidence / details preserved where available
# ===========================================================================

class TestConfidenceDetailsPreservation(unittest.TestCase):
    """Confidence, source_audit, and structured details survive serialization."""

    def test_confidence_and_source_audit_present(self) -> None:
        f = _make_finding("MA-IMG-001", confidence="high", source_audit="machine_answerability")
        report = _build_report(findings=[f])
        d = report.as_final_dict()

        self.assertEqual(d["findings"][0]["confidence"], "high")
        self.assertEqual(d["findings"][0]["source_audit"], "machine_answerability")

    def test_details_preserved_as_structured_dict(self) -> None:
        f = Finding(
            id="MA-IMG-001",
            title="Image content dependency",
            severity="medium",
            evidence="Page has 3 images without alt text.",
            suggested_action=SuggestedAction(summary="Add alt text.", priority=2),
            priority=2,
            confidence="high",
            details={"unhelpful_image_count": 3, "total_images": 5, "visible_word_count": 10},
            source_audit="machine_answerability",
        )
        report = _build_report(findings=[f])
        d = report.as_final_dict()

        self.assertEqual(d["findings"][0]["details"]["unhelpful_image_count"], 3)
        self.assertEqual(d["findings"][0]["details"]["visible_word_count"], 10)
        # details is a dict, not an opaque string
        self.assertIsInstance(d["findings"][0]["details"], dict)

    def test_no_details_key_when_empty(self) -> None:
        f = Finding(
            id="A-001",
            title="No details",
            severity="low",
            evidence="Evidence.",
            suggested_action=SuggestedAction(summary="Fix.", priority=3),
            priority=3,
            confidence="low",
            details={},
            source_audit="test",
        )
        report = _build_report(findings=[f])
        d = report.as_final_dict()
        self.assertNotIn("details", d["findings"][0])


# ===========================================================================
# 9. Structured failure handling
# ===========================================================================

class TestStructuredFailureHandling(unittest.TestCase):
    """Pipeline failure is structured — no Python stack traces in the report."""

    def test_failure_block_when_pipeline_fails(self) -> None:
        report = _build_report(
            is_success=False,
            error_message="HTTP error status 404.",
        )
        d = report.as_final_dict()

        self.assertIn("failure", d)
        self.assertEqual(d["failure"]["url"], "https://example.com/")
        self.assertEqual(d["failure"]["status_code"], 500)
        self.assertEqual(d["failure"]["message"], "HTTP error status 404.")

    def test_no_failure_block_on_success(self) -> None:
        report = _build_report(findings=[])
        d = report.as_final_dict()
        self.assertNotIn("failure", d)

    def test_audit_errors_preserved_when_present(self) -> None:
        report = _build_report(
            audit_errors={"engagement": "Runtime error in engagement audit."},
        )
        d = report.as_final_dict()

        self.assertIn("audit_errors", d)
        self.assertEqual(d["audit_errors"]["engagement"], "Runtime error in engagement audit.")

    def test_no_audit_errors_key_when_none(self) -> None:
        report = _build_report(findings=[])
        d = report.as_final_dict()
        self.assertNotIn("audit_errors", d)

    def test_no_stack_trace_in_failure_message(self) -> None:
        msg = "Connection timeout after 10s"
        report = _build_report(is_success=False, error_message=msg)
        d = report.as_final_dict()
        # No Python trace markers in the user-facing message
        self.assertNotIn("Traceback", d["failure"]["message"])
        self.assertNotIn("File \"", d["failure"]["message"])
        self.assertEqual(d["failure"]["message"], msg)


# ===========================================================================
# 10. Deterministic serialization
# ===========================================================================

class TestDeterministicSerialization(unittest.TestCase):
    """Same report produces identical JSON every time; audited_at is injectable."""

    def test_same_report_same_json_string(self) -> None:
        f = _make_finding("MA-001", severity="medium")
        report = _build_report(findings=[f], audited_at="2026-01-15T10:00:00Z")
        json_a = report.to_json()
        json_b = report.to_json()
        self.assertEqual(json_a, json_b)

    def test_injectable_audited_at_appears_in_output(self) -> None:
        ts = "2026-03-20T14:30:00Z"
        report = _build_report(audited_at=ts)
        d = report.as_final_dict()
        self.assertEqual(d["audited_at"], ts)

    def test_no_audited_at_defaults_to_none(self) -> None:
        report = _build_report()
        self.assertIsNone(report.audited_at)
        d = report.as_final_dict()
        self.assertIsNone(d["audited_at"])

    def test_json_sort_keys_stable(self) -> None:
        """Sorted keys guarantee reproducible output across Python hash seed."""
        f = _make_finding("MA-001")
        report = _build_report(findings=[f], audited_at="2026-01-01T00:00:00Z")
        parsed_top = json.loads(report.to_json(sort_keys=True))
        keys = list(parsed_top.keys())
        self.assertEqual(keys, sorted(keys))

    def test_report_to_json_matches_to_json(self) -> None:
        f = _make_finding("H-001", severity="high")
        report = _build_report(findings=[f], audited_at="2026-06-01T00:00:00Z")
        self.assertEqual(report.to_json(), report_to_json(report))


# ===========================================================================
# 11. Invalid / incomplete finding handling
# ===========================================================================

class TestInvalidIncompleteFindingHandling(unittest.TestCase):
    """Serialization tolerates partial, empty, or malformed inputs gracefully."""

    def _make_finding_like(
        self,
        *,
        finding_id: str = "X-001",
        title: str = "",
        severity: str = "blocker",
        evidence: str = "",
        suggested_action: object = None,
        priority: int = 1,
        confidence: str | None = None,
        source_audit: str = "",
        details: dict | None = None,
    ) -> object:
        """Return a plain object that duck-types a Finding (missing class)."""

        class _FakeFinding:
            pass

        obj = _FakeFinding()
        obj.id = finding_id
        obj.title = title
        obj.severity = severity
        obj.evidence = evidence
        obj.suggested_action = suggested_action
        obj.priority = priority
        obj.confidence = confidence
        obj.source_audit = source_audit
        obj.details = details or {}
        return obj

    def test_empty_title_uses_finding_id(self) -> None:
        f = self._make_finding_like(finding_id="TR-001", title="")
        d = _finding_to_dict(f)
        self.assertEqual(d["title"], "TR-001")

    def test_empty_title_and_id_uses_untitled(self) -> None:
        f = self._make_finding_like(finding_id="", title="")
        d = _finding_to_dict(f)
        self.assertEqual(d["title"], "Untitled finding")

    def test_unknown_severity_not_in_summary(self) -> None:
        findings = [self._make_finding_like(severity="blocker")]
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[],  # type: ignore[arg-type]
            audit_errors={},
            audits_run=[],
        )
        # Manually build a report with the fake finding
        report = AuditReport(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=findings,  # type: ignore[list-item]
            audit_errors={},
            audits_run=[],
        )
        d = report.as_final_dict()
        # Unknown severity is not counted, but total is still 1
        self.assertEqual(d["summary"]["total_findings"], 1)
        self.assertNotIn("blocker", d["summary"])
        self.assertEqual(d["summary"]["high"], 0)

    def test_empty_evidence_serializes_as_empty_string(self) -> None:
        f = self._make_finding_like(evidence="")
        d = _finding_to_dict(f)
        self.assertEqual(d["evidence"], "")

    def test_suggested_action_none_serializes_as_none(self) -> None:
        f = self._make_finding_like(suggested_action=None)
        d = _finding_to_dict(f)
        self.assertIsNone(d["suggested_action"])

    def test_missing_confidence_omitted_from_dict(self) -> None:
        f = self._make_finding_like(confidence=None)
        d = _finding_to_dict(f)
        self.assertNotIn("confidence", d)

    def test_full_incomplete_finding_serializes_without_exception(self) -> None:
        f = self._make_finding_like(
            finding_id="",
            title="",
            severity="unknown",
            evidence="",
            suggested_action=None,
            confidence=None,
        )
        d = _finding_to_dict(f)
        self.assertIsInstance(d, dict)
        self.assertEqual(d["title"], "Untitled finding")

    def test_json_safe_handles_nested_set_deterministically(self) -> None:
        val = {"tags": {"z", "a", "m"}, "count": 3}
        safe = _json_safe(val)
        self.assertIsInstance(safe["tags"], list)
        self.assertEqual(safe["tags"], ["a", "m", "z"])

    def test_json_safe_preserves_primitives(self) -> None:
        self.assertEqual(_json_safe(None), None)
        self.assertEqual(_json_safe(42), 42)
        self.assertEqual(_json_safe("hello"), "hello")
        self.assertTrue(_json_safe(True))
        self.assertAlmostEqual(_json_safe(3.14), 3.14)

    def test_json_safe_stringifies_unknown_types(self) -> None:
        class _Weird:
            def __repr__(self) -> str:
                return "WeirdObject"

        result = _json_safe(_Weird())
        self.assertEqual(result, "WeirdObject")


# ===========================================================================
# 12. Task-Aware Reporting (cases 15-20)
# ===========================================================================

class TestTaskAwareReporting(unittest.TestCase):
    """Tests for task-aware reporting serialization and schema."""

    def test_task_context_appears_when_supplied(self) -> None:
        """15. task_context block appears in as_final_dict when supplied in report."""
        f = _make_finding(rule_id="TR-INT-001")
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[f],
            audit_errors={},
            audits_run=["task_readiness"],
            task_intent="purchase shoes",
            task_description="buying sneakers online",
            target_objectives=["transaction_completion"],
        )
        d = report.as_final_dict()
        self.assertIn("task_context", d)
        self.assertEqual(d["task_context"]["task_intent"], "purchase shoes")
        self.assertEqual(d["task_context"]["task_description"], "buying sneakers online")
        self.assertEqual(d["task_context"]["target_objectives"], ["transaction_completion"])

    def test_task_aware_fields_appear_on_findings(self) -> None:
        """16, 17, 18, 19: affected_objectives, task_relevance, priority_reason appear and details remain intact."""
        from src.audits.objective_mapping import ObjectiveMapping
        from src.audits.priority_engine import PrioritySignal

        f = Finding(
            id="TR-INT-001",
            title="Non-navigable action affordance",
            severity="critical",
            evidence="Evidence for non-navigable buy button.",
            suggested_action=SuggestedAction(summary="Fix link", priority=1),
            priority=1,
            confidence="high",
            source_audit="task_readiness",
            details={"button_selector": "#buy-btn"},
        )
        m = ObjectiveMapping(
            finding_id="TR-INT-001",
            source_audit="task_readiness",
            affected_objectives=["transaction_completion"],
            task_relevance="direct_blocker",
            relevance_reason="Blocks purchase.",
        )
        s = PrioritySignal(
            finding_id="TR-INT-001",
            finding_title=f.title,
            severity=f.severity,
            confidence=f.confidence,
            priority=f.priority,
            source_audit=f.source_audit,
            affected_url="https://example.com/",
            task_relevance="direct_blocker",
            priority_reason="Direct blocker for transaction_completion.",
            affected_objectives=["transaction_completion"],
        )

        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[f],
            audit_errors={},
            audits_run=["task_readiness"],
            task_intent="buy shoes",
            target_objectives=["transaction_completion"],
            objective_mappings=[m],
            priority_signals=[s],
        )

        d = report.as_final_dict()
        self.assertEqual(len(d["findings"]), 1)
        fd = d["findings"][0]

        # Case 16: affected_objectives
        self.assertEqual(fd["affected_objectives"], ["transaction_completion"])
        # Case 17: task_relevance
        self.assertEqual(fd["task_relevance"], "direct_blocker")
        # Case 18: priority_reason
        self.assertEqual(fd["priority_reason"], "Direct blocker for transaction_completion.")
        # Case 19: details preserved
        self.assertEqual(fd["details"], {"button_selector": "#buy-btn"})
        self.assertEqual(fd["evidence"], "Evidence for non-navigable buy button.")
        self.assertEqual(fd["confidence"], "high")

    def test_no_context_report_remains_backward_compatible(self) -> None:
        """20. When no task context is supplied, as_final_dict omits task_context and finding task fields."""
        f = _make_finding(rule_id="TR-INT-001")
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[f],
            audit_errors={},
            audits_run=["task_readiness"],
        )
        d = report.as_final_dict()
        self.assertNotIn("task_context", d)
        self.assertNotIn("affected_objectives", d["findings"][0])
        self.assertNotIn("task_relevance", d["findings"][0])
        self.assertNotIn("priority_reason", d["findings"][0])


if __name__ == "__main__":
    unittest.main()
