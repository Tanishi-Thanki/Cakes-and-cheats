"""Comprehensive orchestrator integration tests.

Test categories (per specification):
1. Valid URL → crawler → observation → audit → findings
2. Multiple audits can consume the same observation
3. Findings from multiple audits are aggregated
4. Duplicate findings are conservatively deduplicated
5. Distinct findings are NOT accidentally merged
6. One audit failure does not unnecessarily destroy other results
7. Invalid URL/fetch failure is handled cleanly
8. Task context is preserved
9. Existing Finding/SuggestedAction information survives orchestration
10. Optional/future audit unavailable — architecture remains extensible

Additional coverage:
- AuditRequest-based invocation
- AuditReport generation via run_audit_full
- PriorityEngine signal collection
- ObjectiveMapping foundation
- Source audit attribution on findings
"""


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest
from unittest.mock import MagicMock, patch

from src.audits.audit_request import AuditContext, AuditRequest
from src.audits.finding_aggregator import (
    aggregate_findings,
    deduplicate_findings,
    sort_findings,
    tag_findings,
)
from src.audits.models import Finding, SuggestedAction
from src.audits.objective_mapping import map_findings_to_objectives
from src.audits.priority_engine import PriorityEngine, collect_signals, compute_priorities
from src.audits.reporting import build_report
from src.crawler.crawler import FetchedPage
from src.observation.observation import PageObservation, PageSignals
from src.orchestrator import (
    AUDIT_REGISTRY,
    AuditOptions,
    AuditResult,
    _DEFAULT_AUDITS,
    _deduplicate_findings,
    _sort_findings,
    run_audit,
    run_audit_full,
)
from unittest.mock import MagicMock
from tests.testing_helpers import _stub_t4_no_findings

def _make_mock_site_context(is_success=True, error_message=None, obs=None):
    ctx = MagicMock()
    ctx.normalized_start_url = "https://example.com/"
    if obs is None:
        obs = _make_observation(is_success=is_success, error_message=error_message)
    if is_success:
        ctx.observations = {"https://example.com/": obs}
        page = MagicMock()
        page.observation = obs
        ctx.page_by_url = {"https://example.com/": page}
    else:
        ctx.observations = {}
        ctx.page_by_url = {}
    return ctx

def _make_mock_crawl_result(is_success=True, error_message=None):
    res = MagicMock()
    res.is_success = is_success
    res.error_message = error_message
    return res



# ---------------------------------------------------------------------------
# Shared factories
# ---------------------------------------------------------------------------

def _make_fetched_page(is_success: bool = True, error_message: str | None = None) -> FetchedPage:
    return FetchedPage(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200 if is_success else 500,
        content_type="text/html",
        html_content="<html><body><p>Test page content.</p></body></html>" if is_success else "",
        is_success=is_success,
        error_message=error_message,
    )


def _make_observation(is_success: bool = True, error_message: str | None = None) -> PageObservation:
    return PageObservation(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200 if is_success else None,
        is_success=is_success,
        error_message=error_message,
        title="Test Page",
        signals=PageSignals(
            has_title=True,
            has_meta_description=False,
            has_canonical=False,
            num_links=0,
            num_images=0,
            num_tables=0,
            num_headings=0,
        ),
    )


def _make_finding(
    rule_id: str = "TR-DATA-001",
    title: str = "Test Finding",
    severity: str = "high",
    evidence: str = "Evidence text for test finding.",
    priority: int = 1,
    confidence: str = "medium",
) -> Finding:
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Fix it.", priority=priority),
        priority=priority,
        confidence=confidence,  # type: ignore[arg-type]
        details={"internal_key": "internal_value"},
    )


# ===========================================================================
# Category 1: Valid URL → crawler → observation → audit → findings
# ===========================================================================

class TestValidUrlPipeline(unittest.TestCase):
    """End-to-end: URL → fetch → observe → audit → findings."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_valid_url_produces_findings(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """1a: Valid URL with findings returns structured AuditResult."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-IMG-001")]
        mock_tr.return_value = [_make_finding("TR-DATA-001")]

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertEqual(len(result.findings), 2)
        mock_crawl.assert_called_once()
        mock_build.assert_called_once()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_clean_page_returns_empty_findings(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """1b: Valid URL with no issues returns empty findings list."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = []
        mock_tr.return_value = []

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_findings_carry_source_audit_attribution(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """1c: Findings in the result carry source_audit tags."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-IMG-001")]
        mock_tr.return_value = [_make_finding("TR-DATA-001")]

        result = run_audit("https://example.com/")

        # With explicit audits via patching only MA and TR, those are the sources.
        ma_findings = [f for f in result.findings if f.source_audit == "machine_answerability"]
        tr_findings = [f for f in result.findings if f.source_audit == "task_readiness"]
        self.assertEqual(len(ma_findings), 1)
        self.assertEqual(len(tr_findings), 1)


# ===========================================================================
# Category 2: Multiple audits consume the same observation
# ===========================================================================

class TestSharedObservation(unittest.TestCase):
    """All audits receive the identical PageObservation object."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_same_observation_identity(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """2a: Both audits receive the exact same obs instance."""
        canonical_obs = _make_observation()
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context(obs=canonical_obs)
        mock_ma.return_value = []
        mock_tr.return_value = []

        run_audit("https://example.com/")

        self.assertIs(mock_ma.call_args[0][0], canonical_obs)
        self.assertIs(mock_tr.call_args[0][0], canonical_obs)
        self.assertIs(mock_ma.call_args[0][0], mock_tr.call_args[0][0])

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_fetch_called_once_observe_called_once(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """2b: fetch_page and observe_page each called exactly once."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-001")]
        mock_tr.return_value = [_make_finding("TR-001")]

        run_audit("https://example.com/")

        self.assertEqual(mock_crawl.call_count, 1)
        self.assertEqual(mock_build.call_count, 1)


# ===========================================================================
# Category 3: Findings from multiple audits are aggregated
# ===========================================================================

class TestFindingAggregation(unittest.TestCase):
    """Findings from different audits are combined into a single result."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_findings_from_both_audits_present(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """3a: Both MA and TR findings appear in the combined result."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [
            _make_finding("MA-IMG-001", evidence="MA evidence."),
        ]
        mock_tr.return_value = [
            _make_finding("TR-DATA-001", evidence="TR evidence."),
        ]

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 2)
        ids = {f.id for f in result.findings}
        self.assertIn("MA-IMG-001", ids)
        self.assertIn("TR-DATA-001", ids)

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_findings_sorted_by_priority_and_severity(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """3b: Aggregated findings are sorted (priority ASC, severity DESC)."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [
            _make_finding("MA-A", priority=2, severity="medium"),
        ]
        mock_tr.return_value = [
            _make_finding("TR-B", priority=1, severity="critical"),
        ]

        result = run_audit("https://example.com/")

        self.assertEqual(result.findings[0].id, "TR-B")
        self.assertEqual(result.findings[0].priority, 1)
        self.assertEqual(result.findings[1].id, "MA-A")
        self.assertEqual(result.findings[1].priority, 2)


# ===========================================================================
# Category 4: Duplicate findings are conservatively deduplicated
# ===========================================================================

class TestConservativeDeduplication(unittest.TestCase):
    """Exact duplicates (same id + same evidence fingerprint) are removed."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_exact_duplicate_removed(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """4a: Same finding object from two audits → deduplicated to one."""
        shared = _make_finding("XX-001", evidence="Shared identical evidence.")
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [shared]
        mock_tr.return_value = [shared]

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 1)

    def test_same_id_different_evidence_not_merged(self) -> None:
        """4b: Same id but different evidence → two distinct findings."""
        f1 = _make_finding("TR-INT-001", evidence="First broken link evidence.")
        f2 = _make_finding("TR-INT-001", evidence="Second broken link evidence, entirely different content.")
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_dedup_preserves_first_occurrence_order(self) -> None:
        """4c: When duplicates exist, first occurrence is kept."""
        f_a = _make_finding("MA-001", evidence="Alpha evidence.")
        f_b = _make_finding("TR-001", evidence="Beta evidence.")
        f_a_dup = _make_finding("MA-001", evidence="Alpha evidence.")
        result = deduplicate_findings([f_a, f_b, f_a_dup])
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], f_a)
        self.assertIs(result[1], f_b)

    def test_tagged_dedup_works_across_sources(self) -> None:
        """4d: Same finding tagged from different audits is deduplicated."""
        f1 = tag_findings([_make_finding("XX-001", evidence="Identical evidence.")], "audit_a")
        f2 = tag_findings([_make_finding("XX-001", evidence="Identical evidence.")], "audit_b")
        combined = f1 + f2
        result = deduplicate_findings(combined)
        self.assertEqual(len(result), 1)
        # First occurrence wins (from audit_a)
        self.assertEqual(result[0].source_audit, "audit_a")


# ===========================================================================
# Category 5: Distinct findings are NOT accidentally merged
# ===========================================================================

class TestDistinctFindingsPreserved(unittest.TestCase):
    """Different findings must never be collapsed."""

    def test_different_ids_both_preserved(self) -> None:
        """5a: Two findings with different ids are never merged."""
        f1 = _make_finding("MA-IMG-001", evidence="Image alt text issue.")
        f2 = _make_finding("TR-DATA-001", evidence="Missing price data.")
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_same_id_different_evidence_preserved(self) -> None:
        """5b: Same rule triggered by different evidence on different elements."""
        f1 = _make_finding(
            "TR-INT-001",
            evidence="Add to Cart link has href='#' on product page.",
        )
        f2 = _make_finding(
            "TR-INT-001",
            evidence="Checkout link uses javascript:void(0) on cart page.",
        )
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_similar_titles_different_evidence_not_merged(self) -> None:
        """5c: Similar titles with different evidence are distinct findings."""
        f1 = _make_finding(
            "MA-REP-001",
            title="Conflicting representations",
            evidence="Price mismatch: JSON-LD says 12999 but visible shows 9999.",
        )
        f2 = _make_finding(
            "MA-REP-001",
            title="Conflicting representations",
            evidence="Availability mismatch: JSON-LD says InStock but page shows Out of Stock.",
        )
        result = deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)

    def test_aggregate_preserves_distinct_findings(self) -> None:
        """5d: aggregate_findings does not merge distinct findings."""
        f1 = _make_finding("A-001", evidence="Issue A evidence.", priority=1, severity="critical")
        f2 = _make_finding("B-002", evidence="Issue B evidence.", priority=2, severity="high")
        f3 = _make_finding("C-003", evidence="Issue C evidence.", priority=3, severity="low")
        result = aggregate_findings([f1, f2, f3])
        self.assertEqual(len(result), 3)


# ===========================================================================
# Category 6: One audit failure does not destroy other results
# ===========================================================================

class TestAuditFailureIsolation(unittest.TestCase):
    """Individual audit exceptions are isolated."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_one_audit_crash_preserves_others(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """6a: MA crashes → TR still runs, MA error recorded."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.side_effect = RuntimeError("MA internal crash.")
        mock_tr.return_value = [_make_finding("TR-DATA-001")]

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertIn("machine_answerability", result.audit_errors)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].id, "TR-DATA-001")

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_both_audits_crash_empty_findings(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """6b: Both audits crash → is_success still True, findings empty, both errors recorded."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.side_effect = RuntimeError("MA crash.")
        mock_tr.side_effect = RuntimeError("TR crash.")

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])
        self.assertEqual(len(result.audit_errors), 2)
        self.assertIn("machine_answerability", result.audit_errors)
        self.assertIn("task_readiness", result.audit_errors)

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_failed_audit_error_recorded_as_string(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """6c: Audit error is recorded as a string, not raised."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.side_effect = ValueError("Bad data.")
        mock_tr.return_value = []

        result = run_audit("https://example.com/")

        self.assertIsInstance(result.audit_errors["machine_answerability"], str)
        self.assertIn("Bad data", result.audit_errors["machine_answerability"])


# ===========================================================================
# Category 7: Invalid URL / fetch failure is handled cleanly
# ===========================================================================

class TestFetchFailureHandling(unittest.TestCase):
    """Network errors, invalid URLs, and non-HTML responses are handled."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_404_returns_failure_no_audits_run(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """7a: HTTP 404 → is_success=False, no audits run."""
        mock_crawl.return_value = _make_mock_crawl_result(
            is_success=False, error_message="HTTP error status 404."
        )

        result = run_audit("https://example.com/missing")

        self.assertFalse(result.is_success)
        self.assertEqual(result.findings, [])
        mock_build.assert_not_called()
        mock_ma.assert_not_called()
        mock_tr.assert_not_called()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_observation_failure_stops_pipeline(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """7b: Observation exception → is_success=False, no audits run."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.side_effect = RuntimeError("Parse failure.")

        result = run_audit("https://example.com/")

        self.assertFalse(result.is_success)
        self.assertIn("Parse failure", result.error_message or "")
        mock_ma.assert_not_called()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_observation_is_success_false_stops(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """7c: Observation with is_success=False stops the pipeline."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context(is_success=False)

        result = run_audit("https://example.com/")

        self.assertFalse(result.is_success)
        mock_ma.assert_not_called()


# ===========================================================================
# Category 8: Task context is preserved
# ===========================================================================

class TestTaskContextPreservation(unittest.TestCase):
    """AuditRequest context flows through the pipeline."""

    def test_audit_request_carries_context(self) -> None:
        """8a: AuditRequest with context is accepted and options are derived."""
        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(
                task_intent="purchase shoes",
                task_description="Find and buy running shoes.",
                target_objectives=["conversion", "user_satisfaction"],
            ),
            audits=("machine_answerability",),
            include_details=True,
        )

        self.assertEqual(request.target_url, "https://example.com/")
        self.assertEqual(request.context.task_intent, "purchase shoes")
        self.assertEqual(request.context.target_objectives, ["conversion", "user_satisfaction"])

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_run_audit_accepts_audit_request(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """8b: run_audit(AuditRequest(...)) works correctly."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = []
        mock_tr.return_value = []

        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(task_intent="compare products"),
        )
        result = run_audit(request)

        self.assertTrue(result.is_success)
        self.assertEqual(result.requested_url, "https://example.com/")

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_run_audit_full_returns_report(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """8c: run_audit_full returns both AuditResult and AuditReport with context."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-001")]
        mock_tr.return_value = []

        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(
                task_intent="buy shoes",
                task_description="Purchase running shoes online.",
                target_objectives=["conversion"],
            ),
        )
        result, report = run_audit_full(request)

        self.assertTrue(result.is_success)
        self.assertEqual(report.task_intent, "buy shoes")
        self.assertEqual(report.task_description, "Purchase running shoes online.")
        self.assertEqual(report.target_objectives, ["conversion"])
        self.assertEqual(report.summary["total_findings"], 1)

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_run_audit_full_wires_downstream_stages(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """8c2: Objective Mapping and Priority Engine run downstream of run_audit."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [
            _make_finding("P2-001", priority=2, severity="low"),
            _make_finding("P1-001", priority=1, severity="high"),
        ]
        mock_tr.return_value = []

        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(target_objectives=["conversion"]),
        )
        result, report = run_audit_full(request)

        # One mapping and one priority signal per finding.
        self.assertEqual(len(report.objective_mappings), 2)
        self.assertEqual(len(report.priority_signals), 2)
        # No unestablished objective links are claimed.
        for m in report.objective_mappings:
            self.assertEqual(m.affected_objectives, [])
        # Priority Engine orders by audit-supplied priority (P1 before P2).
        self.assertEqual(report.priority_signals[0].finding_id, "P1-001")
        self.assertEqual(report.priority_signals[1].finding_id, "P2-001")
        # Serialized report exposes both stages.
        d = report.as_dict()
        self.assertEqual(len(d["objective_mappings"]), 2)
        self.assertEqual(d["priority_signals"][0]["finding_id"], "P1-001")
        # run_audit itself returns no mappings/signals (backward-compatible).
        self.assertFalse(hasattr(result, "objective_mappings"))

    def test_audit_context_defaults(self) -> None:
        """8d: AuditContext defaults are sensible."""
        ctx = AuditContext()
        self.assertIsNone(ctx.task_intent)
        self.assertIsNone(ctx.task_description)
        self.assertEqual(ctx.target_objectives, [])
        self.assertEqual(ctx.extra, {})

    def test_audit_request_defaults(self) -> None:
        """8e: AuditRequest defaults are sensible."""
        req = AuditRequest(target_url="https://example.com/")
        self.assertIsNone(req.context)
        self.assertIsNone(req.audits)
        self.assertIsNone(req.timeout_seconds)
        self.assertFalse(req.include_details)


# ===========================================================================
# Category 9: Finding/SuggestedAction info survives orchestration
# ===========================================================================

class TestFindingPreservation(unittest.TestCase):
    """All Finding fields survive the orchestration pipeline."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    @_stub_t4_no_findings
    def test_all_finding_fields_preserved(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """9a: Every field of a Finding survives orchestration."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()

        original = Finding(
            id="MA-IMG-001",
            title="Image content dependency",
            severity="medium",
            evidence="Page has 3 images without alt text.",
            suggested_action=SuggestedAction(summary="Add alt text.", priority=2),
            priority=2,
            confidence="high",
            details={"image_count": 3, "word_count": 10},
        )
        mock_ma.return_value = [original]
        mock_tr.return_value = []

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 1)
        f = result.findings[0]
        self.assertEqual(f.id, "MA-IMG-001")
        self.assertEqual(f.title, "Image content dependency")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.evidence, "Page has 3 images without alt text.")
        self.assertEqual(f.suggested_action.summary, "Add alt text.")
        self.assertEqual(f.suggested_action.priority, 2)
        self.assertEqual(f.priority, 2)
        self.assertEqual(f.confidence, "high")
        self.assertEqual(f.details, {"image_count": 3, "word_count": 10})
        self.assertEqual(f.source_audit, "machine_answerability")

    def test_original_finding_not_mutated(self) -> None:
        """9b: The original Finding object returned by an audit is never modified."""
        original = _make_finding("MA-001", evidence="Test evidence.")
        tagged = tag_findings([original], "test_audit")
        self.assertEqual(original.source_audit, "")
        self.assertEqual(tagged[0].source_audit, "test_audit")
        self.assertNotEqual(original, tagged[0])

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_as_report_dict_includes_source_audit(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """9c: as_report_dict() includes source_audit field."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-001")]
        mock_tr.return_value = []

        result = run_audit("https://example.com/")
        report = result.as_report_dict()

        self.assertIn("source_audit", report["findings"][0])
        self.assertEqual(report["findings"][0]["source_audit"], "machine_answerability")


# ===========================================================================
# Category 10: Optional/future audit unavailable — extensibility
# ===========================================================================

class TestOptionalAuditExtensibility(unittest.TestCase):
    """Architecture remains extensible when optional audits are unavailable."""

    def test_unknown_audit_name_skipped(self) -> None:
        """10a: Requesting an unknown audit name doesn't crash the pipeline."""
        from src.orchestrator import _resolve_audit_names
        resolved = _resolve_audit_names(
            ("machine_answerability", "nonexistent_future_audit", "task_readiness")
        )
        self.assertEqual(resolved, ("machine_answerability", "task_readiness"))

    def test_all_unknown_names_resolves_empty(self) -> None:
        """10b: All unknown names → empty tuple, pipeline still runs."""
        from src.orchestrator import _resolve_audit_names
        resolved = _resolve_audit_names(("fake_audit_1", "fake_audit_2"))
        self.assertEqual(resolved, ())

    def test_audit_registry_is_extensible(self) -> None:
        """10c: AUDIT_REGISTRY can accept new audit callables."""
        from src.observation.observation import PageObservation

        def custom_audit(obs: PageObservation) -> list[Finding]:
            return []

        AUDIT_REGISTRY["custom_future_audit"] = custom_audit
        self.assertIn("custom_future_audit", AUDIT_REGISTRY)
        self.assertTrue(callable(AUDIT_REGISTRY["custom_future_audit"]))
        # Cleanup
        del AUDIT_REGISTRY["custom_future_audit"]

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_selective_audit_execution(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """10d: Only selected audits run when AuditOptions.audits is specified."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-001")]
        mock_tr.return_value = []

        options = AuditOptions(audits=("machine_answerability",))
        result = run_audit("https://example.com/", options=options)

        mock_ma.assert_called_once()
        mock_tr.assert_not_called()
        self.assertEqual(len(result.findings), 1)


# ===========================================================================
# FindingAggregator unit tests
# ===========================================================================

class TestFindingAggregator(unittest.TestCase):
    """Tests for the finding_aggregator module."""

    def test_tag_findings_sets_source_audit(self) -> None:
        """tag_findings creates new Finding objects with source_audit."""
        f = _make_finding("MA-001")
        tagged = tag_findings([f], "machine_answerability")
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0].source_audit, "machine_answerability")
        self.assertEqual(tagged[0].id, "MA-001")

    def test_tag_findings_preserves_all_fields(self) -> None:
        """tag_findings preserves every field of the original finding."""
        f = _make_finding("TR-001", severity="critical", priority=1, evidence="Evid.")
        tagged = tag_findings([f], "test")
        t = tagged[0]
        self.assertEqual(t.id, "TR-001")
        self.assertEqual(t.severity, "critical")
        self.assertEqual(t.priority, 1)
        self.assertEqual(t.evidence, "Evid.")
        self.assertEqual(t.details, {"internal_key": "internal_value"})

    def test_sort_findings_orders_correctly(self) -> None:
        """sort_findings orders by priority ASC, severity DESC."""
        f1 = _make_finding("A", priority=3, severity="low")
        f2 = _make_finding("B", priority=1, severity="high")
        f3 = _make_finding("C", priority=1, severity="critical")
        result = sort_findings([f1, f2, f3])
        self.assertEqual(result[0].id, "C")
        self.assertEqual(result[1].id, "B")
        self.assertEqual(result[2].id, "A")

    def test_aggregate_findings_full_pipeline(self) -> None:
        """aggregate_findings deduplicates and sorts."""
        f1 = _make_finding("A-001", evidence="Evidence A.", priority=2)
        f2 = _make_finding("B-002", evidence="Evidence B.", priority=1)
        f1_dup = _make_finding("A-001", evidence="Evidence A.")  # duplicate
        result = aggregate_findings([f1, f2, f1_dup])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].id, "B-002")
        self.assertEqual(result[1].id, "A-001")


# ===========================================================================
# PriorityEngine tests
# ===========================================================================

class TestPriorityEngine(unittest.TestCase):
    """Tests for the PriorityEngine signal collection and ranking."""

    def test_collect_signals_from_findings(self) -> None:
        """collect_signals creates PrioritySignal per finding."""
        f1 = _make_finding("MA-001", severity="high", confidence="high")
        f1 = Finding(
            id=f1.id, title=f1.title, severity=f1.severity,
            evidence=f1.evidence, suggested_action=f1.suggested_action,
            priority=f1.priority, confidence=f1.confidence,
            details=f1.details, source_audit="machine_answerability",
        )
        signals = collect_signals([f1], affected_url="https://example.com/")
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].finding_id, "MA-001")
        self.assertEqual(signals[0].source_audit, "machine_answerability")
        self.assertEqual(signals[0].affected_url, "https://example.com/")

    def test_compute_priorities_sorts_signals(self) -> None:
        """compute_priorities returns signals sorted by priority."""
        from src.audits.priority_engine import PrioritySignal
        s1 = PrioritySignal(
            finding_id="A", finding_title="A", severity="medium",
            confidence="high", priority=2, source_audit="x", affected_url="",
        )
        s2 = PrioritySignal(
            finding_id="B", finding_title="B", severity="critical",
            confidence="high", priority=1, source_audit="x", affected_url="",
        )
        result = compute_priorities([s1, s2])
        self.assertEqual(result[0].finding_id, "B")
        self.assertEqual(result[1].finding_id, "A")

    def test_priorityEngine_class_interface(self) -> None:
        """PriorityEngine class provides collect + rank interface."""
        engine = PriorityEngine()
        f = Finding(
            id="X-001", title="Test", severity="high",
            evidence="Evidence.", suggested_action=SuggestedAction(summary="Fix.", priority=1),
            priority=1, confidence="high", source_audit="test",
        )
        signals = engine.collect([f], affected_url="https://example.com/")
        self.assertEqual(len(signals), 1)
        ranked = engine.rank(signals)
        self.assertEqual(len(ranked), 1)


# ===========================================================================
# ObjectiveMapping tests
# ===========================================================================

class TestObjectiveMapping(unittest.TestCase):
    """Tests for the ObjectiveMapping foundation."""

    def test_map_findings_with_objectives(self) -> None:
        """Provided objectives do NOT auto-map — no unestablished relevance claims."""
        f = _make_finding("MA-001")
        f = Finding(
            id=f.id, title=f.title, severity=f.severity,
            evidence=f.evidence, suggested_action=f.suggested_action,
            priority=f.priority, confidence=f.confidence,
            details=f.details, source_audit="machine_answerability",
        )
        mappings = map_findings_to_objectives([f], target_objectives=["conversion", "seo"])
        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].finding_id, "MA-001")
        self.assertEqual(mappings[0].source_audit, "machine_answerability")
        # Until evidence-based matching exists, no objective is claimed.
        self.assertEqual(mappings[0].affected_objectives, [])

    def test_map_findings_without_objectives(self) -> None:
        """map_findings_to_objectives with no objectives → empty affected list."""
        f = _make_finding("TR-001")
        f = Finding(
            id=f.id, title=f.title, severity=f.severity,
            evidence=f.evidence, suggested_action=f.suggested_action,
            priority=f.priority, confidence=f.confidence,
            details=f.details, source_audit="task_readiness",
        )
        mappings = map_findings_to_objectives([f])
        self.assertEqual(mappings[0].affected_objectives, [])


# ===========================================================================
# AuditReport tests
# ===========================================================================

class TestAuditReport(unittest.TestCase):
    """Tests for the AuditReport builder."""

    def test_build_report_basic(self) -> None:
        """build_report constructs a report with summary."""
        f = _make_finding("MA-001", severity="high")
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[f],
            audit_errors={},
            audits_run=["machine_answerability", "task_readiness"],
        )
        self.assertTrue(report.is_success)
        self.assertEqual(report.summary["total_findings"], 1)
        self.assertEqual(report.summary["audits_attempted"], 2)
        self.assertEqual(report.summary["audits_succeeded"], 2)
        self.assertEqual(report.summary["audits_failed"], 0)

    def test_build_report_with_errors(self) -> None:
        """build_report reflects audit failures in summary."""
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[],
            audit_errors={"engagement": "Runtime error"},
            audits_run=["machine_answerability", "engagement"],
        )
        self.assertEqual(report.summary["audits_failed"], 1)
        self.assertEqual(report.summary["audit_status"]["engagement"], "failed")
        self.assertEqual(report.summary["audit_status"]["machine_answerability"], "completed")

    def test_report_as_dict(self) -> None:
        """AuditReport.as_dict() serializes correctly."""
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[],
            audit_errors={},
            audits_run=[],
            task_intent="buy shoes",
            target_objectives=["conversion"],
        )
        d = report.as_dict()
        self.assertEqual(d["task_intent"], "buy shoes")
        self.assertEqual(d["target_objectives"], ["conversion"])
        self.assertEqual(d["summary"]["total_findings"], 0)
        # Downstream stages serialize by default (empty when not provided).
        self.assertEqual(d["objective_mappings"], [])
        self.assertEqual(d["priority_signals"], [])


# ===========================================================================
# AuditRequest model tests
# ===========================================================================

class TestAuditRequestModel(unittest.TestCase):
    """Tests for AuditRequest and AuditContext models."""

    def test_audit_request_frozen(self) -> None:
        """AuditRequest is frozen — attribute assignment must raise."""
        req = AuditRequest(target_url="https://example.com/")
        with self.assertRaises((AttributeError, TypeError)):
            req.target_url = "https://other.com/"  # type: ignore[misc]

    def test_audit_context_frozen(self) -> None:
        """AuditContext is frozen."""
        ctx = AuditContext(task_intent="test")
        with self.assertRaises((AttributeError, TypeError)):
            ctx.task_intent = "changed"  # type: ignore[misc]

    def test_audit_request_with_all_fields(self) -> None:
        """AuditRequest accepts all fields."""
        req = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(task_intent="test", target_objectives=["obj1"]),
            audits=("machine_answerability",),
            timeout_seconds=5.0,
            user_agent="CustomBot/1.0",
            include_details=True,
        )
        self.assertEqual(req.target_url, "https://example.com/")
        self.assertEqual(req.audits, ("machine_answerability",))
        self.assertEqual(req.timeout_seconds, 5.0)
        self.assertTrue(req.include_details)


# ===========================================================================
# Multi-audit with engagement + freshness integration
# ===========================================================================

class TestMultiAuditIntegration(unittest.TestCase):
    """Integration test: running all 4 available audits together."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_all_four_audits_can_be_selected(
        self, mock_crawl, mock_build, mock_ma, mock_tr
    ) -> None:
        """All 4 registered audits run when selected in options."""
        mock_crawl.return_value = _make_mock_crawl_result()
        mock_build.return_value = _make_mock_site_context()
        mock_ma.return_value = [_make_finding("MA-001")]
        mock_tr.return_value = [_make_finding("TR-001")]

        options = AuditOptions(audits=(
            "machine_answerability",
            "task_readiness",
            "engagement",
            "freshness_corroboration",
        ))
        result = run_audit("https://example.com/", options=options)

        self.assertTrue(result.is_success)
        # MA and TR are mocked and return findings; engagement and freshness
        # run against minimal observation and return [] (no triggers).
        self.assertEqual(len(result.findings), 2)
        source_audits = {f.source_audit for f in result.findings}
        self.assertIn("machine_answerability", source_audits)
        self.assertIn("task_readiness", source_audits)


if __name__ == "__main__":
    unittest.main()
