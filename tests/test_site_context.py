"""Unit tests for P1-T5 site-level crawl context (src.crawler.site_context).

All tests run fully offline against deterministic in-memory sites served by a
fake fetcher (test seam). Covers the T5 scenario matrix: internal link graph
construction, discovery sources (start/link/sitemap), sitemap semantics (never
conflated with literal link depth), unvisited-URL bounds, representative page
selection heuristics, determinism, and output constraints.
"""

from __future__ import annotations

import json
import urllib.parse
import unittest

from src.crawler.crawl_session import CrawlConfig, CrawlPageRecord, crawl_site
from src.crawler.crawler import FetchedPage
from src.observation.observation import observe_page
from src.crawler.site_context import (
    DEFAULT_MAX_REPRESENTATIVE_PAGES,
    RepresentativePage,
    SiteAuditContext,
    build_internal_graph,
    build_site_audit_context,
    select_representative_pages,
)
from src.crawler.url_utils import normalize_url

SCOPE = "https://site.test"


def html_with_links(*hrefs: str) -> str:
    """Build deterministic fixture HTML containing the given hrefs."""
    anchors = "".join(f'<a href="{href}">link {i + 1}</a>' for i, href in enumerate(hrefs))
    return f"<html><body><h1>Page</h1>{anchors}</body></html>"


def sitemap_xml(*urls: str) -> str:
    """Build a deterministic sitemap XML block listing the given URLs."""
    entries = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>"""


def path_area(url: str) -> str:
    """First non-empty path segment (lowercased); root maps to empty string."""
    path = urllib.parse.urlsplit(url).path.strip("/")
    if not path:
        return ""
    return path.split("/", 1)[0].lower()


class FakeSite:
    """An in-memory site the fake fetcher serves. Optionally serves robots/sitemaps."""

    def __init__(
        self,
        pages: dict[str, str],
        failures: set[str] | None = None,
        *,
        robots: str | None = None,
        sitemaps: dict[str, str] | None = None,
    ) -> None:
        self.pages = pages
        self.failures = failures or set()
        self.robots = robots
        self.sitemaps = sitemaps or {}
        self.fetch_counts: dict[str, int] = {}

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        path = urllib.parse.urlsplit(url).path
        if path in self.failures:
            return FetchedPage(
                requested_url=url,
                final_url=url,
                status_code=404,
                content_type="text/html",
                html_content="",
                is_success=False,
                error_message="HTTP error status 404 (Not Found).",
            )
        content_type = "text/html"
        html = ""
        if path == "/robots.txt" and self.robots is not None:
            content_type = "text/plain"
            html = self.robots
        elif path in self.sitemaps:
            content_type = "application/xml"
            html = self.sitemaps[path]
        elif path in self.pages:
            html = self.pages[path]
        if not html:
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
            content_type=content_type,
            html_content=html,
            is_success=True,
            error_message=None,
        )


class TestSiteContextBuild(unittest.TestCase):
    def _crawl(
        self,
        pages: dict[str, str],
        *,
        failures: set[str] | None = None,
        robots: str | None = None,
        sitemaps: dict[str, str] | None = None,
        **kwargs,
    ) -> tuple:
        site = FakeSite(pages, failures=failures, robots=robots, sitemaps=sitemaps)
        config = CrawlConfig(start_url=f"{SCOPE}/", **kwargs)
        result = crawl_site(config, fetcher=site.fetch)
        return result, site

    def _context(self, *args, **kwargs) -> SiteAuditContext:
        result, _ = self._crawl(*args, **kwargs)
        return build_site_audit_context(result)

    # ---- SITE CONTEXT: graph + page composition --------------------------------

    def test_one_page_site(self) -> None:
        ctx = self._context({"/": html_with_links()})
        self.assertEqual(ctx.page_urls, (f"{SCOPE}/",))
        self.assertEqual(ctx.successful_pages_urls, (f"{SCOPE}/",))
        self.assertEqual(ctx.failed_pages_urls, ())
        self.assertEqual(ctx.graph.node_count, 1)
        self.assertEqual(ctx.graph.edge_count, 0)
        self.assertEqual(ctx.stats.graph_edges, 0)
        self.assertEqual(ctx.stats.total_discovered, 1)

    def test_multi_page_site_flat(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/about", "/contact"), "/about": html_with_links(), "/contact": html_with_links()},
            max_depth=2,
        )
        self.assertEqual(ctx.page_urls, (f"{SCOPE}/", f"{SCOPE}/about", f"{SCOPE}/contact"))
        self.assertEqual(ctx.graph.nodes, (f"{SCOPE}/", f"{SCOPE}/about", f"{SCOPE}/contact"))
        self.assertEqual(
            ctx.graph.edges,
            ((f"{SCOPE}/", f"{SCOPE}/about"), (f"{SCOPE}/", f"{SCOPE}/contact")),
        )
        self.assertEqual(ctx.graph.outgoing[f"{SCOPE}/"], (f"{SCOPE}/about", f"{SCOPE}/contact"))
        self.assertEqual(ctx.graph.incoming[f"{SCOPE}/about"], (f"{SCOPE}/",))
        self.assertEqual(ctx.stats.internal_link_edges, 2)

    def test_cyclic_graph(self) -> None:
        ctx = self._context({"/": html_with_links("/b"), "/b": html_with_links("/", "/b")}, max_depth=5)
        self.assertEqual(ctx.graph.nodes, (f"{SCOPE}/", f"{SCOPE}/b"))
        self.assertEqual(
            ctx.graph.edges,
            ((f"{SCOPE}/", f"{SCOPE}/b"), (f"{SCOPE}/b", f"{SCOPE}/"), (f"{SCOPE}/b", f"{SCOPE}/b")),
        )
        self.assertEqual(ctx.graph.edge_count, 3)
        self.assertEqual(ctx.stats.graph_edges, 3)

    def test_external_links_excluded_from_graph(self) -> None:
        ctx = self._context(
            {"/": html_with_links("https://external.example/x", "/internal"), "/internal": html_with_links()},
            max_depth=2,
        )
        self.assertEqual(ctx.graph.nodes, (f"{SCOPE}/", f"{SCOPE}/internal"))
        self.assertEqual(ctx.graph.edges, ((f"{SCOPE}/", f"{SCOPE}/internal"),))
        self.assertEqual(ctx.external_links[self._norm(f"{SCOPE}/")], ("https://external.example/x",))
        self.assertEqual(ctx.stats.external_link_urls, 1)
        self.assertEqual(ctx.stats.graph_edges, 1)

    def test_duplicate_links_deduplicated_in_graph(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/dup", "/dup", "/dup", "/other"), "/dup": html_with_links(), "/other": html_with_links()},
            max_depth=2,
        )
        self.assertEqual(
            ctx.graph.edges,
            ((f"{SCOPE}/", f"{SCOPE}/dup"), (f"{SCOPE}/", f"{SCOPE}/other")),
        )
        self.assertEqual(ctx.stats.graph_edges, 2)

    def test_fragment_links_deduplicated_in_graph(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/frag#team", "/frag", "/other#x", "/other#y"), "/frag": html_with_links(), "/other": html_with_links()},
            max_depth=2,
        )
        self.assertEqual(
            ctx.graph.edges,
            ((f"{SCOPE}/", f"{SCOPE}/frag"), (f"{SCOPE}/", f"{SCOPE}/other")),
        )
        self.assertEqual(ctx.graph.edge_count, 2)
        self.assertNotIn(f"{SCOPE}/other#y", ctx.graph.nodes)

    def test_failed_page_represented_correctly(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/broken", "/ok"), "/ok": html_with_links()},
            failures={"/broken"},
            max_depth=2,
        )
        self.assertEqual(ctx.stats.failed_pages, 1)
        self.assertIn(f"{SCOPE}/broken", ctx.failed_pages_urls)
        self.assertIn(f"{SCOPE}/broken", ctx.page_urls)
        self.assertIn(f"{SCOPE}/broken", ctx.graph.nodes)
        self.assertIn((f"{SCOPE}/", f"{SCOPE}/broken"), ctx.graph.edges)
        self.assertNotIn(f"{SCOPE}/broken", ctx.successful_pages_urls)
        self.assertEqual(ctx.stats.successful_pages, 2)

    def test_empty_start_page(self) -> None:
        ctx = self._context({})
        self.assertEqual(ctx.page_urls, (f"{SCOPE}/",))
        self.assertFalse(ctx.page_by_url[f"{SCOPE}/"].is_success)
        self.assertEqual(ctx.graph.node_count, 0)

    def test_observations_referenced_not_copied(self) -> None:
        result, _ = self._crawl({"/": html_with_links("/about"), "/about": html_with_links()}, max_depth=2)
        ctx = build_site_audit_context(result)
        self.assertIs(ctx.observations[f"{SCOPE}/"], result.pages_by_url[f"{SCOPE}/"].observation)
        self.assertIsNotNone(ctx.observations[f"{SCOPE}/about"])

    def test_unvisited_urls_bounded_and_tagged(self) -> None:
        result, _ = self._crawl(
            {"/": html_with_links(*[f"/k{i}" for i in range(20)])},
            max_pages=5,
            max_depth=2,
        )
        ctx = build_site_audit_context(result)
        self.assertLessEqual(len(ctx.unvisited_urls), 5)
        for rec in ctx.unvisited_urls:
            self.assertEqual(rec.reason, "max_pages")
            self.assertEqual(rec.sources, ("link",))
            self.assertEqual(rec.discovered_from, f"{SCOPE}/")
        self.assertEqual(ctx.stats.unvisited_pages, len(ctx.unvisited_urls))

    def test_context_to_dict_json_serializable(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/about"), "/about": html_with_links()},
            max_depth=2,
        )
        json.dumps(ctx.to_dict())
        self.assertEqual(
            ctx.to_dict()["discovery_sources"][self._norm(f"{SCOPE}/")],
            ["start"],
        )

    def test_build_rejects_non_crawl_result(self) -> None:
        with self.assertRaises(TypeError):
            build_site_audit_context("not a crawl result")  # type: ignore[arg-type]

    def test_default_max_representative_pages(self) -> None:
        self.assertEqual(DEFAULT_MAX_REPRESENTATIVE_PAGES, 10)

    # ---- DISCOVERY SOURCES -----------------------------------------------------

    def test_start_discovery_source(self) -> None:
        ctx = self._context({"/": html_with_links()})
        self.assertEqual(ctx.discovery_sources[self._norm(f"{SCOPE}/")], ("start",))

    def test_link_discovery_source(self) -> None:
        ctx = self._context({"/": html_with_links("/by-link"), "/by-link": html_with_links()}, max_depth=2)
        self.assertEqual(ctx.discovery_sources[self._norm(f"{SCOPE}/by-link")], ("link",))

    def test_sitemap_discovery_source(self) -> None:
        ctx = self._context(
            {"/": html_with_links(), "/solo": html_with_links()},
            robots="Sitemap: https://site.test/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/solo")},
            max_depth=2,
        )
        self.assertEqual(ctx.discovery_sources[self._norm(f"{SCOPE}/solo")], ("sitemap",))
        self.assertIn(f"{SCOPE}/solo", ctx.sitemap_discovered_urls)
        self.assertEqual(ctx.stats.sitemap_urls_discovered, 1)

    def test_multiple_discovery_sources(self) -> None:
        ctx = self._context(
            {"/": html_with_links("/shared"), "/shared": html_with_links()},
            robots="Sitemap: https://site.test/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/shared")},
            max_depth=2,
        )
        normalized = self._norm(f"{SCOPE}/shared")
        self.assertEqual(ctx.discovery_sources[normalized], ("link", "sitemap"))

    def test_sitemap_seeded_pages_stay_at_depth_one(self) -> None:
        """Sitemap seeding must never be conflated with literal link depth."""
        ctx = self._context(
            {"/": html_with_links("/a"), "/a": html_with_links("/b"), "/b": html_with_links()},
            robots="Sitemap: https://site.test/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/a", f"{SCOPE}/b")},
            max_depth=3,
        )
        self.assertEqual(ctx.page_depths[self._norm(f"{SCOPE}/b")], 1)
        self.assertEqual(ctx.page_depths[self._norm(f"{SCOPE}/a")], 1)
        self.assertIn("sitemap", ctx.discovery_sources[self._norm(f"{SCOPE}/a")])

    def test_sitemap_seeded_page_without_links(self) -> None:
        """A sitemap-only page is reported at depth 1, never depth 0."""
        ctx = self._context(
            {"/": html_with_links(), "/deep": html_with_links()},
            robots="Sitemap: https://site.test/sitemap.xml\n",
            sitemaps={"/sitemap.xml": sitemap_xml(f"{SCOPE}/deep")},
            max_depth=2,
        )
        self.assertEqual(ctx.page_depths[self._norm(f"{SCOPE}/deep")], 1)

    # ---- REPRESENTATIVE PAGE SELECTION -----------------------------------------

    def test_homepage_always_preferred_when_successful(self) -> None:
        pages = {"/": html_with_links("/hub", *(f"/l{i}" for i in range(5)))}
        for leaf in ("/hub", *(f"/l{i}" for i in range(5))):
            pages[leaf] = html_with_links("/hub" if leaf != "/hub" else "")
        ctx = self._context(pages, max_depth=2)
        selection = ctx.representative_selection
        self.assertEqual(selection[0].url, f"{SCOPE}/")
        self.assertEqual(selection[0].rank, 1)
        self.assertEqual(selection[0].reason, "homepage_start")
        self.assertIn(f"{SCOPE}/hub", ctx.representative_pages)

    def test_high_connectivity_page_selected(self) -> None:
        pages = {"/": html_with_links("/hub", *(f"/l{i}" for i in range(5)))}
        for leaf in ("/hub", *(f"/l{i}" for i in range(5))):
            pages[leaf] = html_with_links("/hub" if leaf != "/hub" else "")
        ctx = self._context(pages, max_depth=2)
        hub = next(p for p in ctx.representative_selection if p.url == f"{SCOPE}/hub")
        self.assertEqual(hub.incoming_links, 6)

    def test_multiple_site_sections_represented(self) -> None:
        pages = {"/": html_with_links("/products/a", "/support/faq")}
        pages["/products/a"] = html_with_links("/products/b")
        pages["/products/b"] = html_with_links()
        pages["/support/faq"] = html_with_links()
        ctx = self._context(pages, max_depth=3)
        areas = {path_area(p.url) for p in ctx.representative_selection}
        self.assertIn("products", areas)
        self.assertIn("support", areas)

    def test_depth_diversity(self) -> None:
        pages = {"/": html_with_links("/x"), "/x": html_with_links("/x/y"), "/x/y": html_with_links()}
        ctx = self._context(pages, max_depth=3)
        depths = {p.depth for p in ctx.representative_selection}
        self.assertEqual(depths, {0, 1, 2})

    def test_deterministic_tie_breaking(self) -> None:
        pages = {"/": html_with_links("/a", "/b"), "/a": html_with_links(), "/b": html_with_links()}
        ctx = self._context(pages, max_depth=2)
        selection = ctx.representative_selection
        self.assertEqual([p.url for p in selection], [f"{SCOPE}/", f"{SCOPE}/a", f"{SCOPE}/b"])

    def test_fewer_pages_than_selection_limit(self) -> None:
        ctx = self._context({"/": html_with_links("/only"), "/only": html_with_links()}, max_depth=2)
        self.assertEqual(len(ctx.representative_selection), 2)

    def test_one_page_site_selection(self) -> None:
        ctx = self._context({"/": html_with_links()})
        self.assertEqual(ctx.representative_pages, (f"{SCOPE}/",))

    def test_zero_successful_pages(self) -> None:
        ctx = self._context({"/": html_with_links("/nope")}, failures={"/"}, max_depth=2)
        self.assertFalse(ctx.page_by_url[f"{SCOPE}/"].is_success)
        self.assertEqual(ctx.representative_selection, ())

    def test_selection_respects_maximum(self) -> None:
        pages = {"/": html_with_links(*(f"/s{i}" for i in range(9)))}
        for i in range(9):
            pages[f"/s{i}"] = html_with_links()
        ctx = self._context(pages, max_depth=2)
        result, _ = self._crawl(pages, max_depth=2)
        successful = tuple(p for p in result.pages if p.is_success)
        selected = select_representative_pages(
            successful_pages=successful,
            graph=ctx.graph,
            discovery_sources={p.url: p.discovery_sources for p in result.pages},
            normalized_start_url=f"{SCOPE}/",
            max_representative_pages=3,
        )
        self.assertLessEqual(len(selected), 3)

    def test_selection_returns_representative_page_objects(self) -> None:
        ctx = self._context({"/": html_with_links()})
        page = ctx.representative_selection[0]
        expected = RepresentativePage(
            url=f"{SCOPE}/",
            rank=1,
            incoming_links=0,
            outgoing_links=0,
            depth=0,
            reason="homepage_start",
        )
        self.assertEqual(page, expected)

    # ---- BOUNDS -----------------------------------------------------------------

    def test_max_depth_boundary_unvisited_records(self) -> None:
        result, _ = self._crawl({"/": html_with_links(*[f"/k{i}" for i in range(60)])}, max_pages=10, max_depth=0)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.stats.crawled_urls, [f"{SCOPE}/"])
        self.assertLessEqual(len(result.unvisited_urls), 10)
        self.assertTrue(result.unvisited_urls)
        self.assertTrue(all(rec.reason == "max_depth" for rec in result.unvisited_urls))

    def test_exhausted_budget_unvisited_records(self) -> None:
        result, _ = self._crawl({"/": html_with_links(*[f"/k{i}" for i in range(20)])}, max_pages=5, max_depth=2)
        self.assertEqual(result.stats.pages_attempted, 5)
        self.assertEqual(result.stats.urls_discovered, 5)
        self.assertLessEqual(len(result.unvisited_urls), 5)
        self.assertTrue(all(rec.reason in ("max_pages", "max_depth") for rec in result.unvisited_urls))

    def test_crawl_budget_authoritative_after_context_build(self) -> None:
        pages = {"/": html_with_links(*[f"/k{i}" for i in range(40)])}
        result, site = self._crawl(pages, max_pages=3, max_depth=1)
        before = dict(site.fetch_counts)
        ctx = build_site_audit_context(result)
        after = dict(site.fetch_counts)
        self.assertEqual(before, after)
        self.assertEqual(len(ctx.page_urls), 3)
        self.assertEqual(ctx.stats.total_discovered, 3)
        self.assertLessEqual(len(ctx.unvisited_urls), 3)

    # ---- DETERMINISM ------------------------------------------------------------

    def test_same_crawl_result_yields_identical_context(self) -> None:
        result, _ = self._crawl({"/": html_with_links("/a", "/b"), "/a": html_with_links(), "/b": html_with_links()}, max_depth=2)
        first = build_site_audit_context(result)
        second = build_site_audit_context(result)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first, second)

    def test_same_graph_yields_identical_selection(self) -> None:
        ctx = self._context({"/": html_with_links("/a", "/b"), "/a": html_with_links(), "/b": html_with_links()}, max_depth=2)
        result, _ = self._crawl({"/": html_with_links("/a", "/b"), "/a": html_with_links(), "/b": html_with_links()}, max_depth=2)
        successful = tuple(p for p in result.pages if p.is_success)
        one = select_representative_pages(
            successful_pages=successful,
            graph=ctx.graph,
            discovery_sources={p.url: p.discovery_sources for p in result.pages},
            normalized_start_url=f"{SCOPE}/",
        )
        two = select_representative_pages(
            successful_pages=successful,
            graph=ctx.graph,
            discovery_sources={p.url: p.discovery_sources for p in result.pages},
            normalized_start_url=f"{SCOPE}/",
        )
        self.assertEqual(one, two)

    def test_repeated_crawls_are_stable(self) -> None:
        pages = {"/": html_with_links("/products/a", "/support/faq")}
        pages["/products/a"] = html_with_links()
        pages["/support/faq"] = html_with_links()
        one = self._context(pages, max_depth=2)
        two = self._context(pages, max_depth=2)
        self.assertEqual(one.to_dict(), two.to_dict())

    # ---- helpers ----------------------------------------------------------------

    def _norm(self, url: str) -> str:
        return normalize_url(url)


class TestSiteGraphDirect(unittest.TestCase):
    @staticmethod
    def _page(url: str, depth: int, links: tuple[str, ...] = (), external: tuple[str, ...] = ()) -> CrawlPageRecord:
        fetched = FetchedPage(
            requested_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            html_content=html_with_links(*(links + external)),
            is_success=True,
            error_message=None,
        )
        return CrawlPageRecord(
            url=url,
            depth=depth,
            status_code=200,
            is_success=True,
            error_message=None,
            observation=observe_page(fetched),
            discovery_sources=("link",),
        )

    def test_build_internal_graph_master_slave(self) -> None:
        a = self._page(f"{SCOPE}/a", 0, links=(f"{SCOPE}/b",), external=("https://external.example/",))
        b = self._page(f"{SCOPE}/b", 1)
        graph = build_internal_graph([a, b], scope_url=f"{SCOPE}/")
        self.assertEqual(graph.nodes, (f"{SCOPE}/a", f"{SCOPE}/b"))
        self.assertEqual(graph.edges, ((f"{SCOPE}/a", f"{SCOPE}/b"),))
        self.assertEqual(graph.incoming[f"{SCOPE}/b"], (f"{SCOPE}/a",))
        self.assertEqual(graph.outgoing[f"{SCOPE}/a"], (f"{SCOPE}/b",))

    def test_build_internal_graph_deduplicates_and_filters(self) -> None:
        a = self._page(
            f"{SCOPE}/a",
            0,
            links=(f"{SCOPE}/b", f"{SCOPE}/b", "https://ext.example/", "mailto:x@y.com", f"{SCOPE}/a#frag"),
        )
        graph = build_internal_graph([a], scope_url=f"{SCOPE}/")
        self.assertEqual(graph.nodes, (f"{SCOPE}/a", f"{SCOPE}/b"))
        self.assertEqual(
            graph.edges,
            ((f"{SCOPE}/a", f"{SCOPE}/a"), (f"{SCOPE}/a", f"{SCOPE}/b")),
        )
        self.assertEqual(graph.incoming[f"{SCOPE}/a"], (f"{SCOPE}/a",))

    def test_build_internal_graph_without_scope(self) -> None:
        a = self._page(f"{SCOPE}/a", 0)
        graph = build_internal_graph([a], scope_url="")
        self.assertEqual(graph.nodes, (f"{SCOPE}/a",))
        self.assertEqual(graph.edges, ())

    def test_build_internal_graph_isolated_node(self) -> None:
        a = self._page(f"{SCOPE}/a", 0)
        graph = build_internal_graph([a], scope_url=f"{SCOPE}/")
        self.assertEqual(graph.nodes, (f"{SCOPE}/a",))
        self.assertEqual(graph.outgoing[f"{SCOPE}/a"], ())
