"""P1-T5: deterministic site-level crawl context built from a bounded crawl.

Consumes an existing ``CrawlResult`` (the bounded ``crawl_site`` output) and
composes it into a single structural ``SiteAuditContext`` that downstream
modules (P2 evidence/consistency, P3 orchestrator, final audit integration) can
consume without re-fetching or re-parsing anything:

- per-URL discovery sources (``start`` / ``link`` / ``sitemap``);
- a normalized directed internal-link graph (``SiteInternalGraph``) with
  outgoing and incoming relationships derived from successful page
  observations;
- external-link relationships (structural; counts are reported, URLs are
  retained per page);
- a bounded record of same-site URLs discovered but never fetched
  (``UnvisitedUrlRecord``), together with the deterministic reason they were
  skipped;
- deterministic representative-page selection that is homepage-first,
  connectivity-aware, and path/depth-diverse — no ML, no LLM, no embeddings,
  no random selection;
- compact site-level statistics for downstream audits.

Scope boundary: this module is *structural site context only*. It never
fetches, never re-parses HTML, and never crawls; the crawl budget and
discovery semantics live in ``crawl_site`` and are preserved verbatim.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, Union

from src.crawler.crawl_session import (
    DISCOVERY_SOURCE_LINK,
    DISCOVERY_SOURCE_SITEMAP,
    DISCOVERY_SOURCE_START,
    CrawlPageRecord,
    CrawlResult,
    CrawlStats,
    UnvisitedUrlRecord,
)
from src.crawler.url_utils import (
    is_crawlable_scheme,
    is_rejected_scheme,
    is_same_site,
    normalize_url,
)
from src.observation.observation import PageObservation

DEFAULT_MAX_REPRESENTATIVE_PAGES: int = 10

_PageSequence = Union[list[CrawlPageRecord], tuple[CrawlPageRecord, ...]]


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteInternalGraph:
    """Normalized directed internal-link graph.

    Nodes are every normalized internal URL that participates in the graph:
    the source of any edge (a successfully observed page) or the target of any
    edge (including targets that failed to fetch or were never fetched). One
    edge ``(source, target)`` exists per distinct normalized internal link
    discovered on a successfully observed page; fragment-only duplicates and
    repeated links collapse to a single deterministic edge.

    Attributes:
        nodes: Sorted normalized internal URLs referenced by the graph.
        edges: Unique normalized ``(source, target)`` pairs, sorted.
        outgoing: URL -> sorted tuple of distinct internal targets.
        incoming: URL -> sorted tuple of distinct internal sources.
    """

    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    outgoing: dict[str, tuple[str, ...]]
    incoming: dict[str, tuple[str, ...]]

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


def _internal_targets(page: CrawlPageRecord, scope_url: str):
    """Yield normalized, distinct internal http(s) targets observed on a page."""
    obs = page.observation
    if obs is None:
        return
    seen: set[str] = set()
    for link in obs.links:
        if not link.href:
            continue
        candidate = normalize_url(link.href)
        if (not candidate or not is_crawlable_scheme(candidate)
                or is_rejected_scheme(candidate)):
            continue
        if not scope_url or not is_same_site(candidate, scope_url):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def _external_targets(page: CrawlPageRecord, scope_url: str):
    """Yield normalized, distinct external http(s) targets observed on a page."""
    obs = page.observation
    if obs is None:
        return
    seen: set[str] = set()
    for link in obs.links:
        if not link.href:
            continue
        candidate = normalize_url(link.href)
        if (not candidate or not is_crawlable_scheme(candidate)
                or is_rejected_scheme(candidate)):
            continue
        if is_same_site(candidate, scope_url):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def build_internal_graph(
    pages: _PageSequence,
    scope_url: str,
) -> SiteInternalGraph:
    """Build the normalized directed internal-link graph for observed pages.

    ``pages`` should be the successfully observed ``CrawlPageRecord`` list;
    only records with a non-None observation contribute edges. This function
    never fetches and never parses HTML — it reads the already-observed link
    data. Edges are globally deduplicated and normalized.
    """
    edge_set: set[tuple[str, str]] = set()
    outgoing_map: dict[str, set[str]] = {}
    node_set: set[str] = set()

    for page in pages:
        if page.observation is None:
            continue
        source = page.url
        node_set.add(source)
        targets = outgoing_map.setdefault(source, set())
        for target in _internal_targets(page, scope_url):
            targets.add(target)
            edge_set.add((source, target))
            node_set.add(target)

    nodes = tuple(sorted(node_set))
    edges = tuple(sorted(edge_set))

    outgoing_final: dict[str, tuple[str, ...]] = {url: () for url in nodes}
    for url, targets in outgoing_map.items():
        outgoing_final[url] = tuple(sorted(targets))

    incoming_map: dict[str, set[str]] = {}
    for source, target in edge_set:
        incoming_map.setdefault(target, set()).add(source)
    incoming_final: dict[str, tuple[str, ...]] = {url: () for url in nodes}
    for url, sources in incoming_map.items():
        incoming_final[url] = tuple(sorted(sources))

    return SiteInternalGraph(
        nodes=nodes,
        edges=edges,
        outgoing=outgoing_final,
        incoming=incoming_final,
    )


# ---------------------------------------------------------------------------
# Site statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteStats:
    """Compact deterministic statistics about one site crawl context.

    These are internal context/statistics for downstream modules — not a final
    audit report redesign.
    """

    total_discovered: int
    successful_pages: int
    failed_pages: int
    internal_link_edges: int
    external_link_urls: int
    graph_nodes: int
    graph_edges: int
    pages_by_depth: dict[int, int] = field(default_factory=dict)
    pages_by_source: dict[str, int] = field(default_factory=dict)
    unvisited_pages: int = 0
    representative_pages: int = 0
    sitemap_files_parsed: int = 0
    sitemap_urls_discovered: int = 0

    def to_dict(self) -> dict[str, object]:
        """Plain deterministic dict for downstream statistics consumption."""
        return {
            "total_discovered": self.total_discovered,
            "successful_pages": self.successful_pages,
            "failed_pages": self.failed_pages,
            "internal_link_edges": self.internal_link_edges,
            "external_link_urls": self.external_link_urls,
            "graph_nodes": self.graph_nodes,
            "graph_edges": self.graph_edges,
            "pages_by_depth": dict(sorted(self.pages_by_depth.items())),
            "pages_by_source": dict(sorted(self.pages_by_source.items())),
            "unvisited_pages": self.unvisited_pages,
            "representative_pages": self.representative_pages,
            "sitemap_files_parsed": self.sitemap_files_parsed,
            "sitemap_urls_discovered": self.sitemap_urls_discovered,
        }


def _build_site_stats(
    result: CrawlResult,
    *,
    pages: tuple[CrawlPageRecord, ...],
    discovery_sources: dict[str, tuple[str, ...]],
    external_links: dict[str, tuple[str, ...]],
    graph: SiteInternalGraph,
    unvisited: tuple[UnvisitedUrlRecord, ...],
    representative_count: int,
) -> SiteStats:
    pages_by_depth: dict[int, int] = {}
    for page in pages:
        pages_by_depth[page.depth] = pages_by_depth.get(page.depth, 0) + 1

    pages_by_source: dict[str, int] = {}
    for sources in discovery_sources.values():
        for source in sources:
            pages_by_source[source] = pages_by_source.get(source, 0) + 1

    external_urls: set[str] = set()
    for targets in external_links.values():
        external_urls.update(targets)

    return SiteStats(
        total_discovered=result.stats.urls_discovered,
        successful_pages=sum(1 for p in pages if p.is_success),
        failed_pages=sum(1 for p in pages if not p.is_success),
        internal_link_edges=graph.edge_count,
        external_link_urls=len(external_urls),
        graph_nodes=graph.node_count,
        graph_edges=graph.edge_count,
        pages_by_depth=dict(sorted(pages_by_depth.items())),
        pages_by_source=dict(sorted(pages_by_source.items())),
        unvisited_pages=len(unvisited),
        representative_pages=representative_count,
        sitemap_files_parsed=result.stats.sitemap_files_parsed,
        sitemap_urls_discovered=result.stats.sitemap_urls_discovered,
    )


# ---------------------------------------------------------------------------
# Representative page selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepresentativePage:
    """Metadata for one deterministically selected representative page.

    Attributes:
        url: Normalized URL.
        rank: 1-based position in selection priority order.
        incoming_links: Distinct internal sources linking to the page.
        outgoing_links: Distinct internal targets found on the page.
        depth: Literal crawl depth recorded by the bounded crawl.
        reason: Transparent selection rationale (phase that chose the page).
    """

    url: str
    rank: int
    incoming_links: int
    outgoing_links: int
    depth: int
    reason: str


def _path_area(url: str) -> str:
    """First non-empty path segment of a normalized URL (lowercased).

    Structural grouping only: no business meaning is ever inferred from the
    text. The root path maps to an empty string.
    """
    try:
        path = urllib.parse.urlsplit(url).path.strip("/")
    except Exception:
        return ""
    if not path:
        return ""
    return path.split("/", 1)[0].lower()


def select_representative_pages(
    successful_pages: _PageSequence,
    graph: SiteInternalGraph,
    discovery_sources: dict[str, tuple[str, ...]],
    normalized_start_url: str,
    max_representative_pages: int = DEFAULT_MAX_REPRESENTATIVE_PAGES,
) -> list[RepresentativePage]:
    """Select a deterministic, structurally diverse set of representative pages.

    Transparent greedy heuristic, never ML/random:

    1. Homepage/start page, whenever it was successfully observed.
    2. One top-ranked page per distinct site section (first path segment),
       ranked by connectivity (incoming, then outgoing links).
    3. One page per crawl depth not yet represented.
    4. Remaining capacity filled by connectivity rank.

    Connectivity ranks use incoming-link counts, then outgoing-link counts;
    a sitemap-discovered page breaks "up" ties slightly, then shallower crawl
    depth, then the normalized URL as the final stable tie-breaker.

    Args:
        successful_pages: Successfully observed page records.
        graph: The site internal-link graph built from those pages.
        discovery_sources: URL -> discovery-source tuple for crawled pages.
        normalized_start_url: Normalized start URL (homepage), may be "".
        max_representative_pages: Hard cap on the returned set.

    Returns:
        RepresentativePage list in selection priority order (empty when there
        are no successful pages or the limit is zero).
    """
    limit = max(0, int(max_representative_pages))
    if limit == 0:
        return []
    candidates = [p for p in successful_pages if p.observation is not None and p.url]
    if not candidates:
        return []

    def _sort_key(page: CrawlPageRecord):
        is_home = 1 if page.url == normalized_start_url else 0
        incoming = len(graph.incoming.get(page.url, ()))
        outgoing = len(graph.outgoing.get(page.url, ()))
        sitemap = 1 if DISCOVERY_SOURCE_SITEMAP in discovery_sources.get(page.url, ()) else 0
        return (-is_home, -incoming, -outgoing, -sitemap, page.depth, page.url)

    ranked = sorted(candidates, key=_sort_key)

    selected_urls: set[str] = set()
    selected: list[RepresentativePage] = []

    def _add(page: CrawlPageRecord, reason: str) -> bool:
        if len(selected) >= limit or page.url in selected_urls:
            return False
        selected_urls.add(page.url)
        selected.append(
            RepresentativePage(
                url=page.url,
                rank=len(selected) + 1,
                incoming_links=len(graph.incoming.get(page.url, ())),
                outgoing_links=len(graph.outgoing.get(page.url, ())),
                depth=page.depth,
                reason=reason,
            )
        )
        return True

    # 1. Homepage / start page, whenever it was successfully observed.
    if normalized_start_url:
        for page in ranked:
            if page.url == normalized_start_url:
                _add(page, "homepage_start")
                break

    seen_areas = {_path_area(rep.url) for rep in selected}

    # 2. One top-ranked page per distinct site section.
    for page in ranked:
        if len(selected) >= limit:
            break
        area = _path_area(page.url)
        if area in seen_areas:
            continue
        seen_areas.add(area)
        _add(page, f"site_section:{area if area else '/'}")

    # 3. One page per crawl depth not yet represented.
    seen_depths = {rep.depth for rep in selected}
    for page in ranked:
        if len(selected) >= limit:
            break
        if page.depth in seen_depths:
            continue
        seen_depths.add(page.depth)
        _add(page, f"depth_diversity:{page.depth}")

    # 4. Remaining capacity by connectivity rank.
    for page in ranked:
        if len(selected) >= limit:
            break
        _add(page, "rank_fill")

    return selected


# ---------------------------------------------------------------------------
# Site audit context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteAuditContext:
    """Deterministic structural context for one bounded site crawl.

    References the same ``CrawlPageRecord`` objects produced by ``crawl_site``
    (observations are never duplicated). All derived structures are pure
    deterministic functions of the ``CrawlResult`` and are computed once at
    build time.

    This is structural site context for downstream audits — it performs no
    semantic analysis and makes no judgments about page quality.
    """

    start_url: str
    normalized_start_url: Optional[str]
    is_success: bool
    crawl_error: Optional[str]
    crawl_stats: CrawlStats
    pages: tuple[CrawlPageRecord, ...]
    page_by_url: dict[str, CrawlPageRecord]
    observations: dict[str, PageObservation]
    discovery_sources: dict[str, tuple[str, ...]]
    unvisited_urls: tuple[UnvisitedUrlRecord, ...]
    internal_links: dict[str, tuple[str, ...]]
    external_links: dict[str, tuple[str, ...]]
    graph: SiteInternalGraph
    incoming_links: dict[str, tuple[str, ...]]
    representative_pages: tuple[str, ...]
    representative_selection: tuple[RepresentativePage, ...]
    sitemap_discovered_urls: tuple[str, ...]
    sitemap_files_parsed: int
    stats: SiteStats

    # -- Derived conveniences ----------------------------------------------

    @property
    def page_urls(self) -> tuple[str, ...]:
        return tuple(page.url for page in self.pages)

    @property
    def successful_pages(self) -> tuple[CrawlPageRecord, ...]:
        return tuple(page for page in self.pages if page.is_success)

    @property
    def successful_pages_urls(self) -> tuple[str, ...]:
        return tuple(page.url for page in self.pages if page.is_success)

    @property
    def failed_pages(self) -> tuple[CrawlPageRecord, ...]:
        return tuple(page for page in self.pages if not page.is_success)

    @property
    def failed_pages_urls(self) -> tuple[str, ...]:
        return tuple(page.url for page in self.pages if not page.is_success)

    @property
    def page_depths(self) -> dict[str, int]:
        return {page.url: page.depth for page in self.pages}

    # -- Serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Plain deterministic dict (JSON-serializable) for downstream use."""
        return {
            "start_url": self.start_url,
            "normalized_start_url": self.normalized_start_url,
            "is_success": self.is_success,
            "crawl_error": self.crawl_error,
            "page_urls": list(self.page_urls),
            "successful_page_urls": list(self.successful_pages_urls),
            "failed_page_urls": list(self.failed_pages_urls),
            "page_depths": dict(sorted(self.page_depths.items())),
            "discovery_sources": {
                url: list(sources)
                for url, sources in sorted(self.discovery_sources.items())
            },
            "internal_links": {
                url: list(targets)
                for url, targets in sorted(self.internal_links.items())
            },
            "external_links": {
                url: list(targets)
                for url, targets in sorted(self.external_links.items())
            },
            "incoming_links": {
                url: list(sources)
                for url, sources in sorted(self.incoming_links.items())
            },
            "graph": {
                "nodes": list(self.graph.nodes),
                "edges": [list(edge) for edge in self.graph.edges],
                "node_count": self.graph.node_count,
                "edge_count": self.graph.edge_count,
            },
            "unvisited_urls": [
                {
                    "url": record.url,
                    "reason": record.reason,
                    "depth": record.depth,
                    "sources": list(record.sources),
                    "discovered_from": record.discovered_from,
                }
                for record in self.unvisited_urls
            ],
            "sitemap_discovered_urls": list(self.sitemap_discovered_urls),
            "sitemap_files_parsed": self.sitemap_files_parsed,
            "representative_pages": list(self.representative_pages),
            "representative_selection": [
                {
                    "url": rep.url,
                    "rank": rep.rank,
                    "incoming_links": rep.incoming_links,
                    "outgoing_links": rep.outgoing_links,
                    "depth": rep.depth,
                    "reason": rep.reason,
                }
                for rep in self.representative_selection
            ],
            "stats": self.stats.to_dict(),
        }


def build_site_audit_context(
    result: CrawlResult,
    max_representative_pages: int = DEFAULT_MAX_REPRESENTATIVE_PAGES,
) -> SiteAuditContext:
    """Compose a bounded ``CrawlResult`` into a deterministic site-level context.

    Purely structural and offline: the context is a deterministic function of
    the crawl result. No fetches, no parses, no additional crawling.

    Args:
        result: The bounded ``CrawlResult`` produced by ``crawl_site``.
        max_representative_pages: Maximum number of representative pages to
            select (see :func:`select_representative_pages`).

    Returns:
        A deterministic ``SiteAuditContext``.

    Raises:
        TypeError: If ``result`` is not a ``CrawlResult``.
    """
    if not isinstance(result, CrawlResult):
        raise TypeError(
            f"build_site_audit_context expects a CrawlResult, got {type(result).__name__}"
        )

    normalized_start: str = result.stats.normalized_start_url or ""
    pages: tuple[CrawlPageRecord, ...] = tuple(result.pages)
    page_by_url: dict[str, CrawlPageRecord] = dict(result.pages_by_url)

    discovery_sources: dict[str, tuple[str, ...]] = {}
    for page in pages:
        sources = tuple(sorted(page.discovery_sources or ()))
        if not sources:
            # Defensive fallback: every crawled URL has a discovery source.
            if normalized_start and page.url == normalized_start:
                sources = (DISCOVERY_SOURCE_START,)
            else:
                sources = (DISCOVERY_SOURCE_LINK,)
        discovery_sources[page.url] = sources

    internal_links: dict[str, tuple[str, ...]] = {}
    external_links: dict[str, tuple[str, ...]] = {}
    observations: dict[str, PageObservation] = {}
    for page in pages:
        if page.observation is None:
            continue
        observations[page.url] = page.observation
        internal_links[page.url] = tuple(_internal_targets(page, normalized_start))
        external_links[page.url] = tuple(_external_targets(page, normalized_start))

    graph = build_internal_graph(pages, normalized_start)

    unvisited: tuple[UnvisitedUrlRecord, ...] = tuple(result.unvisited_urls)

    sitemap_urls: set[str] = {
        url
        for url, sources in discovery_sources.items()
        if DISCOVERY_SOURCE_SITEMAP in sources
    }
    sitemap_urls.update(
        record.url for record in unvisited if DISCOVERY_SOURCE_SITEMAP in record.sources
    )
    if result.sitemap_discovery is not None:
        sitemap_urls.update(result.sitemap_discovery.discovered_urls)
    sitemap_discovered_urls: tuple[str, ...] = tuple(sorted(sitemap_urls))

    successful = tuple(page for page in pages if page.is_success)
    selection = select_representative_pages(
        successful_pages=successful,
        graph=graph,
        discovery_sources=discovery_sources,
        normalized_start_url=normalized_start,
        max_representative_pages=max_representative_pages,
    )
    representative_pages: tuple[str, ...] = tuple(rep.url for rep in selection)

    stats = _build_site_stats(
        result=result,
        pages=pages,
        discovery_sources=discovery_sources,
        external_links=external_links,
        graph=graph,
        unvisited=unvisited,
        representative_count=len(selection),
    )

    return SiteAuditContext(
        start_url=result.stats.start_url,
        normalized_start_url=result.stats.normalized_start_url,
        is_success=result.is_success,
        crawl_error=result.error_message,
        crawl_stats=result.stats,
        pages=pages,
        page_by_url=page_by_url,
        observations=observations,
        discovery_sources=discovery_sources,
        unvisited_urls=unvisited,
        internal_links=internal_links,
        external_links=external_links,
        graph=graph,
        incoming_links=graph.incoming,
        representative_pages=representative_pages,
        representative_selection=tuple(selection),
        sitemap_discovered_urls=sitemap_discovered_urls,
        sitemap_files_parsed=result.stats.sitemap_files_parsed,
        stats=stats,
    )