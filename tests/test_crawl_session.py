"""Unit tests for P1-T1 bounded crawl session (src.crawler.crawl_session).

All tests run fully offline against a deterministic in-memory site served by a
fake fetcher (test seam), so they never depend on internet access. Covers the
T1 scenario matrix: relative/absolute links, fragments, unsupported schemes,
duplicates, cycles, depth/page limits, external exclusion, nested resolution,
broken pages, and empty pages.
"""

from __future__ import annotations

import urllib.parse
import unittest

from src.crawler.crawl_session import CrawlConfig, crawl_site
from src.crawler.crawler import FetchedPage

SCOPE = "https://site.test"


def html_with_links(*hrefs: str) -> str:
    """Build deterministic fixture HTML containing the given hrefs."""
    anchors = "".join(f'<a href="{href}">link {i + 1}</a>' for i, href in enumerate(hrefs))
    return f"<html><body><h1>Page</h1>{anchors}</body></html>"


class FakeSite:
    """An in-memory site the fake fetcher serves. path -> html, or failure."""

    def __init__(self, pages: dict[str, str], failures: set[str] | None = None) -> None:
        self.pages = pages
        self.failures = failures or set()
        self.fetch_counts: dict[str, int] = {}

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        path = urllib.parse.urlsplit(url).path
        if path in self.failures or path not in self.pages:
            return FetchedPage(
                requested_url=url,
                final_url=url,
                status_code=404,
                content_type="text/html",
                html_content="",
                is_success=False,
                error_message="HTTP error status 404 (Not Found).",
            )
        return FetchedPage(
            requested_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            html_content=self.pages[path],
            is_success=True,
            error_message=None,
        )


class TestCrawlSession(unittest.TestCase):
    def _crawl(self, pages: dict[str, str], *, start: str = f"{SCOPE}/", failures: set[str] | None = None, **kwargs) -> tuple:
        site = FakeSite(pages, failures=failures)
        config = CrawlConfig(start_url=start, **kwargs)
        result = crawl_site(config, fetcher=site.fetch)
        return result, site

    def test_relative_and_absolute_internal_links(self) -> None:
        pages = {"/": html_with_links("/about", f"{SCOPE}/contact"), "/about": html_with_links(), "/contact": html_with_links()}
        result, _ = self._crawl(pages, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/about", f"{SCOPE}/contact"])
        self.assertEqual(result.stats.pages_fetched, 3)
        self.assertEqual(result.stats.pages_failed, 0)
        self.assertEqual(result.stats.urls_discovered, 3)

    def test_fragment_links_normalize_to_one_crawl(self) -> None:
        pages = {"/": html_with_links("/about#team"), "/about": html_with_links()}
        result, site = self._crawl(pages, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/about"])
        self.assertEqual(site.fetch_counts[f"{SCOPE}/about"], 1)
        self.assertNotIn(f"{SCOPE}/about#team", site.fetch_counts)

    def test_unsupported_schemes_never_crawled(self) -> None:
        pages = {
            "/": html_with_links("javascript:void(0)", "mailto:a@b.com", "tel:+123", "data:text/html,x", f"{SCOPE}/ok"),
            "/ok": html_with_links(),
        }
        result, site = self._crawl(pages, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/ok"])
        self.assertEqual(result.stats.urls_discovered, 2)
        for scheme in ("javascript", "mailto", "tel", "data"):
            self.assertFalse(any(key.startswith(scheme) for key in site.fetch_counts))

    def test_duplicate_links_crawled_once(self) -> None:
        pages = {"/": html_with_links("/dup", "/dup", "/dup#top"), "/dup": html_with_links()}
        result, site = self._crawl(pages, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/dup"])
        self.assertEqual(site.fetch_counts[f"{SCOPE}/dup"], 1)

    def test_cycle_terminates(self) -> None:
        pages = {"/": html_with_links("/b"), "/b": html_with_links("/", "/b")}
        result, _ = self._crawl(pages, max_depth=5)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/b"])
        self.assertEqual(result.stats.pages_fetched, 2)
        self.assertEqual(result.stats.max_depth_reached, 1)

    def test_depth_limit(self) -> None:
        pages = {"/": html_with_links("/b"), "/b": html_with_links("/c"), "/c": html_with_links()}
        result, _ = self._crawl(pages, max_depth=1)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/b"])
        self.assertNotIn(f"{SCOPE}/c", result.stats.crawled_urls)
        self.assertEqual(result.stats.max_depth_reached, 1)
        self.assertEqual(result.stats.urls_discovered, 2)

    def test_page_limit(self) -> None:
        pages = {"/": html_with_links("/a", "/b", "/c"), "/a": html_with_links(), "/b": html_with_links(), "/c": html_with_links()}
        result, _ = self._crawl(pages, max_pages=2)
        self.assertEqual(len(result.stats.crawled_urls), 2)
        self.assertEqual(result.stats.pages_attempted, 2)
        # Discovery is bounded to max_pages distinct URLs as well.
        self.assertEqual(result.stats.urls_discovered, 2)

    def test_external_links_never_crawled(self) -> None:
        pages = {
            "/": html_with_links("https://external.example/x", "//cdn.external.example/y", f"{SCOPE}/internal"),
            "/internal": html_with_links(),
        }
        result, site = self._crawl(pages, max_depth=2)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/", f"{SCOPE}/internal"])
        self.assertTrue(all(key.startswith(SCOPE) for key in site.fetch_counts))

    def test_query_strings_preserved_as_distinct_resources(self) -> None:
        pages = {"/": html_with_links("/list?view=1", "/list?view=2"), "/list?view=1": html_with_links(), "/list?view=2": html_with_links()}
        result, site = self._crawl(pages, max_depth=2)
        self.assertEqual(len(result.stats.crawled_urls), 3)  # schema: / + two query variants
        self.assertEqual(site.fetch_counts[f"{SCOPE}/list?view=1"], 1)
        self.assertEqual(site.fetch_counts[f"{SCOPE}/list?view=2"], 1)

    def test_nested_relative_resolution(self) -> None:
        sub = "/a/b/"
        pages = {
            "/": html_with_links("/a/b/"),
            sub: html_with_links("../c", "c2", "/root", "//site.test/proto", "?keep=1"),
            "/a/c": html_with_links(),
            "/a/b/c2": html_with_links(),
            "/root": html_with_links(),
            "/proto": html_with_links(),
            "/a/b/?keep=1": html_with_links(),
        }
        result, _ = self._crawl(pages, max_depth=3)
        expected = [f"{SCOPE}/", f"{SCOPE}/a/b/", f"{SCOPE}/a/c", f"{SCOPE}/a/b/c2",
                    f"{SCOPE}/root", f"{SCOPE}/proto", f"{SCOPE}/a/b/?keep=1"]
        self.assertEqual(result.stats.crawled_urls, expected)

    def test_broken_page_recorded_and_crawl_continues(self) -> None:
        pages = {"/": html_with_links("/broken", "/ok"), "/ok": html_with_links()}
        result, _ = self._crawl(pages, failures={"/broken"}, max_depth=2)
        self.assertTrue(result.is_success)
        self.assertEqual(result.stats.pages_fetched, 2)
        self.assertEqual(result.stats.pages_failed, 1)
        self.assertEqual(result.stats.pages_attempted, 3)
        self.assertEqual(result.stats.errors, {"http_error": 1})
        self.assertEqual(len(result.stats.error_samples), 1)
        self.assertEqual(result.pages_by_url[f"{SCOPE}/broken"].is_success, False)
        self.assertEqual(result.pages_by_url[f"{SCOPE}/ok"].is_success, True)
        self.assertIsNotNone(result.pages_by_url[f"{SCOPE}/ok"].observation)

    def test_empty_no_link_page_terminates(self) -> None:
        result, _ = self._crawl({"/": "<html><body><p>No links here</p></body></html>"}, max_depth=3)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/"])
        self.assertEqual(result.stats.urls_discovered, 1)
        self.assertEqual(result.stats.pages_failed, 0)
        self.assertIsNotNone(result.pages_by_url[f"{SCOPE}/"].observation)

    def test_self_and_fragment_only_links(self) -> None:
        pages = {"/": html_with_links("/", "#top"), "/ok": html_with_links()}
        result, _ = self._crawl(pages, max_depth=2)
        self.assertEqual(len(result.stats.crawled_urls), 1)

    def test_robots_disallowed_categorized(self) -> None:
        def blocking_fetcher(url, timeout, user_agent, check_robots):
            return FetchedPage(url, "", None, "", "", False, "Access disallowed by robots.txt at 'http://..' for User-Agent 'x'.")

        result = crawl_site(CrawlConfig(start_url="https://site.test/", max_pages=5), fetcher=blocking_fetcher)
        self.assertFalse(result.is_success)
        self.assertEqual(result.stats.errors.get("blocked_by_robots"), 1)

    def test_invalid_and_unsupported_start_urls(self) -> None:
        for bad in ("javascript:void(0)", "mailto:a@b.com", "relative/path", ""):
            with self.subTest(start=bad):
                result = crawl_site(CrawlConfig(start_url=bad))
                self.assertFalse(result.is_success)
                self.assertIsNotNone(result.error_message)
                self.assertEqual(result.stats.errors.get("invalid_url"), 1)

    def test_start_url_normalized_in_stats(self) -> None:
        result, _ = self._crawl({"/": html_with_links()}, start="https://site.test")
        self.assertEqual(result.stats.normalized_start_url, f"{SCOPE}/")
        self.assertEqual(result.stats.start_url, "https://site.test")
        self.assertTrue(result.is_success)

    def test_determinism(self) -> None:
        pages = {"/": html_with_links("/x", "/y"), "/x": html_with_links("/z"), "/y": html_with_links(), "/z": html_with_links()}
        first, _ = self._crawl(pages, max_depth=3)
        second, _ = self._crawl(pages, max_depth=3)
        self.assertEqual(first.stats.to_dict(), second.stats.to_dict())

    def test_observation_links_are_discovery_only(self) -> None:
        pages = {"/": html_with_links("/leaf"), "/leaf": html_with_links()}
        result, _ = self._crawl(pages, max_depth=2)
        obs = result.pages_by_url[f"{SCOPE}/"].observation
        self.assertIsNotNone(obs)
        self.assertEqual(obs.num_links, 1)
        self.assertEqual(obs.links[0].href, f"{SCOPE}/leaf")


if __name__ == "__main__":
    unittest.main()