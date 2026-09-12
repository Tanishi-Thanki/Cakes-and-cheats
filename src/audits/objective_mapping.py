"""Objective Mapping — deterministic task-aware finding to objective mapping.

The Objective Mapping layer receives findings and task context, and
establishes which findings are relevant to which objectives and how severely
they affect the requested task.

Design principles:
- Read-only, deterministic, provider-neutral: no LLMs, external APIs, databases,
  or heuristic guessing.
- Evidence before inference: findings are only linked to objectives with
  deterministic support from rule IDs, explicit target objectives, or task intent keywords.
- When there is insufficient or no task context, findings remain 'general' with an
  empty affected_objectives list.
- Existing audits remain responsible for detecting defects; Objective Mapping only
  interprets relevance to requested objectives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from src.audits.models import Finding

# ---------------------------------------------------------------------------
# Canonical Objective Dimensions
# ---------------------------------------------------------------------------

CANONICAL_OBJECTIVES: frozenset[str] = frozenset({
    "transaction_completion",
    "task_completion",
    "price_accuracy",
    "price_evaluation",
    "inventory_verification",
    "entity_identity",
    "information_sufficiency",
    "content_discovery",
    "content_extraction",
    "workflow_progression",
    "authentication_access",
})

# ---------------------------------------------------------------------------
# Rule-to-Objective Taxonomy
# ---------------------------------------------------------------------------

# Primary canonical objectives associated with each deterministic finding rule ID.
FINDING_RULE_OBJECTIVES: dict[str, tuple[str, ...]] = {
    # Task Readiness
    "TR-INT-001": ("transaction_completion", "task_completion"),
    "TR-FORM-001": ("task_completion", "workflow_progression"),
    "TR-PROC-001": ("workflow_progression", "task_completion"),
    "TR-AUTH-001": ("authentication_access", "task_completion"),
    "TR-DATA-001": ("information_sufficiency", "price_evaluation"),
    # Evidence Consistency
    "EVID-PRC-001": ("price_accuracy", "price_evaluation"),
    "EVID-AVT-001": ("inventory_verification",),
    "EVID-ENT-001": ("entity_identity",),
    "EVID-ATT-001": ("information_sufficiency", "content_extraction"),
    # Freshness & Corroboration
    "FC-REP-001": ("price_accuracy", "price_evaluation"),
    "FC-REP-002": ("inventory_verification",),
    "FC-ATTR-001": ("information_sufficiency", "content_extraction"),
    "FC-ENT-001": ("entity_identity",),
    "FC-ENT-002": ("entity_identity",),
    "FC-FRSH-001": ("price_evaluation", "information_sufficiency"),
    # Machine Answerability
    "MA-REP-001": ("price_accuracy", "price_evaluation"),
    "MA-REP-002": ("inventory_verification",),
    "MA-IMG-001": ("content_extraction", "information_sufficiency"),
    "MA-TXT-001": ("content_extraction", "content_discovery"),
    "MA-CTX-001": ("entity_identity", "information_sufficiency"),
    # Crawl / Render
    "CR-001": ("content_discovery", "task_completion"),
    "CR-002": ("content_discovery", "content_extraction"),
    "CR-003": ("content_discovery",),
    "CR-004": ("content_discovery",),
    # Engagement
    "ENG-001": ("task_completion", "workflow_progression"),
    "ENG-002": ("task_completion", "workflow_progression"),
    "ENG-003": ("task_completion", "workflow_progression"),
    "ENG-004": ("task_completion", "workflow_progression"),
}

# ---------------------------------------------------------------------------
# Normalization & Aliases
# ---------------------------------------------------------------------------

OBJECTIVE_ALIASES: dict[str, str] = {
    # transaction_completion
    "transaction_completion": "transaction_completion",
    "transaction": "transaction_completion",
    "purchase": "transaction_completion",
    "buy": "transaction_completion",
    "checkout": "transaction_completion",
    "order": "transaction_completion",
    "booking": "transaction_completion",
    "payment": "transaction_completion",
    # task_completion
    "task_completion": "task_completion",
    "task": "task_completion",
    # price_accuracy / price_evaluation
    "price_accuracy": "price_accuracy",
    "price_consistency": "price_accuracy",
    "price_evaluation": "price_evaluation",
    "price_comparison": "price_evaluation",
    "pricing": "price_evaluation",
    "price": "price_evaluation",
    "compare_prices": "price_evaluation",
    # inventory_verification
    "inventory_verification": "inventory_verification",
    "inventory": "inventory_verification",
    "stock": "inventory_verification",
    "availability": "inventory_verification",
    # entity_identity
    "entity_identity": "entity_identity",
    "brand_consistency": "entity_identity",
    "identity": "entity_identity",
    "brand": "entity_identity",
    # information_sufficiency
    "information_sufficiency": "information_sufficiency",
    "decision_evidence": "information_sufficiency",
    "product_details": "information_sufficiency",
    "specifications": "information_sufficiency",
    "specs": "information_sufficiency",
    # content_discovery
    "content_discovery": "content_discovery",
    "crawlability": "content_discovery",
    "indexing": "content_discovery",
    "discoverability": "content_discovery",
    # content_extraction
    "content_extraction": "content_extraction",
    "extraction": "content_extraction",
    "machine_answerability": "content_extraction",
    # workflow_progression
    "workflow_progression": "workflow_progression",
    "workflow": "workflow_progression",
    "multi_step": "workflow_progression",
    "registration": "workflow_progression",
    # authentication_access
    "authentication_access": "authentication_access",
    "authentication": "authentication_access",
    "login": "authentication_access",
    "auth": "authentication_access",
}

# Specific keyword patterns in task_intent / task_description mapped to canonical objectives
INTENT_KEYWORD_MAP: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"\b(purchas(?:e|es|ing)|buy(?:s|ing)?|orders?|checkouts?|cart|books?|bookings?|pay(?:s|ment)?|reserve)\b", re.I), ("transaction_completion", "task_completion")),
    (re.compile(r"\b(prices?|pricing|costs?|cheaper|expensive|rates?|discounts?|compare\s+prices?|price\s+comparison)\b", re.I), ("price_accuracy", "price_evaluation")),
    (re.compile(r"\b(stocks?|inventor(?:y|ies)|available|availability|in\s+stock)\b", re.I), ("inventory_verification",)),
    (re.compile(r"\b(brands?|models?|identity|genuine|manufacturers?)\b", re.I), ("entity_identity",)),
    (re.compile(r"\b(spec|specs|specifications?|features?|details?|terms|decision)\b", re.I), ("information_sufficiency",)),
    (re.compile(r"\b(crawls?|crawlable|index(?:ing)?|discover(?:y)?|find)\b", re.I), ("content_discovery",)),
    (re.compile(r"\b(extract(?:ion|s)?|read|parse|render|text)\b", re.I), ("content_extraction",)),
    (re.compile(r"\b(steps?|process(?:es)?|workflows?|regist(?:er|ration)|apply|onboard(?:ing)?)\b", re.I), ("workflow_progression",)),
    (re.compile(r"\b(log(?:in|ins|\s+in)|sign(?:in|\s+in)|auth(?:enticat(?:e|ion))?|passwords?|accounts?)\b", re.I), ("authentication_access",)),
]


def _normalize_token(text: str) -> str:
    """Normalize text into lowercase alphanumeric with underscores."""
    cleaned = re.sub(r"[^\w\s-]", "", text.strip().lower())
    return re.sub(r"[\s-]+", "_", cleaned)


def _resolve_target_objectives(
    target_objectives: Optional[list[str]],
    task_intent: Optional[str],
    task_description: Optional[str],
) -> set[str]:
    """Derive canonical objectives from explicit target objectives and intent text."""
    resolved: set[str] = set()

    # 1. Explicit target objectives
    if target_objectives:
        for obj in target_objectives:
            norm = _normalize_token(obj)
            canonical = OBJECTIVE_ALIASES.get(norm)
            if canonical:
                resolved.add(canonical)
            elif norm in CANONICAL_OBJECTIVES:
                resolved.add(norm)

    # 2. Extract from task_intent / task_description keywords
    combined_text = f"{task_intent or ''} {task_description or ''}".strip()
    if combined_text:
        for pattern, canon_list in INTENT_KEYWORD_MAP:
            if pattern.search(combined_text):
                resolved.update(canon_list)

    return resolved


# ---------------------------------------------------------------------------
# ObjectiveMapping Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectiveMapping:
    """Maps a finding to the objectives it affects and its task relevance.

    Attributes:
        finding_id: The finding's rule identifier.
        source_audit: Name of the audit that produced the finding.
        affected_objectives: List of canonical objective names this finding impacts.
            Empty when no task relationship has been established.
        task_relevance: Relevance tier ('direct_blocker', 'task_impairing', 'general').
        relevance_reason: Transparent evidence-based explanation for the relevance assignment.
    """

    finding_id: str
    source_audit: str
    affected_objectives: list[str] = field(default_factory=list)
    task_relevance: str = "general"
    relevance_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        """Serialize this mapping to a plain dictionary."""
        return {
            "finding_id": self.finding_id,
            "source_audit": self.source_audit,
            "affected_objectives": list(self.affected_objectives),
            "task_relevance": self.task_relevance,
            "relevance_reason": self.relevance_reason,
        }


# ---------------------------------------------------------------------------
# Mapping Engine
# ---------------------------------------------------------------------------


def _classify_relevance(
    finding: Finding,
    matched_objectives: list[str],
    task_intent: Optional[str],
    target_objectives: Optional[list[str]],
) -> tuple[str, str]:
    """Determine task relevance ('direct_blocker', 'task_impairing', 'general') and reason."""
    if not matched_objectives:
        return "general", "General site issue; no requested task/objective relationship was established."

    intent_lower = (task_intent or "").lower()

    # Rule 1: Access failure (CR-001) blocks any task on the page
    if finding.id == "CR-001":
        return (
            "direct_blocker",
            "Directly blocks the requested task because the page failed to crawl successfully.",
        )

    # Rule 2: Non-navigable action affordance on a transaction/action task
    if finding.id == "TR-INT-001" and "transaction_completion" in matched_objectives:
        return (
            "direct_blocker",
            "Directly affects the requested transaction_completion objective because the purchase action is not navigable.",
        )

    # Rule 3: Unreachable authentication barrier on a gated task
    if finding.id == "TR-AUTH-001" and ("authentication_access" in matched_objectives or "task_completion" in matched_objectives):
        return (
            "direct_blocker",
            "Directly affects the requested task because a mandatory authentication barrier lacks an entry point.",
        )

    # Rule 4: Explicit objective is price evaluation/accuracy and finding has a price conflict
    price_target_explicit = bool(
        target_objectives
        and any(_normalize_token(t) in ("price_accuracy", "price_evaluation", "price_comparison", "pricing", "price") for t in target_objectives)
    )
    price_intent_explicit = bool(re.search(r"\b(compare\s+price|check\s+price|price\s+comparison)\b", intent_lower))
    if finding.id in ("EVID-PRC-001", "FC-REP-001", "MA-REP-001") and (price_target_explicit or price_intent_explicit):
        return (
            "direct_blocker",
            "Directly affects the requested price objective because conflicting price representations were detected.",
        )

    # Rule 5: Explicit objective is inventory verification and finding has stock conflict
    stock_target_explicit = bool(
        target_objectives
        and any(_normalize_token(t) in ("inventory_verification", "stock", "availability") for t in target_objectives)
    )
    stock_intent_explicit = bool(re.search(r"\b(check\s+stock|verify\s+inventory|stock\s+availability)\b", intent_lower))
    if finding.id in ("EVID-AVT-001", "FC-REP-002", "MA-REP-002") and (stock_target_explicit or stock_intent_explicit):
        return (
            "direct_blocker",
            "Directly affects the requested inventory objective because conflicting stock status was detected.",
        )

    # Specific Task Impairing Reasons
    if finding.id in ("EVID-PRC-001", "FC-REP-001", "MA-REP-001"):
        return (
            "task_impairing",
            "Impairs price evaluation because conflicting price representations were detected for the same product.",
        )
    if finding.id in ("EVID-AVT-001", "FC-REP-002", "MA-REP-002"):
        return (
            "task_impairing",
            "Impairs inventory verification because conflicting availability status was detected.",
        )
    if finding.id == "TR-DATA-001":
        return (
            "task_impairing",
            "Impairs task evaluation because transactional decision evidence is insufficient across available channels.",
        )
    if finding.id == "TR-PROC-001":
        return (
            "task_impairing",
            "Impairs workflow progression because multi-step task guidance is unstructured.",
        )

    # Default Task Impairing
    objectives_str = ", ".join(matched_objectives)
    return (
        "task_impairing",
        f"Impairs the requested objective(s) ({objectives_str}) due to {finding.title.lower()}.",
    )


def map_findings_to_objectives(
    findings: list[Finding],
    target_objectives: Optional[list[str]] = None,
    task_intent: Optional[str] = None,
    task_description: Optional[str] = None,
) -> list[ObjectiveMapping]:
    """Map findings to their affected objectives and task relevance.

    Evaluates each finding's rule identity against the requested task context
    (target_objectives, task_intent, task_description).

    Returns:
        List of ObjectiveMapping objects, one per finding.
        When no context is provided or no link exists, affected_objectives is []
        and task_relevance is 'general'.
    """
    requested_objectives = _resolve_target_objectives(
        target_objectives,
        task_intent,
        task_description,
    )

    mappings: list[ObjectiveMapping] = []
    for f in findings:
        if not requested_objectives:
            # Conservative: no task context means no objective link claimed
            mappings.append(
                ObjectiveMapping(
                    finding_id=f.id,
                    source_audit=f.source_audit,
                    affected_objectives=[],
                    task_relevance="general",
                    relevance_reason="General site issue; no requested task/objective relationship was established.",
                )
            )
            continue

        rule_objectives = FINDING_RULE_OBJECTIVES.get(f.id, ())
        matched = [obj for obj in rule_objectives if obj in requested_objectives]

        if not matched:
            mappings.append(
                ObjectiveMapping(
                    finding_id=f.id,
                    source_audit=f.source_audit,
                    affected_objectives=[],
                    task_relevance="general",
                    relevance_reason="General site issue; no requested task/objective relationship was established.",
                )
            )
            continue

        relevance, reason = _classify_relevance(
            f,
            matched,
            task_intent,
            target_objectives,
        )

        mappings.append(
            ObjectiveMapping(
                finding_id=f.id,
                source_audit=f.source_audit,
                affected_objectives=matched,
                task_relevance=relevance,
                relevance_reason=reason,
            )
        )

    return mappings
