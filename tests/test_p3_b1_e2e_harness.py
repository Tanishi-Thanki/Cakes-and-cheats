"""P3-B1 (Person B): offline E2E evaluation suite (scenario fixtures + validation).

Runs the judge-style evaluation harness (tests/p3_b1_harness.py) against twelve
deterministic in-memory fixture sites and validates the full chain:

    website fixture -> crawl_site -> observe -> SiteAuditContext
        -> all six specialist audits -> aggregation -> final structured report

Scenario coverage (spec-mandated):
    1. strong / healthy site                (zero findings)
    2. poor machine-answerability           (MA-IMG-001, TR-FORM-001)
    3. inconsistent facts across pages       (MA-REP/FC-REP/EVID conflicts)
    4. freshness problems                    (FC-FRSH-001)
    5. structured-data vs visible mismatch   (EVID-PRC-001 / EVID-AVT-001)
    6. poor engagement / task flow           (TR-INT-001, TR-PROC-001, ENG-004)
    7. broken internal links                 (structured crawl failures)
    8. robots edge cases                     (Disallow block, missing robots)
    9. sitemap edge cases                    (index nesting, malformed, scoping)
   10. malformed / hostile HTML              (never crashes, always bounded)
   11. very small site                       (single page)
   12. larger bounded site                   (crawl budgets respected)

Plus explicit negative fixtures (findings SHOULD NOT be produced): a plain page
with no structured data, normal content variation across pages, an absent
external-reputation influence, and current (non-stale) content.

Validation: final report contract (total_findings == len(findings), severity
summary agrees, findings carry evidence + suggested_action), determinism across
repeat runs, crawl limits respected, offline execution (no network, no AI API),
and the read-only single-fetch property (no website modification).

Fully offline: fixtures are served by the FakeSite FetcherCallable seam; the
only entrypoint exercised that touches requests is run_audit_full, where
src.crawler.crawler.requests.get is patched (established repo pattern).
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path
from unittest.mock import patch

_HERE_DIR = Path(__file__).resolve().parent
if str(_HERE_DIR) not in sys.path:
    sys.path.insert(0, str(_HERE_DIR))

import requests
import unittest

from p3_b1_harness import (
    FakeSite,
    ok_page,
    fail_page,
    crawl_fixture,
    build_context,
    run_site_evaluation,
    validate_site_report,
)
from src.orchestrator import _DEFAULT_AUDITS, run_audit_full

HST = "https://healthy.test"
PRB = "https://problems.test"
PRD = "https://products.test"
FRS = "https://fresh.test"
CHK = "https://checkout.test"
BRK = "https://broken.test"
EDG = "https://edge.test"
CNV = "https://conventional.test"
HSTL = "https://hostile.test"
SML = "https://small.test"
BIG = "https://big.test"
VAR = "https://variance.test"


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


def pg(title: str, body: str, extra: str = "") -> str:
    return (
        "<!DOCTYPE html><html><head><title>"
        + title
        + "</title>"
        + extra
        + "</head><body>"
        + body
        + "</body></html>"
    )


def link(href: str, anchor: str | None = None) -> str:
    return f'<a href="{href}">{anchor if anchor is not None else href.split("/")[-1] or "home"}</a>'


def img(name: str, alt: str) -> str:
    return f"<img src=\"/{name}.png\" alt=\"{alt}\">"


# ---------------------------------------------------------------------------
# Scenario 1: strong / healthy site (expected: ZERO findings)
# ---------------------------------------------------------------------------

_HEALTHY_ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://healthy.test/sitemap.xml\n"
_HEALTHY_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<url><loc>{HST}/</loc></url><url><loc>{HST}/about</loc></url>"
    f"<url><loc>{HST}/products</loc></url><url><loc>{HST}/products/kenya</loc></url>"
    f"<url><loc>{HST}/products/colombia</loc></url><url><loc>{HST}/contact</loc></url>"
    "</urlset>"
)

_HEALTHY_PAGES = {
    "/": pg(
        "Northwind Coffee Roasters | Handcrafted Coffee",
        "<h1>Northwind Coffee Roasters</h1>"
        "<p>We roast single-origin beans from Kenya, Colombia, and Sumatra and deliver fresh coffee "
        "across the region. Every bag is roasted in small batches within 48 hours of shipping so the "
        "flavour is always at its peak.</p>"
        + link("/products", "View products")
        + link("/about", "About us")
        + link("/contact", "Contact us")
        + link("mailto:hello@healthy.test", "Email us"),
        '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",'
        f'"name":"Northwind Coffee Roasters","url":"{HST}/"}}</script>'
        '<meta name="description" content="We roast single-origin beans and deliver fresh coffee across the region.">'
        f'<link rel="canonical" href="{HST}/">'
        '<meta name="robots" content="index,follow">'
        '<meta property="og:title" content="Northwind Coffee Roasters">'
        '<meta property="og:type" content="website">',
    ),
    "/about": pg(
        "About Northwind Coffee Roasters",
        "<h1>About Northwind Coffee Roasters</h1>"
        "<p>Northwind Coffee Roasters was founded in 2012 and has roasted more than two million pounds "
        "of coffee since then. We buy directly from farmers in Kenya, Colombia, and Sumatra and pay "
        "above market prices for sustainable beans. Our small team cups every lot before it ships to "
        "ensure consistent quality in every bag.</p>"
        + link("/products", "Products")
        + link("/contact", "Contact"),
        '<meta name="description" content="The story of Northwind Coffee Roasters and how we source beans.">'
        f'<link rel="canonical" href="{HST}/about">',
    ),
    "/products": pg(
        "Coffee Products | Northwind Coffee Roasters",
        "<h1>Our Coffee Products</h1>"
        "<p>Browse our full range of single-origin coffees. All beans are roasted to order and shipped "
        "within 48 hours. Free shipping on orders over $30. See the specifications and tasting notes "
        "for each origin below before you decide.</p>"
        + link("/products/kenya", "Kenya AA")
        + link("/products/colombia", "Colombia Supremo")
        + link("/contact", "Contact"),
        '<meta name="description" content="Buy single-origin coffee beans from Northwind Coffee Roasters.">',
    ),
    "/products/kenya": pg(
        "Kenya AA Beans | Northwind Coffee Roasters",
        "<h1>Kenya AA Coffee Beans</h1>"
        "<p>A bright, complex coffee with notes of blackcurrant and citrus. Grown at high altitude in "
        "Nyeri County and washed-processed for a clean cup. The roast profile is light to medium and "
        "the flavour stays sweet through the finish.</p>"
        "<p>Price: $18. Net weight: 250g. Specifications: washed, grade AA, roasted to order.</p>"
        "<p>In stock.</p>"
        + link("/products", "Products")
        + link("/products/colombia", "Colombia Supremo")
        + link("/contact", "Contact"),
        '<meta name="description" content="Kenya AA single-origin coffee beans, $18 per bag.">'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Kenya AA Coffee Beans",'
        '"description":"Single-origin Kenya AA coffee beans.","offers":{"@type":"Offer",'
        '"price":"18.00","priceCurrency":"USD","availability":"https://schema.org/InStock"}}'
        "</script>",
    ),
    "/products/colombia": pg(
        "Colombia Supremo Beans | Northwind Coffee Roasters",
        "<h1>Colombia Supremo Coffee Beans</h1>"
        "<p>A smooth, sweet coffee with caramel and nut flavours. Sourced from the Huila region and "
        "sun-dried for a balanced body. The roast profile is medium and the cup is full and round, "
        "with a long cocoa finish.</p>"
        "<p>Price: $16. Net weight: 250g. Specifications: natural processed, big bean, sweet cup.</p>"
        "<p>In stock.</p>"
        + link("/products", "Products")
        + link("/products/kenya", "Kenya AA")
        + link("/contact", "Contact"),
        '<meta name="description" content="Colombia Supremo single-origin coffee beans, $16 per bag.">'
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Colombia Supremo Coffee Beans",'
        '"description":"Single-origin Colombia Supremo coffee beans.","offers":{"@type":"Offer",'
        '"price":"16.00","priceCurrency":"USD","availability":"https://schema.org/InStock"}}'
        "</script>",
    ),
    "/contact": pg(
        "Contact | Northwind Coffee Roasters",
        "<h1>Contact Northwind Coffee Roasters</h1>"
        "<p>Reach us by email at hello@healthy.test or call us on +1 555 010 2030. Our roasting "
        "facility is open Monday to Friday from eight in the morning until five in the evening. We "
        "answer every message within one business day.</p>"
        + link("/products", "Products")
        + link("/about", "About"),
        '<meta name="description" content="Contact Northwind Coffee Roasters.">',
    ),
}


def build_healthy_site() -> FakeSite:
    return FakeSite(pages=_HEALTHY_PAGES, robots=_HEALTHY_ROBOTS, sitemap=_HEALTHY_SITEMAP)


# ---------------------------------------------------------------------------
# Scenario 2: poor machine-answerability (problems.test)
# ---------------------------------------------------------------------------

_PROBLEMS_PAGES = {
    "/": pg(
        "Trails Hub",
        "<h1>Trails Hub</h1>"
        "<p>Stories and field guides from the mountain trail, updated every week.</p>"
        + link("/blog/photo-diary", "Photo diary")
        + link("/catalog", "Search catalog"),
    ),
    "/blog/photo-diary": pg(
        "Photo Diary | Trails Blog",
        "<h1>Photo diary from the alpine trail</h1>"
        + img("p1", "image") + img("p2", "photo") + img("p3", "thumbnail")
        + "<p>A short note about the trip.</p>"
        + link("/blog", "Back to blog"),
    ),
    "/catalog": pg(
        "Search Catalog | Trails Hub",
        "<h1>Search catalog</h1>"
        + img("c1", "image") + img("c2", "photo") + img("c3", "img")
        + link("/", "Home"),
    ),
}


def build_problems_site() -> FakeSite:
    return FakeSite(pages=_PROBLEMS_PAGES)


# ---------------------------------------------------------------------------
# Scenarios 3 & 5: inconsistent facts / structured-vs-visible mismatch
# ---------------------------------------------------------------------------

_PRODUCTS_PAGES = {
    "/": pg(
        "Acme Storefront",
        "<h1>Acme Storefront</h1>"
        "<p>Acme sells a small but reliable range of durable consumer goods. Each product page lists "
        "the current price, availability, and full specifications in structured form so you can "
        "compare before you buy.</p>"
        + link("/product/acme-shoes", "Acme Running Shoes")
        + link("/product/acme-widget", "Acme Widget")
        + link("mailto:sales@acme.test", "Contact sales"),
    ),
    "/product/acme-shoes": pg(
        "Acme Running Shoes",
        "<h1>Acme Running Shoes</h1><p>$999</p>" + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes",'
        '"offers":{"@type":"Offer","price":"949","priceCurrency":"USD",'
        '"availability":"https://schema.org/InStock"}}</script>',
    ),
    "/product/acme-widget": pg(
        "Acme Widgets",
        "<h1>Acme Widget</h1><p>$29.99 Out of stock</p>" + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Acme Widget",'
        '"offers":{"@type":"Offer","price":"29.99","priceCurrency":"USD",'
        '"availability":"https://schema.org/InStock"}}</script>',
    ),
}


def build_products_site() -> FakeSite:
    return FakeSite(pages=_PRODUCTS_PAGES)


# ---------------------------------------------------------------------------
# Scenario 4: freshness problems (fresh.test)
# ---------------------------------------------------------------------------


def _deal_page(valid_until: str, updated_on: str, title: str) -> str:
    return pg(
        title,
        "<h1>Acme Laptop Special Offer</h1>"
        "<p>Limited time offer on the Acme Ultra laptop.</p>"
        "<p>Price: $799. Specifications: 512GB storage, 16GB memory, 14-inch display.</p>"
        f"<p>Last updated: {updated_on}</p>"
        + link("/", "View product details")
        + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Acme Ultra Laptop",'
        '"offers":{"@type":"Offer","price":"799","priceCurrency":"USD",'
        f'"priceValidUntil":"{valid_until}","availability":"https://schema.org/InStock"}}</script>',
    )


_FRESHNESS_PAGES = {
    "/": pg(
        "Acme Deals Home",
        "<h1>Acme deals</h1><p>Browse our current laptop offers.</p>"
        + link("/deals", "Stale laptop deal")
        + link("/current-deal", "Current laptop deal")
        + link("/news/historical-article", "Historical article"),
    ),
    "/deals": _deal_page("2019-12-31", "2019-11-30", "Acme Laptop Deals | Limited Time Offer"),
    "/current-deal": _deal_page("2026-12-31", "2026-01-15", "Acme Laptop Deal | Current Offer"),
    "/news/historical-article": pg(
        "Historical Analysis | Acme Journal",
        "<h1>Historical article from 2018</h1>"
        "<p>This article covers a topic of purely historical interest and deliberately carries no "
        "pricing, offers, or availability data. Old dates on informational content are expected and "
        "are not treated as a freshness problem.</p>"
        + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"NewsArticle","name":"Historical Analysis",'
        '"datePublished":"2018-04-01","dateModified":"2018-04-02"}</script>',
    ),
}


def build_freshness_site() -> FakeSite:
    return FakeSite(pages=_FRESHNESS_PAGES)


# ---------------------------------------------------------------------------
# Scenario 6: poor engagement / task flow (checkout.test)
# ---------------------------------------------------------------------------

_CHECKOUT_PAGES = {
    "/": pg(
        "Welcome | Acme",
        "<h1>Welcome to Acme</h1>"
        "<p>Acme is a small online retailer of durable goods. Follow the checkout link below to "
        "place your order.</p>"
        + link("/checkout", "Checkout"),
    ),
    "/checkout": pg(
        "Checkout - Step 1 of 3",
        "<h1>Checkout</h1>"
        "<p>To complete your purchase, please proceed to checkout and confirm your details.</p>"
        '<a href="javascript:void(0)">Buy now</a>'
        '<a href="javascript:void(0)">Checkout</a>'
        + link("/", "Home"),
    ),
}


def build_checkout_site() -> FakeSite:
    return FakeSite(pages=_CHECKOUT_PAGES)


# ---------------------------------------------------------------------------
# Scenario 7: broken internal links (broken.test)
# ---------------------------------------------------------------------------

_BROKEN_PAGES = {
    "/": pg(
        "Broken Test Home",
        "<h1>Broken Test Home</h1>"
        "<p>A small site exercising broken-link handling. The links below point at pages in various "
        "failure states so the harness can verify that failures are recorded structurally without "
        "crashing the crawl.</p>"
        + link("/ok", "OK page")
        + link("/broken", "Broken page")
        + link("/slow", "Slow page")
        + link("/binary", "Binary file")
        + link("/gone", "Gone page"),
    ),
    "/ok": pg("OK Page", "<h1>OK Page</h1><p>This page loads correctly.</p>" + link("/", "Home")),
}

_BROKEN_FAILURES = {
    "/broken": fail_page(f"{BRK}/broken", "HTTP error status 404 (Not Found).", 404),
    "/gone": fail_page(f"{BRK}/gone", "HTTP error status 500 (Internal Server Error).", 500),
    "/slow": fail_page(f"{BRK}/slow", "Request timed out after 10.0s: read timed out."),
    "/binary": fail_page(f"{BRK}/binary", "Rejected non-HTML response (Content-Type: 'image/png').", 200),
}


def build_broken_site() -> FakeSite:
    return FakeSite(pages=_BROKEN_PAGES, failures=_BROKEN_FAILURES)


# ---------------------------------------------------------------------------
# Scenario 8: robots edge cases (edge.test + conventional fallback)
# ---------------------------------------------------------------------------

_EDGE_PAGES = {
    "/": pg(
        "Edge Home",
        "<h1>Edge Home</h1>"
        "<p>Welcome to the Edge test site. This homepage contains a full paragraph of ordinary text "
        "so that automated systems receive plenty of readable content directly from the server-"
        "delivered HTML, plus navigation links to the rest of the site.</p>"
        + link("/public", "Public page")
        + link("/private", "Private area")
        + link("/about", "About"),
    ),
    "/public": pg(
        "Public Page", "<h1>Public Page</h1><p>This page is public and crawlable.</p>"
        + link("/", "Home") + link("/about", "About"),
    ),
    "/about": pg(
        "About Edge", "<h1>About</h1><p>About the edge site.</p>"
        + link("/", "Home") + link("/public", "Public"),
    ),
}

_EDGE_ROBOTS = "User-agent: *\nDisallow: /private\nSitemap: https://edge.test/sitemap.xml\n"
_EDGE_SITEMAP_INDEX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<sitemap><loc>{EDG}/sitemap-a.xml</loc></sitemap>"
    f"<sitemap><loc>{EDG}/bad.xml</loc></sitemap>"
    f"<sitemap><loc>https://other.test/sitemap.xml</loc></sitemap>"
    "</sitemapindex>"
)
_EDGE_SITEMAP_A = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<url><loc>{EDG}/</loc></url><url><loc>{EDG}/about</loc></url>"
    f"<url><loc>{EDG}/public</loc></url><url><loc>{EDG}/private</loc></url>"
    "</urlset>"
)


def build_edge_site() -> FakeSite:
    return FakeSite(
        pages=_EDGE_PAGES,
        robots=_EDGE_ROBOTS,
        sitemap=_EDGE_SITEMAP_INDEX,
        sitemaps={
            "/sitemap-a.xml": _EDGE_SITEMAP_A,
            "/bad.xml": "<sitemap><loc>oops</urlset>",
        },
        failures={
            "/private": fail_page(
                f"{EDG}/private", "Access denied by robots.txt: path /private disallowed by robots.txt"
            ),
        },
    )


_CONV_PAGES = {
    "/": pg(
        "Conventional Home",
        "<h1>Conventional Home</h1><p>This site has no robots.txt but exposes the conventional "
        "sitemap location so discovery should probe it and fall back successfully.</p>"
        + link("/about", "About"),
    ),
    "/about": pg("Conventional About", "<h1>About</h1><p>An about page.</p>" + link("/", "Home")),
}
_CONV_SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    f"<url><loc>{CNV}/</loc></url><url><loc>{CNV}/about</loc></url>"
    "</urlset>"
)


def build_conventional_site() -> FakeSite:
    return FakeSite(pages=_CONV_PAGES, sitemap=_CONV_SITEMAP)


# ---------------------------------------------------------------------------
# Scenario 10: malformed / hostile HTML (hostile.test)
# ---------------------------------------------------------------------------

_HOSTILE_LINKS = "".join(
    [
        link("/x0"), link("/x1"), link("/x2"), link("/x3"), link("/x4"),
        link("/image.png", "binary"), link("/oversized", "oversized"), link("/slow", "slow"),
        link("/a1"), link("/a2"), link("/a3"), link("/a4"), link("/a5"), link("/a6"),
        link("#top", "top"), link(f"{HSTL}:abc/bad-port", "badport"),
        link("https://external.example/path", "external"),
    ]
)
_HOSTILE_PAGES = {
    "/": (
        "<html><body><h1>Hostile <b>markup</b></div><p>control \x00\x01 was here</p>"
        + _HOSTILE_LINKS
        + "</body></html>"
    ),
    "/x0": "<html><body><p>x0</p></body></html>",
    "/x1": "<html><body><p>x1</p></body></html>",
    "/x2": "<html><body><p>x2</p></body></html>",
    "/x3": "<html><body><p>x3</p></body></html>",
    "/x4": "<html><body><p>x4</p></body></html>",
}
_HOSTILE_FAILURES = {
    "/image.png": fail_page(f"{HSTL}/image.png", "Rejected non-HTML response (Content-Type: 'image/png').", 200),
    "/oversized": fail_page(f"{HSTL}/oversized", "Response body exceeds the 5000000-byte limit."),
    "/slow": fail_page(f"{HSTL}/slow", "Request timed out after 10.0s: read timed out."),
}


def build_hostile_site() -> FakeSite:
    return FakeSite(pages=_HOSTILE_PAGES, failures=_HOSTILE_FAILURES)


# ---------------------------------------------------------------------------
# Scenario 11: very small site (small.test)
# ---------------------------------------------------------------------------

_SMALL_PAGES = {
    "/": pg(
        "Small Crafts | Handmade Goods",
        "<h1>Small Crafts</h1>"
        "<p>We make handmade leather goods one piece at a time in a small workshop. Every wallet and "
        "belt is cut, stitched, and finished by hand using vegetable-tanned leather that ages well "
        "and lasts for years.</p>"
        + link("mailto:hello@small.test", "Email us"),
        '<meta name="description" content="Handmade leather goods from Small Crafts.">'
        f'<link rel="canonical" href="{SML}/">',
    ),
}


def build_small_site() -> FakeSite:
    return FakeSite(pages=_SMALL_PAGES)


# ---------------------------------------------------------------------------
# Scenario 12: larger bounded site (big.test)
# ---------------------------------------------------------------------------


def _big_product_page(i: int) -> str:
    return pg(
        f"Product {i} | Big Store",
        f"<h1>Product {i}</h1>"
        f"<p>Product {i} is a high quality item with clear specifications and a fair price. Net "
        f"weight: 500g, colour: grey, material: recycled plastic, and it includes a two-year "
        f"warranty. Price: ${10 + i}. In stock.</p>"
        + link("/", "View details")
        + link(f"/p{(i + 1) % 40}", "Next product")
        + link(f"/detail/{i}", "Full details"),
    )


def _big_detail_page(i: int) -> str:
    return pg(
        f"Full Details for Product {i} | Big Store",
        f"<h1>Full details for product {i}</h1>" +
        f"<p>All technical specifications for product {i} are listed here, including dimensions, "
        f"materials, warranty terms, and assembly notes. Price: ${10 + i}. In stock.</p>"
        + link("/", "View details")
        + link(f"/p0", "Back to product")
        + link(f"/leaf/{i}", "Leaf"),
    )


def build_big_site() -> FakeSite:
    pages: dict[str, str] = {
        "/": pg(
            "Big Store",
            "<h1>Big Store</h1>"
            "<p>We sell many products across many categories and each one has its own page below. "
            "Browse the full catalogue and read the details for each item you are interested in "
            "before ordering.</p>"
            + "".join(link(f"/p{i}", f"Product {i}") for i in range(40))
            + link("/p0", "View catalog details"),
        )
    }
    pages.update({f"/p{i}": _big_product_page(i) for i in range(40)})
    pages.update({f"/detail/{i}": _big_detail_page(i) for i in range(40)})
    return FakeSite(pages=pages)


# ---------------------------------------------------------------------------
# Negative fixtures (pages where findings SHOULD NOT be produced)
# ---------------------------------------------------------------------------

_PLAIN_PAGES = {
    "/": pg(
        "Plain Informational Page",
        "<h1>An Informational Article</h1>"
        "<p>This page describes a topic in ordinary prose. It contains enough words to be readable by "
        "humans and by automated systems alike. There is no image, no structured data, and no table "
        "on this page. Absence of structured data alone is deliberately not a defect.</p>"
        + link("/", "Home"),
        '<meta name="description" content="A plain informational article.">',
    ),
}


def build_plain_site() -> FakeSite:
    return FakeSite(pages=_PLAIN_PAGES)


_VARIANCE_PAGES = {
    "/": pg(
        "Lumio Home",
        "<h1>Lumio Home</h1>"
        "<p>Lumio sells carefully selected lighting and furniture. Each listing states a different "
        "price because each product really is priced differently; normal content variation across "
        "pages must not be reported as an inconsistency.</p>"
        + link("/catalog/lamp", "Arc Lamp")
        + link("/catalog/chair", "Oak Chair"),
    ),
    "/catalog/lamp": pg(
        "Arc Lamp | Lumio Home",
        "<h1>Arc Lamp</h1>"
        "<p>A minimalist floor lamp in matte black steel. Specifications: E27 socket, 220cm height, "
        "dimmable. Price: $240. In stock.</p>"
        + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Arc Lamp",'
        '"offers":{"@type":"Offer","price":"240","priceCurrency":"USD",'
        '"availability":"https://schema.org/InStock"}}</script>',
    ),
    "/catalog/chair": pg(
        "Oak Chair | Lumio Home",
        "<h1>Oak Chair</h1>"
        "<p>A solid oak dining chair with a woven seat. Specifications: 45cm seat height, 60cm width, "
        "60kg weight. Price: $310. In stock.</p>"
        + link("/", "Home"),
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Product","name":"Oak Chair",'
        '"offers":{"@type":"Offer","price":"310","priceCurrency":"USD",'
        '"availability":"https://schema.org/InStock"}}</script>',
    ),
}


def build_variance_site() -> FakeSite:
    return FakeSite(pages=_VARIANCE_PAGES)


# ===========================================================================
# Scenario test classes
# ===========================================================================


class TestScenarioStrongHealthySite(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_healthy_site(), f"{HST}/", max_pages=50, max_depth=3)

    def test_01_all_pages_produce_zero_findings(self) -> None:
        expected_urls = [f"{HST}/{u}" for u in ("", "about", "products", "products/kenya", "products/colombia", "contact")]
        self.assertEqual(set(self.eval.ctx.page_urls), set(expected_urls))
        for url in expected_urls:
            self.assertEqual(self.eval.finding_ids(url), (), f"unexpected findings on {url}")

    def test_02_crawl_stats_and_sitemap_discovery(self) -> None:
        stats = self.eval.result.stats
        self.assertTrue(self.eval.result.is_success)
        self.assertEqual(stats.pages_attempted, 6)
        self.assertEqual(stats.pages_fetched, 6)
        self.assertEqual(stats.pages_failed, 0)
        self.assertEqual(stats.errors, {})
        discovery = self.eval.result.sitemap_discovery
        self.assertTrue(discovery.robots_fetched)
        self.assertFalse(discovery.conventional_checked)
        self.assertEqual(sorted(discovery.discovered_urls), sorted(f"{HST}{p}" for p in _HEALTHY_PAGES))
        self.assertEqual(stats.sitemap_files_parsed, 1)

    def test_03_no_external_or_non_http_fetch(self) -> None:
        self.assertNotIn("https://external.example/", self.eval.result.pages_by_url)
        for url in self.eval.result.stats.crawled_urls:
            self.assertTrue(url.startswith(f"{HST}/"), url)
        # Every page fetched exactly once (deterministic, read-only).
        for count in build_healthy_site().fetch_counts.values():  # noqa: B905
            self.assertEqual(count, 1)

    def test_04_final_report_has_zero_findings(self) -> None:
        report = self.eval.final
        self.assertEqual(report["summary"], {"total_findings": 0, "high": 0, "medium": 0, "low": 0})
        self.assertEqual(report["findings"], [])


class TestScenarioPoorMachineAnswerability(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_problems_site(), f"{PRB}/", max_pages=10, max_depth=2)

    def test_blog_image_dependency_flagged(self) -> None:
        blog = f"{PRB}/blog/photo-diary"
        self.assertEqual(self.eval.finding_ids(blog), ("MA-IMG-001",))

    def test_catalog_capability_deficit_flagged(self) -> None:
        catalog = f"{PRB}/catalog"
        self.assertEqual(self.eval.finding_ids(catalog), ("MA-IMG-001", "TR-FORM-001"))
        self.assertEqual(self.eval.audit_ids(catalog, "task_readiness"), ("TR-FORM-001",))

    def test_home_clean(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{PRB}/"), ())


class TestScenarioInconsistentFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_products_site(), f"{PRD}/", max_pages=10, max_depth=2)

    def test_price_conflict_surfaces_across_audits(self) -> None:
        shoes = f"{PRD}/product/acme-shoes"
        self.assertEqual(
            self.eval.finding_ids(shoes),
            ("ENG-003", "EVID-PRC-001", "FC-REP-001", "MA-REP-001", "TR-DATA-001"),
        )
        self.assertEqual(self.eval.audit_ids(shoes, "evidence_consistency"), ("EVID-PRC-001",))

    def test_availability_conflict_surfaces_across_audits(self) -> None:
        widget = f"{PRD}/product/acme-widget"
        self.assertEqual(
            self.eval.finding_ids(widget),
            ("EVID-AVT-001", "FC-REP-002", "MA-REP-002", "TR-DATA-001"),
        )

    def test_home_is_clean(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{PRD}/"), ())


class TestScenarioStructuredVsVisibleMismatch(unittest.TestCase):
    """Structured (JSON-LD) data contradicts what a human sees in visible text."""

    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_products_site(), f"{PRD}/", max_pages=10, max_depth=2)

    def test_evidence_cites_both_conflicting_values(self) -> None:
        shoes = f"{PRD}/product/acme-shoes"
        evid = self.eval.per_page[shoes]["evidence_consistency"][0]
        self.assertEqual(evid.id, "EVID-PRC-001")
        self.assertIn("949", evid.evidence)
        self.assertIn("999", evid.evidence)

    def test_availability_evidence_cites_both_states(self) -> None:
        widget = f"{PRD}/product/acme-widget"
        evid = self.eval.per_page[widget]["evidence_consistency"][0]
        self.assertEqual(evid.id, "EVID-AVT-001")
        self.assertIn("Out of Stock", evid.evidence)
        self.assertIn("InStock", evid.evidence)


class TestScenarioFreshnessProblems(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_freshness_site(), f"{FRS}/", max_pages=10, max_depth=2)

    def test_stale_time_sensitive_deal_flagged(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{FRS}/deals"), ("FC-FRSH-001",))

    def test_current_deal_not_flagged(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{FRS}/current-deal"), ())

    def test_historical_article_not_flagged(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{FRS}/news/historical-article"), ())

    def test_home_clean(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{FRS}/"), ())


class TestScenarioPoorEngagementTaskFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_checkout_site(), f"{CHK}/", max_pages=10, max_depth=2)

    def test_checkout_workflow_breakdown_detected(self) -> None:
        checkout = f"{CHK}/checkout"
        self.assertEqual(
            self.eval.finding_ids(checkout),
            ("ENG-004", "TR-INT-001", "TR-PROC-001"),
        )
        self.assertEqual(self.eval.audit_ids(checkout, "task_readiness"), ("TR-INT-001", "TR-PROC-001"))
        self.assertEqual(self.eval.audit_ids(checkout, "engagement"), ("ENG-004",))

    def test_home_clean(self) -> None:
        self.assertEqual(self.eval.finding_ids(f"{CHK}/"), ())


class TestScenarioBrokenInternalLinks(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_broken_site(), f"{BRK}/", max_pages=10, max_depth=2)

    def test_failures_recorded_with_stable_categories(self) -> None:
        stats = self.eval.result.stats
        self.assertTrue(self.eval.result.is_success)
        self.assertEqual(stats.pages_attempted, 6)
        self.assertEqual(stats.pages_fetched, 2)
        self.assertEqual(stats.pages_failed, 4)
        self.assertEqual(stats.errors, {"http_error": 2, "non_html": 1, "timeout": 1})
        self.assertEqual(len(self.eval.result.pages), 6)

    def test_failed_pages_structured_and_isolated(self) -> None:
        for path in ("/broken", "/gone", "/slow", "/binary"):
            record = self.eval.result.pages_by_url[f"{BRK}{path}"]
            self.assertFalse(record.is_success)
            self.assertIsNone(record.observation)
            self.assertTrue(record.error_message)

    def test_crawl_continues_after_failures(self) -> None:
        self.assertIn(f"{BRK}/ok", self.eval.result.stats.crawled_urls)
        self.assertEqual(self.eval.finding_ids(f"{BRK}/ok"), ())
        self.assertEqual(self.eval.finding_ids(f"{BRK}/"), ())


class TestScenarioRobotsEdges(unittest.TestCase):
    def test_disallowed_path_blocks_while_site_continues(self) -> None:
        ev = run_site_evaluation(build_edge_site(), f"{EDG}/", max_pages=50, max_depth=3)
        stats = ev.result.stats
        self.assertTrue(ev.result.is_success)
        self.assertEqual(stats.errors, {"blocked_by_robots": 1})
        private = ev.result.pages_by_url[f"{EDG}/private"]
        self.assertFalse(private.is_success)
        self.assertIn("robots.txt", private.error_message or "")
        for url in (f"{EDG}/", f"{EDG}/public", f"{EDG}/about"):
            self.assertEqual(ev.finding_ids(url), ())

    def test_robots_sitemap_directive_received(self) -> None:
        ev = run_site_evaluation(build_edge_site(), f"{EDG}/", max_pages=50, max_depth=3)
        discovery = ev.result.sitemap_discovery
        self.assertTrue(discovery.robots_fetched)
        self.assertEqual(discovery.robots_sitemap_urls, [f"{EDG}/sitemap.xml"])
        self.assertFalse(discovery.conventional_checked)

    def test_missing_robots_fails_open(self) -> None:
        ev = run_site_evaluation(build_small_site(), f"{SML}/", max_pages=5, max_depth=2)
        self.assertTrue(ev.result.is_success)
        discovery = ev.result.sitemap_discovery
        self.assertFalse(discovery.robots_fetched)
        self.assertTrue(discovery.conventional_checked)


class TestScenarioSitemapEdges(unittest.TestCase):
    def test_sitemap_index_nested_and_scoped(self) -> None:
        ev = run_site_evaluation(build_edge_site(), f"{EDG}/", max_pages=50, max_depth=3)
        discovery = ev.result.sitemap_discovery
        stats = ev.result.stats
        self.assertEqual(stats.sitemap_files_parsed, 2)
        self.assertEqual(stats.sitemap_urls_discovered, 3)
        self.assertEqual(
            discovery.discovered_urls,
            [f"{EDG}/", f"{EDG}/about", f"{EDG}/public", f"{EDG}/private"],
        )
        for url in discovery.discovered_urls:
            self.assertTrue(url.startswith(f"{EDG}/"), f"external URL leaked into discovery: {url}")
        self.assertNotIn("https://other.test/", discovery.discovered_urls)

    def test_malformed_sitemap_is_counted_not_fatal(self) -> None:
        ev = run_site_evaluation(build_edge_site(), f"{EDG}/", max_pages=50, max_depth=3)
        self.assertEqual(ev.result.sitemap_discovery.errors, {"malformed_sitemap": 1})
        self.assertTrue(ev.result.is_success)

    def test_conventional_sitemap_probed_when_no_robots(self) -> None:
        ev = run_site_evaluation(build_conventional_site(), f"{CNV}/", max_pages=10, max_depth=2)
        discovery = ev.result.sitemap_discovery
        self.assertTrue(discovery.conventional_checked)
        self.assertFalse(discovery.robots_fetched)
        self.assertEqual(sorted(discovery.discovered_urls), sorted([f"{CNV}/", f"{CNV}/about"]))
        self.assertEqual(ev.finding_ids(f"{CNV}/"), ())


class TestScenarioMalformedHostileHTML(unittest.TestCase):
    def setUp(self) -> None:
        self.eval = run_site_evaluation(build_hostile_site(), f"{HSTL}/", max_pages=12, max_depth=2)

    def test_home_observed_despite_malformed_markup(self) -> None:
        home = self.eval.result.pages_by_url[f"{HSTL}/"]
        self.assertTrue(home.is_success)
        self.assertIsNotNone(home.observation)
        self.assertTrue(home.observation.is_success)

    def test_budget_bounded_and_error_categories_isolated(self) -> None:
        stats = self.eval.result.stats
        self.assertEqual(stats.pages_attempted, 12)
        self.assertLessEqual(stats.pages_attempted, 12)
        self.assertEqual(stats.errors.get("non_html"), 1)
        self.assertEqual(stats.errors.get("response_too_large"), 1)
        self.assertEqual(stats.errors.get("timeout"), 1)
        self.assertGreaterEqual(stats.errors.get("http_error", 0), 3)

    def test_rejected_and_external_urls_never_fetched(self) -> None:
        for url in build_hostile_site().fetch_counts:  # noqa: B905
            self.assertTrue(url.startswith(f"{HSTL}/"), url)
        self.assertNotIn(f"{HSTL}:abc/bad-port", build_hostile_site().fetch_counts)
        self.assertFalse(any("external.example" in key for key in build_hostile_site().fetch_counts))

    def test_deterministic_across_runs(self) -> None:
        other = run_site_evaluation(build_hostile_site(), f"{HSTL}/", max_pages=12, max_depth=2)
        self.assertEqual(self.eval.result.stats.to_dict(), other.result.stats.to_dict())
        self.assertEqual(self.eval.ids_across_site(), other.ids_across_site())


class TestScenarioVerySmallSite(unittest.TestCase):
    def test_single_page_site_runs_clean(self) -> None:
        ev = run_site_evaluation(build_small_site(), f"{SML}/", max_pages=5, max_depth=2)
        self.assertTrue(ev.result.is_success)
        self.assertEqual(ev.result.stats.pages_attempted, 1)
        self.assertEqual(ev.result.stats.pages_fetched, 1)
        self.assertEqual(ev.result.stats.pages_failed, 0)
        self.assertEqual(ev.finding_ids(f"{SML}/"), ())
        self.assertEqual(ev.final["summary"]["total_findings"], 0)


class TestScenarioLargerBoundedSite(unittest.TestCase):
    def test_budget_limited_crawl_respects_max_pages(self) -> None:
        ev = run_site_evaluation(build_big_site(), f"{BIG}/", max_pages=35, max_depth=2)
        stats = ev.result.stats
        self.assertTrue(ev.result.is_success)
        self.assertEqual(stats.pages_attempted, 35)
        self.assertEqual(stats.pages_fetched, 35)
        self.assertEqual(stats.pages_failed, 0)
        self.assertLessEqual(stats.urls_discovered, 35)
        self.assertTrue(any(rec.reason == "max_pages" for rec in ev.result.unvisited_urls))
        # Clean, fully specified product/detail/hub pages produce no findings.
        self.assertEqual(ev.finding_ids(f"{BIG}/"), ())
        self.assertEqual(ev.ids_across_site(), ())

    def test_depth_limited_crawl_respects_max_depth(self) -> None:
        ev = run_site_evaluation(build_big_site(), f"{BIG}/", max_pages=100, max_depth=2)
        self.assertTrue(ev.result.is_success)
        reasons = {rec.reason for rec in ev.result.unvisited_urls}
        self.assertIn("max_depth", reasons)
        self.assertLessEqual(len(ev.result.unvisited_urls), 100)
        self.assertLessEqual(ev.result.stats.max_depth_reached, 2)
        self.assertEqual(ev.ids_across_site(), ())

    def test_deterministic_across_runs(self) -> None:
        first = run_site_evaluation(build_big_site(), f"{BIG}/", max_pages=35, max_depth=2)
        second = run_site_evaluation(build_big_site(), f"{BIG}/", max_pages=35, max_depth=2)
        self.assertEqual(first.result.stats.to_dict(), second.result.stats.to_dict())
        self.assertEqual(json.dumps(first.final, sort_keys=True), json.dumps(second.final, sort_keys=True))


class TestNegativeFixtures(unittest.TestCase):
    def test_plain_page_without_structured_data_is_clean(self) -> None:
        ev = run_site_evaluation(build_plain_site(), f"{HST}/", max_pages=5, max_depth=2)
        self.assertEqual(ev.finding_ids(f"{HST}/"), ())
        self.assertEqual(ev.final["summary"]["total_findings"], 0)

    def test_normal_content_variation_across_pages_is_clean(self) -> None:
        ev = run_site_evaluation(build_variance_site(), f"{VAR}/", max_pages=10, max_depth=2)
        for url in (f"{VAR}/", f"{VAR}/catalog/lamp", f"{VAR}/catalog/chair"):
            self.assertEqual(ev.finding_ids(url), (), f"unexpected findings on {url}")
        self.assertEqual(ev.final["summary"]["total_findings"], 0)

    def test_external_reputation_does_not_influence_results(self) -> None:
        ev = run_site_evaluation(build_healthy_site(), f"{HST}/", max_pages=50, max_depth=3)
        # External links exist on the fixture but are never fetched, and no
        # finding is generated for or influenced by an external host.
        self.assertFalse(any("external.example" in u for u in ev.result.stats.crawled_urls))
        self.assertEqual(ev.finding_ids(f"{HST}/"), ())


class TestReportConsistencyAcrossScenarios(unittest.TestCase):
    """Every fixture's final report satisfies the verification contract."""

    _FIXTURES = [
        ("healthy", build_healthy_site, f"{HST}/", dict(max_pages=50, max_depth=3)),
        ("problems", build_problems_site, f"{PRB}/", dict(max_pages=10, max_depth=2)),
        ("products", build_products_site, f"{PRD}/", dict(max_pages=10, max_depth=2)),
        ("freshness", build_freshness_site, f"{FRS}/", dict(max_pages=10, max_depth=2)),
        ("checkout", build_checkout_site, f"{CHK}/", dict(max_pages=10, max_depth=2)),
        ("broken", build_broken_site, f"{BRK}/", dict(max_pages=10, max_depth=2)),
        ("edge", build_edge_site, f"{EDG}/", dict(max_pages=50, max_depth=3)),
        ("hostile", build_hostile_site, f"{HSTL}/", dict(max_pages=12, max_depth=2)),
        ("big", build_big_site, f"{BIG}/", dict(max_pages=35, max_depth=2)),
        ("small", build_small_site, f"{SML}/", dict(max_pages=5, max_depth=2)),
    ]

    def test_each_fixture_passes_full_contract(self) -> None:
        for name, factory, start, kwargs in self._FIXTURES:
            with self.subTest(fixture=name):
                ev = run_site_evaluation(factory(), start, **kwargs)
                problems = validate_site_report(ev)
                self.assertEqual(problems, [], f"{name}: {problems}")

    def test_findings_carry_evidence_and_suggested_action(self) -> None:
        for name, factory, start, kwargs in self._FIXTURES:
            with self.subTest(fixture=name):
                ev = run_site_evaluation(factory(), start, **kwargs)
                for finding in ev.aggregated:
                    self.assertTrue(finding.evidence.strip(), f"{name}: empty evidence on {finding.id}")
                    self.assertTrue(finding.suggested_action.summary.strip())
                    self.assertTrue(finding.source_audit)

    def test_final_reports_are_deterministic(self) -> None:
        for name, factory, start, kwargs in self._FIXTURES:
            with self.subTest(fixture=name):
                first = run_site_evaluation(factory(), start, **kwargs)
                second = run_site_evaluation(factory(), start, **kwargs)
                self.assertEqual(
                    json.dumps(first.final, sort_keys=True),
                    json.dumps(second.final, sort_keys=True),
                )
                self.assertEqual(first.ids_across_site(), second.ids_across_site())
                self.assertEqual(first.result.stats.to_dict(), second.result.stats.to_dict())


class TestOfflineNoExternalDependency(unittest.TestCase):
    """The fixture harness must run with zero network and zero AI APIs."""

    def test_no_network_access_attempted(self) -> None:
        for name, factory, start, kwargs in [
            ("healthy", build_healthy_site, f"{HST}/", dict(max_pages=50, max_depth=3)),
            ("problems", build_problems_site, f"{PRB}/", dict(max_pages=10, max_depth=2)),
            ("products", build_products_site, f"{PRD}/", dict(max_pages=10, max_depth=2)),
        ]:
            with self.subTest(fixture=name):
                with patch("requests.sessions.Session.request", side_effect=AssertionError("network attempted")):
                    ev = run_site_evaluation(factory(), start, **kwargs)
                self.assertEqual(validate_site_report(ev), [])

    def test_read_only_and_single_fetch_per_page(self) -> None:
        site = build_healthy_site()
        run_site_evaluation(site, f"{HST}/", max_pages=50, max_depth=3)
        self.assertTrue(site.fetch_counts)
        for url, count in site.fetch_counts.items():
            parsed = urllib.parse.urlsplit(url)
            self.assertIn(parsed.scheme, ("http", "https"))
            self.assertEqual(count, 1, f"url fetched more than once: {url}")

    def test_no_ai_or_browser_dependencies_loaded(self) -> None:
        for forbidden in ("openai", "anthropic", "google.genai", "playwright", "selenium", "torch"):
            self.assertNotIn(forbidden, sys.modules, f"unexpected external dependency: {forbidden}")


class TestEntrypointOfflinePipeline(unittest.TestCase):
    """The marketplace entrypoint (run_audit_full) via the real orchestrator/report."""

    _HEALTHY_HTML = (
        "<html><head><title>Acme Widgets | Quality Gadgets</title>"
        '<meta name="description" content="Acme makes high quality gadgets for professionals.">'
        f'<link rel="canonical" href="{HST}/">'
        '<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization",'
        f'"name":"Acme Widgets","url":"{HST}/"}}</script>'
        "</head><body><h1>Acme Widgets</h1>"
        "<p>Acme Widgets designs and ships durable gadgets for professionals. Every order includes a "
        "two year warranty, free shipping, and a 30 day returns policy. See our full catalogue below "
        "and read the specifications for each product before you order.</p>"
        '<a href="/products">View products</a><a href="/contact">Contact us</a>'
        "</body></html>"
    )
    _CHECKOUT_HTML = (
        "<html><head><title>Checkout - Step 1 of 3</title></head><body>"
        "<h1>Checkout</h1>"
        "<p>To complete your purchase, please proceed to checkout and confirm your details.</p>"
        '<a href="javascript:void(0)">Buy now</a>'
        '<a href="javascript:void(0)">Checkout</a>'
        '<a href="/">Home</a>'
        "</body></html>"
    )

    @staticmethod
    def _fake_get(html: str):
        def make(url: str, content_type: str, status: int = 200) -> requests.Response:
            resp = requests.Response()
            resp.status_code = status
            resp.url = url
            resp.headers = {"Content-Type": content_type}
            resp.reason = "OK"
            resp._content = html.encode("utf-8")
            return resp

        def fake_get(url: str, **kwargs):  # noqa: ANN003
            if urllib.parse.urlsplit(url).path == "/robots.txt":
                return make(url, "text/plain; charset=utf-8", 404)
            return make(url, "text/html; charset=utf-8")

        return fake_get

    @patch("src.crawler.crawler.requests.get")
    def test_healthy_entrypoint_clean_report(self, mock_get) -> None:
        mock_get.side_effect = self._fake_get(self._HEALTHY_HTML)
        result, report = run_audit_full(f"{HST}/")
        self.assertTrue(result.is_success)
        self.assertEqual(result.audit_errors, {})
        # The six specialist audits are clean on a well-formed page; the only
        # findings come from the deterministic t4_indexability audit (always-on
        # informational low-severity signals in the default pipeline).
        self.assertEqual(sorted(f.id for f in result.findings),
                         ["T4-005", "T4-021", "T4-024", "T4-024", "T4-031", "T4-032"])
        self.assertTrue(all(f.source_audit == "t4_indexability" for f in result.findings))
        self.assertTrue(all(f.severity == "low" for f in result.findings))
        final = report.as_final_dict()
        self.assertEqual(final["summary"], {"total_findings": 6, "high": 0, "medium": 0, "low": 6})
        self.assertEqual(len(final["findings"]), 6)
        self.assertEqual(final["audits_run"], list(_DEFAULT_AUDITS))

    @patch("src.crawler.crawler.requests.get")
    def test_checkout_entrypoint_findings_and_summary(self, mock_get) -> None:
        mock_get.side_effect = self._fake_get(self._CHECKOUT_HTML)
        result, report = run_audit_full(f"{CHK}/")
        self.assertTrue(result.is_success)
        self.assertEqual(result.audit_errors, {})
        ids = [f.id for f in result.findings]
        self.assertEqual(sorted(ids), ["ENG-004", "T4-005", "T4-020", "T4-030", "TR-INT-001", "TR-PROC-001"])
        self.assertEqual(sum(1 for f in result.findings if f.severity == "critical"), 1)
        t4_ids = [f.id for f in result.findings if f.source_audit == "t4_indexability"]
        self.assertEqual(sorted(t4_ids), ["T4-005", "T4-020", "T4-030"])

        final = report.as_final_dict()
        self.assertEqual(final["summary"], {"total_findings": 6, "high": 0, "medium": 2, "low": 3, "critical": 1})
        self.assertEqual(len(final["findings"]), final["summary"]["total_findings"])
        for finding in final["findings"]:
            self.assertTrue(finding["evidence"])
            self.assertTrue(finding["suggested_action"]["summary"])

    @patch("src.crawler.crawler.requests.get")
    def test_entrypoint_report_json_is_deterministic(self, mock_get) -> None:
        mock_get.side_effect = self._fake_get(self._CHECKOUT_HTML)
        _, first_report = run_audit_full(f"{CHK}/")
        _, second_report = run_audit_full(f"{CHK}/")
        self.assertEqual(first_report.as_final_dict(), second_report.as_final_dict())

        parsed = json.loads(first_report.to_json())
        self.assertEqual(parsed, first_report.as_final_dict())
        self.assertEqual(json.dumps(first_report.to_json(), sort_keys=True),
                         json.dumps(second_report.to_json(), sort_keys=True))


if __name__ == "__main__":
    unittest.main()