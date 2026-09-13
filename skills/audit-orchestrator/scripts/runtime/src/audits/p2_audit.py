"""P2 Evidence Audit – aggregates multi‑page evidence and produces Findings.

This module implements a single audit function that can be invoked directly by P3
or any other component that already has a collection of ``PageObservation``
objects.  It deliberately does **not** modify the existing marketplace
orchestrator – P2 is offered as an optional, independent entry point.

The audit builds a :class:`SiteEvidenceInventory` from the observations, then
examines each aggregated fact.  Only a genuine value contradiction
(``cross_page_status == CONFLICT``) produces a :class:`Finding`.  Evidence
states such as ``AMBIGUOUS``, ``INSUFFICIENT_EVIDENCE``, and the temporal
classifications (``HISTORICAL``, ``EXPIRED``, ``FUTURE``) describe uncertainty
about a signal — they are never converted into defects.  Findings retain full
provenance via the ``evidence`` field, which contains a concise description of
the conflicting sources.

The implementation re‑uses the data‑models defined in ``src.audits.models``
and the inventory utilities in ``src.audits.fact_inventory``.
"""

from __future__ import annotations

from typing import Any, List

from src.audits.fact_inventory import SiteEvidenceInventory, AggregatedFact
from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# ---------------------------------------------------------------------------
# Helper: convert an AggregatedFact with a problem status into a Finding.
# ---------------------------------------------------------------------------

def _fact_to_finding(fact: AggregatedFact) -> Finding | None:
    """Create a :class:`Finding` only for a genuine evidence contradiction.

    A fact is a defect only when its ``cross_page_status`` is ``CONFLICT``,
    meaning multiple distinct concrete values were observed for the same
    (entity, attribute) in the current timeframe.

    ``AMBIGUOUS`` (values could not be confidently compared),
    ``INSUFFICIENT_EVIDENCE`` (too few sources), and the temporal states
    ``HISTORICAL`` / ``EXPIRED`` / ``FUTURE`` describe uncertainty about the
    evidence rather than a violation; reporting them as findings would turn
    missing or uncomparable signals into defects. ``MATCH`` and ``SINGLE_PAGE``
    are likewise non-issues.
    """
    status = fact.cross_page_status
    if status != "CONFLICT":
        return None

    # Map status to a short ID prefix – these are stable across runs.
    status_id_map = {
        "CONFLICT": "EVID-001",
        "HISTORICAL": "EVID-002",
        "FUTURE": "EVID-003",
        "EXPIRED": "EVID-004",
        "AMBIGUOUS": "EVID-005",
        "INSUFFICIENT_EVIDENCE": "EVID-006",
    }
    finding_id = status_id_map.get(status, "EVID-000")

    # Build a concise evidence description.
    values = {str(u.value) for u in fact.units if u.value is not None}
    sources = {u.source for u in fact.units if u.source}
    evidence = (
        f"Observed {len(sources)} source(s) with values {sorted(values)} for "
        f"entity '{fact.entity}' attribute '{fact.attribute}'."
    )

    # Suggested action – generic but useful.
    suggested = SuggestedAction(
        summary=f"Review and resolve the {status.lower()} for {fact.attribute}.",
        priority=2,
    )

    return Finding(
        id=finding_id,
        title=f"{status.title()} detected for {fact.attribute}",
        severity="high" if status == "CONFLICT" else "medium",
        evidence=evidence,
        suggested_action=suggested,
        priority=2,
        confidence="high",
        details={
            "entity": fact.entity,
            "attribute": fact.attribute,
            "cross_page_status": status,
            "sources": list(sorted(sources)),
            "values": list(sorted(values)),
        },
    )

# ---------------------------------------------------------------------------
# Public audit function – entry point for P2.
# ---------------------------------------------------------------------------

def audit_p2_evidence(observations: List[PageObservation]) -> List[Finding]:
    """Run the full P2 evidence analysis on a collection of pages.

    Parameters
    ----------
    observations:
        A list of ``PageObservation`` objects that have already passed the
        observation layer.  Items that are ``None`` or have ``is_success``
        ``False`` are ignored – this mirrors the robust behaviour required by
        the specification.

    Returns
    -------
    List[Finding]
        Findings for genuine cross-page value contradictions only. Ambiguous,
        insufficient, unknown, and temporally-differentiated evidence never
        produces a finding — uncertainty is not a defect.
    """
    # Build a site‑wide inventory, safely handling empty or malformed input.
    inventory = SiteEvidenceInventory.from_page_observations(observations)

    findings: List[Finding] = []
    for fact in inventory.facts_across_pages:
        f = _fact_to_finding(fact)
        if f is not None:
            findings.append(f)

    # Additional diagnostics – entity identity analysis could be added here in
    # the future without changing the public contract.
    return findings

__all__ = ["audit_p2_evidence"]
