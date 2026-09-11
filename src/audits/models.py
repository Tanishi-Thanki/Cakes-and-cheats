"""Data models for audit findings and remediation actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SeverityLevel = Literal["critical", "high", "medium", "low"]
ConfidenceLevel = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class SuggestedAction:
    """Actionable recommendation associated with an audit finding.

    Attributes:
        summary: Clear, practical remediation advice.
        priority: Deterministic numeric priority (1 = highest, 2 = medium, 3 = lower).
    """

    summary: str
    priority: int


@dataclass(frozen=True)
class Finding:
    """Structured audit finding representing an evidence-backed issue.

    Attributes:
        id: Unique rule identifier for this finding (e.g., 'MA-IMG-001').
        title: Short descriptive title of the finding.
        severity: Severity level ('critical', 'high', 'medium', 'low').
        evidence: Concrete evidence string detailing observed facts and mechanism.
        suggested_action: Actionable recommendation with summary and priority.
        priority: Priority ranking (1 = highest, 2 = medium, 3 = lower).
        confidence: Confidence level in the finding ('high', 'medium', 'low').
        details: Optional dictionary for additional structured diagnostic data.
    """

    id: str
    title: str
    severity: SeverityLevel
    evidence: str
    suggested_action: SuggestedAction
    priority: int
    confidence: ConfidenceLevel = "medium"
    details: dict[str, Any] = field(default_factory=dict)
