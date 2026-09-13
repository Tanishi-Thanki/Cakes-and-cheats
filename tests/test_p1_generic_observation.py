"""P1 regression tests: generalized observation for arbitrary (non-dataset) sites.

These tests lock in the fix for the Adobe Hackathon P1 bug where dataset/reference
sites produced findings but arbitrary non-dataset websites systematically produced
empty observations. The assertions are purely structural and offline (no network):

- a generic non-dataset page must always produce non-empty observations;
- content components must carry source provenance (kind, tag, id/class,
  nearest heading) so pricing/product cards stay resolvable;
- Twitter Card metadata and JSON-LD must survive the visible-text cleanup;
- crawler failures must be classified into stable machine-readable categories.

They intentionally contain no hostname/dataset-specific fixtures.
"""

from __future__ import annotations

import urllib.parse
import unittest
from unittest.mock import MagicMock, patch

from src.crawler.crawl_session import CrawlConfig, crawl_site
from src.crawler.crawler import FetchedPage, fetch_page
from src.observation.observation import observe_html

SCOPE = "https://genericsite.test"

# ---------------------------------------------------------------------------
# Generic (arbitrary, non-dataset) fixtures
# ---------------------------------------------------------------------------

SAAS_PRICING_HTML = """\
<html><head>
<title>Acme SaaS Pricing</title>
<meta name="description" content="Simple transparent pricing for every team">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Acme Pricing">
<meta name="twitter:description" content="Plans for every team size">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Pro","offers":{"price":"20","priceCurrency":"USD"}}</script>
</head><body>
<h1>Acme Pricing</h1>
<section id="plans" class="pricing-cards">
  <div class="plan card" id="pro">
    <h2>Pro</h2>
    <p>$20 / user / month with unlimited integrations</p>
    <a href="/pricing/pro">Learn more</a>
  </div>
  <div class="plan card" id="business">
    <h2>Business</h2>
    <p>$50 / user / month with priority support</p>
    <a href="/pricing/business">Learn more</a>
  </div>
</section>
<nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
<footer>Copyright 2026 Acme Inc</footer>
</body></html>
"""

CORPORATE_HTML = """\
<html><head><title>Regional Law Firm</title></head>
<body><h1>Welcome</h1><p>We serve clients in three states.</p>
<ul><li>Family law</li><li>Real estate</li><li>Corporate counsel</li></ul>
<a href="/practice-areas">Practice areas</a></body></html>
"""

UNIVERSITY_HTML = """\
<html><head><title>Springfield State University</title></head>
<body><h1>Academics</h1><p>Explore 120 programs across five colleges.</p>
<article><h2>Undergraduate</h2><p>Admissions open until Dec 15.</p></article>
<article><h2>Graduate</h2><p>Research fellowships available.</p></article>
</body></html>
"""


class FakeSite:
    """In-memory site served by the offline fetcher seam."""

    def __init__(self, pages: dict[str, str], failures: dict[str, FetchedPage] | None = None) -> None:
        self.pages = pages
        self.failures = failures or {}

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        path = urllib.parse.urlsplit(url).path or "/"
        if path in self.failures:
            return self.failures[path]
        html = self.pages.get(path, "")
        return FetchedPage(
            requested_url=url,
            final_url=url,
            status_code=200 if html else 404,
            content_type="text/html",
            html_content=html,
            is_success=bool(html),
            error_message=None if html else "HTTP error status 404 (Not Found).",
            failure_category="" if html else "http_error",
        )


class TestGenericPageProducesObservations(unittest.TestCase):
    """CRITICAL REGRESSION: arbitrary non-dataset pages must observe, not empty."""

    def test_saas_pricing_page_crawls_and_observes(self) -> None:
        config = CrawlConfig(
            start_url=f"{SCOPE}/",
            max_pages=5,
            max_depth=1,
            check_robots=False,
            discover_sitemaps=False,
        )
        result = crawl_site(config, fetcher=FakeSite({"/": SAAS_PRICING_HTML}).fetch)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.pages_fetched, 1)
        record = result.pages[0]
        self.assertIsNotNone(record.observation)
        obs = record.observation
        self.assertTrue(obs.is_success)
        self.assertEqual(obs.title, "Acme SaaS Pricing")
        self.assertEqual(obs.h1, ["Acme Pricing"])
        self.assertGreater(len(obs.visible_text), 0)
        self.assertGreater(len(obs.links), 0)

    def test_corporate_and_university_pages_observe_too(self) -> None:
        fixtures = [("/", CORPORATE_HTML), ("/", UNIVERSITY_HTML), ("/", SAAS_PRICING_HTML)]
        for start, html in fixtures:
            result = crawl_site(
                CrawlConfig(start_url=f"{SCOPE}/", discover_sitemaps=False, check_robots=False),
                fetcher=FakeSite({"/": html}).fetch,
            )
            self.assertTrue(result.is_success, msg=f"crawl failed for HTML: {html[:40]}")
            obs = result.pages[0].observation
            self.assertIsNotNone(obs)
            self.assertTrue(obs.is_success)
            self.assertTrue(
                obs.title or obs.h1 or obs.visible_text or obs.links or obs.components,
                msg="observation must not be entirely empty",
            )

    def test_observation_is_not_flat_text_only(self) -> None:
        obs = observe_html(SAAS_PRICING_HTML, requested_url=f"{SCOPE}/")
        self.assertGreater(obs.num_components, 0)
        self.assertGreater(len(obs.json_ld), 0)
        self.assertIsNotNone(obs.twitter_title)
        self.assertIn("$20", obs.visible_text)


class TestComponentProvenance(unittest.TestCase):
    """Component observations preserve source context for downstream audits."""

    def test_pricing_cards_keep_per_card_provenance(self) -> None:
        obs = observe_html(SAAS_PRICING_HTML, requested_url=f"{SCOPE}/")
        by_id = {c.source_identifier: c for c in obs.components}
        self.assertIn('id="pro" class="plan card"', by_id)
        self.assertIn('id="business" class="plan card"', by_id)

        pro = by_id['id="pro" class="plan card"']
        business = by_id['id="business" class="plan card"']
        self.assertEqual(pro.kind, "content_component")
        self.assertEqual(pro.source_tag, "div")
        self.assertEqual(pro.heading, "Pro")
        self.assertIn("$20", pro.text)
        self.assertIn("Pro", pro.text)
        self.assertNotIn("$50", pro.text)
        self.assertEqual(pro.page_url, obs.final_url)

        self.assertEqual(business.heading, "Business")
        self.assertIn("$50", business.text)
        self.assertIn("Business", business.text)
        self.assertNotIn("$20", business.text)

    def test_container_kept_when_no_more_specific_candidate(self) -> None:
        obs = observe_html(CORPORATE_HTML, requested_url=f"{SCOPE}/")
        kinds = [c.kind for c in obs.components]
        self.assertIn("list", kinds)

    def test_nested_div_container_is_collapsed_to_innermost(self) -> None:
        html = (
            "<html><body><h1>Products</h1>"
            "<div class='products'><div class='card'><h2>A</h2><p>$10</p></div>"
            "<div class='card'><h2>B</h2><p>$20</p></div></div></body></html>"
        )
        obs = observe_html(html, requested_url=f"{SCOPE}/")
        cards = [c for c in obs.components if c.source_tag == "div" and c.heading in ("A", "B")]
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(c.kind == "content_component" for c in cards))

    def test_component_output_is_bounded_and_deterministic(self) -> None:
        cards = "".join(
            f"<div class='card'><h2>Item {i}</h2><p>Value {i}</p></div>" for i in range(500)
        )
        obs = observe_html(f"<html><body>{cards}</body></html>", requested_url=f"{SCOPE}/")
        self.assertEqual(obs.num_components, 200)
        first = observe_html(f"<html><body>{cards}</body></html>", requested_url=f"{SCOPE}/")
        self.assertEqual([c.heading for c in obs.components],
                         [c.heading for c in first.components])


class TestTwitterAndJsonLd(unittest.TestCase):
    def test_twitter_card_metadata_extracted(self) -> None:
        obs = observe_html(SAAS_PRICING_HTML, requested_url=f"{SCOPE}/")
        self.assertEqual(obs.twitter_card, "summary_large_image")
        self.assertEqual(obs.twitter_title, "Acme Pricing")
        self.assertEqual(obs.twitter_description, "Plans for every team size")

    def test_json_ld_survives_visible_text_cleanup(self) -> None:
        obs = observe_html(SAAS_PRICING_HTML, requested_url=f"{SCOPE}/")
        self.assertEqual(len(obs.json_ld), 1)
        product = obs.json_ld[0]
        self.assertTrue(product.is_valid)
        self.assertEqual(product.parsed_data.get("@type"), "Product")
        self.assertIn("$20", obs.visible_text)

    def test_missing_metadata_is_not_fatal(self) -> None:
        obs = observe_html("<html><body><p>minimal</p></body></html>", requested_url=f"{SCOPE}/")
        self.assertTrue(obs.is_success)
        self.assertIsNone(obs.twitter_title)
        self.assertEqual(obs.twitter_card, None)
        self.assertEqual(obs.json_ld, [])
        self.assertEqual(obs.components, [])


class TestFailureCategories(unittest.TestCase):
    """fetch_page must classify failures into stable categories."""

    def _run_fetch(self, exc: Exception) -> FetchedPage:
        with patch("src.crawler.crawler.requests.get", side_effect=exc):
            return fetch_page(f"{SCOPE}/", check_robots=False)

    def test_timeout_category(self) -> None:
        import requests
        page = self._run_fetch(requests.exceptions.Timeout("timed out"))
        self.assertEqual(page.failure_category, "timeout")

    def test_dns_error_category(self) -> None:
        import requests
        page = self._run_fetch(requests.exceptions.ConnectionError(
            "failed to resolve genericsite.test: Name or service not known"
        ))
        self.assertEqual(page.failure_category, "dns_error")

    def test_connection_error_category_without_dns(self) -> None:
        import requests
        page = self._run_fetch(requests.exceptions.ConnectionError("Connection refused"))
        self.assertEqual(page.failure_category, "connection_error")

    def test_ssl_error_category(self) -> None:
        import requests
        page = self._run_fetch(requests.exceptions.SSLError("certificate verify failed"))
        self.assertEqual(page.failure_category, "ssl_error")

    def test_too_many_redirects_category(self) -> None:
        import requests
        page = self._run_fetch(requests.exceptions.TooManyRedirects("redirect loop"))
        self.assertEqual(page.failure_category, "too_many_redirects")

    def test_http_error_category(self) -> None:
        response = MagicMock()
        response.status_code = 500
        response.reason = "Internal Server Error"
        response.headers = {"Content-Type": "text/html"}
        with patch("src.crawler.crawler.requests.get", return_value=response):
            page = fetch_page(f"{SCOPE}/", check_robots=False)
        self.assertEqual(page.failure_category, "http_error")
        self.assertEqual(page.status_code, 500)

    def test_non_html_category(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.url = f"{SCOPE}/"
        response.headers = {"Content-Type": "application/json"}
        with patch("src.crawler.crawler.requests.get", return_value=response):
            page = fetch_page(f"{SCOPE}/", check_robots=False)
        self.assertEqual(page.failure_category, "non_html")

    def test_crawl_stats_use_failure_categories(self) -> None:
        fail = FetchedPage(
            requested_url=f"{SCOPE}/",
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message="Connection error: failed to resolve",
            failure_category="dns_error",
        )
        result = crawl_site(
            CrawlConfig(start_url=f"{SCOPE}/", discover_sitemaps=False),
            fetcher=FakeSite({}, failures={"/": fail}).fetch,
        )
        self.assertFalse(result.is_success)
        self.assertEqual(result.stats.errors.get("dns_error"), 1)


class TestNoDatasetSpecialCases(unittest.TestCase):
    """Static guard: the crawler/observation source must stay domain-agnostic."""

    def test_source_contains_no_dataset_or_domain_branching(self) -> None:
        import os
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills",
                            "audit-orchestrator", "scripts", "runtime", "src")
        forbidden = ("nirma", "competition", "whitelist", "known_sites",
                     "supported_sites", "hardcoded_domains", "dataset")
        for rel in ("crawler/crawler.py", "crawler/crawl_session.py", "crawler/url_utils.py",
                    "observation/observation.py"):
            path = os.path.join(root, rel)
            with open(path, encoding="utf-8") as handle:
                content = handle.read().lower()
            for token in forbidden:
                self.assertNotIn(token, content, msg=f"{rel} must not special-case '{token}'")


if __name__ == "__main__":
    unittest.main()