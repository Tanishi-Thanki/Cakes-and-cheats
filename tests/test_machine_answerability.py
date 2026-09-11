"""Unit tests for Machine Answerability Audit."""

import unittest

from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding
from src.crawler.crawler import FetchedPage
from src.observation.observation import (
    ImageObservation,
    JsonLdObservation,
    LinkObservation,
    PageObservation,
    PageSignals,
    observe_html,
    observe_page,
)


class TestMachineAnswerabilityAudit(unittest.TestCase):
    """Comprehensive test suite for Machine Answerability Audit."""

    def test_clean_page_produces_no_findings(self) -> None:
        """Test 1: Clean, well-formed page produces zero findings."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Pegasus 40 Running Shoes - Official Store</title>
            <meta name="description" content="Shop the Pegasus 40 running shoe with responsive cushioning.">
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
            <h1>Pegasus 40 Running Shoes</h1>
            <p>
                Engineered with dual Zoom Air units and a breathable engineered mesh upper,
                the Pegasus 40 delivers a balanced, energized ride for everyday road runners.
                Available now for ₹12,999 with complimentary shipping.
            </p>
            <img src="/images/pegasus-40.jpg" alt="Pegasus 40 running shoe profile in white and blue">
            <table>
                <tr><th>Weight</th><th>Heel Drop</th></tr>
                <tr><td>285g</td><td>10mm</td></tr>
            </table>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)
        self.assertEqual(findings, [])

    def test_image_heavy_page_with_weak_text(self) -> None:
        """Test 2: Page with images lacking alt text and sparse body text generates image-dependency finding."""
        html = """
        <html>
        <body>
            <h1>Technical Specifications</h1>
            <img src="/chart1.png" alt="">
            <img src="/chart2.png" alt="photo">
            <img src="/chart3.png">
            <p>Click images to zoom.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        img_findings = [f for f in findings if f.id == "MA-IMG-001"]
        self.assertEqual(len(img_findings), 1)
        finding = img_findings[0]
        self.assertEqual(finding.title, "Important page content may be image-dependent")
        self.assertEqual(finding.severity, "medium")
        self.assertEqual(finding.priority, 2)
        self.assertIn("lacking descriptive alt text", finding.evidence)
        self.assertIn("automated systems may have less reliable access", finding.evidence)

    def test_useful_images_with_equivalent_text_no_finding(self) -> None:
        """Test 3: True negative - images with descriptive alt text and body text do not trigger finding."""
        html = """
        <html>
        <body>
            <h1>Size & Measurement Guide</h1>
            <p>
                To measure your shoe size accurately, place a piece of paper on the floor against a wall.
                Stand on the paper with your heel firmly touching the wall, then trace the outline of your foot.
                Measure the distance from the heel to your longest toe in centimeters.
            </p>
            <img src="/measuring.jpg" alt="Step-by-step diagram showing heel placed against wall on paper">
            <img src="/chart.png" alt="Conversion table: Centimeters to UK and US shoe sizes">
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        img_findings = [f for f in findings if f.id == "MA-IMG-001"]
        self.assertEqual(img_findings, [])

    def test_low_text_representation_on_structured_page(self) -> None:
        """Test 4: Structured page with multiple links and layout but minimal text triggers low-text finding."""
        html = """
        <html>
        <body>
            <header>
                <a href="/home">Home</a>
                <a href="/catalog">Catalog</a>
                <a href="/about">About</a>
                <a href="/contact">Contact</a>
                <a href="/support">Support</a>
                <a href="/login">Login</a>
            </header>
            <main>
                <img src="/app-splash.png" alt="App interface">
                <p>Loading application...</p>
            </main>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        txt_findings = [f for f in findings if f.id == "MA-TXT-001"]
        self.assertEqual(len(txt_findings), 1)
        finding = txt_findings[0]
        self.assertEqual(finding.title, "Low textual representation on structured page")
        self.assertEqual(finding.severity, "medium")
        self.assertEqual(finding.priority, 2)
        self.assertIn("substantial structure", finding.evidence)

    def test_minimal_legitimate_page_no_false_positive(self) -> None:
        """Test 5: True negative - minimal landing page with little structure does not trigger false positive."""
        html = """
        <html>
        <head><title>Search</title></head>
        <body>
            <h1>Minimalist Portal</h1>
            <p>Coming soon in November 2026.</p>
            <a href="/notify">Notify Me</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        txt_findings = [f for f in findings if f.id == "MA-TXT-001"]
        self.assertEqual(txt_findings, [])

    def test_conflicting_visible_and_json_ld_price(self) -> None:
        """Test 6: Discrepancy between visible text price and JSON-LD price generates representation conflict."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Leather Boots",
                "offers": {
                    "@type": "Offer",
                    "price": 12999,
                    "priceCurrency": "INR"
                }
            }
            </script>
        </head>
        <body>
            <h1>Leather Boots</h1>
            <p>Handcrafted genuine Italian leather boots.</p>
            <p class="sale-price">Special Discounted Price: ₹9,999</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        conflict_findings = [f for f in findings if f.id == "MA-REP-001"]
        self.assertEqual(len(conflict_findings), 1)
        finding = conflict_findings[0]
        self.assertEqual(finding.title, "Conflicting page representations may reduce machine confidence")
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.priority, 1)
        self.assertIn("12999", finding.evidence)
        self.assertIn("₹9,999", finding.evidence)

    def test_matching_visible_and_json_ld_price(self) -> None:
        """Test 7: True negative - consistent pricing between JSON-LD and visible text produces no conflict."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Noise Cancelling Headphones",
                "offers": {
                    "@type": "Offer",
                    "price": "14999",
                    "priceCurrency": "INR"
                }
            }
            </script>
        </head>
        <body>
            <h1>Noise Cancelling Headphones</h1>
            <p>Industry leading noise cancellation for ₹14,999.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        conflict_findings = [f for f in findings if f.id.startswith("MA-REP")]
        self.assertEqual(conflict_findings, [])

    def test_malformed_json_ld_does_not_crash(self) -> None:
        """Test 8: Broken JSON-LD is preserved and does not crash the audit."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
                { "name": "Broken", bad_syntax: ??? }
            </script>
        </head>
        <body>
            <h1>Valid Visible Content</h1>
            <p>This page has normal visible content that should be audited safely.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        self.assertFalse(obs.json_ld[0].is_valid)

        # Audit must execute smoothly without raising exception
        findings = audit_machine_answerability(obs)
        self.assertIsInstance(findings, list)

    def test_missing_json_ld_does_not_report_problem(self) -> None:
        """Test 9: True negative - absence of JSON-LD alone must not produce an audit finding."""
        html = """
        <html>
        <head><title>Handmade Pottery</title></head>
        <body>
            <h1>Handmade Pottery</h1>
            <p>
                Each piece of ceramics is crafted by hand on a traditional wheel,
                bisque fired at 1000 degrees Celsius, glazed, and final fired at 1220 degrees.
                We focus on functional stoneware for daily culinary use.
            </p>
        </body>
        </html>
        """
        obs = observe_html(html)
        self.assertEqual(len(obs.json_ld), 0)

        findings = audit_machine_answerability(obs)
        # Missing structured data is not a machine answerability defect
        self.assertEqual(findings, [])

    def test_missing_context_around_structured_entity(self) -> None:
        """Test structured data entity missing from visible text produces context finding."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "UltraBoost 22 Running Shoes"
            }
            </script>
        </head>
        <body>
            <p>Cart (0 items)</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        ctx_findings = [f for f in findings if f.id == "MA-CTX-001"]
        self.assertEqual(len(ctx_findings), 1)
        finding = ctx_findings[0]
        self.assertEqual(finding.title, "Structured information lacks visible page context")
        self.assertIn("UltraBoost 22 Running Shoes", finding.evidence)
        self.assertEqual(finding.severity, "medium")

    def test_multiple_findings_can_coexist(self) -> None:
        """Test 10: Multiple issues on a single page are all identified."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Complex Widget",
                "offers": { "price": 5000, "priceCurrency": "INR" }
            }
            </script>
        </head>
        <body>
            <nav>
                <a href="/1">1</a><a href="/2">2</a><a href="/3">3</a>
                <a href="/4">4</a><a href="/5">5</a><a href="/6">6</a>
            </nav>
            <img src="/p1.jpg" alt="">
            <img src="/p2.jpg" alt="">
            <p>Price: ₹2,500</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)

        # Expected: Representation conflict (5000 vs 2500) AND Low text on structured page
        finding_ids = {f.id for f in findings}
        self.assertIn("MA-REP-001", finding_ids)
        self.assertIn("MA-TXT-001", finding_ids)

    def test_findings_have_valid_structure_and_priority(self) -> None:
        """Test 11: All generated findings strictly conform to the Finding model contract."""
        html = """
        <html>
        <body>
            <h1>Catalog</h1>
            <img src="/i1.png" alt="">
            <img src="/i2.png" alt="">
            <p>Limited text here.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_machine_answerability(obs)
        self.assertTrue(len(findings) > 0)

        for finding in findings:
            self.assertIsInstance(finding, Finding)
            self.assertTrue(bool(finding.id))
            self.assertTrue(bool(finding.title))
            self.assertIn(finding.severity, ["critical", "high", "medium", "low"])
            self.assertIn(finding.priority, [1, 2, 3])
            self.assertIn(finding.confidence, ["high", "medium", "low"])
            self.assertTrue(len(finding.evidence) > 20)
            self.assertTrue(bool(finding.suggested_action.summary))
            self.assertIn(finding.suggested_action.priority, [1, 2, 3])

    def test_failed_fetched_page_returns_no_findings(self) -> None:
        """Test failed page fetch (e.g., 404/500) gracefully returns empty findings."""
        failed_page = FetchedPage(
            requested_url="https://example.com/not-found",
            final_url="https://example.com/not-found",
            status_code=404,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="HTTP error status 404 (Not Found).",
        )
        obs = observe_page(failed_page)
        findings = audit_machine_answerability(obs)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
