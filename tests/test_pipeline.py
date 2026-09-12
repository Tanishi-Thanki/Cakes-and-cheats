"""End-to-end integration test for Crawler -> Observation -> Machine Answerability."""


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest
from unittest.mock import MagicMock, patch

from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding
from src.crawler.crawler import FetchedPage, fetch_page
from src.observation.observation import PageObservation, observe_page


class TestEndToEndPipeline(unittest.TestCase):
    """Integration test verifying composition of Crawler, Observation, and Audit layers."""

    @patch("src.crawler.crawler.requests.get")
    def test_pipeline_with_problematic_page_produces_findings(self, mock_get: MagicMock) -> None:
        """Verify URL -> fetch_page() -> observe_page() -> audit_machine_answerability() -> Finding."""
        target_url = "https://shop.example.com/products/spec-heavy-shoe"
        problematic_html = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <title>Technical Specifications</title>
        </head>
        <body>
            <h1>Shoe Specifications & Sizing</h1>
            <!-- Informational images with empty or generic alt text -->
            <img src="/specs-chart.png" alt="">
            <img src="/size-matrix.jpg" alt="image">
            <img src="/material-breakdown.png">
            <p>Please refer to the images above.</p>
        </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = target_url
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.text = problematic_html
        mock_get.return_value = mock_response

        # Step 1: URL -> fetch_page() -> FetchedPage
        fetched_page: FetchedPage = fetch_page(target_url)
        self.assertIsInstance(fetched_page, FetchedPage)
        self.assertTrue(fetched_page.is_success)
        self.assertEqual(fetched_page.status_code, 200)
        self.assertEqual(fetched_page.final_url, target_url)

        # Step 2: FetchedPage -> observe_page() -> PageObservation
        observation: PageObservation = observe_page(fetched_page)
        self.assertIsInstance(observation, PageObservation)
        self.assertTrue(observation.is_success)
        self.assertEqual(observation.title, "Technical Specifications")
        self.assertEqual(len(observation.images), 3)
        self.assertEqual(observation.h1, ["Shoe Specifications & Sizing"])

        # Step 3: PageObservation -> audit_machine_answerability() -> list[Finding]
        findings: list[Finding] = audit_machine_answerability(observation)
        self.assertIsInstance(findings, list)
        self.assertGreaterEqual(len(findings), 1)

        # Step 4: Validate Finding structure & contract
        for finding in findings:
            self.assertIsInstance(finding, Finding)
            self.assertTrue(bool(finding.id))
            self.assertTrue(bool(finding.title))
            self.assertIn(finding.severity, ["critical", "high", "medium", "low"])
            self.assertTrue(bool(finding.evidence))
            self.assertIsNotNone(finding.suggested_action)
            self.assertTrue(bool(finding.suggested_action.summary))
            self.assertIn(finding.suggested_action.priority, [1, 2, 3])
            self.assertIn(finding.priority, [1, 2, 3])
            self.assertIn(finding.confidence, ["high", "medium", "low"])

        # Specifically verify image-dependency finding was detected
        finding_ids = [f.id for f in findings]
        self.assertIn("MA-IMG-001", finding_ids)

    @patch("src.crawler.crawler.requests.get")
    def test_pipeline_with_clean_page_produces_zero_findings(self, mock_get: MagicMock) -> None:
        """Verify URL -> fetch_page() -> observe_page() -> audit_machine_answerability() on clean page produces []."""
        target_url = "https://shop.example.com/products/pegasus"
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
                "offers": {
                    "@type": "Offer",
                    "price": 12999,
                    "priceCurrency": "INR"
                }
            }
            </script>
        </head>
        <body>
            <h1>Pegasus 40 Performance Running Shoes</h1>
            <p>
                Experience energized road running with dual Zoom Air units and engineered mesh.
                Available today for ₹12,999 with complimentary shipping.
            </p>
            <img src="/images/pegasus-profile.jpg" alt="Side view of Pegasus 40 running shoe in white and navy">
            <table>
                <thead>
                    <tr><th>Specification</th><th>Measurement</th></tr>
                </thead>
                <tbody>
                    <tr><td>Weight</td><td>285 grams</td></tr>
                    <tr><td>Heel Drop</td><td>10 mm</td></tr>
                </tbody>
            </table>
        </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = target_url
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.text = clean_html
        mock_get.return_value = mock_response

        # Execute full end-to-end pipeline
        fetched_page: FetchedPage = fetch_page(target_url)
        observation: PageObservation = observe_page(fetched_page)
        findings: list[Finding] = audit_machine_answerability(observation)

        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
