"""Audit Report builder — structured output for downstream consumers.

Builds a final report from AuditResult + AuditRequest context. The report
preserves all data needed to answer:

- What was audited?
- What problems were found?
- Where were they found?
- What evidence supports them?
- How confident is the system?
- Which task/objective is affected?
- What should be done?

The report also carries the downstream stages that run after aggregation:
objective mappings and priority-ordered signals. Design principle: the
report is a composition of existing structured data, not a replacement for
any component.

The reporting layer exposes two serializations:

1. ``AuditReport.as_dict()`` — the rich, internal orchestration view
   (used by the orchestrator and existing pipelines). Kept unchanged.
2. ``AuditReport.as_final_dict()`` / ``report_to_json()`` — the lightweight
   final report schema delivered to consumers and the future
   audit-orchestrator Agent Skill:

   .. code-block:: json

       {
         "site": "...",
         "audited_at": "...",
         "summary": {"total_findings": 0, "high": 0, "medium": 0, "low": 0},
         "findings": [
           {
             "id": "...",
             "title": "...",
             "severity": "...",
             "evidence": "...",
             "suggested_action": {"summary": "...", "priority": 1}
           }
         ]
       }

Determinism guarantees:

- No LLM, no external API, no random identifiers, no database state.
- ``audited_at`` is never auto-generated; it is only included when the
  caller supplies it (e.g. for tests), so output is reproducible.
- ``report_to_json()`` sorts keys and passes no wall-clock data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from src.audits.models import Finding
from src.audits.objective_mapping import ObjectiveMapping
from src.audits.priority_engine import PrioritySignal


@dataclass(frozen=True)
class AuditReport:
    """Structured audit report produced by the report builder.

    Attributes:
        requested_url: The original URL from the audit request.
        final_url: The URL after redirect resolution.
        status_code: HTTP status code from the crawl.
        is_success: Whether the crawl and observation succeeded.
        error_message: Top-level error when is_success is False.
        findings: All deduplicated, sorted findings.
        audit_errors: Maps audit name to exception string for any
            audit that failed during execution.
        audits_run: List of audit names that were executed.
        task_intent: Task intent from AuditContext, if provided.
        task_description: Task description from AuditContext, if provided.
        target_objectives: Objectives from AuditContext, if provided.
        summary: High-level summary statistics.
        audited_at: Optional ISO-8601 timestamp supplied by the caller. Never
            auto-generated so serialization stays deterministic; tests inject
            a fixed value to make output reproducible.
        objective_mappings: Finding → objective mappings (one per finding).
            Empty affected_objectives until evidence-based mapping exists.
        priority_signals: Priority-ordered signals (one per finding) produced
            by the Priority Engine.
    """

    requested_url: str
    final_url: Optional[str]
    status_code: Optional[int]
    is_success: bool
    error_message: Optional[str]
    findings: list[Finding]
    audit_errors: dict[str, str]
    audits_run: list[str]
    task_intent: Optional[str] = None
    task_description: Optional[str] = None
    target_objectives: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    audited_at: Optional[str] = None
    objective_mappings: list[ObjectiveMapping] = field(default_factory=list)
    priority_signals: list[PrioritySignal] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the report to a plain dictionary.

        Returns:
            Dict suitable for JSON serialization.
        """
        has_task_context = bool(
            (self.task_intent and self.task_intent.strip())
            or (self.task_description and self.task_description.strip())
            or self.target_objectives
        )
        mapping_by_id = {m.finding_id: m for m in self.objective_mappings}
        signal_by_id = {s.finding_id: s for s in self.priority_signals}

        finding_dicts: list[dict[str, Any]] = []
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
            if getattr(f, "classification", "issue") != "issue":
                fd["classification"] = f.classification
            if f.details:
                fd["details"] = f.details
            if has_task_context:
                m = mapping_by_id.get(f.id)
                s = signal_by_id.get(f.id)
                fd["affected_objectives"] = list(m.affected_objectives) if m else []
                fd["task_relevance"] = m.task_relevance if m else "general"
                fd["priority_reason"] = s.priority_reason if (s and s.priority_reason) else (m.relevance_reason if m else "")
            finding_dicts.append(fd)

        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "is_success": self.is_success,
            "error_message": self.error_message,
            "findings": finding_dicts,
            "audit_errors": dict(self.audit_errors),
            "audits_run": list(self.audits_run),
            "task_intent": self.task_intent,
            "task_description": self.task_description,
            "target_objectives": list(self.target_objectives),
            "summary": dict(self.summary),
            "objective_mappings": [m.as_dict() for m in self.objective_mappings],
            "priority_signals": [s.as_dict() for s in self.priority_signals],
        }

    def as_final_dict(self) -> dict[str, Any]:
        """Serialize this report into the lightweight final report schema.

        This is the primary output format for consumers (JSON). It is a
        deterministic projection of existing structured data:

        - ``site``: the audited URL (final URL when available, else requested).
        - ``summary``: deterministic counts — ``total_findings`` plus per-severity
          counts for ``high``/``medium``/``low``. A ``critical`` count is added
          only when critical findings exist so severity is never invented.
        - ``task_context``: task intent, description, and target objectives when
          supplied in the audit request context.
        - ``findings``: orchestrator-ordered findings with evidence, suggested
          action, priority, confidence, source attribution, and when task context
          exists, affected_objectives, task_relevance, and priority_reason.
          Structured ``details`` are retained as structured data (never collapsed
          into an opaque string).
        - Failure state: when the pipeline failed, a concise structured
          ``failure`` block is emitted instead of a Python traceback.

        Returns:
            Plain dict suitable for JSON serialization.
        """
        summary: dict[str, Any] = {
            "total_findings": len(self.findings),
            "high": 0,
            "medium": 0,
            "low": 0,
        }
        observations_count = 0
        critical_count = 0
        for f in self.findings:
            if getattr(f, "classification", "issue") != "issue":
                observations_count += 1
            severity = getattr(f, "severity", "")
            if severity == "critical":
                critical_count += 1
            elif severity in summary:
                summary[severity] += 1
        if observations_count:
            summary["observations"] = observations_count
        if critical_count:
            summary["critical"] = critical_count

        has_task_context = bool(
            (self.task_intent and self.task_intent.strip())
            or (self.task_description and self.task_description.strip())
            or self.target_objectives
        )
        mapping_by_id = {m.finding_id: m for m in self.objective_mappings}
        signal_by_id = {s.finding_id: s for s in self.priority_signals}

        result: dict[str, Any] = {
            "site": self.final_url or self.requested_url,
            "audited_at": self.audited_at,
        }

        if has_task_context:
            result["task_context"] = {
                "task_intent": self.task_intent,
                "task_description": self.task_description,
                "target_objectives": list(self.target_objectives),
            }

        result["summary"] = summary
        result["audits_run"] = list(self.audits_run)
        result["findings"] = [
            _finding_to_dict(
                f,
                mapping=mapping_by_id.get(f.id),
                signal=signal_by_id.get(f.id),
                has_task_context=has_task_context,
            )
            for f in self.findings
        ]

        if not self.is_success:
            result["failure"] = {
                "url": self.requested_url,
                "status_code": self.status_code,
                "message": self.error_message,
            }
        if self.audit_errors:
            result["audit_errors"] = dict(self.audit_errors)

        return result

    def to_json(
        self,
        *,
        indent: int = 2,
        sort_keys: bool = True,
        ensure_ascii: bool = False,
    ) -> str:
        """Serialize this report to a deterministic JSON string.

        Determinism: sorted keys, no wall-clock data, no random identifiers.
        ``audited_at`` is included only when it was supplied to the report.

        Args:
            indent: JSON indentation level.
            sort_keys: Sort keys for stable, diffable output.
            ensure_ascii: When False, non-ASCII characters are preserved.

        Returns:
            JSON string of the final report.
        """
        return report_to_json(
            self,
            indent=indent,
            sort_keys=sort_keys,
            ensure_ascii=ensure_ascii,
        )


def _compute_summary(
    findings: list[Finding],
    audits_run: list[str],
    audit_errors: dict[str, str],
) -> dict[str, Any]:
    """Compute summary statistics for the report.

    Returns:
        Dict with total_findings, severity_counts, audit_status, etc.
    """
    severity_counts: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    observations_count = 0
    for f in findings:
        if f.severity in severity_counts:
            severity_counts[f.severity] += 1
        if getattr(f, "classification", "issue") != "issue":
            observations_count += 1

    audit_status: dict[str, str] = {}
    for name in audits_run:
        if name in audit_errors:
            audit_status[name] = "failed"
        else:
            audit_status[name] = "completed"

    return {
        "total_findings": len(findings),
        "observations": observations_count,
        "severity_counts": severity_counts,
        "audits_attempted": len(audits_run),
        "audits_succeeded": sum(1 for s in audit_status.values() if s == "completed"),
        "audits_failed": sum(1 for s in audit_status.values() if s == "failed"),
        "audit_status": audit_status,
    }


def _finding_to_dict(
    finding: Finding,
    mapping: Optional[ObjectiveMapping] = None,
    signal: Optional[PrioritySignal] = None,
    has_task_context: bool = False,
) -> dict[str, Any]:
    """Serialize one finding without loss, tolerating incomplete inputs.

    Preserves every field of the existing Finding contract: id, title,
    severity, evidence, suggested_action (summary + priority), priority,
    confidence, and source attribution. Structured ``details`` are kept as a
    structured dict when present.

    When ``has_task_context`` is True, exposes:
    - ``affected_objectives``: list of canonical objectives impacted.
    - ``task_relevance``: direct_blocker | task_impairing | general.
    - ``priority_reason``: transparent evidence-based priority rationale.

    Robustness (test: invalid/incomplete finding handling):
    - Missing/blank title falls back to the finding id rather than aborting.
    - Missing evidence serializes as an empty string (never fabricated).
    - An absent suggested_action serializes as ``None`` — no artificial
      recommendation is invented on the consumer's behalf.
    - Unknown severities are preserved verbatim in the finding but are never
      counted in the summary (severity is never invented).

    Args:
        finding: A Finding (or Finding-like object) to serialize.
        mapping: Optional ObjectiveMapping for task relevance and objectives.
        signal: Optional PrioritySignal for priority rationale.
        has_task_context: Whether task context was provided in the audit request.

    Returns:
        Plain dict with evidence and recommendations preserved.
    """
    finding_id = getattr(finding, "id", "") or ""
    title = getattr(finding, "title", "") or ""
    if not title:
        title = finding_id or "Untitled finding"

    suggested_action = getattr(finding, "suggested_action", None)
    if suggested_action is not None:
        suggested_action_dict: Optional[dict[str, Any]] = {
            "summary": getattr(suggested_action, "summary", "") or "",
            "priority": getattr(suggested_action, "priority", 3),
        }
    else:
        suggested_action_dict = None

    finding_dict: dict[str, Any] = {
        "id": finding_id,
        "title": title,
        "severity": getattr(finding, "severity", "") or "",
        "evidence": getattr(finding, "evidence", "") or "",
        "suggested_action": suggested_action_dict,
        "priority": getattr(finding, "priority", 3),
    }

    confidence = getattr(finding, "confidence", None)
    if confidence:
        finding_dict["confidence"] = confidence

    source_audit = getattr(finding, "source_audit", "") or ""
    if source_audit:
        finding_dict["source_audit"] = source_audit

    classification = getattr(finding, "classification", "issue") or "issue"
    if classification != "issue":
        finding_dict["classification"] = classification

    details = getattr(finding, "details", None)
    if details:
        finding_dict["details"] = _json_safe(details)

    if has_task_context:
        affected_objectives = list(mapping.affected_objectives) if mapping else []
        task_relevance = mapping.task_relevance if mapping else "general"
        priority_reason = (
            signal.priority_reason
            if (signal and signal.priority_reason)
            else (mapping.relevance_reason if mapping else "")
        )
        finding_dict["affected_objectives"] = affected_objectives
        finding_dict["task_relevance"] = task_relevance
        finding_dict["priority_reason"] = priority_reason

    return finding_dict


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into JSON-safe, deterministically ordered data.

    Sets are sorted (by their string form) so string hashing randomization can
    never change serialized output between runs. Unknown types degrade to their
    string representation rather than a non-deterministic repr.

    Args:
        value: Arbitrary Python value.

    Returns:
        JSON-serializable equivalent.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted((_json_safe(v) for v in value), key=repr)
    return str(value)


def report_to_json(
    report: AuditReport,
    *,
    indent: int = 2,
    sort_keys: bool = True,
    ensure_ascii: bool = False,
) -> str:
    """Serialize an AuditReport to a deterministic JSON string.

    This is the JSON serialization entry point for the reporting layer and the
    primary format the audit-orchestrator Agent Skill will consume.

    Args:
        report: The AuditReport to serialize.
        indent: JSON indentation level.
        sort_keys: Sort keys for stable, diffable output (default True).
        ensure_ascii: When False, non-ASCII characters are preserved.

    Returns:
        JSON string of the final report.
    """
    return json.dumps(
        report.as_final_dict(),
        indent=indent,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
    )


def build_report(
    *,
    requested_url: str,
    final_url: Optional[str],
    status_code: Optional[int],
    is_success: bool,
    error_message: Optional[str],
    findings: list[Finding],
    audit_errors: dict[str, str],
    audits_run: list[str],
    task_intent: Optional[str] = None,
    task_description: Optional[str] = None,
    target_objectives: Optional[list[str]] = None,
    audited_at: Optional[str] = None,
    objective_mappings: Optional[list[ObjectiveMapping]] = None,
    priority_signals: Optional[list[PrioritySignal]] = None,
) -> AuditReport:
    """Build an AuditReport from orchestrator outputs.

    This is the single entry point for report construction.

    Args:
        requested_url: Original URL from the audit request.
        final_url: Final URL after redirects, if any.
        status_code: HTTP status code, if any.
        is_success: Whether crawl + observation succeeded.
        error_message: Top-level error when is_success is False.
        findings: Deduplicated, sorted findings.
        audit_errors: Audit name → exception string map.
        audits_run: Audit names that were executed.
        task_intent: Task intent from AuditContext, if provided.
        task_description: Task description from AuditContext, if provided.
        target_objectives: Objectives from AuditContext, if provided.
        audited_at: Optional ISO-8601 timestamp for the report. Never generated
            by the builder — inject it in tests/callers to keep determinism.
        objective_mappings: Optional finding → objective mappings produced by
            the Objective Mapping stage. Defaults to empty.
        priority_signals: Optional priority-ordered signals produced by the
            Priority Engine stage. Defaults to empty.

    Returns:
        Fully constructed AuditReport with computed summary.
    """
    if priority_signals:
        ordered_findings: list[Finding] = []
        finding_pool = list(findings)
        for sig in priority_signals:
            for idx, f in enumerate(finding_pool):
                if f.id == sig.finding_id:
                    ordered_findings.append(finding_pool.pop(idx))
                    break
        ordered_findings.extend(finding_pool)
        findings = ordered_findings

    summary = _compute_summary(findings, audits_run, audit_errors)

    return AuditReport(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        is_success=is_success,
        error_message=error_message,
        findings=findings,
        audit_errors=audit_errors,
        audits_run=audits_run,
        task_intent=task_intent,
        task_description=task_description,
        target_objectives=target_objectives or [],
        summary=summary,
        audited_at=audited_at,
        objective_mappings=objective_mappings or [],
        priority_signals=priority_signals or [],
    )
