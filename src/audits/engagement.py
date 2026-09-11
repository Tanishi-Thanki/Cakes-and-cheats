"""Engagement Audit Skill.

Evaluates post-click user orientation and intent continuity when a user lands on a
website following an AI agent recommendation or referral.

Focuses strictly on structural evidence extracted from PageObservation without
assuming live analytics, referral parameters, or external AI model APIs.

Design Principles:
- Evidence-backed findings: no finding without concrete observed structural facts.
- Conservative false-positive guardrails: prefer no finding when evidence is uncertain.
- Cautious language: problem -> evidence -> mechanism -> suggested action.
- Read-only, deterministic, provider-neutral.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# ---------------------------------------------------------------------------
# Dictionaries & Constants
# ---------------------------------------------------------------------------

GENERIC_LANDING_IDENTITIES: frozenset[str] = frozenset({
    "home",
    "homepage",
    "welcome",
    "welcome to our site",
    "main page",
    "index",
    "untitled",
    "default",
})

GENERIC_FALLBACK_HEADINGS: frozenset[str] = frozenset({
    "all products",
    "store directory",
    "category index",
    "page not found",
    "404",
    "404 not found",
    "products",
    "directory",
    "all items",
    "catalog",
})

GENERIC_PATH_SEGMENTS: frozenset[str] = frozenset({
    "",
    "index.html",
    "index.php",
    "home",
    "default.aspx",
    "search",
    "category",
    "categories",
    "products",
    "product",
    "item",
    "items",
    "page",
    "pages",
    "shop",
    "store",
    "catalog",
})

# Broad semantic action and continuation anchor keywords for ENG-003
CONTINUATION_ACTION_KEYWORDS: frozenset[str] = frozenset({
    # Action
    "buy",
    "order",
    "purchase",
    "checkout",
    "add to cart",
    "register",
    "sign up",
    "signup",
    "book",
    "reserve",
    "try",
    "apply",
    "download",
    # Inquiry
    "contact",
    "request quote",
    "get quote",
    "inquire",
    "get started",
    "schedule",
    # Exploration
    "specifications",
    "specs",
    "compare",
    "details",
    "view product",
    "view details",
    "learn more",
    "explore",
    "choose plan",
    "pricing",
    "plans",
})

# Step advancement anchor keywords for ENG-004
STEP_ADVANCEMENT_KEYWORDS: frozenset[str] = frozenset({
    "next",
    "continue",
    "proceed",
    "step 2",
    "step 3",
    "step 4",
    "next step",
    "go to step 2",
    "start",
    "submit",
    "begin",
})

# Commercial / product entity keywords for ENG-003 title/h1 identification
COMMERCIAL_ENTITY_KEYWORDS: frozenset[str] = frozenset({
    "product",
    "service",
    "plan",
    "pricing",
    "store",
    "buy",
    "shoe",
    "shoes",
    "tool",
    "app",
    "model",
    "item",
    "earbuds",
    "phone",
    "edition",
    "pro",
    "tier",
    "laptop",
    "software",
    "course",
    "device",
})

INFORMATIONAL_TITLE_KEYWORDS: frozenset[str] = frozenset({
    "article",
    "blog",
    "news",
    "guide",
    "tutorial",
    "how to",
    "review",
    "story",
    "post",
})


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------

def _is_root_path(url_str: str) -> bool:
    """Check if the URL path represents a root domain or homepage path."""
    if not url_str:
        return True
    try:
        parsed = urllib.parse.urlparse(url_str)
        path = parsed.path.strip().lower()
        return path in ("", "/", "/index.html", "/index.php", "/home", "/default.aspx")
    except Exception:
        return False


def _has_relevant_json_ld_schema(obs: PageObservation) -> bool:
    """Check if any JSON-LD script contains a Product, Service, Offer, or SoftwareApplication entity."""
    relevant_types = {"product", "service", "offer", "softwareapplication"}

    def _check_dict(d: dict[str, Any]) -> bool:
        obj_type = d.get("@type", "")
        if isinstance(obj_type, str) and obj_type.lower() in relevant_types:
            return True
        if isinstance(obj_type, list) and any(isinstance(t, str) and t.lower() in relevant_types for t in obj_type):
            return True
        for v in d.values():
            if isinstance(v, dict) and _check_dict(v):
                return True
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and _check_dict(item):
                        return True
        return False

    for block in obs.json_ld:
        if block.is_valid and isinstance(block.parsed_data, dict):
            if _check_dict(block.parsed_data):
                return True
        elif block.is_valid and isinstance(block.parsed_data, list):
            for item in block.parsed_data:
                if isinstance(item, dict) and _check_dict(item):
                    return True
    return False


def _extract_descriptive_slug_tokens(url_str: str) -> list[str]:
    """Extract descriptive, non-numeric slug tokens from a URL path.

    Filters out numeric IDs, file extensions, and generic path segments.
    """
    if not url_str:
        return []

    try:
        parsed = urllib.parse.urlparse(url_str)
        segments = [s.strip().lower() for s in parsed.path.split("/") if s.strip()]
    except Exception:
        return []

    tokens: list[str] = []
    for seg in segments:
        if seg in GENERIC_PATH_SEGMENTS:
            continue
        # Strip extension
        seg_clean = re.sub(r"\.(html|php|aspx|jsp)$", "", seg)
        # Split by hyphen or underscore
        raw_tokens = re.split(r"[_\-\s]+", seg_clean)
        for t in raw_tokens:
            t = t.strip()
            # Ignore numeric IDs, short codes (<=2 chars), or generic segments
            if len(t) > 2 and not t.isdigit() and t not in GENERIC_PATH_SEGMENTS:
                tokens.append(t)

    return tokens


# ---------------------------------------------------------------------------
# Rule Implementations
# ---------------------------------------------------------------------------

def _check_eng_001_generic_landing(obs: PageObservation) -> Optional[Finding]:
    """Check 1: ENG-001 — Generic Landing Orientation Deficit.

    Fires when a deep landing URL presents a generic orientation identity
    ("Home", "Welcome") with minimal body content, zero subheading structure,
    and no relevant product/service schema.
    """
    # Signal 1: URL is NOT root/homepage path
    target_url = obs.final_url or obs.requested_url
    if _is_root_path(target_url):
        return None

    # Signal 2: Title or primary H1 is a generic identity
    title_clean = (obs.title or "").strip().lower()
    h1_clean = (obs.h1[0].strip().lower()) if obs.h1 else ""

    is_generic_title = title_clean in GENERIC_LANDING_IDENTITIES
    is_generic_h1 = h1_clean in GENERIC_LANDING_IDENTITIES

    if not (is_generic_title or is_generic_h1):
        return None

    # Signal 3: len(h2) < 2
    if len(obs.h2) >= 2:
        return None

    # Signal 4: Visible text has fewer than 200 words
    words = [w for w in obs.visible_text.split() if w.strip()]
    word_count = len(words)
    if word_count >= 200:
        return None

    # Signal 5: No relevant Product, Service, Offer, or SoftwareApplication JSON-LD entity
    if _has_relevant_json_ld_schema(obs):
        return None

    evidence = (
        f"The deep landing page (URL: '{target_url}') presents a generic identity "
        f"(Title: '{obs.title or 'N/A'}', H1: '{obs.h1[0] if obs.h1 else 'N/A'}') "
        f"with minimal visible text ({word_count} words), fewer than 2 H2 subheadings, "
        "and no structured Product/Service schema. When a user arrives from an AI recommendation "
        "with a specific task, the page provides limited immediate orientation to continue."
    )

    return Finding(
        id="ENG-001",
        title="Generic Landing Page Orientation Deficit",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Provide a clear, task-oriented primary heading and summary section "
                "that explicitly identifies the product, service, or topic for arriving users."
            ),
            priority=2,
        ),
        priority=2,
        confidence="high",
        details={
            "target_url": target_url,
            "observed_title": obs.title,
            "observed_h1": obs.h1,
            "word_count": word_count,
            "h2_count": len(obs.h2),
        },
    )


def _check_eng_002_context_mismatch(obs: PageObservation) -> Optional[Finding]:
    """Check 2: ENG-002 — Specific Landing Context Mismatch.

    Fires when a deep URL slug specifies a target entity, but the page displays
    a generic fallback heading and fewer than 20% of slug tokens appear in visible body text.
    """
    target_url = obs.final_url or obs.requested_url
    if _is_root_path(target_url):
        return None

    # Signal 1: URL contains at least 2 descriptive multi-character slug tokens
    slug_tokens = _extract_descriptive_slug_tokens(target_url)
    if len(slug_tokens) < 2:
        return None

    # Signal 2: NONE of the descriptive slug tokens appear in title or H1 as whole words
    title_h1_text = " ".join([obs.title or ""] + obs.h1).lower()
    title_h1_words = set(re.findall(r"\b[a-z0-9]+\b", title_h1_text))
    tokens_in_title_h1 = [t for t in slug_tokens if t in title_h1_words]
    if tokens_in_title_h1:
        return None

    # Signal 3: Title or primary H1 is a generic/fallback heading
    title_clean = (obs.title or "").strip().lower()
    h1_clean = (obs.h1[0].strip().lower()) if obs.h1 else ""
    is_fallback = (title_clean in GENERIC_FALLBACK_HEADINGS) or (h1_clean in GENERIC_FALLBACK_HEADINGS)

    if not is_fallback:
        return None

    # Signal 4: Fewer than 20% of descriptive slug tokens appear in visible body text as whole words
    body_words = set(re.findall(r"\b[a-z0-9]+\b", obs.visible_text.lower()))
    tokens_in_body = [t for t in slug_tokens if t in body_words]
    match_ratio = len(tokens_in_body) / len(slug_tokens)

    if match_ratio >= 0.20:
        return None

    evidence = (
        f"The URL path specifies a specific resource slug ('{'/'.join(slug_tokens)}'), "
        f"but the landing page displays a generic fallback heading (Title: '{obs.title or 'N/A'}', "
        f"H1: '{obs.h1[0] if obs.h1 else 'N/A'}') and contains only {len(tokens_in_body)} of {len(slug_tokens)} "
        "slug tokens in its visible text. A user arriving from an AI recommendation for this path "
        "lands on a page where the expected context is missing."
    )

    return Finding(
        id="ENG-002",
        title="Specific Landing Context Mismatch",
        severity="high",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Ensure deep landing URLs present headings and content matching the target "
                "resource slug rather than displaying generic fallback category pages."
            ),
            priority=1,
        ),
        priority=1,
        confidence="high",
        details={
            "slug_tokens": slug_tokens,
            "matched_tokens_body": tokens_in_body,
            "match_ratio": round(match_ratio, 2),
        },
    )


def _check_eng_003_task_continuation(obs: PageObservation) -> Optional[Finding]:
    """Check 3: ENG-003 — Specific Task Continuation Deficit.

    Fires when a page title/H1 indicates a commercial/product/service entity,
    but zero visible links match continuation action categories and no contact/tel/mailto
    affordances exist.
    """
    title_h1_text = " ".join([obs.title or ""] + obs.h1).lower()

    # Exclude informational articles/blogs
    if any(k in title_h1_text for k in INFORMATIONAL_TITLE_KEYWORDS):
        return None

    # Signal 1: Title or H1 clearly indicates a commercial/product/plan/service entity
    has_commercial_entity = any(k in title_h1_text for k in COMMERCIAL_ENTITY_KEYWORDS)
    if not has_commercial_entity:
        return None

    # Signal 2: No meaningful visible continuation/action affordance observed in link anchors
    action_links = []
    for link in obs.links:
        anchor_lower = (link.anchor_text or "").strip().lower()
        if any(kw in anchor_lower for kw in CONTINUATION_ACTION_KEYWORDS):
            action_links.append(link)

    if len(action_links) > 0:
        return None

    # Signal 3: No alternative visible contact/continuation affordance (mailto, tel, form)
    for link in obs.links:
        raw_href = (link.raw_href or "").lower()
        if raw_href.startswith("mailto:") or raw_href.startswith("tel:"):
            return None

    evidence = (
        f"The landing page identifies a specific commercial entity (Title/H1: '{obs.title or obs.h1[0] if obs.h1 else 'N/A'}'), "
        f"but provides limited visible pathways for continuing the likely task from the observed links ({len(obs.links)} links observed). "
        "A user referred by an AI agent with a specific task may need to search or navigate manually to take next steps."
    )

    return Finding(
        id="ENG-003",
        title="Specific Task Continuation Deficit",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Include clear, visible action or exploratory links (e.g. view specs, "
                "check pricing, contact, or order options) on product and service landing pages."
            ),
            priority=2,
        ),
        priority=2,
        confidence="high",
        details={
            "observed_links": len(obs.links),
            "matching_action_links": 0,
        },
    )


def _check_eng_004_multistep_guidance(obs: PageObservation) -> Optional[Finding]:
    """Check 4: ENG-004 — Multi-Step Workflow Guidance Deficit.

    Fires when headings/title indicate an active multi-step workflow ("Step 1 of 3"),
    but zero links match step advancement anchor keywords.
    """
    heading_title_text = " ".join([obs.title or ""] + obs.h1 + obs.h2).lower()

    # Guardrail: Exclude static informational patterns like "5 steps to better health"
    if re.search(r"\b[0-9]+\s+steps\s+to\b", heading_title_text):
        return None

    # Signal 1: Explicit active workflow indicator in title/H1/H2
    active_workflow_patterns = (
        r"\bstep\s+[1-9]\s+of\b",
        r"\bphase\s+[1-9]\s+of\b",
        r"\bstep\s+[1-9]:\b",
        r"\baccount\s+registration\s*-\s*step\s+[1-9]\b",
        r"\bonboarding\s+step\s+[1-9]\b",
    )

    has_active_workflow = any(re.search(pat, heading_title_text) for pat in active_workflow_patterns)
    if not has_active_workflow:
        return None

    # Signal 2: No observed continuation/advancement pathway in link anchors
    advancement_links = []
    for link in obs.links:
        anchor_lower = (link.anchor_text or "").strip().lower()
        if any(kw in anchor_lower for kw in STEP_ADVANCEMENT_KEYWORDS):
            advancement_links.append(link)

    if len(advancement_links) > 0:
        return None

    evidence = (
        f"The landing page indicates an active multi-step workflow (Heading/Title: '{obs.title or obs.h1[0] if obs.h1 else 'N/A'}'), "
        "but provides zero visible step advancement links (e.g. Next, Continue, Proceed, Step 2) to move to the next stage. "
        "Users arriving from an AI referral cannot proceed smoothly to subsequent workflow steps."
    )

    return Finding(
        id="ENG-004",
        title="Multi-Step Workflow Guidance Deficit",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Provide explicit step advancement links or buttons (e.g. 'Next Step', 'Continue') "
                "to allow users to advance active multi-step workflows."
            ),
            priority=2,
        ),
        priority=2,
        confidence="high",
        details={
            "active_workflow_detected": True,
            "advancement_links_count": 0,
        },
    )


# ---------------------------------------------------------------------------
# Main Audit Entrypoint
# ---------------------------------------------------------------------------

def audit_engagement(obs: PageObservation) -> list[Finding]:
    """Execute the Engagement Audit against a PageObservation.

    Evaluates post-click user orientation and intent continuity across 4 rules:
    - ENG-001: Generic Landing Orientation Deficit
    - ENG-002: Specific Landing Context Mismatch
    - ENG-003: Specific Task Continuation Deficit
    - ENG-004: Multi-Step Workflow Guidance Deficit

    Args:
        obs: PageObservation extracted by the observation layer.

    Returns:
        List of Finding objects (empty list if page fetch failed or no issues found).
    """
    if not obs.is_success:
        return []

    findings: list[Finding] = []

    check_fns = [
        _check_eng_001_generic_landing,
        _check_eng_002_context_mismatch,
        _check_eng_003_task_continuation,
        _check_eng_004_multistep_guidance,
    ]

    for fn in check_fns:
        try:
            finding = fn(obs)
            if finding is not None:
                findings.append(finding)
        except Exception:  # noqa: BLE001
            # Exception isolation guardrail
            continue

    return findings
