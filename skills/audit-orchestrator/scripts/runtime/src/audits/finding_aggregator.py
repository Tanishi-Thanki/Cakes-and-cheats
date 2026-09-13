"""Finding Aggregation and Deduplication layer.

Collects findings from multiple audits, tags them with source information,
and performs conservative deduplication. The aggregator is designed so that:

- All useful evidence is preserved.
- Finding IDs, severity, confidence, and suggested actions survive.
- Obvious duplicates (same id + same evidence) are removed.
- Distinct findings are never accidentally merged.
- Source audit attribution is tracked for downstream reporting.

Identity signals for deduplication:
    (finding.id, normalized_evidence)

where normalized_evidence collapses internal whitespace runs on the FULL
evidence string. There is no truncation, fuzzy matching, or LLM involvement:
two findings collapse only when their rule id is identical AND their evidence
is identical up to whitespace formatting.
"""

from __future__ import annotations

from src.audits.finding_ids import disambiguate_finding_ids
from src.audits.models import SEVERITY_ORDER, Finding


def tag_findings(
    findings: list[Finding],
    source_audit: str,
) -> list[Finding]:
    """Return a new list where each finding carries source_audit attribution.

    Finding objects are frozen dataclasses, so this creates new instances
    rather than mutating the originals.

    Args:
        findings: Findings produced by a single audit.
        source_audit: Stable name of the audit that produced these findings.

    Returns:
        New list of Finding objects with source_audit set.
    """
    tagged: list[Finding] = []
    for f in findings:
        tagged.append(
            Finding(
                id=f.id,
                title=f.title,
                severity=f.severity,
                evidence=f.evidence,
                suggested_action=f.suggested_action,
                priority=f.priority,
                confidence=f.confidence,
                details=dict(f.details) if f.details else {},
                source_audit=source_audit,
            )
        )
    return tagged


def _evidence_fingerprint(evidence: str) -> str:
    """Normalise evidence into a stable, conservative fingerprint.

    Collapses every whitespace run (spaces, tabs, newlines) to a single
    space and strips edges. This tolerates pure formatting differences while
    preserving all meaningful content — no truncation, no fuzzy matching.
    """
    return " ".join(evidence.split())


def _dedup_key(finding: Finding) -> tuple[str, str]:
    """Compute the deduplication fingerprint for a finding.

    Key: (finding.id, normalized_evidence)

    Rationale:
    - Keying on id alone would merge two distinct findings from the same rule
      triggered by different evidence (e.g. two broken Add-to-Cart links).
    - Normalising the FULL evidence string collapses formatting-only
      differences (line breaks, indentation, trailing whitespace) while still
      treating substantively different evidence as distinct.
    - source_audit is intentionally NOT part of the key so that the same
      issue reported by multiple audits is collapsed to one finding.
    """
    return (finding.id, _evidence_fingerprint(finding.evidence))


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Remove duplicate findings while preserving first-occurrence order.

    Deduplication is conservative: only findings with both the same id AND
    evidence that is identical up to whitespace formatting are considered
    duplicates.

    Findings with the same id but different evidence are NOT merged.
    Findings from different audits with different ids are NOT merged.

    Args:
        findings: Combined findings from all audits.

    Returns:
        Deduplicated list preserving first-occurrence order.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[Finding] = []
    for finding in findings:
        key = _dedup_key(finding)
        if key not in seen:
            seen.add(key)
            deduped.append(finding)
    return deduped


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Sort findings by (priority ASC, severity DESC) using audit-supplied values.

    The aggregator does not invent, modify, or reinterpret severity or priority.
    It only orders findings for presentation convenience.

    Args:
        findings: Deduplicated findings.

    Returns:
        Sorted list of findings.
    """
    return sorted(
        findings,
        key=lambda f: (f.priority, SEVERITY_ORDER.get(f.severity, 99)),
    )


def aggregate_findings(
    all_findings: list[Finding],
) -> list[Finding]:
    """Aggregate findings from multiple audits: deduplicate, harden IDs, sort.

    This is the main entry point for the aggregation layer.

    Deduplication is conservative (existing behaviour): findings collapse only
    when rule id and canonical evidence match. After deduplication, finding IDs
    are hardened by :func:`disambiguate_finding_ids` so that the same base rule
    ID never repeats within one report (P3-4). Single-instance rule IDs pass
    through unchanged; colliding instances receive a deterministic suffix
    derived from their own semantic evidence.

    Args:
        all_findings: Combined, unprocessed findings from all audits.
            Findings should already carry source_audit tags.

    Returns:
        Deduplicated, ID-disambiguated, sorted list of findings.
    """
    deduped = deduplicate_findings(all_findings)
    hardened = disambiguate_finding_ids(deduped)
    return sort_findings(hardened)
