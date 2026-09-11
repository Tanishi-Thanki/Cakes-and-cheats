"""Unit tests for Task Readiness Audit.

Tests multi-channel structural signals, false-positive regressions (Scenarios A-F),
and true-positive task readiness defect detection.
"""

import unittest

from src.audits.models import Finding
from src.audits.task_readiness import audit_task_readiness
from src.crawler.crawler import FetchedPage
from src.observation.observation import observe_html, observe_page


class TestTaskReadinessAudit(unittest.TestCase):
    """Comprehensive test suite for Task Readiness Audit."""

    # -------------------------------------------------------------------------
    # REGRESSION TESTS FOR CONFIRMED STRUCTURAL DEFECT FIXES
    # -------------------------------------------------------------------------

    def test_multistep_schema_does_not_rescue_poor_visible_structure(self) -> None:
        """Regression 1: HowTo schema MUST NOT rescue an unstructured visible multi-step text."""
        html = """
        <html>
        <head>
            <title>Registration Guide</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "HowTo",
                "name": "Registration Guide"
            }
            </script>
        </head>
        <body>
            <h1>Registration Guide</h1>
            <p>Please follow Step 1, then Step 2, and finally Step 3 to register.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        proc_findings = [f for f in findings if f.id == "TR-PROC-001"]
        self.assertEqual(len(proc_findings), 1, "TR-PROC-001 MUST be returned when visible headings and navigation links are missing, regardless of HowTo schema.")

    def test_product_with_price_only_still_has_decision_evidence_deficit(self) -> None:
        """Regression 2: Price string alone MUST NOT rescue a product page lacking broader decision evidence."""
        html = """
        <html>
        <head><title>Product Item Model SKU-500</title></head>
        <body>
            <h1>Product Item</h1>
            <p>Price: $50.00</p>
            <a href="/buy">Buy Now</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(len(data_findings), 1, "TR-DATA-001 MUST be returned when a product page contains only title + price + Buy link without specs/description/tables.")

    def test_branded_product_title_with_transactional_evidence(self) -> None:
        """Regression 3: Branded/model h1 (no transactional keywords) with price + availability + Buy link must trigger TR-DATA-001 when decision evidence is sparse."""
        html = """
        <html>
        <head><title>AeroMesh 900 - Official Store</title></head>
        <body>
            <h1>AeroMesh 900</h1>
            <p>$799</p>
            <p>In stock</p>
            <p>Lightweight mesh construction.</p>
            <a href="/buy">Buy Now</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(
            len(data_findings), 1,
            "TR-DATA-001 MUST be returned for branded product pages with price + availability + Buy link but no specs or substantial description.",
        )

    def test_branded_product_title_with_full_decision_evidence_no_finding(self) -> None:
        """Regression 4: Branded/model h1 with price, availability, Buy link, AND specs MUST NOT trigger TR-DATA-001."""
        html = """
        <html>
        <head><title>AeroMesh 900 - Official Store</title></head>
        <body>
            <h1>AeroMesh 900</h1>
            <p>Price: $799 USD</p>
            <p>In stock. Ships within 2 days.</p>
            <h2>Specifications</h2>
            <p>Lightweight mesh construction with carbon-fibre frame. Weight: 280g. Connectivity: Bluetooth 5.3, USB-C.
            Battery life: 30 hours. Available in black and white. Compatible with all standard mounting systems.</p>
            <a href="/buy/aeromesh-900">Buy Now</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(
            data_findings, [],
            "Must NOT generate TR-DATA-001 when branded product page provides full price, availability, and specification details.",
        )

    def test_informational_article_with_price_mention_no_false_positive(self) -> None:
        """Regression 5: An informational article that mentions a price but has no purchase signals MUST NOT trigger TR-DATA-001."""
        html = """
        <html>
        <head><title>How to Budget for Your First Home</title></head>
        <body>
            <h1>How to Budget for Your First Home</h1>
            <p>Buying a home is a major financial milestone. Prices vary widely by region.
            In some cities the average price exceeds $500,000. Understanding down-payment rules
            and mortgage calculations is essential for first-time buyers. Consult a financial
            advisor before committing to any purchase.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(
            data_findings, [],
            "Must NOT generate TR-DATA-001 for informational articles that merely mention a price without transactional structural signals.",
        )

    # -------------------------------------------------------------------------
    # FALSE POSITIVE REGRESSION TESTS (SCENARIOS A - F)
    # -------------------------------------------------------------------------

    def test_fp_scenario_a_product_page_visible_info_no_schema(self) -> None:
        """Scenario A: Product page with clear text, price, specs, use cases, and Buy link, but ZERO JSON-LD."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Ergonomic Desk Chair - Official Store</title></head>
        <body>
            <h1>Ergonomic Desk Chair</h1>
            <p>Designed for all-day lumbar support with breathable mesh back and adjustable armrests.</p>
            <p class="price">Price: ₹14,999 (Inclusive of all taxes)</p>
            <h2>Specifications</h2>
            <ul>
                <li>Weight capacity: 130 kg</li>
                <li>Recline range: 90 to 135 degrees</li>
                <li>Material: High-density foam and steel frame</li>
            </ul>
            <h2>Use Cases</h2>
            <p>Ideal for home offices, corporate workstations, and long gaming sessions.</p>
            <a href="/cart/add?item=chair-01">Buy Now</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)
        self.assertEqual(findings, [])

    def test_fp_scenario_b_search_page_no_searchaction_schema(self) -> None:
        """Scenario B: Search page with visible search feature and functional link, but NO SearchAction JSON-LD."""
        html = """
        <html>
        <head><title>Search Catalog</title></head>
        <body>
            <h1>Search Products</h1>
            <p>Enter your search terms below or browse by category.</p>
            <a href="/search?q=catalog">Browse Search Catalog</a>
            <a href="/categories">All Categories</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        form_findings = [f for f in findings if f.id == "TR-FORM-001"]
        self.assertEqual(form_findings, [], "Must NOT generate TR-FORM-001 for functional search page lacking SearchAction schema.")

    def test_fp_scenario_c_multistep_registration_no_howto_schema(self) -> None:
        """Scenario C: Multi-step registration with <h2>Step 1</h2>, clear instructions, and links, but NO HowTo schema."""
        html = """
        <html>
        <head><title>Account Registration Steps</title></head>
        <body>
            <h1>Registration Steps</h1>
            <h2>Step 1: Account Information</h2>
            <p>Enter your email address and create a secure password.</p>
            <h2>Step 2: Profile Verification</h2>
            <p>Verify your phone number via SMS code.</p>
            <h2>Step 3: Complete Setup</h2>
            <p>Set your preferences and finish registration.</p>
            <a href="/register/step1">Get Started</a>
            <a href="/register/step2">Continue Registration</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        proc_findings = [f for f in findings if f.id == "TR-PROC-001"]
        self.assertEqual(proc_findings, [], "Must NOT generate TR-PROC-001 when visible headings and navigation links structure the steps.")

    def test_fp_scenario_d_authenticated_service_with_login_link(self) -> None:
        """Scenario D: Gated service stating authentication required, with a clear login link, but NO auth schema."""
        html = """
        <html>
        <head><title>Member Dashboard</title></head>
        <body>
            <h1>Customer Portal</h1>
            <p>Authentication required to access account settings and order history.</p>
            <p>Please log in to continue.</p>
            <a href="/auth/login">Sign In</a>
            <a href="/help">Help Center</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        auth_findings = [f for f in findings if f.id == "TR-AUTH-001"]
        self.assertEqual(auth_findings, [], "Must NOT generate TR-AUTH-001 when a clear HTML login link is provided.")

    def test_fp_scenario_e_product_incomplete_schema_strong_visible_text(self) -> None:
        """Scenario E: Incomplete Product JSON-LD (missing priceCurrency), but visible text contains full price & specs."""
        html = """
        <html>
        <head>
            <title>Bluetooth Speaker</title>
            <script type="application/ld+json">
            {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": "Bluetooth Speaker",
                "offers": {
                    "@type": "Offer",
                    "price": "2999"
                }
            }
            </script>
        </head>
        <body>
            <h1>Bluetooth Speaker</h1>
            <p>Portable waterproof speaker with 12-hour battery life.</p>
            <p>Special Offer Price: $49.99 USD - In Stock</p>
            <h2>Specifications</h2>
            <p>Bluetooth 5.3, IPX7 waterproof rating, Type-C charging.</p>
            <a href="/buy/speaker">Buy Now</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(data_findings, [], "Must NOT generate TR-DATA-001 solely because JSON-LD schema is missing specific keys.")

    def test_fp_scenario_f_b2b_service_quote_request_no_fixed_price(self) -> None:
        """Scenario F: Service page with pricing on request, clear service description, and Request Quote CTA."""
        html = """
        <html>
        <head><title>Custom Enterprise Logistics Service</title></head>
        <body>
            <h1>Enterprise Supply Chain Logistics</h1>
            <p>End-to-end global freight forwarding and custom warehouse management solutions for large enterprises.</p>
            <h2>Service Scope</h2>
            <p>Includes dedicated fleet management, customs clearance, and real-time GPS tracking.</p>
            <p class="pricing">Pricing: Custom quotation on request based on shipment volume.</p>
            <a href="/quote/request">Request Quote</a>
            <a href="/contact">Contact Sales</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(data_findings, [], "Must NOT generate TR-DATA-001 for B2B service pages offering quote requests.")

    # -------------------------------------------------------------------------
    # TRUE POSITIVE TESTS
    # -------------------------------------------------------------------------

    def test_tp_unnavigable_action_affordance_tr_int_001(self) -> None:
        """True Positive 1: Critical Add to Cart link has href='#' with no valid HTML fallback link."""
        html = """
        <html>
        <body>
            <h1>Store Item</h1>
            <a href="#">Add to Cart</a>
            <a href="javascript:void(0)">Checkout</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        int_findings = [f for f in findings if f.id == "TR-INT-001"]
        self.assertEqual(len(int_findings), 1)
        finding = int_findings[0]
        self.assertEqual(finding.title, "Critical task action affordance is non-navigable")
        self.assertEqual(finding.severity, "critical")
        self.assertIn("Add to Cart", finding.evidence)

    def test_tp_action_endpoint_deficit_tr_form_001(self) -> None:
        """True Positive 2: Advertised capability with zero target links, text guidance, or endpoints."""
        html = """
        <html>
        <head><title>Catalog - Official Store</title></head>
        <body>
            <h1>Search Products</h1>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        form_findings = [f for f in findings if f.id == "TR-FORM-001"]
        self.assertEqual(len(form_findings), 1)
        finding = form_findings[0]
        self.assertEqual(finding.title, "Advertised task capability lacks actionable endpoint or navigation support")
        self.assertIn("Search Products", finding.evidence)

    def test_tp_unstructured_multistep_workflow_tr_proc_001(self) -> None:
        """True Positive 3: Multi-step process in text but headings are unstructured and no step navigation links exist."""
        html = """
        <html>
        <body>
            <h1>Account Portal</h1>
            <p>Please complete Step 1 registration and then proceed through the registration steps.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        proc_findings = [f for f in findings if f.id == "TR-PROC-001"]
        self.assertEqual(len(proc_findings), 1)
        finding = proc_findings[0]
        self.assertEqual(finding.title, "Multi-step task workflow lacks structural guidance or step navigation")

    def test_tp_unreachable_authentication_barrier_tr_auth_001(self) -> None:
        """True Positive 4: Mandatory login required stated in text, but zero login links exist on page."""
        html = """
        <html>
        <body>
            <h1>Private Workspace</h1>
            <p>Authentication required to access file repository.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        auth_findings = [f for f in findings if f.id == "TR-AUTH-001"]
        self.assertEqual(len(auth_findings), 1)
        finding = auth_findings[0]
        self.assertEqual(finding.title, "Mandatory authentication barrier lacks navigable authentication entry point")

    def test_tp_decision_evidence_deficit_tr_data_001(self) -> None:
        """True Positive 5: Product page with title/heading intent but severe decision evidence deficit across all channels."""
        html = """
        <html>
        <head><title>Product Item Model SKU-900</title></head>
        <body>
            <h1>Product Item</h1>
            <p>Item page.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        data_findings = [f for f in findings if f.id == "TR-DATA-001"]
        self.assertEqual(len(data_findings), 1)
        finding = data_findings[0]
        self.assertEqual(finding.title, "Transactional page exhibits decision evidence deficit across available channels")

    def test_multiple_findings_coexist(self) -> None:
        """True Positive 6: Multiple defects on a single page generate multiple findings."""
        html = """
        <html>
        <body>
            <h1>Checkout Portal</h1>
            <a href="#">Add to Cart</a>
            <p>Authentication required to complete order.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)

        finding_ids = {f.id for f in findings}
        self.assertIn("TR-INT-001", finding_ids)
        self.assertIn("TR-AUTH-001", finding_ids)

    def test_finding_model_contract_compliance(self) -> None:
        """Contract test: Every finding strictly satisfies dataclass field expectations."""
        html = """
        <html>
        <body>
            <h1>Private Workspace</h1>
            <p>Authentication required</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)
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
        """Edge Case: Failed fetch page returns empty list without error."""
        failed_page = FetchedPage(
            requested_url="https://example.com/checkout",
            final_url="https://example.com/checkout",
            status_code=500,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="Internal Server Error",
        )
        obs = observe_page(failed_page)
        findings = audit_task_readiness(obs)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
