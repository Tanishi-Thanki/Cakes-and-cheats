"""Marketplace Orchestrator — single designated entrypoint.

Composes the existing reusable Agent Skills into one deterministic audit pipeline:

    URL
     ↓ fetch_page()          (crawler — one HTTP GET, read-only)
     ↓ observe_page()        (observation — one HTML parse, no network)
     ↓ audit_*(obs)          (each registered audit receives the SAME PageObservation)
     ↓ deduplicate + sort
     ↓ AuditResult

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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding
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

# Deterministic default execution order (preserves result ordering).
_DEFAULT_AUDITS: tuple[str, ...] = ("machine_answerability", "task_readiness")

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

    Deduplication key: (finding.id, finding.evidence[:120])

    Rationale:
    - Keying on id alone would incorrectly merge two distinct findings from the
      same rule triggered by different evidence (e.g. two broken Add-to-Cart links).
    - Truncating evidence to 120 chars provides a stable fingerprint while
      tolerating minor trailing-whitespace differences.
    - First occurrence is kept; subsequent identical (id, evidence) pairs are dropped.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = (finding.id, finding.evidence[:120])
        if key not in seen:
            seen.add(key)
            deduped.append(finding)
    return deduped


_SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _sort_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by (priority ASC, severity DESC) using audit-supplied values only.

    The orchestrator does not invent, modify, or reinterpret severity or priority.
    It only orders findings for presentation convenience.
    """
    return sorted(
        findings,
        key=lambda f: (f.priority, _SEVERITY_ORDER.get(f.severity, 99)),
    )


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


# ---------------------------------------------------------------------------
# Public entrypoint — ONE designated orchestration function
# ---------------------------------------------------------------------------


def run_audit(url: str, options: Optional[AuditOptions] = None) -> AuditResult:
    """Execute a full read-only audit pipeline for a single URL.

    This is the single designated entrypoint for the marketplace orchestrator.
    It composes the existing crawler, observation, and audit Agent Skills without
    duplicating any of their logic.

    Flow:
        1. Validate/normalise inputs.
        2. fetch_page(url)          — one HTTP GET (crawler).
        3. observe_page(fetched)    — one HTML parse (observation layer).
        4. For each selected audit: _run_audit_safe(audit_fn, obs, …)
           — all audits receive the SAME PageObservation object.
           — individual audit exceptions are isolated; other audits continue.
        5. Deduplicate findings (key: id + evidence[:120]).
        6. Sort findings by (priority ASC, severity DESC).
        7. Return AuditResult.

    Args:
        url: Target URL to audit. Must be an http/https URL. The crawler
            validates the scheme and returns a failure result for invalid URLs.
        options: Optional AuditOptions controlling audit selection, HTTP timeout,
            User-Agent, and include_details behaviour. Safe defaults are applied
            when None.

    Returns:
        AuditResult. is_success is True if crawling and observation both succeeded
        (even if findings are present). is_success is False on network errors,
        non-HTML content, or observation failures.
    """
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
        url,
        timeout=options.timeout_seconds,
        user_agent=options.user_agent,
    )

    if not fetched.is_success:
        return _failure_result(
            requested_url=url,
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
            requested_url=url,
            error_message=f"Observation layer raised an unexpected exception: {exc}",
            options=options,
            fetched=fetched,
        )

    # observe_page() may succeed structurally but set is_success=False on the
    # observation (e.g. HTML parse failure). Treat this as a pipeline failure.
    if not obs.is_success:
        return _failure_result(
            requested_url=url,
            error_message=obs.error_message or "Observation of fetched page was unsuccessful.",
            options=options,
            fetched=fetched,
        )

    # ------------------------------------------------------------------
    # Step 4 — Run each selected audit against THE SAME obs
    # ------------------------------------------------------------------
    audit_errors: dict[str, str] = {}
    all_findings: list[Finding] = []

    for name in audit_names:
        audit_fn = AUDIT_REGISTRY[name]
        findings = _run_audit_safe(audit_fn, obs, name, audit_errors)
        all_findings.extend(findings)

    # ------------------------------------------------------------------
    # Step 5 — Deduplicate then sort
    # ------------------------------------------------------------------
    deduped = _deduplicate_findings(all_findings)
    sorted_findings = _sort_findings(deduped)

    # ------------------------------------------------------------------
    # Step 6 — Return structured result
    # ------------------------------------------------------------------
    return AuditResult(
        requested_url=url,
        final_url=obs.final_url,
        status_code=obs.status_code,
        is_success=True,
        error_message=None,
        findings=sorted_findings,
        audit_errors=audit_errors,
        options=options,
    )
