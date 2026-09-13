"""Website fetching layer for single-page audit pipeline.

Fetches the raw HTML content of a single target web page using read-only HTTP GET.
Also provides ``fetch_text`` for text/XML site-metadata resources (robots.txt,
sitemap files) through the same single HTTP layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
import urllib.parse
import urllib.robotparser
import requests

DEFAULT_TIMEOUT_SECONDS: float = 10.0
DEFAULT_USER_AGENT: str = (
    "CakesAndCheats-Auditor/1.0 (+https://github.com/Tanishi-Thanki/Cakes-and-cheats; Adobe Hackathon 2026)"
)

# Conservative per-response body cap (bytes). A non-HTML/oversized body is
# rejected before decode so single fetches stay memory-bounded even when a
# server streams unbounded content. Pass max_response_bytes=None to disable.
DEFAULT_MAX_RESPONSE_BYTES: int = 5_000_000

# Accepted HTML MIME types
HTML_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
)

# Accepted text/XML MIME types for metadata fetches (robots.txt, sitemaps).
TEXT_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "text/plain",
    "text/xml",
    "application/xml",
    "application/rss+xml",
    "application/atom+xml",
)

# Stable machine-readable failure categories for FetchedPage.failure_category.
FAILURE_CATEGORY_INVALID_URL = "invalid_url"
FAILURE_CATEGORY_ROBOTS_DENIED = "robots_denied"
FAILURE_CATEGORY_TIMEOUT = "timeout"
FAILURE_CATEGORY_SSL_ERROR = "ssl_error"
FAILURE_CATEGORY_CONNECTION_ERROR = "connection_error"
FAILURE_CATEGORY_DNS_ERROR = "dns_error"
FAILURE_CATEGORY_TOO_MANY_REDIRECTS = "too_many_redirects"
FAILURE_CATEGORY_HTTP_ERROR = "http_error"
FAILURE_CATEGORY_NON_HTML = "non_html"
FAILURE_CATEGORY_NON_TEXT = "non_text"
FAILURE_CATEGORY_RESPONSE_TOO_LARGE = "response_too_large"
FAILURE_CATEGORY_DECODE_ERROR = "decode_error"
FAILURE_CATEGORY_UNEXPECTED_ERROR = "unexpected_error"

_DNS_ERROR_HINTS: tuple[str, ...] = (
    "failed to resolve",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "name resolution",
)


def _classify_connection_error(message: str) -> str:
    """Distinguish DNS resolution failures from other connection errors."""
    normalized = str(message).lower()
    if any(hint in normalized for hint in _DNS_ERROR_HINTS):
        return FAILURE_CATEGORY_DNS_ERROR
    return FAILURE_CATEGORY_CONNECTION_ERROR


@dataclass(frozen=True)
class FetchedPage:
    """Structured result of fetching a single webpage.

    Attributes:
        requested_url: The original URL provided for fetching.
        final_url: The final URL after following HTTP redirects (empty if failed before response).
        status_code: HTTP status code returned by server (None if request failed).
        content_type: Value of the Content-Type header from response.
        html_content: Raw decoded HTML string (empty if fetch failed or content was not HTML).
        is_success: True if HTTP GET succeeded, status code is 2xx, and content is HTML.
        error_message: Explanatory error details if is_success is False.
        response_headers: Raw response headers (string values) when a response was
            received; empty dict otherwise. Used by the observation layer for
            X-Robots-Tag and other metadata; never required for fetch success.
        failure_category: Stable, machine-readable failure category when
            is_success is False (e.g. "dns_error", "timeout", "robots_denied",
            "http_error", "non_html", "decode_error"), otherwise "".
    """

    requested_url: str
    final_url: str
    status_code: Optional[int]
    content_type: str
    html_content: str
    is_success: bool
    error_message: Optional[str] = None
    response_headers: dict[str, str] = field(default_factory=dict)
    failure_category: str = ""


def _extract_response_headers(response) -> dict[str, str]:
    """Extract a plain string-value dict of response headers, never raising.

    Tolerates mocked/minimal response objects (which may expose no usable
    ``headers`` mapping) used by offline tests.
    """
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    try:
        items = headers.items() if hasattr(headers, "items") else None
        if items is None:
            return {}
        return {str(key): str(value) for key, value in items if key is not None}
    except Exception:
        return {}


def _is_html_content_type(content_type_header: str) -> bool:
    """Check whether the Content-Type header corresponds to an HTML response."""
    if not content_type_header:
        return False
    media_type = content_type_header.split(";")[0].strip().lower()
    return media_type in HTML_CONTENT_TYPES


def _is_text_content_type(content_type_header: str) -> bool:
    """Check whether the Content-Type header corresponds to a text or XML response."""
    if not content_type_header:
        return False
    media_type = content_type_header.split(";")[0].strip().lower()
    return media_type in TEXT_CONTENT_TYPES or media_type.endswith("+xml")


def _response_over_limit(response, max_bytes: int) -> bool:
    """Return True when response declares or materializes a body over max_bytes.

    Never raises, tolerating minimal/fake response objects (no usable headers,
    content, or text). A declared ``Content-Length`` over the cap is rejected
    before the body is read; a materialized bytes body is measured exactly;
    otherwise decoded text length (UTF-8) is used as a conservative proxy.
    """
    if max_bytes is None or max_bytes <= 0:
        return False
    headers = getattr(response, "headers", None)
    if headers is not None and hasattr(headers, "get"):
        try:
            declared = int(str(headers.get("Content-Length") or "").strip())
        except (TypeError, ValueError):
            declared = 0
        if declared > max_bytes:
            return True
    try:
        content = getattr(response, "content", None)
    except Exception:
        content = None
    if isinstance(content, bytes):
        return len(content) > max_bytes
    try:
        text = getattr(response, "text", None)
    except Exception:
        text = None
    if isinstance(text, str):
        return len(text.encode("utf-8", errors="replace")) > max_bytes
    return False


def check_robots_txt(
    url: str,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    """Check whether url can be fetched according to the site's robots.txt.

    Uses urllib.robotparser.RobotFileParser and issues a single read-only HTTP GET
    to '{scheme}://{netloc}/robots.txt'.

    Returns:
        (allowed, reason):
        - If allowed, or if robots.txt is missing/unavailable (4xx, network error, timeout),
          returns (True, None) to fail open conservatively per web crawler standards.
        - If explicitly disallowed for the user_agent, returns (False, explanation).
    """
    if not isinstance(url, str) or not url.strip():
        return True, None

    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return True, None
        if parsed.path == "/robots.txt":
            return True, None
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    except Exception:
        return True, None

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/plain,text/*;q=0.9,*/*;q=0.8",
    }

    try:
        resp = requests.get(
            robots_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
    except Exception:
        # Network errors, timeouts, SSL errors: robots.txt is unreachable -> fail open
        return True, None

    # If 4xx (e.g. 404), robots.txt is absent -> allowed
    # If 5xx, treat as unavailable -> fail open
    # An oversized robots.txt body is treated as unavailable -> fail open
    if resp.status_code != 200:
        return True, None
    if _response_over_limit(resp, DEFAULT_MAX_RESPONSE_BYTES):
        return True, None
    if not resp.text:
        return True, None

    try:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(resp.text.splitlines())
        if not rp.can_fetch(user_agent, url):
            return False, f"Access disallowed by robots.txt at '{robots_url}' for User-Agent '{user_agent}'."
    except Exception:
        return True, None

    return True, None


def fetch_page(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    check_robots: bool = True,
    max_response_bytes: Optional[int] = DEFAULT_MAX_RESPONSE_BYTES,
) -> FetchedPage:
    """Fetch a single web page via HTTP GET and return structured page data.

    Args:
        url: The URL of the page to fetch. Must be an http or https URL.
        timeout: Timeout in seconds for connection and read. Defaults to 10.0.
        user_agent: Descriptive User-Agent string sent in request headers.
        check_robots: Whether to check robots.txt before fetching. Defaults to True.
        max_response_bytes: Maximum accepted body size in bytes (None disables).
            Oversized responses are rejected deterministically without being
            retained or decoded.

    Returns:
        FetchedPage containing requested URL, final URL, status code,
        content type, raw HTML, and success/error status.
    """
    # 1. Validate URL input
    if not isinstance(url, str) or not url.strip():
        return FetchedPage(
            requested_url=url if isinstance(url, str) else str(url),
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message="Invalid URL: URL must be a non-empty string.",
            failure_category=FAILURE_CATEGORY_INVALID_URL,
        )

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlparse(clean_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return FetchedPage(
                requested_url=clean_url,
                final_url="",
                status_code=None,
                content_type="",
                html_content="",
                is_success=False,
                error_message=f"Invalid URL: '{clean_url}' must start with http:// or https:// and have a valid host.",
                failure_category=FAILURE_CATEGORY_INVALID_URL,
            )
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Invalid URL syntax: {err}",
            failure_category=FAILURE_CATEGORY_INVALID_URL,
        )

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 2. Check robots.txt compliance if enabled
    if check_robots:
        allowed, reason = check_robots_txt(clean_url, user_agent=user_agent, timeout=timeout)
        if not allowed:
            return FetchedPage(
                requested_url=clean_url,
                final_url="",
                status_code=None,
                content_type="",
                html_content="",
                is_success=False,
                error_message=reason or f"Access disallowed by robots.txt for User-Agent '{user_agent}'.",
                failure_category=FAILURE_CATEGORY_ROBOTS_DENIED,
            )

    # 3. Execute read-only HTTP GET
    try:
        response = requests.get(
            clean_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
is_success=False,
                error_message=f"Request timed out after {timeout}s: {err}",
                failure_category=FAILURE_CATEGORY_TIMEOUT,
            )
    except requests.exceptions.SSLError as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"SSL certificate validation failed: {err}",
            failure_category=FAILURE_CATEGORY_SSL_ERROR,
            )
    except requests.exceptions.ConnectionError as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Connection error: {err}",
            failure_category=_classify_connection_error(str(err)),
        )
    except requests.exceptions.TooManyRedirects as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Too many redirects while following URL chain: {err}",
            failure_category=FAILURE_CATEGORY_TOO_MANY_REDIRECTS,
        )
    except requests.exceptions.RequestException as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"HTTP request failed: {err}",
            failure_category=FAILURE_CATEGORY_HTTP_ERROR,
        )
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Unexpected error while fetching URL: {err}",
            failure_category=FAILURE_CATEGORY_UNEXPECTED_ERROR,
        )

    # 3. Process response attributes
    final_url = response.url or clean_url
    status_code = response.status_code
    content_type = response.headers.get("Content-Type", "")
    response_headers = _extract_response_headers(response)

    # Check for HTTP error status codes (not in 2xx range)
    if not (200 <= status_code < 300):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"HTTP error status {status_code} ({response.reason or 'Unsuccessful'}).",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_HTTP_ERROR,
        )

    # Check for HTML content type
    if not _is_html_content_type(content_type):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"Rejected non-HTML response (Content-Type: '{content_type}').",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_NON_HTML,
        )

    # Check for oversized responses before any decoding work
    if max_response_bytes is not None and _response_over_limit(response, max_response_bytes):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=(
                f"Response body exceeds the {max_response_bytes}-byte limit "
                f"(Content-Type: '{content_type}')."
            ),
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_RESPONSE_TOO_LARGE,
        )

    # 4. Safely extract decoded text
    try:
        html_text = response.text
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"Failed to decode response content: {err}",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_DECODE_ERROR,
        )

    return FetchedPage(
        requested_url=clean_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
        html_content=html_text,
        is_success=True,
        error_message=None,
        response_headers=response_headers,
    )


def fetch_text(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    check_robots: bool = False,
    max_response_bytes: Optional[int] = DEFAULT_MAX_RESPONSE_BYTES,
) -> FetchedPage:
    """Fetch a text/XML resource (robots.txt, sitemap.xml) via read-only HTTP GET.

    Sibling of :func:`fetch_page` used for site metadata discovery. It accepts
    text/plain and XML content types (where ``fetch_page`` intentionally rejects
    non-HTML) so robots.txt and sitemap files can be read through the same single
    HTTP layer.

    ``check_robots`` is accepted for signature compatibility with the crawler's
    ``FetcherCallable`` seam but is ignored: robots.txt cannot disallow itself,
    and sitemap files are site metadata fetched under the same read-only policy.

    Returns:
        FetchedPage with ``is_success`` True only for 2xx text/XML responses.
    """
    if not isinstance(url, str) or not url.strip():
        return FetchedPage(
            requested_url=url if isinstance(url, str) else str(url),
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message="Invalid URL: URL must be a non-empty string.",
            failure_category=FAILURE_CATEGORY_INVALID_URL,
        )

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlparse(clean_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return FetchedPage(
                requested_url=clean_url,
                final_url="",
                status_code=None,
                content_type="",
                html_content="",
                is_success=False,
                error_message=f"Invalid URL: '{clean_url}' must start with http:// or https:// and have a valid host.",
                failure_category=FAILURE_CATEGORY_INVALID_URL,
            )
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Invalid URL syntax: {err}",
            failure_category=FAILURE_CATEGORY_INVALID_URL,
        )

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/plain,text/xml,application/xml,application/rss+xml,application/atom+xml,*/*;q=0.1",
    }

    try:
        response = requests.get(
            clean_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.exceptions.Timeout as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
is_success=False,
                error_message=f"Request timed out after {timeout}s: {err}",
                failure_category=FAILURE_CATEGORY_TIMEOUT,
            )
    except requests.exceptions.SSLError as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"SSL certificate validation failed: {err}",
            failure_category=FAILURE_CATEGORY_SSL_ERROR,
            )
    except requests.exceptions.ConnectionError as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Connection error: {err}",
            failure_category=_classify_connection_error(str(err)),
        )
    except requests.exceptions.TooManyRedirects as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Too many redirects while following URL chain: {err}",
            failure_category=FAILURE_CATEGORY_TOO_MANY_REDIRECTS,
        )
    except requests.exceptions.RequestException as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"HTTP request failed: {err}",
            failure_category=FAILURE_CATEGORY_HTTP_ERROR,
        )
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url="",
            status_code=None,
            content_type="",
            html_content="",
            is_success=False,
            error_message=f"Unexpected error while fetching URL: {err}",
            failure_category=FAILURE_CATEGORY_UNEXPECTED_ERROR,
        )

    final_url = response.url or clean_url
    status_code = response.status_code
    content_type = response.headers.get("Content-Type", "")
    response_headers = _extract_response_headers(response)

    if not (200 <= status_code < 300):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"HTTP error status {status_code} ({response.reason or 'Unsuccessful'}).",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_HTTP_ERROR,
        )

    if not _is_text_content_type(content_type):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"Rejected non-text response (Content-Type: '{content_type}').",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_NON_TEXT,
        )

    if max_response_bytes is not None and _response_over_limit(response, max_response_bytes):
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"Response body exceeds the {max_response_bytes}-byte limit.",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_RESPONSE_TOO_LARGE,
        )

    try:
        text_content = response.text
    except Exception as err:
        return FetchedPage(
            requested_url=clean_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            html_content="",
            is_success=False,
            error_message=f"Failed to decode response content: {err}",
            response_headers=response_headers,
            failure_category=FAILURE_CATEGORY_DECODE_ERROR,
        )

    return FetchedPage(
        requested_url=clean_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
        html_content=text_content,
        is_success=True,
        error_message=None,
        response_headers=response_headers,
    )
