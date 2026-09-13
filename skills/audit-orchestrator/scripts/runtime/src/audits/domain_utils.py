"""Registrable-domain helpers shared across audits.

Computes a deterministic eTLD+1 approximation ("registrable domain") used for
comparing host identity without hardcoding any specific client site. A public
suffix table (single-label TLDs plus common multi-label second-level suffixes)
distinguishes ``example.co.uk`` (registrable domain ``example.co.uk``) from
``example.com`` (registrable domain ``example.com``).

This is a generic, offline, config-only mapping — never a per-client whitelist.
"""

from __future__ import annotations

# Common multi-label public suffixes (second-level labels that still form the
# public suffix, e.g. ``co.uk`` in ``example.co.uk``).
_MULTI_LABEL_PUBLIC_SUFFIXES: frozenset[str] = frozenset({
    "ac.uk", "co.uk", "gov.uk", "org.uk", "net.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au", "asn.au",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "gr.jp", "ad.jp", "ed.jp",
    "com.br", "net.br", "org.br", "gov.br", "edu.br", "mil.br",
    "co.in", "net.in", "org.in", "gov.in", "ac.in", "edu.in",
    "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
    "co.nz", "org.nz", "net.nz", "ac.nz", "govt.nz", "geek.nz",
    "co.za", "net.za", "org.za", "gov.za", "ac.za",
    "co.kr", "or.kr", "ne.kr", "go.kr", "ac.kr", "re.kr",
    "com.mx", "org.mx", "net.mx", "gob.mx", "edu.mx",
    "com.hk", "org.hk", "net.hk", "edu.hk", "gov.hk",
    "com.sg", "org.sg", "net.sg", "edu.sg", "gov.sg",
    "com.my", "org.my", "net.my", "edu.my", "gov.my",
    "co.il", "org.il", "net.il", "gov.il", "ac.il",
})


def registrable_domain(hostname: str) -> str:
    """Return the registrable (eTLD+1-like) domain for a hostname.

    ``sub.example.com`` -> ``example.com``
    ``help.zooniverse.org`` -> ``zooniverse.org``
    ``developer.semrush.com`` -> ``semrush.com``
    ``www.nps.gov`` -> ``nps.gov``
    ``example.co.uk`` -> ``example.co.uk``

    Hosts that are not hierarchical domains (IP addresses, bare hosts such as
    ``localhost``) are returned unchanged.

    Args:
        hostname: A hostname, optionally with a trailing dot.

    Returns:
        The registrable domain, lowercased.
    """
    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # IP literals are not hierarchical domains.
    if all(p.isdigit() for p in parts):
        return host
    # Multi-label public suffix (e.g. co.uk) -> take last three labels.
    if ".".join(parts[-2:]) in _MULTI_LABEL_PUBLIC_SUFFIXES:
        return ".".join(parts[-3:])
    # Standard single-label TLD -> take last two labels (eTLD+1).
    return ".".join(parts[-2:])