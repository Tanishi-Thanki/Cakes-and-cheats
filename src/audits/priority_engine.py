"""Priority Engine — interface and signal collection foundation.

The Priority Engine receives structured signals from the orchestrator
and computes final priority ordering. This module:

1. Defines the signal dataclass that the orchestrator collects.
2. Provides a conservative default implementation that orders by
   audit-supplied priority and severity only.
3. Exposes an interface for future sophisticated priority logic.

Design principle: the orchestrator collects signals; the Priority Engine
scores. The orchestrator does NOT implement priority logic, and the engine
does NOT invent business-impact numbers — it only ranks the values the
audits already supplied.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.audits.models import SEVERITY_ORDER, Finding


@dataclass(frozen=True)
class PrioritySignal:
    """Structured signal collected by the orchestrator for the Priority Engine.

    Every field originates from deterministic Finding values only; no scores
    are fabricated at collection time.

    Attributes:
        finding_id: The finding's rule identifier.
        finding_title: Short title of the finding.
        severity: Severity level from the audit.
        confidence: Confidence level from the audit.
        priority: Audit-supplied priority value (1=highest).
        source_audit: Name of the audit that produced this finding.
        affected_url: URL where the finding was observed.
    """

    finding_id: str
    finding_title: str
    severity: str
    confidence: str
    priority: int
    source_audit: str
    affected_url: str

    def as_dict(self) -> dict[str, object]:
        """Serialize this signal to a plain dictionary."""
        return {
            "finding_id": self.finding_id,
            "finding_title": self.finding_title,
            "severity": self.severity,
            "confidence": self.confidence,
            "priority": self.priority,
            "source_audit": self.source_audit,
            "affected_url": self.affected_url,
        }


def collect_signals(
    findings: list[Finding],
    affected_url: str = "",
) -> list[PrioritySignal]:
    """Collect PrioritySignals from a list of tagged findings.

    This is called by the orchestrator after aggregation to prepare
    signals for the Priority Engine.

    Args:
        findings: Deduplicated, sorted findings with source_audit tags.
        affected_url: The final URL that was audited.

    Returns:
        List of PrioritySignal objects, one per finding.
    """
    signals: list[PrioritySignal] = []
    for f in findings:
        signals.append(
            PrioritySignal(
                finding_id=f.id,
                finding_title=f.title,
                severity=f.severity,
                confidence=f.confidence,
                priority=f.priority,
                source_audit=f.source_audit,
                affected_url=affected_url,
            )
        )
    return signals


def compute_priorities(
    signals: list[PrioritySignal],
) -> list[PrioritySignal]:
    """Compute final priority ordering for each signal.

    Conservative default: orders by (priority ASC, severity DESC) using only
    the values audits supplied. No weighted scoring, no invented numbers.

    Args:
        signals: Collected priority signals.

    Returns:
        Signals ordered by descending priority (lowest number first).
    """
    return sorted(
        signals,
        key=lambda s: (s.priority, SEVERITY_ORDER.get(s.severity, 99)),
    )


class PriorityEngine:
    """Interface and default implementation for priority computation.

    The Priority Engine receives signals from the orchestrator and
    computes final priority ordering. This default implementation
    preserves audit-supplied values. Subclass or replace for
    sophisticated priority logic.

    Usage:
        engine = PriorityEngine()
        sorted_signals = engine.rank(signals)
    """

    def collect(self, findings: list[Finding], affected_url: str = "") -> list[PrioritySignal]:
        """Collect priority signals from tagged findings.

        Args:
            findings: Aggregated findings with source_audit tags.
            affected_url: The final URL that was audited.

        Returns:
            List of PrioritySignal objects.
        """
        return collect_signals(findings, affected_url)

    def rank(self, signals: list[PrioritySignal]) -> list[PrioritySignal]:
        """Compute final priority ordering for signals.

        Default: preserves audit-supplied priority/severity ordering.

        Args:
            signals: Collected priority signals.

        Returns:
            Signals ordered by computed priority.
        """
        return compute_priorities(signals)
