"""Unit tests for single-page crawler layer."""

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
        result: FetchedPage = fetch_page(initial_url)

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


if __name__ == "__main__":
    unittest.main()
