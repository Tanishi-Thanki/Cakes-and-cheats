"""Crawler module: single-page fetching and bounded site crawl foundation.

Imports are kept lazy (PEP 562) for the crawl-foundation additions to avoid a
circular import: ``src.observation.observation`` imports ``src.crawler.crawler``
during its own initialization, so this package must not eagerly import any
module that (transitively) imports the observation layer at import time.
"""

from src.crawler.crawler import FetchedPage, check_robots_txt, fetch_page, fetch_text

__all__ = [
    "FetchedPage",
    "check_robots_txt",
    "fetch_page",
    "fetch_text",
    "CrawlConfig",
    "CrawlPageRecord",
    "CrawlResult",
    "CrawlStats",
    "crawl_site",
    "extract_site_key",
    "is_crawlable_scheme",
    "is_rejected_scheme",
    "is_same_site",
    "normalize_url",
    "resolve_and_normalize",
    "resolve_url",
    "SitemapDiscoveryResult",
    "discover_sitemap_urls",
    "DISCOVERY_SOURCE_LINK",
    "DISCOVERY_SOURCE_SITEMAP",
    "DISCOVERY_SOURCE_START",
    "UnvisitedUrlRecord",
    "DEFAULT_MAX_REPRESENTATIVE_PAGES",
    "RepresentativePage",
    "SiteAuditContext",
    "SiteInternalGraph",
    "SiteStats",
    "build_internal_graph",
    "build_site_audit_context",
    "select_representative_pages",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "CrawlConfig": ("src.crawler.crawl_session", "CrawlConfig"),
    "CrawlPageRecord": ("src.crawler.crawl_session", "CrawlPageRecord"),
    "CrawlResult": ("src.crawler.crawl_session", "CrawlResult"),
    "CrawlStats": ("src.crawler.crawl_session", "CrawlStats"),
    "crawl_site": ("src.crawler.crawl_session", "crawl_site"),
    "extract_site_key": ("src.crawler.url_utils", "extract_site_key"),
    "is_crawlable_scheme": ("src.crawler.url_utils", "is_crawlable_scheme"),
    "is_rejected_scheme": ("src.crawler.url_utils", "is_rejected_scheme"),
    "is_same_site": ("src.crawler.url_utils", "is_same_site"),
    "normalize_url": ("src.crawler.url_utils", "normalize_url"),
    "resolve_and_normalize": ("src.crawler.url_utils", "resolve_and_normalize"),
    "resolve_url": ("src.crawler.url_utils", "resolve_url"),
    "SitemapDiscoveryResult": ("src.crawler.robots_sitemaps", "SitemapDiscoveryResult"),
    "discover_sitemap_urls": ("src.crawler.robots_sitemaps", "discover_sitemap_urls"),
    "DISCOVERY_SOURCE_LINK": ("src.crawler.crawl_session", "DISCOVERY_SOURCE_LINK"),
    "DISCOVERY_SOURCE_SITEMAP": ("src.crawler.crawl_session", "DISCOVERY_SOURCE_SITEMAP"),
    "DISCOVERY_SOURCE_START": ("src.crawler.crawl_session", "DISCOVERY_SOURCE_START"),
    "UnvisitedUrlRecord": ("src.crawler.crawl_session", "UnvisitedUrlRecord"),
    "DEFAULT_MAX_REPRESENTATIVE_PAGES": ("src.crawler.site_context", "DEFAULT_MAX_REPRESENTATIVE_PAGES"),
    "RepresentativePage": ("src.crawler.site_context", "RepresentativePage"),
    "SiteAuditContext": ("src.crawler.site_context", "SiteAuditContext"),
    "SiteInternalGraph": ("src.crawler.site_context", "SiteInternalGraph"),
    "SiteStats": ("src.crawler.site_context", "SiteStats"),
    "build_internal_graph": ("src.crawler.site_context", "build_internal_graph"),
    "build_site_audit_context": ("src.crawler.site_context", "build_site_audit_context"),
    "select_representative_pages": ("src.crawler.site_context", "select_representative_pages"),
}


def __getattr__(name: str) -> object:
    """Lazily resolve crawl-foundation names on first access."""
    if name in _LAZY_EXPORTS:
        import importlib

        module_name, attr_name = _LAZY_EXPORTS[name]
        return getattr(importlib.import_module(module_name), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")