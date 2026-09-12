"""Audit layer for evaluating machine answerability, discovery, task readiness, and crawlability."""

from src.audits.crawl_render import audit_crawl_render
from src.audits.audit_request import AuditContext, AuditRequest
from src.audits.evidence_consistency import (
    ConsistencyClassification,
    EvidenceUnit,
    FactInventory,
    audit_evidence_consistency,
    build_fact_inventory,
    classify_consistency,
)
from src.audits.finding_aggregator import aggregate_findings, deduplicate_findings, sort_findings, tag_findings
from src.audits.freshness_corroboration import audit_freshness_corroboration
from src.audits.engagement import audit_engagement
from src.audits.fact_inventory import (
    extract_fact_inventory,
)
from src.audits.machine_answerability import audit_machine_answerability
from src.audits.models import Finding, SuggestedAction
from src.audits.objective_mapping import ObjectiveMapping, map_findings_to_objectives
from src.audits.priority_engine import PriorityEngine, PrioritySignal, collect_signals, compute_priorities
from src.audits.reporting import AuditReport, build_report, report_to_json
from src.audits.task_readiness import audit_task_readiness

__all__ = [
    "AuditContext",
    "AuditReport",
    "AuditRequest",
    "ConsistencyClassification",
    "EvidenceUnit",
    "FactInventory",
    "Finding",
    "ObjectiveMapping",
    "PriorityEngine",
    "PrioritySignal",
    "SuggestedAction",
    "aggregate_findings",
    "audit_crawl_render",
    "audit_evidence_consistency",
    "audit_engagement",
    "audit_freshness_corroboration",
    "audit_machine_answerability",
    "audit_task_readiness",
    "build_fact_inventory",
    "build_report",
    "classify_consistency",
    "collect_signals",
    "compute_priorities",
    "deduplicate_findings",
    "extract_fact_inventory",
    "map_findings_to_objectives",
    "report_to_json",
    "sort_findings",
    "tag_findings",
]
