"""Fact Inventory & Evidence Consistency Layer.

Provides lightweight provenance-aware evidence units (EvidenceUnit) and a Fact Inventory
(FactInventory) to represent, normalize, and evaluate consistency across page surfaces
(visible text, headings, tables, JSON-LD, and internal pages).

Design Principles:
- Evidence & Provenance: Every fact records entity, attribute, normalized value, raw text,
  source URL, source type, confidence, and temporal information.
- Conservative Normalization: Parses price, availability, dates, and physical quantities safely.
  Preserves raw strings and flags ambiguous/range prefixes ("From $999") to prevent false conflicts.
- Consistency Classification: Classifies evidence into MATCH, CONFLICT, HISTORICAL, AMBIGUOUS,
  or INSUFFICIENT_EVIDENCE.
- Read-only, deterministic, provider-neutral, lightweight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Literal, Optional

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

ConsistencyState = Literal[
    "MATCH",
    "CONFLICT",
    "HISTORICAL",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
]

# Currency symbol/code regex mapping
CURRENCY_MAP: dict[str, str] = {
    "$": "USD",
    "usd": "USD",
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}

PRICE_PATTERN: re.Pattern[str] = re.compile(
    r"(?:from|starting\s+at)?\s*(₹|Rs\.?|\$|€|£|INR|USD|EUR|GBP)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

DATE_PATTERN: re.Pattern[str] = re.compile(
    r"\b(20[2-3][0-9])\b",
)

WEIGHT_PATTERN: re.Pattern[str] = re.compile(
    r"\b([0-9]+(?:\.[0-9]+)?)\s*(kg|g|grams|kilograms)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceUnit:
    """Immutable evidence unit capturing a single observed factual claim with provenance.

    Attributes:
        entity: Identified entity name (e.g., 'Product X').
        attribute: Attribute name (e.g., 'price', 'availability', 'weight', 'date').
        value: Normalized comparable value representation.
        raw_value: Exact raw string observed on the page.
        source: Source URL or path provenance.
        source_type: Source location type ('visible_text', 'heading', 'table', 'json_ld', 'metadata').
        confidence: Extraction confidence ('high', 'medium', 'low').
        timestamp: Extracted date/year string if available (e.g., '2025', '2026'), else None.
        is_range_or_prefix: True if value is prefixed by 'From', 'Starting at', or a range.
        details: Optional additional diagnostic details.
    """

    entity: str
    attribute: str
    value: Any
    raw_value: str
    source: str
    source_type: Literal["visible_text", "heading", "table", "json_ld", "metadata", "internal_page"]
    confidence: Literal["high", "medium", "low"] = "high"
    timestamp: Optional[str] = None
    is_range_or_prefix: bool = False
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Attribute Normalization Helpers
# ---------------------------------------------------------------------------

def normalize_price(raw_str: Any, default_currency: str = "USD") -> tuple[Optional[str], Optional[int], bool]:
    """Parse raw text or numeric value to extract currency code, integer amount, and range/prefix flag.

    Returns:
        tuple: (currency_code, integer_amount, is_range_or_prefix)
        e.g., ("USD", 999, False) or ("INR", 5499, True)
    """
    if raw_str is None:
        return None, None, False
    s = str(raw_str).strip()
    if not s:
        return None, None, False

    raw_lower = s.lower()
    is_prefix = any(prefix in raw_lower for prefix in ("from", "starting at", "starting from", "up to"))

    # Currency regex matching prefix or suffix currency strings.
    # We use two passes to handle both comma-formatted (12,999) and plain (12999) numbers
    # correctly, since regex alternation picks the first match which can truncate.
    currency_pat = r"(₹|Rs\.?|\$|€|£|INR|USD|EUR|GBP)"
    # Pattern 1: comma-formatted numbers (e.g., 12,999 or 1,200,000)
    comma_num = r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?"
    # Pattern 2: plain numbers (e.g., 12999 or 999.99)
    plain_num = r"[0-9]+(?:\.[0-9]+)?"
    # Combined: try comma-formatted first (requires at least one comma group), then plain
    num_group = f"({comma_num}|{plain_num})"
    pattern = re.compile(
        f"{currency_pat}?\\s*{num_group}\\s*{currency_pat}?",
        re.IGNORECASE,
    )
    match = pattern.search(s)
    if not match or not match.group(2):
        return None, None, is_prefix

    curr_symbol = (match.group(1) or match.group(3) or "").lower().rstrip(".")
    currency = CURRENCY_MAP.get(curr_symbol, default_currency)

    num_str = re.sub(r"[^\d.]", "", match.group(2))
    try:
        integer_amount = int(float(num_str))
        return currency, integer_amount, is_prefix
    except (ValueError, OverflowError):
        return None, None, is_prefix


def normalize_availability(raw_str: str) -> Optional[str]:
    """Normalize raw availability text to 'instock' or 'outofstock'."""
    if not raw_str:
        return None
    raw_lower = raw_str.strip().lower()

    if "instock" in raw_lower or "in stock" in raw_lower or "available now" in raw_lower:
        if "outofstock" not in raw_lower and "out of stock" not in raw_lower and "unavailable" not in raw_lower:
            return "instock"
    if "outofstock" in raw_lower or "out of stock" in raw_lower or "sold out" in raw_lower or "unavailable" in raw_lower:
        return "outofstock"

    return None


def normalize_numeric_spec(raw_str: str) -> tuple[Optional[str], Optional[float]]:
    """Normalize weight/measurement strings to standard units (e.g. grams).

    Example:
        '1.2 kg' -> ('g', 1200.0)
        '1200 g' -> ('g', 1200.0)
    """
    if not raw_str:
        return None, None

    match = WEIGHT_PATTERN.search(raw_str)
    if not match:
        return None, None

    val = float(match.group(1))
    unit = match.group(2).lower()

    if unit in ("kg", "kilograms"):
        return "g", round(val * 1000.0, 2)
    elif unit in ("g", "grams"):
        return "g", round(val, 2)

    return None, None


# ---------------------------------------------------------------------------
# Fact Inventory Class
# ---------------------------------------------------------------------------

class FactInventory:
    """Provenance-aware fact inventory storing and classifying EvidenceUnits."""

    def __init__(self) -> None:
        self._units: list[EvidenceUnit] = []

    def add_unit(self, unit: EvidenceUnit) -> None:
        """Add an EvidenceUnit to the inventory."""
        self._units.append(unit)

    def get_units(
        self,
        entity: Optional[str] = None,
        attribute: Optional[str] = None,
        source_type: Optional[str] = None,
    ) -> list[EvidenceUnit]:
        """Filter evidence units by entity, attribute, or source_type."""
        filtered = self._units
        if entity is not None:
            filtered = [u for u in filtered if u.entity.lower() == entity.lower()]
        if attribute is not None:
            filtered = [u for u in filtered if u.attribute.lower() == attribute.lower()]
        if source_type is not None:
            filtered = [u for u in filtered if u.source_type == source_type]
        return filtered

    @property
    def all_units(self) -> list[EvidenceUnit]:
        return list(self._units)

    def evaluate_consistency(self, entity: str, attribute: str) -> tuple[ConsistencyState, str]:
        """Evaluate consistency classification for an (entity, attribute) pair.

        Returns:
            tuple: (ConsistencyState, explanation_string)
        """
        units = self.get_units(entity=entity, attribute=attribute)
        if len(units) <= 1:
            return "INSUFFICIENT_EVIDENCE", f"Fewer than 2 sources observed for attribute '{attribute}'."

        # Filter out range/prefix values if exact values exist
        exact_units = [u for u in units if not u.is_range_or_prefix]
        eval_units = exact_units if len(exact_units) >= 2 else units

        # Check for temporal differences (current vs historical)
        timestamps = {u.timestamp for u in eval_units if u.timestamp is not None}
        if len(timestamps) > 1:
            return "HISTORICAL", f"Observed values belong to different timeframes ({', '.join(sorted(timestamps))})."

        # Compare values across distinct source types or sources
        unique_values = {u.value for u in eval_units if u.value is not None}

        if len(unique_values) <= 1:
            return "MATCH", f"All {len(eval_units)} sources agree on attribute '{attribute}'."

        if len(unique_values) > 1:
            # Check if values are genuinely comparable
            return "CONFLICT", f"Conflicting values observed for attribute '{attribute}': {unique_values}."

        return "AMBIGUOUS", f"Attribute '{attribute}' values could not be confidently compared."


# ---------------------------------------------------------------------------
# Extraction & Audit Logic
# ---------------------------------------------------------------------------

def _collect_json_ld_dicts(obs: PageObservation) -> list[dict[str, Any]]:
    """Traverse all valid JSON-LD blocks to gather top-level and nested entity dictionaries."""
    entities: list[dict[str, Any]] = []

    def _traverse(item: Any) -> None:
        if isinstance(item, dict):
            if "@graph" in item and isinstance(item["@graph"], list):
                for sub in item["@graph"]:
                    _traverse(sub)
            entities.append(item)
            for v in item.values():
                if isinstance(v, (dict, list)):
                    _traverse(v)
        elif isinstance(item, list):
            for sub in item:
                _traverse(sub)

    for block in obs.json_ld:
        if block.is_valid and block.parsed_data is not None:
            _traverse(block.parsed_data)

    return entities


def extract_fact_inventory(obs_input: PageObservation | list[PageObservation]) -> FactInventory:
    """Extract a FactInventory from one or more PageObservations."""
    inventory = FactInventory()
    observations = [obs_input] if isinstance(obs_input, PageObservation) else obs_input

    for obs in observations:
        if not obs.is_success:
            continue

        source_url = obs.final_url or obs.requested_url

        # Determine primary entity name
        entity_name = (obs.title or "").strip()
        if obs.h1 and len(obs.h1[0].strip()) > 2:
            entity_name = obs.h1[0].strip()
        if not entity_name:
            entity_name = "Page Entity"

        # Extract date timestamp if present in text/title
        extracted_year: Optional[str] = None
        year_match = DATE_PATTERN.search(entity_name + " " + obs.visible_text)
        if year_match:
            extracted_year = year_match.group(1)

        # 1. Extract JSON-LD Facts
        json_entities = _collect_json_ld_dicts(obs)
        for j_entity in json_entities:
            j_name = str(j_entity.get("name") or entity_name).strip()

            # JSON-LD Price
            candidates = []
            offers = j_entity.get("offers")
            if isinstance(offers, dict):
                candidates.append(offers)
            elif isinstance(offers, list):
                candidates.extend(o for o in offers if isinstance(o, dict))
            if "price" in j_entity:
                candidates.append(j_entity)

            for c in candidates:
                raw_price = c.get("price")
                if raw_price is not None:
                    json_curr = str(c.get("priceCurrency") or "").strip() or "USD"
                    curr, amt, is_prefix = normalize_price(raw_price, default_currency=json_curr)
                    if amt is not None:
                        inventory.add_unit(
                            EvidenceUnit(
                                entity=j_name,
                                attribute="price",
                                value=(curr, amt),
                                raw_value=str(raw_price),
                                source=source_url,
                                source_type="json_ld",
                                confidence="high",
                                timestamp=extracted_year,
                                is_range_or_prefix=is_prefix,
                                details={"currency": curr, "amount": amt},
                            )
                        )

            # JSON-LD Availability
            for c in candidates:
                raw_avail = c.get("availability")
                if raw_avail:
                    norm_avail = normalize_availability(str(raw_avail))
                    if norm_avail:
                        inventory.add_unit(
                            EvidenceUnit(
                                entity=j_name,
                                attribute="availability",
                                value=norm_avail,
                                raw_value=str(raw_avail),
                                source=source_url,
                                source_type="json_ld",
                                confidence="high",
                                timestamp=extracted_year,
                                details={"availability": norm_avail},
                            )
                        )

        # 2. Extract Visible Text Price Facts
        visible_price_matches = PRICE_PATTERN.finditer(obs.visible_text)
        for m in visible_price_matches:
            raw_str = m.group(0).strip()
            curr, amt, is_prefix = normalize_price(raw_str)
            if amt is not None:
                inventory.add_unit(
                    EvidenceUnit(
                        entity=entity_name,
                        attribute="price",
                        value=(curr, amt),
                        raw_value=raw_str,
                        source=source_url,
                        source_type="visible_text",
                        confidence="high",
                        timestamp=extracted_year,
                        is_range_or_prefix=is_prefix,
                        details={"currency": curr, "amount": amt},
                    )
                )

        # 3. Extract Visible Availability Facts
        if re.search(r"\b(out of stock|sold out|unavailable)\b", obs.visible_text, re.IGNORECASE):
            inventory.add_unit(
                EvidenceUnit(
                    entity=entity_name,
                    attribute="availability",
                    value="outofstock",
                    raw_value="Out of Stock",
                    source=source_url,
                    source_type="visible_text",
                    confidence="high",
                    timestamp=extracted_year,
                )
            )
        elif re.search(r"\b(in stock|available now)\b", obs.visible_text, re.IGNORECASE):
            inventory.add_unit(
                EvidenceUnit(
                    entity=entity_name,
                    attribute="availability",
                    value="instock",
                    raw_value="In Stock",
                    source=source_url,
                    source_type="visible_text",
                    confidence="high",
                    timestamp=extracted_year,
                )
            )

        # 4. Extract Table Numeric Specifications (e.g. Weight)
        for table in obs.tables:
            for row in table.rows:
                row_str = " ".join(row)
                unit_name, norm_val = normalize_numeric_spec(row_str)
                if norm_val is not None:
                    inventory.add_unit(
                        EvidenceUnit(
                            entity=entity_name,
                            attribute="weight",
                            value=(unit_name, norm_val),
                            raw_value=row_str,
                            source=source_url,
                            source_type="table",
                            confidence="high",
                            timestamp=extracted_year,
                        )
                    )

    return inventory


def audit_evidence_consistency(obs: PageObservation) -> list[Finding]:
    """Execute the Evidence + Consistency Audit using FactInventory.

    Generates findings for price conflicts, availability conflicts, and attribute conflicts.
    """
    if not obs.is_success:
        return []

    findings: list[Finding] = []
    inventory = extract_fact_inventory(obs)

    # 1. Evaluate Price Consistency
    price_json_units = inventory.get_units(attribute="price", source_type="json_ld")
    price_vis_units = inventory.get_units(attribute="price", source_type="visible_text")

    if price_json_units and price_vis_units:
        exact_vis_units = [u for u in price_vis_units if not u.is_range_or_prefix]
        vis_eval_units = exact_vis_units if exact_vis_units else price_vis_units

        j_nums = {u.value[1] for u in price_json_units if isinstance(u.value, tuple) and u.value[1] is not None}
        v_nums = {u.value[1] for u in vis_eval_units if isinstance(u.value, tuple) and u.value[1] is not None}

        # Report conflict only if visible prices exist and NONE of the numeric amounts match JSON-LD
        if j_nums and v_nums and not j_nums.intersection(v_nums):
            j_unit = price_json_units[0]
            vis_strs = [u.raw_value for u in vis_eval_units]
            evidence = (
                f"Structured JSON-LD data specifies price '{j_unit.raw_value}' "
                f"({j_unit.details.get('currency') or 'unspecified currency'}), whereas visible page text "
                f"explicitly shows conflicting price(s): {', '.join(vis_strs)}. "
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
                        "json_ld_price": j_unit.raw_value,
                        "json_ld_currency": j_unit.details.get("currency"),
                        "visible_prices": vis_strs,
                    },
                )
            )

    # 2. Evaluate Availability Consistency
    avail_json_units = inventory.get_units(attribute="availability", source_type="json_ld")
    avail_vis_units = inventory.get_units(attribute="availability", source_type="visible_text")

    if avail_json_units and avail_vis_units:
        j_avail = avail_json_units[0].value
        v_avail = avail_vis_units[0].value

        if j_avail != v_avail:
            if j_avail == "instock" and v_avail == "outofstock":
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
                            summary="Synchronize stock status between structured data and visible text.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "json_ld_availability": avail_json_units[0].raw_value,
                            "visible_status": "Out of Stock",
                        },
                    )
                )
            elif j_avail == "outofstock" and v_avail == "instock":
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
                            summary="Synchronize stock status between structured data and visible text.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={
                            "json_ld_availability": avail_json_units[0].raw_value,
                            "visible_status": "In Stock",
                        },
                    )
                )

    return findings
