"""P1-T6 robustness hardening tests (crawler/observation crawl bounds).

Covers the T6 matrix fully offline: finite timeouts, HTTP/redirect failures,
redirect-cycle handling, response-size limits, content-type/encoding tolerance,
malformed HTML, URL pathologies (invalid ports, very long/unicode URLs),
sitemap/robots bound integrity, and determinism. No test touches the network.
"""

import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest
from unittest.mock import MagicMock, PropertyMock, patch
import urllib.parse

import requests

from src.crawler.crawler import (
    DEFAULT_MAX_RESPONSE_BYTES,
    FetchedPage,
    fetch_page,
    fetch_text,
)
from src.crawler.crawl_session import CrawlConfig, crawl_site
from src.crawler.robots_sitemaps import discover_sitemap_urls
from src.crawler.url_utils import (
    extract_site_key,
    is_crawlable_scheme,
    is_same_site,
    normalize_url,
)
from src.observation.observation import observe_page

SCOPE = "https://site.test"


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


def html_with_links(*hrefs: str) -> str:
    anchors = "".join(f'<a href="{href}">link {i + 1}</a>' for i, href in enumerate(hrefs))
    return f"<html><body><h1>Page</h1>{anchors}</body></html>"


def sitemap_xml(*urls: str) -> str:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )


class TestUrlPathologyRobustness(unittest.TestCase):
    def test_invalid_port_urls_fail_soft_never_raise(self) -> None:
        for url in ("https://host:abc/", "https://host:99999/", "http://host:-1/"):
            with self.subTest(url=url):
                self.assertEqual(normalize_url(url), url)
                self.assertIsNone(extract_site_key(url))
                self.assertFalse(is_same_site(url, "https://host/"))
                self.assertTrue(is_crawlable_scheme(url))

    def test_valid_ports_and_scoping_unchanged(self) -> None:
        self.assertEqual(extract_site_key("https://host:8443/"), "host:8443")
        self.assertEqual(extract_site_key("https://host:443/"), "host")
        self.assertTrue(is_same_site("https://host:443/a", "https://host/b"))
        self.assertEqual(normalize_url("https://Host:443/x"), "https://host/x")


class TestFetchRobustness(unittest.TestCase):
    def _mock_response(self, *, text: str = "", headers=None, url: str = "https://example.com/") -> MagicMock:
        resp = MagicMock()
        resp.status_code = 200
        resp.url = url
        resp.headers = headers if headers is not None else {"Content-Type": "text/html"}
        resp.text = text
        return resp

    @patch("src.crawler.crawler.requests.get")
    def test_redirect_cycle_is_clean_failure(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = requests.exceptions.TooManyRedirects("Exceeded 30 redirects.")
        result = fetch_page("https://example.com/loop", check_robots=False)
        self.assertFalse(result.is_success)
        self.assertIsNone(result.status_code)
        self.assertEqual(result.html_content, "")
        self.assertIn("Too many redirects", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_oversized_response_rejected_by_declared_length(self, mock_get: MagicMock) -> None:
        resp = self._mock_response(
            text="<html>x</html>",
            headers={
                "Content-Type": "text/html",
                "Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1),
            },
        )
        mock_get.return_value = resp
        result = fetch_page("https://example.com/", check_robots=False)
        self.assertFalse(result.is_success)
        self.assertEqual(result.html_content, "")
        self.assertIn("exceeds the", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_oversized_response_rejected_by_body_with_small_cap(self, mock_get: MagicMock) -> None:
        resp = self._mock_response(text="x" * 2000, headers={"Content-Type": "text/html"})
        mock_get.return_value = resp
        result = fetch_page("https://example.com/", check_robots=False, max_response_bytes=1000)
        self.assertFalse(result.is_success)
        self.assertEqual(result.html_content, "")
        self.assertIn("exceeds the", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_max_response_bytes_none_disables_limit(self, mock_get: MagicMock) -> None:
        body = "x" * 2000
        resp = self._mock_response(text=body, headers={"Content-Type": "text/html"})
        mock_get.return_value = resp
        result = fetch_page("https://example.com/", check_robots=False, max_response_bytes=None)
        self.assertTrue(result.is_success)
        self.assertEqual(result.html_content, body)

    @patch("src.crawler.crawler.requests.get")
    def test_oversized_text_fetch_rejected(self, mock_get: MagicMock) -> None:
        resp = self._mock_response(
            text="<urlset/>",
            headers={
                "Content-Type": "text/xml",
                "Content-Length": str(DEFAULT_MAX_RESPONSE_BYTES + 1),
            },
        )
        mock_get.return_value = resp
        result = fetch_text("https://example.com/sitemap.xml")
        self.assertFalse(result.is_success)
        self.assertIn("exceeds the", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_missing_content_type_rejected(self, mock_get: MagicMock) -> None:
        resp = self._mock_response(text="<html>x</html>", headers={})
        mock_get.return_value = resp
        result = fetch_page("https://example.com/", check_robots=False)
        self.assertFalse(result.is_success)
        self.assertIn("non-HTML", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_malformed_content_type_variants_accepted(self, mock_get: MagicMock) -> None:
        variants = [
            "text/html; charset=utf-8",
            "TEXT/HTML",
            "text/html;; charset=utf-8",
            "text/html; charset=",
            "application/xhtml+xml; charset=utf-8",
        ]
        for content_type in variants:
            with self.subTest(content_type=content_type):
                resp = self._mock_response(text="<html>ok</html>", headers={"Content-Type": content_type})
                mock_get.return_value = resp
                result = fetch_page("https://example.com/", check_robots=False)
                self.assertTrue(result.is_success)

    @patch("src.crawler.crawler.requests.get")
    def test_unknown_charset_never_crashes(self, mock_get: MagicMock) -> None:
        resp = requests.Response()
        resp.status_code = 200
        resp.url = "https://example.com/"
        resp.headers = {"Content-Type": "text/html; charset=made-up-charset"}
        resp._content = "<html><body><p>caf\u00e9</p></body></html>".encode("utf-8")
        mock_get.return_value = resp
        result = fetch_page("https://example.com/", check_robots=False)
        self.assertTrue(result.is_success)
        self.assertNotEqual(result.html_content, "")

    @patch("src.crawler.crawler.requests.get")
    def test_decode_failure_is_clean(self, mock_get: MagicMock) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.url = "https://example.com/encoding-broken"
        resp.headers = {"Content-Type": "text/html"}
        type(resp).text = PropertyMock(side_effect=RuntimeError("decode boom"))
        mock_get.return_value = resp
        result = fetch_page("https://example.com/encoding-broken", check_robots=False)
        self.assertFalse(result.is_success)
        self.assertIn("Failed to decode", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_very_long_url_fails_cleanly(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = requests.exceptions.InvalidURL("URL too long")
        result = fetch_page(f"https://example.com/{'a' * 3000}", check_robots=False)
        self.assertFalse(result.is_success)
        self.assertIsNotNone(result.error_message)


class TestCrawlRobustness(unittest.TestCase):
    class FakeSite:
        def __init__(self, pages: dict[str, str], failures: dict[str, FetchedPage] | None = None) -> None:
            self.pages = pages
            self.failures = failures or {}
            self.fetch_counts: dict[str, int] = {}

        def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
            path = urllib.parse.urlsplit(url).path
            if path in self.failures:
                return self.failures[path]
            if path in self.pages:
                return ok_page(url, self.pages[path])
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)

    def _crawl(self, pages: dict[str, str], *, failures: dict[str, FetchedPage] | None = None, **kwargs):
        site = self.FakeSite(pages, failures=failures)
        result = crawl_site(CrawlConfig(start_url=f"{SCOPE}/", **kwargs), fetcher=site.fetch)
        return result, site

    def test_malformed_port_link_does_not_crash_crawl(self) -> None:
        pages = {"/": html_with_links(f"{SCOPE}:abc/x", f"{SCOPE}/ok"), "/ok": html_with_links()}
        result, site = self._crawl(pages, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/ok"])
        self.assertFalse(any(":abc" in key for key in site.fetch_counts))

    def test_very_long_and_unicode_links_survive(self) -> None:
        long_path = "/" + "a" * 3000 + "/x"
        pages = {"/": html_with_links(long_path, "/caf\u00e9/\u2603", "/ok"), "/ok": html_with_links()}
        result, _ = self._crawl(pages, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertIn(f"{SCOPE}/ok", result.stats.crawled_urls)

    def test_timeout_page_categorized(self) -> None:
        pages = {"/": html_with_links("/slow", "/ok"), "/ok": html_with_links()}
        failures = {
            "/slow": fail_page(f"{SCOPE}/slow", "Request timed out after 10.0s: read timed out."),
        }
        result, _ = self._crawl(pages, failures=failures, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.errors, {"timeout": 1})
        self.assertEqual(result.stats.pages_failed, 1)

    def test_redirect_cycle_page_categorized(self) -> None:
        pages = {"/": html_with_links("/loop", "/ok"), "/ok": html_with_links()}
        failures = {
            "/loop": fail_page(
                f"{SCOPE}/loop", "Too many redirects while following URL chain: Exceeded 30 redirects."
            ),
        }
        result, _ = self._crawl(pages, failures=failures, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.errors, {"too_many_redirects": 1})

    def test_oversized_page_fails_but_crawl_survives(self) -> None:
        pages = {"/": html_with_links("/big", "/ok"), "/ok": html_with_links()}
        failures = {
            "/big": fail_page(
                f"{SCOPE}/big",
                "Response body exceeds the 5000000-byte limit (Content-Type: 'text/html').",
            ),
        }
        result, _ = self._crawl(pages, failures=failures, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.errors, {"response_too_large": 1})
        self.assertFalse(result.pages_by_url[f"{SCOPE}/big"].is_success)
        self.assertTrue(result.pages_by_url[f"{SCOPE}/ok"].is_success)

    def test_oversized_start_page_reported(self) -> None:
        failures = {
            "/": fail_page("https://site.test/", "Response body exceeds the 5000000-byte limit."),
        }
        result, _ = self._crawl({}, failures=failures)
        self.assertFalse(result.is_success)
        self.assertIn("exceeds the", result.error_message or "")

    def test_non_html_page_categorized(self) -> None:
        pages = {"/": html_with_links("/pdf", "/ok"), "/ok": html_with_links()}
        failures = {
            "/pdf": fail_page(
                f"{SCOPE}/pdf", "Rejected non-HTML response (Content-Type: 'application/pdf').", 200
            ),
        }
        result, _ = self._crawl(pages, failures=failures, max_depth=2)
        self.assertEqual(result.stats.errors, {"non_html": 1})

    def test_decode_error_page_categorized(self) -> None:
        pages = {"/": html_with_links("/bad", "/ok"), "/ok": html_with_links()}
        failures = {
            "/bad": fail_page(f"{SCOPE}/bad", "Failed to decode response content: boom"),
        }
        result, _ = self._crawl(pages, failures=failures, max_depth=2)
        self.assertEqual(result.stats.errors, {"decode_error": 1})

    def test_huge_link_list_bounded_and_deterministic(self) -> None:
        links = [f"/p{i}" for i in range(5000)]
        pages = {"/": html_with_links(*links)}
        first, first_site = self._crawl(pages, max_pages=50)
        second, second_site = self._crawl(pages, max_pages=50)
        self.assertEqual(first.stats.pages_attempted, 50)
        self.assertEqual(first.stats.urls_discovered, 50)
        self.assertEqual(first.stats.crawled_urls, second.stats.crawled_urls)
        self.assertEqual(first_site.fetch_counts, second_site.fetch_counts)

    def test_sitemap_seeds_respect_page_budget(self) -> None:
        urls = [f"{SCOPE}/seed{i}" for i in range(100)]

        def fetch(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            if url.endswith("/robots.txt"):
                return ok_page(url, f"Sitemap: {SCOPE}/sitemap.xml\n", content_type="text/plain")
            if url.endswith("/sitemap.xml"):
                return ok_page(url, sitemap_xml(*urls), content_type="application/xml")
            if url.endswith("/"):
                return ok_page(url, html_with_links())
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)

        result = crawl_site(CrawlConfig(start_url=f"{SCOPE}/", max_pages=5), fetcher=fetch)
        self.assertLessEqual(result.stats.pages_attempted, 5)
        self.assertLessEqual(result.stats.urls_discovered, 5)
        self.assertGreaterEqual(result.stats.sitemap_urls_discovered, 1)


class TestDiscoveryRobustness(unittest.TestCase):
    def test_oversized_robots_fails_open(self) -> None:
        def fetch(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            if url.endswith("/robots.txt"):
                return fail_page(url, "Response body exceeds the 5000000-byte limit.")
            if url.endswith("/sitemap.xml"):
                return ok_page(url, sitemap_xml(f"{SCOPE}/a"), content_type="application/xml")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)

        result = discover_sitemap_urls(SCOPE, fetcher=fetch, max_urls=10)
        self.assertIn("robots_fetch_failed", result.errors)
        self.assertTrue(result.conventional_checked)
        self.assertIn(f"{SCOPE}/a", result.discovered_urls)

    def test_oversized_sitemap_is_harmless(self) -> None:
        def fetch(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            if url.endswith("/robots.txt"):
                return ok_page(url, f"Sitemap: {SCOPE}/sitemap.xml\n", content_type="text/plain")
            if url.endswith("/sitemap.xml"):
                return fail_page(url, "Response body exceeds the 5000000-byte limit.")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)

        result = discover_sitemap_urls(SCOPE, fetcher=fetch, max_urls=10)
        self.assertIn("sitemap_fetch_failed", result.errors)
        self.assertEqual(result.discovered_urls, [])


class TestObservationRobustness(unittest.TestCase):
    def test_malformed_html_observed_not_crashed(self) -> None:
        html = "<html><body><h1>Hello <b>broken</b><div class='x'><ul><li>1<li>2</ul>"
        obs = observe_page(ok_page(f"{SCOPE}/", html))
        self.assertTrue(obs.is_success)
        self.assertIn("Hello", obs.visible_text)

    def test_control_char_html_tolerated(self) -> None:
        html = "abc\x00\x01\x02<img src='x'>def</html>"
        obs = observe_page(ok_page(f"{SCOPE}/", html))
        self.assertTrue(obs.is_success)
        self.assertEqual(obs.signals.num_images, 1)


if __name__ == "__main__":
    unittest.main()