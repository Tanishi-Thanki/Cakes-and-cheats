"""Bounded, deterministic site crawl foundation (P1-T1).

Provides ``crawl_site()``: a breadth-first crawl of a target site that is always
bounded (page limit + depth limit + per-request timeout) and produces one
``PageObservation`` per crawled page plus deterministic ``CrawlStats``.

The existing single-page primitives remain the only network touchpoint:
- ``fetch_page`` stays the single fetch primitive (read-only, robots-aware,
  structured errors) — the crawl never re-implements HTTP.
- ``observe_page`` stays the single HTML-parse primitive — the crawl consumes
  ``PageObservation.links`` for discovery.
- ``fetch_text`` (via sitemap discovery) is the single text/XML metadata fetch
  primitive; outbound discovery requests use the same ``FetcherCallable`` seam.

Sitemap discovery (P1-T2) is enabled by default and is purely additive: robots.txt
``Sitemap:`` records and the conventional ``/sitemap.xml`` seed the BFS queue at
depth 1, always within the existing ``max_pages``/``max_depth`` budget authority.

Downstream compatibility: this module is additive. It does not change
``FetchedPage``, ``PageObservation``, ``fetch_page``, ``observe_page``, or the
orchestrator. It builds no evidence graph and performs no audit logic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.crawler.crawler import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_AGENT,
    FetchedPage,
    fetch_page,
)
from src.crawler.robots_sitemaps import (
    SitemapDiscoveryResult,
    default_text_fetcher,
    discover_sitemap_urls,
)
from src.crawler.url_utils import (
    extract_site_key,
    is_crawlable_scheme,
    is_rejected_scheme,
    is_same_site,
    normalize_url,
    resolve_and_normalize,
)
from src.observation.observation import PageObservation, observe_page

MAX_ERROR_SAMPLES: int = 10

# Discovery-source labels assigned to every crawled URL (P1-T5). Purely
# additive bookkeeping: they describe how a URL was first reached (or, for
# unvisited URLs, how it would have been), and never alter crawl-depth
# semantics — a sitemap-seeded page is still crawled at literal BFS depth 1.
DISCOVERY_SOURCE_START: str = "start"
DISCOVERY_SOURCE_LINK: str = "link"
DISCOVERY_SOURCE_SITEMAP: str = "sitemap"

FetcherCallable = Callable[[str, float, str, bool], FetchedPage]
ObserverCallable = Callable[[FetchedPage], PageObservation]


def _default_fetcher(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
    """Default fetcher: the project's single-page, read-only crawler."""
    return fetch_page(url, timeout=timeout, user_agent=user_agent, check_robots=check_robots)


def _classify_error(message: Optional[str]) -> str:
    """Map a structured fetch error message to a stable failure category."""
    text = message or ""
    for token, category in (
        ("robots.txt", "blocked_by_robots"),
        ("Connection error", "connection_error"),
        ("timed out", "timeout"),
        ("SSL", "ssl_error"),
        ("HTTP error status", "http_error"),
        ("Invalid URL", "invalid_url"),
        ("non-HTML", "non_html"),
        ("Failed to decode", "decode_error"),
    ):
        if token in text:
            return category
    return "other"


@dataclass(frozen=True)
class CrawlConfig:
    """Bounded configuration for a site crawl.

    Attributes:
        start_url: The URL (http/https) where the crawl begins.
        max_pages: Hard cap on the number of distinct URLs fetched. The crawler
            also never keeps more than ``max_pages`` distinct discovered URLs,
            so memory and time stay bounded even on thousands of links.
        max_depth: Maximum link hops from start_url. Pages beyond this depth are
            never discovered (depth 0 is the start page).
        timeout_seconds: Per-request read/connect timeout passed to fetch_page().
        user_agent: User-Agent header passed to fetch_page().
        check_robots: Passed to fetch_page(); requests robots.txt first when True.
        discover_sitemaps: When True (default), robots.txt Sitemap: records and
            the conventional /sitemap.xml seed the crawl queue at depth 1,
            subject to the same max_pages/max_depth budget.
    """

    start_url: str
    max_pages: int = 50
    max_depth: int = 3
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    check_robots: bool = True
    discover_sitemaps: bool = True


@dataclass(frozen=True)
class CrawlPageRecord:
    """Record for one page attempted during the crawl.

    ``observation`` is populated (and the crawl finds no links from it) only
    when the page fetched successfully; on failure it is None and the error is
    preserved in ``error_message``.

    ``discovery_sources`` (P1-T5) records how the URL was reached: a subset of
    ``start`` / ``link`` / ``sitemap``. A URL may legitimately carry more than
    one source (for example a sitemap seed that other pages also link to).
    """

    url: str  # normalized URL
    depth: int
    status_code: Optional[int]
    is_success: bool
    error_message: Optional[str]
    observation: Optional[PageObservation] = field(repr=False, compare=False)
    discovery_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class UnvisitedUrlRecord:
    """A same-site http(s) URL the crawl discovered but never attempted to fetch.

    Deliberately distinct from ``CrawlPageRecord`` failures: these URLs were
    never fetched at all. ``reason`` is a stable deterministic category:

    - ``"max_pages"``: found on a discovered page, but the crawl budget
      (``max_pages``) was exhausted before the URL could be admitted to the
      queue (or the queue could not be drained).
    - ``"max_depth"``: found only on a page at the depth boundary, so the URL
      was never eligible to be fetched under the depth limit.

    The collection is always capped at ``max_pages`` records so it can never
    defeat the crawl budget or grow without bound.

    Attributes:
        url: Normalized same-site http(s) URL.
        reason: Stable reason the URL was not visited.
        depth: The crawl depth at which the URL would have been fetched
            (nominal discovery depth; never a literal link depth).
        sources: Discovery sources observed (subset of ``link`` / ``sitemap``).
        discovered_from: Page URL whose links revealed this URL, when known;
            None for URLs first seen through sitemap admission.
    """

    url: str
    reason: str
    depth: int
    sources: tuple[str, ...] = ()
    discovered_from: Optional[str] = None


@dataclass(frozen=True)
class CrawlStats:
    """Deterministic statistics for one crawl run."""

    start_url: str
    normalized_start_url: Optional[str]
    max_pages: int
    max_depth: int
    pages_attempted: int = 0  # distinct URLs fetched (success or failure)
    pages_fetched: int = 0  # distinct URLs successfully crawled
    pages_failed: int = 0  # pages_attempted - pages_fetched
    urls_discovered: int = 0  # distinct eligible internal URLs seen (incl. start)
    urls_crawled: int = 0  # aliases pages_fetched for reporting clarity
    max_depth_reached: int = 0
    errors: dict[str, int] = field(default_factory=dict)  # category -> count
    error_samples: list[str] = field(default_factory=list)  # first N failures
    crawled_urls: list[str] = field(default_factory=list)  # crawl (BFS) order
    sitemap_files_parsed: int = 0  # sitemap/index files parsed during discovery
    sitemap_urls_discovered: int = 0  # distinct URLs seeded from sitemaps

    def to_dict(self) -> dict[str, object]:
        """Plain deterministic dict for downstream statistics consumption."""
        return {
            "start_url": self.start_url,
            "normalized_start_url": self.normalized_start_url,
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "pages_attempted": self.pages_attempted,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "urls_discovered": self.urls_discovered,
            "urls_crawled": self.urls_crawled,
            "max_depth_reached": self.max_depth_reached,
            "errors": dict(self.errors),
            "error_samples": list(self.error_samples),
            "crawled_urls": list(self.crawled_urls),
            "sitemap_files_parsed": self.sitemap_files_parsed,
            "sitemap_urls_discovered": self.sitemap_urls_discovered,
        }


@dataclass(frozen=True)
class CrawlResult:
    """Outcome of a bounded site crawl.

    ``is_success`` is True when the start URL itself was fetched. A crawl that
    ran but had individual page failures still has ``is_success`` True; per-page
    failures are always recorded in ``stats`` and ``pages``.

    P1-T5 additive fields:
    - ``unvisited_urls``: bounded records of same-site URLs discovered but never
      fetched (with the deterministic reason they were skipped).
    - ``sitemap_discovery``: the bounded sitemap discovery result used to seed
      the crawl, when discovery ran and produced one.
    """

    stats: CrawlStats
    pages: list[CrawlPageRecord]  # deterministic BFS order
    pages_by_url: dict[str, CrawlPageRecord] = field(repr=False, compare=False)
    is_success: bool
    error_message: Optional[str] = None
    unvisited_urls: list[UnvisitedUrlRecord] = field(default_factory=list)
    sitemap_discovery: Optional[SitemapDiscoveryResult] = None


def _invalid_start(start_url: str, reason: str) -> CrawlResult:
    """Build a CrawlResult when the start URL itself is unusable."""
    stats = CrawlStats(
        start_url=start_url,
        normalized_start_url=None,
        max_pages=0,
        max_depth=0,
        errors={"invalid_url": 1},
        error_samples=[f"{start_url}: {reason}"],
    )
    return CrawlResult(
        stats=stats,
        pages=[],
        pages_by_url={},
        is_success=False,
        error_message=reason,
    )


def crawl_site(
    config: CrawlConfig,
    *,
    fetcher: FetcherCallable = _default_fetcher,
    observer: ObserverCallable = observe_page,
    text_fetcher: Optional[FetcherCallable] = None,
) -> CrawlResult:
    """Crawl the site rooted at config.start_url within bounded limits.

    Traversal is a deterministic breadth-first search over same-site, http(s)
    URLs, seeded at depth 1 by sitemap discovery when enabled. Duplicate,
    cyclic, self-referencing, external, and unsupported-scheme links are all
    safely ignored; a single bad page is recorded and never crashes the crawl;
    the crawl always terminates.

    Args:
        config: Bounded crawl configuration.
        fetcher: Overridable fetch primitive (defaults to the project's
            fetch_page). Test seam only — never re-implement HTTP here.
        observer: Overridable observation primitive (defaults to the project's
            observe_page). Consumed for its ``links`` discovery targets.
        text_fetcher: Overridable fetch primitive for sitemap/robots metadata
            (defaults to fetch_text). When a custom ``fetcher`` is supplied but
            ``text_fetcher`` is None, the custom ``fetcher`` is also used for
            metadata so offline fakes can serve both pages and sitemaps.

    Returns:
        CrawlResult with deterministic stats and per-page records.
    """
    start_url = config.start_url.strip() if isinstance(config.start_url, str) else ""

    if not start_url:
        return _invalid_start(config.start_url, "start URL is empty")

    if is_rejected_scheme(start_url):
        return _invalid_start(config.start_url, "start URL uses an unsupported scheme (must be http/https)")

    if not is_crawlable_scheme(start_url):
        return _invalid_start(config.start_url, "start URL must be an absolute http/https URL")

    start_norm = normalize_url(start_url)
    if not start_norm or not extract_site_key(start_norm):
        return _invalid_start(config.start_url, "start URL could not be normalized")

    max_pages = max(1, config.max_pages)
    max_depth = max(0, config.max_depth)
    scope_url = start_norm

    # Sitemap discovery (P1-T2): seed the BFS queue at depth 1, always within
    # max_pages/max_depth budget authority. Depth-0 crawls never seed sitemaps.
    sitemap_files_parsed = 0
    sitemap_urls_discovered = 0
    discovery: Optional[SitemapDiscoveryResult] = None
    if config.discover_sitemaps and max_depth >= 1:
        discovery_fetcher: FetcherCallable
        if text_fetcher is not None:
            discovery_fetcher = text_fetcher
        elif fetcher is not _default_fetcher:
            discovery_fetcher = fetcher
        else:
            discovery_fetcher = default_text_fetcher
        discovery = discover_sitemap_urls(
            scope_url,
            fetcher=discovery_fetcher,
            timeout=config.timeout_seconds,
            user_agent=config.user_agent,
            max_urls=max_pages,
        )
        sitemap_files_parsed = len(discovery.sitemap_files_parsed)

    discovered: set[str] = {start_norm}
    sources_by_url: dict[str, set[str]] = {start_norm: {DISCOVERY_SOURCE_START}}
    queue: deque[tuple[str, int]] = deque([(start_norm, 0)])
    if discovery is not None:
        for seed in discovery.discovered_urls:
            if len(discovered) >= max_pages:
                break  # discovery budget exhausted; stop cleanly
            sources_by_url.setdefault(seed, set()).add(DISCOVERY_SOURCE_SITEMAP)
            if seed in discovered:
                continue
            discovered.add(seed)
            queue.append((seed, 1))
            sitemap_urls_discovered += 1

    visited_order: list[str] = []
    visited: set[str] = set()
    pages: list[CrawlPageRecord] = []
    pages_by_url: dict[str, CrawlPageRecord] = {}
    error_counts: dict[str, int] = {}
    error_samples: list[str] = []
    pages_attempted = 0
    pages_fetched = 0
    max_depth_reached = 0

    is_success = False
    start_error: Optional[str] = None

    # P1-T5: bounded record of same-site URLs discovered but never fetched,
    # capped at max_pages so it can never defeat the crawl budget.
    unvisited: list[UnvisitedUrlRecord] = []
    unvisited_seen: set[str] = set()

    def _record_unvisited(
        url: str,
        reason: str,
        depth: int,
        discovered_from: Optional[str] = None,
    ) -> None:
        if url in unvisited_seen or len(unvisited) >= max_pages:
            return
        unvisited_seen.add(url)
        sources = set(sources_by_url.get(url, ()))
        if reason in ("max_pages", "max_depth"):
            # Both categories were found as link targets on a crawled page.
            sources.add(DISCOVERY_SOURCE_LINK)
        unvisited.append(
            UnvisitedUrlRecord(
                url=url,
                reason=reason,
                depth=depth,
                sources=tuple(sorted(sources)),
                discovered_from=discovered_from,
            )
        )

    while queue and len(visited) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue

        visited.add(url)
        visited_order.append(url)
        pages_attempted = len(visited_order)
        max_depth_reached = max(max_depth_reached, depth)

        fetched = fetcher(url, config.timeout_seconds, config.user_agent, config.check_robots)

        if not fetched.is_success:
            error_message = fetched.error_message or "Page fetch was unsuccessful."
            pages.append(
                CrawlPageRecord(
                    url=url,
                    depth=depth,
                    status_code=fetched.status_code,
                    is_success=False,
                    error_message=error_message,
                    observation=None,
                    discovery_sources=tuple(sorted(sources_by_url.get(url, ()))),
                )
            )
            category = _classify_error(error_message)
            error_counts[category] = error_counts.get(category, 0) + 1
            if len(error_samples) < MAX_ERROR_SAMPLES:
                error_samples.append(f"{url}: {error_message}")
            if url == start_norm and not is_success:
                start_error = error_message
            continue

        pages_fetched += 1
        if url == start_norm:
            is_success = True

        obs = observer(fetched)

        if obs.is_success and obs.links and depth + 1 <= max_depth:
            budget_exhausted = False
            for link in obs.links:
                if budget_exhausted and len(unvisited) >= max_pages:
                    break  # only capped bookkeeping remains; stay bounded
                if not link.href:
                    continue
                if not budget_exhausted and len(discovered) >= max_pages:
                    budget_exhausted = True  # stop admitting; record the rest
                candidate = resolve_and_normalize(url, link.href)
                if (not candidate or not is_crawlable_scheme(candidate)
                        or is_rejected_scheme(candidate)):
                    continue
                if not is_same_site(candidate, scope_url):
                    continue
                if candidate in visited or candidate in discovered:
                    if candidate in discovered and not budget_exhausted:
                        # Track overlap sources without admitting the URL again.
                        sources_by_url[candidate].add(DISCOVERY_SOURCE_LINK)
                    continue
                if budget_exhausted:
                    _record_unvisited(candidate, "max_pages", depth + 1, url)
                    continue
                discovered.add(candidate)
                sources_by_url[candidate] = {DISCOVERY_SOURCE_LINK}
                queue.append((candidate, depth + 1))
        elif obs.is_success and obs.links:
            # Depth-boundary page: link targets are discovered but never
            # eligible to be fetched. Record a bounded sample of the internal
            # ones (reason "max_depth") without touching the crawl budget.
            for link in obs.links:
                if len(unvisited) >= max_pages:
                    break
                if not link.href:
                    continue
                candidate = resolve_and_normalize(url, link.href)
                if (not candidate or not is_crawlable_scheme(candidate)
                        or is_rejected_scheme(candidate)):
                    continue
                if not is_same_site(candidate, scope_url):
                    continue
                if candidate in visited or candidate in discovered:
                    continue
                _record_unvisited(candidate, "max_depth", depth + 1, url)

        pages.append(
            CrawlPageRecord(
                url=url,
                depth=depth,
                status_code=obs.status_code,
                is_success=obs.is_success,
                error_message=obs.error_message,
                observation=obs if obs.is_success else None,
                discovery_sources=tuple(sorted(sources_by_url.get(url, ()))),
            )
        )

    stats = CrawlStats(
        start_url=config.start_url,
        normalized_start_url=start_norm,
        max_pages=max_pages,
        max_depth=max_depth,
        pages_attempted=len(visited_order),
        pages_fetched=pages_fetched,
        pages_failed=len(visited_order) - pages_fetched,
        urls_discovered=len(discovered),
        urls_crawled=pages_fetched,
        max_depth_reached=max_depth_reached,
        errors=dict(sorted(error_counts.items())),
        error_samples=error_samples,
        crawled_urls=visited_order,
        sitemap_files_parsed=sitemap_files_parsed,
        sitemap_urls_discovered=sitemap_urls_discovered,
    )
    for record in pages:
        pages_by_url[record.url] = record

    return CrawlResult(
        stats=stats,
        pages=pages,
        pages_by_url=pages_by_url,
        is_success=is_success,
        error_message=start_error,
        unvisited_urls=unvisited,
        sitemap_discovery=discovery,
    )