"""Unit tests for P1-T3 page-observation enrichment.

All tests run fully offline against local HTML fixtures / mocked fetch responses
(no internet access). Covers the T3 scenario matrix: title, meta description,
canonical, robots meta, X-Robots-Tag, headings, visible text, internal/external
links, relative-link resolution, JSON-LD (single/multiple/malformed), Open Graph,
malformed HTML, duplicate/fragment link normalization, redirect/final-URL
metadata, non-HTML handling, and deterministic observation failure states.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from src.crawler.crawler import FetchedPage, fetch_page
from src.observation.observation import JsonLdObservation, LinkObservation, observe_html, observe_page

SCOPE = "https://example.com"


def build_page(
    html: str,
    *,
    content_type: str = "text/html",
    headers: dict[str, str] | None = None,
    requested_url: str = f"{SCOPE}/product",
    final_url: str = f"{SCOPE}/product",
    status_code: int = 200,
    is_success: bool = True,
    error_message: str | None = None,
) -> FetchedPage:
    return FetchedPage(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
        html_content=html,
        is_success=is_success,
        error_message=error_message,
        response_headers=dict(headers or {}),
    )


class TestObservationEnrichment(unittest.TestCase):
    def test_title_extraction(self) -> None:
        obs = observe_html("<html><head><title>  Sea Salt Caramel  </title></head><body></body></html>")
        self.assertEqual(obs.title, "Sea Salt Caramel")
        self.assertTrue(obs.has_title)

    def test_meta_description_extraction(self) -> None:
        obs = observe_html(
            '<html><head><meta name="description" content="  Handmade chocolate truffles. "></head></html>'
        )
        self.assertEqual(obs.meta_description, "Handmade chocolate truffles.")
        self.assertTrue(obs.has_meta_description)

    def test_canonical_resolved_relative(self) -> None:
        obs = observe_html(
            '<html><head><link rel="canonical" href="/canonical-page"></head></html>',
            requested_url=f"{SCOPE}/deep/page",
        )
        self.assertEqual(obs.canonical_url, f"{SCOPE}/canonical-page")
        self.assertTrue(obs.has_canonical)

    def test_robots_meta_directives(self) -> None:
        obs = observe_html(
            '<html><head><meta name="robots" content="NOINDEX, nofollow"></head></html>'
        )
        self.assertEqual(obs.robots_meta_raw, "NOINDEX, nofollow")
        self.assertEqual(obs.robots_meta_directives, ("noindex", "nofollow"))
        self.assertTrue(obs.has_robots_meta)

    def test_robots_meta_case_insensitive_and_multiple(self) -> None:
        obs = observe_html(
            '<html><head>'
            '<meta NAME="ROBOTS" content="index">'
            '<meta name="robots" content="noarchive">'
            "</head></html>"
        )
        self.assertEqual(obs.robots_meta_directives, ("index", "noarchive"))
        self.assertEqual(obs.robots_meta_raw, "index, noarchive")

    def test_robots_meta_absent(self) -> None:
        obs = observe_html("<html><head><title>Nope</title></head></html>")
        self.assertIsNone(obs.robots_meta_raw)
        self.assertEqual(obs.robots_meta_directives, ())
        self.assertFalse(obs.has_robots_meta)

    def test_x_robots_tag_from_headers(self) -> None:
        page = build_page("<html><body><p>hi</p></body></html>",
                          headers={"X-Robots-Tag": "noindex, noarchive"})
        obs = observe_page(page)
        self.assertEqual(obs.x_robots_tag_raw, "noindex, noarchive")
        self.assertEqual(obs.x_robots_tag_directives, ("noindex", "noarchive"))
        self.assertTrue(obs.has_x_robots_tag)

    def test_x_robots_tag_case_insensitive_key_and_ua_prefix(self) -> None:
        page = build_page("<html></html>",
                          headers={"x-robots-tag": "googlebot: none"})
        obs = observe_page(page)
        self.assertEqual(obs.x_robots_tag_directives, ("none",))

    def test_x_robots_tag_directive_with_parameter(self) -> None:
        page = build_page(
            "<html></html>",
            headers={"X-Robots-Tag": "max-image-preview:large, unavailable_after: 2024-01-01"},
        )
        obs = observe_page(page)
        self.assertEqual(obs.x_robots_tag_directives, ("max-image-preview", "unavailable_after"))

    def test_robots_meta_and_header_together(self) -> None:
        page = build_page('<html><head><meta name="robots" content="noindex"></head></html>',
                          headers={"X-Robots-Tag": "nofollow"})
        obs = observe_page(page)
        self.assertEqual(obs.robots_meta_directives, ("noindex",))
        self.assertEqual(obs.x_robots_tag_directives, ("nofollow",))

    def test_heading_extraction(self) -> None:
        obs = observe_html(
            "<html><body><h1>One</h1><h2>Two</h2><h2>Three</h2><h3>Four</h3></body></html>"
        )
        self.assertEqual(obs.h1, ["One"])
        self.assertEqual(obs.h2, ["Two", "Three"])
        self.assertEqual(obs.h3, ["Four"])
        self.assertEqual(obs.num_headings, 4)

    def test_visible_text_excludes_script_and_style(self) -> None:
        obs = observe_html(
            "<html><head><style>.x{color:red}</style><script>var secret='s3cret';</script></head>"
            "<body><h1>Headline</h1><p>Body copy here.</p></body></html>"
        )
        self.assertIn("Headline", obs.visible_text)
        self.assertIn("Body copy here.", obs.visible_text)
        self.assertNotIn("s3cret", obs.visible_text)
        self.assertNotIn(".x", obs.visible_text)

    def test_internal_and_external_link_classification(self) -> None:
        page = build_page(
            '<html><body>'
            '<a href="/internal">Internal</a>'
            '<a href="https://example.com/absolute-internal">Abs Internal</a>'
            '<a href="https://other-site.com/out">External</a>'
            '<a href="mailto:a@example.com">Mail</a>'
            "</body></html>"
        )
        obs = observe_page(page)
        self.assertEqual([link.is_internal for link in obs.links], [True, True, False, False])
        self.assertEqual(obs.links[0].href, f"{SCOPE}/internal")
        self.assertEqual(obs.links[2].href, "https://other-site.com/out")

    def test_relative_link_resolution(self) -> None:
        page = build_page(
            '<html><body><a href="../sibling">Sibling</a>'
            '<a href="//cdn.example.com/res">Protocol-relative</a></body></html>',
            requested_url=f"{SCOPE}/a/b/product",
            final_url=f"{SCOPE}/a/b/page",
        )
        obs = observe_page(page)
        self.assertEqual(obs.links[0].href, f"{SCOPE}/a/sibling")
        self.assertEqual(obs.links[1].href, "https://cdn.example.com/res")

    def test_json_ld_single_block(self) -> None:
        obs = observe_html(
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget"}</script></head></html>'
        )
        self.assertEqual(len(obs.json_ld), 1)
        block = obs.json_ld[0]
        self.assertTrue(block.is_valid)
        self.assertEqual(block.parsed_data.get("@type"), "Product")

    def test_multiple_json_ld_blocks(self) -> None:
        obs = observe_html(
            '<html><head>'
            '<script type="application/ld+json">{"@type":"Organization"}</script>'
            '<script type="application/ld+json">{"@type":"WebSite"}</script>'
            "</head></html>"
        )
        self.assertEqual(len(obs.json_ld), 2)
        self.assertEqual(
            [block.parsed_data.get("@type") for block in obs.json_ld],
            ["Organization", "WebSite"],
        )

    def test_malformed_json_ld_is_not_fatal(self) -> None:
        obs = observe_html(
            '<html><head><script type="application/ld+json">{ broken json : }</script></head></html>'
        )
        self.assertEqual(len(obs.json_ld), 1)
        block = obs.json_ld[0]
        self.assertFalse(block.is_valid)
        self.assertIsNone(block.parsed_data)
        self.assertIsNotNone(block.error_message)

    def test_open_graph_metadata(self) -> None:
        obs = observe_html(
            '<html><head>'
            '<meta property="og:title" content="  Frosted Cake  ">'
            '<meta property="og:description" content="A rich dessert.">'
            '<meta property="og:type" content="product">'
            '<meta property="og:url" content="/cake">'
            "</head></html>"
        )
        self.assertEqual(obs.og_title, "Frosted Cake")
        self.assertEqual(obs.og_description, "A rich dessert.")
        self.assertEqual(obs.og_type, "product")
        self.assertEqual(obs.og_url, f"{SCOPE}/cake")
        self.assertTrue(obs.has_open_graph)

    def test_open_graph_via_name_attribute(self) -> None:
        obs = observe_html('<html><head><meta name="og:title" content="Named"></head></html>')
        self.assertEqual(obs.og_title, "Named")

    def test_malformed_html_is_tolerated(self) -> None:
        obs = observe_html(
            "<html><body><div>Partial <a href='/x'>link</a"  # unclosed tags
        )
        self.assertTrue(obs.is_success)
        self.assertEqual(obs.links[0].href, f"{SCOPE}/x")

    def test_duplicate_and_fragment_links_collapse_to_one_target(self) -> None:
        obs = observe_html(
            '<html><body>'
            '<a href="/same">one</a>'
            '<a href="/same#section">two</a>'
            '<a href="/same#other">three</a>'
            '<a href="HTTP://EXAMPLE.com/same">four</a>'
            "</body></html>"
        )
        hrefs = [link.href for link in obs.links]
        self.assertEqual(
            hrefs,
            [f"{SCOPE}/same", f"{SCOPE}/same", f"{SCOPE}/same", "http://example.com/same"],
        )
        self.assertEqual(obs.num_links, 4)
        for link in obs.links:
            self.assertNotIn("#", link.href)

    def test_redirect_final_url_metadata_preserved(self) -> None:
        page = build_page(
            "<html><body>Moved page</body></html>",
            requested_url="https://example.com/start",
            final_url="https://example.com/destination",
        )
        obs = observe_page(page)
        self.assertEqual(obs.requested_url, "https://example.com/start")
        self.assertEqual(obs.final_url, "https://example.com/destination")
        self.assertEqual(obs.status_code, 200)

    def test_non_html_page_observation_is_failure_state(self) -> None:
        page = build_page(
            '{"json": true}',
            content_type="application/json",
            is_success=False,
            error_message="Rejected non-HTML response (Content-Type: 'application/json').",
        )
        obs = observe_page(page)
        self.assertFalse(obs.is_success)
        self.assertEqual(obs.content_type, "application/json")
        self.assertIn("non-HTML", obs.error_message or "")
        self.assertIsNone(obs.title)
        self.assertEqual(obs.links, [])
        self.assertEqual(obs.x_robots_tag_directives, ())

    def test_observation_failure_is_deterministic(self) -> None:
        page = build_page(
            "",
            is_success=False,
            error_message="HTTP error status 500 (Internal Server Error).",
            status_code=500,
        )
        obs = observe_page(page)
        self.assertFalse(obs.is_success)
        self.assertEqual(obs.status_code, 500)
        self.assertIsNone(obs.title)
        self.assertIsNone(obs.meta_description)
        self.assertIsNone(obs.canonical_url)
        self.assertEqual(obs.h1, [])
        self.assertEqual(obs.visible_text, "")
        self.assertEqual(obs.links, [])
        self.assertEqual(obs.images, [])
        self.assertEqual(obs.tables, [])
        self.assertEqual(obs.json_ld, [])
        self.assertEqual(obs.robots_meta_directives, ())
        self.assertEqual(obs.x_robots_tag_directives, ())
        self.assertIsNone(obs.og_title)

    def test_content_type_and_response_headers_exposed(self) -> None:
        page = build_page(
            "<html><body>ok</body></html>",
            content_type="text/html; charset=utf-8",
            headers={"Content-Type": "text/html; charset=utf-8", "X-Powered-By": "test"},
        )
        obs = observe_page(page)
        self.assertEqual(obs.content_type, "text/html; charset=utf-8")
        self.assertEqual(obs.response_headers.get("X-Powered-By"), "test")
        self.assertEqual(obs.response_headers.get("Content-Type"), "text/html; charset=utf-8")

    def test_observe_html_defaults_to_empty_headers(self) -> None:
        obs = observe_html("<html><body>ok</body></html>")
        self.assertEqual(obs.response_headers, {})
        self.assertEqual(obs.content_type, "text/html")

    def test_end_to_end_headers_flow_from_fetch_to_observation(self) -> None:
        with patch("src.crawler.crawler.requests.get") as mock_get:
            response = MagicMock()
            response.status_code = 200
            response.url = f"{SCOPE}/page"
            response.headers = {"Content-Type": "text/html", "X-Robots-Tag": "noindex"}
            response.text = "<html><head><title>T</title></head><body>hi</body></html>"
            mock_get.return_value = response

            fetched = fetch_page(f"{SCOPE}/page", check_robots=False)
            obs = observe_page(fetched)
        self.assertTrue(obs.is_success)
        self.assertEqual(obs.x_robots_tag_directives, ("noindex",))
        self.assertEqual(obs.response_headers.get("X-Robots-Tag"), "noindex")

    def test_determinism_across_calls(self) -> None:
        html = (
            '<html><head><title>T</title>'
            '<meta name="robots" content="noindex">'
            '<meta property="og:title" content="OG">'
            "</head><body><a href='/a'>A</a><a href='/b'>B</a></body></html>"
        )
        first = observe_html(html)
        second = observe_html(html)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()