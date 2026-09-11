"""Task Readiness Audit module.

Evaluates whether a web page structurally supports automated task execution
by AI agents and programmatic workflows. Checks for non-navigable action affordances,
action endpoint deficits on advertised capabilities, unstructured multi-step workflows,
unreachable authentication barriers, and decision evidence deficits on transactional pages.

Design Principle: Task Readiness != Schema Completeness.
Consumes PageObservation without re-crawling or re-parsing.
"""

from __future__ import annotations

import re
from typing import Any

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# Critical transactional action verbs
CRITICAL_ACTION_KEYWORDS = re.compile(
    r"\b(add to cart|checkout|buy now|place order|book now|submit application|confirm booking|pay now)\b",
    re.I,
)

# Advertised interactive capabilities
CAPABILITY_KEYWORDS = re.compile(
    r"\b(search catalog|search products|search site|book a flight|request a quote|subscribe to newsletter|find items|filter catalog)\b",
    re.I,
)

# Multi-step process indicators
MULTISTEP_KEYWORDS = re.compile(
    r"\b(step 1|step 2|step 3|prerequisites|requirements|how to apply|checkout process|registration steps|onboarding steps)\b",
    re.I,
)

# Mandatory authentication barrier indicators
AUTH_BARRIER_KEYWORDS = re.compile(
    r"\b(login required|sign in to complete|members only|authentication required|log in to view price|must be logged in)\b",
    re.I,
)

# Transactional / Product page indicators (primary keyword match)
TRANSACTIONAL_INTENT_KEYWORDS = re.compile(
    r"\b(product|price|shop|store|buy|cart|item|model|sku|specs|specifications|offer|service|quote)\b",
    re.I,
)

# Availability / stock signals
AVAILABILITY_PATTERN = re.compile(
    r"\b(in stock|out of stock|available|limited stock|ships in|delivery|add to bag|order now)\b",
    re.I,
)

# Purchase / acquisition link anchors
PURCHASE_LINK_PATTERN = re.compile(
    r"\b(buy|add to cart|purchase|order|checkout|get it now|add to bag|buy now)\b",
    re.I,
)

# Product model identifier: alphanumeric code containing letters AND digits (e.g. AeroMesh 900, SKU-12X, iPhone 15)
MODEL_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Za-z][A-Za-z0-9]*[-\s]?\d{2,}[A-Za-z0-9]*\b",
)

# Currency and price patterns in text
PRICE_PATTERN = re.compile(
    r"(\$|₹|€|£|USD|INR|EUR|GBP)\s?\d+([.,]\d{2})?|\b\d+([.,]\d{2})?\s?(USD|INR|EUR|GBP)\b",
    re.I,
)


def _check_unnavigable_action_affordances(obs: PageObservation) -> list[Finding]:
    """TR-INT-001: Detect critical task actions whose HTML affordances are non-navigable with no valid fallback link."""
    findings: list[Finding] = []

    intent_links: dict[str, list[tuple[str, str]]] = {}

    for link in obs.links:
        anchor = link.anchor_text.strip()
        if not anchor:
            continue
        match = CRITICAL_ACTION_KEYWORDS.search(anchor)
        if match:
            intent = match.group(0).lower()
            if intent not in intent_links:
                intent_links[intent] = []
            intent_links[intent].append((anchor, link.raw_href or ""))

    broken_intents: list[str] = []

    for intent, links in intent_links.items():
        all_non_navigable = True
        for anchor, raw_href in links:
            raw = raw_href.strip().lower()
            if raw and raw != "#" and not raw.startswith("javascript:"):
                all_non_navigable = False
                break

        if all_non_navigable and links:
            sample_anchor, sample_href = links[0]
            broken_intents.append(f"'{sample_anchor}' (href='{sample_href}')")

    if broken_intents:
        evidence_str = (
            f"Found critical task action link(s) with non-navigable or JavaScript-only href attributes: "
            + ", ".join(broken_intents)
            + ". While the action affordance is visually presented, no navigable HTML fallback URL or POST target is exposed, "
            "which creates a weak pathway for automated agents attempting to execute the task step."
        )
        findings.append(
            Finding(
                id="TR-INT-001",
                title="Critical task action affordance is non-navigable",
                severity="critical" if any("checkout" in b.lower() or "buy" in b.lower() or "cart" in b.lower() for b in broken_intents) else "high",
                evidence=evidence_str,
                suggested_action=SuggestedAction(
                    summary="Replace JavaScript pseudo-links (javascript: or #) with standard HTML anchor URLs or HTTP POST form targets to support automated execution.",
                    priority=1,
                ),
                priority=1,
                confidence="high",
                details={"broken_intents": broken_intents},
            )
        )

    return findings


def _check_action_endpoint_deficit(obs: PageObservation) -> list[Finding]:
    """TR-FORM-001: Detect advertised interactive capability with zero visible guidance or navigable endpoint support."""
    findings: list[Finding] = []

    text_to_check = (obs.title or "") + " " + " ".join(obs.h1 + obs.h2) + " " + obs.visible_text[:1000]
    match = CAPABILITY_KEYWORDS.search(text_to_check)

    if match:
        capability = match.group(0)

        # 1. Check if navigable link exists for that capability
        has_navigable_link = any(
            re.search(r"\b(search|book|quote|subscribe|filter)\b", l.anchor_text + " " + l.href, re.I)
            and l.href
            and not l.raw_href.startswith("javascript:")
            and l.raw_href != "#"
            for l in obs.links
        )

        # 2. Check if visible text or tables contain input/endpoint guidance
        has_text_guidance = bool(
            re.search(r"\b(enter|type|select|form|input|submit|query|url|api)\b", obs.visible_text, re.I)
        )

        # 3. Check JSON-LD (optional supporting evidence)
        has_schema_endpoint = False
        for ld in obs.json_ld:
            if ld.is_valid and isinstance(ld.parsed_data, dict):
                tp = str(ld.parsed_data.get("@type", "")).lower()
                if "action" in tp or "potentialaction" in ld.parsed_data:
                    has_schema_endpoint = True
                    break

        if not has_navigable_link and not has_text_guidance and not has_schema_endpoint:
            evidence_str = (
                f"Page advertises an interactive task capability ('{capability}') in headings/text, "
                "but provides zero navigable target links, zero visible input instructions, and no structured endpoint definitions, "
                "which may limit an assistant's ability to discover or trigger the workflow."
            )
            findings.append(
                Finding(
                    id="TR-FORM-001",
                    title="Advertised task capability lacks actionable endpoint or navigation support",
                    severity="high",
                    evidence=evidence_str,
                    suggested_action=SuggestedAction(
                        summary="Provide navigable HTML action links, visible form guidance, or Schema.org PotentialAction endpoints for advertised capabilities.",
                        priority=1,
                    ),
                    priority=1,
                    confidence="medium",
                    details={"advertised_capability": capability},
                )
            )

    return findings


def _check_unstructured_multistep_workflow(obs: PageObservation) -> list[Finding]:
    """TR-PROC-001: Detect multi-step task workflows with insufficient structural or step-navigation guidance."""
    findings: list[Finding] = []

    text_to_check = " ".join(obs.h1 + obs.h2 + obs.h3) + " " + obs.visible_text[:1500]
    match = MULTISTEP_KEYWORDS.search(text_to_check)

    if match:
        phrase = match.group(0)

        # Signal 1: Structured headings (h1/h2/h3 contains step numbers or step titles)
        has_step_headings = any(
            re.search(r"\b(step\s*\d|phase\s*\d|stage\s*\d|part\s*\d|1\.|2\.|3\.)\b", h, re.I)
            for h in obs.h1 + obs.h2 + obs.h3
        )

        # Signal 2: Step navigation links (Next, Continue, Step 2, Proceed)
        has_step_links = any(
            re.search(r"\b(next|continue|proceed|step\s*\d|start|get started)\b", l.anchor_text, re.I)
            and l.href
            and not l.raw_href.startswith("javascript:")
            and l.raw_href != "#"
            for l in obs.links
        )

        # Flag ONLY if multi-step text exists BUT visible headings are unstructured AND zero step links exist.
        # Schema (HowTo/ItemList) does NOT rescue an unstructured visible workflow.
        if not has_step_headings and not has_step_links:
            evidence_str = (
                f"Page describes a multi-step workflow ('{phrase}') in paragraph text, "
                "but lacks structured heading steps (h1/h2/h3) and step navigation links ('Next', 'Continue'), "
                "providing limited structural support for multi-step agent execution."
            )
            findings.append(
                Finding(
                    id="TR-PROC-001",
                    title="Multi-step task workflow lacks structural guidance or step navigation",
                    severity="medium",
                    evidence=evidence_str,
                    suggested_action=SuggestedAction(
                        summary="Structure multi-step tasks using clear heading hierarchies (e.g. <h2>Step 1</h2>) and navigable step-transition links.",
                        priority=2,
                    ),
                    priority=2,
                    confidence="medium",
                    details={"matched_phrase": phrase},
                )
            )

    return findings


def _check_unreachable_authentication_barrier(obs: PageObservation) -> list[Finding]:
    """TR-AUTH-001: Detect mandatory authentication barrier where no navigable authentication pathway is exposed."""
    findings: list[Finding] = []

    text_to_check = (obs.title or "") + " " + " ".join(obs.h1 + obs.h2) + " " + obs.visible_text[:1000]
    match = AUTH_BARRIER_KEYWORDS.search(text_to_check)

    if match:
        auth_phrase = match.group(0)

        # Check if ANY navigable login, sign-in, account, or auth link exists
        has_login_link = any(
            re.search(r"\b(log\s*in|sign\s*in|account|auth|register|user)\b", l.anchor_text + " " + l.href, re.I)
            and l.href
            and not l.raw_href.startswith("javascript:")
            and l.raw_href != "#"
            for l in obs.links
        )

        if not has_login_link:
            evidence_str = (
                f"Page states that authentication is required ('{auth_phrase}') to execute the task, "
                "but no navigable login or account entry link was discovered on the page, "
                "creating an obstacle for automated agents attempting to satisfy authentication prerequisites."
            )
            findings.append(
                Finding(
                    id="TR-AUTH-001",
                    title="Mandatory authentication barrier lacks navigable authentication entry point",
                    severity="medium",
                    evidence=evidence_str,
                    suggested_action=SuggestedAction(
                        summary="Provide a clear, navigable HTML login or sign-in link on gated task pages.",
                        priority=2,
                    ),
                    priority=2,
                    confidence="high",
                    details={"auth_phrase": auth_phrase},
                )
            )

    return findings


def _is_transactional_page(obs: PageObservation) -> tuple[bool, bool]:
    """Determine if a page is transactional using multiple independent structural signals.

    Returns:
        (is_transactional, has_product_schema)

    A page is transactional if:
    - Primary keyword signals match in title/headings, OR
    - Product/Offer/Service schema is present, OR
    - Multiple structural signals collectively indicate a transactional page:
        e.g. (price OR availability) + (purchase link OR model identifier)

    Design principle: No single signal alone (e.g. price, schema, or model name)
    is sufficient. Structural signal combinations are required for the secondary path.
    """
    title_head_text = (obs.title or "") + " " + " ".join(obs.h1 + obs.h2 + obs.h3)
    visible_excerpt = obs.visible_text[:2000]

    # Path 1: Primary keyword match in title/headings
    if TRANSACTIONAL_INTENT_KEYWORDS.search(title_head_text):
        has_schema = _has_product_schema(obs)
        return True, has_schema

    # Path 2: Product/Offer/Service schema
    if _has_product_schema(obs):
        return True, True

    # Path 3: Multi-signal structural detection (catches branded product pages
    # like 'AeroMesh 900' without explicit transactional keywords in headings)
    signal_score = 0

    # Signal A: Visible price currency amount
    if PRICE_PATTERN.search(visible_excerpt):
        signal_score += 1

    # Signal B: Availability / stock language
    if AVAILABILITY_PATTERN.search(visible_excerpt):
        signal_score += 1

    # Signal C: Purchase / acquisition link with navigable href
    if any(
        PURCHASE_LINK_PATTERN.search(lnk.anchor_text)
        and lnk.href
        and not lnk.raw_href.startswith("javascript:")
        and lnk.raw_href != "#"
        for lnk in obs.links
    ):
        signal_score += 1

    # Signal D: Product model identifier in h1 (e.g. 'AeroMesh 900', 'Galaxy S24')
    if any(MODEL_IDENTIFIER_PATTERN.search(h) for h in obs.h1):
        signal_score += 1

    # Signal E: Quote / pricing-on-request language in visible text
    if re.search(r"\b(quote|inquire|contact for price|pricing on request|request quote)\b", visible_excerpt, re.I):
        signal_score += 1

    # Require at least 2 independent structural signals to classify as transactional
    # This prevents informational articles with a single price mention from triggering.
    if signal_score >= 2:
        return True, False

    return False, False


def _has_product_schema(obs: PageObservation) -> bool:
    """Return True if any valid Product/Offer/Service JSON-LD is present."""
    for ld in obs.json_ld:
        if ld.is_valid and isinstance(ld.parsed_data, dict):
            tp = str(ld.parsed_data.get("@type", ""))
            if tp in ("Product", "Offer", "Service", "FlightReservation", "HotelReservation"):
                return True
    return False


def _check_decision_evidence_deficit(obs: PageObservation) -> list[Finding]:
    """TR-DATA-001: Detect transactional / product / service page with severe decision-evidence deficit across all channels."""
    findings: list[Finding] = []

    is_transactional, has_product_schema = _is_transactional_page(obs)

    if not is_transactional:
        return []

    has_visible_price = bool(PRICE_PATTERN.search(obs.visible_text))
    has_table_data = obs.num_tables > 0
    has_specs_text = bool(
        re.search(
            r"\b(specification|specifications|features|details|weight|dimensions|size|material|"
            r"includes|warranty|description|use cases|terms|battery|connectivity|processor|resolution)\b",
            obs.visible_text,
            re.I,
        )
    )
    # Raised from 40 to 80 words to avoid flagging service/B2B pages with
    # adequate paragraph-level descriptions as having insufficient content.
    has_substantial_text = len(obs.visible_text.split()) > 80
    is_quote_based = bool(
        re.search(r"\b(quote|inquire|contact for price|pricing on request|request quote)\b", obs.visible_text, re.I)
    )

    # Evaluate decision evidence categories across channels:
    # 1. Pricing or quote indication
    # 2. Specifications or structured table data
    # 3. Substantial body description (> 80 words)
    evidence_score = 0
    if has_visible_price or is_quote_based:
        evidence_score += 1
    if has_specs_text or has_table_data:
        evidence_score += 1
    if has_substantial_text:
        evidence_score += 1

    # Broad deficit trigger: If fewer than 2 independent evidence categories exist, flag decision evidence deficit.
    if evidence_score < 2:
        evidence_str = (
            "Transactional/product page exhibits a decision evidence deficit across visible text, table data, and structured metadata. "
            "Key evaluation attributes (such as price, specifications, terms, or service scope) are absent across available channels, "
            "providing limited structural support for an assistant evaluating task execution readiness."
        )
        findings.append(
            Finding(
                id="TR-DATA-001",
                title="Transactional page exhibits decision evidence deficit across available channels",
                severity="high",
                evidence=evidence_str,
                suggested_action=SuggestedAction(
                    summary="Provide clear decision attributes (pricing, specifications, availability, or quote instructions) in visible text, tables, or structured metadata.",
                    priority=1,
                ),
                priority=1,
                confidence="medium",
                details={
                    "has_visible_price": has_visible_price,
                    "has_specs_text": has_specs_text,
                    "has_table_data": has_table_data,
                    "has_product_schema": has_product_schema,
                    "evidence_score": evidence_score,
                },
            )
        )

    return findings


def audit_task_readiness(obs: PageObservation) -> list[Finding]:
    """Audit a PageObservation for task readiness defects.

    Args:
        obs: Clean PageObservation constructed by observation layer.

    Returns:
        List of Finding dataclass instances representing identified task readiness issues.
    """
    if not obs.is_success:
        return []

    findings: list[Finding] = []
    findings.extend(_check_unnavigable_action_affordances(obs))
    findings.extend(_check_action_endpoint_deficit(obs))
    findings.extend(_check_unstructured_multistep_workflow(obs))
    findings.extend(_check_unreachable_authentication_barrier(obs))
    findings.extend(_check_decision_evidence_deficit(obs))

    return findings
