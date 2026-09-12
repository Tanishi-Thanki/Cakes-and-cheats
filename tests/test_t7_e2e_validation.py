"""P1-T7 end-to-end integration + final P1 validation (fully offline).

Validates the complete P1 stack against two deterministic in-memory fixture
sites (a realistic bakery site and a hostile/edge-case site), plus:
- the bounded crawl -> SiteAuditContext chain (steps 4-5);
- determinism of structural output across repeat runs (step 6);
- six-audit compatibility against crawled PageObservation objects (step 7);
- the full run_audit_full entrypoint smoke test (step 8);
- observed runtime for representative fixtures (step 9);
- regression tests for the two crawl-boundary exception guards added by T7.

No network access, no LLM, no browser automation. Source fixtures are built
inline so the validation is reproducible and self-contained.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import json
import time
import unittest
import urllib.parse
from unittest.mock import patch

import requests

from src.audits.crawl_render import audit_crawl_render
from src.audits.engagement import audit_engagement
from src.audits.evidence_consistency import audit_evidence_consistency
from src.audits.freshness_corroboration import audit_freshness_corroboration
from src.audits.machine_answerability import audit_machine_answerability
from src.audits.task_readiness import audit_task_readiness
from src.crawler.crawler import FetchedPage
from src.crawler.crawl_session import CrawlConfig, crawl_site
from src.crawler.robots_sitemaps import discover_sitemap_urls
from src.crawler.site_context import build_site_audit_context
from src.orchestrator import _DEFAULT_AUDITS, run_audit_full

SCOPE = "https://bakery.test"
HOSTILE = "https://hostile.test"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def ok_page(url: str, html: str, content_type: str = "text/html") -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type=content_type,
        html_content=html,
        is_success=True,
        error_message=None,
    )


def fail_page(url: str, message: str, status=None) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type="",
        html_content="",
        is_success=False,
        error_message=message,
    )


class FakeSite:
    """In-memory site serving pages, robots.txt, and sitemap.xml by path.

    ``failures`` maps path -> FetchedPage and wins over all other routes, so
    oversized / non-HTML / timeout / redirect-cycle pages can be emulated.
    ``robots=None`` / ``sitemap=None`` mean 404 (missing) for those resources.
    """

    def __init__(self, *, pages: dict[str, str], robots=None, sitemap=None, failures=None) -> None:
        self.pages = pages
        self.robots = robots
        self.sitemap = sitemap
        self.failures = failures or {}
        self.fetch_counts: dict[str, int] = {}

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        path = urllib.parse.urlsplit(url).path
        if path in self.failures:
            return self.failures[path]
        if path == "/robots.txt":
            if self.robots is not None:
                return ok_page(url, self.robots, content_type="text/plain")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)
        if path == "/sitemap.xml":
            if self.sitemap is not None:
                return ok_page(url, self.sitemap, content_type="application/xml")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)
        if path in self.pages:
            return ok_page(url, self.pages[path])
        return fail_page(url, "HTTP error status 404 (Not Found).", 404)


def crawl(site: FakeSite, *, start: str = f"{SCOPE}/", **kwargs):
    config = CrawlConfig(start_url=start, **kwargs)
    result = crawl_site(config, fetcher=site.fetch)
    return result, site


# ---------------------------------------------------------------------------
# Realistic fixture: HOME / ABOUT / PRODUCTS / SERVICES / CONTACT
# ---------------------------------------------------------------------------

_REALISTIC_ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://bakery.test/sitemap.xml\n"
_REALISTIC_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<url><loc>{SCOPE}/</loc></url>"
    f"<url><loc>{SCOPE}/about</loc></url>"
    f"<url><loc>{SCOPE}/products</loc></url>"
    f"<url><loc>{SCOPE}/products/product-a</loc></url>"
    f"<url><loc>{SCOPE}/products/product-b</loc></url>"
    f"<url><loc>{SCOPE}/services</loc></url>"
    f"<url><loc>{SCOPE}/contact</loc></url>"
    "</urlset>"
)

_REALISTIC_PAGES: dict[str, str] = {
    "/": (
        "<!DOCTYPE html><html><head>"
        "<title>The Bakery | Home</title>"
        '<meta name="description" content="Fresh bread and pastries baked daily.">'
        '<link rel="canonical" href="https://bakery.test/">'
        '<meta name="robots" content="index,follow">'
        '<meta property="og:title" content="The Bakery">'
        '<meta property="og:description" content="Handcrafted bread and pastries.">'
        '<meta property="og:type" content="website">'
        '<meta property="og:url" content="https://bakery.test/">'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"The Bakery","url":"https://bakery.test/"}'
        "</script></head><body>"
        "<h1>Welcome to The Bakery</h1><p>The best bread in town.</p>"
        '<a href="/about">About</a>'
        '<a href="/about">About again</a>'
        '<a href="/about#team">Team</a>'
        '<a href="/products">Products</a>'
        '<a href="https://bakery.test/services">Services</a>'
        '<a href="https://bakery.test/contact">Contact</a>'
        '<a href="#top">Back to top</a>'
        '<a href="https://external.example/">External</a>'
        '<a href="mailto:hello@bakery.test">Email</a>'
        '<a href="javascript:void(0)">JS</a>'
        "</body></html>"
    ),
    "/about": (
        "<html><head><title>About Us</title>"
        '<link rel="canonical" href="https://bakery.test/about">'
        '<meta name="robots" content="index,follow">'
        "</head><body><h1>About Us</h1>"
        '<a href="/">Home</a>'
        '<a href="/products">Products</a>'
        '<a href="/missing">Missing</a>'
        '<a href="/missing">Missing again</a>'
        "</body></html>"
    ),
    "/products": (
        "<html><head><title>Products</title>"
        "</head><body><h1>Products</h1>"
        '<a href="/products/product-a">Product A</a>'
        '<a href="/products/product-b">Product B</a>'
        '<a href="/">Home</a>'
        '<a href="/services">Services</a>'
        "</body></html>"
    ),
    "/products/product-a": (
        "<html><head><title>Product A</title>"
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Product A",'
        '"offers":{"@type":"Offer","price":"12.00","priceCurrency":"USD"}}'
        "</script></head><body><h1>Product A</h1>"
        '<a href="/products">Products</a>'
        '<a href="/contact">Contact</a>'
        "</body></html>"
    ),
    "/products/product-b": (
        "<html><head><title>Product B (Sold Out)</title>"
        '<meta name="robots" content="noindex,follow">'
        "</head><body><h1>Product B</h1>"
        '<a href="/products">Products</a>'
        "</body></html>"
    ),
    "/services": (
        "<html><head><title>Services</title>"
        "</head><body><h1>Services</h1>"
        '<a href="/">Home</a>'
        '<a href="/contact">Contact</a>'
        '<a href="/about">About</a>'
        "</body></html>"
    ),
    "/contact": (
        "<html><head><title>Contact</title>"
        "</head><body><h1>Contact</h1>"
        '<a href="/">Home</a>'
        '<a href="mailto:hello@bakery.test">Email</a>'
        "</body></html>"
    ),
}


def build_realistic_site() -> FakeSite:
    return FakeSite(
        pages=_REALISTIC_PAGES,
        robots=_REALISTIC_ROBOTS,
        sitemap=_REALISTIC_SITEMAP,
    )


# ---------------------------------------------------------------------------
# Realistic site fixtures
# ---------------------------------------------------------------------------


class TestRealisticCrawlValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.site = build_realistic_site()
        self.result, self.site = crawl(self.site, max_pages=50, max_depth=3)

    def test_01_start_and_normalized_url(self) -> None:
        self.assertEqual(self.result.stats.start_url, f"{SCOPE}/")
        self.assertEqual(self.result.stats.normalized_start_url, f"{SCOPE}/")
        self.assertTrue(self.result.is_success)

    def test_02_robots_and_sitemap_behavior(self) -> None:
        self.assertIsNotNone(self.result.sitemap_discovery)
        discovery = self.result.sitemap_discovery
        self.assertTrue(discovery.robots_fetched)
        self.assertEqual(discovery.robots_sitemap_urls, [f"{SCOPE}/sitemap.xml"])
        self.assertEqual(
            discovery.sitemap_files_parsed,
            [f"{SCOPE}/sitemap.xml"],
        )
        self.assertEqual(
            set(discovery.discovered_urls),
            {f"{SCOPE}{p}" for p in _REALISTIC_PAGES},
        )

    def test_03_internal_and_external_discovery(self) -> None:
        expected = [f"{SCOPE}/", f"{SCOPE}/about", f"{SCOPE}/products",
                    f"{SCOPE}/products/product-a", f"{SCOPE}/products/product-b",
                    f"{SCOPE}/services", f"{SCOPE}/contact", f"{SCOPE}/missing"]
        self.assertEqual(set(self.result.stats.crawled_urls), set(expected))
        self.assertEqual(len(self.result.stats.crawled_urls), 8)
        # External and unsupported schemes never fetched.
        self.assertFalse(any("external.example" in key for key in self.site.fetch_counts))
        self.assertFalse(any(key.startswith("mailto") or key.startswith("javascript") for key in self.site.fetch_counts))
        # Each distinct page fetched exactly once (dedup + fragment elimination).
        for expected_url in expected:
            self.assertEqual(self.site.fetch_counts[expected_url], 1)

    def test_04_broken_page_handled(self) -> None:
        rec = self.result.pages_by_url[f"{SCOPE}/missing"]
        self.assertFalse(rec.is_success)
        self.assertIsNone(rec.observation)
        self.assertIn("404", rec.error_message or "")
        self.assertEqual(self.result.stats.errors, {"http_error": 1})

    def test_05_depths_and_sources(self) -> None:
        self.assertEqual(self.result.stats.max_depth_reached, 2)
        self.assertEqual(
            {p.url: p.depth for p in self.result.pages}["https://bakery.test/missing"],
            2,
        )
        sources = {p.url: tuple(sorted(p.discovery_sources)) for p in self.result.pages}
        self.assertEqual(sources[f"{SCOPE}/"], ("link", "sitemap", "start"))  # self-link adds "link"
        self.assertEqual(sources[f"{SCOPE}/about"], ("link", "sitemap"))
        self.assertEqual(sources[f"{SCOPE}/missing"], ("link",))
        self.assertEqual(self.result.stats.sitemap_urls_discovered, 6)
        self.assertEqual(self.result.stats.sitemap_files_parsed, 1)

    def test_06_observation_metadata_preserved(self) -> None:
        ctx = build_site_audit_context(self.result)
        home = ctx.observations[f"{SCOPE}/"]
        self.assertTrue(home.is_success)
        self.assertEqual(home.title, "The Bakery | Home")
        self.assertTrue(home.has_meta_description)
        self.assertEqual(home.canonical_url, f"{SCOPE}/")
        self.assertEqual(home.robots_meta_directives, ("index", "follow"))
        self.assertEqual(home.og_title, "The Bakery")
        self.assertEqual(home.og_url, f"{SCOPE}/")
        self.assertEqual(len(home.json_ld), 1)
        self.assertTrue(home.json_ld[0].is_valid)
        # Links normalized: fragment-only duplicate collapses into the one target.
        internal = {link.href for link in home.links if link.is_internal}
        self.assertIn(f"{SCOPE}/about", internal)
        self.assertNotIn(f"{SCOPE}/about#team", internal)
        self.assertIn(f"{SCOPE}/products", internal)
        # Robots meta on product-b observed without judgment.
        prod_b = ctx.observations[f"{SCOPE}/products/product-b"]
        self.assertEqual(prod_b.robots_meta_directives, ("noindex", "follow"))
        prod_a = ctx.observations[f"{SCOPE}/products/product-a"]
        self.assertEqual(len(prod_a.json_ld), 1)
        self.assertTrue(prod_a.json_ld[0].is_valid)
        self.assertEqual(
            prod_a.json_ld[0].parsed_data["@type"],
            "Product",
        )

    def test_07_graph_internal_edges_and_relationships(self) -> None:
        ctx = build_site_audit_context(self.result)
        graph = ctx.graph
        self.assertNotIn("https://external.example/", graph.nodes)
        self.assertIn(f"{SCOPE}/missing", graph.nodes)  # target of an internal edge
        self.assertIn((f"{SCOPE}/", f"{SCOPE}/about"), graph.edges)
        self.assertIn((f"{SCOPE}/about", f"{SCOPE}/missing"), graph.edges)
        # Edge count sanity: computed from the fixture link sets (raw href
        # edges, including the two repeated "/about" links on the homepage).
        self.assertEqual(graph.edge_count, 19)
        self.assertEqual(graph.node_count, 8)
        # Incoming/outgoing consistency.
        self.assertEqual(set(graph.incoming[f"{SCOPE}/products"]),
                         {f"{SCOPE}/", f"{SCOPE}/about",
                          f"{SCOPE}/products/product-a", f"{SCOPE}/products/product-b"})
        self.assertIn(f"{SCOPE}/contact", graph.incoming[f"{SCOPE}/"])
        # Links are normalized to distinct edges (raw hrefs, no dedupe beyond
        # normalization); the homepage fragment self-link is a real edge.
        self.assertIn((f"{SCOPE}/", f"{SCOPE}/"), graph.edges)

    def test_08_site_statistics(self) -> None:
        ctx = build_site_audit_context(self.result)
        stats = ctx.stats
        self.assertEqual(stats.total_discovered, 8)
        self.assertEqual(stats.successful_pages, 7)
        self.assertEqual(stats.failed_pages, 1)
        self.assertEqual(stats.graph_nodes, 8)
        self.assertEqual(stats.graph_edges, 19)
        self.assertEqual(stats.external_link_urls, 1)
        self.assertEqual(stats.pages_by_depth, {0: 1, 1: 6, 2: 1})
        self.assertEqual(stats.pages_by_source, {"link": 8, "sitemap": 7, "start": 1})
        self.assertEqual(stats.unvisited_pages, 0)
        self.assertEqual(stats.representative_pages, 7)

    def test_09_representative_selection(self) -> None:
        ctx = build_site_audit_context(self.result)
        self.assertEqual(len(ctx.representative_pages), 7)
        self.assertEqual(ctx.representative_pages[0], f"{SCOPE}/")
        self.assertTrue(ctx.representative_selection[0].reason.startswith("homepage"))
        # Limit respected.
        ctx_limited = build_site_audit_context(self.result, max_representative_pages=3)
        self.assertEqual(len(ctx_limited.representative_pages), 3)
        self.assertEqual(ctx_limited.representative_pages[0], f"{SCOPE}/")
        # Deterministic selection order.
        ctx_again = build_site_audit_context(self.result, max_representative_pages=3)
        self.assertEqual(ctx_again.representative_pages, ctx_limited.representative_pages)

    def test_10_context_serializable_and_site_scoped(self) -> None:
        ctx = build_site_audit_context(self.result)
        json.loads(json.dumps(ctx.to_dict()))
        self.assertEqual(ctx.normalized_start_url, f"{SCOPE}/")
        self.assertEqual(ctx.is_success, True)
        # sitemap_discovered_urls does not leak external URLs.
        for url in ctx.sitemap_discovered_urls:
            self.assertTrue(url.startswith(SCOPE))


# ---------------------------------------------------------------------------
# Hostile / edge-case fixture
# ---------------------------------------------------------------------------


def _hostile_links() -> str:
    # Boundary/edge links first so a small max_pages budget reaches them
    # (the scale loop below floods the frontier otherwise).
    parts: list[str] = [
        '<a href="#top">self fragment</a>',
        '<a href="/">self</a>',
        '<a href="/image.png">binary resource</a>',
        '<a href="/oversized">oversized body</a>',
        '<a href="/slow">slow page</a>',
        '<a href="/loop">redirect loop</a>',
        '<a href="/baddecode">undecodable page</a>',
        '<a href="https://external.example/path">external</a>',
        '<a href="javascript:void(0)">js</a>',
        '<a href="mailto:a@b.test">mail</a>',
        '<a href="data:text/html,x">data</a>',
        '<a href="tel:+1234">tel</a>',
        f'<a href="{HOSTILE}:abc/bad-port">bad port</a>',
        f'<a href="{HOSTILE}/caf\u00e9/\u2603/very-long-path">unicode</a>',
        f'<a href="{HOSTILE}/{'a' * 400}">long</a>',
    ]
    for i in range(250):
        parts.append(f'<a href="/x{i}">x{i}</a>')
        if i < 50:
            parts.append(f'<a href="/x{i}">dup {i}</a>')
            parts.append(f'<a href="/x{i}#frag">frag {i}</a>')
    return "".join(parts)


_HOSTILE_PAGES = {
    "/": (
        "<html><body><h1>Hostile <b>markup</b></div><p>control \x00\x01 was here</p>"
        f"{_hostile_links()}"
        "</body></html>"
    ),
    "/x0": "<html><body><h1>x0</h1></body></html>",
    "/x1": "<html><body><a href=\"/x2\">to x2</a></body></html>",
    "/x2": "<html><body><a href=\"/\">home</a></body></html>",
}

_HOSTILE_FAILURES = {
    "/image.png": fail_page(f"{HOSTILE}/image.png", "Rejected non-HTML response (Content-Type: 'image/png').", 200),
    "/oversized": fail_page(f"{HOSTILE}/oversized", "Response body exceeds the 5000000-byte limit."),
    "/slow": fail_page(f"{HOSTILE}/slow", "Request timed out after 10.0s: read timed out."),
    "/loop": fail_page(f"{HOSTILE}/loop", "Too many redirects while following URL chain: Exceeded 30 redirects."),
    "/baddecode": fail_page(f"{HOSTILE}/baddecode", "Failed to decode response content: boom"),
}


class TestHostileCrawlValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.site = FakeSite(pages=_HOSTILE_PAGES, failures=_HOSTILE_FAILURES)
        # 39 >= 1 (home) + 3 valid + 30 404-only pages (x3..x32) + 5 boundary
        # failures, while all higher /xNN links stay beyond the budget.
        self.result, self.site = crawl(self.site, start=f"{HOSTILE}/", max_pages=39, max_depth=2)

    def test_home_observed_despite_malformed_html(self) -> None:
        home_obs = self.result.pages_by_url[f"{HOSTILE}/"].observation
        self.assertTrue(self.result.pages_by_url[f"{HOSTILE}/"].is_success)
        self.assertIsNotNone(home_obs)
        self.assertTrue(home_obs.is_success)
        self.assertGreaterEqual(home_obs.num_links, 1)

    def test_budget_bounded(self) -> None:
        self.assertLessEqual(self.result.stats.pages_attempted, 39)
        self.assertLessEqual(self.result.stats.urls_discovered, 39)
        self.assertEqual(self.result.stats.pages_attempted, 39)
        # Only home + the three valid x-pages (x0/x1/x2) succeed.
        self.assertEqual(self.result.stats.pages_failed, 35)
        self.assertEqual(self.result.stats.pages_fetched, 4)

    def test_all_error_categories_isolated(self) -> None:
        errors = self.result.stats.errors
        for category in ("http_error", "non_html", "response_too_large",
                         "timeout", "too_many_redirects", "decode_error"):
            self.assertIn(category, errors, f"missing category {category}")
        self.assertIn("http_error", errors)
        self.assertEqual(errors["non_html"], 1)
        self.assertEqual(errors["response_too_large"], 1)
        self.assertEqual(errors["timeout"], 1)
        self.assertEqual(errors["too_many_redirects"], 1)
        self.assertEqual(errors["decode_error"], 1)

    def test_malformed_and_external_links_never_fetched(self) -> None:
        for key in self.site.fetch_counts:
            self.assertFalse(":abc" in key)
            self.assertTrue(key.startswith(f"{HOSTILE}/"), key)
        self.assertFalse(any("external.example" in key for key in self.site.fetch_counts))

    def test_no_page_fetched_more_than_once(self) -> None:
        for key, count in self.site.fetch_counts.items():
            self.assertEqual(count, 1, f"url fetched {count}x: {key}")

    def test_cyclic_and_duplicate_links_terminate(self) -> None:
        self.assertTrue(self.result.is_success)
        self.assertIn(f"{HOSTILE}/x2", self.result.stats.crawled_urls)
        self.assertEqual(self.site.fetch_counts[f"{HOSTILE}/x0"], 1)


# ---------------------------------------------------------------------------
# Determinism (step 6)
# ---------------------------------------------------------------------------


class TestDeterminism(unittest.TestCase):
    def test_realistic_repeat_runs_identical(self) -> None:
        first = crawl(build_realistic_site(), max_pages=50, max_depth=3)
        second = crawl(build_realistic_site(), max_pages=50, max_depth=3)
        ctx1 = build_site_audit_context(first[0])
        ctx2 = build_site_audit_context(second[0])
        self.assertEqual(ctx1.to_dict(), ctx2.to_dict())
        self.assertEqual(first[0].stats.to_dict(), second[0].stats.to_dict())
        self.assertEqual(first[1].fetch_counts, second[1].fetch_counts)

    def test_hostile_repeat_runs_identical(self) -> None:
        # Pages at literal depth 1 (home's children).  The frontier cap keeps
        # this at 39: home + 3 valid + 30 404-only pages + 5 boundary failures.
        def run() -> dict:
            site = FakeSite(pages=_HOSTILE_PAGES, failures=_HOSTILE_FAILURES)
            result, site = crawl(site, start=f"{HOSTILE}/", max_pages=39, max_depth=2)
            ctx = build_site_audit_context(result)
            return {
                "crawled": result.stats.crawled_urls,
                "errors": result.stats.errors,
                "context": ctx.to_dict(),
                "fetches": dict(site.fetch_counts),
            }

        self.assertEqual(run(), run())


# ---------------------------------------------------------------------------
# Six-audit compatibility (step 7)
# ---------------------------------------------------------------------------


class TestSixAuditCompatibility(unittest.TestCase):
    def _observations(self):
        result, _ = crawl(build_realistic_site(), max_pages=50, max_depth=3)
        ctx = build_site_audit_context(result)
        return ctx

    def test_all_six_audits_run_on_crawled_observations(self) -> None:
        ctx = self._observations()
        audits = {
            "machine_answerability": audit_machine_answerability,
            "task_readiness": audit_task_readiness,
            "crawl_render": audit_crawl_render,
            "engagement": audit_engagement,
            "evidence_consistency": audit_evidence_consistency,
            "freshness_corroboration": audit_freshness_corroboration,
        }
        for url in (f"{SCOPE}/", f"{SCOPE}/products/product-a", f"{SCOPE}/contact"):
            obs = ctx.observations[url]
            for name, fn in audits.items():
                with self.subTest(url=url, audit=name):
                    findings = fn(obs)
                    self.assertIsInstance(findings, list)
                    for finding in findings:
                        self.assertIsNotNone(getattr(finding, "id", None))

    def test_context_constructible_without_report_contract_change(self) -> None:
        ctx = self._observations()
        # The existing report contract only ever consumed the audit result.
        self.assertIsInstance(ctx.page_by_url, dict)
        self.assertTrue(set(ctx.observations).issubset(set(ctx.page_by_url)))


# ---------------------------------------------------------------------------
# Entrypoint smoke test (step 8)
# ---------------------------------------------------------------------------


class TestEntrypointSmoke(unittest.TestCase):
    @staticmethod
    def _fixture_requests_get():
        """Serve robots.txt and the home page as real requests.Response objects."""

        def make(url: str, html: str, content_type: str, status: int = 200) -> requests.Response:
            resp = requests.Response()
            resp.status_code = status
            resp.url = url
            resp.headers = {"Content-Type": content_type}
            resp.reason = "OK"
            resp._content = html.encode("utf-8")
            return resp

        def fake_get(url: str, **kwargs):
            path = urllib.parse.urlsplit(url).path
            if path == "/robots.txt":
                return make(url, _REALISTIC_ROBOTS, "text/plain")
            if path == "/":
                return make(url, _REALISTIC_PAGES["/"], "text/html")
            return make(url, "<html>not found</html>", "text/html", 404)

        return fake_get

    @patch("src.crawler.crawler.requests.get")
    def test_run_audit_full_completes_with_valid_json(self, mock_get) -> None:
        mock_get.side_effect = self._fixture_requests_get()

        result, report = run_audit_full(f"{SCOPE}/")

        self.assertTrue(result.is_success)
        self.assertEqual(result.audit_errors, {})
        self.assertEqual(list(result.options.audits), list(_DEFAULT_AUDITS))
        self.assertEqual(len(result.options.audits), 6)

        final = report.as_final_dict()
        for key in ("site", "audited_at", "summary", "findings", "audits_run"):
            self.assertIn(key, final)
        self.assertEqual(final["audits_run"], list(_DEFAULT_AUDITS))
        self.assertIn("total_findings", final["summary"])

        parsed = json.loads(report.to_json())
        self.assertEqual(parsed["site"], f"{SCOPE}/")


# ---------------------------------------------------------------------------
# Runtime observation (step 9) — no tight assertions, just detect regressions
# ---------------------------------------------------------------------------


class TestRuntimeObservation(unittest.TestCase):
    def test_observed_runtime_for_representative_fixtures(self) -> None:
        t0 = time.perf_counter()
        site = FakeSite(pages=_HOSTILE_PAGES, failures=_HOSTILE_FAILURES)
        result, _ = crawl(site, start=f"{HOSTILE}/", max_pages=30, max_depth=2)
        ctx = build_site_audit_context(result)
        hostile_s = time.perf_counter() - t0
        self.assertLess(hostile_s, 10.0)

        t1 = time.perf_counter()
        result2, _ = crawl(build_realistic_site(), max_pages=50, max_depth=3)
        ctx2 = build_site_audit_context(result2)
        realistic_s = time.perf_counter() - t1
        self.assertLess(realistic_s, 10.0)
        # Recorded (printed) for the handoff; not compared as exact values.
        print(f"T7 runtime: hostile+crawl+context={hostile_s:.3f}s "
              f"realistic+crawl+context={realistic_s:.3f}s "
              f"(hostile crawled={len(result.stats.crawled_urls)}, "
              f"realistic crawled={len(result2.stats.crawled_urls)})")


# ---------------------------------------------------------------------------
# Regression tests for the crawl-boundary exception guards added in T7
# ---------------------------------------------------------------------------


class TestCrawlBoundaryGuards(unittest.TestCase):
    def test_raising_fetcher_recorded_not_crashed(self) -> None:
        def boom(url, timeout, user_agent, check_robots):
            raise RuntimeError("fetch exploded")

        result = crawl_site(CrawlConfig(start_url=f"{SCOPE}/"), fetcher=boom)
        self.assertFalse(result.is_success)
        self.assertEqual(result.stats.errors.get("fetch_exception"), 1)
        self.assertIn("fetch exploded", result.error_message or "")
        self.assertEqual(len(result.pages), 1)
        self.assertIsNone(result.pages[0].observation)

    def test_raising_observer_recorded_not_crashed(self) -> None:
        def boom(obs):
            raise RuntimeError("observer exploded")

        site = build_realistic_site()
        result = crawl_site(
            CrawlConfig(start_url=f"{SCOPE}/", max_pages=3, max_depth=1),
            fetcher=site.fetch,
            observer=boom,
        )
        # The start page itself was fetched, so is_success stays True (fetch-level
        # reachability); the observation failure is isolated and recorded for each
        # page the crawl attempts (home + up to two sitemap seeds at depth 1).
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.errors.get("observation_error"), 3)
        self.assertEqual(len(result.pages), 3)
        for record in result.pages:
            self.assertFalse(record.is_success)
            self.assertIsNone(record.observation)
            self.assertIn("Observation raised unexpected exception", record.error_message or "")
        # Fetch-level reachability is preserved: no crawl-wide failure surfaced.
        self.assertIsNone(result.error_message)

    def test_raising_text_fetcher_fails_open_in_discovery(self) -> None:
        def boom(url, timeout, user_agent, check_robots):
            raise RuntimeError("text fetch exploded")

        result = discover_sitemap_urls(SCOPE, fetcher=boom, max_urls=10)
        self.assertIn("robots_fetch_failed", result.errors)
        self.assertTrue(result.conventional_checked)

    def test_raising_sitemap_fetcher_fails_open(self) -> None:
        def fetch(url, timeout, user_agent, check_robots):
            if url.endswith("/robots.txt"):
                return ok_page(url, f"Sitemap: {SCOPE}/sitemap.xml\n", content_type="text/plain")
            raise RuntimeError("sitemap exploded")

        result = discover_sitemap_urls(SCOPE, fetcher=fetch, max_urls=10)
        self.assertIn("sitemap_fetch_failed", result.errors)
        self.assertEqual(result.discovered_urls, [])


if __name__ == "__main__":
    unittest.main()