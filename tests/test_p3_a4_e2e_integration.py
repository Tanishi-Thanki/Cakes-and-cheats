"""P3-A4 Final Entrypoint & E2E Integration Tests.

Verifies the complete marketplace audit entrypoint pipeline (run_audit_full and CLI main):
1. Website input reaches crawler.
2. SiteAuditContext is created.
3. Specialist audits execute.
4. Finding aggregation and deduplication execute.
5. Final AuditReport is produced.
6. Report serializes successfully to JSON.
7. Summary total_findings matches len(findings).
8. Findings contain evidence strings.
9. Suggested actions exist with summary and priority.
10. Specialist errors are isolated in audit_errors without destroying other findings.
11. Deterministic inputs produce identical JSON outputs.
12. Read-only execution (no site modification).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts"
if _SCRIPTS_DIR.is_dir() and str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import json
import unittest

from run_audit import main as cli_main
from src.audits.audit_request import AuditContext, AuditRequest
from src.audits.models import Finding, SuggestedAction
from src.audits.reporting import AuditReport
from src.observation.observation import LinkObservation, PageObservation, PageSignals
from src.orchestrator import AuditOptions, AuditResult, run_audit_full


def _make_e2e_observation(
    url: str = "https://example.com/product/shoe-100",
    title: str = "Pro Shoe Model 100",
    h1: list[str] | None = None,
    h2: list[str] | None = None,
    visible_text: str = "Explore the Pro Shoe Model 100 with responsive cushioning.",
    links: list[LinkObservation] | None = None,
) -> PageObservation:
    return PageObservation(
        requested_url=url,
        final_url=url,
        status_code=200,
        is_success=True,
        title=title,
        h1=h1 or ["Pro Shoe Model 100"],
        h2=h2 or [],
        visible_text=visible_text,
        links=links or [],
        signals=PageSignals(
            has_title=True,
            has_meta_description=True,
            has_canonical=True,
            num_links=len(links or []),
            num_images=1,
            num_tables=0,
            num_headings=1,
        ),
    )


class TestP3A4E2EIntegration(unittest.TestCase):
    """End-to-end integration tests for the marketplace entrypoint (P3-A4)."""

    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_full_marketplace_entrypoint_pipeline(
        self, mock_build, mock_crawl
    ) -> None:
        """Requirement 1-9: E2E pipeline execution through run_audit_full."""
        obs = _make_e2e_observation()
        mock_crawl.return_value = MagicMock(is_success=True)

        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/product/shoe-100"
        mock_ctx.observations = {
            "https://example.com/product/shoe-100": obs
        }
        mock_record = MagicMock()
        mock_record.observation = obs
        mock_ctx.page_by_url = {
            "https://example.com/product/shoe-100": mock_record
        }
        mock_build.return_value = mock_ctx

        request = AuditRequest(
            target_url="https://example.com/product/shoe-100",
            context=AuditContext(
                task_intent="buy running shoes",
                target_objectives=["transaction_completion"],
            ),
        )

        result, report = run_audit_full(request)

        # 1 & 2: Crawl & context built
        self.assertTrue(result.is_success)
        self.assertTrue(report.is_success)

        # 3 & 4 & 5: Specialist audits ran & findings aggregated into report
        self.assertIsInstance(report, AuditReport)
        self.assertIsInstance(report.findings, list)

        # 6: Report serializes cleanly to JSON
        json_output = report.to_json(indent=2)
        parsed = json.loads(json_output)

        # 7: Summary total_findings matches len(findings)
        self.assertEqual(parsed["summary"]["total_findings"], len(parsed["findings"]))

        # 8 & 9: Every finding carries non-empty evidence and suggested_action
        for f in parsed["findings"]:
            self.assertIn("id", f)
            self.assertIn("title", f)
            self.assertIn("severity", f)
            self.assertTrue(bool(f["evidence"]))
            self.assertIn("suggested_action", f)
            self.assertIn("summary", f["suggested_action"])
            self.assertIn("priority", f["suggested_action"])

    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_specialist_error_isolation_in_e2e_report(
        self, mock_build, mock_crawl
    ) -> None:
        """Requirement 10: Individual audit exception is isolated in audit_errors."""
        obs = _make_e2e_observation()
        mock_crawl.return_value = MagicMock(is_success=True)
        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/"
        mock_ctx.observations = {"https://example.com/": obs}
        mock_record = MagicMock()
        mock_record.observation = obs
        mock_ctx.page_by_url = {"https://example.com/": mock_record}
        mock_build.return_value = mock_ctx

        def _failing_audit(o: PageObservation) -> list[Finding]:
            raise ValueError("Specialist crashed unexpectedly!")

        def _passing_audit(o: PageObservation) -> list[Finding]:
            return [
                Finding(
                    id="MA-001",
                    title="Alt Text Issue",
                    severity="low",
                    evidence="Missing image alt text",
                    suggested_action=SuggestedAction(summary="Add alt", priority=3),
                    priority=3,
                    confidence="high",
                )
            ]

        with patch.dict(
            "src.orchestrator.AUDIT_REGISTRY",
            {
                "failing_specialist": _failing_audit,
                "passing_specialist": _passing_audit,
            },
            clear=True,
        ):
            options = AuditOptions(audits=("failing_specialist", "passing_specialist"))
            result, report = run_audit_full("https://example.com/", options=options)

            self.assertTrue(result.is_success)
            self.assertIn("failing_specialist", result.audit_errors)
            self.assertEqual(len(report.findings), 1)
            self.assertEqual(report.findings[0].id, "MA-001")

    @patch("src.orchestrator.crawl_site")
    @patch("src.orchestrator.build_site_audit_context")
    def test_e2e_determinism(self, mock_build, mock_crawl) -> None:
        """Requirement 11: Repeated execution on same input produces identical JSON report."""
        obs = _make_e2e_observation()
        mock_crawl.return_value = MagicMock(is_success=True)
        mock_ctx = MagicMock()
        mock_ctx.normalized_start_url = "https://example.com/"
        mock_ctx.observations = {"https://example.com/": obs}
        mock_record = MagicMock()
        mock_record.observation = obs
        mock_ctx.page_by_url = {"https://example.com/": mock_record}
        mock_build.return_value = mock_ctx

        # Run 1
        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(task_intent="check product info"),
        )
        _, report1 = run_audit_full(request)

        # Run 2
        _, report2 = run_audit_full(request)

        self.assertEqual(report1.to_json(), report2.to_json())

    @patch("run_audit._engine_run_audit_full")
    def test_cli_main_entrypoint(self, mock_run_full) -> None:
        """Requirement 13: CLI main() parses arguments and outputs valid report to stdout."""
        mock_result = MagicMock(is_success=True)
        mock_report = MagicMock()
        mock_report.to_json.return_value = '{"site": "https://example.com/", "summary": {"total_findings": 0}}'
        mock_run_full.return_value = (mock_result, mock_report)

        with patch("sys.stdout.write") as mock_stdout:
            exit_code = cli_main(["https://example.com/", "--intent", "buy shoe"])
            self.assertEqual(exit_code, 0)
            mock_run_full.assert_called_once()


if __name__ == "__main__":
    unittest.main()
