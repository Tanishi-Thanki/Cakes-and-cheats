"""T4 deterministic observation audit – indexability, robots, canonical, structured data.

Provides `audit_t4_indexability` which returns a list of `Finding` objects based on a
`PageObservation`.
"""

from __future__ import annotations

from typing import List
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.audits.domain_utils import registrable_domain
from src.audits.models import Finding, SuggestedAction
from src.observation.observation import (
    JsonLdObservation,
    PageObservation,
)

# Well-known tracking / campaign parameters that do not change the resource
# identity of a URL. Stripped only for canonical-vs-final *semantic* comparison;
# the crawler and url_utils normalization are untouched.
_TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "gclsrc", "dclid", "wbraid", "gbraid", "fbclid",
    "msclkid", "igshid", "yclid", "ymclid", "mc_cid", "mc_eid",
    "_ga", "_gl", "_gac", "_gaexp", "_hsenc", "_hsmi",
    "__hstc", "__hssc", "__hsfp", "hsCtaTracking", "hsLang",
    "mtm_source", "mtm_medium", "mtm_campaign", "mtm_cid", "mtm_keyword",
    "pk_source", "pk_medium", "pk_campaign", "pk_cid", "pk_keyword",
})


def _semantic_url(url: str) -> str:
    """Normalize a URL for semantic comparison (canonical vs final).

    Applies schemeless normalization: default ports are folded, fragments are
    dropped, trailing slashes are collapsed, and well-known tracking parameters
    are removed while every other query parameter is preserved (sorted for a
    deterministic comparison). Host-affinity decisions use registrable-domain
    comparison separately (see _canonical_findings), so ``www`` vs bare-host
    variants of the same site are never treated as external.

    Args:
        url: A URL string.

    Returns:
        A normalized URL string suitable for equality comparison.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None:
        if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
            netloc = host
        else:
            netloc = f"{host}:{port}"
    else:
        netloc = host
    path = parsed.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))
    return urlunparse((scheme, netloc, path, "", query, ""))

# ---------------------------------------------------------------------------
# Helper functions for each signal group
# ---------------------------------------------------------------------------

def _indexability_findings(obs: PageObservation) -> List[Finding]:
    findings: List[Finding] = []

    # 1. Fetch failure
    if not obs.is_success:
        findings.append(
            Finding(
                id="T4-001",
                title="Page fetch failed",
                severity="high",
                evidence=f"Fetch failed for URL '{obs.requested_url}': {obs.error_message or 'unknown error'}",
                suggested_action=SuggestedAction(
                    summary="Investigate network or server errors and ensure the URL is reachable.",
                    priority=1,
                ),
                priority=1,
                confidence="high",
            )
        )
        return findings

    # 2. Non‑HTML content
    ct = obs.content_type.lower() if obs.content_type else ""
    if ct and not ct.startswith("text/html"):
        findings.append(
            Finding(
                id="T4-002",
                title="Non‑HTML response",
                severity="medium",
                evidence=f"Content‑Type '{obs.content_type}' indicates non‑HTML payload.",
                suggested_action=SuggestedAction(
                    summary="Only HTML pages can be analysed for SEO signals.",
                    priority=2,
                ),
                priority=2,
                confidence="high",
            )
        )

    # 3. Explicit noindex / index handling
    directives = set(obs.robots_meta_directives) | set(obs.x_robots_tag_directives)
    # Treat 'none' as equivalent to 'noindex' for indexability decisions
    if "none" in directives:
        directives.add("noindex")
    # Determine source attribution for noindex/none directives
    source = []
    if "noindex" in obs.robots_meta_directives or "none" in obs.robots_meta_directives:
        source.append("meta")
    if "noindex" in obs.x_robots_tag_directives or "none" in obs.x_robots_tag_directives:
        source.append("header")
    # Add finding for noindex (including none) if present
    if "noindex" in directives:
        findings.append(
            Finding(
                id="T4-003",
                title="Explicit noindex observed",
                severity="high",
                evidence=f"noindex directive found in {', '.join(source)}",
                suggested_action=SuggestedAction(
                    summary="Remove noindex directive if the page should be indexed.",
                    priority=1,
                ),
                priority=1,
                confidence="high",
            )
        )
    elif "index" in directives:
        findings.append(
            Finding(
                id="T4-004",
                title="Explicit index observed",
                severity="low",
                evidence="index directive present without noindex",
                suggested_action=SuggestedAction(
                    summary="No action required; page is explicitly indexable.",
                    priority=3,
                ),
                priority=3,
                confidence="high",
                classification="observation",
            )
        )
    else:
        findings.append(
            Finding(
                id="T4-005",
                title="Indexability not explicitly specified",
                severity="low",
                evidence="Neither noindex nor index directives present.",
                suggested_action=SuggestedAction(
                    summary="Do not assume the page is indexable; consider adding explicit directives.",
                    priority=3,
                ),
                priority=3,
                confidence="medium",
                classification="observation",
            )
        )
    return findings

def _robots_findings(obs: PageObservation) -> List[Finding]:
    findings: List[Finding] = []
    # The 'none' token means "no directives present", not a directive itself.
    # Exclude it so T4-010/T4-011 never report an empty/none directive list.
    meta_directives = [d for d in obs.robots_meta_directives if d != "none"]
    if meta_directives:
        findings.append(
            Finding(
                id="T4-010",
                title="Robots meta directives present",
                severity="low",
                evidence=f"Meta directives: {', '.join(meta_directives)}",
                suggested_action=SuggestedAction(
                    summary="Review robots meta directives for intended crawl behaviour.",
                    priority=3,
                ),
                priority=3,
                confidence="high",
                classification="observation",
            )
        )
    header_directives = [d for d in obs.x_robots_tag_directives if d != "none"]
    if header_directives:
        findings.append(
            Finding(
                id="T4-011",
                title="X‑Robots‑Tag directives present",
                severity="low",
                evidence=f"Header directives: {', '.join(header_directives)}",
                suggested_action=SuggestedAction(
                    summary="Review X‑Robots‑Tag header for intended crawl behaviour.",
                    priority=3,
                ),
                priority=3,
                confidence="high",
                classification="observation",
            )
        )
    return findings

def _canonical_findings(obs: PageObservation) -> List[Finding]:
    findings: List[Finding] = []
    if not obs.canonical_url:
        findings.append(
            Finding(
                id="T4-020",
                title="Canonical tag missing",
                severity="low",
                evidence="No <link rel='canonical'> element found.",
                suggested_action=SuggestedAction(
                    summary="Consider adding a canonical tag to declare the preferred URL.",
                    priority=3,
                ),
                priority=3,
                confidence="medium",
            )
        )
        return findings

    canonical = obs.canonical_url
    final = obs.final_url

    # Semantic comparison: strip tracking params, fragments, collapse
    # trailing slash, normalize scheme/host.
    canonical_semantic = _semantic_url(canonical)
    final_semantic = _semantic_url(final)

    if canonical_semantic == final_semantic:
        findings.append(
            Finding(
                id="T4-021",
                title="Canonical points to itself",
                severity="low",
                evidence=f"Canonical URL matches final URL (ignoring fragment): {canonical}",
                suggested_action=SuggestedAction(
                    summary="No action required; canonical correctly references the page itself.",
                    priority=3,
                ),
                priority=3,
                confidence="high",
                classification="observation",
            )
        )
    else:
        try:
            can_host = urlparse(canonical).hostname or ""
            fin_host = urlparse(final).hostname or ""
        except Exception:
            can_host = fin_host = ""
        if not can_host:
            findings.append(
                Finding(
                    id="T4-022",
                    title="Canonical URL unresolvable",
                    severity="high",
                    evidence=f"Canonical URL '{canonical}' could not be parsed.",
                    suggested_action=SuggestedAction(
                        summary="Fix malformed canonical URL.",
                        priority=1,
                    ),
                    priority=1,
                    confidence="high",
                )
            )
        elif registrable_domain(can_host) != registrable_domain(fin_host):
            findings.append(
                Finding(
                    id="T4-023",
                    title="Canonical points to external domain",
                    severity="medium",
                    evidence=f"Canonical '{canonical}' differs host from final URL '{final}'.",
                    suggested_action=SuggestedAction(
                        summary="Verify that the external canonical is intentional.",
                        priority=2,
                    ),
                    priority=2,
                    confidence="high",
                )
            )
        else:
            findings.append(
                Finding(
                    id="T4-024",
                    title="Canonical points to internal alternate URL",
                    severity="low",
                    evidence=f"Canonical '{canonical}' differs from final URL '{final}' but shares host.",
                    suggested_action=SuggestedAction(
                        summary="Confirm that the alternate canonical is correct.",
                        priority=3,
                    ),
                    priority=3,
                    confidence="high",
                    classification="observation",
                )
            )
    return findings

def _structured_data_findings(obs: PageObservation) -> List[Finding]:
    findings: List[Finding] = []
    json_ld_list: List[JsonLdObservation] = obs.json_ld
    if not json_ld_list:
        findings.append(
            Finding(
                id="T4-030",
                title="No JSON‑LD structured data",
                severity="low",
                evidence="No <script type='application/ld+json'> blocks found.",
                suggested_action=SuggestedAction(
                    summary="If applicable, add JSON‑LD markup to expose structured data.",
                    priority=3,
                ),
                priority=3,
                confidence="medium",
                classification="observation",
            )
        )
        return findings

    block_count = len(json_ld_list)
    findings.append(
        Finding(
            id="T4-031",
            title="JSON‑LD blocks detected",
            severity="low",
            evidence=f"{block_count} JSON‑LD block(s) found.",
            suggested_action=SuggestedAction(
                summary="Review the JSON‑LD content for correctness.",
                priority=3,
            ),
            priority=3,
            confidence="high",
            classification="observation",
        )
    )
    # Analyze JSON‑LD blocks for @type and @context (detect @context anywhere)
    types: set[str] = set()
    missing_type = False
    malformed = False
    context_found = False
    for blk in json_ld_list:
        if not getattr(blk, "is_valid", True):
            malformed = True
            continue
        data = getattr(blk, "parsed_data", None)
        if data is None:
            malformed = True
            continue
        def _process_dict(d: dict):
            nonlocal missing_type, types, context_found
            if "@type" in d:
                typ = d["@type"]
                if isinstance(typ, list):
                    types.update(map(str, typ))
                else:
                    types.add(str(typ))
            else:
                missing_type = True
            if "@context" in d and "@type" in d:
                context_found = True
        if isinstance(data, dict):
            _process_dict(data)
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    if isinstance(item, dict):
                        _process_dict(item)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    _process_dict(item)
                else:
                    missing_type = True
        else:
            missing_type = True
    missing_context = not context_found
    missing_type = not bool(types)
    if types:
        findings.append(
            Finding(
                id="T4-032",
                title="Detected @type values",
                severity="low",
                evidence=f"@type values: {', '.join(sorted(types))}",
                suggested_action=SuggestedAction(
                    summary="Ensure detected types are appropriate for the page.",
                    priority=3,
                ),
                priority=3,
                confidence="high",
                classification="observation",
            )
        )
    if missing_context:
        findings.append(
            Finding(
                id="T4-033",
                title="JSON‑LD missing @context",
                severity="medium",
                evidence="One or more JSON‑LD blocks lack @context.",
                suggested_action=SuggestedAction(
                    summary="Add @context to each JSON‑LD block.",
                    priority=2,
                ),
                priority=2,
                confidence="high",
            )
        )
    if missing_type:
        findings.append(
            Finding(
                id="T4-034",
                title="JSON‑LD missing @type",
                severity="medium",
                evidence="One or more JSON‑LD blocks lack @type.",
                suggested_action=SuggestedAction(
                    summary="Add @type to each JSON‑LD block.",
                    priority=2,
                ),
                priority=2,
                confidence="high",
            )
        )
    if malformed:
        findings.append(
            Finding(
                id="T4-035",
                title="Malformed JSON‑LD block",
                severity="high",
                evidence="At least one JSON‑LD block failed to parse.",
                suggested_action=SuggestedAction(
                    summary="Fix the JSON syntax of the malformed block.",
                    priority=1,
                ),
                priority=1,
                confidence="high",
            )
        )
    return findings

def audit_t4_indexability(obs: PageObservation) -> List[Finding]:
    """Deterministic audit that derives indexability, robots, canonical and
    structured‑data signals from a :class:`PageObservation`."""
    findings: List[Finding] = []
    findings.extend(_indexability_findings(obs))
    if obs.is_success:
        findings.extend(_robots_findings(obs))
        findings.extend(_canonical_findings(obs))
        findings.extend(_structured_data_findings(obs))
    return findings

# Register the source audit name on each finding (handled by orchestrator tagging).
