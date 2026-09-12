"""Tests for the unified six-audit pipeline via run_audit() and run_audit_full().

Architecture:
    run_audit()      → fetch once → observe once → all six specialist audits → aggregate
    run_audit_full() → run_audit() → objective mapping → priority engine → final report

Verifies:
1. run_audit() defaults to all six specialist audits.
2. run_audit_full() also defaults to all six specialist audits.
3. All six specialist audits receive the same PageObservation instance (single fetch+observe).
4. An individual audit failure is isolated and does not crash the pipeline.
5. Findings from all six audits reach the unified report.
6. Objective mapping and priority ordering function seamlessly after all six audits run.
7. Explicit audit selection (AuditRequest.audits or AuditOptions.audits) is respected.
8. Final report schema compliance and required finding fields.
"""

from __future__ import annotations


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import json
import unittest
from unittest.mock import MagicMock, patch

from src.audits.audit_request import AuditContext, AuditRequest
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
    run_audit_full,
)
from unittest.mock import MagicMock

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


# Convenience alias — _DEFAULT_AUDITS is the single canonical constant.
_ALL_SIX = _DEFAULT_AUDITS


class TestUnifiedSixAuditPipeline(unittest.TestCase):
    """Test suite for the unified six-audit default pipeline."""

    def _mock_crawled(
        self,
        html: str = "<html><head><title>Test</title></head><body><h1>Heading</h1><p>Body text</p></body></html>",
    ) -> FetchedPage:
        return FetchedPage(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            content_type="text/html",
            html_content=html,
            is_success=True,
            error_message=None,
        )

    def _mock_observation(self) -> PageObservation:
        return PageObservation(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            title="Test",
            signals=PageSignals(
                has_title=True,
                has_meta_description=False,
                has_canonical=False,
                num_links=0,
                num_images=0,
                num_tables=0,
                num_headings=1,
            ),
        )

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_run_audit_defaults_to_all_six_audits(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """1. run_audit() with no options defaults to all six specialist audits."""
        mock_crawl.return_value = self._mock_crawled()

        res_str = run_audit("https://example.com/")
        self.assertTrue(res_str.is_success)
        self.assertEqual(list(res_str.options.audits), list(_ALL_SIX))
        self.assertEqual(len(res_str.options.audits), 6)

        res_req = run_audit(AuditRequest("https://example.com/"))
        self.assertTrue(res_req.is_success)
        self.assertEqual(list(res_req.options.audits), list(_ALL_SIX))
        self.assertEqual(len(res_req.options.audits), 6)

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_run_audit_full_defaults_to_all_six_audits(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """2. run_audit_full() with no options also defaults to all six specialist audits."""
        mock_crawl.return_value = self._mock_crawled()

        res_str, rep_str = run_audit_full("https://example.com/")
        self.assertTrue(res_str.is_success)
        self.assertEqual(list(res_str.options.audits), list(_ALL_SIX))
        self.assertEqual(rep_str.audits_run, list(_ALL_SIX))
        self.assertEqual(len(rep_str.audits_run), 6)

        req = AuditRequest("https://example.com/")
        res_req, rep_req = run_audit_full(req)
        self.assertTrue(res_req.is_success)
        self.assertEqual(list(res_req.options.audits), list(_ALL_SIX))
        self.assertEqual(rep_req.audits_run, list(_ALL_SIX))

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_all_six_audits_receive_same_observation(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """3. All six specialist audits receive the identical PageObservation instance (single fetch)."""
        mock_crawl.return_value = self._mock_crawled()

        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered
        _ensure_optional_audits_registered()

        received_observations: dict[str, list[PageObservation]] = {name: [] for name in _ALL_SIX}

        original_registry = dict(AUDIT_REGISTRY)
        try:
            for name in _ALL_SIX:
                orig_fn = original_registry[name]

                def make_spy(n: str, fn):
                    def spy(obs: PageObservation):
                        received_observations[n].append(obs)
                        return fn(obs)
                    return spy

                AUDIT_REGISTRY[name] = make_spy(name, orig_fn)

            res, _ = run_audit_full("https://example.com/")
            self.assertTrue(res.is_success)

            # Exactly one HTTP fetch for all six audits
            self.assertEqual(mock_crawl.call_count, 1)

            # Each audit invoked exactly once
            for name in _ALL_SIX:
                self.assertEqual(len(received_observations[name]), 1, f"Audit {name} was not called once")

            # All received the EXACT same PageObservation instance
            first_obs = received_observations[_ALL_SIX[0]][0]
            for name in _ALL_SIX[1:]:
                self.assertIs(
                    received_observations[name][0],
                    first_obs,
                    f"Audit {name} did not receive the same observation instance as {_ALL_SIX[0]}",
                )
        finally:
            AUDIT_REGISTRY.clear()
            AUDIT_REGISTRY.update(original_registry)

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_single_audit_failure_isolated_in_full_audit(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """4. Failure in one audit does not crash the pipeline; remaining audits still run."""
        mock_crawl.return_value = self._mock_crawled()

        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered
        _ensure_optional_audits_registered()

        original_registry = dict(AUDIT_REGISTRY)
        try:
            def broken_audit(obs: PageObservation):
                raise RuntimeError("Boom in crawl_render")

            AUDIT_REGISTRY["crawl_render"] = broken_audit

            res, _ = run_audit_full("https://example.com/")
            self.assertTrue(res.is_success)
            self.assertIn("crawl_render", res.audit_errors)
            self.assertIn("Boom in crawl_render", res.audit_errors["crawl_render"])
            # All six audits were selected; the failure is recorded but does not propagate
            self.assertEqual(len(res.options.audits), 6)
            self.assertNotIn("machine_answerability", res.audit_errors)
            self.assertNotIn("engagement", res.audit_errors)
        finally:
            AUDIT_REGISTRY.clear()
            AUDIT_REGISTRY.update(original_registry)

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_findings_from_all_six_audits_reach_unified_report(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """5. Findings produced by each of the six audits flow into the unified report."""
        mock_crawl.return_value = self._mock_crawled()

        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered
        _ensure_optional_audits_registered()

        mock_findings = {
            "machine_answerability": [
                Finding(id="MA-HEAD-001", title="Missing H1", severity="medium", evidence="No H1.",
                        suggested_action=SuggestedAction("Add H1.", priority=2), priority=2)
            ],
            "task_readiness": [
                Finding(id="TR-INT-001", title="Primary CTA broken", severity="high", evidence="href='#'",
                        suggested_action=SuggestedAction("Fix href.", priority=1), priority=1)
            ],
            "crawl_render": [
                Finding(id="CR-CAN-001", title="Missing canonical", severity="low", evidence="No canonical.",
                        suggested_action=SuggestedAction("Add canonical.", priority=3), priority=3)
            ],
            "engagement": [
                Finding(id="ENG-CLUT-001", title="Visual clutter", severity="medium", evidence="Dense banners.",
                        suggested_action=SuggestedAction("Reduce banners.", priority=2), priority=2)
            ],
            "evidence_consistency": [
                Finding(id="EVID-PRC-001", title="Price mismatch", severity="high", evidence="Schema $20, DOM $30.",
                        suggested_action=SuggestedAction("Align prices.", priority=1), priority=1)
            ],
            "freshness_corroboration": [
                Finding(id="FC-DATE-001", title="Outdated timestamp", severity="low", evidence="Modified 2018.",
                        suggested_action=SuggestedAction("Update timestamp.", priority=3), priority=3)
            ],
        }

        original_registry = dict(AUDIT_REGISTRY)
        try:
            for name in _ALL_SIX:
                AUDIT_REGISTRY[name] = lambda obs, n=name: list(mock_findings[n])

            res, rep = run_audit_full("https://example.com/")
            self.assertTrue(res.is_success)

            report_dict = rep.as_final_dict()
            findings = report_dict["findings"]
            found_ids = [f["id"] for f in findings]

            self.assertEqual(len(findings), 6)
            for expected_id in ["MA-HEAD-001", "TR-INT-001", "CR-CAN-001", "ENG-CLUT-001", "EVID-PRC-001", "FC-DATE-001"]:
                self.assertIn(expected_id, found_ids)

            # Verify source_audit attribution covers all six audits
            source_audits = {f["source_audit"] for f in findings}
            self.assertEqual(source_audits, set(_ALL_SIX))
        finally:
            AUDIT_REGISTRY.clear()
            AUDIT_REGISTRY.update(original_registry)

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_objective_mapping_and_priority_ordering_with_all_six_audits(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """6. Objective mapping and priority engine work correctly after all six audits run."""
        mock_crawl.return_value = self._mock_crawled()

        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered
        _ensure_optional_audits_registered()

        mock_findings = {
            "machine_answerability": [
                Finding(id="MA-HEAD-001", title="Missing H1", severity="low", evidence="No H1.",
                        suggested_action=SuggestedAction("Add H1.", priority=3), priority=3)
            ],
            "task_readiness": [
                Finding(id="TR-INT-001", title="Buy now button unnavigable", severity="high",
                        evidence="Checkout action is disabled.",
                        suggested_action=SuggestedAction("Fix checkout button.", priority=1), priority=1)
            ],
            "crawl_render": [],
            "engagement": [],
            "evidence_consistency": [],
            "freshness_corroboration": [],
        }

        original_registry = dict(AUDIT_REGISTRY)
        try:
            for name in _ALL_SIX:
                AUDIT_REGISTRY[name] = lambda obs, n=name: list(mock_findings[n])

            req = AuditRequest(
                target_url="https://example.com/",
                context=AuditContext(
                    task_intent="purchase running shoes",
                    target_objectives=["transaction_completion"],
                ),
            )
            res, rep = run_audit_full(req)
            self.assertTrue(res.is_success)

            d = rep.as_final_dict()
            findings = d["findings"]
            self.assertEqual(len(findings), 2)

            # The direct task blocker must be ordered first
            first = findings[0]
            self.assertEqual(first["id"], "TR-INT-001")
            self.assertEqual(first["task_relevance"], "direct_blocker")
            self.assertIn("transaction_completion", first["affected_objectives"])
            self.assertTrue(len(first["priority_reason"]) > 0)
        finally:
            AUDIT_REGISTRY.clear()
            AUDIT_REGISTRY.update(original_registry)

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_explicit_audit_selection_not_overridden(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """7. Explicit audit selections (via AuditRequest or AuditOptions) are respected."""
        mock_crawl.return_value = self._mock_crawled()

        # Via AuditRequest.audits — run_audit
        req = AuditRequest("https://example.com/", audits=("engagement", "evidence_consistency"))
        res_req = run_audit(req)
        self.assertEqual(list(res_req.options.audits), ["engagement", "evidence_consistency"])

        # Via AuditRequest.audits — run_audit_full
        res_full, rep_full = run_audit_full(req)
        self.assertEqual(list(res_full.options.audits), ["engagement", "evidence_consistency"])
        self.assertEqual(rep_full.audits_run, ["engagement", "evidence_consistency"])

        # Via AuditOptions.audits — run_audit
        opts = AuditOptions(audits=("crawl_render",))
        res_opts = run_audit("https://example.com/", opts)
        self.assertEqual(list(res_opts.options.audits), ["crawl_render"])

        # Via AuditOptions.audits — run_audit_full
        res_opts_full, rep_opts_full = run_audit_full("https://example.com/", opts)
        self.assertEqual(list(res_opts_full.options.audits), ["crawl_render"])
        self.assertEqual(rep_opts_full.audits_run, ["crawl_render"])

    @patch("src.orchestrator.build_site_audit_context")
    @patch("src.orchestrator.crawl_site")
    def test_final_report_schema_compliance(self, mock_crawl: MagicMock, mock_build: MagicMock) -> None:
        mock_build.return_value = _make_mock_site_context(obs=self._mock_observation())
        """8. Final report contains site, audited_at, summary, findings, and required finding fields."""
        mock_crawl.return_value = self._mock_crawled()

        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered
        _ensure_optional_audits_registered()

        original_registry = dict(AUDIT_REGISTRY)
        try:
            AUDIT_REGISTRY["machine_answerability"] = lambda obs: [
                Finding(
                    id="MA-001",
                    title="Schema missing",
                    severity="high",
                    evidence="No ld+json found.",
                    suggested_action=SuggestedAction("Add schema", priority=1),
                    priority=1,
                )
            ]
            for name in _ALL_SIX:
                if name != "machine_answerability":
                    AUDIT_REGISTRY[name] = lambda obs: []

            res, rep = run_audit_full("https://example.com/")
            d = rep.as_final_dict()

            # Top-level required keys
            self.assertIn("site", d)
            self.assertIn("audited_at", d)
            self.assertIn("summary", d)
            self.assertIn("findings", d)
            self.assertIn("audits_run", d)

            # audits_run reflects all six
            self.assertEqual(d["audits_run"], list(_ALL_SIX))

            # Summary structure
            self.assertIn("total_findings", d["summary"])
            self.assertIn("high", d["summary"])
            self.assertIn("medium", d["summary"])
            self.assertIn("low", d["summary"])

            # Finding required fields
            self.assertEqual(len(d["findings"]), 1)
            f = d["findings"][0]
            self.assertIn("id", f)
            self.assertIn("title", f)
            self.assertIn("severity", f)
            self.assertIn("evidence", f)
            self.assertIn("suggested_action", f)
            self.assertIn("summary", f["suggested_action"])
            self.assertIn("priority", f["suggested_action"])

            # Valid JSON serializable
            json_str = rep.to_json()
            parsed = json.loads(json_str)
            self.assertEqual(parsed["site"], "https://example.com/")
        finally:
            AUDIT_REGISTRY.clear()
            AUDIT_REGISTRY.update(original_registry)


if __name__ == "__main__":
    unittest.main()
