"""Crawler module for single-page fetching."""

from src.crawler.crawler import FetchedPage, check_robots_txt, fetch_page

__all__ = ["FetchedPage", "check_robots_txt", "fetch_page"]
