"""Unit tests for P1-T2 sitemap discovery (src.crawler.robots_sitemaps) and its
integration into the bounded crawl session.

All tests run fully offline against deterministic in-memory sites served by a
fake fetcher (the FetcherCallable test seam). Covers the T2 scenario matrix:
robots.txt with/without Sitemap, missing/malformed robots, ordinary sitemap.xml,
sitemap index nesting, malformed sitemaps, duplicate/external/fragment URLs,
sitemap-cycle termination, budget caps, crawl integration, and determinism.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import urllib.parse

import requests

from src.crawler.crawler import FetchedPage, fetch_text
from src.crawler.crawl_session import CrawlConfig, crawl_site
from src.crawler.robots_sitemaps import (
    MAX_SITEMAP_DEPTH,
    MAX_SITEMAP_FILES,
    discover_sitemap_urls,
    parse_robots_sitemap_records,
)

SCOPE = "https://site.test"


def html_with_links(*hrefs: str) -> str:
    """Build deterministic fixture HTML containing the given hrefs."""
    anchors = "".join(f'<a href="{href}">link {i + 1}</a>' for i, href in enumerate(hrefs))
    return f"<html><body><h1>Page</h1>{anchors}</body></html>"


def sitemap_xml(*urls: str) -> str:
    """Namespaced urlset sitemap fixture with the given loc values."""
    urls_block = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls_block}</urlset>'


def sitemap_index_xml(*children: str) -> str:
    """Namespaced sitemapindex fixture with the given child sitemap locs."""
    children_block = "".join(f"<sitemap><loc>{c}</loc></sitemap>" for c in children)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{children_block}</sitemapindex>'


def ok(path: str, content: str, content_type: str = "text/html") -> FetchedPage:
    url = f"{SCOPE}{path}"
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type=content_type,
        html_content=content,
        is_success=True,
        error_message=None,
    )


def not_found(url: str) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=404,
        content_type="text/html",
        html_content="",
        is_success=False,
        error_message="HTTP error status 404 (Not Found).",
    )


class FakeDiscoverySite:
    """In-memory site serving pages, robots.txt, and sitemap files by path.

    robots=None means robots.txt is unavailable (404). Any path not served as a
    page, robots file, or sitemap returns 404.
    """

    def __init__(self, *, robots: str | None = None, sitemaps: dict[str, str] | None = None,
                 pages: dict[str, str] | None = None) -> None:
        self.robots = robots
        self.sitemaps = sitemaps or {}
        self.pages = pages or {}
        self.fetch_counts: dict[str, int] = {}

    def _bump(self, url: str) -> str:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        return url

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        self._bump(url)
        path = urllib.parse.urlsplit(url).path
        if path == "/robots.txt":
            if self.robots is not None:
                return ok(path, self.robots, content_type="text/plain")
            return not_found(url)
        if path in self.sitemaps:
            return ok(path, self.sitemaps[path], content_type="application/xml")
        if path in self.pages:
            return ok(path, self.pages[path])
        return not_found(url)


class TestParseRobotsSitemapRecords(unittest.TestCase):
    def test_extracts_sitemap_directives_only(self) -> None:
        text = (
            "User-agent: *\n"
            "Disallow: /private/\n"
            f"Sitemap: {SCOPE}/sitemap.xml\n"
            f"Sitemap: <https://other.example/sitemap.xml> # trailing comment\n"
            "Sitemap:\n"
            "# Sitemap: https://commented.example/s.xml\n"
            "  sitemap : https://site.test/second.xml\n"
        )
        self.assertEqual(
            parse_robots_sitemap_records(text),
            [f"{SCOPE}/sitemap.xml", "https://other.example/sitemap.xml", "https://site.test/second.xml"],
        )

    def test_empty_input_yields_nothing(self) -> None:
        self.assertEqual(parse_robots_sitemap_records(""), [])
        self.assertEqual(parse_robots_sitemap_records("# only a comment\nUser-agent: *\n"), [])


class TestSitemapDiscovery(unittest.TestCase):
    def _discover(self, site: FakeDiscoverySite, **kwargs) -> tuple:
        result = discover_sitemap_urls(SCOPE, fetcher=site.fetch, **kwargs)
        return result, site

    def test_robots_sitemap_directive_drives_discovery(self) -> None:
        site = FakeDiscoverySite(
            robots=f"User-agent: *\nDisallow:\nSitemap: {SCOPE}/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a", f"{SCOPE}/b")},
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a", f"{SCOPE}/b"])
        self.assertEqual(result.robots_sitemap_urls, [f"{SCOPE}/sitemap.xml"])
        self.assertTrue(result.robots_fetched)
        self.assertFalse(result.conventional_checked)
        self.assertEqual(result.sitemap_files_parsed, [f"{SCOPE}/sitemap.xml"])
        self.assertEqual(result.errors, {})

    def test_robots_without_sitemap_falls_back_to_conventional(self) -> None:
        site = FakeDiscoverySite(
            robots="User-agent: *\nAllow: /\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a")},
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a"])
        self.assertEqual(result.robots_sitemap_urls, [])
        self.assertTrue(result.robots_fetched)
        self.assertTrue(result.conventional_checked)

    def test_missing_robots_falls_back_to_conventional(self) -> None:
        site = FakeDiscoverySite(sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a")})
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a"])
        self.assertFalse(result.robots_fetched)
        self.assertTrue(result.conventional_checked)
        self.assertIn("robots_fetch_failed", result.errors)

    def test_malformed_robots_is_harmless(self) -> None:
        site = FakeDiscoverySite(
            robots="this is not robots at all \nno sitemap anywhere @@ \u2265 ##\nxxxx : yyyy\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a")},
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a"])
        self.assertTrue(result.robots_fetched)
        self.assertTrue(result.conventional_checked)
        self.assertEqual(result.robots_sitemap_urls, [])

    def test_declared_sitemap_404_skips_conventional(self) -> None:
        site = FakeDiscoverySite(robots=f"Sitemap: {SCOPE}/sitemap.xml\n")
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [])
        self.assertEqual(result.sitemap_files_parsed, [])
        self.assertFalse(result.conventional_checked)
        self.assertIn("sitemap_fetch_failed", result.errors)

    def test_ordinary_urlset_extracts_absolute_locs(self) -> None:
        site = FakeDiscoverySite(sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/x", f"{SCOPE}/y/z")})
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/x", f"{SCOPE}/y/z"])

    def test_non_namespaced_urlset_parsed(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={"/sitemap.xml": '<urlset><url><loc>https://site.test/ns</loc></url></urlset>'}
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/ns"])

    def test_sitemap_index_nesting(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={
                "/sitemap.xml": sitemap_index_xml(f"{SCOPE}/sitemap-a.xml", f"{SCOPE}/sitemap-b.xml"),
                "/sitemap-a.xml": sitemap_xml(f"{SCOPE}/a1", f"{SCOPE}/a2"),
                "/sitemap-b.xml": sitemap_xml(f"{SCOPE}/b1"),
            }
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a1", f"{SCOPE}/a2", f"{SCOPE}/b1"])
        self.assertEqual(
            result.sitemap_files_parsed,
            [f"{SCOPE}/sitemap.xml", f"{SCOPE}/sitemap-a.xml", f"{SCOPE}/sitemap-b.xml"],
        )

    def test_malformed_sitemap_is_recorded_not_fatal(self) -> None:
        site = FakeDiscoverySite(robots=f"Sitemap: {SCOPE}/sitemap.xml\n",
                                 sitemaps={"/sitemap.xml": "<urlset><url><loc>oops"})
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [])
        self.assertIn("malformed_sitemap", result.errors)
        self.assertEqual(result.sitemap_files_parsed, [])

    def test_duplicate_urls_in_sitemap_deduplicated(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/dup", f"{SCOPE}/dup", f"{SCOPE}/dup",
                                                  f"{SCOPE}/other")}
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/dup", f"{SCOPE}/other"])

    def test_external_urls_in_sitemap_filtered_out(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={"/sitemap.xml": sitemap_xml(
                f"{SCOPE}/internal",
                "https://external.example/x",
                "http://other.test/y",
                "https://sub.site.test/z",
                "mailto:info@site.test",
            )}
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/internal"])

    def test_fragment_urls_in_sitemap_normalized_and_deduplicated(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a#section-1", f"{SCOPE}/a#section-2",
                                                  f"{SCOPE}/b#top")}
        )
        result, _ = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/a", f"{SCOPE}/b"])

    def test_sitemap_exceeding_url_budget_is_capped(self) -> None:
        urls = [f"{SCOPE}/u{i}" for i in range(50)]
        site = FakeDiscoverySite(sitemaps={"/sitemap.xml": sitemap_xml(*urls)})
        result, _ = self._discover(site, max_urls=3)
        self.assertEqual(len(result.discovered_urls), 3)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/u0", f"{SCOPE}/u1", f"{SCOPE}/u2"])

    def test_sitemap_cycle_terminates(self) -> None:
        site = FakeDiscoverySite(
            sitemaps={
                "/sitemap.xml": sitemap_index_xml(f"{SCOPE}/sitemap.xml", f"{SCOPE}/sitemap-a.xml"),
                "/sitemap-a.xml": sitemap_index_xml(f"{SCOPE}/sitemap.xml", f"{SCOPE}/sitemap-b.xml"),
                "/sitemap-b.xml": sitemap_xml(f"{SCOPE}/leaf"),
            }
        )
        result, site = self._discover(site, max_urls=10)
        self.assertEqual(result.discovered_urls, [f"{SCOPE}/leaf"])
        self.assertLessEqual(len(result.sitemap_files_parsed), MAX_SITEMAP_FILES)
        # each sitemap file fetched at most once
        for path in ("/sitemap.xml", "/sitemap-a.xml", "/sitemap-b.xml"):
            self.assertEqual(site.fetch_counts[f"{SCOPE}{path}"], 1)

    def test_deep_index_chain_bounded_by_file_budget(self) -> None:
        sitemaps: dict[str, str] = {}
        for i in range(10):
            child = f"{SCOPE}/sitemap-level-{i + 1}.xml"
            if i == 0:
                sitemaps["/sitemap.xml"] = sitemap_index_xml(child)
            else:
                sitemaps[f"/sitemap-level-{i}.xml"] = sitemap_index_xml(child)
        site = FakeDiscoverySite(sitemaps=sitemaps)
        result, _ = self._discover(site, max_urls=10)
        self.assertLessEqual(len(result.sitemap_files_parsed), MAX_SITEMAP_FILES)
        self.assertEqual(result.discovered_urls, [])

    def test_determinism_across_runs(self) -> None:
        site = FakeDiscoverySite(
            robots=f"User-agent: *\nSitemap: {SCOPE}/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_index_xml(
                f"{SCOPE}/sitemap-a.xml", f"{SCOPE}/sitemap-b.xml")},
            pages={},
        )
        site.sitemaps["/sitemap-a.xml"] = sitemap_xml(f"{SCOPE}/a1", f"{SCOPE}/a2")
        site.sitemaps["/sitemap-b.xml"] = sitemap_xml(f"{SCOPE}/b1")
        first, _ = self._discover(site, max_urls=10)
        second, _ = self._discover(site, max_urls=10)
        self.assertEqual(first.discovered_urls, second.discovered_urls)
        self.assertEqual(first.sitemap_files_parsed, second.sitemap_files_parsed)
        self.assertEqual(first.robots_sitemap_urls, second.robots_sitemap_urls)
        self.assertEqual(first.errors, second.errors)


class TestSitemapDiscoveryCrawlIntegration(unittest.TestCase):
    def _crawl(self, site: FakeDiscoverySite, **kwargs) -> tuple:
        config = CrawlConfig(start_url=f"{SCOPE}/", **kwargs)
        return crawl_site(config, fetcher=site.fetch), site

    def test_sitemap_discovered_page_is_crawled(self) -> None:
        site = FakeDiscoverySite(
            robots=f"User-agent: *\nSitemap: {SCOPE}/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/from-sitemap", f"{SCOPE}/also-from-sitemap")},
            pages={"/": html_with_links("/page-two"), "/page-two": html_with_links(),
                   "/from-sitemap": html_with_links(), "/also-from-sitemap": html_with_links()},
        )
        result, site = self._crawl(site, max_depth=2, max_pages=10)
        self.assertEqual(
            result.stats.crawled_urls,
            [f"{SCOPE}/", f"{SCOPE}/from-sitemap", f"{SCOPE}/also-from-sitemap", f"{SCOPE}/page-two"],
        )
        self.assertEqual(result.stats.sitemap_files_parsed, 1)
        self.assertEqual(result.stats.sitemap_urls_discovered, 2)
        self.assertIn(f"{SCOPE}/from-sitemap", result.pages_by_url)
        self.assertTrue(result.pages_by_url[f"{SCOPE}/from-sitemap"].is_success)

    def test_sitemap_exceeding_crawl_budget_is_bounded(self) -> None:
        pages = {"/": html_with_links()}
        urls = [f"{SCOPE}/u{i}" for i in range(50)]
        for u in urls:
            pages["/" + u.removeprefix(SCOPE)] = html_with_links()
        site = FakeDiscoverySite(
            sitemaps={"/sitemap.xml": sitemap_xml(*urls)}, pages=pages,
        )
        result, _ = self._crawl(site, max_pages=3, max_depth=3)
        self.assertEqual(len(result.stats.crawled_urls), 3)
        self.assertEqual(result.stats.urls_discovered, 3)
        self.assertEqual(result.stats.sitemap_urls_discovered, 2)
        self.assertNotIn(f"{SCOPE}/u2", result.stats.crawled_urls)

    def test_discovery_opt_out_does_not_fetch_metadata(self) -> None:
        site = FakeDiscoverySite(
            robots=f"Sitemap: {SCOPE}/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/from-sitemap")},
            pages={"/": html_with_links(), "/from-sitemap": html_with_links()},
        )
        result, site = self._crawl(site, discover_sitemaps=False, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/"])
        self.assertEqual(result.stats.sitemap_files_parsed, 0)
        self.assertEqual(result.stats.sitemap_urls_discovered, 0)
        self.assertNotIn(f"{SCOPE}/robots.txt", site.fetch_counts)
        self.assertNotIn(f"{SCOPE}/sitemap.xml", site.fetch_counts)

    def test_depth_zero_keeps_sitemap_seeds_out(self) -> None:
        site = FakeDiscoverySite(
            robots=f"Sitemap: {SCOPE}/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/from-sitemap")},
            pages={"/": html_with_links(), "/from-sitemap": html_with_links()},
        )
        result, site = self._crawl(site, max_depth=0)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/"])
        self.assertEqual(result.stats.sitemap_urls_discovered, 0)
        self.assertNotIn(f"{SCOPE}/from-sitemap", result.stats.crawled_urls)


class TestFetchText(unittest.TestCase):
    @patch("src.crawler.crawler.requests.get")
    def test_fetch_text_accepts_plain_text(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/robots.txt"
        mock_response.headers = {"Content-Type": "text/plain"}
        mock_response.text = "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        mock_get.return_value = mock_response

        result = fetch_text("https://example.com/robots.txt", check_robots=True)
        self.assertTrue(result.is_success)
        self.assertIn("Sitemap: https://example.com/sitemap.xml", result.html_content)
        # check_robots is ignored for metadata fetches: exactly one request.
        self.assertEqual(mock_get.call_count, 1)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_text_accepts_xml_suffixed_types(self, mock_get: MagicMock) -> None:
        for content_type in ("application/xml", "text/xml", "application/rss+xml"):
            with self.subTest(content_type=content_type):
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.url = "https://example.com/sitemap.xml"
                mock_response.headers = {"Content-Type": content_type}
                mock_response.text = "<urlset/>"
                mock_get.return_value = mock_response
                result = fetch_text("https://example.com/sitemap.xml")
                self.assertTrue(result.is_success)
                self.assertEqual(result.html_content, "<urlset/>")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_text_rejects_non_text(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/feed.png"
        mock_response.headers = {"Content-Type": "image/png"}
        mock_response.text = "PNG"
        mock_get.return_value = mock_response

        result = fetch_text("https://example.com/feed.png")
        self.assertFalse(result.is_success)
        self.assertIn("Rejected non-text", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_text_http_error(self, mock_get: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason = "Internal Server Error"
        mock_get.return_value = mock_response

        result = fetch_text("https://example.com/sitemap.xml")
        self.assertFalse(result.is_success)
        self.assertIn("HTTP error status 500", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_text_timeout(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out after 10s")
        result = fetch_text("https://example.com/sitemap.xml")
        self.assertFalse(result.is_success)
        self.assertIn("timed out", result.error_message or "")


if __name__ == "__main__":
    unittest.main()