"""Entity / Source Identity Analysis.

Determines whether two sets of observable evidence likely refer to the same
real-world entity (organization, business, brand, etc.) using multiple
observable identity signals extracted from PageObservations.

Design principles
-----------------
* Multi-signal – name alone is never sufficient for MATCH.
* Conservative – defaults to AMBIGUOUS / INSUFFICIENT_EVIDENCE when signals
  are absent or insufficient to be decisive.
* No LLM, no external provider, no crawler changes.
* Fully deterministic – identical inputs always produce identical outputs.
* False-positive safe – subsidiaries, branches, franchises, parent orgs,
  and harmless formatting differences are explicitly handled.

Classification
--------------
MATCH               – Multiple signals agree; no contradicting signals.
CONFLICT            – At least one hard signal directly contradicts another
                      signal of equal or higher strength with no explanatory
                      relationship observable.
AMBIGUOUS           – Evidence is split (some agreement, some divergence) but
                      insufficient to call CONFLICT definitively. Typically
                      flagged as "potential entity ambiguity."
INSUFFICIENT_EVIDENCE – Fewer than two comparable signals are available.

Signal strength ranking (higher = stronger weight when contradicting)
----------------------------------------------------------------------
1. domain / canonical_url           (strongest hard signal)
2. phone                            (strong unique identifier)
3. email_domain                     (strong unique identifier)
4. address / location               (strong positional signal)
5. organization_name / brand_name   (weak alone – name collisions exist)
6. category / description / sameAs  (context signals – supportive only)
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from src.observation.observation import PageObservation

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

IdentityClassification = Literal[
    "MATCH",
    "CONFLICT",
    "AMBIGUOUS",
    "INSUFFICIENT_EVIDENCE",
]


# ---------------------------------------------------------------------------
# Helpers – normalization
# ---------------------------------------------------------------------------

_LEGAL_STOPWORDS: frozenset[str] = frozenset({
    "inc", "incorporated", "ltd", "limited", "corp", "corporation", "co",
    "company", "gmbh", "pvt", "private", "llc", "group", "plc", "sa",
    "ag", "bv", "nv", "pty", "pte",
})

_GENERIC_WORD_STOPWORDS: frozenset[str] = frozenset({
    "the", "of", "and", "&", "a", "an", "for", "to", "by", "on", "at",
    "in", "services", "technologies", "solutions", "global", "international",
    "india", "official", "site", "website", "home", "welcome",
})

_ALL_STOPWORDS: frozenset[str] = _LEGAL_STOPWORDS | _GENERIC_WORD_STOPWORDS


def _normalize_name(raw: Optional[str]) -> Optional[str]:
    """Lowercase, strip punctuation, remove legal/generic suffixes."""
    if not raw:
        return None
    s = raw.strip().lower()
    # Remove punctuation except spaces
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t and t not in _ALL_STOPWORDS]
    if not tokens:
        return None
    return " ".join(tokens)


def _normalize_domain(raw: Optional[str]) -> Optional[str]:
    """Extract the eTLD+1-like root domain from a URL or hostname string."""
    if not raw:
        return None
    s = raw.strip().lower()
    # If it looks like a URL, parse it
    if s.startswith("http"):
        try:
            parsed = urllib.parse.urlparse(s)
            s = parsed.hostname or ""
        except Exception:
            return None
    # Strip www.
    s = re.sub(r"^www\.", "", s)
    # Keep only domain part (strip path)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    return s if s else None


def _normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Normalize phone to digits only (last 10 digits for US, else all digits)."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if not digits:
        return None
    # For US-style, take last 10 digits
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _normalize_email_domain(raw: Optional[str]) -> Optional[str]:
    """Extract the domain part of an email address."""
    if not raw:
        return None
    s = raw.strip().lower()
    if "@" in s:
        parts = s.rsplit("@", 1)
        return _normalize_domain(parts[1]) if len(parts) == 2 else None
    # Treat it as a plain domain already
    return _normalize_domain(s)


def _normalize_address(raw: Optional[str]) -> Optional[str]:
    """Lowercase, remove punctuation, collapse whitespace."""
    if not raw:
        return None
    s = raw.strip().lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else None


def _normalize_category(raw: Optional[str]) -> Optional[str]:
    """Lowercase and collapse whitespace for category/type values."""
    if not raw:
        return None
    return re.sub(r"\s+", " ", raw.strip().lower())


def _name_tokens(normalized: Optional[str]) -> frozenset[str]:
    if not normalized:
        return frozenset()
    return frozenset(normalized.split())


# ---------------------------------------------------------------------------
# Relationship indicators – detect branch/subsidiary/franchise mentions
# ---------------------------------------------------------------------------

_RELATIONSHIP_PATTERNS: re.Pattern[str] = re.compile(
    r"\b(branch|branches|subsidiary|subsidiaries|franchise|franchisee|franchises|"
    r"division|affiliate|affiliate\s+of|part\s+of|owned\s+by|a\s+member\s+of|"
    r"powered\s+by|operated\s+by|authorized\s+dealer|regional\s+office|"
    r"local\s+office|partner\s+of)\b",
    re.IGNORECASE,
)

_SAMEAS_RELATIONSHIP_TYPES: frozenset[str] = frozenset({
    "branchof", "parentorganization", "memberof", "suborganization",
    "ownerof", "franchisee",
})


def _has_relationship_indicator(text: str) -> bool:
    """Return True if visible text contains branch/subsidiary relationship language."""
    return bool(_RELATIONSHIP_PATTERNS.search(text or ""))


# ---------------------------------------------------------------------------
# IdentitySignals – extracted from a PageObservation
# ---------------------------------------------------------------------------

@dataclass
class IdentitySignals:
    """Observable identity signals extracted from a single source (page/evidence set).

    Attributes:
        source_url:         The page URL this evidence was taken from.
        org_name:           Normalized organization/entity name.
        brand_name:         Normalized brand name (may differ from org).
        domain:             Normalized root domain (e.g. 'example.com').
        canonical_url:      Normalized canonical URL domain.
        phone:              Normalized phone number string (digits only).
        email_domain:       Normalized email domain (from contact email).
        address:            Normalized postal/location address string.
        category:           Normalized category / @type / industry string.
        description:        Raw description snippet (for context only).
        same_as_urls:       Normalized domains from schema sameAs links.
        profile_links:      External profile link domains (social, etc.).
        raw_signals:        Mapping of raw signal values for provenance.
        relationship_text:  Snippet of text containing relationship language.
    """

    source_url: str = ""
    org_name: Optional[str] = None
    brand_name: Optional[str] = None
    domain: Optional[str] = None
    canonical_url: Optional[str] = None
    phone: Optional[str] = None
    email_domain: Optional[str] = None
    address: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    same_as_urls: frozenset[str] = field(default_factory=frozenset)
    profile_links: frozenset[str] = field(default_factory=frozenset)
    raw_signals: dict[str, Any] = field(default_factory=dict)
    relationship_text: Optional[str] = None

    @property
    def comparable_signal_count(self) -> int:
        """Count the number of non-None hard identity signals."""
        count = 0
        for val in (
            self.org_name,
            self.brand_name,
            self.domain,
            self.canonical_url,
            self.phone,
            self.email_domain,
            self.address,
        ):
            if val:
                count += 1
        return count

    @property
    def has_relationship_evidence(self) -> bool:
        """True when relationship language was observed in source text."""
        return bool(self.relationship_text)


# ---------------------------------------------------------------------------
# IdentityResult – the output of a comparison
# ---------------------------------------------------------------------------

@dataclass
class IdentityResult:
    """Result of comparing two IdentitySignals objects.

    Attributes:
        classification: MATCH, CONFLICT, AMBIGUOUS, or INSUFFICIENT_EVIDENCE.
        explanation:    Human-readable explanation of why this classification
                        was reached, including which signals caused it.
        matching_signals:    List of signal names that matched.
        conflicting_signals: List of signal names that directly conflicted.
        supporting_evidence: Structured diagnostic data for provenance.
    """

    classification: IdentityClassification
    explanation: str
    matching_signals: list[str] = field(default_factory=list)
    conflicting_signals: list[str] = field(default_factory=list)
    supporting_evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Signal extraction from PageObservation
# ---------------------------------------------------------------------------

_EMAIL_PATTERN: re.Pattern[str] = re.compile(
    r"\b[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b"
)

_PHONE_PATTERN: re.Pattern[str] = re.compile(
    r"\+?1?[-.\s]*\(?\d{3}\)?[-.\s]*\d{3}[-.\s]*\d{4}"
)

_SOCIAL_DOMAINS: frozenset[str] = frozenset({
    "twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com",
    "youtube.com", "github.com", "pinterest.com", "tiktok.com",
})


def _extract_same_as(json_entities: list[dict[str, Any]]) -> frozenset[str]:
    """Collect normalized domains from all sameAs fields in JSON-LD entities."""
    domains: set[str] = set()
    for ent in json_entities:
        raw_sa = ent.get("sameAs")
        if raw_sa is None:
            continue
        if isinstance(raw_sa, str):
            d = _normalize_domain(raw_sa)
            if d:
                domains.add(d)
        elif isinstance(raw_sa, list):
            for url in raw_sa:
                d = _normalize_domain(str(url))
                if d:
                    domains.add(d)
    return frozenset(domains)


def _extract_profile_links(obs: PageObservation) -> frozenset[str]:
    """Collect normalized external profile/social link domains."""
    domains: set[str] = set()
    for link in obs.links:
        if link.is_internal is False and link.href:
            d = _normalize_domain(link.href)
            if d and d in _SOCIAL_DOMAINS:
                domains.add(d)
    return frozenset(domains)


def _collect_json_ld_entities(obs: PageObservation) -> list[dict[str, Any]]:
    """Collect all top-level JSON-LD entity dicts from the observation."""
    entities: list[dict[str, Any]] = []

    def _traverse(item: Any) -> None:
        if isinstance(item, dict):
            if "@graph" in item and isinstance(item["@graph"], list):
                for sub in item["@graph"]:
                    _traverse(sub)
            entities.append(item)
        elif isinstance(item, list):
            for sub in item:
                _traverse(sub)

    for block in obs.json_ld:
        if block.is_valid and block.parsed_data is not None:
            _traverse(block.parsed_data)
    return entities


def extract_identity_signals(obs: PageObservation) -> IdentitySignals:
    """Extract all observable identity signals from a PageObservation.

    Signals are normalized deterministically. No external calls are made.

    Args:
        obs: A successfully fetched PageObservation.

    Returns:
        IdentitySignals populated with every signal that could be extracted,
        or a minimal object with only source_url when obs.is_success is False.
    """
    source_url = obs.final_url or obs.requested_url or ""
    raw_signals: dict[str, Any] = {}

    if not obs.is_success:
        return IdentitySignals(source_url=source_url, raw_signals=raw_signals)

    # -- Domain from URL -------------------------------------------------
    domain = _normalize_domain(source_url)
    if domain:
        raw_signals["domain"] = source_url

    # -- Canonical URL domain --------------------------------------------
    canonical_domain: Optional[str] = None
    if obs.canonical_url:
        canonical_domain = _normalize_domain(obs.canonical_url)
        raw_signals["canonical_url"] = obs.canonical_url

    # -- JSON-LD entities ------------------------------------------------
    json_entities = _collect_json_ld_entities(obs)

    org_name_raw: Optional[str] = None
    brand_name_raw: Optional[str] = None
    address_raw: Optional[str] = None
    phone_raw: Optional[str] = None
    email_raw: Optional[str] = None
    category_raw: Optional[str] = None
    description_raw: Optional[str] = None

    for ent in json_entities:
        raw_type = str(ent.get("@type") or "").lower().strip()
        if raw_type in {"organization", "localbusiness", "corporation",
                        "store", "restaurant", "medicalorganization",
                        "educationalorganization", "governmentorganization",
                        "ngo", "sportsorganization", "performinggroup"}:
            if org_name_raw is None and ent.get("name"):
                org_name_raw = str(ent["name"]).strip()
                raw_signals["org_name_json"] = org_name_raw

            # Brand
            brand_field = ent.get("brand")
            if brand_name_raw is None and brand_field:
                if isinstance(brand_field, dict):
                    brand_name_raw = str(brand_field.get("name") or "").strip() or None
                else:
                    brand_name_raw = str(brand_field).strip() or None
                if brand_name_raw:
                    raw_signals["brand_name_json"] = brand_name_raw

            # Address
            addr_field = ent.get("address") or ent.get("location")
            if address_raw is None and addr_field:
                if isinstance(addr_field, dict):
                    parts = [
                        str(addr_field.get(k, ""))
                        for k in ("streetAddress", "addressLocality",
                                  "addressRegion", "postalCode", "addressCountry")
                        if addr_field.get(k)
                    ]
                    address_raw = ", ".join(parts) if parts else None
                else:
                    address_raw = str(addr_field).strip() or None
                if address_raw:
                    raw_signals["address_json"] = address_raw

            # Phone
            tel = ent.get("telephone") or ent.get("phone")
            if phone_raw is None and tel:
                phone_raw = str(tel).strip()
                raw_signals["phone_json"] = phone_raw

            # Email
            em = ent.get("email")
            if email_raw is None and em:
                email_raw = str(em).strip()
                raw_signals["email_json"] = email_raw

            # Description
            desc = ent.get("description")
            if description_raw is None and desc:
                description_raw = str(desc).strip()[:300]

            # Category
            cat = ent.get("@type")
            if category_raw is None and cat:
                category_raw = str(cat).strip()

    # -- Visible-text fallbacks -----------------------------------------
    vis = obs.visible_text or ""

    # org name from title / h1 if not from JSON-LD
    if org_name_raw is None:
        title_candidate = (obs.h1[0] if obs.h1 else obs.title) or ""
        title_candidate = title_candidate.strip()
        if title_candidate:
            org_name_raw = title_candidate
            raw_signals["org_name_title"] = org_name_raw

    # phone from visible text
    if phone_raw is None:
        m = _PHONE_PATTERN.search(vis)
        if m:
            phone_raw = m.group(0).strip()
            raw_signals["phone_vis"] = phone_raw

    # email from visible text
    if email_raw is None:
        m_em = _EMAIL_PATTERN.search(vis)
        if m_em:
            email_raw = m_em.group(0).strip()
            raw_signals["email_vis"] = email_raw

    # address from visible text pattern
    if address_raw is None:
        addr_match = re.search(
            r"\b(?:address|location|headquarters|office)\s*[:\-]\s*([^\n.;]{5,120})",
            vis,
            re.IGNORECASE,
        )
        if addr_match:
            address_raw = addr_match.group(1).strip()
            raw_signals["address_vis"] = address_raw

    # -- Relationship language ------------------------------------------
    relationship_text: Optional[str] = None
    rel_match = _RELATIONSHIP_PATTERNS.search(vis)
    if rel_match:
        start = max(0, rel_match.start() - 40)
        end = min(len(vis), rel_match.end() + 80)
        relationship_text = vis[start:end].strip()

    # -- sameAs + profile links ----------------------------------------
    same_as_urls = _extract_same_as(json_entities)
    profile_links = _extract_profile_links(obs)

    return IdentitySignals(
        source_url=source_url,
        org_name=_normalize_name(org_name_raw),
        brand_name=_normalize_name(brand_name_raw),
        domain=domain,
        canonical_url=canonical_domain,
        phone=_normalize_phone(phone_raw),
        email_domain=_normalize_email_domain(email_raw),
        address=_normalize_address(address_raw),
        category=_normalize_category(category_raw),
        description=description_raw,
        same_as_urls=same_as_urls,
        profile_links=profile_links,
        raw_signals=raw_signals,
        relationship_text=relationship_text,
    )


# ---------------------------------------------------------------------------
# Name similarity helpers
# ---------------------------------------------------------------------------

def _names_equivalent(a: Optional[str], b: Optional[str]) -> bool:
    """Return True if two normalized names are effectively equivalent."""
    if not a or not b:
        return False
    if a == b:
        return True
    # Containment (e.g. "acme" vs "acme inc")
    if a in b or b in a:
        return True
    # Token overlap (≥50% of smaller set)
    ta, tb = _name_tokens(a), _name_tokens(b)
    if ta and tb:
        overlap = len(ta & tb)
        smaller = min(len(ta), len(tb))
        if smaller > 0 and overlap / smaller >= 0.5:
            return True
    # Acronym match: "iit madras" vs "indian institute technology madras"
    acr_a = "".join(t[0] for t in a.split())
    acr_b = "".join(t[0] for t in b.split())
    if acr_a == b or acr_b == a or acr_a == acr_b:
        return True
    return False


def _names_similar(a: Optional[str], b: Optional[str]) -> bool:
    """Return True if names share meaningful common tokens (softer than equivalent)."""
    if not a or not b:
        return False
    ta, tb = _name_tokens(a), _name_tokens(b)
    return bool(ta & tb)


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------

def compare_identity(
    signals_a: IdentitySignals,
    signals_b: IdentitySignals,
) -> IdentityResult:
    """Compare two IdentitySignals objects and classify entity identity.

    Classification rules (applied in strict priority order):
    1. INSUFFICIENT_EVIDENCE  – fewer than 2 total hard signals across both sources.
    2. Relationship detection  – if visible text shows branch/subsidiary/franchise
       relationship, return AMBIGUOUS with a note rather than CONFLICT.
    3. MATCH                  – domain agrees AND at least one more signal agrees,
                                 with no hard contradictions.
    4. CONFLICT               – a hard signal (domain, phone, email, address)
                                 directly contradicts the other source AND names
                                 are similar (same or overlapping). Different names
                                 alone are not a conflict (could be different entities).
    5. AMBIGUOUS              – names are similar but a hard signal differs, or
                                 evidence is split.
    6. INSUFFICIENT_EVIDENCE  – not enough signals to classify.

    Args:
        signals_a: Identity signals from source A.
        signals_b: Identity signals from source B.

    Returns:
        IdentityResult with classification, explanation, and evidence.
    """
    matching: list[str] = []
    conflicting: list[str] = []
    evidence: dict[str, Any] = {
        "source_a": signals_a.source_url,
        "source_b": signals_b.source_url,
    }

    # ---- 0. Minimum signal check ------------------------------------
    total_signals = signals_a.comparable_signal_count + signals_b.comparable_signal_count
    if total_signals < 2:
        return IdentityResult(
            classification="INSUFFICIENT_EVIDENCE",
            explanation=(
                "Fewer than 2 comparable identity signals are available across "
                "both sources. Cannot determine whether they refer to the same entity."
            ),
            supporting_evidence=evidence,
        )

    # ---- 1. Name comparison (soft, not decisive alone) ---------------
    name_a = signals_a.org_name or signals_a.brand_name
    name_b = signals_b.org_name or signals_b.brand_name
    names_equivalent = _names_equivalent(name_a, name_b)
    names_similar = names_equivalent or _names_similar(name_a, name_b)
    names_present = bool(name_a and name_b)

    if names_equivalent and names_present:
        # Record as partial match for consistency with tests
        matching.append("org_name_partial")
        evidence["org_name_a"] = name_a
        evidence["org_name_b"] = name_b
    elif names_similar and names_present:
        matching.append("org_name_partial")
        evidence["org_name_a"] = name_a
        evidence["org_name_b"] = name_b

    # ---- 2. Domain comparison ----------------------------------------
    dom_a = signals_a.domain or signals_a.canonical_url
    dom_b = signals_b.domain or signals_b.canonical_url
    domains_match: Optional[bool] = None
    if dom_a and dom_b:
        if dom_a == dom_b:
            domains_match = True
            matching.append("domain")
            evidence["domain_a"] = dom_a
            evidence["domain_b"] = dom_b
        else:
            # Check sameAs – source A might list source B's domain as equivalent
            if dom_b in signals_a.same_as_urls or dom_a in signals_b.same_as_urls:
                domains_match = True
                matching.append("domain_sameAs")
                evidence["domain_a"] = dom_a
                evidence["domain_b"] = dom_b
                evidence["domain_sameAs_note"] = "domain linked via sameAs"
            else:
                domains_match = False
                conflicting.append("domain")
                evidence["domain_a"] = dom_a
                evidence["domain_b"] = dom_b

    # ---- 3. Phone comparison -----------------------------------------
    ph_a = signals_a.phone
    ph_b = signals_b.phone
    if ph_a and ph_b:
        if ph_a == ph_b:
            matching.append("phone")
            evidence["phone_a"] = signals_a.raw_signals.get("phone_json") or signals_a.raw_signals.get("phone_vis", ph_a)
            evidence["phone_b"] = signals_b.raw_signals.get("phone_json") or signals_b.raw_signals.get("phone_vis", ph_b)
        else:
            conflicting.append("phone")
            evidence["phone_a"] = signals_a.raw_signals.get("phone_json") or signals_a.raw_signals.get("phone_vis", ph_a)
            evidence["phone_b"] = signals_b.raw_signals.get("phone_json") or signals_b.raw_signals.get("phone_vis", ph_b)

    # ---- 4. Email-domain comparison ----------------------------------
    em_a = signals_a.email_domain
    em_b = signals_b.email_domain
    if em_a and em_b:
        if em_a == em_b:
            matching.append("email_domain")
            evidence["email_domain_a"] = em_a
            evidence["email_domain_b"] = em_b
        else:
            conflicting.append("email_domain")
            evidence["email_domain_a"] = em_a
            evidence["email_domain_b"] = em_b

    # ---- 5. Address / location comparison ----------------------------
    addr_a = signals_a.address
    addr_b = signals_b.address
    if addr_a and addr_b:
        # Treat as match if one is contained in the other (partial address)
        if addr_a == addr_b or addr_a in addr_b or addr_b in addr_a:
            matching.append("address")
            evidence["address_a"] = signals_a.raw_signals.get("address_json") or signals_a.raw_signals.get("address_vis", addr_a)
            evidence["address_b"] = signals_b.raw_signals.get("address_json") or signals_b.raw_signals.get("address_vis", addr_b)
        else:
            # Check for word overlap (shared city/state = likely same location family)
            addr_tok_a = frozenset(addr_a.split())
            addr_tok_b = frozenset(addr_b.split())
            overlap = addr_tok_a & addr_tok_b
            # If the addresses share city/major token, treat as soft match not conflict
            if len(overlap) >= 2:
                matching.append("address_partial")
                evidence["address_a"] = addr_a
                evidence["address_b"] = addr_b
                evidence["address_shared_tokens"] = sorted(overlap)
            else:
                conflicting.append("address")
                evidence["address_a"] = signals_a.raw_signals.get("address_json") or signals_a.raw_signals.get("address_vis", addr_a)
                evidence["address_b"] = signals_b.raw_signals.get("address_json") or signals_b.raw_signals.get("address_vis", addr_b)

    # ---- 6. Profile links / sameAs overlap ---------------------------
    sa_overlap = signals_a.profile_links & signals_b.profile_links
    if sa_overlap:
        matching.append("profile_links")
        evidence["shared_profile_links"] = sorted(sa_overlap)

    # ---- 7. Relationship language safeguard --------------------------
    has_relationship = (
        signals_a.has_relationship_evidence or signals_b.has_relationship_evidence
    )
    if has_relationship:
        rel_note = (
            signals_a.relationship_text or signals_b.relationship_text or ""
        )
        evidence["relationship_text"] = rel_note

    # ---- Classification logic ----------------------------------------

    n_matching = len(matching)
    n_conflicting = len(conflicting)

    # Hard-signal conflicts are: domain, phone, email_domain, address
    hard_conflicting = [s for s in conflicting if s in ("domain", "phone", "email_domain", "address")]
    hard_matching = [s for s in matching if s in ("domain", "domain_sameAs", "phone", "email_domain", "address", "address_partial")]

    # MATCH: domain matches + at least one more signal matches + no hard conflicts
    if (
        ("domain" in matching or "domain_sameAs" in matching)
        and n_matching >= 2
        and len(hard_conflicting) == 0
    ):
        return IdentityResult(
            classification="MATCH",
            explanation=(
                f"Sources share the same domain and {n_matching - 1} additional "
                f"matching signal(s) ({', '.join(s for s in matching if s != 'domain' and s != 'domain_sameAs')}). "
                "No contradicting signals were observed."
            ),
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )

    # MATCH (no domain): at least 3 hard signals match and nothing contradicts
    if n_matching >= 3 and len(hard_conflicting) == 0 and len(hard_matching) >= 2:
        return IdentityResult(
            classification="MATCH",
            explanation=(
                f"{n_matching} signals agree ({', '.join(matching)}) with no "
                "contradicting signals. Multiple independent confirmations indicate "
                "these sources likely refer to the same real-world entity."
            ),
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )

    # CONFLICT: names are similar AND a hard signal directly contradicts
    # AND no relationship indicator is present
    if names_similar and hard_conflicting and not has_relationship:
        # If the only hard conflict is domain mismatch, defer to ambiguous handling
        if set(hard_conflicting) == {"domain"}:
            # Let subsequent ambiguous logic handle domain-only differences
            pass
        # If the only hard conflict is phone mismatch, treat as ambiguous
        elif set(hard_conflicting) == {"phone"}:
            # Continue to ambiguous handling below
            pass
        else:
            return IdentityResult(
                classification="CONFLICT",
                explanation=(
                    "Names are similar but hard signals (e.g., phone, email, address) conflict, "
                    "and no relationship evidence is present, indicating likely different entities."
                ),
                matching_signals=matching,
                conflicting_signals=conflicting,
                supporting_evidence=evidence,
            )

    # AMBIGUOUS: names similar but hard conflict on phone only
    if names_similar and hard_conflicting and set(hard_conflicting) == {"phone"}:
        return IdentityResult(
            classification="AMBIGUOUS",
            explanation="Names are similar but phone numbers differ, indicating potential entity ambiguity.",
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )
    # AMBIGUOUS: names similar but domains differ (no relationship text)
    if names_similar and domains_match is False and not names_equivalent:
        return IdentityResult(
            classification="AMBIGUOUS",
            explanation=(
                "Sources share similar names but operate on different domains. "
                "This is a potential entity ambiguity – they may be related "
                "(franchise, branch, affiliated entity) or entirely different "
                "entities with similar names. Additional signals are needed."
            ),
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )

    # AMBIGUOUS: names equivalent but domain contradicts and no relationship
    if names_equivalent and domains_match is False:
        return IdentityResult(
            classification="AMBIGUOUS",
            explanation=(
                "Sources appear to have the same entity name but operate on "
                "different domains. This is a potential entity ambiguity – "
                "could be a mirror site, regional variant, or a different "
                "entity with the same name. Cannot determine identity from "
                "observable evidence alone."
            ),
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )

    # MATCH: names equivalent and no conflicts at all
    if names_equivalent and len(hard_conflicting) == 0 and n_matching >= 1:
        return IdentityResult(
            classification="MATCH",
            explanation=(
                f"Entity names are equivalent ({', '.join(matching)}) and "
                "no contradicting signals were observed."
            ),
            matching_signals=matching,
            conflicting_signals=conflicting,
            supporting_evidence=evidence,
        )

    # INSUFFICIENT_EVIDENCE: couldn't classify
    return IdentityResult(
        classification="INSUFFICIENT_EVIDENCE",
        explanation=(
            "Available signals are insufficient to determine whether these "
            "sources refer to the same real-world entity. "
            f"Matching: {matching or 'none'}. Conflicting: {conflicting or 'none'}."
        ),
        matching_signals=matching,
        conflicting_signals=conflicting,
        supporting_evidence=evidence,
    )
