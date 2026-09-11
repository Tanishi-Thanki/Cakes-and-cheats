"""Freshness & Corroboration Audit.

Evaluates whether facts exposed on a page are internally consistent and whether
available freshness signals give an automated agent sufficient reason to treat
those facts as current and authoritative.

Design Principles:
- Single-page internal corroboration: evaluates consistency within the observed page.
- Evidence before inference: reports only deterministic, demonstrable discrepancies.
- Conservative severity: avoids flagging normal minimal pages or historical articles.
"""

from __future__ import annotations

import re
from typing import Any, Optional
import urllib.parse

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# Regex to find currency amounts in visible text
PRICE_REGEX: re.Pattern[str] = re.compile(
    r"(?:₹|Rs\.?|\$|€|£|INR|USD)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

# Standardized attribute keys we inspect for consistency
STANDARD_ATTRIBUTES: frozenset[str] = frozenset({
    "color",
    "colour",
    "material",
    "size",
    "weight",
    "capacity",
    "storage",
    "model",
})


def _collect_json_ld_entities(obs: PageObservation) -> list[dict[str, Any]]:
    """Traverse all valid JSON-LD blocks to gather entity dictionaries."""
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
    """Extract visible currency strings and their normalized numeric integer value."""
    matches = PRICE_REGEX.finditer(visible_text)
    results: list[tuple[str, str]] = []
    for m in matches:
        raw = m.group(0).strip()
        num_str = re.sub(r"[^\d.]", "", m.group(1))
        integer_part = num_str.split(".")[0]
        if integer_part:
            results.append((raw, integer_part))
    return results


def _clean_attribute_value(val: str) -> str:
    """Normalize attribute text for conservative, deterministic comparison."""
    cleaned = val.lower().strip()
    # Strip common qualifying fluff
    cleaned = re.sub(r"\b(genuine|pure|100%|authentic|official)\b", "", cleaned)
    # Standardize full unit names to standard abbreviations
    cleaned = re.sub(r"\bgrams?\b", "g", cleaned)
    cleaned = re.sub(r"\bkilograms?\b", "kg", cleaned)
    cleaned = re.sub(r"\bgigabytes?\b", "gb", cleaned)
    cleaned = re.sub(r"\bterabytes?\b", "tb", cleaned)
    cleaned = re.sub(r"\bmillimeters?\b", "mm", cleaned)
    # Remove whitespace between numbers and unit letters (e.g. '285 g' -> '285g')
    cleaned = re.sub(r"(\d+)\s+([a-z]+)", r"\1\2", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _are_attribute_values_conflicting(val1: str, val2: str) -> bool:
    """Determine whether two explicit attribute values contradict each other."""
    c1 = _clean_attribute_value(val1)
    c2 = _clean_attribute_value(val2)
    if not c1 or not c2:
        return False
    # If equal or one is a substring/superstring of the other, no conflict
    if c1 == c2 or c1 in c2 or c2 in c1:
        return False
    return True


def _check_representation_conflicts(obs: PageObservation) -> list[Finding]:
    """Check 1: Representation conflicts between JSON-LD and visible text."""
    findings: list[Finding] = []
    entities = _collect_json_ld_entities(obs)
    if not entities:
        return findings

    visible_prices = _extract_visible_prices(obs.visible_text)
    visible_numeric_set = {num for _, num in visible_prices}

    # 1. Price representation conflict
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

            # Only report if visible prices exist and NONE match JSON-LD price
            if visible_numeric_set and json_integer not in visible_numeric_set:
                raw_visible_strs = [raw for raw, _ in visible_prices]
                currency = str(c.get("priceCurrency") or "").strip()
                evidence = (
                    f"Visible page contains '{raw_visible_strs[0]}' while Product JSON-LD specifies "
                    f"'{raw_price}' ({currency or 'unspecified currency'})."
                )
                findings.append(
                    Finding(
                        id="FC-REP-001",
                        title="Conflicting price representations",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary=(
                                "Establish the authoritative current price and synchronize the "
                                "visible and structured representations."
                            ),
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "visible_price": raw_visible_strs[0],
                            "json_ld_price": raw_price,
                            "currency": currency,
                        },
                    )
                )
                break
        if findings:
            break

    # 2. Availability representation conflict
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
                findings.append(
                    Finding(
                        id="FC-REP-002",
                        title="Conflicting availability representations",
                        severity="high",
                        evidence=(
                            "Visible page states 'Out of Stock' while structured JSON-LD specifies 'InStock'. "
                            "Inconsistent representations reduce confidence in which state is authoritative."
                        ),
                        suggested_action=SuggestedAction(
                            summary="Establish the authoritative inventory state and reconcile visible and structured representations.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                    )
                )
                break
            elif is_json_outofstock and has_visible_instock and not has_visible_outofstock:
                findings.append(
                    Finding(
                        id="FC-REP-002",
                        title="Conflicting availability representations",
                        severity="high",
                        evidence=(
                            "Visible page states 'In Stock' while structured JSON-LD specifies 'OutOfStock'. "
                            "Inconsistent representations reduce confidence in which state is authoritative."
                        ),
                        suggested_action=SuggestedAction(
                            summary="Establish the authoritative inventory state and reconcile visible and structured representations.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                    )
                )
                break

    return findings


def _check_attribute_inconsistency(obs: PageObservation) -> list[Finding]:
    """Check 2: Contradictory product attributes between structured data and page tables/text."""
    findings: list[Finding] = []
    entities = _collect_json_ld_entities(obs)
    if not entities:
        return findings

    # Extract JSON-LD attributes
    json_attrs: dict[str, str] = {}
    for entity in entities:
        for key in STANDARD_ATTRIBUTES:
            val = entity.get(key)
            if isinstance(val, str) and val.strip():
                norm_k = "color" if key in ("color", "colour") else ("capacity" if key in ("capacity", "storage") else key)
                json_attrs[norm_k] = val.strip()

        add_props = entity.get("additionalProperty")
        if isinstance(add_props, list):
            for prop in add_props:
                if isinstance(prop, dict):
                    name = str(prop.get("name") or "").strip().lower()
                    val = str(prop.get("value") or "").strip()
                    if name and val:
                        norm_k = "color" if name in ("color", "colour") else ("capacity" if name in ("capacity", "storage") else name)
                        if norm_k in STANDARD_ATTRIBUTES:
                            json_attrs[norm_k] = val

    if not json_attrs:
        return findings

    # Extract table specifications (e.g. 2-column key-value rows)
    table_attrs: dict[str, str] = {}
    for table in obs.tables:
        for row in table.rows:
            if len(row) >= 2:
                key_candidate = row[0].strip().lower()
                val_candidate = row[1].strip()
                norm_k = "color" if key_candidate in ("color", "colour") else ("capacity" if key_candidate in ("capacity", "storage") else key_candidate)
                if norm_k in STANDARD_ATTRIBUTES and val_candidate:
                    table_attrs[norm_k] = val_candidate

    # Compare common attribute keys
    for attr_name, json_val in json_attrs.items():
        if attr_name in table_attrs:
            table_val = table_attrs[attr_name]
            if _are_attribute_values_conflicting(json_val, table_val):
                evidence = (
                    f"Structured data specifies {attr_name} '{json_val}' while specification "
                    f"table explicitly states '{table_val}'. Contradictory attribute specifications "
                    "create ambiguity for automated agents attempting to answer specific product queries."
                )
                findings.append(
                    Finding(
                        id="FC-ATTR-001",
                        title="Conflicting product attribute representations",
                        severity="medium",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary=(
                                f"Verify the correct {attr_name} specification and synchronize "
                                "structured metadata with visible specifications."
                            ),
                            priority=2,
                        ),
                        priority=2,
                        confidence="high",
                        details={
                            "attribute": attr_name,
                            "json_ld_value": json_val,
                            "table_value": table_val,
                        },
                    )
                )

    return findings


def _check_entity_ambiguity(obs: PageObservation) -> list[Finding]:
    """Check 3: Conflicting entity identity or brand signals."""
    findings: list[Finding] = []
    entities = _collect_json_ld_entities(obs)
    if not entities:
        return findings

    h1_text = " ".join(obs.h1).strip()
    title_text = (obs.title or "").strip()
    visible_headline = h1_text or title_text

    # Extract explicit visible brand if stated as "Brand: X"
    visible_brand_match = re.search(r"\bbrand\s*:\s*([A-Za-z0-9\s-]+)", obs.visible_text, re.IGNORECASE)
    visible_brand = visible_brand_match.group(1).strip() if visible_brand_match else ""

    for entity in entities:
        # 1. Product Name Identity Conflict
        raw_name = entity.get("name")
        if isinstance(raw_name, str) and visible_headline:
            json_name = raw_name.strip()
            # Extract significant tokens of length >= 4
            json_tokens = {w.lower() for w in re.findall(r"\b[A-Za-z0-9]{4,}\b", json_name)}
            headline_tokens = {w.lower() for w in re.findall(r"\b[A-Za-z0-9]{4,}\b", visible_headline)}

            # Only flag if both have at least 2 significant tokens and ZERO token overlap
            if len(json_tokens) >= 2 and len(headline_tokens) >= 2:
                if not (json_tokens & headline_tokens):
                    # Ensure the JSON-LD name does not appear anywhere in visible text
                    if json_name.lower() not in obs.visible_text.lower():
                        evidence = (
                            f"Page title/heading identifies entity as '{visible_headline}', but structured "
                            f"JSON-LD declares entity name as '{json_name}'. Different identity representations "
                            "may make it harder for automated systems to determine which entity the page represents."
                        )
                        findings.append(
                            Finding(
                                id="FC-ENT-001",
                                title="Conflicting entity identity signals",
                                severity="medium",
                                evidence=evidence,
                                suggested_action=SuggestedAction(
                                    summary=(
                                        "Synchronize the primary entity name between visible headings and structured "
                                        "metadata to avoid entity ambiguity."
                                    ),
                                    priority=2,
                                ),
                                priority=2,
                                confidence="medium",
                                details={
                                    "visible_identity": visible_headline,
                                    "json_ld_name": json_name,
                                },
                            )
                        )
                        break

        # 2. Brand Conflict
        raw_brand = entity.get("brand")
        json_brand = ""
        if isinstance(raw_brand, str):
            json_brand = raw_brand.strip()
        elif isinstance(raw_brand, dict):
            json_brand = str(raw_brand.get("name") or "").strip()

        if json_brand and visible_brand:
            jb_clean = json_brand.lower()
            vb_clean = visible_brand.lower()
            if jb_clean != vb_clean and jb_clean not in vb_clean and vb_clean not in jb_clean:
                evidence = (
                    f"Structured JSON-LD declares brand as '{json_brand}', while page text "
                    f"identifies brand as '{visible_brand}'. Contradictory brand signals create "
                    "entity ambiguity for automated agents."
                )
                findings.append(
                    Finding(
                        id="FC-ENT-002",
                        title="Conflicting brand representations",
                        severity="medium",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary="Ensure consistent brand identification across structured JSON-LD and visible text.",
                            priority=2,
                        ),
                        priority=2,
                        confidence="high",
                        details={
                            "visible_brand": visible_brand,
                            "json_ld_brand": json_brand,
                        },
                    )
                )
                break

    return findings


def _check_freshness_signals(obs: PageObservation, reference_year: int = 2026) -> Optional[Finding]:
    """Check 4: Freshness signals on time-sensitive or transactional content.

    Evaluates whether time-sensitive or transactional information (pricing, inventory,
    or active offers) is supported by recency signals. An old timestamp on a transactional
    page is treated as a conservative heuristic indicating that freshness evidence is limited,
    NOT as proof that facts are definitively stale or invalid.

    Timeless or historical articles with old dates are explicitly permitted and not flagged.
    Pages without date metadata are not flagged as stale.
    """
    # 1. Determine if page is time-sensitive / transactional
    has_prices = bool(_extract_visible_prices(obs.visible_text))
    has_transactional_text = bool(
        re.search(
            r"\b(in stock|out of stock|add to cart|buy now|limited time offer|discount|sale ends)\b",
            obs.visible_text,
            re.IGNORECASE,
        )
    )

    entities = _collect_json_ld_entities(obs)
    has_json_offers = any(bool(e.get("offers")) for e in entities)

    is_time_sensitive = has_prices or has_transactional_text or has_json_offers
    if not is_time_sensitive:
        # Timeless or historical content: an old date is NOT a defect
        return None

    # Track specific observed time-sensitive indicators for transparent evidence
    observed_indicators: list[str] = []
    if has_prices:
        observed_indicators.append("pricing information")
    if has_transactional_text:
        observed_indicators.append("transactional or inventory indicators")
    if has_json_offers:
        observed_indicators.append("structured offers metadata")
    content_desc = ", ".join(observed_indicators) if observed_indicators else "transactional details"

    # 2. Extract explicit date signals from JSON-LD and visible text
    date_signals: list[tuple[str, int]] = []  # (raw_date_string, year)

    # Check JSON-LD date fields
    for entity in entities:
        for field_name in ("dateModified", "datePublished", "uploadDate", "priceValidUntil"):
            raw_date = entity.get(field_name)
            if isinstance(raw_date, str) and raw_date.strip():
                year_match = re.search(r"\b(20[0-2][0-9]|19[0-9]{2})\b", raw_date)
                if year_match:
                    date_signals.append((raw_date.strip(), int(year_match.group(1))))

    # Check visible text for explicit "Last updated: YYYY" or "Updated: YYYY"
    visible_date_match = re.search(
        r"\b(?:last\s+updated|updated\s+on|modified)\s*:\s*([A-Za-z0-9,\s-]+)",
        obs.visible_text,
        re.IGNORECASE,
    )
    if visible_date_match:
        raw_v_date = visible_date_match.group(1).strip()
        y_match = re.search(r"\b(20[0-2][0-9]|19[0-9]{2})\b", raw_v_date)
        if y_match:
            date_signals.append((raw_v_date, int(y_match.group(1))))

    if not date_signals:
        # Normal product page without explicit date metadata should NOT automatically be declared stale
        return None

    # Sort to find the latest explicit timestamp
    latest_signal = max(date_signals, key=lambda s: s[1])
    latest_raw, latest_year = latest_signal

    # Deterministic heuristic threshold: a timestamp from >= 3 years ago (reference_year - 3)
    # serves as a heuristic signal of limited recency evidence, NOT definitive proof of staleness.
    if latest_year <= reference_year - 3:
        evidence = (
            f"Page presents time-sensitive content ({content_desc}) but carries an explicit timestamp of "
            f"'{latest_raw}' (year {latest_year}). When active transactional details accompany an older "
            "timestamp, automated systems have limited evidence to establish that facts such as pricing, "
            "terms, or inventory remain current. This timestamp represents limited freshness evidence rather "
            "than definitive proof of staleness, but it may reduce confidence in whether the information is up to date."
        )
        return Finding(
            id="FC-FRSH-001",
            title="Limited freshness evidence for time-sensitive information",
            severity="low",
            evidence=evidence,
            suggested_action=SuggestedAction(
                summary=(
                    "Maintain explicit current modification dates and periodically review time-sensitive "
                    "facts (such as pricing, availability, and promotional terms) to ensure automated "
                    "systems can reliably establish recency."
                ),
                priority=3,
            ),
            priority=3,
            confidence="medium",
            details={
                "observed_content": observed_indicators,
                "explicit_timestamp": latest_raw,
                "detected_year": latest_year,
                "reference_year": reference_year,
                "heuristic_threshold_years": 3,
            },
        )

    return None


def audit_freshness_corroboration(
    obs: PageObservation,
    reference_year: int = 2026,
) -> list[Finding]:
    """Execute the Freshness & Corroboration Audit on a PageObservation.

    Args:
        obs: PageObservation object populated by the observation layer.
        reference_year: Baseline year for freshness evaluation (default: 2026).

    Returns:
        List of Finding objects representing detected consistency and freshness issues.
    """
    findings: list[Finding] = []

    # If request failed or page has no HTML content, return empty findings
    if not obs.is_success:
        return findings

    # Check 1: Representation conflicts (Price, Availability)
    rep_findings = _check_representation_conflicts(obs)
    findings.extend(rep_findings)

    # Check 2: Attribute inconsistency (Color, Material, Weight, etc.)
    attr_findings = _check_attribute_inconsistency(obs)
    findings.extend(attr_findings)

    # Check 3: Entity identity and brand ambiguity
    ent_findings = _check_entity_ambiguity(obs)
    findings.extend(ent_findings)

    # Check 4: Freshness signals on time-sensitive content
    fresh_finding = _check_freshness_signals(obs, reference_year=reference_year)
    if fresh_finding is not None:
        findings.append(fresh_finding)

    return findings
