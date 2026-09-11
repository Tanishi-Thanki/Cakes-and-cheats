"""Unit tests for the Freshness & Corroboration Audit."""

import unittest

from src.audits.freshness_corroboration import audit_freshness_corroboration
from src.audits.models import Finding
from src.crawler.crawler import FetchedPage
from src.observation.observation import observe_html, observe_page


class TestFreshnessCorroborationAudit(unittest.TestCase):
    """Test suite for audit_freshness_corroboration."""

    # -------------------------------------------------------------------------
    # TRUE POSITIVES
    # -------------------------------------------------------------------------

    def test_tp1_conflicting_product_name(self) -> None:
        """TP 1: Conflicting visible product name and JSON-LD product name."""
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
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)

        name_findings = [f for f in findings if f.id == "FC-ENT-001"]
        self.assertEqual(len(name_findings), 1)
        f = name_findings[0]
        self.assertEqual(f.title, "Conflicting entity identity signals")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertIn("Acme Running Shoes", f.evidence)
        self.assertIn("Beta Footwear Ultra", f.evidence)

    def test_tp2_conflicting_brand(self) -> None:
        """TP 2: Conflicting visible brand and JSON-LD brand."""
        html = """
        <html>
        <head>
            <title>Pro Striker Cleats</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Pro Striker Cleats",
                "brand": "Nike"
            }
            </script>
        </head>
        <body>
            <h1>Pro Striker Cleats</h1>
            <p>Brand: Adidas</p>
            <p>High performance football boots for soft ground.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)

        brand_findings = [f for f in findings if f.id == "FC-ENT-002"]
        self.assertEqual(len(brand_findings), 1)
        f = brand_findings[0]
        self.assertEqual(f.title, "Conflicting brand representations")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertIn("Nike", f.evidence)
        self.assertIn("Adidas", f.evidence)

    def test_tp3_conflicting_price(self) -> None:
        """TP 3: Conflicting visible and JSON-LD price."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Trail Runner",
                "offers": {
                    "@type": "Offer",
                    "price": 12999,
                    "priceCurrency": "INR"
                }
            }
            </script>
        </head>
        <body>
            <h1>Trail Runner</h1>
            <p>Price: ₹9,999</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)

        price_findings = [f for f in findings if f.id == "FC-REP-001"]
        self.assertEqual(len(price_findings), 1)
        f = price_findings[0]
        self.assertEqual(f.title, "Conflicting price representations")
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.priority, 1)
        self.assertIn("₹9,999", f.evidence)
        self.assertIn("12999", f.evidence)

    def test_tp4_conflicting_product_attribute(self) -> None:
        """TP 4: Clear conflicting product attribute (e.g. material: Leather vs Canvas)."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Classic Backpack",
                "material": "Leather"
            }
            </script>
        </head>
        <body>
            <h1>Classic Backpack</h1>
            <table>
                <tr><td>Material</td><td>Canvas</td></tr>
                <tr><td>Volume</td><td>20L</td></tr>
            </table>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)

        attr_findings = [f for f in findings if f.id == "FC-ATTR-001"]
        self.assertEqual(len(attr_findings), 1)
        f = attr_findings[0]
        self.assertEqual(f.title, "Conflicting product attribute representations")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertIn("Leather", f.evidence)
        self.assertIn("Canvas", f.evidence)

    def test_tp5_outdated_freshness_on_time_sensitive_page(self) -> None:
        """TP 5: Time-sensitive transactional page with explicit significantly outdated timestamp."""
        html = """
        <html>
        <head>
            <title>Gaming Console Offer</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Gaming Console X",
                "dateModified": "2020-04-15",
                "offers": {
                    "@type": "Offer",
                    "price": 39999,
                    "priceCurrency": "INR",
                    "availability": "https://schema.org/InStock"
                }
            }
            </script>
        </head>
        <body>
            <h1>Gaming Console X</h1>
            <p>Current Price: ₹39,999</p>
            <p>In Stock - Add to Cart</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs, reference_year=2026)

        fresh_findings = [f for f in findings if f.id == "FC-FRSH-001"]
        self.assertEqual(len(fresh_findings), 1)
        f = fresh_findings[0]
        self.assertEqual(f.title, "Limited freshness evidence for time-sensitive information")
        self.assertEqual(f.severity, "low")
        self.assertEqual(f.priority, 3)
        self.assertIn("2020", f.evidence)
        self.assertIn("pricing information", f.evidence)
        # Verify the finding language does not make false/definite claims of staleness
        self.assertNotIn("content is stale", f.evidence.lower())
        self.assertNotIn("this price is outdated", f.evidence.lower())
        self.assertIn("may reduce confidence", f.evidence.lower())
        self.assertIn("maintain explicit current modification dates", f.suggested_action.summary.lower())

    def test_tp6_multiple_independent_problems_coexist(self) -> None:
        """TP 6: Multiple independent consistency problems can coexist on a single page."""
        html = """
        <html>
        <head>
            <title>Speedster Running Shoes</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Speedster Running Shoes",
                "material": "Leather",
                "offers": {
                    "@type": "Offer",
                    "price": 14999,
                    "priceCurrency": "INR"
                }
            }
            </script>
        </head>
        <body>
            <h1>Speedster Running Shoes</h1>
            <p>Price: ₹11,999</p>
            <table>
                <tr><td>Material</td><td>Mesh Fabric</td></tr>
            </table>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)

        finding_ids = {f.id for f in findings}
        self.assertIn("FC-REP-001", finding_ids)   # Price conflict
        self.assertIn("FC-ATTR-001", finding_ids)  # Material conflict

    # -------------------------------------------------------------------------
    # TRUE NEGATIVES
    # -------------------------------------------------------------------------

    def test_tn1_matching_product_name(self) -> None:
        """TN 1: Matching visible and JSON-LD product name produces no ambiguity finding."""
        html = """
        <html>
        <head>
            <title>Nike Pegasus 40 - Official Store</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Nike Pegasus 40"
            }
            </script>
        </head>
        <body>
            <h1>Nike Pegasus 40</h1>
            <p>Standard neutral daily training shoe.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual([f for f in findings if f.id.startswith("FC-ENT")], [])

    def test_tn2_matching_price(self) -> None:
        """TN 2: Matching visible and JSON-LD price produces no conflict finding."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Nike Pegasus 40",
                "offers": { "price": 12999, "priceCurrency": "INR" }
            }
            </script>
        </head>
        <body>
            <h1>Nike Pegasus 40</h1>
            <p>Buy now for ₹12,999.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual([f for f in findings if f.id.startswith("FC-REP")], [])

    def test_tn3_matching_brand(self) -> None:
        """TN 3: Matching brand in JSON-LD and visible text produces no conflict."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Pegasus 40",
                "brand": "Nike"
            }
            </script>
        </head>
        <body>
            <h1>Pegasus 40</h1>
            <p>Brand: Nike</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual([f for f in findings if f.id == "FC-ENT-002"], [])

    def test_tn4_equivalent_formatting_attributes(self) -> None:
        """TN 4: Minor formatting differences in attributes (e.g. 10 mm vs 10mm) do not conflict."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Road Runner",
                "material": "100% Cotton",
                "weight": "285g"
            }
            </script>
        </head>
        <body>
            <h1>Road Runner</h1>
            <table>
                <tr><td>Material</td><td>Cotton</td></tr>
                <tr><td>Weight</td><td>285 grams</td></tr>
            </table>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual([f for f in findings if f.id == "FC-ATTR-001"], [])

    def test_tn5_no_json_ld_produces_no_finding(self) -> None:
        """TN 5: Absence of JSON-LD does not trigger any freshness or corroboration finding."""
        html = """
        <html>
        <head><title>Simple Local Bakery</title></head>
        <body>
            <h1>Simple Local Bakery</h1>
            <p>Fresh artisanal sourdough bread baked every morning.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual(findings, [])

    def test_tn6_historical_article_old_date_no_finding(self) -> None:
        """TN 6: Historical or informational article with old publication date is allowed."""
        html = """
        <html>
        <head>
            <title>The History of Marathons in the 20th Century</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": "The History of Marathons",
                "datePublished": "2012-08-14",
                "dateModified": "2014-01-10"
            }
            </script>
        </head>
        <body>
            <h1>The History of Marathons</h1>
            <p>In 1896, the first modern Olympic marathon was held in Athens...</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs, reference_year=2026)
        self.assertEqual([f for f in findings if f.id == "FC-FRSH-001"], [])

    def test_tn7_normal_product_without_date_metadata_no_finding(self) -> None:
        """TN 7: Normal product page without explicit freshness metadata is NOT declared stale."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Standard Mug",
                "offers": { "price": 499, "priceCurrency": "INR" }
            }
            </script>
        </head>
        <body>
            <h1>Standard Mug</h1>
            <p>Ceramic coffee mug, price: ₹499.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs, reference_year=2026)
        self.assertEqual([f for f in findings if f.id == "FC-FRSH-001"], [])

    def test_tn8_malformed_json_ld_does_not_crash(self) -> None:
        """TN 8: Malformed JSON-LD does not crash the audit."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
                { "name": "Broken", corrupted_syntax: ... }
            </script>
        </head>
        <body>
            <h1>Normal Page</h1>
            <p>Content is intact.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertIsInstance(findings, list)

    def test_tn9_failed_page_returns_empty_list(self) -> None:
        """TN 9: Unsuccessful FetchedPage returns [] without crashing."""
        failed_page = FetchedPage(
            requested_url="https://example.com/broken",
            final_url="https://example.com/broken",
            status_code=500,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="Server error 500",
        )
        obs = observe_page(failed_page)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual(findings, [])

    def test_tn10_vague_wording_differences_no_entity_conflict(self) -> None:
        """TN 10: Vague wording differences (e.g. marketing prefix in title) are not flagged."""
        html = """
        <html>
        <head>
            <title>Buy Nike Pegasus 40 Online Today with Free Shipping!</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Nike Pegasus 40"
            }
            </script>
        </head>
        <body>
            <h1>Nike Pegasus 40</h1>
            <p>Popular everyday road running trainer.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertEqual([f for f in findings if f.id.startswith("FC-ENT")], [])

    def test_all_finding_fields_conformance(self) -> None:
        """Verify that every returned Finding conforms to the contract."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Alpha Shoes",
                "brand": "BrandA",
                "offers": { "price": 9999, "priceCurrency": "INR" }
            }
            </script>
        </head>
        <body>
            <h1>Beta Shoes</h1>
            <p>Brand: BrandB</p>
            <p>Price: ₹4,999</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs)
        self.assertTrue(len(findings) > 0)

        for f in findings:
            self.assertIsInstance(f, Finding)
            self.assertTrue(bool(f.id))
            self.assertTrue(bool(f.title))
            self.assertIn(f.severity, ["critical", "high", "medium", "low"])
            self.assertIn(f.priority, [1, 2, 3])
            self.assertIn(f.confidence, ["high", "medium", "low"])
            self.assertTrue(len(f.evidence) > 15)
            self.assertTrue(bool(f.suggested_action.summary))
            self.assertIn(f.suggested_action.priority, [1, 2, 3])

    def test_freshness_language_does_not_claim_definite_staleness(self) -> None:
        """Verify FC-FRSH-001 evidence and mechanism use cautious non-definitive language."""
        html = """
        <html>
        <head>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Smart Watch",
                "dateModified": "2019-11-20",
                "offers": { "price": 19999, "priceCurrency": "INR" }
            }
            </script>
        </head>
        <body>
            <h1>Smart Watch</h1>
            <p>Available now for ₹19,999.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_freshness_corroboration(obs, reference_year=2026)

        fresh_findings = [f for f in findings if f.id == "FC-FRSH-001"]
        self.assertEqual(len(fresh_findings), 1)
        f = fresh_findings[0]

        # 1. Title must use cautious language
        self.assertEqual(f.title, "Limited freshness evidence for time-sensitive information")

        # 2. Evidence must specify observed content and timestamp
        self.assertIn("pricing information", f.evidence)
        self.assertIn("2019-11-20", f.evidence)

        # 3. Mechanism must qualify reduced confidence rather than declaring definite staleness
        self.assertIn("may reduce confidence", f.evidence)
        self.assertNotIn("is stale", f.evidence.lower())
        self.assertNotIn("outdated price", f.evidence.lower())
        self.assertNotIn("definitely outdated", f.evidence.lower())

        # 4. Suggested action recommends maintaining explicit current modification dates
        self.assertIn("maintain explicit current modification dates", f.suggested_action.summary.lower())

        # 5. Severity must be low, priority 3
        self.assertEqual(f.severity, "low")
        self.assertEqual(f.priority, 3)


if __name__ == "__main__":
    unittest.main()
