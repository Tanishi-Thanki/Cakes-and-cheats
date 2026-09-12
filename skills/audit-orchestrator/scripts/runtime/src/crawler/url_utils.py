"""Deterministic URL utilities for the crawler foundation (P1-T1).

Safe canonicalization only: every rewrite must be provably identity-preserving.
This module never aggressively rewrites URLs (no stripping of meaningful query
parameters, no forced trailing-slash collapse, no re-quoting of paths) so that
distinct resources are never merged and an external URL is never accidentally
converted into an internal one.

The single-page fetcher (``fetch_page``) and the bounded crawl session
(``crawl_site``) are the only consumers. Nothing here touches the network.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

# Schemes the crawler will follow when discovering internal pages.
CRAWLABLE_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Default ports that are identity-equivalent to an explicit port.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def is_crawlable_scheme(url: str) -> bool:
    """Return True when url uses an http(s) scheme the crawler may follow."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        scheme = urllib.parse.urlsplit(url.strip()).scheme.lower()
    except Exception:
        return False
    return scheme in CRAWLABLE_SCHEMES


def is_rejected_scheme(url: str) -> bool:
    """Return True when url carries a non-http scheme (javascript:, mailto:,
    tel:, data:, file:, ftp:, ...) that must never be fetched.

    Relative URLs (no scheme) are not rejected here; they must be resolved
    against the source page first.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        scheme = urllib.parse.urlsplit(url.strip()).scheme.lower()
    except Exception:
        return False
    return bool(scheme) and scheme not in CRAWLABLE_SCHEMES


def _idna_host(host: str) -> str:
    """Lowercase and IDNA-encode a hostname deterministically."""
    lowered = host.lower()
    try:
        return lowered.encode("idna").decode("ascii")
    except Exception:
        return lowered


def _build_netloc(parts: urllib.parse.SplitResult) -> str:
    """Reconstruct netloc with lowercased/idna host, default port dropped,
    and any userinfo preserved verbatim."""
    host = _idna_host(parts.hostname or "")
    if not host:
        return ""
    port = parts.port
    default_port = _DEFAULT_PORTS.get(parts.scheme.lower())
    host_port = host if port is None or port == default_port else f"{host}:{port}"
    userinfo = ""
    if parts.username is not None or parts.password is not None:
        userinfo = f"{parts.username or ''}:{parts.password or ''}@"
    return f"{userinfo}{host_port}"


def normalize_url(url: str) -> str:
    """Return a deterministic, safe canonical form of an absolute http(s) URL.

    Applied rules (each identity-preserving):
    - scheme and host are lowercased; internationalized hosts are IDNA-encoded;
    - the fragment is dropped (``#...`` never identifies a distinct resource
      for crawling purposes);
    - an explicit default port for the scheme is dropped (``https://x:443/``
      and ``https://x/`` are the same resource);
    - an empty path becomes ``/``;
    - the path and query string are preserved verbatim — meaningful query
      parameters are never removed.

    Non-http(s) URLs and relative/malformed inputs are returned unchanged so
    callers can reject them explicitly via :func:`is_rejected_scheme` /
    :func:`is_crawlable_scheme`. Resolve relative URLs first with
    :func:`resolve_url` / :func:`resolve_and_normalize`.
    """
    if not isinstance(url, str) or not url.strip():
        return url
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except Exception:
        return url
    if parts.scheme.lower() not in CRAWLABLE_SCHEMES:
        return url
    netloc = _build_netloc(parts)
    if not netloc:
        return url
    path = parts.path or "/"
    fragment = ""
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, path, parts.query, fragment))


def extract_site_key(url: str) -> Optional[str]:
    """Return a stable key identifying the site scope of url.

    The key is ``host[:port]`` with default ports dropped and the host
    lowercased/IDNA-encoded, independent of scheme — so http and https of the
    same host are treated as the same site. Subdomains and non-default ports
    produce different keys (they are external scopes by default).

    Returns None for relative, malformed, or non-http(s) URLs.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except Exception:
        return None
    if parts.scheme.lower() not in CRAWLABLE_SCHEMES or not parts.hostname:
        return None
    host = _idna_host(parts.hostname or "")
    if not host:
        return None
    port = parts.port
    default_port = _DEFAULT_PORTS.get(parts.scheme.lower())
    if port is None or port == default_port:
        return host
    return f"{host}:{port}"


def is_same_site(url_a: str, url_b: str) -> bool:
    """Return True when both urls belong to the same site scope.

    Comparison ignores scheme (http/https) but not host or effective port.
    Relative or non-http urls never match.
    """
    key_a = extract_site_key(url_a)
    key_b = extract_site_key(url_b)
    return key_a is not None and key_a == key_b


def resolve_url(base_url: str, url: str) -> str:
    """Resolve url against base_url using urllib.parse.urljoin semantics.

    Handles absolute URLs, root-relative (``/about``), protocol-relative
    (``//cdn.example/x``), relative paths, and fragment/query-only strings.
    Non-http schemes (``javascript:`` etc.) are preserved unchanged and must be
    filtered by :func:`is_rejected_scheme`.
    """
    if not isinstance(url, str) or not url:
        return url
    if not isinstance(base_url, str) or not base_url:
        return url
    try:
        return urllib.parse.urljoin(base_url, url.strip())
    except Exception:
        return url


def resolve_and_normalize(base_url: str, url: str) -> str:
    """Resolve url against base_url and return its normalized canonical form.

    Returns ``url`` unchanged when it cannot be resolved/normalized, so callers
    can run :func:`is_crawlable_scheme` / :func:`is_same_site` on the result.
    """
    resolved = resolve_url(base_url, url)
    return normalize_url(resolved)