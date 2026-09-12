"""Marketplace Orchestrator — single designated entrypoint.

Composes the existing reusable Agent Skills into one deterministic audit pipeline:

    AuditRequest (or plain URL string)
     ↓ fetch_page()          (crawler — one HTTP GET, read-only)
     ↓ observe_page()        (observation — one HTML parse, no network)
     ↓ audit_*(obs)          (each registered audit receives the SAME PageObservation)
     ↓ source-tag findings   (each finding tagged with its originating audit)
     ↓ aggregate + deduplicate + sort
     ↓ AuditResult
     ↓ (+ run_audit_full only)
     ↓ objective mapping      (map_findings_to_objectives — no semantic links claimed yet)
     ↓ priority engine        (collect_signals → compute_priorities — orders by audit values only)
     ↓ AuditReport

Design constraints (enforced, never relaxed):
- Exactly one crawler call per run.
- Exactly one observation call per run.
- Every audit receives the same PageObservation object.
- No LLM, no external AI API, no browser automation, no analytics.
- No writes to the audited website (read-only throughout).
- Deterministic: same URL + same HTML → same findings.
- Individual audit failures are isolated; other audits continue.
- Finding objects are never mutated.
- Finding.details are gated at the serialisation boundary; not stripped from objects.
- Source audit attribution is tracked on every finding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from src.audits.finding_aggregator import (
    aggregate_findings,
    deduplicate_findings,
    sort_findings,
    tag_findings,
)
from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding
from src.audits.objective_mapping import map_findings_to_objectives
from src.audits.priority_engine import collect_signals, compute_priorities
from src.audits.reporting import AuditReport, build_report
from src.audits.task_readiness import audit_task_readiness
from src.crawler.crawler import DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER_AGENT, FetchedPage, fetch_page
from src.observation.observation import PageObservation, observe_page

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audit registry — internal only. Add future audits here without touching
# any existing file. Each entry maps a stable audit name to its callable.
# ---------------------------------------------------------------------------
_AuditFn = Callable[[PageObservation], list[Finding]]

AUDIT_REGISTRY: dict[str, _AuditFn] = {
    "machine_answerability": lambda obs: audit_machine_answerability(obs),
    "task_readiness": lambda obs: audit_task_readiness(obs),
}

# Optional audits — registered but not in default set.
# They can be activated by name in AuditOptions.audits or AuditRequest.audits.
_optional_audits_registered = False


def _ensure_optional_audits_registered() -> None:
    """Lazy-register optional audits to avoid import failures when
    dependencies are unavailable. Called once on first need."""
    global _optional_audits_registered  # noqa: PLW0603
    if _optional_audits_registered:
        return
    try:
        from src.audits.crawl_render import audit_crawl_render
        from src.audits.engagement import audit_engagement
        from src.audits.evidence_consistency import audit_evidence_consistency
        from src.audits.freshness_corroboration import audit_freshness_corroboration
        from src.audits.t4_indexability import audit_t4_indexability

        AUDIT_REGISTRY["crawl_render"] = lambda obs: audit_crawl_render(obs)
        AUDIT_REGISTRY["engagement"] = lambda obs: audit_engagement(obs)
        AUDIT_REGISTRY["evidence_consistency"] = lambda obs: audit_evidence_consistency(obs)
        AUDIT_REGISTRY["freshness_corroboration"] = lambda obs: audit_freshness_corroboration(obs)
        AUDIT_REGISTRY["t4_indexability"] = lambda obs: audit_t4_indexability(obs)
    except ImportError:
        logger.debug(
            "Optional audits (crawl_render, engagement, evidence_consistency, "
            "freshness_corroboration) not available."
        )
    _optional_audits_registered = True


# Deterministic default execution order — all six specialist audits run by default.
# Explicit audit selection (via AuditRequest.audits or AuditOptions.audits) overrides this.
_DEFAULT_AUDITS: tuple[str, ...] = (
    "machine_answerability",
    "task_readiness",
    "crawl_render",
    "engagement",
    "evidence_consistency",
    "freshness_corroboration",
)

# ---------------------------------------------------------------------------
# Import AuditRequest for public API
# ---------------------------------------------------------------------------
from src.audits.audit_request import AuditContext, AuditRequest  # noqa: E402


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditOptions:
    """Immutable orchestration configuration.

    Attributes:
        audits: Ordered tuple of audit names to execute. Each name must exist in
            AUDIT_REGISTRY. Duplicate names are silently de-duplicated to prevent
            the same audit running twice. Defaults to all registered audits in
            stable order.
        timeout_seconds: HTTP read/connect timeout passed to fetch_page().
        user_agent: User-Agent header passed to fetch_page().
        include_details: When True, Finding.details is included in any serialised
            output produced by as_report_dict(). Defaults to False so that internal
            diagnostic keys (evidence_score, broken_intents, etc.) are not exposed
            to marketplace consumers by default.
    """

    audits: tuple[str, ...] = _DEFAULT_AUDITS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    include_details: bool = False
    check_robots: bool = True


@dataclass(frozen=True)
class AuditResult:
    """Structured result of a single orchestrated audit run.

    Attributes:
        requested_url: The original URL provided to run_audit().
        final_url: The URL after HTTP redirect resolution; None if fetch failed
            before a response was received.
        status_code: HTTP status code; None on network or pre-response failure.
        is_success: True if crawling and observation both succeeded. Does NOT
            reflect whether audits found problems — a page with findings is still
            a successful run.
        error_message: Top-level error detail when is_success is False.
        findings: Deduplicated, priority-sorted list of all audit findings.
            Always a list (never None). Empty list on failure or clean pages.
        audit_errors: Maps audit name → exception string for any audit that raised
            an unexpected exception during execution. Empty dict on clean runs.
        options: The AuditOptions used for this run (for traceability).
    """

    requested_url: str
    final_url: Optional[str]
    status_code: Optional[int]
    is_success: bool
    error_message: Optional[str]
    findings: list[Finding]
    audit_errors: dict[str, str]
    options: AuditOptions

    def as_report_dict(self) -> dict[str, Any]:
        """Serialise this result to a plain dictionary suitable for JSON output.

        Respects options.include_details: Finding.details is omitted from each
        finding dict when include_details is False (the default). The underlying
        Finding objects are never mutated.

        Returns:
            Plain dict containing all result fields with findings as dicts.
        """
        finding_dicts = []
        for f in self.findings:
            fd: dict[str, Any] = {
                "id": f.id,
                "title": f.title,
                "severity": f.severity,
                "evidence": f.evidence,
                "priority": f.priority,
                "confidence": f.confidence,
                "source_audit": f.source_audit,
                "suggested_action": {
                    "summary": f.suggested_action.summary,
                    "priority": f.suggested_action.priority,
                },
            }
            if self.options.include_details:
                fd["details"] = f.details
            finding_dicts.append(fd)

        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "is_success": self.is_success,
            "error_message": self.error_message,
            "findings": finding_dicts,
            "audit_errors": dict(self.audit_errors),
            "audits_run": list(self.options.audits),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Remove exact-duplicate findings while preserving first-occurrence order.

    Delegates to finding_aggregator.deduplicate_findings for the actual logic.
    Kept as a module-level alias for backward compatibility with existing tests.
    """
    return deduplicate_findings(findings)


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by (priority ASC, severity DESC) using audit-supplied values only.

    Delegates to finding_aggregator.sort_findings for the actual logic.
    Kept as a module-level alias for backward compatibility with existing tests.
    """
    return sort_findings(findings)


def _run_audit_safe(
    audit_fn: _AuditFn,
    obs: PageObservation,
    name: str,
    audit_errors: dict[str, str],
) -> list[Finding]:
    """Execute one audit function with full exception isolation.

    If the audit raises any exception, the error is recorded in audit_errors and
    an empty list is returned. Other audits are not affected.

    Args:
        audit_fn: The audit callable from AUDIT_REGISTRY.
        obs: The shared PageObservation for this run.
        name: Stable audit name (used as the key in audit_errors).
        audit_errors: Mutable dict populated with any exception details.

    Returns:
        list[Finding] from the audit, or [] on exception.
    """
    try:
        return audit_fn(obs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit '%s' raised an unexpected exception: %s", name, exc)
        audit_errors[name] = str(exc)
        return []


def _resolve_audit_names(requested: tuple[str, ...]) -> tuple[str, ...]:
    """Validate and de-duplicate audit names, preserving requested order.

    Unknown audit names are logged and silently skipped rather than raising,
    so that an unrecognised name in options.audits never crashes the pipeline.
    Duplicate names are reduced to a single occurrence (first seen wins).

    Args:
        requested: Tuple of audit names from AuditOptions.audits.

    Returns:
        De-duplicated tuple of recognised audit names in their original order.
    """
    _ensure_optional_audits_registered()

    seen: set[str] = set()
    resolved: list[str] = []
    for name in requested:
        if name in seen:
            logger.debug("Audit '%s' listed more than once; running only once.", name)
            continue
        if name not in AUDIT_REGISTRY:
            logger.warning("Unknown audit '%s' requested; skipping.", name)
            seen.add(name)
            continue
        seen.add(name)
        resolved.append(name)
    return tuple(resolved)


def _failure_result(
    requested_url: str,
    error_message: str,
    options: AuditOptions,
    fetched: Optional[FetchedPage] = None,
) -> AuditResult:
    """Construct a standardised failure AuditResult.

    Preserves any URL/status information already available from the crawler.
    """
    return AuditResult(
        requested_url=requested_url,
        final_url=fetched.final_url if fetched else None,
        status_code=fetched.status_code if fetched else None,
        is_success=False,
        error_message=error_message,
        findings=[],
        audit_errors={},
        options=options,
    )


def _options_from_request(request: AuditRequest) -> AuditOptions:
    """Build AuditOptions from an AuditRequest, applying safe defaults."""
    kwargs: dict[str, Any] = {}
    if request.audits is not None:
        kwargs["audits"] = request.audits
    if request.timeout_seconds is not None:
        kwargs["timeout_seconds"] = request.timeout_seconds
    if request.user_agent is not None:
        kwargs["user_agent"] = request.user_agent
    if getattr(request, "check_robots", None) is not None:
        kwargs["check_robots"] = request.check_robots
    kwargs["include_details"] = request.include_details
    return AuditOptions(**kwargs)


# ---------------------------------------------------------------------------
# Public entrypoint — ONE designated orchestration function
# ---------------------------------------------------------------------------


def run_audit(
    url: Union[str, AuditRequest],
    options: Optional[AuditOptions] = None,
) -> AuditResult:
    """Execute a full read-only audit pipeline for a single URL.

    This is the single designated entrypoint for the marketplace orchestrator.
    It composes the existing crawler, observation, and audit Agent Skills without
    duplicating any of their logic.

    Accepts either:
    - A plain URL string (backward-compatible with existing callers).
    - An AuditRequest carrying URL, task context, and configuration.

    Flow:
        1. Validate/normalise inputs.
        2. fetch_page(url)          — one HTTP GET (crawler).
        3. observe_page(fetched)    — one HTML parse (observation layer).
        4. For each selected audit: _run_audit_safe(audit_fn, obs, …)
           — all audits receive the SAME PageObservation object.
           — individual audit exceptions are isolated; other audits continue.
        5. Source-tag findings with originating audit name.
        6. Aggregate: deduplicate + sort.
        7. Return AuditResult.

    Args:
        url: Target URL string or AuditRequest. When an AuditRequest is provided,
            its target_url is used for crawling and its configuration overrides
            the options parameter.
        options: Optional AuditOptions controlling audit selection, HTTP timeout,
            User-Agent, and include_details behaviour. Safe defaults are applied
            when None. Ignored when request carries its own audit configuration.

    Returns:
        AuditResult. is_success is True if crawling and observation both succeeded
        (even if findings are present). is_success is False on network errors,
        non-HTML content, or observation failures.
    """
    # ------------------------------------------------------------------
    # Step 0 — Normalise inputs (handle AuditRequest or plain URL)
    # ------------------------------------------------------------------
    request: Optional[AuditRequest] = None
    if isinstance(url, AuditRequest):
        request = url
        target_url = request.target_url
        if options is None:
            options = _options_from_request(request)
        elif request.audits is not None:
            options = _options_from_request(request)
    else:
        target_url = url

    if options is None:
        options = AuditOptions()

    # ------------------------------------------------------------------
    # Step 1 — Resolve audit names (de-duplicate, validate against registry)
    # ------------------------------------------------------------------
    audit_names = _resolve_audit_names(options.audits)

    # ------------------------------------------------------------------
    # Step 2 — Crawl (exactly one fetch_page call)
    # ------------------------------------------------------------------
    fetched: FetchedPage = fetch_page(
        target_url,
        timeout=options.timeout_seconds,
        user_agent=options.user_agent,
        check_robots=options.check_robots,
    )

    if not fetched.is_success:
        return _failure_result(
            requested_url=target_url,
            error_message=fetched.error_message or "Page fetch was unsuccessful.",
            options=options,
            fetched=fetched,
        )

    # ------------------------------------------------------------------
    # Step 3 — Observe (exactly one observe_page call)
    # ------------------------------------------------------------------
    try:
        obs: PageObservation = observe_page(fetched)
    except Exception as exc:  # noqa: BLE001
        return _failure_result(
            requested_url=target_url,
            error_message=f"Observation layer raised an unexpected exception: {exc}",
            options=options,
            fetched=fetched,
        )

    # observe_page() may succeed structurally but set is_success=False on the
    # observation (e.g. HTML parse failure). Treat this as a pipeline failure.
    if not obs.is_success:
        return _failure_result(
            requested_url=target_url,
            error_message=obs.error_message or "Observation of fetched page was unsuccessful.",
            options=options,
            fetched=fetched,
        )

    # ------------------------------------------------------------------
    # Step 4 — Run each selected audit against THE SAME obs
    #         Source-tag findings with originating audit name
    # ------------------------------------------------------------------
    audit_errors: dict[str, str] = {}
    all_findings: list[Finding] = []

    for name in audit_names:
        audit_fn = AUDIT_REGISTRY[name]
        raw_findings = _run_audit_safe(audit_fn, obs, name, audit_errors)
        tagged = tag_findings(raw_findings, source_audit=name)
        all_findings.extend(tagged)

    # ------------------------------------------------------------------
    # Step 5 — Aggregate: deduplicate then sort
    # ------------------------------------------------------------------
    sorted_findings = aggregate_findings(all_findings)

    # ------------------------------------------------------------------
    # Step 6 — Return structured result
    # ------------------------------------------------------------------
    return AuditResult(
        requested_url=target_url,
        final_url=obs.final_url,
        status_code=obs.status_code,
        is_success=True,
        error_message=None,
        findings=sorted_findings,
        audit_errors=audit_errors,
        options=options,
    )


# ---------------------------------------------------------------------------
# Extended entrypoint — returns AuditReport for downstream consumers
# ---------------------------------------------------------------------------


def run_audit_full(
    url: Union[str, AuditRequest],
    options: Optional[AuditOptions] = None,
) -> tuple[AuditResult, AuditReport]:
    """Execute the complete audit pipeline and return both AuditResult and AuditReport.

    This is the designated marketplace entrypoint covering both AI discoverability
    and on-site engagement. It builds on run_audit() — which already defaults to
    all six specialist audits — and adds the downstream reasoning stages:

        run_audit (fetch → observe → all-six audits → aggregate)
            ↓ Objective Mapping    (map_findings_to_objectives)
            ↓ Priority Engine      (collect_signals → compute_priorities)
            ↓ Final Report         (build_report)

    Explicit audit selection (via AuditRequest.audits or AuditOptions.audits) is
    respected and passed through unchanged.

    Args:
        url: Target URL string or AuditRequest.
        options: Optional AuditOptions.

    Returns:
        Tuple of (AuditResult, AuditReport).
    """
    result = run_audit(url, options)

    # Extract context from AuditRequest if provided
    task_intent = None
    task_description = None
    target_objectives: list[str] = []
    if isinstance(url, AuditRequest) and url.context is not None:
        task_intent = url.context.task_intent
        task_description = url.context.task_description
        target_objectives = url.context.target_objectives

    # Objective Mapping — evaluates findings against requested objectives and task intent
    objective_mappings = map_findings_to_objectives(
        result.findings,
        target_objectives=target_objectives or None,
        task_intent=task_intent,
        task_description=task_description,
    )

    # Priority Engine — computes task-aware ranking and evidence-based rationale
    priority_signals = compute_priorities(
        collect_signals(
            result.findings,
            affected_url=result.final_url or result.requested_url,
            objective_mappings=objective_mappings,
        )
    )

    report = build_report(
        requested_url=result.requested_url,
        final_url=result.final_url,
        status_code=result.status_code,
        is_success=result.is_success,
        error_message=result.error_message,
        findings=result.findings,
        audit_errors=result.audit_errors,
        audits_run=list(result.options.audits),
        task_intent=task_intent,
        task_description=task_description,
        target_objectives=target_objectives,
        objective_mappings=objective_mappings,
        priority_signals=priority_signals,
    )

    return result, report
