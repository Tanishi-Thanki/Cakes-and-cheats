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
"""

from __future__ import annotations

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
    objective_mappings: list[ObjectiveMapping] = field(default_factory=list)
    priority_signals: list[PrioritySignal] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialize the report to a plain dictionary.

        Returns:
            Dict suitable for JSON serialization.
        """
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
            if f.details:
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
            "audits_run": list(self.audits_run),
            "task_intent": self.task_intent,
            "task_description": self.task_description,
            "target_objectives": list(self.target_objectives),
            "summary": dict(self.summary),
            "objective_mappings": [m.as_dict() for m in self.objective_mappings],
            "priority_signals": [s.as_dict() for s in self.priority_signals],
        }


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
    for f in findings:
        if f.severity in severity_counts:
            severity_counts[f.severity] += 1

    audit_status: dict[str, str] = {}
    for name in audits_run:
        if name in audit_errors:
            audit_status[name] = "failed"
        else:
            audit_status[name] = "completed"

    return {
        "total_findings": len(findings),
        "severity_counts": severity_counts,
        "audits_attempted": len(audits_run),
        "audits_succeeded": sum(1 for s in audit_status.values() if s == "completed"),
        "audits_failed": sum(1 for s in audit_status.values() if s == "failed"),
        "audit_status": audit_status,
    }


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
        objective_mappings: Optional finding → objective mappings produced by
            the Objective Mapping stage. Defaults to empty.
        priority_signals: Optional priority-ordered signals produced by the
            Priority Engine stage. Defaults to empty.

    Returns:
        Fully constructed AuditReport with computed summary.
    """
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
        objective_mappings=objective_mappings or [],
        priority_signals=priority_signals or [],
    )
