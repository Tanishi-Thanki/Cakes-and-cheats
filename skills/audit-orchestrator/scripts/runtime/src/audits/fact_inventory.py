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
    "FUTURE",
    "EXPIRED",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
]

TemporalQualifier = Literal["CURRENT", "HISTORICAL", "FUTURE", "EXPIRED", "UNKNOWN"]

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
    r"(?:from|starting\s+at)?\s*(?:(₹|Rs\.?|\$|€|£|INR|USD|EUR|GBP)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)|([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*(₹|Rs\.?|\$|€|£|INR|USD|EUR|GBP))",
    re.IGNORECASE,
)

DATE_PATTERN: re.Pattern[str] = re.compile(
    r"\b(20[2-3][0-9])\b",
)

DATE_ISO_PATTERN: re.Pattern[str] = re.compile(
    r"\b(\d{4})-(\d{2})-(\d{2})\b",
)

DATE_TEXT_PATTERN: re.Pattern[str] = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)

DATE_DAY_MONTH_PATTERN: re.Pattern[str] = re.compile(
    r"\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec),?\s+(\d{4})\b",
    re.IGNORECASE,
)

DATE_SLASH_PATTERN: re.Pattern[str] = re.compile(
    r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
)

WEIGHT_PATTERN: re.Pattern[str] = re.compile(
    r"\b([0-9]+(?:\.[0-9]+)?)\s*(kg|g|grams|kilograms)\b",
    re.IGNORECASE,
)

PHONE_PATTERN: re.Pattern[str] = re.compile(
    r"\b(?:\+?1[-.\s]?)?\(?([2-9]\d{2})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})\b",
)

HISTORICAL_INDICATORS: re.Pattern[str] = re.compile(
    r"\b(was|formerly|previous|previously|old|prior|historical|in 20[0-2][0-9]|as of 20[0-2][0-9]|archived)\b",
    re.IGNORECASE,
)

EXPIRED_INDICATORS: re.Pattern[str] = re.compile(
    r"\b(sale\s+until|valid\s+through|expired|offer\s+ended|ended|valid\s+until)\b",
    re.IGNORECASE,
)

FUTURE_INDICATORS: re.Pattern[str] = re.compile(
    r"\b(effective|starting|coming\s+soon|upcoming|from\s+202[7-9])\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EvidenceUnit:
    """Immutable evidence unit capturing a single observed factual claim with provenance.

    Attributes:
        entity: Identified entity name (e.g., 'Product X').
        attribute: Attribute name (e.g., 'price', 'availability', 'weight', 'date', 'phone', 'location').
        value: Normalized comparable value representation.
        raw_value: Exact raw string observed on the page.
        source: Source URL or path provenance.
        source_type: Source location type ('visible_text', 'heading', 'table', 'json_ld', 'metadata').
        confidence: Extraction confidence ('high', 'medium', 'low').
        timestamp: Extracted date/year string if available (e.g., '2025', '2026'), else None.
        is_range_or_prefix: True if value is prefixed by 'From', 'Starting at', or a range.
        details: Optional additional diagnostic details.
        source_classification: Origin classification ('FIRST_PARTY', 'EXTERNAL').
        temporal_qualifier: Temporal qualifier ('CURRENT', 'HISTORICAL', 'FUTURE', 'EXPIRED', 'UNKNOWN').
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
    source_classification: Literal["FIRST_PARTY", "EXTERNAL"] = "FIRST_PARTY"
    temporal_qualifier: TemporalQualifier = "UNKNOWN"


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


MONTH_LOOKUP: dict[str, str] = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09",
    "oct": "10", "nov": "11", "dec": "12",
}


def normalize_date(raw_str: Any) -> Optional[str]:
    """Parse raw text or date string to extract a standardized ISO date (YYYY-MM-DD) or year (YYYY)."""
    if raw_str is None:
        return None
    s = str(raw_str).strip()
    if not s:
        return None

    # 1. ISO format: YYYY-MM-DD
    match_iso = DATE_ISO_PATTERN.search(s)
    if match_iso:
        return f"{match_iso.group(1)}-{match_iso.group(2)}-{match_iso.group(3)}"

    # 2. Textual format: "June 30, 2026"
    match_text = DATE_TEXT_PATTERN.search(s)
    if match_text:
        month_str = MONTH_LOOKUP.get(match_text.group(1).lower(), "01")
        day_str = f"{int(match_text.group(2)):02d}"
        year_str = match_text.group(3)
        return f"{year_str}-{month_str}-{day_str}"

    # 3. Day Month format: "30 June 2026"
    match_day_month = DATE_DAY_MONTH_PATTERN.search(s)
    if match_day_month:
        day_str = f"{int(match_day_month.group(1)):02d}"
        month_str = MONTH_LOOKUP.get(match_day_month.group(2).lower(), "01")
        year_str = match_day_month.group(3)
        return f"{year_str}-{month_str}-{day_str}"

    # 4. Slash format: "06/30/2026"
    match_slash = DATE_SLASH_PATTERN.search(s)
    if match_slash:
        m_val, d_val, y_val = int(match_slash.group(1)), int(match_slash.group(2)), match_slash.group(3)
        if m_val <= 12 and d_val <= 31:
            return f"{y_val}-{m_val:02d}-{d_val:02d}"

    # 5. Fallback Year: "2026"
    match_year = DATE_PATTERN.search(s)
    if match_year:
        return match_year.group(1)

    return None


def normalize_phone(raw_str: Any) -> Optional[str]:
    """Normalize phone numbers to standard digit representation (e.g. 18005550199)."""
    if not raw_str:
        return None
    s = str(raw_str).strip()
    match = PHONE_PATTERN.search(s)
    if match:
        return f"1{match.group(1)}{match.group(2)}{match.group(3)}"
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 10:
        return digits[-10:]
    return None


def normalize_location(raw_str: Any) -> Optional[str]:
    """Normalize location or address strings for consistent comparison."""
    if not raw_str:
        return None
    s = str(raw_str).strip().lower()
    cleaned = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", s))
    return cleaned if cleaned else None


def _detect_temporal_qualifier(
    text_snippet: str,
    extracted_year: Optional[str] = None,
    audit_year: str = "2026",
) -> tuple[TemporalQualifier, Optional[str]]:
    """Determine temporal qualifier ('CURRENT', 'HISTORICAL', 'FUTURE', 'EXPIRED') from text snippet and year."""
    if not text_snippet and not extracted_year:
        return "CURRENT", extracted_year

    s_lower = (text_snippet or "").lower()

    if EXPIRED_INDICATORS.search(s_lower):
        match_y = DATE_PATTERN.search(s_lower)
        if match_y:
            y_str = match_y.group(1)
            if int(y_str) <= int(audit_year):
                return "EXPIRED", y_str
        return "EXPIRED", extracted_year

    if HISTORICAL_INDICATORS.search(s_lower):
        match_y = DATE_PATTERN.search(s_lower)
        y_str = match_y.group(1) if match_y else extracted_year
        return "HISTORICAL", y_str

    if FUTURE_INDICATORS.search(s_lower):
        match_y = DATE_PATTERN.search(s_lower)
        y_str = match_y.group(1) if match_y else extracted_year
        return "FUTURE", y_str

    if extracted_year:
        try:
            y = int(extracted_year)
            a_y = int(audit_year)
            if y < a_y:
                return "HISTORICAL", extracted_year
            elif y > a_y:
                return "FUTURE", extracted_year
        except ValueError:
            pass

    return "CURRENT", extracted_year


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

    def evaluate_consistency(
        self,
        entity: str,
        attribute: str,
        audit_timestamp: Optional[str] = "2026",
    ) -> tuple[ConsistencyState, str]:
        """Evaluate consistency classification for an (entity, attribute) pair.

        Returns:
            tuple: (ConsistencyState, explanation_string)
        """
        units = self.get_units(entity=entity, attribute=attribute)
        if len(units) <= 1:
            return "INSUFFICIENT_EVIDENCE", f"Fewer than 2 sources observed for attribute '{attribute}'."

        exact_units = [u for u in units if not u.is_range_or_prefix]
        eval_units = exact_units if len(exact_units) >= 1 else units

        historical_units = [u for u in eval_units if getattr(u, "temporal_qualifier", "UNKNOWN") == "HISTORICAL"]
        expired_units = [u for u in eval_units if getattr(u, "temporal_qualifier", "UNKNOWN") == "EXPIRED"]
        future_units = [u for u in eval_units if getattr(u, "temporal_qualifier", "UNKNOWN") == "FUTURE"]
        current_units = [u for u in eval_units if getattr(u, "temporal_qualifier", "UNKNOWN") in ("CURRENT", "UNKNOWN")]

        timestamps = {u.timestamp for u in eval_units if u.timestamp is not None}

        if historical_units or (len(timestamps) > 1 and any(u.timestamp != (audit_timestamp or "2026") for u in eval_units if u.timestamp)):
            return "HISTORICAL", f"Observed values belong to different timeframes ({', '.join(sorted(timestamps or {'historical'}))})."

        if expired_units:
            return "EXPIRED", f"Observed value differences for attribute '{attribute}' are attributable to an expired promotion."

        if future_units:
            return "FUTURE", f"Observed value differences for attribute '{attribute}' represent a future schedule."

        curr_eval = current_units if current_units else eval_units
        unique_values = {u.value for u in curr_eval if u.value is not None}

        if len(unique_values) <= 1:
            return "MATCH", f"All {len(curr_eval)} current sources agree on attribute '{attribute}'."

        if len(unique_values) > 1:
            return "CONFLICT", f"Conflicting values observed for attribute '{attribute}' in current timeframe: {unique_values}."

        return "AMBIGUOUS", f"Attribute '{attribute}' values could not be confidently compared."


@dataclass(frozen=True)
class AggregatedFact:
    """Site-level aggregated representation of a fact grouped by entity and attribute across pages.

    Attributes:
        entity: Identified entity name.
        attribute: Attribute name (e.g., 'price', 'availability', 'weight', 'title').
        normalized_value: Primary normalized value or tuple of distinct values.
        units: Tuple of underlying EvidenceUnits supporting this fact.
        sources: Tuple of distinct source page URLs contributing evidence.
        source_types: Tuple of distinct source location types ('visible_text', 'json_ld', 'metadata', etc.).
        cross_page_status: Status classification ('MATCH', 'CONFLICT', 'HISTORICAL', 'FUTURE', 'EXPIRED', 'AMBIGUOUS', 'SINGLE_PAGE', 'INSUFFICIENT_EVIDENCE').
        source_classifications: Tuple of distinct source classifications ('FIRST_PARTY', etc.).
    """

    entity: str
    attribute: str
    normalized_value: Any
    units: tuple[EvidenceUnit, ...]
    sources: tuple[str, ...]
    source_types: tuple[str, ...]
    cross_page_status: ConsistencyState | Literal["SINGLE_PAGE"]
    source_classifications: tuple[str, ...] = ("FIRST_PARTY",)

    @property
    def is_repeating(self) -> bool:
        """Returns True if evidence for this fact appears on more than one page."""
        return len(self.sources) > 1

    @property
    def is_single_page(self) -> bool:
        """Returns True if evidence for this fact appears on exactly one page."""
        return len(self.sources) == 1

    @property
    def has_visible_text(self) -> bool:
        """Returns True if any underlying evidence came from visible text."""
        return "visible_text" in self.source_types

    @property
    def has_structured_data(self) -> bool:
        """Returns True if any underlying evidence came from JSON-LD / structured data."""
        return "json_ld" in self.source_types

    @property
    def has_metadata(self) -> bool:
        """Returns True if any underlying evidence came from metadata."""
        return "metadata" in self.source_types


class SiteEvidenceInventory:
    """Site-level evidence inventory managing and aggregating evidence across multiple PageObservations."""

    def __init__(
        self,
        input_data: Optional[FactInventory | PageObservation | list[PageObservation]] = None,
    ) -> None:
        self._fact_inventory = FactInventory()
        self._pages: list[PageObservation] = []

        if isinstance(input_data, FactInventory):
            self._fact_inventory = input_data
        elif isinstance(input_data, PageObservation):
            self._pages = [input_data]
            self._fact_inventory = extract_fact_inventory(input_data)
        elif isinstance(input_data, list):
            valid_pages = [
                p for p in input_data
                if p is not None and isinstance(p, PageObservation) and getattr(p, "is_success", False)
            ]
            self._pages = valid_pages
            self._fact_inventory = extract_fact_inventory(valid_pages)

    @classmethod
    def from_page_observations(cls, pages: list[PageObservation] | PageObservation) -> SiteEvidenceInventory:
        """Construct a SiteEvidenceInventory from one or more PageObservations safely."""
        return cls(pages)

    @classmethod
    def from_fact_inventory(cls, inventory: FactInventory) -> SiteEvidenceInventory:
        """Construct a SiteEvidenceInventory from an existing FactInventory."""
        return cls(inventory)

    @property
    def fact_inventory(self) -> FactInventory:
        """Return the underlying FactInventory."""
        return self._fact_inventory

    @property
    def pages(self) -> list[PageObservation]:
        """Return the list of contributing PageObservation objects."""
        return list(self._pages)

    @property
    def contributing_pages(self) -> list[str]:
        """Return a sorted list of unique source page URLs present in the inventory."""
        urls = {u.source for u in self._fact_inventory.all_units if u.source}
        for page in self._pages:
            if page.final_url:
                urls.add(page.final_url)
            elif page.requested_url:
                urls.add(page.requested_url)
        return sorted(urls)

    @property
    def all_units(self) -> list[EvidenceUnit]:
        """Return all underlying EvidenceUnits across all pages."""
        return self._fact_inventory.all_units

    def get_aggregated_facts(
        self,
        entity: Optional[str] = None,
        attribute: Optional[str] = None,
    ) -> list[AggregatedFact]:
        """Group evidence units into AggregatedFacts by (entity, attribute)."""
        units = self._fact_inventory.all_units
        if entity is not None:
            units = [u for u in units if u.entity.lower() == entity.lower()]
        if attribute is not None:
            units = [u for u in units if u.attribute.lower() == attribute.lower()]

        groups: dict[tuple[str, str], list[EvidenceUnit]] = {}
        for u in units:
            key = (u.entity, u.attribute)
            if key not in groups:
                groups[key] = []
            groups[key].append(u)

        aggregated: list[AggregatedFact] = []
        for (ent_name, attr_name), group_units in groups.items():
            sources = tuple(sorted({u.source for u in group_units if u.source}))
            source_types = tuple(sorted({u.source_type for u in group_units if u.source_type}))
            source_classifications = tuple(sorted({getattr(u, "source_classification", "FIRST_PARTY") for u in group_units}))

            exact_units = [u for u in group_units if not u.is_range_or_prefix]
            eval_units = exact_units if exact_units else group_units
            unique_values = {u.value for u in eval_units if u.value is not None}

            if len(unique_values) == 1:
                norm_val = next(iter(unique_values))
            elif len(unique_values) > 1:
                norm_val = tuple(sorted([str(v) for v in unique_values]))
            elif group_units:
                norm_val = group_units[0].value
            else:
                norm_val = None

            eval_status, _ = self._fact_inventory.evaluate_consistency(ent_name, attr_name)

            if len(group_units) == 0:
                status: ConsistencyState | Literal["SINGLE_PAGE"] = "INSUFFICIENT_EVIDENCE"
            elif len(sources) == 1:
                if eval_status in ("CONFLICT", "HISTORICAL", "EXPIRED", "FUTURE", "AMBIGUOUS"):
                    status = eval_status
                else:
                    status = "SINGLE_PAGE"
            else:
                status = eval_status

            aggregated.append(
                AggregatedFact(
                    entity=ent_name,
                    attribute=attr_name,
                    normalized_value=norm_val,
                    units=tuple(group_units),
                    sources=sources,
                    source_types=source_types,
                    cross_page_status=status,
                    source_classifications=source_classifications,
                )
            )

        return aggregated

    @property
    def facts_across_pages(self) -> list[AggregatedFact]:
        """Return all aggregated facts across all pages."""
        return self.get_aggregated_facts()

    def get_repeating_facts(self) -> list[AggregatedFact]:
        """Which facts repeat across multiple pages?"""
        return [f for f in self.get_aggregated_facts() if f.is_repeating]

    @property
    def repeating_facts(self) -> list[AggregatedFact]:
        return self.get_repeating_facts()

    def get_single_page_facts(self) -> list[AggregatedFact]:
        """Which facts occur on only one page?"""
        return [f for f in self.get_aggregated_facts() if f.is_single_page]

    @property
    def single_page_facts(self) -> list[AggregatedFact]:
        return self.get_single_page_facts()

    def get_visible_text_facts(self) -> list[AggregatedFact]:
        """Which facts have visible-text evidence?"""
        return [f for f in self.get_aggregated_facts() if f.has_visible_text]

    @property
    def visible_text_facts(self) -> list[AggregatedFact]:
        return self.get_visible_text_facts()

    def get_structured_data_facts(self) -> list[AggregatedFact]:
        """Which facts have structured-data (JSON-LD) evidence?"""
        return [f for f in self.get_aggregated_facts() if f.has_structured_data]

    @property
    def structured_data_facts(self) -> list[AggregatedFact]:
        return self.get_structured_data_facts()

    def get_metadata_facts(self) -> list[AggregatedFact]:
        """Which facts have metadata evidence?"""
        meta_facts = [f for f in self.get_aggregated_facts() if f.has_metadata]
        if meta_facts:
            return meta_facts

        page_meta_units: list[EvidenceUnit] = []
        for page in self._pages:
            source_url = page.final_url or page.requested_url
            ent_name = (page.title or "Page Entity").strip()
            if page.title:
                page_meta_units.append(
                    EvidenceUnit(
                        entity=ent_name,
                        attribute="title",
                        value=page.title,
                        raw_value=page.title,
                        source=source_url,
                        source_type="metadata",
                        confidence="high",
                        source_classification="FIRST_PARTY",
                    )
                )
            if page.meta_description:
                page_meta_units.append(
                    EvidenceUnit(
                        entity=ent_name,
                        attribute="meta_description",
                        value=page.meta_description,
                        raw_value=page.meta_description,
                        source=source_url,
                        source_type="metadata",
                        confidence="high",
                        source_classification="FIRST_PARTY",
                    )
                )

        if not page_meta_units:
            return []

        groups: dict[tuple[str, str], list[EvidenceUnit]] = {}
        for u in page_meta_units:
            key = (u.entity, u.attribute)
            if key not in groups:
                groups[key] = []
            groups[key].append(u)

        result: list[AggregatedFact] = []
        for (ent_name, attr_name), group_units in groups.items():
            sources = tuple(sorted({u.source for u in group_units}))
            result.append(
                AggregatedFact(
                    entity=ent_name,
                    attribute=attr_name,
                    normalized_value=group_units[0].value,
                    units=tuple(group_units),
                    sources=sources,
                    source_types=("metadata",),
                    cross_page_status="SINGLE_PAGE" if len(sources) == 1 else "MATCH",
                    source_classifications=("FIRST_PARTY",),
                )
            )
        return result

    @property
    def metadata_facts(self) -> list[AggregatedFact]:
        return self.get_metadata_facts()

    def get_provenance(self, entity: Optional[str] = None, attribute: Optional[str] = None) -> list[EvidenceUnit]:
        """Return underlying EvidenceUnits preserving provenance."""
        return self._fact_inventory.get_units(entity=entity, attribute=attribute)


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


def extract_fact_inventory(
    obs_input: PageObservation | list[PageObservation] | None,
) -> FactInventory:
    """Extract a FactInventory from one or more PageObservations cleanly and safely."""
    inventory = FactInventory()
    if obs_input is None:
        return inventory

    if isinstance(obs_input, PageObservation):
        observations = [obs_input]
    elif isinstance(obs_input, list):
        observations = obs_input
    else:
        return inventory

    for obs in observations:
        if obs is None or not isinstance(obs, PageObservation):
            continue
        if not getattr(obs, "is_success", False):
            continue

        try:
            source_url = obs.final_url or obs.requested_url or "https://example.com"

            # Primary entity name
            entity_name = (obs.title or "").strip()
            if obs.h1 and len(obs.h1[0].strip()) > 2:
                entity_name = obs.h1[0].strip()
            if not entity_name:
                entity_name = "Page Entity"

            # Clean common title/page suffixes (e.g. "Product X Review" -> "Product X")
            entity_name = re.sub(r"\s+-\s+.*$", "", entity_name)
            entity_name = re.sub(r"\s+(Review|News|Deals|Overview|Guide|Page|Store|Official Site)\b", "", entity_name, flags=re.IGNORECASE).strip()
            if not entity_name:
                entity_name = "Page Entity"

            # Extracted date/year timestamp & temporal qualifier
            extracted_year: Optional[str] = None
            vis_text = obs.visible_text or ""
            text_to_search = (entity_name + " " + vis_text).strip()
            if text_to_search:
                year_match = DATE_PATTERN.search(text_to_search)
                if year_match:
                    extracted_year = year_match.group(1)

            t_qual, _ = _detect_temporal_qualifier(vis_text, extracted_year)

            # 1. Extract JSON-LD Facts
            if obs.json_ld:
                json_entities = _collect_json_ld_dicts(obs)
                for j_entity in json_entities:
                    if not isinstance(j_entity, dict):
                        continue
                    raw_j_name = str(j_entity.get("name") or entity_name).strip()
                    j_name = re.sub(r"\s+(Review|News|Deals|Overview|Guide|Page|Store|Official Site)\b", "", raw_j_name, flags=re.IGNORECASE).strip() or entity_name

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
                                price_qual = "EXPIRED" if c.get("priceValidUntil") and str(c.get("priceValidUntil")) < "2026" else t_qual
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
                                        source_classification="FIRST_PARTY",
                                        temporal_qualifier=price_qual,
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
                                        source_classification="FIRST_PARTY",
                                        temporal_qualifier=t_qual,
                                    )
                                )

                    # JSON-LD Telephone / Phone
                    raw_tel = j_entity.get("telephone") or j_entity.get("phone")
                    if raw_tel:
                        norm_phone = normalize_phone(raw_tel)
                        if norm_phone:
                            inventory.add_unit(
                                EvidenceUnit(
                                    entity=j_name,
                                    attribute="phone",
                                    value=norm_phone,
                                    raw_value=str(raw_tel),
                                    source=source_url,
                                    source_type="json_ld",
                                    confidence="high",
                                    temporal_qualifier=t_qual,
                                    source_classification="FIRST_PARTY",
                                )
                            )

                    # JSON-LD Address / Location
                    raw_addr = j_entity.get("address") or j_entity.get("location")
                    if raw_addr:
                        addr_str = json.dumps(raw_addr) if isinstance(raw_addr, dict) else str(raw_addr)
                        norm_loc = normalize_location(addr_str)
                        if norm_loc:
                            inventory.add_unit(
                                EvidenceUnit(
                                    entity=j_name,
                                    attribute="location",
                                    value=norm_loc,
                                    raw_value=addr_str,
                                    source=source_url,
                                    source_type="json_ld",
                                    confidence="high",
                                    temporal_qualifier=t_qual,
                                    source_classification="FIRST_PARTY",
                                )
                            )

            # 2. Extract Visible Text Price Facts
            if vis_text:
                visible_price_matches = PRICE_PATTERN.finditer(vis_text)
                for m in visible_price_matches:
                    raw_str = m.group(0).strip()
                    curr, amt, is_prefix = normalize_price(raw_str)
                    if amt is not None:
                        # Extract surrounding snippet to detect temporal context
                        start_idx = max(0, m.start() - 30)
                        end_idx = min(len(vis_text), m.end() + 30)
                        snippet = vis_text[start_idx:end_idx]
                        vis_qual, snippet_year = _detect_temporal_qualifier(snippet, extracted_year)

                        inventory.add_unit(
                            EvidenceUnit(
                                entity=entity_name,
                                attribute="price",
                                value=(curr, amt),
                                raw_value=raw_str,
                                source=source_url,
                                source_type="visible_text",
                                confidence="high",
                                timestamp=snippet_year or extracted_year,
                                is_range_or_prefix=is_prefix,
                                details={"currency": curr, "amount": amt},
                                source_classification="FIRST_PARTY",
                                temporal_qualifier=vis_qual,
                            )
                        )

                # 3. Extract Visible Availability Facts
                if re.search(r"\b(out of stock|sold out|unavailable)\b", vis_text, re.IGNORECASE):
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
                            source_classification="FIRST_PARTY",
                            temporal_qualifier=t_qual,
                        )
                    )
                elif re.search(r"\b(in stock|available now)\b", vis_text, re.IGNORECASE):
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
                            source_classification="FIRST_PARTY",
                            temporal_qualifier=t_qual,
                        )
                    )

                # 4. Extract Visible Phone Numbers
                phone_match = PHONE_PATTERN.search(vis_text)
                if phone_match:
                    raw_p = phone_match.group(0).strip()
                    norm_p = normalize_phone(raw_p)
                    if norm_p:
                        inventory.add_unit(
                            EvidenceUnit(
                                entity=entity_name,
                                attribute="phone",
                                value=norm_p,
                                raw_value=raw_p,
                                source=source_url,
                                source_type="visible_text",
                                confidence="high",
                                temporal_qualifier=t_qual,
                                source_classification="FIRST_PARTY",
                            )
                        )

                # 5. Extract Visible Location/Address text if explicit
                loc_match = re.search(r"\b(location|address|headquarters|office):\s*([^\n.,;]+)", vis_text, re.IGNORECASE)
                if loc_match:
                    raw_l = loc_match.group(0).strip()
                    norm_l = normalize_location(loc_match.group(2))
                    if norm_l:
                        inventory.add_unit(
                            EvidenceUnit(
                                entity=entity_name,
                                attribute="location",
                                value=norm_l,
                                raw_value=raw_l,
                                source=source_url,
                                source_type="visible_text",
                                confidence="high",
                                temporal_qualifier=t_qual,
                                source_classification="FIRST_PARTY",
                            )
                        )

            # 6. Extract Table Numeric Specifications (e.g. Weight)
            if obs.tables:
                for table in obs.tables:
                    if not table or not getattr(table, "rows", None):
                        continue
                    for row in table.rows:
                        if not row:
                            continue
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
                                    source_classification="FIRST_PARTY",
                                    temporal_qualifier=t_qual,
                                )
                            )

        except Exception:
            # Safely continue on malformed page
            continue

    return inventory


COMMON_BRAND_STOPWORDS: frozenset[str] = frozenset({
    "inc", "incorporated", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "pvt", "private", "llc", "group", "services", "technologies", "india",
    "global", "official", "site", "website", "home", "welcome", "the", "of", "and", "&",
    "com", "org", "net", "in", "edu", "ac", "a", "an", "for", "to", "by", "on", "at",
})


def _is_genuine_identity_conflict(name1: str, name2: str, domain: str = "") -> bool:
    """Determine if two entity identity declarations represent a genuine conflict."""
    n1 = name1.strip().lower()
    n2 = name2.strip().lower()
    if not n1 or not n2 or n1 == n2:
        return False

    if n1 in n2 or n2 in n1:
        return False

    n1_words = [w for w in re.findall(r"\b[a-z0-9]+\b", n1) if w not in COMMON_BRAND_STOPWORDS]
    n2_words = [w for w in re.findall(r"\b[a-z0-9]+\b", n2) if w not in COMMON_BRAND_STOPWORDS]

    if not n1_words or not n2_words:
        return False

    acronym1 = "".join(w[0] for w in n1_words)
    acronym2 = "".join(w[0] for w in n2_words)
    raw_n1 = "".join(re.findall(r"[a-z0-9]", n1))
    raw_n2 = "".join(re.findall(r"[a-z0-9]", n2))

    if acronym1 == raw_n2 or acronym2 == raw_n1 or acronym1 == n2 or acronym2 == n1:
        return False

    tokens1 = set(n1_words)
    tokens2 = set(n2_words)
    if tokens1.intersection(tokens2):
        return False

    domain_clean = re.sub(r"https?://|(www\.)", "", domain.lower()).split(".")[0]
    if domain_clean and len(domain_clean) >= 3:
        if domain_clean in n1 or domain_clean in n2:
            return False
        if any(t in domain_clean or domain_clean in t for t in tokens1.union(tokens2)):
            return False

    return True


def audit_evidence_consistency(
    obs_input: PageObservation | list[PageObservation] | SiteEvidenceInventory,
) -> list[Finding]:
    """Execute the Evidence + Consistency Audit using FactInventory or SiteEvidenceInventory.

    Generates findings for price conflicts, availability conflicts, and attribute conflicts.
    """
    if obs_input is None:
        return []

    if isinstance(obs_input, SiteEvidenceInventory):
        inventory = obs_input.fact_inventory
        observations = obs_input.pages
    elif isinstance(obs_input, list):
        site_inv = SiteEvidenceInventory(obs_input)
        inventory = site_inv.fact_inventory
        observations = site_inv.pages
    elif isinstance(obs_input, PageObservation):
        if not obs_input.is_success:
            return []
        inventory = extract_fact_inventory(obs_input)
        observations = [obs_input]
    else:
        return []

    findings: list[Finding] = []

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

    # Evaluate general cross-page price conflict (EVID-PRC-001)
    price_units = inventory.get_units(attribute="price")
    if price_units:
        entities = {u.entity for u in price_units}
        for ent in entities:
            st, exp = inventory.evaluate_consistency(ent, "price")
            if st == "CONFLICT":
                # Check if MA-REP-001 finding wasn't already generated for this
                if not any(f.id == "MA-REP-001" for f in findings):
                    p_units = inventory.get_units(entity=ent, attribute="price")
                    p_vals = sorted({u.raw_value for u in p_units})
                    evidence = (
                        f"Conflicting product prices ({', '.join(p_vals)}) observed for '{ent}' "
                        "for current timeframe across site surfaces. Inconsistent pricing reduces machine confidence."
                    )
                    findings.append(
                        Finding(
                            id="EVID-PRC-001",
                            title="Conflicting product price declarations",
                            severity="high",
                            evidence=evidence,
                            suggested_action=SuggestedAction(
                                summary="Reconcile product pricing across visible content and structured data.",
                                priority=1,
                            ),
                            priority=1,
                            confidence="high",
                            details={"entity": ent, "attribute": "price", "conflicting_prices": p_vals},
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

    # 3. Evaluate Entity Identity Consistency (EVID-ENT-001)
    for obs in observations:
        json_entities = _collect_json_ld_dicts(obs)
        typed_entity_names: dict[str, set[str]] = {}
        for j_entity in json_entities:
            raw_type = str(j_entity.get("@type") or "").strip().lower()
            raw_name = str(j_entity.get("name") or "").strip()
            if raw_type and raw_name and len(raw_name) >= 3:
                if raw_type not in typed_entity_names:
                    typed_entity_names[raw_type] = set()
                typed_entity_names[raw_type].add(raw_name)

        domain = obs.final_url or obs.requested_url or ""
        for etype, names in typed_entity_names.items():
            if len(names) >= 2:
                name_list = sorted(names)
                for i in range(len(name_list)):
                    for j in range(i + 1, len(name_list)):
                        n1, n2 = name_list[i], name_list[j]
                        if _is_genuine_identity_conflict(n1, n2, domain=domain):
                            evidence = (
                                f"Structured data declares conflicting entity identities ('{n1}' vs '{n2}') "
                                f"for the same entity type ('{etype}'). "
                                "Genuinely contradictory entity declarations reduce automated confidence "
                                "in entity identity and resolution."
                            )
                            findings.append(
                                Finding(
                                    id="EVID-ENT-001",
                                    title="Conflicting entity identity declarations",
                                    severity="high",
                                    evidence=evidence,
                                    suggested_action=SuggestedAction(
                                        summary="Reconcile entity identity names in structured data.",
                                        priority=1,
                                    ),
                                    priority=1,
                                    confidence="high",
                                    details={
                                        "entity_conflict": True,
                                        "entity_type": etype,
                                        "entity_a": n1,
                                        "entity_b": n2,
                                    },
                                )
                            )

    # 4. Evaluate Phone Number Consistency (EVID-PHN-001)
    phone_units = inventory.get_units(attribute="phone")
    if phone_units:
        entities = {u.entity for u in phone_units}
        for ent in entities:
            st, exp = inventory.evaluate_consistency(ent, "phone")
            if st == "CONFLICT":
                p_units = inventory.get_units(entity=ent, attribute="phone")
                p_vals = sorted({u.raw_value for u in p_units})
                evidence = (
                    f"Conflicting contact phone numbers ({', '.join(p_vals)}) observed for '{ent}' "
                    "for current timeframe. Inconsistent phone numbers reduce machine confidence in contact resolution."
                )
                findings.append(
                    Finding(
                        id="EVID-PHN-001",
                        title="Conflicting contact phone numbers",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary="Reconcile telephone numbers across visible page content and structured data.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={"entity": ent, "attribute": "phone", "conflicting_phones": p_vals},
                    )
                )

    # 5. Evaluate Location / Address Consistency (EVID-LOC-001)
    loc_units = inventory.get_units(attribute="location")
    if loc_units:
        entities = {u.entity for u in loc_units}
        for ent in entities:
            st, exp = inventory.evaluate_consistency(ent, "location")
            if st == "CONFLICT":
                l_units = inventory.get_units(entity=ent, attribute="location")
                l_vals = sorted({u.raw_value for u in l_units})
                evidence = (
                    f"Conflicting business location declarations ({', '.join(l_vals)}) observed for '{ent}' "
                    "for current timeframe. Inconsistent locations reduce machine confidence in business verification."
                )
                findings.append(
                    Finding(
                        id="EVID-LOC-001",
                        title="Conflicting business location addresses",
                        severity="high",
                        evidence=evidence,
                        suggested_action=SuggestedAction(
                            summary="Reconcile physical address and location declarations across site surfaces.",
                            priority=1,
                        ),
                        priority=1,
                        confidence="high",
                        details={"entity": ent, "attribute": "location", "conflicting_locations": l_vals},
                    )
                )

    return findings
