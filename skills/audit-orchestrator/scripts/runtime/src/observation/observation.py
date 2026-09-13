"""Website observation layer.

Parses raw HTML content from a FetchedPage using BeautifulSoup to extract
deterministic facts, structural elements, visible text, and metadata.

Design Principle: Observation != Audit.
This layer records what is on the page without judging quality or correctness.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
import json
import re
from typing import Any, Optional, Union
import urllib.parse

from bs4 import BeautifulSoup

from src.crawler.crawler import FetchedPage
from src.crawler.url_utils import resolve_and_normalize


@dataclass(frozen=True)
class LinkObservation:
    """Observed hyperlink within the page.

    Attributes:
        href: Fully resolved, normalized absolute URL (or original if
            unresolvable). Normalization reuses the crawler URL utilities:
            hosts are lowercased, default ports are dropped, and fragments are
            removed so duplicate/fragment-only links collapse to one target.
        raw_href: The exact attribute value present in the href attribute.
        anchor_text: Cleaned, visible text inside the anchor element.
        is_internal: True if the link shares the same hostname as the final page URL,
            False if external or non-HTTP scheme, or None if undetermined.
    """

    href: str
    raw_href: str
    anchor_text: str
    is_internal: Optional[bool]


@dataclass(frozen=True)
class ImageObservation:
    """Observed image within the page.

    Attributes:
        src: Fully resolved absolute URL of the image source.
        alt: Cleaned alternative text from the alt attribute.
    """

    src: str
    alt: str


@dataclass(frozen=True)
class TableObservation:
    """Observed HTML table.

    Attributes:
        headers: Extracted header cell texts (from <thead> or first <th> row).
        rows: All rows in the table, where each row is a list of cell text strings.
    """

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class JsonLdObservation:
    """Observed JSON-LD script block.

    Attributes:
        raw_text: Raw content inside the <script type="application/ld+json"> tag.
        parsed_data: Parsed JSON object (dict) or array (list), or None if invalid.
        is_valid: True if JSON parsing succeeded.
        error_message: Error string if JSON parsing failed.
    """

    raw_text: str
    parsed_data: Optional[Union[dict[str, Any], list[Any]]] = None
    is_valid: bool = False
    error_message: Optional[str] = None


@dataclass(frozen=True)
class ComponentObservation:
    """Observed self-contained content component (structural, domain-agnostic).

    Covers semantic blocks (``<main>``, ``<article>``, ``<section>``,
    ``<nav>``, ``<header>``, ``<footer>``, ``<aside>``, ``<form>``, ``<ul>``,
    ``<ol>``) and generic ``<div>`` blocks whose class/id carry content
    scaffolding keywords (e.g. pricing, plan, product, card). Component
    recognition is purely structural -- it never depends on a hostname or
    any particular kind of site.

    Attributes:
        kind: Component category (e.g. "main", "article", "section",
            "navigation", "list", "header", "footer", "complementary",
            "form", "content_component").
        source_tag: HTML tag of the component element (e.g. "section", "div").
        source_identifier: Structural locator from the element's id/class
            attributes (e.g. ``id="pricing" class="card"``), empty if none.
        heading: Heading text associated with the component (a heading inside
            the component, or the nearest preceding heading in document order).
        text: Cleaned visible text of the component, bounded to a maximum
            length for determinism.
        page_url: Final (post-redirect) URL of the page the component came from.
    """

    kind: str
    source_tag: str
    source_identifier: str
    heading: str
    text: str
    page_url: str


@dataclass(frozen=True)
class PageSignals:
    """Basic quantitative and presence signals observed on the page.

    These are factual counts and presence flags, not audit pass/fail evaluations.
    """

    has_title: bool
    has_meta_description: bool
    has_canonical: bool
    num_links: int
    num_images: int
    num_tables: int
    num_headings: int
    num_components: int = 0


@dataclass(frozen=True)
class PageObservation:
    """Structured extraction of raw facts observed from a FetchedPage.

    Attributes:
        requested_url: The original requested URL.
        final_url: The final URL after following HTTP redirects.
        status_code: HTTP status code from response (or None if failed).
        is_success: Whether the page was fetched and has content to observe.
        error_message: Network/fetch error details if is_success is False.
        title: Extracted <title> text, or None if absent.
        meta_description: Content of <meta name="description">, or None if absent.
        canonical_url: Resolved href of <link rel="canonical">, or None if absent.
        h1: List of cleaned <h1> heading texts.
        h2: List of cleaned <h2> heading texts.
        h3: List of cleaned <h3> heading texts.
        visible_text: Cleaned visible human-readable text (no script/style content).
        links: List of observed hyperlinks with resolved URLs and internal flags.
        images: List of observed images with resolved sources and alt texts.
        tables: List of observed tables with structured row/cell data.
        json_ld: List of observed JSON-LD script blocks (parsed and raw).
        signals: Quantitative summary of page elements.
        content_type: Content-Type of the fetched response, if known.
        response_headers: Response headers (string values), if known.
        robots_meta_raw: Raw content of <meta name="robots">, or None.
        robots_meta_directives: Normalized robots meta directive tokens.
        x_robots_tag_raw: Raw X-Robots-Tag header value(s), or None.
        x_robots_tag_directives: Normalized X-Robots-Tag directive tokens.
        og_title: Content of og:title, or None.
        og_description: Content of og:description, or None.
        og_type: Content of og:type, or None.
        og_url: Resolved content of og:url, or None.
    """

    # 1. URL information
    requested_url: str
    final_url: str
    status_code: Optional[int]
    is_success: bool
    error_message: Optional[str] = None

    # 2. Basic metadata
    title: Optional[str] = None
    meta_description: Optional[str] = None
    canonical_url: Optional[str] = None

    # 3. Headings
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    h3: list[str] = field(default_factory=list)

    # 4. Visible text
    visible_text: str = ""

    # 5. Links, Images, Tables, JSON-LD
    links: list[LinkObservation] = field(default_factory=list)
    images: list[ImageObservation] = field(default_factory=list)
    tables: list[TableObservation] = field(default_factory=list)
    json_ld: list[JsonLdObservation] = field(default_factory=list)

    # 6. Signals
    signals: PageSignals = field(
        default_factory=lambda: PageSignals(
            has_title=False,
            has_meta_description=False,
            has_canonical=False,
            num_links=0,
            num_images=0,
            num_tables=0,
            num_headings=0,
        )
    )

    # 7. Response metadata (P1-T3 enrichment)
    content_type: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)

    # 8. Robots / indexability metadata (observable directives only — no judgment)
    robots_meta_raw: Optional[str] = None
    robots_meta_directives: tuple[str, ...] = ()
    x_robots_tag_raw: Optional[str] = None
    x_robots_tag_directives: tuple[str, ...] = ()

    # 9. Basic Open Graph metadata
    og_title: Optional[str] = None
    og_description: Optional[str] = None
    og_type: Optional[str] = None
    og_url: Optional[str] = None

    # 10. Twitter Card metadata
    twitter_title: Optional[str] = None
    twitter_description: Optional[str] = None
    twitter_card: Optional[str] = None

    # 11. Generic content components with source provenance (P1 generalization)
    components: list[ComponentObservation] = field(default_factory=list)

    # Convenience properties
    @property
    def has_title(self) -> bool:
        return self.signals.has_title

    @property
    def has_meta_description(self) -> bool:
        return self.signals.has_meta_description

    @property
    def has_canonical(self) -> bool:
        return self.signals.has_canonical

    @property
    def num_links(self) -> int:
        return self.signals.num_links

    @property
    def num_images(self) -> int:
        return self.signals.num_images

    @property
    def num_tables(self) -> int:
        return self.signals.num_tables

    @property
    def num_headings(self) -> int:
        return self.signals.num_headings

    @property
    def num_components(self) -> int:
        if self.signals is None:
            return len(self.components)
        return self.signals.num_components

    @property
    def has_robots_meta(self) -> bool:
        return bool(self.robots_meta_directives)

    @property
    def has_x_robots_tag(self) -> bool:
        return bool(self.x_robots_tag_directives)

    @property
    def has_open_graph(self) -> bool:
        return any(
            value is not None
            for value in (self.og_title, self.og_description, self.og_type, self.og_url)
        )


def _resolve_url(base_url: str, relative_url: str) -> str:
    """Resolve a relative URL against base_url safely using urllib.parse."""
    if not relative_url:
        return ""
    if not base_url:
        return relative_url
    try:
        return urllib.parse.urljoin(base_url, relative_url)
    except Exception:
        return relative_url


def _classify_is_internal(base_url: str, target_url: str) -> Optional[bool]:
    """Determine whether target_url is internal to base_url based on hostname.

    Returns:
        True if target_url has the exact same hostname as base_url.
        False if target_url has a different hostname or an external scheme (mailto/tel).
        None if determination cannot be made reliably.
    """
    if not base_url or not target_url:
        return None
    try:
        base_parsed = urllib.parse.urlparse(base_url)
        target_parsed = urllib.parse.urlparse(target_url)

        # Handle non-HTTP navigation schemes
        if target_parsed.scheme in ("mailto", "tel", "javascript"):
            return False

        base_host = (base_parsed.hostname or "").lower()
        target_host = (target_parsed.hostname or "").lower()

        if not base_host or not target_host:
            return None

        return base_host == target_host
    except Exception:
        return None


def _clean_text(raw_text: Optional[str]) -> str:
    """Normalize internal excessive whitespace."""
    if not raw_text:
        return ""
    return " ".join(raw_text.split())


def _normalize_directives(raw_value: Optional[str]) -> tuple[str, ...]:
    """Normalize robots meta / X-Robots-Tag content into deterministic tokens.

    Rules (each identity-preserving for directive names):
    - lowercased;
    - split on commas, then on whitespace;
    - a user-agent-qualified directive keeps only its directive portion
      (``googlebot: none`` -> ``none``), using a known UA-prefix set;
    - a directive with a parameter keeps only the directive name
      (``max-image-preview:large`` -> ``max-image-preview``,
      ``unavailable_after: 2024-01-01`` -> ``unavailable_after``);
    - duplicates removed, first-seen order preserved.
    """
    if not raw_value:
        return ()
    robot_user_agents = {
        "google", "googlebot", "googlebot-news", "googlebot-video",
        "googlebot-image", "bingbot", "bing", "yahoo", "slurp",
        "yandex", "yandexbot", "baiduspider", "duckduckbot", "msnbot",
        "ia_archiver", "archive.org_bot", "applebot", "ahrefsbot",
        "semrushbot", "mj12bot", "pingdombot", "uptimerobot", "facebookexternalhit",
    }
    seen: list[str] = []
    for segment in raw_value.split(","):
        token = segment.strip().lower()
        if not token:
            continue
        if ":" in token:
            left, _, right = token.partition(":")
            left = left.strip()
            right = right.strip()
            if left in robot_user_agents:
                # UA-qualified directive: keep only the directive portion.
                token = right
            else:
                # Directive with a parameter: keep only the directive name.
                token = left
            if not token:
                continue
        for word in token.split():
            word = word.strip()
            if word and word not in seen:
                seen.append(word)
    return tuple(seen)


def _extract_robots_meta(soup: BeautifulSoup) -> tuple[Optional[str], tuple[str, ...]]:
    """Extract robots meta directives: raw content plus normalized tokens."""
    contents: list[str] = []
    for meta in soup.find_all("meta"):
        name_attr = (meta.get("name") or "").strip().lower()
        if name_attr != "robots":
            continue
        content = (meta.get("content") or "").strip()
        if content:
            contents.append(content)
    if not contents:
        return None, ()
    raw = ", ".join(contents)
    return raw, _normalize_directives(raw)


def _extract_x_robots_tag(response_headers: dict[str, str]) -> tuple[Optional[str], tuple[str, ...]]:
    """Extract X-Robots-Tag directives from response headers (case-insensitive)."""
    values: list[str] = []
    for key, value in response_headers.items():
        if str(key).strip().lower() == "x-robots-tag":
            value_str = str(value)
            if value_str.strip():
                values.append(value_str.strip())
    if not values:
        return None, ()
    raw = "; ".join(values)
    return raw, _normalize_directives(raw)


def _extract_open_graph(soup: BeautifulSoup, base_url: str) -> dict[str, Optional[str]]:
    """Extract the four most useful Open Graph fields (property or name attr)."""
    result: dict[str, Optional[str]] = {
        "og:title": None,
        "og:description": None,
        "og:type": None,
        "og:url": None,
    }
    for meta in soup.find_all("meta"):
        prop_attr = str(meta.get("property") or meta.get("name") or "").strip().lower()
        if prop_attr not in result:
            continue
        content = (meta.get("content") or "").strip()
        if not content:
            continue
        if prop_attr == "og:url":
            content = _resolve_url(base_url, content)
        result[prop_attr] = content
    return result


def _extract_headings(soup: BeautifulSoup, tag_name: str) -> list[str]:
    """Extract and normalize all text contents for a given heading level."""
    results: list[str] = []
    for element in soup.find_all(tag_name):
        cleaned = _clean_text(element.get_text())
        if cleaned:
            results.append(cleaned)
    return results


def _extract_json_ld(soup: BeautifulSoup) -> list[JsonLdObservation]:
    """Extract and parse all application/ld+json script blocks."""
    json_ld_list: list[JsonLdObservation] = []
    for script_tag in soup.find_all("script"):
        script_type = (script_tag.get("type") or "").strip().lower()
        if script_type != "application/ld+json":
            continue

        raw_script_content = script_tag.string
        if raw_script_content is None:
            raw_script_content = script_tag.get_text()
        raw_text = raw_script_content.strip() if raw_script_content else ""

        if not raw_text:
            json_ld_list.append(
                JsonLdObservation(
                    raw_text="",
                    parsed_data=None,
                    is_valid=False,
                    error_message="Empty JSON-LD block",
                )
            )
            continue

        try:
            parsed = json.loads(raw_text)
            json_ld_list.append(
                JsonLdObservation(
                    raw_text=raw_text,
                    parsed_data=parsed,
                    is_valid=True,
                    error_message=None,
                )
            )
        except Exception as parse_err:
            json_ld_list.append(
                JsonLdObservation(
                    raw_text=raw_text,
                    parsed_data=None,
                    is_valid=False,
                    error_message=f"JSON decode error: {parse_err}",
                )
            )
    return json_ld_list


def _extract_visible_text(html_content: str, soup: Optional[BeautifulSoup] = None) -> str:
    """Extract human-readable visible text, removing scripts, styles, and templates.

    When a ``soup`` instance is provided it is reused (and mutated) instead of
    re-parsing ``html_content``, so JSON-LD/component extraction can run first
    against the unmodified DOM.
    """
    if soup is None:
        if not html_content:
            return ""
        try:
            text_soup = BeautifulSoup(html_content, "html.parser")
        except Exception:
            return ""
    else:
        text_soup = soup

    try:
        for tag in text_soup(["script", "style", "noscript", "template"]):
            tag.decompose()

        lines = text_soup.get_text(separator="\n").splitlines()
        normalized_lines = [_clean_text(line) for line in lines]
        return "\n".join(line for line in normalized_lines if line)
    except Exception:
        return ""


#: Semantic block tags used for generic component discovery.
_COMPONENT_TAGS: frozenset[str] = frozenset(
    {"article", "aside", "header", "footer", "main", "nav", "section"}
)

#: Structured block tags whose contents are typically self-contained.
_COMPONENT_STRUCTURED_TAGS: frozenset[str] = frozenset({"form", "ul", "ol"})

#: Map from tag to a domain-agnostic component kind label.
_COMPONENT_KIND_BY_TAG: dict[str, str] = {
    "nav": "navigation",
    "form": "form",
    "ul": "list",
    "ol": "list",
    "article": "article",
    "aside": "complementary",
    "header": "header",
    "footer": "footer",
    "main": "main",
    "section": "section",
}

#: Generic content scaffolding keywords used to recognize component-like
#: division blocks (pricing cards, product tiles, FAQ panels, ...). These are
#: structural only and never site/domain specific.
_COMPONENT_HINT_WORDS: frozenset[str] = frozenset(
    {
        "pricing", "price", "plan", "plans", "product", "products", "card",
        "cards", "tile", "panel", "feature", "features", "faq", "service",
        "services", "offer", "offers", "package", "packages", "bundle",
        "item", "listing", "plans", "tier", "tiers", "hero", "spotlight",
        "comparison", "grid", "catalog", "menu", "details", "summary",
    }
)

_MAX_COMPONENTS: int = 200
_MAX_COMPONENT_TEXT: int = 2000
_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")


def _component_hint_word(element: Any) -> str:
    """Return the first generic scaffolding keyword found in id/class tokens."""
    for attr_name in ("id", "class"):
        attr_value = element.get(attr_name)
        if attr_value is None:
            continue
        tokens: list[str] = []
        if isinstance(attr_value, list):
            for token in attr_value:
                tokens.extend(str(token).split())
        else:
            tokens.extend(str(attr_value).split())
        for token in tokens:
            for part in re.split(r"[^a-z0-9]+", token.lower()):
                if part in _COMPONENT_HINT_WORDS:
                    return part
    return ""


def _component_source_identifier(element: Any) -> str:
    """Build a structural locator from the element's id/class attributes."""
    parts: list[str] = []
    element_id = element.get("id")
    if element_id:
        parts.append(f'id="{element_id}"')
    classes = element.get("class")
    if classes:
        class_text = " ".join(classes) if isinstance(classes, list) else str(classes)
        parts.append(f'class="{class_text}"')
    return " ".join(parts)


def _extract_components(soup: BeautifulSoup, base_url: str) -> list[ComponentObservation]:
    """Extract self-contained content components with heading provenance.

    Candidate selection is purely structural: semantic block tags plus generic
    ``<div>`` blocks carrying content-scaffolding keywords in id/class. Nested
    candidates are collapsed to the outermost component, output is bounded and
    deterministic, and each component records its nearest preceding heading.
    """
    if soup is None:
        return []

    elements = soup.find_all(True)
    element_index = {id(element): i for i, element in enumerate(elements)}

    heading_positions: list[int] = []
    heading_texts: list[str] = []
    for i, element in enumerate(elements):
        if element.name in _HEADING_TAGS:
            cleaned = _clean_text(element.get_text())
            if cleaned:
                heading_positions.append(i)
                heading_texts.append(cleaned)

    def _nearest_heading(element: Any) -> str:
        inner = element.find(_HEADING_TAGS)
        if inner is not None:
            cleaned = _clean_text(inner.get_text())
            if cleaned:
                return cleaned
        position = element_index.get(id(element), -1)
        idx = bisect.bisect_right(heading_positions, position) - 1
        if idx >= 0:
            return heading_texts[idx]
        return ""

    candidates: list[tuple[Any, str, str]] = []
    for element in elements:
        tag_name = (element.name or "").lower()
        if tag_name in _COMPONENT_KIND_BY_TAG:
            hint = ""
        elif tag_name == "div":
            hint = _component_hint_word(element)
            if not hint:
                continue
        else:
            continue
        text = _clean_text(element.get_text())[:_MAX_COMPONENT_TEXT]
        if not text:
            continue
        candidates.append((element, tag_name, hint))

    kept: list[Any] = []
    observations: list[ComponentObservation] = []
    # Innermost-first (reverse document order): a container candidate is kept
    # only when it does not wrap an already-kept, more specific candidate, so
    # per-card/product components survive instead of being folded into a parent
    # section. Results are re-reversed to preserve document order.
    for element, tag_name, _hint in reversed(candidates):
        if any(kept_elem in element for kept_elem in kept):
            continue
        kind = _COMPONENT_KIND_BY_TAG.get(tag_name, "content_component")
        observations.append(
            ComponentObservation(
                kind=kind,
                source_tag=tag_name,
                source_identifier=_component_source_identifier(element),
                heading=_nearest_heading(element),
                text=_clean_text(element.get_text())[:_MAX_COMPONENT_TEXT],
                page_url=base_url,
            )
        )
        kept.append(element)
        if len(kept) >= _MAX_COMPONENTS:
            break
    observations.reverse()
    return observations


def _extract_twitter_card(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    """Extract the three most useful Twitter Card meta fields (name or property)."""
    result: dict[str, Optional[str]] = {
        "twitter:title": None,
        "twitter:description": None,
        "twitter:card": None,
    }
    for meta in soup.find_all("meta"):
        key = str(meta.get("name") or meta.get("property") or "").strip().lower()
        if key not in result:
            continue
        content = (meta.get("content") or "").strip()
        if not content:
            continue
        result[key] = content
    return result


def observe_page(page: FetchedPage) -> PageObservation:
    """Extract structural facts, metadata, text, and signals from a FetchedPage.

    Args:
        page: A FetchedPage instance returned by the crawler layer.

    Returns:
        A PageObservation instance capturing the extracted data.
    """
    base_url = page.final_url or page.requested_url

    # Handle failed or empty page responses cleanly
    if not page.is_success or not page.html_content:
        return PageObservation(
            requested_url=page.requested_url,
            final_url=base_url,
            status_code=page.status_code,
            is_success=page.is_success,
            error_message=page.error_message or "No HTML content available to observe.",
            title=None,
            meta_description=None,
            canonical_url=None,
            h1=[],
            h2=[],
            h3=[],
            visible_text="",
            links=[],
            images=[],
            tables=[],
            json_ld=[],
            signals=PageSignals(
                has_title=False,
                has_meta_description=False,
                has_canonical=False,
                num_links=0,
                num_images=0,
                num_tables=0,
                num_headings=0,
            ),
            content_type=page.content_type,
            response_headers=dict(page.response_headers or {}),
            robots_meta_raw=None,
            robots_meta_directives=(),
            x_robots_tag_raw=None,
            x_robots_tag_directives=(),
            og_title=None,
            og_description=None,
            og_type=None,
            og_url=None,
        )

    # Parse HTML DOM with BeautifulSoup
    try:
        soup = BeautifulSoup(page.html_content, "html.parser")
    except Exception as err:
        return PageObservation(
            requested_url=page.requested_url,
            final_url=base_url,
            status_code=page.status_code,
            is_success=False,
            error_message=f"Failed to parse HTML document: {err}",
            title=None,
            meta_description=None,
            canonical_url=None,
            h1=[],
            h2=[],
            h3=[],
            visible_text="",
            links=[],
            images=[],
            tables=[],
            json_ld=[],
            signals=PageSignals(
                has_title=False,
                has_meta_description=False,
                has_canonical=False,
                num_links=0,
                num_images=0,
                num_tables=0,
                num_headings=0,
            ),
            content_type=page.content_type,
            response_headers=dict(page.response_headers or {}),
            robots_meta_raw=None,
            robots_meta_directives=(),
            x_robots_tag_raw=None,
            x_robots_tag_directives=(),
            og_title=None,
            og_description=None,
            og_type=None,
            og_url=None,
        )

    # 1. Metadata: Title
    title: Optional[str] = None
    title_tag = soup.find("title")
    if title_tag:
        title_text = _clean_text(title_tag.get_text())
        if title_text:
            title = title_text

    # 2. Metadata: Meta Description
    meta_description: Optional[str] = None
    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if meta_desc_tag and meta_desc_tag.get("content"):
        desc_text = _clean_text(meta_desc_tag.get("content"))
        if desc_text:
            meta_description = desc_text

    # 3. Metadata: Canonical URL
    canonical_url: Optional[str] = None
    canonical_tag = soup.find(
        "link",
        attrs={"rel": lambda r: bool(r and "canonical" in [item.lower() for item in (r if isinstance(r, list) else [r])])},
    )
    if canonical_tag and canonical_tag.get("href"):
        raw_canonical = canonical_tag.get("href", "").strip()
        if raw_canonical:
            canonical_url = _resolve_url(base_url, raw_canonical)

    # 4. Headings (h1, h2, h3)
    h1 = _extract_headings(soup, "h1")
    h2 = _extract_headings(soup, "h2")
    h3 = _extract_headings(soup, "h3")

    # 5. JSON-LD scripts (extracted before visible-text cleanup so the shared
    #    soup still contains the <script> blocks)
    json_ld_list = _extract_json_ld(soup)

    # 6. Content components with source provenance (P1 generalization)
    components = _extract_components(soup, base_url)

    # 7. Visible text (excludes scripts, styles, noscript, templates)
    visible_text = _extract_visible_text(page.html_content, soup=soup)

    # 8. Links
    links: list[LinkObservation] = []
    for a_tag in soup.find_all("a"):
        raw_href = a_tag.get("href")
        raw_href_str = raw_href.strip() if raw_href is not None else ""
        resolved_href = resolve_and_normalize(base_url, raw_href_str) if raw_href_str else ""
        anchor_text = _clean_text(a_tag.get_text())
        is_internal = _classify_is_internal(base_url, resolved_href) if resolved_href else None

        links.append(
            LinkObservation(
                href=resolved_href,
                raw_href=raw_href_str,
                anchor_text=anchor_text,
                is_internal=is_internal,
            )
        )

    # 7. Images
    images: list[ImageObservation] = []
    for img_tag in soup.find_all("img"):
        raw_src = img_tag.get("src")
        raw_src_str = raw_src.strip() if raw_src is not None else ""
        resolved_src = _resolve_url(base_url, raw_src_str) if raw_src_str else ""
        alt_text = _clean_text(img_tag.get("alt", ""))

        images.append(
            ImageObservation(
                src=resolved_src,
                alt=alt_text,
            )
        )

    # 8. Tables
    tables: list[TableObservation] = []
    for table_tag in soup.find_all("table"):
        headers: list[str] = []
        rows: list[list[str]] = []

        # Header detection via <thead>
        thead = table_tag.find("thead")
        if thead:
            header_cells = thead.find_all(["th", "td"])
            headers = [_clean_text(cell.get_text()) for cell in header_cells]

        # Extract all rows
        for tr in table_tag.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            row_content = [_clean_text(cell.get_text()) for cell in cells]
            if row_content:
                rows.append(row_content)

        # Fallback header detection if <thead> was not used but first row is <th>
        if not headers and rows:
            first_tr = table_tag.find("tr")
            if first_tr and first_tr.find_all("th") and not first_tr.find_all("td"):
                headers = list(rows[0])

        tables.append(TableObservation(headers=headers, rows=rows))

    # 9. Quantitative Signals
    signals = PageSignals(
        has_title=title is not None,
        has_meta_description=meta_description is not None,
        has_canonical=canonical_url is not None,
        num_links=len(links),
        num_images=len(images),
        num_tables=len(tables),
        num_headings=len(h1) + len(h2) + len(h3),
        num_components=len(components),
    )

    # 10. Robots / indexability metadata (P1-T3)
    robots_meta_raw, robots_meta_directives = _extract_robots_meta(soup)
    x_robots_tag_raw, x_robots_tag_directives = _extract_x_robots_tag(page.response_headers or {})

    # 11. Open Graph metadata (P1-T3)
    og = _extract_open_graph(soup, base_url)

    # 12. Twitter Card metadata
    twitter = _extract_twitter_card(soup)

    return PageObservation(
        requested_url=page.requested_url,
        final_url=base_url,
        status_code=page.status_code,
        is_success=True,
        error_message=None,
        title=title,
        meta_description=meta_description,
        canonical_url=canonical_url,
        h1=h1,
        h2=h2,
        h3=h3,
        visible_text=visible_text,
        links=links,
        images=images,
        tables=tables,
        json_ld=json_ld_list,
        signals=signals,
        content_type=page.content_type,
        response_headers=dict(page.response_headers or {}),
        robots_meta_raw=robots_meta_raw,
        robots_meta_directives=robots_meta_directives,
        x_robots_tag_raw=x_robots_tag_raw,
        x_robots_tag_directives=x_robots_tag_directives,
        og_title=og.get("og:title"),
        og_description=og.get("og:description"),
        og_type=og.get("og:type"),
        og_url=og.get("og:url"),
        twitter_title=twitter.get("twitter:title"),
        twitter_description=twitter.get("twitter:description"),
        twitter_card=twitter.get("twitter:card"),
        components=components,
    )


def create_page_observation(page: FetchedPage) -> PageObservation:
    """Alias for observe_page to construct a PageObservation."""
    return observe_page(page)


def observe_html(
    html_content: str,
    requested_url: str = "https://example.com/",
    final_url: str = "https://example.com/",
    status_code: Optional[int] = 200,
) -> PageObservation:
    """Convenience helper to observe raw HTML strings directly."""
    page = FetchedPage(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        content_type="text/html",
        html_content=html_content,
        is_success=True,
        error_message=None,
    )
    return observe_page(page)
