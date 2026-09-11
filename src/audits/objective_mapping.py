"""Objective Mapping — foundation for linking findings to task objectives.

The Objective Mapping layer receives findings and task context, and
establishes which findings are relevant to which objectives. This module:

1. Defines the mapping data structures.
2. Returns no objective links until evidence-based matching exists.
3. Exposes an interface for future objective-aware logic.

Design principle: provider-neutral and honest. The system must not hardcode
Adobe-specific or any specific business objectives, and it must NOT claim a
finding affects an objective unless that relationship has been established.
Until real evidence-based mapping exists, affected_objectives is always empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.audits.models import Finding


@dataclass(frozen=True)
class ObjectiveMapping:
    """Maps a finding to the objectives it affects.

    Attributes:
        finding_id: The finding's rule identifier.
        source_audit: Name of the audit that produced the finding.
        affected_objectives: List of objective names this finding impacts.
            Always empty until evidence-based matching is implemented.
            An empty list means "no objective relationship has been established",
            not "no objective exists".
    """

    finding_id: str
    source_audit: str
    affected_objectives: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """Serialize this mapping to a plain dictionary."""
        return {
            "finding_id": self.finding_id,
            "source_audit": self.source_audit,
            "affected_objectives": list(self.affected_objectives),
        }


def map_findings_to_objectives(
    findings: list[Finding],
    target_objectives: Optional[list[str]] = None,
) -> list[ObjectiveMapping]:
    """Map findings to their affected objectives.

    Default implementation: produces a mapping for every finding with an
    EMPTY affected_objectives list. No semantic relevance is claimed until
    evidence-based matching exists. The target_objectives argument is
    accepted for interface stability and future use; it is not yet consulted.

    Args:
        findings: Aggregated findings with source_audit tags.
        target_objectives: Named objectives from AuditContext. Reserved for
            future evidence-based matching; not yet used to assign links.

    Returns:
        List of ObjectiveMapping objects, one per finding, each with an
        empty affected_objectives list.
    """
    del target_objectives  # reserved for evidence-based matching

    mappings: list[ObjectiveMapping] = []
    for f in findings:
        mappings.append(
            ObjectiveMapping(
                finding_id=f.id,
                source_audit=f.source_audit,
            )
        )
    return mappings
