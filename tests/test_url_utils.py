"""Unit tests for P1-T1 URL utilities (src.crawler.url_utils).

Covers the URL-stage half of the T1 test matrix: relative/absolute resolution,
fragment handling, unsupported-scheme rejection, deduplication-friendly
canonicalization, and same-site/internal-vs-external classification.
"""

from __future__ import annotations

import unittest

from src.crawler.url_utils import (
    extract_site_key,
    is_crawlable_scheme,
    is_rejected_scheme,
    is_same_site,
    normalize_url,
    resolve_and_normalize,
    resolve_url,
)

BASE = "https://site.example/a/b/page.html"


class TestNormalizeUrl(unittest.TestCase):
    def test_fragment_stripped(self) -> None:
        self.assertEqual(normalize_url("https://site.example/about#team"), "https://site.example/about")
        self.assertEqual(normalize_url("https://site.example/about?x=1#team"), "https://site.example/about?x=1")

    def test_query_string_preserved(self) -> None:
        self.assertEqual(normalize_url("https://site.example/about?page=2&tab=spec"), "https://site.example/about?page=2&tab=spec")

    def test_scheme_and_host_lowercased(self) -> None:
        self.assertEqual(normalize_url("HTTPS://Example.COM/About"), "https://example.com/About")

    def test_empty_path_normalized_to_slash(self) -> None:
        self.assertEqual(normalize_url("https://site.example"), "https://site.example/")

    def test_default_port_stripped_nondefault_kept(self) -> None:
        self.assertEqual(normalize_url("https://site.example:443/x"), "https://site.example/x")
        self.assertEqual(normalize_url("http://site.example:80/x"), "http://site.example/x")
        self.assertEqual(normalize_url("https://site.example:8080/x"), "https://site.example:8080/x")

    def test_trailing_slash_preserved(self) -> None:
        # Conservative canonicalization: identity of the path is not rewritten.
        self.assertEqual(normalize_url("https://site.example/about/"), "https://site.example/about/")
        self.assertNotEqual(normalize_url("https://site.example/about"), "https://site.example/about/")

    def test_idna_host_encoded(self) -> None:
        self.assertEqual(normalize_url("https://ex\u00e1mple.com/"), "https://xn--exmple-qta.com/")

    def test_unsupported_or_relative_left_unchanged(self) -> None:
        self.assertEqual(normalize_url("javascript:void(0)"), "javascript:void(0)")
        self.assertEqual(normalize_url("mailto:a@b.com"), "mailto:a@b.com")
        self.assertEqual(normalize_url("/about#team"), "/about#team")


class TestSchemeClassification(unittest.TestCase):
    def test_crawlable_schemes(self) -> None:
        for url in ("https://site.example/", "http://site.example/", "https://site.example/a#b"):
            with self.subTest(url=url):
                self.assertTrue(is_crawlable_scheme(url))

    def test_rejected_schemes(self) -> None:
        for url in ("javascript:void(0)", "mailto:a@b.com", "tel:+123", "data:text/html,x", "file:///etc/hosts"):
            with self.subTest(url=url):
                self.assertTrue(is_rejected_scheme(url), f"expected rejection: {url}")

    def test_protocol_relative_and_relative_not_rejected(self) -> None:
        self.assertFalse(is_rejected_scheme("//cdn.example/x"))
        self.assertFalse(is_rejected_scheme("/about"))
        self.assertFalse(is_rejected_scheme("about"))


class TestResolution(unittest.TestCase):
    def test_root_relative(self) -> None:
        self.assertEqual(resolve_url(BASE, "/about"), "https://site.example/about")
        self.assertEqual(resolve_and_normalize(BASE, "/about"), "https://site.example/about")

    def test_absolute_internal(self) -> None:
        self.assertEqual(resolve_and_normalize(BASE, "https://site.example/about"), "https://site.example/about")

    def test_nested_relative(self) -> None:
        self.assertEqual(resolve_url(BASE, "c"), "https://site.example/a/b/c")
        self.assertEqual(resolve_url(BASE, "../c"), "https://site.example/a/c")

    def test_protocol_relative(self) -> None:
        self.assertEqual(resolve_url(BASE, "//cdn.site.example/x"), "https://cdn.site.example/x")

    def test_fragment_only_and_query_only(self) -> None:
        self.assertEqual(resolve_and_normalize(BASE, "#top"), "https://site.example/a/b/page.html")
        self.assertEqual(resolve_and_normalize(BASE, "?q=1"), "https://site.example/a/b/page.html?q=1")


class TestSameSiteClassification(unittest.TestCase):
    def test_scheme_agnostic(self) -> None:
        self.assertTrue(is_same_site("https://site.example/", "http://site.example/x"))

    def test_subdomain_is_external(self) -> None:
        self.assertFalse(is_same_site("https://site.example/", "https://www.site.example/x"))
        self.assertFalse(is_same_site("https://site.example/", "https://sub.site.example/x"))

    def test_different_port_is_external(self) -> None:
        self.assertFalse(is_same_site("https://site.example/", "https://site.example:8080/x"))

    def test_default_port_equivalent(self) -> None:
        self.assertTrue(is_same_site("https://site.example/", "https://site.example:443/x"))

    def test_non_http_never_matches(self) -> None:
        self.assertFalse(is_same_site("https://site.example/", "mailto:a@b.com"))
        self.assertFalse(is_same_site("https://site.example/", "relative"))

    def test_external_site_detection(self) -> None:
        self.assertFalse(is_same_site("https://site.example/", "https://external.example/"))

    def test_site_key(self) -> None:
        self.assertEqual(extract_site_key("https://Example.com:443/x"), "example.com")
        self.assertEqual(extract_site_key("http://example.com:8080/x"), "example.com:8080")
        self.assertIsNone(extract_site_key("javascript:void(0)"))
        self.assertIsNone(extract_site_key("relative"))


if __name__ == "__main__":
    unittest.main()