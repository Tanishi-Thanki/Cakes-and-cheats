"""P3-B1 (Person B): offline E2E evaluation harness.

Independent "judge" harness that drives the complete product pipeline against
deterministic in-memory fixture sites and validates the final structured output:

    website (fixture) -> crawler (crawl_site) -> observation (observe_page)
        -> SiteAuditContext (build_site_audit_context)
        -> specialist audits (six) -> aggregation (tag + aggregate_findings)
        -> final structured report (summary + findings)

Design rules:
- Fully offline. Fixtures are served by an in-memory ``FakeSite`` through the
  established ``FetcherCallable`` seam; no network, no LLM, no browser.
- This module only *consumes* the owned pipeline components (crawl_site,
  build_site_audit_context, the six audits, finding_aggregator). It does not
  re-implement crawling, auditing, aggregation, or reporting.
- All output is deterministic: ordered dicts / sorted lists, no wall-clock
  data, no random identifiers.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

from src.audits.crawl_render import audit_crawl_render  # noqa: E402
from src.audits.engagement import audit_engagement  # noqa: E402
from src.audits.evidence_consistency import audit_evidence_consistency  # noqa: E402
from src.audits.finding_aggregator import aggregate_findings, tag_findings  # noqa: E402
from src.audits.freshness_corroboration import audit_freshness_corroboration  # noqa: E402
from src.audits.machine_answerability import audit_machine_answerability  # noqa: E402
from src.audits.task_readiness import audit_task_readiness  # noqa: E402
from src.crawler.crawler import FetchedPage  # noqa: E402
from src.crawler.crawl_session import CrawlConfig, CrawlResult, crawl_site  # noqa: E402
from src.crawler.site_context import SiteAuditContext, build_site_audit_context  # noqa: E402
from src.audits.models import Finding  # noqa: E402

# Order matches the orchestrator's default audit order so results are comparable.
SIX_AUDITS: Tuple[str, ...] = (
    "machine_answerability",
    "task_readiness",
    "crawl_render",
    "engagement",
    "evidence_consistency",
    "freshness_corroboration",
)

_AUDIT_FUNCS: Dict[str, Callable] = {
    "machine_answerability": audit_machine_answerability,
    "task_readiness": audit_task_readiness,
    "crawl_render": audit_crawl_render,
    "engagement": audit_engagement,
    "evidence_consistency": audit_evidence_consistency,
    "freshness_corroboration": audit_freshness_corroboration,
}

SEVERITY_KEYS = ("critical", "high", "medium", "low")


# ---------------------------------------------------------------------------
# In-memory fixture infrastructure (offline FetcherCallable seam)
# ---------------------------------------------------------------------------


def ok_page(url: str, html: str, content_type: str = "text/html") -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type=content_type,
        html_content=html,
        is_success=True,
        error_message=None,
    )


def fail_page(url: str, message: str, status: Optional[int] = None) -> FetchedPage:
    return FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=status,
        content_type="",
        html_content="",
        is_success=False,
        error_message=message,
    )


class FakeSite:
    """In-memory site serving HTML pages, robots.txt, sitemaps and failures.

    ``pages`` maps path -> HTML. ``robots`` / ``sitemap`` are the conventional
    resources (None means "missing" -> 404, the fail-open case). ``sitemaps``
    maps arbitrary sitemap/index paths -> XML for nested sitemap scenarios.
    ``failures`` maps path -> FetchedPage and always wins, so robots-blocked,
    timeout, oversized, non-HTML and redirect-loop pages can be emulated.
    Every request is counted in ``fetch_counts`` (used to prove read-only,
    single-fetch, offline behaviour).
    """

    def __init__(self, *, pages=None, robots=None, sitemap=None, sitemaps=None, failures=None):
        self.pages = pages or {}
        self.robots = robots
        self.sitemap = sitemap
        self.sitemaps = sitemaps or {}
        self.failures = failures or {}
        self.fetch_counts: Dict[str, int] = {}

    def fetch(self, url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
        self.fetch_counts[url] = self.fetch_counts.get(url, 0) + 1
        path = urllib.parse.urlsplit(url).path
        if path in self.failures:
            return self.failures[path]
        if path == "/robots.txt":
            if self.robots is not None:
                return ok_page(url, self.robots, "text/plain")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)
        if path == "/sitemap.xml":
            if self.sitemap is not None:
                return ok_page(url, self.sitemap, "application/xml")
            return fail_page(url, "HTTP error status 404 (Not Found).", 404)
        if path in self.sitemaps:
            return ok_page(url, self.sitemaps[path], "application/xml")
        if path in self.pages:
            return ok_page(url, self.pages[path])
        return fail_page(url, "HTTP error status 404 (Not Found).", 404)


def crawl_fixture(site: FakeSite, start: str, **kwargs) -> CrawlResult:
    config = CrawlConfig(start_url=start, **kwargs)
    return crawl_site(config, fetcher=site.fetch)


def build_context(result: CrawlResult) -> SiteAuditContext:
    return build_site_audit_context(result)


# ---------------------------------------------------------------------------
# Site-level evaluation: crawl -> context -> six audits -> report
# ---------------------------------------------------------------------------


@dataclass
class SiteEvaluation:
    """Result of one judge-style site evaluation.

    Attributes:
        result: The bounded CrawlResult produced by crawl_site.
        ctx: The SiteAuditContext built from the crawl.
        per_page: URL -> (audit name -> sorted tuple of Finding objects)
            for every successfully observed page.
        tagged: Every finding, source-tagged and carrying the observed page in
            details, in deterministic per-page/per-audit order.
        aggregated: Findings after the real aggregation layer (dedupe + sort).
        audit_errors: "url|audit" -> exception string for any audit that raised.
        final: Final structured report dict (summary + findings + crawl stats).
    """

    result: CrawlResult
    ctx: SiteAuditContext
    per_page: Dict[str, Dict[str, Tuple[Finding, ...]]]
    tagged: List[Finding]
    aggregated: List[Finding]
    audit_errors: Dict[str, str]
    final: Dict[str, object]

    def finding_ids(self, url: str) -> Tuple[str, ...]:
        ids: List[str] = []
        for findings in self.per_page.get(url, {}).values():
            ids.extend(f.id for f in findings)
        return tuple(sorted(ids))

    def audit_ids(self, url: str, audit: str) -> Tuple[str, ...]:
        return tuple(f.id for f in self.per_page.get(url, {}).get(audit, ()))

    def ids_across_site(self) -> Tuple[str, ...]:
        ids: List[str] = []
        for url in self.per_page:
            ids.extend(self.finding_ids(url))
        return tuple(sorted(ids))


def run_site_evaluation(
    site: FakeSite,
    start: str,
    *,
    max_pages: int = 50,
    max_depth: int = 3,
    audits: Tuple[str, ...] = SIX_AUDITS,
) -> SiteEvaluation:
    """Run the full offline evaluation chain against a fixture site."""
    result = crawl_fixture(site, start, max_pages=max_pages, max_depth=max_depth)
    ctx = build_context(result)

    per_page: Dict[str, Dict[str, Tuple[Finding, ...]]] = {}
    tagged: List[Finding] = []
    audit_errors: Dict[str, str] = {}

    for record in result.pages:
        if record.observation is None:
            continue
        obs = record.observation
        per_audit: Dict[str, Tuple[Finding, ...]] = {}
        for name in audits:
            try:
                findings = _AUDIT_FUNCS[name](obs)
            except Exception as exc:  # noqa: BLE001 — isolate audit failures like the orchestrator
                audit_errors[f"{record.url}|{name}"] = str(exc)
                findings = []
            per_audit[name] = tuple(findings)
            for finding in findings:
                enriched = dataclasses.replace(
                    finding, details={**finding.details, "page": record.url}
                )
                tagged.extend(tag_findings([enriched], source_audit=name))
        per_page[record.url] = per_audit

    aggregated = aggregate_findings(tagged)
    final = _build_final_report(result, aggregated)

    return SiteEvaluation(
        result=result,
        ctx=ctx,
        per_page=per_page,
        tagged=tagged,
        aggregated=aggregated,
        audit_errors=audit_errors,
        final=final,
    )


def _severity_summary(findings: List[Finding]) -> Dict[str, int]:
    summary: Dict[str, int] = {"high": 0, "medium": 0, "low": 0}
    critical = 0
    for finding in findings:
        level = getattr(finding, "severity", "")
        if level == "critical":
            critical += 1
        elif level in summary:
            summary[level] += 1
    if critical:
        summary["critical"] = critical
    summary["total_findings"] = len(findings)
    return summary


def _finding_dict(finding: Finding) -> Dict[str, object]:
    return {
        "id": finding.id,
        "title": finding.title,
        "severity": finding.severity,
        "evidence": finding.evidence,
        "priority": finding.priority,
        "confidence": finding.confidence,
        "source_audit": finding.source_audit,
        "suggested_action": {
            "summary": finding.suggested_action.summary,
            "priority": finding.suggested_action.priority,
        },
        "details": finding.details,
    }


def _build_final_report(result: CrawlResult, findings: List[Finding]) -> Dict[str, object]:
    stats = result.stats
    unvisited = [
        {"url": record.url, "reason": record.reason, "depth": record.depth}
        for record in result.unvisited_urls
    ]
    return {
        "site": stats.start_url,
        "summary": _severity_summary(findings),
        "findings": [_finding_dict(f) for f in findings],
        "crawl": {
            "is_success": result.is_success,
            "pages_attempted": stats.pages_attempted,
            "pages_fetched": stats.pages_fetched,
            "pages_failed": stats.pages_failed,
            "urls_discovered": stats.urls_discovered,
            "max_pages": stats.max_pages,
            "max_depth": stats.max_depth,
            "depth_reached": stats.max_depth_reached,
            "errors": dict(stats.errors),
            "sitemap_files_parsed": stats.sitemap_files_parsed,
            "sitemap_urls_discovered": stats.sitemap_urls_discovered,
            "unvisited": unvisited,
        },
    }


def validate_site_report(evaluation: SiteEvaluation) -> List[str]:
    """Validate the final structured report contract; return list of problems."""
    problems: List[str] = []
    report = evaluation.final
    summary = report["summary"]
    findings = report["findings"]

    if not isinstance(summary, dict) or summary.get("total_findings") != len(findings):
        problems.append("summary.total_findings != len(findings)")

    counts = {"high": 0, "medium": 0, "low": 0, "critical": 0}
    for f in findings:
        sev = f.get("severity", "")
        if sev in counts:
            counts[sev] += 1
    for key in ("high", "medium", "low", "critical"):
        expected = counts[key]
        present = summary.get(key, 0) if key in summary else 0
        if key not in summary and counts[key] > 0:
            problems.append(f"severity count for '{key}' missing from summary")
        elif expected != present:
            problems.append(f"severity summary mismatch for '{key}': {expected} != {present}")

    for idx, f in enumerate(findings):
        if not f.get("id"):
            problems.append(f"findings[{idx}] missing id")
        if not (f.get("evidence") or "").strip():
            problems.append(f"findings[{idx}] ({f.get('id')}) missing evidence")
        action = f.get("suggested_action")
        if not isinstance(action, dict) or not (action.get("summary") or "").strip():
            problems.append(f"findings[{idx}] ({f.get('id')}) missing suggested_action")

    if evaluation.audit_errors:
        problems.append("audit exceptions observed during evaluation")

    try:
        round_tripped = json.loads(json.dumps(report))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"final report not JSON-serializable: {exc}")
        return problems
    if round_tripped != report:
        problems.append("final report does not survive a JSON round-trip")

    return problems