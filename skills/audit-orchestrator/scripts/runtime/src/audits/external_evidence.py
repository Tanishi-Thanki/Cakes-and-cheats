"""Provider-Neutral External Evidence Abstraction.

Defines the interfaces and data models for optionally consuming externally
discovered public evidence. No concrete provider is implemented here.

Design principles:
- OPTIONAL: The entire audit works with no provider configured.
- BEST EFFORT: All external failures degrade gracefully (never crash).
- PROVENANCE: Every ExternalFact retains source URL, domain, extraction method,
  raw value, normalized value, field, timestamp, confidence, and source type.
- SOURCE INDEPENDENCE: source_domain is tracked explicitly. Multiple URLs from
  the same domain count as ONE corroborating domain.
- BUDGET: Callers supply an ExternalEvidenceBudget. The provider honours it and
  stops when the budget is exhausted.
- NO HIDDEN NETWORK: Core evidence/consistency code must remain deterministic.
  Network access happens only through an explicit ExternalEvidenceProvider.

Classification semantics:
  Missing provider/result → INSUFFICIENT_EVIDENCE
  NOT → "entity does not exist"
  NOT → "website is bad"
  NOT → "not discoverable"
"""

from __future__ import annotations

import time
import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

ExtractionMethod = Literal["search", "fetch", "extract", "structured_data", "unknown"]

ExternalSourceType = Literal["external_search", "external_page", "external_structured"]

ExternalConfidence = Literal["high", "medium", "low"]

ExternalFetchStatus = Literal[
    "ok",
    "empty",
    "timeout",
    "network_error",
    "provider_unavailable",
    "malformed_response",
    "http_error",
    "parse_failure",
    "budget_exhausted",
    "provider_not_configured",
]


# ---------------------------------------------------------------------------
# ExternalFact — the atomic unit of external evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalFact:
    """Immutable external evidence unit retaining full provenance.

    Attributes:
        field: The attribute/field this fact represents (e.g. 'price', 'phone',
            'location', 'description', 'category').
        value: Normalized comparable value (string, tuple, or None).
        raw_value: Exact raw string as returned by the external source.
        source_url: The specific URL this fact was extracted from.
        source_domain: Effective registered domain (e.g. 'yelp.com'), extracted
            automatically from source_url if not supplied explicitly.
        extraction_method: How this fact was obtained.
        source_type: Broad source category.
        confidence: Confidence in this fact's accuracy.
        timestamp: ISO date string or year if the fact carries temporal context.
        entity: Entity name this fact relates to (optional, for traceability).
        details: Additional structured diagnostic data (provider name, query used,
            HTTP status, etc.).
    """

    field: str
    value: Any
    raw_value: str
    source_url: str
    source_domain: str
    extraction_method: ExtractionMethod = "unknown"
    source_type: ExternalSourceType = "external_page"
    confidence: ExternalConfidence = "medium"
    timestamp: Optional[str] = None
    entity: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        field: str,
        value: Any,
        raw_value: str,
        source_url: str,
        *,
        extraction_method: ExtractionMethod = "unknown",
        source_type: ExternalSourceType = "external_page",
        confidence: ExternalConfidence = "medium",
        timestamp: Optional[str] = None,
        entity: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
        source_domain: Optional[str] = None,
    ) -> "ExternalFact":
        """Convenience constructor that auto-derives source_domain from source_url."""
        if not source_domain:
            source_domain = _extract_domain(source_url)
        return cls(
            field=field,
            value=value,
            raw_value=raw_value,
            source_url=source_url,
            source_domain=source_domain,
            extraction_method=extraction_method,
            source_type=source_type,
            confidence=confidence,
            timestamp=timestamp,
            entity=entity,
            details=details or {},
        )


# ---------------------------------------------------------------------------
# ExternalEvidence — the result of one external evidence query
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalEvidence:
    """Result of a single external evidence fetch operation.

    Contains all ExternalFact instances returned, plus provenance metadata
    about the query itself (status, elapsed time, errors).

    Attributes:
        query: The query string or entity name that was searched.
        status: Outcome of the fetch operation.
        facts: Ordered list of extracted external facts (empty on failure).
        provider_name: Identifier for the provider that returned this evidence.
        elapsed_seconds: Wall-clock time consumed by the provider call.
        error_detail: Human-readable error detail on non-ok status.
        unique_domains: Distinct source domains present in facts.
            Multiple URLs from the same domain count as ONE domain here.
    """

    query: str
    status: ExternalFetchStatus
    facts: tuple[ExternalFact, ...]
    provider_name: str
    elapsed_seconds: float = 0.0
    error_detail: Optional[str] = None

    @property
    def is_ok(self) -> bool:
        """True if the fetch succeeded and facts are available."""
        return self.status == "ok"

    @property
    def unique_domains(self) -> frozenset[str]:
        """Distinct source domains present in facts.

        Per the source-independence requirement: multiple URLs from the same
        domain count as a single corroborating domain.
        """
        return frozenset(f.source_domain for f in self.facts if f.source_domain)

    @property
    def corroborating_domain_count(self) -> int:
        """Number of distinct external domains contributing facts."""
        return len(self.unique_domains)

    @property
    def is_empty(self) -> bool:
        return len(self.facts) == 0

    @classmethod
    def insufficient(cls, query: str, provider_name: str) -> "ExternalEvidence":
        """Return a canonical INSUFFICIENT_EVIDENCE result."""
        return cls(
            query=query,
            status="empty",
            facts=(),
            provider_name=provider_name,
            error_detail="No external evidence found.",
        )

    @classmethod
    def not_configured(cls, query: str) -> "ExternalEvidence":
        """Return a canonical result when no provider is configured."""
        return cls(
            query=query,
            status="provider_not_configured",
            facts=(),
            provider_name="none",
            error_detail="No external evidence provider configured.",
        )

    @classmethod
    def from_error(
        cls,
        query: str,
        provider_name: str,
        status: ExternalFetchStatus,
        detail: str,
        elapsed: float = 0.0,
    ) -> "ExternalEvidence":
        """Return a failed result with the given status and detail string."""
        return cls(
            query=query,
            status=status,
            facts=(),
            provider_name=provider_name,
            elapsed_seconds=elapsed,
            error_detail=detail,
        )


# ---------------------------------------------------------------------------
# ExternalEvidenceBudget — explicit bounded controls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExternalEvidenceBudget:
    """Explicit budget controlling all external evidence work.

    Attributes:
        max_queries: Maximum number of external queries allowed per audit run.
        max_results_per_query: Maximum number of facts/results to accept per query.
        timeout_seconds: Per-query network/provider timeout.
        max_retries: Maximum retry attempts on transient failures.
        max_response_bytes: Maximum accepted response body size (bytes).
        total_time_budget_seconds: Hard ceiling on total elapsed time across
            all provider calls for one audit run. When exhausted, further
            queries return budget_exhausted status without calling the provider.
    """

    max_queries: int = 5
    max_results_per_query: int = 10
    timeout_seconds: float = 8.0
    max_retries: int = 1
    max_response_bytes: int = 512 * 1024  # 512 KB
    total_time_budget_seconds: float = 30.0

    def is_valid(self) -> bool:
        return (
            self.max_queries >= 0
            and self.max_results_per_query >= 0
            and self.timeout_seconds > 0
            and self.max_retries >= 0
            and self.max_response_bytes > 0
            and self.total_time_budget_seconds > 0
        )


# ---------------------------------------------------------------------------
# BudgetTracker — mutable runtime state (not frozen)
# ---------------------------------------------------------------------------


class BudgetTracker:
    """Runtime mutable tracker for a single audit run's budget consumption.

    Passed to fetch_external_evidence() so providers can check and update
    remaining budget without callers having to manage this manually.
    """

    def __init__(self, budget: ExternalEvidenceBudget) -> None:
        self._budget = budget
        self._queries_used: int = 0
        self._elapsed_seconds: float = 0.0

    @property
    def queries_remaining(self) -> int:
        return max(0, self._budget.max_queries - self._queries_used)

    @property
    def time_remaining(self) -> float:
        return max(0.0, self._budget.total_time_budget_seconds - self._elapsed_seconds)

    @property
    def is_exhausted(self) -> bool:
        return self._queries_used >= self._budget.max_queries or self._elapsed_seconds >= self._budget.total_time_budget_seconds

    def record_query(self, elapsed: float) -> None:
        """Record that one query was completed, consuming elapsed seconds."""
        self._queries_used += 1
        self._elapsed_seconds += elapsed

    def snapshot(self) -> dict[str, Any]:
        """Return a diagnostic snapshot for provenance/details."""
        return {
            "queries_used": self._queries_used,
            "queries_remaining": self.queries_remaining,
            "elapsed_seconds": round(self._elapsed_seconds, 3),
            "time_remaining": round(self.time_remaining, 3),
        }


# ---------------------------------------------------------------------------
# ExternalEvidenceProvider — the abstract base class
# ---------------------------------------------------------------------------


class ExternalEvidenceProvider(ABC):
    """Abstract base class for all external evidence providers.

    Concrete implementations plug in specific sources (search engines, APIs,
    public directories, etc.) without touching any core audit logic.

    Invariants every implementation must respect:
    - Never crash: all network/parsing errors must be caught and returned as
      an ExternalEvidence with an appropriate non-ok status.
    - Honour the budget: stop and return budget_exhausted when the
      BudgetTracker is exhausted or per-query limits are exceeded.
    - Retain provenance: every returned ExternalFact must carry source_url,
      source_domain, extraction_method, raw_value, and confidence.
    - Do not mutate the BudgetTracker except through record_query().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'yelp', 'google_search')."""

    @abstractmethod
    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        """Perform an external evidence lookup for the given query.

        Args:
            query: The entity name or query string to look up.
            budget: The budget constraints for this call.
            tracker: Runtime budget tracker. Provider must call
                tracker.record_query(elapsed) once per attempt and must
                check tracker.is_exhausted before starting any network work.
            entity: Optional entity name hint (may differ from query).
            fields: Optional list of specific fields wanted (e.g. ['phone',
                'location']). Provider may ignore unsupported fields.

        Returns:
            ExternalEvidence with status reflecting the outcome. Never raises.
        """


# ---------------------------------------------------------------------------
# ExternalEvidenceClient — the optional gateway used by callers
# ---------------------------------------------------------------------------


class ExternalEvidenceClient:
    """Thin optional gateway that wraps zero or one ExternalEvidenceProvider.

    Callers use this class instead of calling providers directly. When no
    provider is configured, all calls return ExternalEvidence.not_configured().

    This is the only place network activity may originate from the evidence
    layer. Passing None for provider disables external evidence entirely while
    keeping all audit logic intact.
    """

    def __init__(
        self,
        provider: Optional[ExternalEvidenceProvider] = None,
        budget: Optional[ExternalEvidenceBudget] = None,
    ) -> None:
        self._provider = provider
        self._budget = budget or ExternalEvidenceBudget()
        self._tracker = BudgetTracker(self._budget)

    @property
    def has_provider(self) -> bool:
        return self._provider is not None

    @property
    def provider_name(self) -> str:
        return self._provider.name if self._provider else "none"

    @property
    def budget(self) -> ExternalEvidenceBudget:
        return self._budget

    @property
    def tracker(self) -> BudgetTracker:
        return self._tracker

    def fetch_evidence(
        self,
        query: str,
        *,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        """Fetch external evidence for query via the configured provider.

        Returns ExternalEvidence.not_configured() if no provider is set.
        Returns a budget_exhausted result if the budget is spent.
        Never raises — all errors are captured in the returned object.
        """
        if self._provider is None:
            return ExternalEvidence.not_configured(query)

        if self._tracker.is_exhausted:
            return ExternalEvidence.from_error(
                query=query,
                provider_name=self._provider.name,
                status="budget_exhausted",
                detail=(
                    f"Budget exhausted before query could be issued. "
                    f"Budget snapshot: {self._tracker.snapshot()}"
                ),
            )

        start = time.monotonic()
        try:
            result = self._provider.fetch_evidence(
                query,
                budget=self._budget,
                tracker=self._tracker,
                entity=entity,
                fields=fields,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = time.monotonic() - start
            self._tracker.record_query(elapsed)
            result = ExternalEvidence.from_error(
                query=query,
                provider_name=self._provider.name,
                status="provider_unavailable",
                detail=f"Provider raised an unexpected exception: {exc}",
                elapsed=elapsed,
            )
        return result

    def budget_snapshot(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of current budget consumption."""
        return self._tracker.snapshot()


# ---------------------------------------------------------------------------
# EvidenceUnit bridge — convert ExternalFact → EvidenceUnit
# ---------------------------------------------------------------------------


def external_fact_to_evidence_unit(
    fact: ExternalFact,
    *,
    entity: Optional[str] = None,
) -> "Any":
    """Convert an ExternalFact into an EvidenceUnit for integration with FactInventory.

    This is the only bridge between the external evidence layer and the core
    evidence consistency layer. It preserves all provenance fields and always
    marks source_classification='EXTERNAL'.

    Import is deferred to avoid circular dependencies at module load time.
    """
    # Late import to prevent circular dependency
    from src.audits.fact_inventory import EvidenceUnit  # noqa: PLC0415

    ent = entity or fact.entity or "External Entity"
    return EvidenceUnit(
        entity=ent,
        attribute=fact.field,
        value=fact.value,
        raw_value=fact.raw_value,
        source=fact.source_url,
        source_type="metadata",  # closest neutral source_type for external data
        confidence=fact.confidence,
        timestamp=fact.timestamp,
        source_classification="EXTERNAL",
        details={
            "source_domain": fact.source_domain,
            "extraction_method": fact.extraction_method,
            "provider": fact.details.get("provider", "unknown"),
            **fact.details,
        },
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_domain(url: str) -> str:
    """Extract the effective domain from a URL (strips www. prefix)."""
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc or parsed.path
        host = host.lower().lstrip("www.") if host.startswith("www.") else host.lower()
        return host.split(":")[0]  # strip port if present
    except Exception:  # noqa: BLE001
        return ""
