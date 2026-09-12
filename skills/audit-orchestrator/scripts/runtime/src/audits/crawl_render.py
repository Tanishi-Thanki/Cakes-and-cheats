"""Crawl and Render Audit.

Evaluates whether technical delivery, accessibility, or rendering characteristics
prevent automated AI agents and web crawlers from reliably accessing and extracting
website content from the delivered representation.

Design Principles:
- Answers: "Is there evidence that important website content may be difficult for an
  AI crawler/agent to discover, access, or extract from the delivered representation?"
- Evidence before inference: reports only observed delivery and accessibility facts.
- Not a generic SEO checker: focuses on genuine AI discovery/access barriers.
- Conservative severity: avoids flagging normal redirects, minimal pages, or standard JS usage.
"""

from __future__ import annotations

import re
from typing import Optional
import urllib.parse

from src.audits.models import Finding, SuggestedAction
from src.observation.observation import PageObservation

# Keywords often indicating client-side placeholder shells or deferred rendering
CLIENT_RENDER_PLACEHOLDER_REGEX: re.Pattern[str] = re.compile(
    r"\b(enable javascript|requires javascript|loading application|loading\.\.\.|please wait|initializing app)\b",
    re.IGNORECASE,
)


def _check_access_failure(obs: PageObservation) -> Optional[Finding]:
    """Rule A (CR-001): Detect actual HTTP access failures or network errors.

    Triggers when the crawler was unable to successfully retrieve the page (4xx, 5xx,
    or connection/timeout errors).
    """
    if obs.is_success and (obs.status_code is None or obs.status_code < 400):
        return None

    status_str = str(obs.status_code) if obs.status_code is not None else "None"
    err_desc = obs.error_message or f"HTTP status {status_str}"
    target_url = obs.final_url or obs.requested_url

    evidence = (
        f"Observed access failure for URL '{target_url}': HTTP status code {status_str} ({err_desc}). "
        "If automated systems cannot retrieve the page successfully, they cannot reliably extract or cite its content."
    )

    return Finding(
        id="CR-001",
        title="Page is not reliably crawlable",
        severity="high",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary="Resolve the HTTP access failure and verify that the intended public URL returns a successful crawlable response.",
            priority=1,
        ),
        priority=1,
        confidence="high",
        details={
            "status_code": obs.status_code,
            "error_message": obs.error_message,
            "target_url": target_url,
        },
    )


def _check_render_gap(obs: PageObservation) -> Optional[Finding]:
    """Rule B (CR-002): Detect initial HTML / render gaps where content depends on client execution.

    Triggers when the delivered HTML representation exhibits *combined* structural shell evidence
    alongside negligible extractable visible text.  Combined evidence means:
      - four or more links together with at least one heading or image, OR
      - two or more headings together with two or more links.
    A bare count of links or headings alone is not sufficient to conclude that content
    is being withheld by client-side rendering.

    Guardrails:
    - Does NOT fire if visible text is sufficient (>= 25 words).
    - Does NOT fire on intentionally minimal pages (e.g. navigation/legal pages with few or
      no headings/images, even if they contain several links).
    - Does NOT fire on failed fetches (handled by CR-001).
    - Uses cautious language; does NOT claim "JavaScript makes this page inaccessible".
    """
    if not obs.is_success:
        return None

    words = obs.visible_text.split()
    word_count = len(words)

    # If the page already delivers substantive visible text, client-side rendering
    # is not withholding critical initial content.
    if word_count >= 25:
        return None

    num_headings = len(obs.h1) + len(obs.h2) + len(obs.h3)
    num_links = len(obs.links)
    num_images = len(obs.images)

    # Check for client-side placeholder phrasing
    has_placeholder = bool(CLIENT_RENDER_PLACEHOLDER_REGEX.search(obs.visible_text))

    # Meaningful structural shell signals.
    # Requires evidence of *both* navigation/content links AND heading or image
    # structure.  Bare link counts alone (e.g. a navigation/legal page) are not
    # sufficient to conclude that client-side rendering is withholding content.
    has_substantial_structure = (
        (num_links >= 4 and (num_headings >= 1 or num_images >= 1))
        or (num_headings >= 2 and num_links >= 2)
    )

    # Must have either explicit client placeholder phrasing or substantial shell structure with negligible text
    if not (has_placeholder or (has_substantial_structure and word_count < 20)):
        return None

    evidence = (
        f"Initial HTML exposes limited extractable content ({word_count} words) despite meaningful "
        f"page structure ({num_headings} headings, {num_links} links, {num_images} images). "
        "When an initial delivery contains structural shells but scant visible text, it indicates that "
        "substantive content may depend on client-side rendering. Automated crawlers that do not execute "
        "client-side JavaScript may extract insufficient information from the initial response."
    )

    return Finding(
        id="CR-002",
        title="Limited initial HTML content may require client-side rendering",
        severity="medium",
        evidence=evidence,
        suggested_action=SuggestedAction(
            summary=(
                "Expose critical product/entity information in server-delivered HTML or another reliably "
                "crawlable representation, and verify that essential facts are available without relying "
                "exclusively on client-side execution."
            ),
            priority=2,
        ),
        priority=2,
        confidence="medium",
        details={
            "visible_word_count": word_count,
            "num_headings": num_headings,
            "num_links": num_links,
            "num_images": num_images,
            "has_placeholder_text": has_placeholder,
        },
    )


def _check_problematic_redirect(obs: PageObservation) -> Optional[Finding]:
    """Rule C (CR-003): Detect problematic redirect behavior such as protocol downgrade.

    Triggers when a secure HTTPS request is redirected to an insecure HTTP destination.
    Normal protocol upgrades (http -> https) and standard path redirects to valid 200 pages
    are permitted and NOT flagged.
    """
    if not obs.requested_url or not obs.final_url:
        return None

    # Normal redirects where destination is valid and secure are not defects
    is_downgrade = obs.requested_url.startswith("https://") and obs.final_url.startswith("http://")

    if is_downgrade:
        evidence = (
            f"Request for secure URL '{obs.requested_url}' was redirected to the insecure HTTP "
            f"destination '{obs.final_url}'. The final destination is served over plain HTTP, meaning "
            "HTTPS transport protections (encryption, integrity, and certificate validation) are absent "
            "for the retrieved content."
        )
        return Finding(
            id="CR-003",
            title="Problematic redirect destination or protocol downgrade",
            severity="medium",
            evidence=evidence,
            suggested_action=SuggestedAction(
                summary="Ensure redirects preserve HTTPS encryption and do not downgrade secure requests to plaintext HTTP.",
                priority=2,
            ),
            priority=2,
            confidence="high",
            details={
                "requested_url": obs.requested_url,
                "final_url": obs.final_url,
                "type": "protocol_downgrade",
            },
        )

    return None


def _check_problematic_canonical(obs: PageObservation) -> Optional[Finding]:
    """Rule D (CR-004): Detect problematic or malformed canonical URL declarations.

    Triggers when a declared canonical URL uses an invalid scheme (e.g. javascript:, file:)
    or is unresolvable.
    Does NOT trigger if canonical is simply absent (absence alone is not a failure).
    """
    if not obs.canonical_url:
        return None

    try:
        parsed = urllib.parse.urlparse(obs.canonical_url)
    except Exception as parse_err:
        return Finding(
            id="CR-004",
            title="Problematic canonical declaration",
            severity="medium",
            evidence=f"Declared canonical URL '{obs.canonical_url}' is malformed and could not be parsed: {parse_err}.",
            suggested_action=SuggestedAction(
                summary="Update the canonical link element to reference a valid, well-formed absolute HTTP or HTTPS URL.",
                priority=2,
            ),
            priority=2,
            confidence="high",
            details={"canonical_url": obs.canonical_url},
        )

    # Canonical must have valid http or https scheme
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        evidence = (
            f"Page declares an invalid canonical URL '{obs.canonical_url}' with unsupported scheme "
            f"'{parsed.scheme or 'none'}'. Canonical declarations must specify valid HTTP or HTTPS "
            "URLs to establish authoritative indexing."
        )
        return Finding(
            id="CR-004",
            title="Problematic canonical declaration",
            severity="medium",
            evidence=evidence,
            suggested_action=SuggestedAction(
                summary="Update the canonical link element to reference a valid, absolute HTTP or HTTPS URL.",
                priority=2,
            ),
            priority=2,
            confidence="high",
            details={
                "canonical_url": obs.canonical_url,
                "scheme": parsed.scheme,
            },
        )

    return None


def audit_crawl_render(obs: PageObservation) -> list[Finding]:
    """Execute the Crawl & Render Audit on a PageObservation.

    Args:
        obs: PageObservation instance populated by the observation layer.

    Returns:
        List of Finding objects representing detected crawl or render accessibility issues.
    """
    findings: list[Finding] = []

    # Rule A: Actual Access Failure (HTTP 4xx/5xx or network failures)
    access_finding = _check_access_failure(obs)
    if access_finding is not None:
        findings.append(access_finding)
        # If the page failed to retrieve entirely, subsequent content-level render
        # checks are inapplicable.
        return findings

    # Rule B: Initial HTML / Render Gap
    render_finding = _check_render_gap(obs)
    if render_finding is not None:
        findings.append(render_finding)

    # Rule C: Problematic Redirect
    redirect_finding = _check_problematic_redirect(obs)
    if redirect_finding is not None:
        findings.append(redirect_finding)

    # Rule D: Problematic Canonical Declaration
    canonical_finding = _check_problematic_canonical(obs)
    if canonical_finding is not None:
        findings.append(canonical_finding)

    return findings
