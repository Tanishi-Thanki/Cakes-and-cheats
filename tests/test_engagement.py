"""Unit tests for the Engagement Audit skill (src/audits/engagement.py).

Test contract:
    - audit_engagement(obs) returns list[Finding].
    - Returns [] when obs.is_success is False.
    - ENG-001 triggers on deep generic landing pages with low content and no schema.
    - ENG-001 does NOT trigger on root homepages, rich text pages, or pages with schema.
    - ENG-002 triggers on deep URL slug mismatch to generic fallback headings.
    - ENG-002 does NOT trigger on numeric IDs, matching headings, or body token presence.
    - ENG-003 triggers on commercial entities lacking continuation/action affordances.
    - ENG-003 does NOT trigger when Buy, Get Started, Learn More, Request Quote, mailto/tel exist.
    - ENG-004 triggers on active multi-step workflows ("Step 1 of 3") lacking advancement links.
    - ENG-004 does NOT trigger on static "5 Steps to..." articles or when Next/Continue exist.
    - Models strictly satisfy Finding dataclass standards.
"""


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest

from src.audits.engagement import (
    _extract_descriptive_slug_tokens,
    _is_root_path,
    audit_engagement,
)
from src.audits.models import Finding, SuggestedAction
from src.observation.observation import (
    JsonLdObservation,
    LinkObservation,
    PageObservation,
    PageSignals,
)


def _make_observation(
    requested_url: str = "https://example.com/products/pro-running-shoe-900",
    final_url: str = "https://example.com/products/pro-running-shoe-900",
    is_success: bool = True,
    title: str | None = "Pro Running Shoe 900",
    h1: list[str] | None = None,
    h2: list[str] | None = None,
    visible_text: str = "Explore the Pro Running Shoe 900 with engineered mesh and responsive cushion.",
    links: list[LinkObservation] | None = None,
    json_ld: list[JsonLdObservation] | None = None,
) -> PageObservation:
    """Helper to build a controlled PageObservation object for testing."""
    if h1 is None:
        h1 = ["Pro Running Shoe 900"]
    if h2 is None:
        h2 = []
    if links is None:
        links = [
            LinkObservation(
                href="https://example.com/cart/add",
                raw_href="/cart/add",
                anchor_text="Buy Now",
                is_internal=True,
            )
        ]
    if json_ld is None:
        json_ld = []

    return PageObservation(
        requested_url=requested_url,
        final_url=final_url,
        status_code=200 if is_success else None,
        is_success=is_success,
        title=title,
        h1=h1,
        h2=h2,
        visible_text=visible_text,
        links=links,
        json_ld=json_ld,
        signals=PageSignals(
            has_title=bool(title),
            has_meta_description=False,
            has_canonical=False,
            num_links=len(links),
            num_images=0,
            num_tables=0,
            num_headings=len(h1) + len(h2),
        ),
    )


class TestEngagementAudit(unittest.TestCase):
    """Comprehensive test suite for Engagement Audit rules ENG-001 through ENG-004."""

    # ---------------------------------------------------------------------------
    # General & Contract Tests
    # ---------------------------------------------------------------------------

    def test_failed_observation_returns_empty_list(self) -> None:
        """Test 24: Unsuccessful PageObservation returns [] without exception."""
        obs = _make_observation(is_success=False)
        findings = audit_engagement(obs)
        self.assertEqual(findings, [])

    def test_finding_dataclass_contract_compliance(self) -> None:
        """Test 26: Every generated finding strictly complies with Finding dataclass expectations."""
        obs = _make_observation(
            requested_url="https://example.com/dept/welcome-page",
            final_url="https://example.com/dept/welcome-page",
            title="Welcome",
            h1=["Welcome"],
            visible_text="Short text here.",
            links=[],
        )
        findings = audit_engagement(obs)
        self.assertGreaterEqual(len(findings), 1)
        for f in findings:
            self.assertIsInstance(f, Finding)
            self.assertTrue(f.id.startswith("ENG-"))
            self.assertIn(f.severity, ["critical", "high", "medium", "low"])
            self.assertTrue(bool(f.evidence))
            self.assertIsInstance(f.suggested_action, SuggestedAction)
            self.assertIn(f.suggested_action.priority, [1, 2, 3])
            self.assertIn(f.priority, [1, 2, 3])
            self.assertIn(f.confidence, ["high", "medium", "low"])

    # ---------------------------------------------------------------------------
    # Helper Unit Tests
    # ---------------------------------------------------------------------------

    def test_is_root_path(self) -> None:
        """Test helper _is_root_path logic."""
        self.assertTrue(_is_root_path("https://example.com/"))
        self.assertTrue(_is_root_path("https://example.com"))
        self.assertTrue(_is_root_path("https://example.com/index.html"))
        self.assertFalse(_is_root_path("https://example.com/products/shoe-x"))

    def test_extract_descriptive_slug_tokens(self) -> None:
        """Test helper _extract_descriptive_slug_tokens logic."""
        tokens = _extract_descriptive_slug_tokens("https://example.com/products/pro-running-shoe-900")
        self.assertIn("running", tokens)
        self.assertIn("shoe", tokens)
        self.assertNotIn("products", tokens)
        self.assertNotIn("900", tokens)  # Numeric ignored

    # ---------------------------------------------------------------------------
    # ENG-001 Tests
    # ---------------------------------------------------------------------------

    def test_eng_001_true_positive_deep_generic_landing(self) -> None:
        """Test 1: Deep generic landing page with 'Welcome' title and 30 words triggers ENG-001."""
        obs = _make_observation(
            requested_url="https://example.com/landing/portal-view",
            final_url="https://example.com/landing/portal-view",
            title="Welcome",
            h1=["Welcome"],
            h2=[],
            visible_text="Welcome to our website portal. Please browse our sections below.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertIn("ENG-001", finding_ids)
        eng_001 = next(f for f in findings if f.id == "ENG-001")
        self.assertEqual(eng_001.severity, "medium")
        self.assertEqual(eng_001.priority, 2)
        self.assertEqual(eng_001.confidence, "high")

    def test_eng_001_root_homepage_does_not_trigger(self) -> None:
        """Test 2: Root homepage URL with 'Welcome' title does NOT trigger ENG-001."""
        obs = _make_observation(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            title="Welcome",
            h1=["Welcome"],
            h2=[],
            visible_text="Welcome to our main portal site.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-001", finding_ids)

    def test_eng_001_well_oriented_deep_page_does_not_trigger(self) -> None:
        """Test 3: Well-oriented deep page with specific title does NOT trigger ENG-001."""
        obs = _make_observation(
            requested_url="https://example.com/products/shoe-x",
            final_url="https://example.com/products/shoe-x",
            title="Shoe X Performance",
            h1=["Shoe X Performance"],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-001", finding_ids)

    def test_eng_001_generic_title_with_rich_text_does_not_trigger(self) -> None:
        """Test 4: Generic title 'Welcome' with >200 words body text does NOT trigger ENG-001."""
        rich_text = "word " * 220
        obs = _make_observation(
            requested_url="https://example.com/info/welcome",
            final_url="https://example.com/info/welcome",
            title="Welcome",
            h1=["Welcome"],
            visible_text=rich_text,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-001", finding_ids)

    def test_eng_001_generic_title_with_product_schema_does_not_trigger(self) -> None:
        """Test 5: Generic title 'Welcome' but valid Product JSON-LD schema does NOT trigger ENG-001."""
        json_ld_block = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Running Shoe"}',
            parsed_data={"@type": "Product", "name": "Running Shoe"},
            is_valid=True,
        )
        obs = _make_observation(
            requested_url="https://example.com/landing/welcome",
            final_url="https://example.com/landing/welcome",
            title="Welcome",
            h1=["Welcome"],
            visible_text="Short content.",
            json_ld=[json_ld_block],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-001", finding_ids)

    # ---------------------------------------------------------------------------
    # ENG-002 Tests
    # ---------------------------------------------------------------------------

    def test_eng_002_true_positive_slug_mismatch_to_fallback(self) -> None:
        """Test 6: Specific URL slug mismatch landing on 'All Products' fallback triggers ENG-002."""
        obs = _make_observation(
            requested_url="https://example.com/catalog/pro-running-shoe",
            final_url="https://example.com/catalog/pro-running-shoe",
            title="All Products",
            h1=["All Products"],
            visible_text="Browse our full catalog of general merchandise.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertIn("ENG-002", finding_ids)
        eng_002 = next(f for f in findings if f.id == "ENG-002")
        self.assertEqual(eng_002.severity, "high")
        self.assertEqual(eng_002.priority, 1)

    def test_eng_002_matching_heading_does_not_trigger(self) -> None:
        """Test 7: URL slug tokens matching H1 title do NOT trigger ENG-002."""
        obs = _make_observation(
            requested_url="https://example.com/catalog/pro-running-shoe",
            final_url="https://example.com/catalog/pro-running-shoe",
            title="Pro Running Shoe Catalog",
            h1=["Pro Running Shoe"],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-002", finding_ids)

    def test_eng_002_numeric_id_url_does_not_trigger(self) -> None:
        """Test 8: URL containing only numeric IDs (/item/98421) does NOT trigger ENG-002."""
        obs = _make_observation(
            requested_url="https://example.com/item/98421",
            final_url="https://example.com/item/98421",
            title="All Products",
            h1=["All Products"],
            visible_text="Browse items.",
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-002", finding_ids)

    def test_eng_002_slight_wording_difference_does_not_trigger(self) -> None:
        """Test 9: Slight wording difference between URL and title does NOT trigger ENG-002."""
        obs = _make_observation(
            requested_url="https://example.com/store/performance-running-sneaker",
            final_url="https://example.com/store/performance-running-sneaker",
            title="Running Sneaker - Performance Edition",
            h1=["Running Sneaker Details"],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-002", finding_ids)

    def test_eng_002_body_token_presence_prevents_finding(self) -> None:
        """Test 11: Body text containing >20% slug tokens prevents ENG-002 finding."""
        obs = _make_observation(
            requested_url="https://example.com/items/lightweight-marathon-footwear",
            final_url="https://example.com/items/lightweight-marathon-footwear",
            title="All Products",
            h1=["All Products"],
            visible_text="Check out our lightweight marathon footwear collection designed for athletes.",
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-002", finding_ids)

    # ---------------------------------------------------------------------------
    # ENG-003 Tests
    # ---------------------------------------------------------------------------

    def test_eng_003_true_positive_commercial_entity_no_continuation(self) -> None:
        """Test 12: Product page with zero action/continuation links triggers ENG-003."""
        obs = _make_observation(
            requested_url="https://example.com/products/pro-shoe",
            final_url="https://example.com/products/pro-shoe",
            title="Pro Shoe Model 100",
            h1=["Pro Shoe Model 100"],
            visible_text="Product description of Pro Shoe Model 100.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertIn("ENG-003", finding_ids)
        eng_003 = next(f for f in findings if f.id == "ENG-003")
        self.assertEqual(eng_003.severity, "medium")
        self.assertEqual(eng_003.priority, 2)

    def test_eng_003_buy_link_prevents_finding(self) -> None:
        """Test 13: Link with 'Buy Now' anchor text prevents ENG-003."""
        links = [LinkObservation(href="/buy", raw_href="/buy", anchor_text="Buy Now", is_internal=True)]
        obs = _make_observation(
            title="Pro Shoe Model 100",
            h1=["Pro Shoe Model 100"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    def test_eng_003_get_started_link_prevents_finding(self) -> None:
        """Test 14: Link with 'Get Started' anchor text prevents ENG-003."""
        links = [LinkObservation(href="/start", raw_href="/start", anchor_text="Get Started", is_internal=True)]
        obs = _make_observation(
            title="Cloud API Plan",
            h1=["Cloud API Plan"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    def test_eng_003_explore_learn_more_prevents_finding(self) -> None:
        """Test 15: Link with 'Explore Specifications' or 'Learn More' anchor text prevents ENG-003."""
        links = [LinkObservation(href="/specs", raw_href="/specs", anchor_text="Explore Specifications", is_internal=True)]
        obs = _make_observation(
            title="Device Model X",
            h1=["Device Model X"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    def test_eng_003_request_quote_prevents_finding(self) -> None:
        """Test 16: Link with 'Request Quote' anchor text prevents ENG-003."""
        links = [LinkObservation(href="/quote", raw_href="/quote", anchor_text="Request Quote", is_internal=True)]
        obs = _make_observation(
            title="Enterprise Service Tier",
            h1=["Enterprise Service Tier"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    def test_eng_003_mailto_tel_prevents_finding(self) -> None:
        """Test 17: mailto: or tel: link href prevents ENG-003."""
        links = [LinkObservation(href="mailto:info@example.com", raw_href="mailto:info@example.com", anchor_text="Email Sales", is_internal=False)]
        obs = _make_observation(
            title="Custom Tool Service",
            h1=["Custom Tool Service"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    def test_eng_003_informational_article_does_not_trigger(self) -> None:
        """Test 18: Informational article title does NOT trigger ENG-003."""
        obs = _make_observation(
            title="Article: History of Running Shoe Engineering",
            h1=["History of Running Shoe Engineering"],
            visible_text="Informational blog story.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-003", finding_ids)

    # ---------------------------------------------------------------------------
    # ENG-004 Tests
    # ---------------------------------------------------------------------------

    def test_eng_004_true_positive_multistep_workflow_no_advancement(self) -> None:
        """Test 19: Active workflow 'Step 1 of 3' with zero advancement links triggers ENG-004."""
        obs = _make_observation(
            requested_url="https://example.com/register/step1",
            final_url="https://example.com/register/step1",
            title="Account Registration - Step 1",
            h1=["Account Registration - Step 1"],
            visible_text="Enter your profile details below.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertIn("ENG-004", finding_ids)
        eng_004 = next(f for f in findings if f.id == "ENG-004")
        self.assertEqual(eng_004.severity, "medium")
        self.assertEqual(eng_004.priority, 2)

    def test_eng_004_continue_link_prevents_finding(self) -> None:
        """Test 20: 'Continue' link prevents ENG-004 finding."""
        links = [LinkObservation(href="/step2", raw_href="/step2", anchor_text="Continue to Step 2", is_internal=True)]
        obs = _make_observation(
            title="Onboarding Step 1",
            h1=["Onboarding Step 1"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-004", finding_ids)

    def test_eng_004_next_link_prevents_finding(self) -> None:
        """Test 21: 'Next Step' link prevents ENG-004 finding."""
        links = [LinkObservation(href="/step2", raw_href="/step2", anchor_text="Next Step", is_internal=True)]
        obs = _make_observation(
            title="Step 1 of 3",
            h1=["Step 1 of 3"],
            links=links,
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-004", finding_ids)

    def test_eng_004_static_steps_article_does_not_trigger(self) -> None:
        """Test 22: Static informational article '5 Steps to Better Health' does NOT trigger ENG-004."""
        obs = _make_observation(
            title="5 Steps to Better Health",
            h1=["5 Steps to Better Health"],
            h2=["Step 1: Eat Vegetables", "Step 2: Exercise Daily"],
            visible_text="Read our guide on 5 steps to better health.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertNotIn("ENG-004", finding_ids)

    # ---------------------------------------------------------------------------
    # Edge Cases & Coexistence Tests
    # ---------------------------------------------------------------------------

    def test_empty_title_and_h1_edge_case(self) -> None:
        """Test edge case: Empty title and empty H1 list handled gracefully."""
        obs = _make_observation(
            title=None,
            h1=[],
            h2=[],
            visible_text="Plain text.",
            links=[],
        )
        findings = audit_engagement(obs)
        self.assertIsInstance(findings, list)

    def test_multiple_engagement_rules_coexist(self) -> None:
        """Test 30: Multiple rules can coexist on a single severely defective page."""
        # Deep generic landing (ENG-001) + commercial title no links (ENG-003)
        obs = _make_observation(
            requested_url="https://example.com/store/welcome-view",
            final_url="https://example.com/store/welcome-view",
            title="Welcome",
            h1=["Welcome - Store Model Pro"],
            visible_text="Short content.",
            links=[],
        )
        findings = audit_engagement(obs)
        finding_ids = [f.id for f in findings]
        self.assertIn("ENG-001", finding_ids)
        self.assertIn("ENG-003", finding_ids)


if __name__ == "__main__":
    unittest.main()
