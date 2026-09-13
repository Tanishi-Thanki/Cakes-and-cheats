"""Evidence & Consistency foundation.

Establishes the shared, provider-neutral evidence/provenance layer that audits such
as Task Readiness and Person 3's orchestrator can later consume:

    PageObservation
          |
          v
    Evidence extraction  ->  EvidenceUnit
          |
          v
    FactInventory  (grouped by entity -> attribute -> evidence values)
          |
          v
    Consistency classification  (MATCH | CONFLICT | HISTORICAL | AMBIGUOUS | INSUFFICIENT_EVIDENCE)
          |
          v
    Finding  (only for meaningful consistency problems, using the shared Finding model)

Design Principles
-----------------
- Read-only: consumes PageObservation; never re-fetches, crawls, or renders.
- Provenance-preserving: every value keeps its raw text, source URL, source type,
  confidence, qualifier, and (when actually observed) date.
- Conservative: never manufactures certainty; prefers AMBIGUOUS/INSUFFICIENT_EVIDENCE
  over a false CONFLICT; treats legitimate variants (sale prices, price ranges,
  "from" prices, historical pages) as non-conflicts.
- Lightweight: standard library only (re, dataclasses, decimal); no LLM/API/DB/platform.

Overlap with existing audits (documented, not duplicated)
--------------------------------------------------------
Machine Answerability (MA-REP-001/002) and Freshness & Corroboration (FC-REP-001/002,
FC-ATTR-001) both already detect price/availability/attribute inconsistencies on a
single page. This module is intentionally NOT a re-implementation of those checks:

- It produces the reusable EvidenceUnit/FactInventory provenance primitive.
- Its price normalization is exact (decimal-accurate, currency-aware), whereas the
  existing audits compare integer parts only. Those audits are left untouched to
  preserve their behavior and tests; converging them onto this caretaker logic is a
  documented follow-up, not a silent divergence.

Entity identity
---------------
Single-page extraction assigns every unit to one resolved "primary entity" (JSON-LD
name, else H1, else title). Cross-page construction (merging FactInventories) groups
units by the entity label supplied per page, so different products are never compared
(Product A != Product B). Name/brand comparisons use significant-token overlap so
"Pegasus 40 Performance Running Shoes" vs "Pegasus 40" is a MATCH, not a conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Literal, Optional, Sequence

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# ---------------------------------------------------------------------------
# Public type vocabulary
# ---------------------------------------------------------------------------

ConsistencyClassification = Literal[
    "MATCH",
    "CONFLICT",
    "HISTORICAL",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
]

SourceType = Literal[
    "visible_text",
    "heading",
    "table",
    "json_ld",
    "metadata",
    "internal_page",
]

# ---------------------------------------------------------------------------
# EvidenceUnit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceUnit:
    """A single observed fact value, together with its provenance.

    Attributes:
        entity: Identity label the fact belongs to (resolved primary entity).
        attribute: Canonical attribute key ('price', 'availability', 'brand',
            'weight', 'datePublished', ...).
        value: Raw value as observed (string for text/metrics, number for JSON-LD).
        source: URL / page the fact was observed on (provenance anchor).
        source_type: One of visible_text | heading | table | json_ld | metadata | internal_page.
        confidence: 'high' | 'medium' | 'low' (derived deterministically).
        raw_value: Original/raw rendering of the value where it differs from ``value``.
        date: Explicit temporal evidence observed for this fact, if any (never invented).
        qualifier: Optional context marker ('sale', 'original', 'from', 'range').
    """

    entity: str
    attribute: str
    value: Any
    source: str
    source_type: SourceType = "visible_text"
    confidence: str = "medium"
    raw_value: Optional[Any] = None
    date: Optional[str] = None
    qualifier: Optional[str] = None
    period: Optional[str] = None


# Deterministic confidence tier per source type (conservative).
SOURCE_CONFIDENCE: dict[str, str] = {
    "json_ld": "high",
    "table": "high",
    "metadata": "medium",
    "heading": "medium",
    "visible_text": "medium",
    "internal_page": "low",
}

# ---------------------------------------------------------------------------
# FactInventory
# ---------------------------------------------------------------------------


class FactInventory:
    """In-memory grouping of EvidenceUnits by entity -> attribute -> values.

    Lightweight by design: no database, graph, or web-wide knowledge store.
    Multiple observations (one per internal page) can be merged via :meth:`merge`,
    preserving each unit's provenance.
    """

    def __init__(self) -> None:
        self._units: list[EvidenceUnit] = []
        self._by_key: dict[tuple[str, str], list[EvidenceUnit]] = {}

    def add_unit(self, unit: EvidenceUnit) -> None:
        """Register a single evidence unit, grouped by (entity, attribute)."""
        self._units.append(unit)
        self._by_key.setdefault((unit.entity, unit.attribute), []).append(unit)

    def merge(self, other: "FactInventory") -> None:
        """Absorb every unit from another inventory, keeping provenance intact."""
        for unit in other.iter_units():
            self.add_unit(unit)

    def iter_units(self) -> Iterator[EvidenceUnit]:
        return iter(self._units)

    def entities(self) -> tuple[str, ...]:
        return tuple(sorted({u.entity for u in self._units}))

    def attributes(self, entity: str) -> tuple[str, ...]:
        return tuple(
            sorted({attr for (ent, attr) in self._by_key if ent == entity})
        )

    def units_for(self, entity: str, attribute: str) -> tuple[EvidenceUnit, ...]:
        return tuple(self._by_key.get((entity, attribute), []))

    def groups(self) -> tuple[tuple[tuple[str, str], tuple[EvidenceUnit, ...]], ...]:
        """Return ((entity, attribute), units) pairs in stable order."""
        return tuple(
            (key, tuple(units))
            for key, units in sorted(self._by_key.items(), key=lambda kv: kv[0])
        )

    def __len__(self) -> int:
        return len(self._units)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": {
                entity: {
                    attribute: [
                        {
                            "value": u.value,
                            "raw_value": u.raw_value,
                            "source": u.source,
                            "source_type": u.source_type,
                            "confidence": u.confidence,
                            "date": u.date,
                            "qualifier": u.qualifier,
                        }
                        for u in self.units_for(entity, attribute)
                    ]
                    for attribute in self.attributes(entity)
                }
                for entity in self.entities()
            }
        }


# ---------------------------------------------------------------------------
# Regex / vocabulary used by extraction and normalization
# ---------------------------------------------------------------------------

# Currency symbol/amount shapes (aligned with the existing audit regexes).
_CURRENCY = r"(?:₹|Rs\.?|\$|€|£|USD|INR|EUR|GBP)"
_AMOUNT = r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"

PRICE_REGEX: re.Pattern[str] = re.compile(rf"{_CURRENCY}\s*{_AMOUNT}", re.IGNORECASE)
PRICE_RANGE_REGEX: re.Pattern[str] = re.compile(
    rf"{_CURRENCY}\s*{_AMOUNT}\s*[-–—]\s*(?:{_CURRENCY})?\s*{_AMOUNT}",
    re.IGNORECASE,
)

CURRENCY_CODES: dict[str, str] = {
    "₹": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}

# Availability phrase sets (mirror the conservative phrases used by the audits).
IN_STOCK_PHRASE = re.compile(r"\b(in stock|available now|in-stock)\b", re.IGNORECASE)
OUT_OF_STOCK_PHRASE = re.compile(r"\b(out of stock|sold out|currently unavailable|unavailable)\b", re.IGNORECASE)

AVAILABILITY_CANONICAL: dict[str, str] = {
    "instock": "InStock",
    "outofstock": "OutOfStock",
    "preorder": "PreOrder",
    "presale": "PreOrder",
    "backorder": "BackOrder",
    "limitedavailability": "LimitedStock",
}

# Billing / pricing unit context (e.g. "$10/month", "per user", "annually").
PRICE_PERIOD_PATTERN: re.Pattern[str] = re.compile(
    r"/\s*(month|mo|year|yr|day|week|user|seat|item|unit|gb|license)"
    r"|\bper\s+(month|year|day|week|user|seat|item|unit|gb|license)\b"
    r"|\b(monthly|annually|yearly|weekly|daily)\b",
    re.IGNORECASE,
)

PERIOD_CANONICAL: dict[str, str] = {
    "month": "month", "mo": "month", "monthly": "month",
    "year": "year", "yr": "year", "annually": "year", "yearly": "year",
    "day": "day", "daily": "day",
    "week": "week", "weekly": "week",
    "user": "user", "seat": "seat", "item": "item", "unit": "unit",
    "gb": "gb", "license": "license",
}


def detect_billing_period(text_snippet: str) -> Optional[str]:
    """Normalize a billing/unit context ("/month", "per user", "annually") or None."""
    if not text_snippet:
        return None
    m = PRICE_PERIOD_PATTERN.search(text_snippet)
    if not m:
        return None
    raw_key = next((g for g in m.groups() if g), "").lower().strip()
    return PERIOD_CANONICAL.get(raw_key)


def _identity_compatible_with_primary(name: str, primary: str) -> bool:
    """Whether a JSON-LD name plausibly resolves to the page's primary entity.

    Conservative string/token heuristic: exact match, substring containment, or a
    shared identity of at least two significant tokens qualify as the same entity.
    Generic single-token overlaps (e.g. two different "Product A"/"Product B")
    never merge, so cross-entity evidence is never compared as one fact.
    """
    if not name or not primary:
        return False
    n = re.sub(r"\s+", " ", str(name).strip().lower())
    p = re.sub(r"\s+", " ", str(primary).strip().lower())
    if n == p:
        return True
    if n in p or p in n:
        return True
    return len(_significant_tokens(n) & _significant_tokens(p)) >= 2

# Date fields considered authoritative temporal evidence (aligns with the audits).
DATE_FIELDS: tuple[str, ...] = ("datePublished", "dateModified", "uploadDate", "priceValidUntil")

YEAR_PATTERN = re.compile(r"\b(20[0-2][0-9]|19[0-9]{2})\b")

VISIBLE_DATE_PATTERN = re.compile(
    r"\b(?:last\s+updated|updated\s+on|modified)\s*:\s*([A-Za-z0-9,\s\-/]+)",
    re.IGNORECASE,
)

BRAND_PATTERN = re.compile(r"\bbrand\s*:\s*([A-Za-z0-9][A-Za-z0-9\s\-]{1,40})", re.IGNORECASE)

# Attributes comparable as physical quantities (weight/storage/capacity/battery/length).
QUANTITY_ATTRIBUTE_PATTERN = re.compile(
    r"^(weight|storage|ram|memory|capacity|battery(?:\s*life)?|dimensions?|"
    r"screen\s*size|height|width|depth|length|resolution|battery)\b",
    re.IGNORECASE,
)

# Base-unit conversions for reliable quantities only (no guessed conversions).
_QUANTITY_BASES: dict[str, tuple[str, float]] = {
    "mg": ("mass", 0.001),
    "g": ("mass", 1.0),
    "kg": ("mass", 1000.0),
    "mm": ("length", 1.0),
    "cm": ("length", 10.0),
    "m": ("length", 1000.0),
    "mb": ("storage", 1.0),
    "gb": ("storage", 1024.0),
    "tb": ("storage", 1048576.0),
    "ml": ("volume", 1.0),
    "l": ("volume", 1000.0),
    "mah": ("battery", 1.0),
    "h": ("time", 1.0),
}
# Explicit plural/word variants map onto the base keys above (no guessing).
_UNIT_ALIASES: dict[str, str] = {
    "mg": "mg",
    "milligram": "mg",
    "milligrams": "mg",
    "g": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "mb": "mb",
    "mbs": "mb",
    "gb": "gb",
    "gbs": "gb",
    "tb": "tb",
    "tbs": "tb",
    "ml": "ml",
    "mls": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "mah": "mah",
    "mahs": "mah",
    "h": "h",
    "hour": "h",
    "hours": "h",
}
_QUANTITY_REGEX = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*([a-zµ]+)\b",
    re.IGNORECASE,
)

# Qualitative attribute vocabulary (compared as clean text, not quantities).
QUALITATIVE_ATTRIBUTES: frozenset[str] = frozenset({
    "color",
    "colour",
    "material",
    "size",
    "model",
    "finish",
    "connectivity",
    "compatibility",
})


def _normalize_key(key: str) -> str:
    """Canonicalize a table/JSON additionalProperty key into an attribute name."""
    return re.sub(r"[^a-z0-9]+", "", key.lower().strip())


# ---------------------------------------------------------------------------
# Attribute normalization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedValue:
    """Comparable rendering of an observed value for consistency checks."""

    kind: str
    canonical: str
    comparable: tuple[Any, ...] = ()
    raw: Any = None


def _price_to_cents(amount_text: str) -> Optional[int]:
    cleaned = amount_text.replace(",", "").strip()
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if amount < 0:
        return None
    return int((amount * 100).to_integral_value())


def _normalize_price(value: Any, qualifier: Optional[str]) -> Optional[NormalizedValue]:
    if qualifier == "range":
        # Price intervals are never equivalent to a scalar price.
        return None
    text = str(value).strip()
    lowered = text.lower()
    if not lowered:
        return None

    ranges = list(PRICE_RANGE_REGEX.finditer(text))
    if ranges:
        # A value literally containing a range stays incomparable.
        return NormalizedValue(kind="range", canonical=text, comparable=("range",), raw=value)

    m = PRICE_REGEX.search(text)
    if not m:
        # Trailing-currency form: "999 USD"
        suffix = re.search(r"([0-9][0-9,\.]*)\s*(USD|INR|EUR|GBP)\b", text, re.IGNORECASE)
        if not suffix:
            return None
        currency = CURRENCY_CODES.get(suffix.group(2).lower(), suffix.group(2).upper())
        cents = _price_to_cents(suffix.group(1))
    else:
        symbol = re.match(r"(₹|Rs\.?|\$|€|£|USD|INR|EUR|GBP)", m.group(0), re.IGNORECASE)
        currency = CURRENCY_CODES.get((symbol.group(1) if symbol else "").lower(), "")
        cents = _price_to_cents(m.group(1))

    if cents is None:
        return None
    if not currency:
        # Numeric price with no currency context: comparable only against like values.
        currency = "?"
    canonical = f"{currency} {Decimal(cents) / 100}"
    return NormalizedValue(kind="price", canonical=canonical, comparable=("price", currency, cents), raw=value)


def _normalize_availability(value: Any) -> Optional[NormalizedValue]:
    text = str(value).strip()
    lowered = text.lower()
    # Accept full schema.org URIs and plain labels.
    if "/" in lowered:
        lowered = lowered.rstrip("/").rsplit("/", 1)[-1]
    key = re.sub(r"[^a-z]", "", lowered)
    canonical = AVAILABILITY_CANONICAL.get(key)
    if canonical is None:
        return None
    return NormalizedValue(kind="availability", canonical=canonical, comparable=("availability", canonical), raw=value)


def _normalize_date(value: Any) -> Optional[NormalizedValue]:
    text = str(value).strip()
    y = YEAR_PATTERN.search(text)
    if not y:
        return None
    year = int(y.group(1))
    return NormalizedValue(kind="date", canonical=str(year), comparable=("date", year), raw=value)


def _normalize_quantity(value: Any) -> Optional[NormalizedValue]:
    text = str(value).strip()
    m = _QUANTITY_REGEX.search(text)
    if not m:
        return None
    try:
        number = Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    unit_key = _UNIT_ALIASES.get(m.group(2).lower().strip())
    base = _QUANTITY_BASES.get(unit_key or "")
    if base is None:
        return None
    dimension, factor = base
    base_amount = float(number) * factor
    return NormalizedValue(
        kind="quantity",
        canonical=f"{base_amount:g} {dimension}",
        comparable=("quantity", dimension, round(base_amount, 4)),
        raw=value,
    )


def _normalize_text(value: Any) -> Optional[NormalizedValue]:
    cleaned = re.sub(r"\s+", " ", str(value).strip().lower())
    if not cleaned:
        return None
    return NormalizedValue(kind="text", canonical=cleaned, comparable=("text", cleaned), raw=value)


def _significant_tokens(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"\b[A-Za-z0-9]{4,}\b", str(text))}


def normalize_value(attribute: str, value: Any, *, qualifier: Optional[str] = None) -> Optional[NormalizedValue]:
    """Normalize a raw observation into a comparable form (conservative).

    Returns None when the value cannot be reliably normalized (incomparable,
    malformed, or unsupported); such units are never allowed to produce a CONFLICT.
    """
    attr = _normalize_key(attribute)

    if attr == "price":
        return _normalize_price(value, qualifier)
    if attr == "availability":
        return _normalize_availability(value)
    if attr in ("datepublished", "datemodified", "uploaddate", "pricevaliduntil") or "date" in attr:
        return _normalize_date(value)
    if QUANTITY_ATTRIBUTE_PATTERN.match(attr):
        return _normalize_quantity(value)
    if attr in QUALITATIVE_ATTRIBUTES or attr in ("name", "brand", "entity"):
        return _normalize_text(value)
    return None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _iter_json_ld_entities(obs: PageObservation) -> Iterator[dict[str, Any]]:
    """Yield every top-level + nested JSON-LD entity dict (containers excluded)."""
    for block in obs.json_ld:
        if not block.is_valid or block.parsed_data is None:
            continue
        stack: list[Any] = [block.parsed_data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])
                raw_type = str(item.get("@type") or "").lower()
                if raw_type in CONTAINER_JSON_LD_TYPES:
                    for val in item.values():
                        if isinstance(val, (dict, list)):
                            stack.append(val)
                    continue
                if "name" not in item and ("price" in item or "offers" in item):
                    # Anonymous price/offer-bearing container without identity.
                    for val in item.values():
                        if isinstance(val, (dict, list)):
                            stack.append(val)
                    continue
                yield item
                for val in item.values():
                    if isinstance(val, (dict, list)):
                        stack.append(val)
            elif isinstance(item, list):
                stack.extend(item)


# Entity types that can identify a page's primary entity (used to resolve a
# primary entity label when JSON-LD is present).
PRIMARY_ENTITY_TYPES: frozenset[str] = frozenset({
    "product",
    "service",
    "softwareapplication",
    "organization",
    "event",
    "course",
    "recipe",
    "article",
    "newsarticle",
    "brand",
})

# JSON-LD container types whose dicts carry pricing/rating metadata but are NOT
# themselves entities with identity. Skipping them prevents phantom attribution
# of their fields (e.g. an Offer's price) to a fallback page-level label.
CONTAINER_JSON_LD_TYPES: frozenset[str] = frozenset({
    "offer",
    "aggregateoffer",
    "pricespecification",
    "monetaryamount",
    "offercatalog",
    "aggregaterating",
    "offershippingdetails",
})

# Entity types whose `name` constitutes primary-entity name evidence. Explicitly
# excludes nested container entities such as `brand`, whose name belongs to the
# `brand` attribute group, never to the primary entity's `name` group.
NAME_EVIDENCE_TYPES: frozenset[str] = frozenset({
    "product",
    "service",
    "softwareapplication",
    "organization",
    "event",
    "course",
    "recipe",
    "article",
    "newsarticle",
})


def _resolve_primary_entity(obs: PageObservation) -> str:
    for entity in _iter_json_ld_entities(obs):
        raw_type = str(entity.get("@type") or "").lower()
        if raw_type in PRIMARY_ENTITY_TYPES:
            name = entity.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    for heading in obs.h1:
        if heading.strip():
            return heading.strip()
    if obs.title and obs.title.strip():
        return obs.title.strip()
    return ""


def _extract_json_ld_entity_name(entity: dict[str, Any]) -> Optional[str]:
    name = entity.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _extract_json_ld_brand(entity: dict[str, Any]) -> Optional[str]:
    raw = entity.get("brand")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        name = raw.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _offer_candidates(entity: dict[str, Any]) -> list[dict[str, Any]]:
    """Offer/price-bearing sub-dicts of a JSON-LD entity (dedup-safe)."""
    offers = entity.get("offers")
    candidates: list[dict[str, Any]] = []
    if isinstance(offers, dict):
        candidates.append(offers)
    elif isinstance(offers, list):
        candidates.extend(o for o in offers if isinstance(o, dict))
    if "price" in entity:
        # Support a bare entity carrying 'price' directly (no offers wrapper).
        candidates.append(entity)
    return candidates


def _extract_offer_price_units(
    offer: dict[str, Any],
    name: str,
    currency_ctx: Optional[str],
    source: str,
) -> list[EvidenceUnit]:
    """Build price units from ONE offer / priceSpecification / aggregate offer."""
    units: list[EvidenceUnit] = []
    currency = str(offer.get("priceCurrency") or currency_ctx or "").strip()
    qualifier = None

    spec = offer.get("priceSpecification")
    if isinstance(spec, dict) and (spec.get("minPrice") is not None or spec.get("maxPrice") is not None):
        low, high = spec.get("minPrice"), spec.get("maxPrice")
        raw = f"{low}-{high}" if low is not None and high is not None else str(low if low is not None else high)
        units.append(
            EvidenceUnit(
                entity=name,
                attribute="price",
                value=raw,
                source=source,
                source_type="json_ld",
                confidence=SOURCE_CONFIDENCE["json_ld"],
                raw_value=raw,
                qualifier="range",
            )
        )
        return units

    if offer.get("lowPrice") is not None and offer.get("highPrice") is not None:
        raw = f"{offer['lowPrice']}-{offer['highPrice']}"
        units.append(
            EvidenceUnit(
                entity=name,
                attribute="price",
                value=raw,
                source=source,
                source_type="json_ld",
                confidence=SOURCE_CONFIDENCE["json_ld"],
                raw_value=raw,
                qualifier="range",
            )
        )
        return units

    price = offer.get("price")
    if price is None:
        return units
    pvalid = offer.get("priceValidUntil")
    value_text = f"{currency} {price}" if currency else str(price)
    units.append(
        EvidenceUnit(
            entity=name,
            attribute="price",
            value=value_text,
            source=source,
            source_type="json_ld",
            confidence=SOURCE_CONFIDENCE["json_ld"],
            raw_value=str(price),
            date=str(pvalid) if isinstance(pvalid, str) and pvalid.strip() else None,
            qualifier=qualifier,
            period=detect_billing_period(str(price)),
        )
    )
    return units


def _extract_visible_price_units(obs: PageObservation) -> list[EvidenceUnit]:
    """Extract scalar/range prices from visible text with contextual qualifiers."""
    units: list[EvidenceUnit] = []
    work = obs.visible_text

    for rm in PRICE_RANGE_REGEX.finditer(obs.visible_text):
        raw = rm.group(0).strip()
        units.append(
            EvidenceUnit(
                entity="",
                attribute="price",
                value=raw,
                source=obs.final_url or obs.requested_url,
                source_type="visible_text",
                confidence=SOURCE_CONFIDENCE["visible_text"],
                raw_value=raw,
                qualifier="range",
            )
        )
        # Remove the consumed range span so its parts are not also extracted.
        span = slice(rm.start(), rm.end())
        work = work[: rm.start()] + (" " * (rm.end() - rm.start())) + work[rm.end():]
        _ = span

    # Track qualifiers via the 40-char window before each remaining scalar price.
    scalar_spans: list[tuple[int, int, bool]] = []
    keep = work
    for m in PRICE_REGEX.finditer(keep):
        scalar_spans.append((m.start(), m.end(), False))
    for m in re.finditer(r"([0-9][0-9,\.]*)\s*(USD|INR|EUR|GBP)\b", keep, re.IGNORECASE):
        # Only treat as price when a currency-bearing scalar wasn't already captured.
        if not any(s <= m.start() < e for s, e, _ in scalar_spans):
            scalar_spans.append((m.start(), m.end(), True))
    scalar_spans.sort()

    prev_end = 0
    for start, end, is_suffix in scalar_spans:
        raw = re.sub(r"\s+", " ", keep[start:end]).strip()
        context_start = max(0, start - 40)
        qualifier = _price_qualifier(keep[context_start:start].lower())
        period = detect_billing_period(keep[max(0, start - 40): min(len(keep), end + 40)])
        units.append(
            EvidenceUnit(
                entity="",
                attribute="price",
                value=raw,
                source=obs.final_url or obs.requested_url,
                source_type="visible_text",
                confidence=SOURCE_CONFIDENCE["visible_text"],
                raw_value=raw,
                qualifier=qualifier,
                period=period,
            )
        )
        prev_end = end
        _ = is_suffix
    return units


def _price_qualifier(context: str) -> Optional[str]:
    if re.search(r"\b(from|starting at|starts at|begins at)\b", context):
        return "from"
    if re.search(r"\b(was|original|regular|mrp|rrp|before|list price|slashed)\b", context):
        return "original"
    if re.search(r"\b(sale|now|you pay|save|discount|deal|special|offer)\b", context):
        return "sale"
    return None


def _extract_availability_units(obs: PageObservation) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    source = obs.final_url or obs.requested_url

    if IN_STOCK_PHRASE.search(obs.visible_text):
        units.append(
            EvidenceUnit(
                entity="",
                attribute="availability",
                value="In Stock",
                source=source,
                source_type="visible_text",
                confidence=SOURCE_CONFIDENCE["visible_text"],
                raw_value="In Stock",
            )
        )
    if OUT_OF_STOCK_PHRASE.search(obs.visible_text):
        units.append(
            EvidenceUnit(
                entity="",
                attribute="availability",
                value="Out of Stock",
                source=source,
                source_type="visible_text",
                confidence=SOURCE_CONFIDENCE["visible_text"],
                raw_value="Out of Stock",
            )
        )

    for entity in _iter_json_ld_entities(obs):
        for cand in _offer_candidates(entity):
            raw_avail = cand.get("availability")
            if raw_avail is None:
                continue
            units.append(
                EvidenceUnit(
                    entity=entity.get("name", ""),
                    attribute="availability",
                    value=raw_avail,
                    source=source,
                    source_type="json_ld",
                    confidence=SOURCE_CONFIDENCE["json_ld"],
                    raw_value=str(raw_avail),
                )
            )
    return units


def _extract_date_units(obs: PageObservation) -> list[EvidenceUnit]:
    units: list[EvidenceUnit] = []
    source = obs.final_url or obs.requested_url

    for entity in _iter_json_ld_entities(obs):
        for field in DATE_FIELDS:
            raw = entity.get(field)
            if isinstance(raw, str) and raw.strip():
                units.append(
                    EvidenceUnit(
                        entity=entity.get("name", ""),
                        attribute=field,
                        value=raw.strip(),
                        source=source,
                        source_type="json_ld",
                        confidence=SOURCE_CONFIDENCE["json_ld"],
                        raw_value=raw.strip(),
                        date=raw.strip(),
                    )
                )

    v_match = VISIBLE_DATE_PATTERN.search(obs.visible_text)
    if v_match:
        raw = v_match.group(1).strip()
        units.append(
            EvidenceUnit(
                entity="",
                attribute="dateModified",
                value=raw,
                source=source,
                source_type="visible_text",
                confidence=SOURCE_CONFIDENCE["visible_text"],
                raw_value=raw,
                date=raw,
            )
        )
    return units


def _extract_attribute_units(obs: PageObservation) -> list[EvidenceUnit]:
    """Extract qualitative/quantitative attributes from JSON-LD and tables."""
    units: list[EvidenceUnit] = []
    source = obs.final_url or obs.requested_url

    standard_keys = ("color", "colour", "material", "size", "weight", "capacity", "storage", "model")
    for entity in _iter_json_ld_entities(obs):
        for key in standard_keys:
            val = entity.get(key)
            if isinstance(val, str) and val.strip():
                units.append(
                    EvidenceUnit(
                        entity=entity.get("name", ""),
                        attribute=_normalize_key("colour" if key == "colour" else key),
                        value=val.strip(),
                        source=source,
                        source_type="json_ld",
                        confidence=SOURCE_CONFIDENCE["json_ld"],
                        raw_value=val.strip(),
                    )
                )
        for prop in entity.get("additionalProperty", []) if isinstance(entity.get("additionalProperty"), list) else []:
            if not isinstance(prop, dict):
                continue
            name = prop.get("name")
            val = prop.get("value")
            if isinstance(name, str) and name.strip() and isinstance(val, str) and val.strip():
                units.append(
                    EvidenceUnit(
                        entity=entity.get("name", ""),
                        attribute=_normalize_key(name),
                        value=val.strip(),
                        source=source,
                        source_type="json_ld",
                        confidence=SOURCE_CONFIDENCE["json_ld"],
                        raw_value=val.strip(),
                    )
                )

    for table in obs.tables:
        for row in table.rows:
            if len(row) >= 2:
                key = row[0].strip()
                val = row[1].strip()
                if key and val:
                    units.append(
                        EvidenceUnit(
                            entity="",
                            attribute=_normalize_key(key),
                            value=val,
                            source=source,
                            source_type="table",
                            confidence=SOURCE_CONFIDENCE["table"],
                            raw_value=val,
                        )
                    )
    return units


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def build_fact_inventory(
    obs: PageObservation,
    *,
    page_url: Optional[str] = None,
    entity: Optional[str] = None,
) -> FactInventory:
    """Extract high-value evidence from a single PageObservation into a FactInventory.

    Args:
        obs: The shared PageObservation (never re-fetched).
        page_url: Optional explicit source URL (defaults to the final URL).
        entity: Optional explicit entity label (defaults to resolved primary entity).

    Returns:
         A FactInventory grouping the observed facts with full provenance.
         Empty for failed/malformed/empty pages (never raises).
    """
    inventory = FactInventory()
    if not obs.is_success:
        return inventory

    source = page_url or obs.final_url or obs.requested_url
    primary = (entity or _resolve_primary_entity(obs)).strip()

    if primary:
        inventory.add_unit(
            EvidenceUnit(
                entity=primary,
                attribute="entity",
                value=primary,
                source=source,
                source_type="internal_page",
                confidence=SOURCE_CONFIDENCE["internal_page"],
            )
        )

        seen_json_name: set[str] = set()
        for entity_dict in _iter_json_ld_entities(obs):
            # Only dicts that canonically represent the primary entity contribute
            # name evidence. Nested container entities (e.g. Brand, Offer) carry
            # their own `name` keys and must never be attributed to the product's
            # `name` group: doing so lets a shared token (e.g. brand == product
            # name token) mask a genuine identity conflict (EVID-ENT-001).
            if str(entity_dict.get("@type") or "").lower() not in NAME_EVIDENCE_TYPES:
                continue
            name = _extract_json_ld_entity_name(entity_dict)
            if name and name not in seen_json_name:
                seen_json_name.add(name)
                inventory.add_unit(
                    EvidenceUnit(
                        entity=primary,
                        attribute="name",
                        value=name,
                        source=source,
                        source_type="json_ld",
                        confidence=SOURCE_CONFIDENCE["json_ld"],
                        raw_value=name,
                    )
                )
            brand = _extract_json_ld_brand(entity_dict)
            if brand:
                inventory.add_unit(
                    EvidenceUnit(
                        entity=primary,
                        attribute="brand",
                        value=brand,
                        source=source,
                        source_type="json_ld",
                        confidence=SOURCE_CONFIDENCE["json_ld"],
                        raw_value=brand,
                    )
                )

        if obs.h1 and obs.h1[0].strip():
            inventory.add_unit(
                EvidenceUnit(
                    entity=primary,
                    attribute="name",
                    value=obs.h1[0].strip(),
                    source=source,
                    source_type="heading",
                    confidence=SOURCE_CONFIDENCE["heading"],
                    raw_value=obs.h1[0].strip(),
                )
            )
        if obs.title and obs.title.strip() and obs.title.strip() != (obs.h1[0].strip() if obs.h1 else ""):
            inventory.add_unit(
                EvidenceUnit(
                    entity=primary,
                    attribute="name",
                    value=obs.title.strip(),
                    source=source,
                    source_type="metadata",
                    confidence=SOURCE_CONFIDENCE["metadata"],
                    raw_value=obs.title.strip(),
                )
            )

        brand_match = BRAND_PATTERN.search(obs.visible_text)
        if brand_match:
            inventory.add_unit(
                EvidenceUnit(
                    entity=primary,
                    attribute="brand",
                    value=brand_match.group(1).strip(),
                    source=source,
                    source_type="visible_text",
                    confidence=SOURCE_CONFIDENCE["visible_text"],
                    raw_value=brand_match.group(1).strip(),
                )
            )

    # Prices (visible + JSON-LD), availability, dates, and attributes.
    def _assign(unit: EvidenceUnit, *, entity: Optional[str] = None) -> None:
        """Re-tag a page-agnostic unit with the resolved primary entity."""
        target = entity or primary
        if not target:
            return
        inventory.add_unit(
            EvidenceUnit(
                entity=target,
                attribute=unit.attribute,
                value=unit.value,
                source=unit.source,
                source_type=unit.source_type,
                confidence=unit.confidence,
                raw_value=unit.raw_value,
                date=unit.date,
                qualifier=unit.qualifier,
                period=unit.period,
            )
        )

    # The page's *visible* identity (h1/title) scope for visible-text evidence.
    # It collapses onto the JSON-LD primary only when they are naming variants of
    # the same entity; otherwise a visible price belongs to the visible identity,
    # never to a structured entity it does not mention.
    visible_identity = ""
    if obs.h1 and obs.h1[0].strip():
        visible_identity = obs.h1[0].strip()
    elif obs.title and obs.title.strip():
        visible_identity = obs.title.strip()
    if _identity_compatible_with_primary(visible_identity, primary):
        visible_identity = primary

    for unit in _extract_visible_price_units(obs):
        _assign(unit, entity=visible_identity)

    currency_ctx: Optional[str] = None
    seen_offers: set[int] = set()
    for entity_dict in _iter_json_ld_entities(obs):
        currency_ctx = str(entity_dict.get("priceCurrency") or currency_ctx or "").strip() or None
        name = str(entity_dict.get("name") or "").strip()
        for offer in _offer_candidates(entity_dict):
            if id(offer) in seen_offers:
                continue
            seen_offers.add(id(offer))
            for unit in _extract_offer_price_units(offer, name, currency_ctx, source):
                if name and not _identity_compatible_with_primary(name, primary):
                    # Evidence from a different entity on the same page keeps its
                    # own identity label; it must not be attributed to the primary
                    # entity (prevents cross-entity price comparisons).
                    inventory.add_unit(unit)
                else:
                    _assign(unit)

    for unit in _extract_availability_units(obs):
        _assign(unit)

    for unit in _extract_date_units(obs):
        _assign(unit)

    for unit in _extract_attribute_units(obs):
        _assign(unit)

    return inventory


# ---------------------------------------------------------------------------
# Consistency classification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsistencyResult:
    """Outcome of classifying one (entity, attribute) evidence group."""

    entity: str
    attribute: str
    classification: ConsistencyClassification
    units: tuple[EvidenceUnit, ...]
    normalized_values: tuple[NormalizedValue, ...] = ()
    reason: str = ""


def _year_of(unit: EvidenceUnit) -> Optional[int]:
    if not unit.date:
        return None
    y = YEAR_PATTERN.search(str(unit.date))
    return int(y.group(1)) if y else None


def _identity_classification(units: Sequence[EvidenceUnit]) -> ConsistencyClassification:
    """Token-overlap based classification for name/brand/entity attributes.

    MATCH requires substantial shared identity vocabulary (>=2 significant
    tokens) or full containment where the contained claim is itself meaningful
    (>=2 tokens).  A single generic token (e.g. an "Acme" heading next to
    "Acme Logistics Corp" / "Zeta Financial Services") can neither confirm nor
    refute an entity, so it can never mask a genuine structured disagreement.
    """
    texts = [str(u.value) for u in units]
    token_sets = [_significant_tokens(t) for t in texts]
    clean_texts = [re.sub(r"\s+", " ", t.strip().lower()) for t in texts]

    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if len(token_sets[i] & token_sets[j]) >= 2:
                return "MATCH"
            a, b = clean_texts[i], clean_texts[j]
            if a and b and (a in b or b in a):
                if min(len(token_sets[i]), len(token_sets[j])) >= 2:
                    return "MATCH"

    # Only claims carrying a real identity vocabulary (>=2 significant tokens)
    # participate in a conflict decision.  Weak/single-token claims alone are
    # never worth a finding, and a weak claim must not veto a genuine
    # disagreement between two structured entities.
    strong_units = [u for u, s in zip(units, token_sets) if len(s) >= 2]
    if len(strong_units) >= 2:
        if any(u.source_type == "json_ld" for u in strong_units):
            return "CONFLICT"
        # A disagreement among weak/non-authoritative claims only (e.g. metadata
        # <title> vs marketing heading) is not a resolvable identity conflict.
        return "AMBIGUOUS"
    return "AMBIGUOUS"


def _temporal_is_historical(units: Sequence[tuple[EvidenceUnit, Optional[NormalizedValue]]]) -> bool:
    """HISTORICAL only across different pages with a truly older dated unit."""
    dated = [(u, year) for u, _ in units if (year := _year_of(u)) is not None]
    if not dated:
        return False
    and_undated = any(_year_of(u) is None for u, _ in units)
    sources = {u.source for u, _ in units}
    if len(sources) < 2:
        return False

    max_year = max(year for _, year in dated)
    has_old = any(year < max_year for _, year in dated)
    return has_old or (and_undated and any(year is not None for _, year in dated))


def classify_consistency(
    units: Sequence[EvidenceUnit],
    *,
    entity: Optional[str] = None,
    attribute: Optional[str] = None,
) -> ConsistencyResult:
    """Classify a single (entity, attribute) evidence group.

    Classifications:
        MATCH                 all comparable values agree
        CONFLICT              clear, same-context value disagreement (real problem)
        HISTORICAL            cross-page difference where an old dated value ≠ newer value
        AMBIGUOUS             legitimate variants / unknown identity / mixed context
        INSUFFICIENT_EVIDENCE fewer than two units or nothing comparable

    Conservative by design: any uncertainty resolves to AMBIGUOUS or
    INSUFFICIENT_EVIDENCE rather than a fabricated CONFLICT.
    """
    filtered = [u for u in units if (entity is None or u.entity == entity)]
    if attribute is not None:
        filtered = [u for u in filtered if u.attribute == attribute]

    if not filtered:
        return ConsistencyResult(
            entity=entity or "",
            attribute=attribute or "",
            classification="INSUFFICIENT_EVIDENCE",
            units=(),
            reason="no evidence units",
        )
    result_entity = filtered[0].entity
    result_attr = filtered[0].attribute

    if len(filtered) < 2:
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="INSUFFICIENT_EVIDENCE",
            units=tuple(filtered),
            reason="single evidence unit",
        )

    if result_attr in ("name", "brand", "entity"):
        cls = _identity_classification(filtered)
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification=cls,
            units=tuple(filtered),
            reason=f"identity comparison ({cls})",
        )

    normalized: list[tuple[EvidenceUnit, NormalizedValue]] = []
    for unit in filtered:
        nv = normalize_value(unit.attribute, unit.value, qualifier=unit.qualifier)
        if nv is not None:
            normalized.append((unit, nv))

    if len({nv.comparable for _, nv in normalized}) == 1:
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="MATCH",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="all comparable values agree",
        )

    if len(normalized) < 2:
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="INSUFFICIENT_EVIDENCE",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="not enough comparable values",
        )

    # Mixed kinds (e.g. a scalar price vs a price range) can never be equal contexts.
    if len({nv.kind for _, nv in normalized}) > 1:
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="AMBIGUOUS",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="mixed value kinds (range/scalar/date) cannot be compared",
        )

    # Legitimate variants: sale/original/from-qualified prices are never contradictions.
    if any(u.qualifier in ("sale", "original", "from") for u, _ in normalized):
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="AMBIGUOUS",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="price variants (sale/original/from) are legitimate",
        )

    # Different currencies are not comparable.
    if all(nv.kind == "price" for _, nv in normalized):
        currencies = {nv.comparable[1] for _, nv in normalized}
        if len(currencies) > 1:
            return ConsistencyResult(
                entity=result_entity,
                attribute=result_attr,
                classification="AMBIGUOUS",
                units=tuple(filtered),
                normalized_values=tuple(nv for _, nv in normalized),
                reason="different currency contexts",
            )
        if any(c == "?" for c in currencies):
            return ConsistencyResult(
                entity=result_entity,
                attribute=result_attr,
                classification="AMBIGUOUS",
                units=tuple(filtered),
                normalized_values=tuple(nv for _, nv in normalized),
                reason="price currency context missing",
            )

        # Different billing periods/units (monthly vs annual, per-user vs flat)
        # are never the same price claim.
        periods = {getattr(u, "period", None) or "" for u, _ in normalized}
        if len(periods) > 1:
            return ConsistencyResult(
                entity=result_entity,
                attribute=result_attr,
                classification="AMBIGUOUS",
                units=tuple(filtered),
                normalized_values=tuple(nv for _, nv in normalized),
                reason="different billing periods/units cannot be compared",
            )

    # Temporal reasoning (cross-page, explicitly dated evidence only).
    if _temporal_is_historical(normalized):
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="HISTORICAL",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="older dated value differs from a newer/undated value",
        )

    provenance_pairs = {(u.source, u.source_type) for u, _ in normalized}
    if len(provenance_pairs) < 2:
        return ConsistencyResult(
            entity=result_entity,
            attribute=result_attr,
            classification="AMBIGUOUS",
            units=tuple(filtered),
            normalized_values=tuple(nv for _, nv in normalized),
            reason="single-representation multi-value group cannot be attributed to one context",
        )

    return ConsistencyResult(
        entity=result_entity,
        attribute=result_attr,
        classification="CONFLICT",
        units=tuple(filtered),
        normalized_values=tuple(nv for _, nv in normalized),
        reason="same-context comparable values disagree",
    )


def classify_inventory(inventory: FactInventory) -> list[ConsistencyResult]:
    """Classify every (entity, attribute) group in the inventory."""
    results: list[ConsistencyResult] = []
    for (entity, attribute), units in inventory.groups():
        if attribute == "entity":
            continue
        results.append(classify_consistency(units, entity=entity, attribute=attribute))
    return results


# ---------------------------------------------------------------------------
# Finding generation
# ---------------------------------------------------------------------------

_FINDING_META: dict[str, tuple[str, str, int, str]] = {
    "price": ("EVID-PRC-001", "high", 1, "high"),
    "availability": ("EVID-AVT-001", "high", 1, "high"),
    "brand": ("EVID-ENT-001", "medium", 2, "medium"),
    "name": ("EVID-ENT-001", "medium", 2, "medium"),
    "entity": ("EVID-ENT-001", "medium", 2, "medium"),
}


def _provenance_unit_dict(unit: EvidenceUnit) -> dict[str, Any]:
    return {
        "entity": unit.entity,
        "attribute": unit.attribute,
        "raw_value": unit.raw_value if unit.raw_value is not None else unit.value,
        "value": unit.value,
        "source": unit.source,
        "source_type": unit.source_type,
        "confidence": unit.confidence,
        "date": unit.date,
        "qualifier": unit.qualifier,
    }


def _consistency_finding(result: ConsistencyResult) -> Finding:
    """Build ONE Finding from a CONFLICT result, preserving full provenance."""
    attr = result.attribute
    if attr in ("name", "brand", "entity"):
        finding_id, severity, priority, confidence = _FINDING_META["name"]
        title = "Conflicting entity identity signals"
    elif attr == "availability":
        finding_id, severity, priority, confidence = _FINDING_META["availability"]
        title = "Conflicting availability representations"
    elif attr == "price":
        finding_id, severity, priority, confidence = _FINDING_META["price"]
        title = "Conflicting price across site representations"
    else:
        finding_id, severity, priority, confidence = "EVID-ATT-001", "medium", 2, "high"
        title = "Conflicting product attribute representations"

    provenance = [_provenance_unit_dict(u) for u in result.units]
    summary_lines = [
        f"{p['raw_value']!r} was observed from {p['source_type']} on {p['source']}"
        for p in provenance
    ]
    evidence = (
        f"Evidence for entity '{result.entity}', attribute '{result.attribute}' "
        f"classifies as CONFLICT. {'. '.join(summary_lines)}."
    )

    if attr == "price":
        summary = "Reconcile pricing across structured data and visible representations so automated consumers receive one consistent price."
    elif attr == "availability":
        summary = "Synchronize stock status across structured data and visible representations."
    elif attr in ("name", "brand", "entity"):
        summary = "Synchronize identity across structured data and visible headings/text."
    else:
        summary = f"Verify the correct '{result.attribute}' value and synchronize all representations."

    return Finding(
        id=finding_id,
        title=title,
        severity=severity,
        evidence=evidence,
        suggested_action=SuggestedAction(summary=summary, priority=priority),
        priority=priority,
        confidence=confidence,
        details={
            "entity": result.entity,
            "attribute": result.attribute,
            "classification": result.classification,
            "evidence_units": provenance,
        },
        source_audit="evidence_consistency",
    )


def audit_evidence_consistency(obs: PageObservation) -> list[Finding]:
    """Run the Evidence & Consistency audit on one PageObservation.

    Returns findings only for meaningful CONFLICTs; MATCH, AMBIGUOUS, HISTORICAL,
    and INSUFFICIENT_EVIDENCE do not produce findings.
    """
    if not obs.is_success:
        return []

    inventory = build_fact_inventory(obs)
    findings: list[Finding] = []
    for result in classify_inventory(inventory):
        if result.classification == "CONFLICT":
            findings.append(_consistency_finding(result))
    return findings