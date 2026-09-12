"""Priority Engine — deterministic task-aware signal collection and ranking.

The Priority Engine receives structured signals from the orchestrator and objective
mapping, and computes final priority ordering and transparent rationale.

Design principles:
- The orchestrator collects signals; the Priority Engine scores and ranks.
- Deterministic ranking without arbitrary decimal math or fabricated weights:
  1. task relevance (direct_blocker -> task_impairing -> general)
  2. audit-supplied priority (1 -> 2 -> 3)
  3. severity (critical -> high -> medium -> low)
  4. confidence (high -> medium -> low)
  5. finding id (stable alphabetical tie-break)
- Transparent evidence-based priority_reason explaining why each finding was ordered.
- Backward-compatible fallback when no task context or objective mapping exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.audits.models import SEVERITY_ORDER, Finding
from src.audits.objective_mapping import ObjectiveMapping

# Deterministic ordering for task relevance (lower = higher priority)
RELEVANCE_ORDER: dict[str, int] = {
    "direct_blocker": 0,
    "task_impairing": 1,
    "general": 2,
}

# Deterministic ordering for confidence tie-breaks (lower = higher confidence)
CONFIDENCE_ORDER: dict[str, int] = {
    "high": 0,
    "medium": 1,
    "low": 2,
}


@dataclass(frozen=True)
class PrioritySignal:
    """Structured signal collected by the orchestrator for the Priority Engine.

    Every field originates from deterministic Finding values and ObjectiveMapping;
    no arbitrary scores are fabricated.

    Attributes:
        finding_id: The finding's rule identifier.
        finding_title: Short title of the finding.
        severity: Severity level from the audit.
        confidence: Confidence level from the audit.
        priority: Audit-supplied priority value (1=highest).
        source_audit: Name of the audit that produced this finding.
        affected_url: URL where the finding was observed.
        task_relevance: Relevance tier ('direct_blocker', 'task_impairing', 'general').
        priority_reason: Transparent explanation for priority ranking.
        affected_objectives: List of affected objective names.
    """

    finding_id: str
    finding_title: str
    severity: str
    confidence: str
    priority: int
    source_audit: str
    affected_url: str
    task_relevance: str = "general"
    priority_reason: str = ""
    affected_objectives: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Serialize this signal to a plain dictionary."""
        d: dict[str, object] = {
            "finding_id": self.finding_id,
            "finding_title": self.finding_title,
            "severity": self.severity,
            "confidence": self.confidence,
            "priority": self.priority,
            "source_audit": self.source_audit,
            "affected_url": self.affected_url,
        }
        if self.task_relevance != "general" or self.priority_reason:
            d["task_relevance"] = self.task_relevance
            d["priority_reason"] = self.priority_reason
        if self.affected_objectives:
            d["affected_objectives"] = list(self.affected_objectives)
        return d


def _generate_priority_reason(
    finding_title: str,
    severity: str,
    task_relevance: str,
    affected_objectives: list[str],
    mapping_reason: str,
) -> str:
    """Generate transparent, deterministic reason string."""
    if mapping_reason:
        return mapping_reason
    if task_relevance == "direct_blocker":
        objs = ", ".join(affected_objectives) if affected_objectives else "requested task"
        return f"Direct blocker for {objs}."
    if task_relevance == "task_impairing":
        objs = ", ".join(affected_objectives) if affected_objectives else "requested task"
        return f"Impairs {objs} due to {finding_title.lower()}."
    return "General site issue; no requested task/objective relationship was established."


def collect_signals(
    findings: list[Finding],
    affected_url: str = "",
    objective_mappings: Optional[list[ObjectiveMapping]] = None,
) -> list[PrioritySignal]:
    """Collect PrioritySignals from a list of tagged findings and objective mappings.

    Args:
        findings: Deduplicated, sorted findings with source_audit tags.
        affected_url: The final URL that was audited.
        objective_mappings: Optional objective mappings linking findings to task context.

    Returns:
        List of PrioritySignal objects, one per finding.
    """
    mapping_by_id: dict[str, ObjectiveMapping] = {}
    if objective_mappings:
        for m in objective_mappings:
            if m.finding_id not in mapping_by_id:
                mapping_by_id[m.finding_id] = m

    signals: list[PrioritySignal] = []
    for f in findings:
        mapping = mapping_by_id.get(f.id)
        task_relevance = mapping.task_relevance if mapping else "general"
        affected_objectives = list(mapping.affected_objectives) if mapping else []
        relevance_reason = mapping.relevance_reason if mapping else ""

        priority_reason = _generate_priority_reason(
            finding_title=f.title,
            severity=f.severity,
            task_relevance=task_relevance,
            affected_objectives=affected_objectives,
            mapping_reason=relevance_reason,
        )

        signals.append(
            PrioritySignal(
                finding_id=f.id,
                finding_title=f.title,
                severity=f.severity,
                confidence=f.confidence,
                priority=f.priority,
                source_audit=f.source_audit,
                affected_url=affected_url,
                task_relevance=task_relevance,
                priority_reason=priority_reason,
                affected_objectives=affected_objectives,
            )
        )
    return signals


def compute_priorities(
    signals: list[PrioritySignal],
) -> list[PrioritySignal]:
    """Compute final priority ordering for each signal.

    Deterministic ranking criteria:
    1. task_relevance (direct_blocker -> task_impairing -> general)
    2. priority (1 -> 2 -> 3)
    3. severity (critical -> high -> medium -> low)
    4. confidence (high -> medium -> low)
    5. finding_id (alphabetical tie-break)

    Args:
        signals: Collected priority signals.

    Returns:
        Signals ordered by descending priority (highest priority first).
    """
    return sorted(
        signals,
        key=lambda s: (
            RELEVANCE_ORDER.get(s.task_relevance, 2),
            s.priority,
            SEVERITY_ORDER.get(s.severity, 99),
            CONFIDENCE_ORDER.get(s.confidence, 99),
            s.finding_id,
        ),
    )


class PriorityEngine:
    """Interface and implementation for priority computation.

    The Priority Engine receives signals from the orchestrator and
    computes final priority ordering.
    """

    def collect(
        self,
        findings: list[Finding],
        affected_url: str = "",
        objective_mappings: Optional[list[ObjectiveMapping]] = None,
    ) -> list[PrioritySignal]:
        """Collect priority signals from tagged findings and objective mappings."""
        return collect_signals(findings, affected_url, objective_mappings=objective_mappings)

    def rank(self, signals: list[PrioritySignal]) -> list[PrioritySignal]:
        """Compute final priority ordering for signals."""
        return compute_priorities(signals)
