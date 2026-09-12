"""Unit tests for the Crawl & Render Audit."""

import sys
from pathlib import Path
import unittest

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from src.audits.crawl_render import audit_crawl_render
from src.audits.models import Finding
from src.crawler.crawler import FetchedPage
from src.observation.observation import PageObservation, observe_html, observe_page


class TestCrawlRenderAudit(unittest.TestCase):
    """Test suite for audit_crawl_render covering true positives, true negatives, and edge cases."""

    # -------------------------------------------------------------------------
    # Rule A: Actual Access Failure (CR-001)
    # -------------------------------------------------------------------------

    def test_tp_access_failure_404(self) -> None:
        """TP 1: HTTP 404 status triggers CR-001 with high severity."""
        failed_page = FetchedPage(
            requested_url="https://example.com/missing",
            final_url="https://example.com/missing",
            status_code=404,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="HTTP error status 404 (Not Found).",
        )
        obs = observe_page(failed_page)
        findings = audit_crawl_render(obs)

        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.id, "CR-001")
        self.assertEqual(f.title, "Page is not reliably crawlable")
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.priority, 1)
        self.assertIn("404", f.evidence)
        self.assertIn("https://example.com/missing", f.evidence)

    def test_tp_access_failure_500(self) -> None:
        """TP 1b: HTTP 500 server error triggers CR-001."""
        failed_page = FetchedPage(
            requested_url="https://example.com/crash",
            final_url="https://example.com/crash",
            status_code=500,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="HTTP error status 500 (Internal Server Error).",
        )
        obs = observe_page(failed_page)
        findings = audit_crawl_render(obs)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "CR-001")
        self.assertEqual(findings[0].severity, "high")

    def test_tn_normal_200_page(self) -> None:
        """TN 2: Clean, normal 200 page produces zero crawl/render findings."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Running Shoes Catalog</title>
            <link rel="canonical" href="https://example.com/catalog">
        </head>
        <body>
            <h1>Running Shoes Catalog</h1>
            <p>
                Browse our curated selection of high-performance road and trail running shoes.
                Each pair is engineered for breathability, stability, and durable cushioning.
                Orders over ₹5,000 include complimentary nationwide shipping.
            </p>
            <nav>
                <a href="/men">Men's</a>
                <a href="/women">Women's</a>
                <a href="/kids">Kids</a>
            </nav>
        </body>
        </html>
        """
        obs = observe_html(html, requested_url="https://example.com/catalog", final_url="https://example.com/catalog")
        findings = audit_crawl_render(obs)
        self.assertEqual(findings, [])

    # -------------------------------------------------------------------------
    # Rule B: Initial HTML / Render Gap (CR-002)
    # -------------------------------------------------------------------------

    def test_tp_render_gap_with_client_placeholder(self) -> None:
        """TP 3: Substantive shell with client-side loading placeholder and minimal text triggers CR-002."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Dynamic Analytics Portal</title></head>
        <body>
            <header>
                <h1>Analytics Dashboard</h1>
                <nav>
                    <a href="/dash">Dashboard</a>
                    <a href="/reports">Reports</a>
                    <a href="/settings">Settings</a>
                    <a href="/logout">Logout</a>
                </nav>
            </header>
            <main>
                <div id="root">Loading application... Please wait.</div>
            </main>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)

        render_findings = [f for f in findings if f.id == "CR-002"]
        self.assertEqual(len(render_findings), 1)
        f = render_findings[0]
        self.assertEqual(f.title, "Limited initial HTML content may require client-side rendering")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertIn("limited extractable content", f.evidence)
        self.assertIn("client-side rendering", f.evidence)
        self.assertIn("client-side execution", f.suggested_action.summary)

    def test_tn_js_heavy_page_with_rich_server_text(self) -> None:
        """TN 4: Page using JavaScript/React that delivers substantive visible text does NOT trigger CR-002."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Product Details</title>
            <script src="/bundle.js"></script>
            <script src="/analytics.js"></script>
        </head>
        <body>
            <h1>Road Running Shoes</h1>
            <nav>
                <a href="/home">Home</a>
                <a href="/shop">Shop</a>
                <a href="/cart">Cart</a>
                <a href="/help">Help</a>
            </nav>
            <p>
                Engineered with lightweight foam and breathable mesh to deliver a smooth and responsive
                ride on paved roads. Features a rubber outsole designed for maximum traction and long-lasting
                durability across varying weather conditions. Available in multiple sizes and colors.
            </p>
            <div id="app"></div>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)

        render_findings = [f for f in findings if f.id == "CR-002"]
        self.assertEqual(render_findings, [])

    # -------------------------------------------------------------------------
    # Rule C: Problematic Redirects (CR-003)
    # -------------------------------------------------------------------------

    def test_tp_redirect_protocol_downgrade(self) -> None:
        """TP 5a: Redirect from HTTPS to plaintext HTTP triggers CR-003."""
        html = "<html><body><h1>Insecure</h1><p>Welcome to plaintext site with plenty of content.</p></body></html>"
        obs = observe_html(
            html,
            requested_url="https://secure.example.com/login",
            final_url="http://insecure.example.com/login",
        )
        findings = audit_crawl_render(obs)

        redirect_findings = [f for f in findings if f.id == "CR-003"]
        self.assertEqual(len(redirect_findings), 1)
        f = redirect_findings[0]
        self.assertEqual(f.title, "Problematic redirect destination or protocol downgrade")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertIn("https://secure.example.com/login", f.evidence)
        self.assertIn("http://insecure.example.com/login", f.evidence)

    def test_tn_normal_redirect_upgrade(self) -> None:
        """TN 5b: Normal HTTP to HTTPS upgrade redirect does NOT trigger CR-003."""
        html = "<html><body><h1>Secure Site</h1><p>Welcome to our secure store with full details.</p></body></html>"
        obs = observe_html(
            html,
            requested_url="http://example.com/store",
            final_url="https://example.com/store",
        )
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-003"], [])

    # -------------------------------------------------------------------------
    # Rule D: Canonical Declarations (CR-004)
    # -------------------------------------------------------------------------

    def test_tp_malformed_canonical_scheme(self) -> None:
        """TP 6a: Canonical URL with unsupported scheme (e.g. javascript:) triggers CR-004."""
        html = """
        <html>
        <head>
            <title>Page</title>
            <link rel="canonical" href="javascript:void(0)">
        </head>
        <body>
            <h1>Page Title</h1>
            <p>Sufficient textual content for testing purposes on this webpage.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)

        canonical_findings = [f for f in findings if f.id == "CR-004"]
        self.assertEqual(len(canonical_findings), 1)
        f = canonical_findings[0]
        self.assertEqual(f.title, "Problematic canonical declaration")
        self.assertEqual(f.severity, "medium")
        self.assertIn("javascript:void(0)", f.evidence)

    def test_tn_missing_canonical_not_flagged(self) -> None:
        """TN 6b: Missing canonical URL alone is NOT reported as an audit finding."""
        html = """
        <html>
        <head><title>No Canonical</title></head>
        <body>
            <h1>No Canonical Tag</h1>
            <p>This page simply does not specify a canonical link element, which is valid.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        self.assertIsNone(obs.canonical_url)
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-004"], [])

    def test_tn_valid_canonical_not_flagged(self) -> None:
        """TN 6c: Valid canonical URL does NOT trigger CR-004."""
        html = """
        <html>
        <head>
            <title>Canonical Page</title>
            <link rel="canonical" href="https://example.com/clean-url">
        </head>
        <body>
            <h1>Page with Clean Canonical</h1>
            <p>Authoritative content description providing full details to crawlers.</p>
        </body>
        </html>
        """
        obs = observe_html(html, final_url="https://example.com/clean-url?utm_source=test")
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-004"], [])

    # -------------------------------------------------------------------------
    # Edge Cases: Minimal Landing Pages & Static Informational Content
    # -------------------------------------------------------------------------

    def test_edge_case_legitimate_minimal_landing_page(self) -> None:
        """Edge Case 7: Minimal landing page with little structure is NOT flagged as a render gap."""
        html = """
        <html>
        <head><title>Coming Soon</title></head>
        <body>
            <h1>Coming Soon</h1>
            <p>Launch in 2026.</p>
            <a href="/notify">Notify</a>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-002"], [])

    def test_edge_case_static_document_with_concise_text(self) -> None:
        """Edge Case 8: Historical / concise document with little text is NOT flagged as render gap."""
        html = """
        <html>
        <head><title>Motto</title></head>
        <body>
            <h1>Company Motto</h1>
            <p>Simplicity and speed.</p>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-002"], [])

    def test_regression_static_navigation_directory_no_cr002(self) -> None:
        """Regression: Legitimate static legal/navigation page with >= 6 links and sparse text must NOT trigger CR-002."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Legal Directory</title></head>
        <body>
            <nav aria-label="Legal Directory">
                <a href="/terms">Terms</a>
                <a href="/privacy">Privacy</a>
                <a href="/cookies">Cookies</a>
                <a href="/security">Security</a>
                <a href="/notices">Notices</a>
                <a href="/legal">Legal</a>
            </nav>
            <footer>
                <p>&copy; 2026 Example Corp.</p>
            </footer>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)
        self.assertEqual([f for f in findings if f.id == "CR-002"], [])

    # -------------------------------------------------------------------------
    # Language Guardrail Verification
    # -------------------------------------------------------------------------

    def test_render_finding_language_does_not_make_unsupported_claims(self) -> None:
        """Language Test 9: CR-002 evidence and mechanism do not claim JS makes the page inaccessible."""
        html = """
        <html>
        <head><title>App Shell</title></head>
        <body>
            <h1>Interactive App</h1>
            <nav>
                <a href="/1">1</a><a href="/2">2</a><a href="/3">3</a>
                <a href="/4">4</a><a href="/5">5</a>
            </nav>
            <div id="app">Loading...</div>
        </body>
        </html>
        """
        obs = observe_html(html)
        findings = audit_crawl_render(obs)

        render_findings = [f for f in findings if f.id == "CR-002"]
        self.assertEqual(len(render_findings), 1)
        f = render_findings[0]

        # Guardrails: Must NOT use definitive anti-JS assertions
        self.assertNotIn("javascript makes this page inaccessible", f.evidence.lower())
        self.assertNotIn("this page cannot be crawled", f.evidence.lower())
        self.assertIn("limited extractable content", f.evidence.lower())
        self.assertIn("may depend on client-side rendering", f.evidence.lower())

    # -------------------------------------------------------------------------
    # Finding Contract Conformance
    # -------------------------------------------------------------------------

    def test_contract_all_finding_fields_conformance(self) -> None:
        """Contract Test 10: Verify all returned Finding objects conform to model specifications."""
        failed_page = FetchedPage(
            requested_url="https://example.com/err",
            final_url="https://example.com/err",
            status_code=503,
            content_type="text/html",
            html_content="",
            is_success=False,
            error_message="Service Unavailable",
        )
        obs = observe_page(failed_page)
        findings = audit_crawl_render(obs)
        self.assertTrue(len(findings) > 0)

        for f in findings:
            self.assertIsInstance(f, Finding)
            self.assertTrue(bool(f.id))
            self.assertTrue(bool(f.title))
            self.assertIn(f.severity, ["critical", "high", "medium", "low"])
            self.assertIn(f.priority, [1, 2, 3])
            self.assertIn(f.confidence, ["high", "medium", "low"])
            self.assertTrue(len(f.evidence) > 20)
            self.assertTrue(bool(f.suggested_action.summary))
            self.assertIn(f.suggested_action.priority, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
