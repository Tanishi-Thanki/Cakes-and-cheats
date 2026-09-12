"""Unit tests for single-page crawler layer."""


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest
from unittest.mock import MagicMock, patch
import requests

from src.crawler.crawler import FetchedPage, fetch_page


class TestCrawler(unittest.TestCase):
    """Test suite for fetch_page function and FetchedPage dataclass."""

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_success(self, mock_get: MagicMock) -> None:
        """Test successful page retrieval with standard HTML."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/"
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.text = "<!DOCTYPE html><html><body><h1>Hello World</h1></body></html>"
        mock_get.return_value = mock_response

        url = "https://example.com/"
        result: FetchedPage = fetch_page(url)

        self.assertTrue(result.is_success)
        self.assertEqual(result.requested_url, url)
        self.assertEqual(result.final_url, "https://example.com/")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "text/html; charset=utf-8")
        self.assertEqual(result.html_content, "<!DOCTYPE html><html><body><h1>Hello World</h1></body></html>")
        self.assertIsNone(result.error_message)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_follows_redirects(self, mock_get: MagicMock) -> None:
        """Test that redirect destination is captured in final_url."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/destination"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.text = "<html><body>Redirected</body></html>"
        mock_get.return_value = mock_response

        initial_url = "http://example.com"
        result: FetchedPage = fetch_page(initial_url, check_robots=False)

        self.assertTrue(result.is_success)
        self.assertEqual(result.requested_url, initial_url)
        self.assertEqual(result.final_url, "https://example.com/destination")
        self.assertEqual(result.status_code, 200)
        mock_get.assert_called_once()
        _, kwargs = mock_get.call_args
        self.assertTrue(kwargs.get("allow_redirects"))

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_custom_user_agent_and_timeout(self, mock_get: MagicMock) -> None:
        """Test that custom timeout and User-Agent are passed to requests.get."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.text = "<html><body>Custom Agent</body></html>"
        mock_get.return_value = mock_response

        custom_ua = "CustomBot/2.0"
        fetch_page("https://example.com/", timeout=5.5, user_agent=custom_ua)

        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs.get("timeout"), 5.5)
        headers = kwargs.get("headers", {})
        self.assertEqual(headers.get("User-Agent"), custom_ua)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_http_404_error(self, mock_get: MagicMock) -> None:
        """Test clean handling of HTTP 404 Not Found status."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.reason = "Not Found"
        mock_response.url = "https://example.com/missing"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_get.return_value = mock_response

        result: FetchedPage = fetch_page("https://example.com/missing")

        self.assertFalse(result.is_success)
        self.assertEqual(result.status_code, 404)
        self.assertEqual(result.final_url, "https://example.com/missing")
        self.assertEqual(result.html_content, "")
        self.assertIn("404", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_http_500_error(self, mock_get: MagicMock) -> None:
        """Test clean handling of HTTP 500 Server Error."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason = "Internal Server Error"
        mock_response.url = "https://example.com/broken"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_get.return_value = mock_response

        result: FetchedPage = fetch_page("https://example.com/broken")

        self.assertFalse(result.is_success)
        self.assertEqual(result.status_code, 500)
        self.assertIn("500", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_rejects_non_html(self, mock_get: MagicMock) -> None:
        """Test rejection of non-HTML content types such as application/json."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/api/data"
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.text = '{"key": "value"}'
        mock_get.return_value = mock_response

        result: FetchedPage = fetch_page("https://example.com/api/data")

        self.assertFalse(result.is_success)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content_type, "application/json")
        self.assertEqual(result.html_content, "")
        self.assertIn("Rejected non-HTML", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_accepts_xhtml(self, mock_get: MagicMock) -> None:
        """Test acceptance of application/xhtml+xml."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/xhtml"
        mock_response.headers = {"Content-Type": "application/xhtml+xml; charset=utf-8"}
        mock_response.text = "<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'><body/></html>"
        mock_get.return_value = mock_response

        result: FetchedPage = fetch_page("https://example.com/xhtml")

        self.assertTrue(result.is_success)
        self.assertIn("xmlns", result.html_content)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_network_timeout(self, mock_get: MagicMock) -> None:
        """Test graceful error handling when requests raises a Timeout exception."""
        mock_get.side_effect = requests.exceptions.Timeout("Connection timed out after 10s")

        result: FetchedPage = fetch_page("https://example.com/slow")

        self.assertFalse(result.is_success)
        self.assertIsNone(result.status_code)
        self.assertEqual(result.html_content, "")
        self.assertIn("timed out", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_connection_error(self, mock_get: MagicMock) -> None:
        """Test graceful handling of network connection failure."""
        mock_get.side_effect = requests.exceptions.ConnectionError("Failed to resolve host")

        result: FetchedPage = fetch_page("https://example.com/nonexistent")

        self.assertFalse(result.is_success)
        self.assertIsNone(result.status_code)
        self.assertIn("Connection error", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_ssl_error(self, mock_get: MagicMock) -> None:
        """Test graceful handling of SSL certificate error."""
        mock_get.side_effect = requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

        result: FetchedPage = fetch_page("https://example.com/badssl")

        self.assertFalse(result.is_success)
        self.assertIsNone(result.status_code)
        self.assertIn("SSL certificate", result.error_message or "")

    def test_fetch_page_invalid_url_schemes(self) -> None:
        """Test rejection of non-HTTP/HTTPS URLs without triggering network requests."""
        invalid_urls = [
            "",
            "   ",
            "ftp://files.example.com",
            "file:///C:/test.html",
            "javascript:alert(1)",
            "not-a-url",
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                result: FetchedPage = fetch_page(url)
                self.assertFalse(result.is_success)
                self.assertIsNone(result.status_code)
                self.assertEqual(result.html_content, "")
                self.assertIn("Invalid URL", result.error_message or "")

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_robots_txt_allowed(self, mock_get: MagicMock) -> None:
        """Test that page fetch succeeds when robots.txt permits the user agent."""
        robots_resp = MagicMock()
        robots_resp.status_code = 200
        robots_resp.text = "User-agent: *\nAllow: /\n"
        robots_resp.url = "https://example.com/robots.txt"

        page_resp = MagicMock()
        page_resp.status_code = 200
        page_resp.text = "<html><body><h1>Allowed Page</h1></body></html>"
        page_resp.headers = {"Content-Type": "text/html"}
        page_resp.url = "https://example.com/allowed"

        mock_get.side_effect = [robots_resp, page_resp]

        result = fetch_page("https://example.com/allowed", check_robots=True)
        self.assertTrue(result.is_success)
        self.assertEqual(result.html_content, "<html><body><h1>Allowed Page</h1></body></html>")
        self.assertEqual(mock_get.call_count, 2)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_robots_txt_disallowed(self, mock_get: MagicMock) -> None:
        """Test that fetch is blocked and returns structured error when disallowed by robots.txt."""
        robots_resp = MagicMock()
        robots_resp.status_code = 200
        robots_resp.text = "User-agent: *\nDisallow: /private/\n"
        robots_resp.url = "https://example.com/robots.txt"

        mock_get.return_value = robots_resp

        result = fetch_page("https://example.com/private/page", check_robots=True)
        self.assertFalse(result.is_success)
        self.assertIsNone(result.status_code)
        self.assertIn("robots.txt", result.error_message or "")
        # Only robots.txt should have been fetched; target page was NOT fetched
        self.assertEqual(mock_get.call_count, 1)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_robots_txt_unavailable_fails_open(self, mock_get: MagicMock) -> None:
        """Test that crawler fails open when robots.txt returns 404 or encounters an error."""
        robots_resp = MagicMock()
        robots_resp.status_code = 404
        robots_resp.text = "Not Found"
        robots_resp.url = "https://example.com/robots.txt"

        page_resp = MagicMock()
        page_resp.status_code = 200
        page_resp.text = "<html><body><h1>Content</h1></body></html>"
        page_resp.headers = {"Content-Type": "text/html"}
        page_resp.url = "https://example.com/page"

        mock_get.side_effect = [robots_resp, page_resp]

        result = fetch_page("https://example.com/page", check_robots=True)
        self.assertTrue(result.is_success)
        self.assertEqual(result.status_code, 200)

    @patch("src.crawler.crawler.requests.get")
    def test_fetch_page_robots_txt_user_agent_specific_rule(self, mock_get: MagicMock) -> None:
        """Test that user-agent specific directives in robots.txt are respected."""
        robots_content = (
            "User-agent: OtherBot\n"
            "Disallow: /\n\n"
            "User-agent: CustomAuditor\n"
            "Disallow: /restricted/\n"
            "Allow: /public/\n"
        )
        robots_resp = MagicMock()
        robots_resp.status_code = 200
        robots_resp.text = robots_content
        robots_resp.url = "https://example.com/robots.txt"

        mock_get.return_value = robots_resp

        # Should be disallowed for CustomAuditor on /restricted/
        result_blocked = fetch_page(
            "https://example.com/restricted/info",
            user_agent="CustomAuditor/1.0",
            check_robots=True,
        )
        self.assertFalse(result_blocked.is_success)
        self.assertIn("robots.txt", result_blocked.error_message or "")

        # Verify User-Agent header passed to robots.txt request
        first_call_args = mock_get.call_args_list[0]
        self.assertEqual(
            first_call_args[1].get("headers", {}).get("User-Agent"),
            "CustomAuditor/1.0",
        )


if __name__ == "__main__":
    unittest.main()
