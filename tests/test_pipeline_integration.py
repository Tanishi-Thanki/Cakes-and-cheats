"""Focused integration tests exercising the REAL P1/P2 audit modules end-to-end.

Each test drives actual audit code (crawl_render, engagement, task_readiness,
machine_answerability, freshness_corroboration, evidence_consistency) through
the real integration chain:

    audit -> tag_findings -> aggregate/deduplicate -> build_report -> final JSON

and through the real orchestrator entrypoints (run_audit / run_audit_full) with a
mocked single fetch (no network). This pins the integration contract without adding
a large or redundant suite.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from src.audits.crawl_render import audit_crawl_render
from src.audits.engagement import audit_engagement
from src.audits.evidence_consistency import audit_evidence_consistency
from src.audits.finding_aggregator import aggregate_findings, tag_findings
from src.audits.freshness_corroboration import audit_freshness_corroboration
from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding
from src.audits.reporting import build_report, report_to_json
from src.audits.task_readiness import audit_task_readiness
from src.crawler.crawler import FetchedPage
from src.observation.observation import LinkObservation, PageObservation, observe_page

ALL_REGISTERED_AUDITS = (
    "machine_answerability",
    "task_readiness",
    "crawl_render",
    "engagement",
    "evidence_consistency",
    "freshness_corroboration",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _observe(html: str, *, requested_url: str = "https://example.com/") -> PageObservation:
    """Build a real PageObservation from fixture HTML (no network)."""
    fetched = FetchedPage(
        requested_url=requested_url,
        final_url=requested_url,
        status_code=200,
        content_type="text/html; charset=utf-8",
        html_content=html,
        is_success=True,
    )
    return observe_page(fetched)


def _direct_obs(
    *,
    requested_url: str = "https://example.com/",
    final_url: str | None = None,
    status_code: int = 200,
    is_success: bool = True,
    error_message: str | None = None,
    title: str | None = None,
    h1: list[str] | None = None,
    visible_text: str = "",
    links: list[LinkObservation] | None = None,
    canonical_url: str | None = None,
) -> PageObservation:
    """Construct a PageObservation directly for URL/meta edge cases."""
    return PageObservation(
        requested_url=requested_url,
        final_url=final_url or requested_url,
        status_code=status_code,
        is_success=is_success,
        error_message=error_message,
        title=title,
        h1=h1 or [],
        visible_text=visible_text,
        links=links or [],
        canonical_url=canonical_url,
    )


def _final_report(findings: list[Finding], audits_run: list[str]) -> dict:
    """Aggregation -> report -> JSON as the orchestrator would surface it."""
    report = build_report(
        requested_url="https://example.com/",
        final_url="https://example.com/",
        status_code=200,
        is_success=True,
        error_message=None,
        findings=findings,
        audit_errors={},
        audits_run=audits_run,
    )
    return json.loads(report_to_json(report))


class TestCrawlRenderThroughFullChain(unittest.TestCase):
    """Real CR-001..CR-004 findings survive aggregation and final JSON."""

    def test_cr001_access_failure_preserved(self) -> None:
        obs = _direct_obs(status_code=404, is_success=False, error_message="HTTP error status 404 (Not Found).")
        raw = audit_crawl_render(obs)
        self.assertEqual([f.id for f in raw], ["CR-001"])

        final = _final_report(tag_findings(raw, "crawl_render"), ["crawl_render"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "CR-001")
        self.assertEqual(f["source_audit"], "crawl_render")
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["priority"], 1)
        self.assertEqual(f["confidence"], "high")
        self.assertTrue(f["evidence"])
        self.assertEqual(f["suggested_action"]["summary"], "Resolve the HTTP access failure and verify that the intended public URL returns a successful crawlable response.")
        self.assertEqual(f["details"]["status_code"], 404)

    def test_cr002_render_gap_preserved(self) -> None:
        shell_links = [
            LinkObservation(href=f"https://example.com/a{i}", raw_href=f"/a{i}", anchor_text=f"a{i}", is_internal=True)
            for i in range(4)
        ]
        obs = _direct_obs(
            requested_url="https://example.com/app/dashboard",
            visible_text="Loading application...",
            h1=["Dashboard"],
            links=shell_links,
        )
        raw = audit_crawl_render(obs)
        self.assertEqual([f.id for f in raw], ["CR-002"])

        final = _final_report(tag_findings(raw, "crawl_render"), ["crawl_render"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "CR-002")
        self.assertEqual(f["source_audit"], "crawl_render")
        self.assertEqual(f["severity"], "medium")
        self.assertTrue(f["evidence"])
        self.assertIn("visible_word_count", f["details"])

    def test_cr003_protocol_downgrade_preserved(self) -> None:
        obs = _direct_obs(
            requested_url="https://example.com/",
            final_url="http://example.com/",
        )
        raw = audit_crawl_render(obs)
        self.assertEqual([f.id for f in raw], ["CR-003"])

        final = _final_report(tag_findings(raw, "crawl_render"), ["crawl_render"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "CR-003")
        self.assertEqual(f["source_audit"], "crawl_render")
        self.assertEqual(f["details"]["type"], "protocol_downgrade")

    def test_cr004_invalid_canonical_preserved(self) -> None:
        obs = _direct_obs(canonical_url="javascript:alert(1)")
        raw = audit_crawl_render(obs)
        self.assertEqual([f.id for f in raw], ["CR-004"])

        final = _final_report(tag_findings(raw, "crawl_render"), ["crawl_render"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "CR-004")
        self.assertEqual(f["source_audit"], "crawl_render")
        self.assertIn("scheme", f["details"])


class TestEngagementThroughFullChain(unittest.TestCase):
    """Real ENG-001 finding survives aggregation and final JSON."""

    def test_eng001_generic_landing_preserved(self) -> None:
        obs = _direct_obs(
            requested_url="https://example.com/landing/portal-view",
            title="Welcome",
            h1=["Welcome"],
            visible_text="Welcome to our website portal. Please browse our sections below.",
        )
        raw = audit_engagement(obs)
        eng = [f for f in raw if f.id == "ENG-001"]
        self.assertEqual(len(eng), 1)

        final = _final_report(tag_findings(raw, "engagement"), ["engagement"])
        f = final["findings"][0]
        self.assertEqual(f["source_audit"], "engagement")
        self.assertEqual(f["severity"], "medium")
        self.assertEqual(f["priority"], 2)
        self.assertEqual(f["confidence"], "high")
        self.assertIsInstance(f["details"], dict)


class TestAggregationAcrossRealAudits(unittest.TestCase):
    """Multiple real findings from different audits aggregate with correct tags."""

    def test_all_real_audits_tagged_and_aggregated(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Acme Running Shoes</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Beta Footwear Ultra"
            }
            </script>
        </head>
        <body>
            <h1>Acme Running Shoes</h1>
            <img src="/chart.png" alt="">
            <img src="/diagram.jpg">
            <p>Please refer to the image above.</p>
        </body>
        </html>
        """
        obs = _observe(html)

        runs: dict[str, list[Finding]] = {
            "machine_answerability": audit_machine_answerability(obs),
            "task_readiness": audit_task_readiness(obs),
            "crawl_render": audit_crawl_render(obs),
            "engagement": audit_engagement(obs),
            "freshness_corroboration": audit_freshness_corroboration(obs),
        }

        tagged: list[Finding] = []
        for name, findings in runs.items():
            tagged.extend(tag_findings(findings, name))

        aggregated = aggregate_findings(tagged)
        self.assertGreater(len(aggregated), 0, "At least one real audit should produce a finding.")
        self.assertTrue(all(f.source_audit in ALL_REGISTERED_AUDITS for f in aggregated))

        final = _final_report(aggregated, list(ALL_REGISTERED_AUDITS))
        self.assertEqual(final["summary"]["total_findings"], len(final["findings"]))
        for f in final["findings"]:
            self.assertIn(f["source_audit"], ALL_REGISTERED_AUDITS)
            self.assertTrue(f["id"])
            self.assertIn(f["severity"], ["critical", "high", "medium", "low"])
            self.assertTrue(f["evidence"])
            self.assertIsNotNone(f["suggested_action"])
            self.assertIn(f["priority"], [1, 2, 3])
            self.assertIn(f["confidence"], ["high", "medium", "low"])

        # Dedup/ordering: aggregation never drops distinct real findings.
        self.assertEqual({f["id"] for f in final["findings"]}, {f.id for f in aggregated})


class TestFreshnessDetailsSurviveToJson(unittest.TestCase):
    """Real structured details (entity representations) survive to final report JSON."""

    def test_fc_conflict_details_roundtrip(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Acme Running Shoes</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Beta Footwear Ultra"
            }
            </script>
        </head>
        <body>
            <h1>Acme Running Shoes</h1>
            <p>Designed for everyday jogging and marathon training.</p>
        </body>
        </html>
        """
        obs = _observe(html)
        raw = audit_freshness_corroboration(obs)
        fc = [f for f in raw if f.id == "FC-ENT-001"]
        self.assertEqual(len(fc), 1)
        self.assertTrue(fc[0].details, "Freshness/consistency finding must carry structured details.")

        final = _final_report(tag_findings(raw, "freshness_corroboration"), ["freshness_corroboration"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "FC-ENT-001")
        self.assertEqual(f["source_audit"], "freshness_corroboration")
        self.assertIsInstance(f["details"], dict)
        self.assertGreater(len(f["details"]), 0)


class TestRealOrchestratorPipeline(unittest.TestCase):
    """Smoke via the real orchestrator entrypoints with a mocked single fetch."""

    def _mock_fetch(self, html: str, target_url: str) -> None:
        response = MagicMock()
        response.status_code = 200
        response.url = target_url
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = html
        self._mock_get.return_value = response

    @patch("src.crawler.crawler.requests.get")
    def test_default_execution_runs_ma_and_tr(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        self._mock_fetch("<html><head><title>Example</title></head><body><h1>Example</h1><p>Content here.</p></body></html>", "https://example.com/")
        from src.orchestrator import run_audit_full
        result, report = run_audit_full("https://example.com/")
        self.assertTrue(result.is_success)
        self.assertEqual(list(result.options.audits), ["machine_answerability", "task_readiness"])
        _ = report

    @patch("src.crawler.crawler.requests.get")
    def test_explicit_all_audits_report_and_json(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Acme Running Shoes</title>
            <script type="application/ld+json">
            {"@context": "https://schema.org", "@type": "Product", "name": "Beta Footwear Ultra"}
            </script>
        </head>
        <body>
            <h1>Acme Running Shoes</h1>
            <img src="/chart.png" alt="">
            <p>Please refer to the image above.</p>
        </body>
        </html>
        """
        self._mock_fetch(html, "https://example.com/shoes")
        from src.orchestrator import AuditOptions, run_audit_full
        result, report = run_audit_full(
            "https://example.com/shoes",
            AuditOptions(audits=ALL_REGISTERED_AUDITS),
        )
        self.assertTrue(result.is_success)
        self.assertEqual(list(result.options.audits), list(ALL_REGISTERED_AUDITS))

        d = report.as_final_dict()
        self.assertEqual(d["summary"]["total_findings"], len(d["findings"]))
        self.assertEqual(d["audits_run"], list(ALL_REGISTERED_AUDITS))
        parsed = json.loads(report.to_json())
        self.assertEqual(parsed["findings"], d["findings"])

    @patch("src.crawler.crawler.requests.get")
    def test_one_audit_failure_isolated(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        self._mock_fetch("<html><head><title>T</title></head><body><h1>T</h1><p>Words words words words words.</p></body></html>", "https://example.com/")

        from src.orchestrator import AUDIT_REGISTRY, AuditOptions, run_audit

        def broken_audit(obs: PageObservation) -> list[Finding]:  # noqa: ARG001
            raise RuntimeError("boom in a future evidence audit")

        AUDIT_REGISTRY["probe_failing"] = broken_audit
        try:
            result = run_audit("https://example.com/", AuditOptions(audits=("probe_failing", "crawl_render")))
            self.assertTrue(result.is_success)
            self.assertIn("probe_failing", result.audit_errors)
            self.assertIn("boom", result.audit_errors["probe_failing"])
            self.assertTrue(all(f.source_audit == "crawl_render" for f in result.findings))
        finally:
            del AUDIT_REGISTRY["probe_failing"]

    @patch("src.crawler.crawler.requests.get")
    def test_empty_findings_still_produce_valid_report(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        clean_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <title>Pegasus 40 Performance Running Shoes</title>
            <meta name="description" content="Shop the Pegasus 40 with responsive dual Zoom Air units.">
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Pegasus 40",
                "offers": {"@type": "Offer", "price": 12999, "priceCurrency": "INR"}
            }
            </script>
        </head>
        <body>
            <h1>Pegasus 40 Performance Running Shoes</h1>
            <p>Experience energized road running with dual Zoom Air units and engineered mesh. Available today for \u20b912,999 with complimentary shipping.</p>
            <img src="/images/pegasus-profile.jpg" alt="Side view of Pegasus 40 running shoe in white and navy">
            <nav>
                <a href="/">Home</a>
                <a href="/products">Products</a>
                <a href="/about">About</a>
            </nav>
            <a href="/cart/add">Buy Now</a>
            <table>
                <thead><tr><th>Specification</th><th>Measurement</th></tr></thead>
                <tbody>
                    <tr><td>Weight</td><td>285 grams</td></tr>
                    <tr><td>Heel Drop</td><td>10 mm</td></tr>
                </tbody>
            </table>
        </body>
        </html>
        """
        self._mock_fetch(clean_html, "https://example.com/products/pegasus")
        from src.orchestrator import AuditOptions, run_audit_full
        result, report = run_audit_full(
            "https://example.com/products/pegasus",
            AuditOptions(audits=ALL_REGISTERED_AUDITS),
        )
        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])
        d = report.as_final_dict()
        self.assertEqual(d["summary"]["total_findings"], 0)
        self.assertEqual(d["findings"], [])
        json.loads(report.to_json())


class TestEvidenceConsistencyThroughFullChain(unittest.TestCase):
    """EVID findings flow through aggregation, mapping, priority, and JSON."""

    _CONFLICT_HTML = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Acme Running Shoes</title>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": "Acme Running Shoes",
            "offers": {"@type": "Offer", "price": 949, "priceCurrency": "USD"}
        }
        </script>
    </head>
    <body>
        <h1>Acme Running Shoes</h1>
        <p>Buy the Acme Running Shoes for $999 with free shipping.</p>
    </body>
    </html>
    """

    def _mock_fetch(self, html: str, target_url: str) -> None:
        response = MagicMock()
        response.status_code = 200
        response.url = target_url
        response.headers = {"Content-Type": "text/html; charset=utf-8"}
        response.text = html
        self._mock_get.return_value = response

    def test_evid_price_conflict_survives_chain_to_json(self) -> None:
        obs = _observe(self._CONFLICT_HTML)
        raw = audit_evidence_consistency(obs)
        self.assertEqual([f.id for f in raw], ["EVID-PRC-001"])

        final = _final_report(tag_findings(raw, "evidence_consistency"), ["evidence_consistency"])
        f = final["findings"][0]
        self.assertEqual(f["id"], "EVID-PRC-001")
        self.assertEqual(f["source_audit"], "evidence_consistency")
        self.assertEqual(f["severity"], "high")
        self.assertEqual(f["priority"], 1)
        self.assertEqual(f["confidence"], "high")
        self.assertIsInstance(f["details"]["evidence_units"], list)

    @patch("src.crawler.crawler.requests.get")
    def test_evid_price_conflict_reaches_mapping_priority_and_report(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        self._mock_fetch(self._CONFLICT_HTML, "https://example.com/shoes")
        from src.orchestrator import AuditOptions, run_audit_full
        result, report = run_audit_full(
            "https://example.com/shoes",
            AuditOptions(audits=("evidence_consistency",)),
        )
        self.assertTrue(result.is_success)
        self.assertEqual(list(result.options.audits), ["evidence_consistency"])
        self.assertEqual([f.id for f in result.findings], ["EVID-PRC-001"])

        mappings = report.as_dict()["objective_mappings"]
        self.assertEqual([m["finding_id"] for m in mappings], ["EVID-PRC-001"])
        self.assertEqual([m["source_audit"] for m in mappings], ["evidence_consistency"])

        signals = report.as_dict()["priority_signals"]
        signal = next(s for s in signals if s["finding_id"] == "EVID-PRC-001")
        self.assertEqual(signal["priority"], 1)
        self.assertEqual(signal["severity"], "high")
        self.assertEqual(signal["source_audit"], "evidence_consistency")

        d = report.as_final_dict()
        self.assertEqual(d["summary"]["total_findings"], 1)
        self.assertEqual(d["summary"]["high"], 1)
        f = d["findings"][0]
        self.assertEqual(f["id"], "EVID-PRC-001")
        self.assertEqual(f["source_audit"], "evidence_consistency")
        self.assertIn("evidence_units", f["details"])
        json.loads(report.to_json())

    @patch("src.crawler.crawler.requests.get")
    def test_matching_prices_produce_no_evid_finding(self, mock_get: MagicMock) -> None:
        self._mock_get = mock_get
        match_html = self._CONFLICT_HTML.replace("$999", "$949")
        self._mock_fetch(match_html, "https://example.com/shoes")
        from src.orchestrator import AuditOptions, run_audit_full
        result, report = run_audit_full(
            "https://example.com/shoes",
            AuditOptions(audits=("evidence_consistency",)),
        )
        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])
        d = report.as_final_dict()
        self.assertEqual(d["summary"]["total_findings"], 0)
        self.assertEqual(d["findings"], [])
        json.loads(report.to_json())

    def test_evidence_consistency_registered_as_optional(self) -> None:
        from src.orchestrator import AUDIT_REGISTRY, _ensure_optional_audits_registered

        _ensure_optional_audits_registered()
        self.assertIn("evidence_consistency", AUDIT_REGISTRY)
        self.assertTrue(callable(AUDIT_REGISTRY["evidence_consistency"]))


if __name__ == "__main__":
    unittest.main()