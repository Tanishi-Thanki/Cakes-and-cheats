"""Unit tests for the marketplace Orchestrator (src/orchestrator.py).

Test contract:
    - run_audit() composes fetch_page, observe_page, and audit functions correctly.
    - Crawling and observation each happen exactly once per run.
    - All audits receive the SAME PageObservation object (identity check).
    - Crawler / observation failures return unsuccessful AuditResult with empty findings.
    - Individual audit failures are isolated; other audits continue.
    - Findings from both audits are collected and deduplicated deterministically.
    - Severity/priority supplied by audits are preserved, never invented.
    - Finding objects are never mutated.
    - Finding.details are excluded from as_report_dict() by default (include_details=False).
    - Default audit selection runs both registered audits exactly once each.
    - Audit selection does not execute an audit more than once.
    - No real network calls are made in any test.
"""

import unittest
from unittest.mock import MagicMock, call, patch

from src.audits.models import Finding, SuggestedAction
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
)


# ---------------------------------------------------------------------------
# Shared helpers / factories
# ---------------------------------------------------------------------------

def _make_fetched_page(is_success: bool = True, error_message: str | None = None) -> FetchedPage:
    """Return a minimal FetchedPage with controllable success state."""
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
    """Return a minimal PageObservation with controllable success state."""
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
) -> Finding:
    """Return a minimal Finding for use in test stubs."""
    return Finding(
        id=rule_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Fix it.", priority=priority),
        priority=priority,
        confidence="medium",
        details={"internal_key": "internal_value"},
    )


# ---------------------------------------------------------------------------
# Test: successful orchestration
# ---------------------------------------------------------------------------

class TestRunAuditSuccess(unittest.TestCase):
    """Test 1 — successful orchestration with both audits returning findings."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_successful_orchestration_returns_audit_result(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 1: Successful run returns AuditResult with is_success=True and collected findings."""
        mock_fetch.return_value = _make_fetched_page(is_success=True)
        mock_observe.return_value = _make_observation(is_success=True)
        mock_ma.return_value = [_make_finding("MA-IMG-001", priority=1, severity="high")]
        mock_tr.return_value = [_make_finding("TR-DATA-001", priority=1, severity="high")]

        result = run_audit("https://example.com/")

        self.assertIsInstance(result, AuditResult)
        self.assertTrue(result.is_success)
        self.assertIsNone(result.error_message)
        self.assertEqual(len(result.findings), 2)
        finding_ids = {f.id for f in result.findings}
        self.assertIn("MA-IMG-001", finding_ids)
        self.assertIn("TR-DATA-001", finding_ids)
        self.assertEqual(result.audit_errors, {})


# ---------------------------------------------------------------------------
# Test: same PageObservation passed to all audits (identity)
# ---------------------------------------------------------------------------

class TestSamePageObservation(unittest.TestCase):
    """Tests 2 — both audits receive the exact same PageObservation object."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_both_audits_receive_same_observation_object(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 2: audit_machine_answerability and audit_task_readiness receive the identical obs object."""
        canonical_obs = _make_observation(is_success=True)
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = canonical_obs
        mock_ma.return_value = []
        mock_tr.return_value = []

        run_audit("https://example.com/")

        # Both audits must be called with the same PageObservation instance (identity)
        ma_call_obs = mock_ma.call_args[0][0]
        tr_call_obs = mock_tr.call_args[0][0]
        self.assertIs(ma_call_obs, canonical_obs, "MA must receive the exact same obs object.")
        self.assertIs(tr_call_obs, canonical_obs, "TR must receive the exact same obs object.")
        self.assertIs(ma_call_obs, tr_call_obs, "Both audits must share the same identity.")


# ---------------------------------------------------------------------------
# Test: crawler called exactly once
# ---------------------------------------------------------------------------

class TestCrawlerCalledOnce(unittest.TestCase):
    """Test 3 — fetch_page is called exactly once per run_audit() invocation."""

    @patch("src.orchestrator.audit_task_readiness", return_value=[])
    @patch("src.orchestrator.audit_machine_answerability", return_value=[])
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_crawler_called_exactly_once(self, mock_fetch, mock_observe, _ma, _tr) -> None:
        """Test 3: fetch_page is called exactly once regardless of how many audits run."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()

        run_audit("https://example.com/")

        self.assertEqual(mock_fetch.call_count, 1)


# ---------------------------------------------------------------------------
# Test: observation called exactly once
# ---------------------------------------------------------------------------

class TestObservationCalledOnce(unittest.TestCase):
    """Test 4 — observe_page is called exactly once per run_audit() invocation."""

    @patch("src.orchestrator.audit_task_readiness", return_value=[])
    @patch("src.orchestrator.audit_machine_answerability", return_value=[])
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_observation_called_exactly_once(self, mock_fetch, mock_observe, _ma, _tr) -> None:
        """Test 4: observe_page is called exactly once regardless of how many audits run."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()

        run_audit("https://example.com/")

        self.assertEqual(mock_observe.call_count, 1)


# ---------------------------------------------------------------------------
# Test: failed fetch returns unsuccessful AuditResult
# ---------------------------------------------------------------------------

class TestFetchFailure(unittest.TestCase):
    """Tests 5–6 — unsuccessful FetchedPage produces failure result without running audits."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_failed_fetch_returns_unsuccessful_result(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 5: Network / HTTP failure → is_success=False, findings=[], observe not called."""
        mock_fetch.return_value = _make_fetched_page(
            is_success=False, error_message="HTTP error status 404."
        )

        result = run_audit("https://example.com/missing")

        self.assertFalse(result.is_success)
        self.assertEqual(result.findings, [])
        self.assertIn("404", result.error_message or "")
        mock_observe.assert_not_called()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_non_html_fetch_does_not_run_audits(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 6: Non-HTML / unsuccessful FetchedPage must not trigger audits."""
        mock_fetch.return_value = _make_fetched_page(
            is_success=False, error_message="Rejected non-HTML response."
        )

        result = run_audit("https://example.com/data.json")

        self.assertFalse(result.is_success)
        self.assertEqual(result.findings, [])
        mock_observe.assert_not_called()
        mock_ma.assert_not_called()
        mock_tr.assert_not_called()


# ---------------------------------------------------------------------------
# Test: observation exception
# ---------------------------------------------------------------------------

class TestObservationFailure(unittest.TestCase):
    """Test 7 — observe_page() exception stops the pipeline; audits must not run."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_observation_exception_stops_pipeline(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 7: observe_page() raising → is_success=False, findings=[], audits not called."""
        mock_fetch.return_value = _make_fetched_page(is_success=True)
        mock_observe.side_effect = RuntimeError("Unexpected parse failure.")

        result = run_audit("https://example.com/")

        self.assertFalse(result.is_success)
        self.assertEqual(result.findings, [])
        self.assertIn("parse failure", result.error_message or "")
        mock_ma.assert_not_called()
        mock_tr.assert_not_called()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_observation_is_success_false_stops_pipeline(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 7b: observe_page() returning is_success=False also stops the pipeline."""
        mock_fetch.return_value = _make_fetched_page(is_success=True)
        mock_observe.return_value = _make_observation(
            is_success=False, error_message="Failed to parse HTML document: malformed."
        )

        result = run_audit("https://example.com/")

        self.assertFalse(result.is_success)
        self.assertEqual(result.findings, [])
        mock_ma.assert_not_called()
        mock_tr.assert_not_called()


# ---------------------------------------------------------------------------
# Test: individual audit failure isolation
# ---------------------------------------------------------------------------

class TestAuditFailureIsolation(unittest.TestCase):
    """Tests 8–9 — one audit's exception must not prevent the other from running."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_ma_failure_does_not_prevent_tr(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 8: Machine Answerability exception → TR still runs, MA error recorded."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        mock_ma.side_effect = RuntimeError("MA internal crash.")
        tr_finding = _make_finding("TR-DATA-001")
        mock_tr.return_value = [tr_finding]

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertIn("machine_answerability", result.audit_errors)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].id, "TR-DATA-001")
        mock_tr.assert_called_once()

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_tr_failure_does_not_prevent_ma(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 9: Task Readiness exception → MA still runs, TR error recorded."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        ma_finding = _make_finding("MA-IMG-001")
        mock_ma.return_value = [ma_finding]
        mock_tr.side_effect = RuntimeError("TR internal crash.")

        result = run_audit("https://example.com/")

        self.assertTrue(result.is_success)
        self.assertIn("task_readiness", result.audit_errors)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].id, "MA-IMG-001")
        mock_ma.assert_called_once()


# ---------------------------------------------------------------------------
# Test: findings collected from both audits
# ---------------------------------------------------------------------------

class TestFindingCollection(unittest.TestCase):
    """Test 10 — findings from MA and TR are both present in AuditResult."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_findings_from_both_audits_collected(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 10: Findings from MA and TR are both present in result.findings."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        ma_finding = _make_finding("MA-IMG-001", evidence="MA evidence text here.")
        tr_finding = _make_finding("TR-DATA-001", evidence="TR evidence text here.")
        mock_ma.return_value = [ma_finding]
        mock_tr.return_value = [tr_finding]

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 2)
        ids = {f.id for f in result.findings}
        self.assertIn("MA-IMG-001", ids)
        self.assertIn("TR-DATA-001", ids)


# ---------------------------------------------------------------------------
# Test: deduplication
# ---------------------------------------------------------------------------

class TestDeduplication(unittest.TestCase):
    """Tests 11–12 — deduplication removes exact duplicates but not distinct-evidence same-ID findings."""

    def test_exact_duplicate_is_removed(self) -> None:
        """Test 11: Two findings with the same id and evidence are deduplicated; first is kept."""
        f = _make_finding("TR-INT-001", evidence="Exact same evidence string for both.")
        result = _deduplicate_findings([f, f])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], f)

    def test_same_id_different_evidence_both_preserved(self) -> None:
        """Test 12: Same Finding.id with different evidence produces two distinct findings."""
        f1 = _make_finding("TR-INT-001", evidence="First broken link evidence.")
        f2 = _make_finding("TR-INT-001", evidence="Second broken link evidence, different content.")
        result = _deduplicate_findings([f1, f2])
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], f1)
        self.assertIs(result[1], f2)

    def test_deduplication_preserves_first_occurrence_order(self) -> None:
        """Test 11b: When duplicates exist, first occurrence is retained and order is stable."""
        f_a = _make_finding("MA-IMG-001", evidence="Alpha evidence.")
        f_b = _make_finding("TR-DATA-001", evidence="Beta evidence.")
        f_a_dup = _make_finding("MA-IMG-001", evidence="Alpha evidence.")  # duplicate of f_a
        result = _deduplicate_findings([f_a, f_b, f_a_dup])
        self.assertEqual(len(result), 2)
        self.assertIs(result[0], f_a)
        self.assertIs(result[1], f_b)

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_orchestrator_deduplicates_duplicate_findings(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 11c: Duplicate findings from different audits are reduced to one in AuditResult."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        shared_finding = _make_finding("XX-001", evidence="Shared identical evidence string.")
        mock_ma.return_value = [shared_finding]
        mock_tr.return_value = [shared_finding]  # exact same object (same id + evidence)

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 1)


# ---------------------------------------------------------------------------
# Test: severity/priority preservation
# ---------------------------------------------------------------------------

class TestSeverityPriorityPreservation(unittest.TestCase):
    """Test 13 — orchestrator preserves and never modifies severity/priority from audits."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_severity_and_priority_unchanged(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 13: Severity and priority values from audit are identical in AuditResult."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        finding = _make_finding("TR-INT-001", severity="critical", priority=1)
        mock_ma.return_value = []
        mock_tr.return_value = [finding]

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].severity, "critical")
        self.assertEqual(result.findings[0].priority, 1)


# ---------------------------------------------------------------------------
# Test: Finding objects are not mutated
# ---------------------------------------------------------------------------

class TestFindingImmutability(unittest.TestCase):
    """Test 14 — Finding objects returned from audits are not mutated by the orchestrator."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_finding_objects_are_not_mutated(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 14: Original Finding objects are never mutated by the orchestrator.

        The orchestrator creates tagged copies with source_audit attribution
        rather than mutating the originals. All original fields are preserved
        in the tagged copy.
        """
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        original_finding = _make_finding("MA-IMG-001", evidence="Original evidence text.")
        mock_ma.return_value = [original_finding]
        mock_tr.return_value = []

        result = run_audit("https://example.com/")

        self.assertEqual(len(result.findings), 1)
        tagged = result.findings[0]
        # All original fields are preserved exactly.
        self.assertEqual(tagged.id, "MA-IMG-001")
        self.assertEqual(tagged.title, "Test Finding")
        self.assertEqual(tagged.severity, "high")
        self.assertEqual(tagged.evidence, "Original evidence text.")
        self.assertEqual(tagged.priority, 1)
        self.assertEqual(tagged.confidence, "medium")
        self.assertEqual(tagged.details, {"internal_key": "internal_value"})
        # Source audit is attributed by the orchestrator.
        self.assertEqual(tagged.source_audit, "machine_answerability")
        # Original finding object is not mutated.
        self.assertEqual(original_finding.source_audit, "")
        self.assertEqual(original_finding.id, "MA-IMG-001")


# ---------------------------------------------------------------------------
# Test: include_details gate on as_report_dict()
# ---------------------------------------------------------------------------

class TestIncludeDetailsGate(unittest.TestCase):
    """Test 15 — Finding.details excluded from as_report_dict() by default."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_details_excluded_from_report_by_default(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 15a: as_report_dict() excludes 'details' key per finding when include_details=False."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        finding_with_details = _make_finding("TR-DATA-001")  # has details={"internal_key": ...}
        mock_ma.return_value = []
        mock_tr.return_value = [finding_with_details]

        result = run_audit("https://example.com/")  # default include_details=False

        report = result.as_report_dict()
        for fd in report["findings"]:
            self.assertNotIn("details", fd, "details must be absent when include_details=False")

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_details_present_in_report_when_enabled(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 15b: as_report_dict() includes 'details' key per finding when include_details=True."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        finding_with_details = _make_finding("TR-DATA-001")
        mock_ma.return_value = []
        mock_tr.return_value = [finding_with_details]

        options = AuditOptions(include_details=True)
        result = run_audit("https://example.com/", options=options)

        report = result.as_report_dict()
        for fd in report["findings"]:
            self.assertIn("details", fd)
            self.assertEqual(fd["details"], {"internal_key": "internal_value"})

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_underlying_finding_details_not_stripped(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 15c: The underlying Finding.details is never removed, only gated at serialisation."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        finding_with_details = _make_finding("TR-DATA-001")
        mock_ma.return_value = []
        mock_tr.return_value = [finding_with_details]

        result = run_audit("https://example.com/")  # default include_details=False

        # Finding object itself retains details
        self.assertEqual(result.findings[0].details, {"internal_key": "internal_value"})


# ---------------------------------------------------------------------------
# Test: default audit selection
# ---------------------------------------------------------------------------

class TestDefaultAuditSelection(unittest.TestCase):
    """Test 16 — default AuditOptions runs both registered audits exactly once each."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_default_options_runs_both_audits(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 16a: Default run calls both audit_machine_answerability and audit_task_readiness."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        mock_ma.return_value = []
        mock_tr.return_value = []

        run_audit("https://example.com/")

        mock_ma.assert_called_once()
        mock_tr.assert_called_once()

    def test_default_audits_tuple_covers_both_skills(self) -> None:
        """Test 16b: _DEFAULT_AUDITS covers both 'machine_answerability' and 'task_readiness'."""
        self.assertIn("machine_answerability", _DEFAULT_AUDITS)
        self.assertIn("task_readiness", _DEFAULT_AUDITS)

    def test_audit_registry_contains_both_skills(self) -> None:
        """Test 16c: AUDIT_REGISTRY maps both audit names to callable functions."""
        self.assertIn("machine_answerability", AUDIT_REGISTRY)
        self.assertIn("task_readiness", AUDIT_REGISTRY)
        self.assertTrue(callable(AUDIT_REGISTRY["machine_answerability"]))
        self.assertTrue(callable(AUDIT_REGISTRY["task_readiness"]))


# ---------------------------------------------------------------------------
# Test: audit selection does not execute an audit more than once
# ---------------------------------------------------------------------------

class TestNoDuplicateExecution(unittest.TestCase):
    """Test 17 — audit selection with duplicate names runs each audit only once."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_duplicate_audit_name_runs_only_once(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 17: Specifying 'task_readiness' twice in options.audits runs it exactly once."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        mock_ma.return_value = []
        mock_tr.return_value = []

        options = AuditOptions(audits=("task_readiness", "task_readiness", "machine_answerability"))
        run_audit("https://example.com/", options=options)

        self.assertEqual(mock_tr.call_count, 1, "TR must only run once even if listed twice.")
        self.assertEqual(mock_ma.call_count, 1)


# ---------------------------------------------------------------------------
# Test: no second crawler/observation path introduced
# ---------------------------------------------------------------------------

class TestSingleCrawlerObservationPath(unittest.TestCase):
    """Test 18 — confirm there is no second path that fetches or observes independently."""

    @patch("src.orchestrator.audit_task_readiness")
    @patch("src.orchestrator.audit_machine_answerability")
    @patch("src.orchestrator.observe_page")
    @patch("src.orchestrator.fetch_page")
    def test_single_fetch_and_observe_across_all_audits(
        self, mock_fetch, mock_observe, mock_ma, mock_tr
    ) -> None:
        """Test 18: Regardless of audit count, fetch_page and observe_page each called once."""
        mock_fetch.return_value = _make_fetched_page()
        mock_observe.return_value = _make_observation()
        mock_ma.return_value = []
        mock_tr.return_value = []

        run_audit("https://example.com/")

        self.assertEqual(mock_fetch.call_count, 1, "fetch_page must be called exactly once.")
        self.assertEqual(mock_observe.call_count, 1, "observe_page must be called exactly once.")


# ---------------------------------------------------------------------------
# Test: AuditOptions and AuditResult dataclass contracts
# ---------------------------------------------------------------------------

class TestDataclassContracts(unittest.TestCase):
    """Verify that AuditOptions and AuditResult meet their documented contracts."""

    def test_audit_options_defaults(self) -> None:
        """AuditOptions() without arguments should produce conservative defaults."""
        opts = AuditOptions()
        self.assertIn("machine_answerability", opts.audits)
        self.assertIn("task_readiness", opts.audits)
        self.assertFalse(opts.include_details)
        self.assertGreater(opts.timeout_seconds, 0)

    def test_audit_options_is_immutable(self) -> None:
        """AuditOptions is frozen — attribute mutation must raise."""
        opts = AuditOptions()
        with self.assertRaises((AttributeError, TypeError)):
            opts.include_details = True  # type: ignore[misc]

    def test_audit_result_fields_present(self) -> None:
        """AuditResult must carry requested_url, final_url, status_code, is_success, findings, audit_errors, options."""
        result = AuditResult(
            requested_url="https://x.com/",
            final_url="https://x.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=[],
            audit_errors={},
            options=AuditOptions(),
        )
        self.assertEqual(result.requested_url, "https://x.com/")
        self.assertIsInstance(result.findings, list)
        self.assertIsInstance(result.audit_errors, dict)


# ---------------------------------------------------------------------------
# Test: sorting preserves audit-supplied priority and severity ordering
# ---------------------------------------------------------------------------

class TestFindingSorting(unittest.TestCase):
    """Verify _sort_findings() uses audit-supplied priority/severity only."""

    def test_sort_by_priority_ascending(self) -> None:
        """Higher-priority (lower number) findings should appear first."""
        low_pri = _make_finding("TR-A", priority=2, severity="medium")
        high_pri = _make_finding("TR-B", priority=1, severity="medium")
        result = _sort_findings([low_pri, high_pri])
        self.assertIs(result[0], high_pri)
        self.assertIs(result[1], low_pri)

    def test_sort_by_severity_descending_when_priority_equal(self) -> None:
        """When priority is equal, critical should precede high, high precede medium."""
        medium = _make_finding("TR-A", priority=1, severity="medium")
        critical = _make_finding("TR-B", priority=1, severity="critical")
        high = _make_finding("TR-C", priority=1, severity="high")
        result = _sort_findings([medium, critical, high])
        self.assertEqual(result[0].severity, "critical")
        self.assertEqual(result[1].severity, "high")
        self.assertEqual(result[2].severity, "medium")


if __name__ == "__main__":
    unittest.main()
