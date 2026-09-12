"""Sitemap-aware URL discovery for the bounded crawl foundation (P1-T2).

Adds robots.txt ``Sitemap:`` parsing plus sitemap index/urlset parsing on top of
the existing single-fetch HTTP layer. Discovery is read-only, deterministic,
bounded, and purely additive:

- It hangs off the same ``FetcherCallable``/``FetchedPage`` seam as
  ``crawl_site``; it never re-implements HTTP itself.
- robots.txt is fetched first; ``Sitemap:`` records (RFC 9309) nominate sitemap
  files. If robots.txt is unavailable or declares no sitemaps, the conventional
  ``/sitemap.xml`` location is probed once ("where appropriate").
- Sitemap files are parsed with stdlib XML only (no new dependencies). A
  ``<sitemapindex>`` may nest further sitemap files, bounded by
  ``MAX_SITEMAP_FILES`` and ``MAX_SITEMAP_DEPTH``; cycles terminate via an
  examined-set.
- Extracted URLs are normalized, deduplicated, restricted to the crawl site
  scope, and returned in deterministic document order.

Only same-site http(s) URLs are surfaced; external URLs, fragments, unsupported
schemes, and malformed files are simply ignored (malformed files are counted in
``errors``). Nothing here touches ``observe_page`` or the observation layer.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.crawler.crawler import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    FetchedPage,
    fetch_text,
)
from src.crawler.url_utils import (
    is_crawlable_scheme,
    is_rejected_scheme,
    is_same_site,
    resolve_and_normalize,
)

MAX_SITEMAP_FILES: int = 5
MAX_SITEMAP_DEPTH: int = 3
DEFAULT_MAX_SITEMAP_URLS: int = 1000
MAX_ERROR_SAMPLES: int = 10

SitemapFetcher = Callable[[str, float, str, bool], FetchedPage]


def default_text_fetcher(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
    """Default metadata fetcher: the project's single read-only text/XML fetch."""
    return fetch_text(url, timeout=timeout, user_agent=user_agent, check_robots=False)


@dataclass(frozen=True)
class SitemapDiscoveryResult:
    """Deterministic outcome of sitemap discovery for one site scope.

    Attributes:
        discovered_urls: Normalized, deduplicated, same-site http(s) URLs, in
            deterministic discovery order (robots-declared sitemaps first, the
            conventional sitemap last; document order within each file).
        sitemap_files_parsed: Sitemap/index files successfully parsed, in fetch
            order.
        robots_sitemap_urls: Sitemap file URLs declared by robots.txt (after
            normalization and site-scope filtering), in record order.
        conventional_checked: True when the conventional ``/sitemap.xml``
            location was probed.
        robots_fetched: True when robots.txt itself was successfully fetched.
        errors: Stable error categories (counts) encountered during discovery.
        error_samples: First few human-readable error details.
    """

    discovered_urls: list[str]
    sitemap_files_parsed: list[str]
    robots_sitemap_urls: list[str]
    conventional_checked: bool
    robots_fetched: bool
    errors: dict[str, int] = field(default_factory=dict)
    error_samples: list[str] = field(default_factory=list)


def _local_name(tag: object) -> str:
    """Return an XML tag's local name, stripping any namespace prefix."""
    if isinstance(tag, str) and tag.startswith("{"):
        return tag.split("}", 1)[1]
    return str(tag) if tag is not None else ""


def _site_origin(scope_url: str) -> tuple[str, str]:
    """Return (scheme, netloc) of a normalized http(s) URL."""
    parts = urllib.parse.urlsplit(scope_url)
    return (parts.scheme or "").lower(), parts.netloc


def _yield_loc(root: ET.Element, *, parent_local: str):
    """Yield ``<loc>`` text found as a direct child of ``<parent_local>`` elements."""
    for element in root.iter():
        if _local_name(element.tag) != parent_local:
            continue
        for child in element:
            if _local_name(child.tag) == "loc":
                text = (child.text or "").strip()
                if text:
                    yield text


def parse_robots_sitemap_records(text: str) -> list[str]:
    """Extract the URL values of ``Sitemap:`` directives from robots.txt text.

    Line-oriented parsing consistent with RFC 9309: directive keys are
    case-insensitive, full-line comments are skipped, an optional trailing
    ``#`` comment (separated by whitespace) is dropped, and surrounding angle
    brackets are tolerated as some sites emit ``Sitemap: <url>``.
    """
    urls: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or key.strip().lower() != "sitemap":
            continue
        target = value.strip()
        target = target.split(" #", 1)[0].strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if target:
            urls.append(target)
    return urls


def _resolve_loc(base_url: str, loc: str, scope_url: str) -> Optional[str]:
    """Resolve/normalize a sitemap <loc> and filter to the crawl scope.

    Returns None for URLs that are not crawlable http(s) or that leave the
    site scope (including external hosts and subdomains).
    """
    candidate = resolve_and_normalize(base_url, loc)
    if not candidate or not is_crawlable_scheme(candidate) or is_rejected_scheme(candidate):
        return None
    if not is_same_site(candidate, scope_url):
        return None
    return candidate


def discover_sitemap_urls(
    scope_url: str,
    *,
    fetcher: SitemapFetcher = default_text_fetcher,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_urls: int = DEFAULT_MAX_SITEMAP_URLS,
    max_sitemap_files: int = MAX_SITEMAP_FILES,
    max_sitemap_depth: int = MAX_SITEMAP_DEPTH,
) -> SitemapDiscoveryResult:
    """Discover same-site URLs reachable via robots.txt Sitemap records or the
    conventional ``/sitemap.xml`` location.

    Always terminates: sitemap-file fetching is bounded by
    ``max_sitemap_files`` and ``max_sitemap_depth``, already-examined files are
    never re-fetched, extracted URLs are capped at ``max_urls``, and every
    network operation is a single read-only fetch through ``fetcher``.

    Args:
        scope_url: Normalized http(s) start URL defining the site scope.
        fetcher: Overridable fetch primitive (defaults to fetch_text). Test seam
            only — never re-implements HTTP here.
        timeout: Per-request timeout seconds passed to fetcher.
        user_agent: User-Agent header passed to fetcher.
        max_urls: Maximum number of distinct discovered URLs.
        max_sitemap_files: Maximum number of sitemap/index files parsed.
        max_sitemap_depth: Maximum sitemap-index nesting depth (0 = no nesting).

    Returns:
        SitemapDiscoveryResult with deterministic discovered_urls.
    """
    rediscovered: list[str] = []
    seen_urls: set[str] = set()
    parsed_files: list[str] = []
    examined: set[str] = set()
    errors: dict[str, int] = {}
    error_samples: list[str] = []
    url_budget = max(0, max_urls)
    file_budget = max(1, max_sitemap_files)

    def _record_error(category: str, message: str) -> None:
        errors[category] = errors.get(category, 0) + 1
        if len(error_samples) < MAX_ERROR_SAMPLES:
            error_samples.append(message)

    def _collect(sitemap_url: str, depth: int) -> None:
        if sitemap_url in examined:
            return  # cycle or duplicate; never re-fetch
        examined.add(sitemap_url)
        if depth > max_sitemap_depth or len(parsed_files) >= file_budget:
            return

        fetched = fetcher(sitemap_url, timeout, user_agent, False)
        error_message = fetched.error_message or "fetch failed"
        if not fetched.is_success or not fetched.html_content:
            _record_error("sitemap_fetch_failed", f"{sitemap_url}: {error_message}")
            return

        try:
            root = ET.fromstring(fetched.html_content)
        except Exception as err:
            _record_error("malformed_sitemap", f"{sitemap_url}: {err}")
            return

        parsed_files.append(sitemap_url)
        root_tag = _local_name(root.tag)
        if root_tag == "sitemapindex":
            for loc in _yield_loc(root, parent_local="sitemap"):
                child = _resolve_loc(sitemap_url, loc, scope_url)
                if child is not None:
                    _collect(child, depth + 1)
        elif root_tag == "urlset":
            for loc in _yield_loc(root, parent_local="url"):
                candidate = _resolve_loc(sitemap_url, loc, scope_url)
                if candidate is None or candidate in seen_urls:
                    continue
                if len(seen_urls) >= url_budget:
                    break  # extraction budget exhausted; stop cleanly
                seen_urls.add(candidate)
                rediscovered.append(candidate)
        else:
            _record_error(
                "unrecognized_sitemap",
                f"{sitemap_url}: root element <{root_tag}> is not urlset/sitemapindex",
            )

    scheme, netloc = _site_origin(scope_url)
    if not scheme or not netloc:
        return SitemapDiscoveryResult(
            discovered_urls=[],
            sitemap_files_parsed=[],
            robots_sitemap_urls=[],
            conventional_checked=False,
            robots_fetched=False,
            errors={"invalid_scope": 1},
            error_samples=[f"invalid site scope URL: {scope_url}"],
        )

    robots_url = f"{scheme}://{netloc}/robots.txt"
    robots_sitemap_urls: list[str] = []
    robots_fetched = False

    robots_resp = fetcher(robots_url, timeout, user_agent, False)
    if robots_resp.is_success and robots_resp.html_content:
        robots_fetched = True
        for record in parse_robots_sitemap_records(robots_resp.html_content):
            candidate = _resolve_loc(robots_url, record, scope_url)
            if candidate is not None and candidate not in robots_sitemap_urls:
                robots_sitemap_urls.append(candidate)
    elif not robots_resp.is_success:
        _record_error("robots_fetch_failed", f"{robots_url}: {robots_resp.error_message or 'fetch failed'}")

    candidates: list[str] = list(robots_sitemap_urls)
    conventional_checked = False
    if not candidates:
        conventional_checked = True
        candidates.append(f"{scheme}://{netloc}/sitemap.xml")

    for sitemap_url in candidates:
        _collect(sitemap_url, 0)

    return SitemapDiscoveryResult(
        discovered_urls=rediscovered,
        sitemap_files_parsed=parsed_files,
        robots_sitemap_urls=robots_sitemap_urls,
        conventional_checked=conventional_checked,
        robots_fetched=robots_fetched,
        errors=dict(sorted(errors.items())),
        error_samples=error_samples,
    )