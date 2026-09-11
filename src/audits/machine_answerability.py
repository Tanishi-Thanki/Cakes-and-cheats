"""Machine Answerability Audit.

Evaluates whether important website information can be extracted and understood
reliably by AI systems based on observed page structure, visible text, images,
and structured metadata.

Design Principles:
- Evidence before inference: no finding without concrete observed facts.
- Deterministic checks without external models, APIs, or re-parsing.
- Conservative severity: avoiding false positives on legitimate minimal pages.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# Generic or non-descriptive alt texts that do not convey meaningful information
GENERIC_ALT_VALUES: frozenset[str] = frozenset({
    "",
    "image",
    "photo",
    "picture",
    "img",
    "banner",
    "screenshot",
    "graphic",
    "logo",
    "icon",
    "thumbnail",
})

# Regex for detecting common currency symbols and currency codes followed by numbers
PRICE_REGEX: re.Pattern[str] = re.compile(
    r"(?:₹|Rs\.?|\$|€|£|INR|USD)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


def _is_useful_alt(alt: str) -> bool:
    """Check if an image alt text is present and descriptive."""
    cleaned = alt.strip().lower()
    return bool(cleaned and cleaned not in GENERIC_ALT_VALUES)


def _collect_json_ld_entities(obs: PageObservation) -> list[dict[str, Any]]:
    """Traverse all valid JSON-LD blocks to gather top-level and nested entity dictionaries."""
    entities: list[dict[str, Any]] = []

    def _traverse(item: Any) -> None:
        if isinstance(item, dict):
            if "@graph" in item and isinstance(item["@graph"], list):
                for sub_item in item["@graph"]:
                    _traverse(sub_item)
            entities.append(item)
            for value in item.values():
                if isinstance(value, (dict, list)):
                    _traverse(value)
        elif isinstance(item, list):
            for sub_item in item:
                _traverse(sub_item)

    for block in obs.json_ld:
        if block.is_valid and block.parsed_data is not None:
            _traverse(block.parsed_data)

    return entities


def _extract_visible_prices(visible_text: str) -> list[tuple[str, str]]:
    """Extract raw visible price strings and their normalized numeric representations.

    Returns:
        List of tuples: (raw_price_string, normalized_integer_part)
        e.g., [("₹9,999", "9999"), ("$120.00", "120")]
    """
    matches = PRICE_REGEX.finditer(visible_text)
    results: list[tuple[str, str]] = []
    for m in matches:
        raw = m.group(0).strip()
        num_str = re.sub(r"[^\d.]", "", m.group(1))
        integer_part = num_str.split(".")[0]
        if integer_part:
            results.append((raw, integer_part))
    return results


def _check_image_dependency(obs: PageObservation) -> Optional[Finding]:
    """Check 1: Important information appears to be image-dependent.

    Fires when a page relies on images lacking descriptive alt text while
    providing minimal visible text and no tables.
    """
    if not obs.images:
        return None

    unhelpful_images = [img for img in obs.images if not _is_useful_alt(img.alt)]
    if len(unhelpful_images) < 2:
        return None

    words = obs.visible_text.split()
    word_count = len(words)

    # If page provides ample body text (>= 50 words) or structured tables,
    # the information is already accessible as text, so images are illustrative.
    if word_count >= 50 or obs.num_tables > 0:
        return None

    # Require evidence of informational intent (headings or multiple images)
    has_structure = bool(obs.h1 or obs.h2 or obs.h3) or len(unhelpful_images) >= 3
    if not has_structure:
        return None

    evidence = (
        f"Page contains {len(unhelpful_images)} image(s) lacking descriptive alt text "
        f"while visible text is minimal ({word_count} words) with no tables. "
        "If key product, specification, pricing, or decision information is conveyed "
        "primarily through images without equivalent text, automated systems may have "
        "less reliable access to that information."
    )

    return Finding(
        id="MA-IMG-001",
        title="Important page content may be image-dependent",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Provide descriptive alt text or equivalent textual descriptions for informative "
                "images so automated systems can access the content."
            ),
            priority=2,
        ),
        priority=2,
        confidence="medium",
        details={
            "unhelpful_image_count": len(unhelpful_images),
            "total_images": len(obs.images),
            "visible_word_count": word_count,
        },
    )


def _check_low_text_representation(obs: PageObservation) -> Optional[Finding]:
    """Check 2: Very low textual representation on a structured page.

    Fires when a page exhibits substantial navigational or layout structure
    (multiple links, images, headings) but contains fewer than 15 words of visible text.
    Reasoning: A structured page with < 15 words offers virtually no textual content
    for AI indexing, answering, or extraction, suggesting content may be locked in
    dynamic client-side code or non-textual components.
    """
    num_links = len(obs.links)
    num_images = len(obs.images)
    num_headings = len(obs.h1) + len(obs.h2) + len(obs.h3)

    is_substantial_structure = (
        (num_links >= 5)
        or (num_links >= 3 and num_images >= 2)
        or (num_headings >= 3 and num_links >= 3)
    )

    if not is_substantial_structure:
        return None

    word_count = len(obs.visible_text.split())
    if word_count >= 15:
        return None

    evidence = (
        f"Page has substantial structure ({num_links} links, {num_images} images, "
        f"{num_headings} headings) but contains only {word_count} words of visible text. "
        "Reasoning: Automated systems rely on accessible text to answer queries. "
        "Pages with layout structure but negligible text indicate that substantive "
        "content may be missing, dynamically rendered via JavaScript, or inaccessible."
    )

    return Finding(
        id="MA-TXT-001",
        title="Low textual representation on structured page",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Ensure core page information and entity descriptions are represented "
                "as accessible, static text within the page body."
            ),
            priority=2,
        ),
        priority=2,
        confidence="medium",
        details={
            "visible_word_count": word_count,
            "num_links": num_links,
            "num_images": num_images,
            "num_headings": num_headings,
        },
    )


def _check_representation_conflict(obs: PageObservation) -> list[Finding]:
    """Check 3: Structured data representation conflict.

    Compares high-confidence fields between JSON-LD and visible text (pricing, availability).
    Fires only when an explicit, demonstrable conflict is present.
    """
    findings: list[Finding] = []
    entities = _collect_json_ld_entities(obs)
    if not entities:
        return findings

    visible_prices = _extract_visible_prices(obs.visible_text)
    visible_numeric_set = {num for _, num in visible_prices}

    # 1. Price comparison
    for entity in entities:
        candidates: list[dict[str, Any]] = []
        offers = entity.get("offers")
        if isinstance(offers, dict):
            candidates.append(offers)
        elif isinstance(offers, list):
            candidates.extend(o for o in offers if isinstance(o, dict))
        if "price" in entity:
            candidates.append(entity)

        for c in candidates:
            raw_price = c.get("price")
            if raw_price is None:
                continue

            num_str = re.sub(r"[^\d.]", "", str(raw_price))
            json_integer = num_str.split(".")[0]
            if not json_integer:
                continue

            # Only report conflict if visible prices exist and NONE match the JSON-LD price
            if visible_numeric_set and json_integer not in visible_numeric_set:
                raw_visible_strs = [raw for raw, _ in visible_prices]
                currency = str(c.get("priceCurrency") or "").strip()
                evidence = (
                    f"Structured JSON-LD data specifies price '{raw_price}' "
                    f"({currency or 'unspecified currency'}), whereas visible page text "
                    f"explicitly shows conflicting price(s): {', '.join(raw_visible_strs)}. "
                    "Inconsistent representations increase the risk of an automated system "
                    "extracting conflicting or outdated pricing information."
                )
                findings.append(
                    Finding(
                        id="MA-REP-001",
                        title="Conflicting page representations may reduce machine confidence",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary=(
                                "Reconcile pricing values between structured data (JSON-LD) "
                                "and visible page text so automated systems receive consistent information."
                            ),
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "json_ld_price": raw_price,
                            "json_ld_currency": currency,
                            "visible_prices": raw_visible_strs,
                        },
                    )
                )
                # One price conflict finding is sufficient
                break
        if findings:
            break

    # 2. Availability comparison
    for entity in entities:
        offers = entity.get("offers")
        candidates = []
        if isinstance(offers, dict):
            candidates.append(offers)
        elif isinstance(offers, list):
            candidates.extend(o for o in offers if isinstance(o, dict))

        for c in candidates:
            raw_avail = str(c.get("availability") or "").lower()
            if not raw_avail:
                continue

            is_json_instock = "instock" in raw_avail and "outofstock" not in raw_avail
            is_json_outofstock = "outofstock" in raw_avail

            has_visible_outofstock = bool(
                re.search(r"\b(out of stock|sold out|unavailable)\b", obs.visible_text, re.IGNORECASE)
            )
            has_visible_instock = bool(
                re.search(r"\b(in stock|available now)\b", obs.visible_text, re.IGNORECASE)
            )

            if is_json_instock and has_visible_outofstock and not has_visible_instock:
                evidence = (
                    "Structured data (JSON-LD) declares availability as 'InStock', but visible "
                    "page text explicitly states 'Out of Stock'. Inconsistent representations "
                    "increase the risk of automated systems providing conflicting stock status."
                )
                findings.append(
                    Finding(
                        id="MA-REP-002",
                        title="Conflicting availability representations may reduce machine confidence",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary=(
                                "Synchronize stock status between structured data and visible text."
                            ),
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "json_ld_availability": c.get("availability"),
                            "visible_status": "Out of Stock",
                        },
                    )
                )
                break
            elif is_json_outofstock and has_visible_instock and not has_visible_outofstock:
                evidence = (
                    "Structured data (JSON-LD) declares availability as 'OutOfStock', but visible "
                    "page text states 'In Stock'. Inconsistent representations may mislead automated agents."
                )
                findings.append(
                    Finding(
                        id="MA-REP-002",
                        title="Conflicting availability representations may reduce machine confidence",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary=(
                                "Synchronize stock status between structured data and visible text."
                            ),
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "json_ld_availability": c.get("availability"),
                            "visible_status": "In Stock",
                        },
                    )
                )
                break

    return findings


def _check_missing_structured_context(obs: PageObservation) -> Optional[Finding]:
    """Check 4: Missing context around structured information.

    Fires when structured data defines a key entity (Product, Event, Course, Recipe, etc.),
    but the entity name is completely missing from visible headings and body text,
    and visible text is sparse (< 25 words).
    """
    entities = _collect_json_ld_entities(obs)
    if not entities:
        return None

    word_count = len(obs.visible_text.split())
    if word_count >= 25:
        return None

    # Check if a primary named entity exists in JSON-LD
    target_types = {
        "product",
        "event",
        "course",
        "recipe",
        "article",
        "newsarticle",
        "service",
        "softwareapplication",
    }

    visible_lower = obs.visible_text.lower()
    headings_lower = [h.lower() for h in obs.h1 + obs.h2 + obs.h3]
    title_lower = (obs.title or "").lower()

    for entity in entities:
        raw_type = str(entity.get("@type") or "").lower()
        if raw_type not in target_types:
            continue

        raw_name = entity.get("name")
        if not isinstance(raw_name, str) or len(raw_name.strip()) < 3:
            continue

        name_str = raw_name.strip()
        # Extract meaningful alphanumeric tokens of length >= 4
        tokens = [t.lower() for t in re.findall(r"\b[A-Za-z0-9]{4,}\b", name_str)]
        if not tokens:
            continue

        # Check if any significant token appears in visible text, headings, or title
        token_found = any(
            t in visible_lower or any(t in h for h in headings_lower) or t in title_lower
            for t in tokens
        )

        if not token_found:
            evidence = (
                f"Structured data (JSON-LD) declares entity '{name_str}' ({entity.get('@type')}), "
                "but the declared entity name does not appear in visible page headings or body text, "
                f"and total visible text is extremely limited ({word_count} words). "
                "Automated agents seeking to ground or corroborate structured claims find no "
                "supporting human-readable context on the page."
            )
            return Finding(
                id="MA-CTX-001",
                title="Structured information lacks visible page context",
                severity="medium",
                evidence=evidence,
                suggested_action=SuggestedAction(
                    summary=(
                        "Ensure key entity names and attributes declared in structured data "
                        "are corroborated by visible headings and body text on the page."
                    ),
                    priority=2,
                ),
                priority=2,
                confidence="medium",
                details={
                    "entity_type": entity.get("@type"),
                    "entity_name": name_str,
                    "visible_word_count": word_count,
                },
            )

    return None


def audit_machine_answerability(obs: PageObservation) -> list[Finding]:
    """Execute the Machine Answerability Audit on a PageObservation.

    Args:
        obs: PageObservation populated by the observation layer.

    Returns:
        List of Finding objects representing evidence-backed issues.
    """
    findings: list[Finding] = []

    # If the page fetch failed or no content was retrieved, return empty findings
    # (crawl/render failures belong to the crawl/render audit layer)
    if not obs.is_success:
        return findings

    # Check 1: Important information appears to be image-dependent
    img_finding = _check_image_dependency(obs)
    if img_finding is not None:
        findings.append(img_finding)

    # Check 2: Very low textual representation on structured page
    txt_finding = _check_low_text_representation(obs)
    if txt_finding is not None:
        findings.append(txt_finding)

    # Check 3: Structured data representation conflict
    rep_findings = _check_representation_conflict(obs)
    findings.extend(rep_findings)

    # Check 4: Missing context around structured information
    ctx_finding = _check_missing_structured_context(obs)
    if ctx_finding is not None:
        findings.append(ctx_finding)

    return findings
