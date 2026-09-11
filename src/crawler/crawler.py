"""Website fetching layer for single-page audit pipeline.

Fetches the raw HTML content of a single target web page using read-only HTTP GET.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import urllib.parse
import requests

DEFAULT_TIMEOUT_SECONDS: float = 10.0
DEFAULT_USER_AGENT: str = (
    "CakesAndCheats-Auditor/1.0 (+https://github.com/Tanishi-Thanki/Cakes-and-cheats; Adobe Hackathon 2026)"
)

# Accepted HTML MIME types
HTML_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
)


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
    """

    requested_url: str
    final_url: str
    status_code: Optional[int]
    content_type: str
    html_content: str
    is_success: bool
    error_message: Optional[str] = None


def _is_html_content_type(content_type_header: str) -> bool:
    """Check whether the Content-Type header corresponds to an HTML response."""
    if not content_type_header:
        return False
    media_type = content_type_header.split(";")[0].strip().lower()
    return media_type in HTML_CONTENT_TYPES


def fetch_page(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> FetchedPage:
    """Fetch a single web page via HTTP GET and return structured page data.

    Args:
        url: The URL of the page to fetch. Must be an http or https URL.
        timeout: Timeout in seconds for connection and read. Defaults to 10.0.
        user_agent: Descriptive User-Agent string sent in request headers.

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
        )

    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # 2. Execute read-only HTTP GET
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
        )

    # 3. Process response attributes
    final_url = response.url or clean_url
    status_code = response.status_code
    content_type = response.headers.get("Content-Type", "")

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
        )

    return FetchedPage(
        requested_url=clean_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type,
        html_content=html_text,
        is_success=True,
        error_message=None,
    )
