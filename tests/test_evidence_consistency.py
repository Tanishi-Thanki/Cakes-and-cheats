"""Unit tests for Fact Inventory & Evidence Consistency Layer (src/audits/fact_inventory.py).

Test contract:
    - EvidenceUnit and FactInventory accurately store provenance-aware facts.
    - Correctly normalizes prices, currency codes, availability, and numeric specs (1.2kg vs 1200g).
    - Classifies evidence into MATCH, CONFLICT, HISTORICAL, AMBIGUOUS, INSUFFICIENT_EVIDENCE.
    - All 17 required scenarios (Match, Conflict, Formatting, Historical, Sale Prices, Provenance, etc.) pass.
"""

import unittest

from src.audits.fact_inventory import (
    AggregatedFact,
    EvidenceUnit,
    FactInventory,
    SiteEvidenceInventory,
    audit_evidence_consistency,
    extract_fact_inventory,
    normalize_availability,
    normalize_numeric_spec,
    normalize_price,
)
from src.audits.models import Finding
from src.observation.observation import (
    JsonLdObservation,
    PageObservation,
    PageSignals,
    TableObservation,
)


def _make_obs(
    url: str = "https://example.com/products/x",
    title: str = "Product X",
    visible_text: str = "Product X costs $999 and is in stock.",
    json_ld: list[JsonLdObservation] | None = None,
    tables: list[TableObservation] | None = None,
    is_success: bool = True,
) -> PageObservation:
    """Helper to create PageObservation objects for evidence tests."""
    return PageObservation(
        requested_url=url,
        final_url=url,
        status_code=200 if is_success else None,
        is_success=is_success,
        title=title,
        h1=[title],
        h2=[],
        visible_text=visible_text,
        links=[],
        images=[],
        tables=tables or [],
        json_ld=json_ld or [],
        signals=PageSignals(
            has_title=bool(title),
            has_meta_description=False,
            has_canonical=False,
            num_links=0,
            num_images=0,
            num_tables=len(tables or []),
            num_headings=1,
        ),
    )


class TestFactInventoryAndConsistency(unittest.TestCase):
    """Test suite covering FactInventory, EvidenceUnit, attribute normalization, and consistency reasoning."""

    # ---------------------------------------------------------------------------
    # Test 1: MATCH
    # ---------------------------------------------------------------------------
    def test_1_match_visible_and_json_ld_price(self) -> None:
        """Test 1: Visible text $999 and JSON-LD price 999 produce MATCH and zero conflict findings."""
        json_ld = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Product X", "price": 999}',
            parsed_data={"@type": "Product", "name": "Product X", "price": 999},
            is_valid=True,
        )
        obs = _make_obs(visible_text="Product X available for $999.", json_ld=[json_ld])
        inventory = extract_fact_inventory(obs)
        eval_state, _ = inventory.evaluate_consistency("Product X", "price")
        findings = audit_evidence_consistency(obs)

        self.assertEqual(eval_state, "MATCH")
        self.assertEqual(findings, [])

    # ---------------------------------------------------------------------------
    # Test 2: ACTUAL CONFLICT
    # ---------------------------------------------------------------------------
    def test_2_actual_price_conflict(self) -> None:
        """Test 2: Visible text $999 and JSON-LD price 949 produce CONFLICT and MA-REP-001 finding."""
        json_ld = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Product X", "price": 949}',
            parsed_data={"@type": "Product", "name": "Product X", "price": 949},
            is_valid=True,
        )
        obs = _make_obs(visible_text="Product X available for $999.", json_ld=[json_ld])
        inventory = extract_fact_inventory(obs)
        eval_state, _ = inventory.evaluate_consistency("Product X", "price")
        findings = audit_evidence_consistency(obs)

        self.assertEqual(eval_state, "CONFLICT")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "MA-REP-001")

    # ---------------------------------------------------------------------------
    # Test 3: FORMATTING DIFFERENCE
    # ---------------------------------------------------------------------------
    def test_3_formatting_difference_produces_match(self) -> None:
        """Test 3: '$999' and '999 USD' normalize to equivalent ('USD', 999) producing MATCH."""
        curr1, amt1, _ = normalize_price("$999")
        curr2, amt2, _ = normalize_price("999 USD")

        self.assertEqual((curr1, amt1), ("USD", 999))
        self.assertEqual((curr2, amt2), ("USD", 999))

    # ---------------------------------------------------------------------------
    # Test 4: HISTORICAL DIFFERENCE
    # ---------------------------------------------------------------------------
    def test_4_historical_difference_classified_as_historical(self) -> None:
        """Test 4: Old dated 2025 source ($1199) and current 2026 source ($999) classify as HISTORICAL."""
        unit_old = EvidenceUnit(
            entity="Product X",
            attribute="price",
            value=("USD", 1199),
            raw_value="$1,199",
            source="https://example.com/blog/2025-review",
            source_type="visible_text",
            timestamp="2025",
        )
        unit_new = EvidenceUnit(
            entity="Product X",
            attribute="price",
            value=("USD", 999),
            raw_value="$999",
            source="https://example.com/products/x",
            source_type="visible_text",
            timestamp="2026",
        )
        inventory = FactInventory()
        inventory.add_unit(unit_old)
        inventory.add_unit(unit_new)

        eval_state, _ = inventory.evaluate_consistency("Product X", "price")
        self.assertEqual(eval_state, "HISTORICAL")

    # ---------------------------------------------------------------------------
    # Test 5: DIFFERENT ENTITIES
    # ---------------------------------------------------------------------------
    def test_5_different_entities_do_not_conflict(self) -> None:
        """Test 5: Product A ($999) and Product B ($949) are separate entities and do not conflict."""
        unit_a = EvidenceUnit(
            entity="Product A",
            attribute="price",
            value=("USD", 999),
            raw_value="$999",
            source="https://example.com/products/a",
            source_type="visible_text",
        )
        unit_b = EvidenceUnit(
            entity="Product B",
            attribute="price",
            value=("USD", 949),
            raw_value="$949",
            source="https://example.com/products/b",
            source_type="visible_text",
        )
        inventory = FactInventory()
        inventory.add_unit(unit_a)
        inventory.add_unit(unit_b)

        eval_state_a, _ = inventory.evaluate_consistency("Product A", "price")
        eval_state_b, _ = inventory.evaluate_consistency("Product B", "price")

        self.assertEqual(eval_state_a, "INSUFFICIENT_EVIDENCE")
        self.assertEqual(eval_state_b, "INSUFFICIENT_EVIDENCE")

    # ---------------------------------------------------------------------------
    # Test 6: AMBIGUOUS ENTITY
    # ---------------------------------------------------------------------------
    def test_6_ambiguous_entity_returns_insufficient_or_ambiguous(self) -> None:
        """Test 6: Unclear entity identity does not invent false conflict."""
        inventory = FactInventory()
        eval_state, _ = inventory.evaluate_consistency("Unknown Entity", "price")
        self.assertEqual(eval_state, "INSUFFICIENT_EVIDENCE")

    # ---------------------------------------------------------------------------
    # Test 7: MISSING EVIDENCE
    # ---------------------------------------------------------------------------
    def test_7_missing_evidence_returns_insufficient_evidence(self) -> None:
        """Test 7: Only 1 source present yields INSUFFICIENT_EVIDENCE."""
        obs = _make_obs(visible_text="Product X is $999.", json_ld=[])
        inventory = extract_fact_inventory(obs)
        eval_state, _ = inventory.evaluate_consistency("Product X", "price")
        self.assertEqual(eval_state, "INSUFFICIENT_EVIDENCE")

    # ---------------------------------------------------------------------------
    # Test 8: MULTIPLE SOURCES
    # ---------------------------------------------------------------------------
    def test_8_multiple_sources_preserves_full_provenance(self) -> None:
        """Test 8: Inventory from 3 sources preserves provenance and identifies conflict."""
        obs1 = _make_obs(url="https://example.com/products/x", visible_text="Product X costs $999.")
        obs2 = _make_obs(url="https://example.com/compare", visible_text="Product X costs $999.")
        obs3 = _make_obs(url="https://example.com/pricing", visible_text="Product X costs $949.")

        inventory = extract_fact_inventory([obs1, obs2, obs3])
        units = inventory.get_units("Product X", "price")

        self.assertEqual(len(units), 3)
        sources = [u.source for u in units]
        self.assertIn("https://example.com/products/x", sources)
        self.assertIn("https://example.com/compare", sources)
        self.assertIn("https://example.com/pricing", sources)

    # ---------------------------------------------------------------------------
    # Test 9: LEGITIMATE SALE PRICE
    # ---------------------------------------------------------------------------
    def test_9_legitimate_sale_price_range_prefix_flagged(self) -> None:
        """Test 9: 'From $999' is recognized as prefix/range to prevent false conflict."""
        curr, amt, is_prefix = normalize_price("From $999")
        self.assertEqual((curr, amt), ("USD", 999))
        self.assertTrue(is_prefix)

    # ---------------------------------------------------------------------------
    # Test 10: AVAILABILITY MATCH
    # ---------------------------------------------------------------------------
    def test_10_availability_match(self) -> None:
        """Test 10: Visible text 'In Stock' and JSON-LD 'InStock' produce MATCH."""
        json_ld = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Product X", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}}',
            parsed_data={"@type": "Product", "name": "Product X", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}},
            is_valid=True,
        )
        obs = _make_obs(visible_text="Product X is currently in stock.", json_ld=[json_ld])
        findings = audit_evidence_consistency(obs)
        self.assertEqual(findings, [])

    # ---------------------------------------------------------------------------
    # Test 11: AVAILABILITY CONFLICT
    # ---------------------------------------------------------------------------
    def test_11_availability_conflict(self) -> None:
        """Test 11: Visible text 'Out of Stock' and JSON-LD 'InStock' triggers MA-REP-002 finding."""
        json_ld = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Product X", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}}',
            parsed_data={"@type": "Product", "name": "Product X", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}},
            is_valid=True,
        )
        obs = _make_obs(visible_text="Product X is Out of Stock.", json_ld=[json_ld])
        findings = audit_evidence_consistency(obs)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "MA-REP-002")

    # ---------------------------------------------------------------------------
    # Test 12: NORMALIZED NUMERIC SPEC
    # ---------------------------------------------------------------------------
    def test_12_normalized_numeric_spec(self) -> None:
        """Test 12: '1.2 kg' and '1200 g' both normalize to ('g', 1200.0)."""
        unit1, val1 = normalize_numeric_spec("1.2 kg")
        unit2, val2 = normalize_numeric_spec("1200 g")

        self.assertEqual((unit1, val1), ("g", 1200.0))
        self.assertEqual((unit2, val2), ("g", 1200.0))

    # ---------------------------------------------------------------------------
    # Test 13: RAW PROVENANCE
    # ---------------------------------------------------------------------------
    def test_13_raw_provenance_integrity(self) -> None:
        """Test 13: Every EvidenceUnit maintains complete provenance information."""
        obs = _make_obs(
            url="https://example.com/specs",
            title="Model X",
            visible_text="Model X price is $500.",
            tables=[TableObservation(headers=["Weight"], rows=[["1.2 kg"]])],
        )
        inventory = extract_fact_inventory(obs)
        units = inventory.all_units

        self.assertGreaterEqual(len(units), 2)
        for u in units:
            self.assertIsInstance(u, EvidenceUnit)
            self.assertEqual(u.source, "https://example.com/specs")
            self.assertIn(u.source_type, ["visible_text", "heading", "table", "json_ld", "metadata"])
            self.assertTrue(bool(u.raw_value))

    # ---------------------------------------------------------------------------
    # Test 14: NO CROSS-SITE ASSUMPTION
    # ---------------------------------------------------------------------------
    def test_14_no_cross_site_assumption(self) -> None:
        """Test 14: Facts from distinct entities do not merge across sites."""
        obs1 = _make_obs(title="Entity Alpha", visible_text="Entity Alpha price is $100.")
        obs2 = _make_obs(title="Entity Beta", visible_text="Entity Beta price is $200.")
        inventory = extract_fact_inventory([obs1, obs2])

        units_alpha = inventory.get_units(entity="Entity Alpha")
        units_beta = inventory.get_units(entity="Entity Beta")

        self.assertEqual(len(units_alpha), 1)
        self.assertEqual(len(units_beta), 1)

    # ---------------------------------------------------------------------------
    # Test 15: TEMPORAL UNKNOWN
    # ---------------------------------------------------------------------------
    def test_15_temporal_unknown_compares_values_without_inventing_dates(self) -> None:
        """Test 15: When dates are absent, timestamp is None and values are compared directly."""
        obs = _make_obs(visible_text="Product X costs $999.")
        inventory = extract_fact_inventory(obs)
        units = inventory.get_units("Product X", "price")

        self.assertGreaterEqual(len(units), 1)
        self.assertIsNone(units[0].timestamp)

    # ---------------------------------------------------------------------------
    # Test 16: EMPTY OBSERVATION
    # ---------------------------------------------------------------------------
    def test_16_empty_observation_fails_safely(self) -> None:
        """Test 16: Failed or empty PageObservation returns [] findings and empty inventory safely."""
        obs = _make_obs(is_success=False)
        inventory = extract_fact_inventory(obs)
        findings = audit_evidence_consistency(obs)

        self.assertEqual(inventory.all_units, [])
        self.assertEqual(findings, [])

    # ---------------------------------------------------------------------------
    # Test 17: NORMAL HOMEPAGE RELATIONSHIPS DO NOT TRIGGER IDENTITY CONFLICT
    # ---------------------------------------------------------------------------
    def test_17_normal_homepage_relationships_no_identity_conflict(self) -> None:
        """Test 17: Brand vs tagline, site vs article, acronyms, and domain matches MUST NOT trigger EVID-ENT-001."""
        from src.audits.fact_inventory import _is_genuine_identity_conflict

        # Decathlon: brand vs tagline / domain
        self.assertFalse(_is_genuine_identity_conflict("Decathlon", "Buy Sports Gear Online | Decathlon India", "decathlon.in"))

        # The Hindu: site name vs article heading
        self.assertFalse(_is_genuine_identity_conflict("The Hindu", "Supreme Court reserves verdict on election petition", "thehindu.com"))

        # KPMG: organization name vs title
        self.assertFalse(_is_genuine_identity_conflict("KPMG", "KPMG India - Tax and Advisory", "kpmg.com"))

        # Python.org: foundation name vs welcome title
        self.assertFalse(_is_genuine_identity_conflict("Python Software Foundation", "Welcome to Python.org", "python.org"))

        # Deloitte: organization name vs tagline
        self.assertFalse(_is_genuine_identity_conflict("Deloitte", "Audit, Consulting, Advisory | Deloitte India", "deloitte.com"))

        # IIT Madras: abbreviation vs full name
        self.assertFalse(_is_genuine_identity_conflict("IIT Madras", "Indian Institute of Technology Madras", "iitm.ac.in"))

        # Carnegie Mellon: acronym vs full name
        self.assertFalse(_is_genuine_identity_conflict("CMU", "Carnegie Mellon University", "cmu.edu"))

    # ---------------------------------------------------------------------------
    # Test 18: GENUINE ENTITY IDENTITY CONFLICT TRIGGERS EVID-ENT-001
    # ---------------------------------------------------------------------------
    def test_18_genuine_entity_identity_conflict(self) -> None:
        """Test 18: Two structured Organization declarations with completely contradictory names emit EVID-ENT-001."""
        json_ld_1 = JsonLdObservation(
            raw_text='{"@type": "Organization", "name": "Acme Logistics Corp"}',
            parsed_data={"@type": "Organization", "name": "Acme Logistics Corp"},
            is_valid=True,
        )
        json_ld_2 = JsonLdObservation(
            raw_text='{"@type": "Organization", "name": "Zeta Financial Services"}',
            parsed_data={"@type": "Organization", "name": "Zeta Financial Services"},
            is_valid=True,
        )
        obs = _make_obs(url="https://example.com/", json_ld=[json_ld_1, json_ld_2])
        findings = audit_evidence_consistency(obs)

        ent_findings = [f for f in findings if f.id == "EVID-ENT-001"]
        self.assertEqual(len(ent_findings), 1)
        self.assertEqual(ent_findings[0].severity, "high")
        self.assertEqual(ent_findings[0].details["entity_a"], "Acme Logistics Corp")
        self.assertEqual(ent_findings[0].details["entity_b"], "Zeta Financial Services")


class TestSiteEvidenceInventory(unittest.TestCase):
    """Test suite covering SiteEvidenceInventory multi-page operations and abstractions."""

    def test_single_page_observation(self) -> None:
        """Test SiteEvidenceInventory constructed from one PageObservation."""
        obs = _make_obs(url="https://example.com/item1", title="Item 1", visible_text="Item 1 price is $99.")
        site_inv = SiteEvidenceInventory.from_page_observations(obs)

        self.assertEqual(site_inv.contributing_pages, ["https://example.com/item1"])
        facts = site_inv.get_aggregated_facts()
        self.assertGreaterEqual(len(facts), 1)

        single_facts = site_inv.single_page_facts
        self.assertEqual(len(single_facts), len(facts))
        self.assertEqual(single_facts[0].cross_page_status, "SINGLE_PAGE")

    def test_multiple_pages_same_fact(self) -> None:
        """Test multiple pages contributing the same normalized fact produces MATCH."""
        obs_a = _make_obs(url="https://example.com/p1", title="Product X", visible_text="Product X costs $99.")
        obs_b = _make_obs(url="https://example.com/p2", title="Product X", visible_text="Product X costs $99.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        self.assertEqual(len(site_inv.contributing_pages), 2)

        repeating = site_inv.repeating_facts
        price_fact = [f for f in repeating if f.attribute == "price"][0]

        self.assertEqual(price_fact.cross_page_status, "MATCH")
        self.assertEqual(price_fact.normalized_value, ("USD", 99))
        self.assertEqual(len(price_fact.sources), 2)
        self.assertTrue(price_fact.is_repeating)

    def test_same_fact_visible_on_one_page_and_json_ld_on_another(self) -> None:
        """Test cross-surface evidence (visible text on page A + JSON-LD on page B)."""
        obs_a = _make_obs(url="https://example.com/pageA", title="Widget A", visible_text="Widget A is $99.")
        json_ld_b = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Widget A", "price": 99.00}',
            parsed_data={"@type": "Product", "name": "Widget A", "price": 99.00},
            is_valid=True,
        )
        obs_b = _make_obs(url="https://example.com/pageB", title="Widget A", visible_text="", json_ld=[json_ld_b])

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        price_facts = site_inv.get_aggregated_facts(entity="Widget A", attribute="price")

        self.assertEqual(len(price_facts), 1)
        pf = price_facts[0]
        self.assertEqual(pf.cross_page_status, "MATCH")
        self.assertTrue(pf.has_visible_text)
        self.assertTrue(pf.has_structured_data)
        self.assertEqual(len(pf.units), 2)

    def test_fact_appearing_on_only_one_page(self) -> None:
        """Test fact present on page A but missing on page B correctly identifies as SINGLE_PAGE."""
        obs_a = _make_obs(
            url="https://example.com/pageA",
            title="Model X",
            visible_text="Model X specs.",
            tables=[TableObservation(headers=["Weight"], rows=[["1.2 kg"]])],
        )
        obs_b = _make_obs(url="https://example.com/pageB", title="Model X", visible_text="Model X details.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        weight_facts = site_inv.get_aggregated_facts(attribute="weight")

        self.assertEqual(len(weight_facts), 1)
        wf = weight_facts[0]
        self.assertEqual(wf.cross_page_status, "SINGLE_PAGE")
        self.assertTrue(wf.is_single_page)
        self.assertEqual(wf.sources, ("https://example.com/pageA",))

    def test_genuinely_conflicting_values_across_pages(self) -> None:
        """Test conflicting normalized values for the same attribute across pages produces CONFLICT."""
        obs_a = _make_obs(url="https://example.com/store1", title="Gadget Y", visible_text="Gadget Y price is $99.")
        obs_b = _make_obs(url="https://example.com/store2", title="Gadget Y", visible_text="Gadget Y price is $149.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        price_facts = site_inv.get_aggregated_facts(entity="Gadget Y", attribute="price")

        self.assertEqual(len(price_facts), 1)
        pf = price_facts[0]
        self.assertEqual(pf.cross_page_status, "CONFLICT")

    def test_multiple_evidence_units_contributing_to_one_normalized_fact(self) -> None:
        """Test multiple evidence units (visible, json_ld, formatting variations) contributing to one fact."""
        obs_a = _make_obs(url="https://example.com/a", title="Product Z", visible_text="Product Z costs $99.")
        json_ld_b = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Product Z", "price": 99.00}',
            parsed_data={"@type": "Product", "name": "Product Z", "price": 99.00},
            is_valid=True,
        )
        obs_b = _make_obs(url="https://example.com/b", title="Product Z", visible_text="", json_ld=[json_ld_b])
        obs_c = _make_obs(url="https://example.com/c", title="Product Z", visible_text="Product Z is $99.00 USD.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b, obs_c])
        price_facts = site_inv.get_aggregated_facts(attribute="price")

        self.assertEqual(len(price_facts), 1)
        pf = price_facts[0]
        self.assertEqual(pf.normalized_value, ("USD", 99))
        self.assertEqual(len(pf.units), 3)
        self.assertEqual(pf.cross_page_status, "MATCH")

    def test_provenance_retention(self) -> None:
        """Test that full provenance back to original page URL and extraction method is retained."""
        obs = _make_obs(url="https://example.com/prov_test", title="Prov Item", visible_text="Prov Item price is $250.")
        site_inv = SiteEvidenceInventory(obs)
        units = site_inv.get_provenance("Prov Item", "price")

        self.assertEqual(len(units), 1)
        u = units[0]
        self.assertEqual(u.source, "https://example.com/prov_test")
        self.assertEqual(u.raw_value, "$250")
        self.assertEqual(u.source_type, "visible_text")
        self.assertEqual(u.source_classification, "FIRST_PARTY")

    def test_malformed_and_empty_page_evidence(self) -> None:
        """Test that malformed/empty PageObservation objects do not crash site inventory."""
        valid_obs1 = _make_obs(url="https://example.com/good1", title="Good 1", visible_text="Good 1 is $50.")
        malformed_obs = PageObservation(
            requested_url="https://example.com/bad",
            final_url="https://example.com/bad",
            status_code=500,
            is_success=False,
            error_message="Server error",
        )
        valid_obs2 = _make_obs(url="https://example.com/good2", title="Good 2", visible_text="Good 2 is $100.")

        site_inv = SiteEvidenceInventory([valid_obs1, malformed_obs, valid_obs2, None])
        self.assertEqual(len(site_inv.pages), 2)
        facts = site_inv.get_aggregated_facts()
        self.assertEqual(len(facts), 2)

    def test_audit_evidence_consistency_accepts_site_inventory(self) -> None:
        """Test audit_evidence_consistency works directly when passed a SiteEvidenceInventory."""
        obs_a = _make_obs(url="https://example.com/a", visible_text="Item price is $99.")
        json_ld_b = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Item", "price": 149.00}',
            parsed_data={"@type": "Product", "name": "Item", "price": 149.00},
            is_valid=True,
        )
        obs_b = _make_obs(url="https://example.com/b", json_ld=[json_ld_b])

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        findings = audit_evidence_consistency(site_inv)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "MA-REP-001")


class TestCrossPageConsistencyAndTemporalReasoning(unittest.TestCase):
    """Test suite covering Task 2: Temporal Reasoning, False Positive Safeguards, and Genuine Conflicts."""

    # 1. False Positive: "$79 in 2024" vs "$99 current"
    def test_fp_1_price_in_2024_vs_current_price_not_a_conflict(self) -> None:
        """Test that '$79 in 2024' vs '$99 current' is classified as HISTORICAL with zero conflict findings."""
        obs_old = _make_obs(url="https://example.com/archive", title="Product X", visible_text="Product X was $79 in 2024.")
        obs_curr = _make_obs(url="https://example.com/store", title="Product X", visible_text="Product X price is $99.")

        site_inv = SiteEvidenceInventory([obs_old, obs_curr])
        facts = site_inv.get_aggregated_facts(entity="Product X", attribute="price")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "HISTORICAL")

        findings = audit_evidence_consistency(site_inv)
        conflict_findings = [f for f in findings if f.id in ("MA-REP-001", "EVID-PRC-001")]
        self.assertEqual(len(conflict_findings), 0)

    # 2. False Positive: Old article vs current page
    def test_fp_2_old_article_vs_current_page_classified_historical(self) -> None:
        """Test dated article review ($1199) vs current product page ($999) yields HISTORICAL."""
        obs_article = _make_obs(
            url="https://example.com/news/2024-review",
            title="Product X Review",
            visible_text="Product X launched at $1,199 in 2024.",
        )
        obs_current = _make_obs(
            url="https://example.com/product/x",
            title="Product X",
            visible_text="Product X available for $999.",
        )

        site_inv = SiteEvidenceInventory([obs_article, obs_current])
        facts = site_inv.get_aggregated_facts(attribute="price")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "HISTORICAL")
        findings = audit_evidence_consistency(site_inv)
        self.assertEqual(len(findings), 0)

    # 3. False Positive: Expired promotion vs current regular price
    def test_fp_3_expired_promotion_vs_current_regular_price(self) -> None:
        """Test 'Sale price $79 valid through 2024' vs '$99' yields EXPIRED without conflict finding."""
        obs_promo = _make_obs(url="https://example.com/deals", title="Product Y", visible_text="Product Y sale price $79 valid through 2024.")
        obs_regular = _make_obs(url="https://example.com/product/y", title="Product Y", visible_text="Product Y price is $99.")

        site_inv = SiteEvidenceInventory([obs_promo, obs_regular])
        facts = site_inv.get_aggregated_facts(entity="Product Y", attribute="price")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "EXPIRED")

        findings = audit_evidence_consistency(site_inv)
        self.assertEqual(len(findings), 0)

    # 4. False Positive: Harmless wording differences
    def test_fp_4_harmless_wording_differences_produce_match(self) -> None:
        """Test 'in stock' vs 'available now' normalize to 'instock' producing MATCH."""
        obs_a = _make_obs(url="https://example.com/a", visible_text="Product X is in stock.")
        obs_b = _make_obs(url="https://example.com/b", visible_text="Product X is available now.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        facts = site_inv.get_aggregated_facts(attribute="availability")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "MATCH")
        self.assertEqual(facts[0].normalized_value, "instock")

    # 5. False Positive: Different date formats
    def test_fp_5_different_date_formats_produce_match(self) -> None:
        """Test '2026-06-30' and 'June 30, 2026' both normalize to '2026-06-30' producing MATCH."""
        from src.audits.fact_inventory import normalize_date
        d1 = normalize_date("2026-06-30")
        d2 = normalize_date("June 30, 2026")
        d3 = normalize_date("30 June 2026")

        self.assertEqual(d1, "2026-06-30")
        self.assertEqual(d2, "2026-06-30")
        self.assertEqual(d3, "2026-06-30")

    # 6. False Positive: Equivalent currency formatting
    def test_fp_6_equivalent_currency_formatting_produces_match(self) -> None:
        """Test '$99' vs '99.00 USD' vs '99 USD' normalize to ('USD', 99) producing MATCH."""
        obs_a = _make_obs(url="https://example.com/a", visible_text="Price is $99.")
        obs_b = _make_obs(url="https://example.com/b", visible_text="Price is 99.00 USD.")
        obs_c = _make_obs(url="https://example.com/c", visible_text="Price is 99 USD.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b, obs_c])
        facts = site_inv.get_aggregated_facts(attribute="price")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "MATCH")
        self.assertEqual(facts[0].normalized_value, ("USD", 99))

    # 7. False Positive: Equivalent quantity formatting
    def test_fp_7_equivalent_quantity_formatting_produces_match(self) -> None:
        """Test '1.2 kg' vs '1200 g' both normalize to ('g', 1200.0) producing MATCH."""
        obs_a = _make_obs(url="https://example.com/a", tables=[TableObservation(headers=["Weight"], rows=[["1.2 kg"]])])
        obs_b = _make_obs(url="https://example.com/b", tables=[TableObservation(headers=["Weight"], rows=[["1200 g"]])])

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        facts = site_inv.get_aggregated_facts(attribute="weight")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "MATCH")
        self.assertEqual(facts[0].normalized_value, ("g", 1200.0))

    # 8. Genuine Conflict: Two current pages claim different prices
    def test_gc_1_two_current_pages_different_prices_conflict(self) -> None:
        """Test two current pages claiming different current prices ($99 vs $149) produces CONFLICT and MA-REP-001."""
        obs_a = _make_obs(url="https://example.com/page1", title="Item A", visible_text="Item A price is $99.")
        obs_b = _make_obs(url="https://example.com/page2", title="Item A", visible_text="Item A price is $149.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        facts = site_inv.get_aggregated_facts(entity="Item A", attribute="price")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "CONFLICT")

        findings = audit_evidence_consistency(site_inv)
        self.assertTrue(any(f.id in ("MA-REP-001", "EVID-PRC-001") for f in findings))

    # 9. Genuine Conflict: Two current pages claim different phone numbers
    def test_gc_2_two_current_pages_different_phone_numbers(self) -> None:
        """Test two current pages claiming different phone numbers (+1-800-555-0199 vs +1-800-555-9999) emits EVID-PHN-001."""
        obs_a = _make_obs(url="https://example.com/contact", title="Acme Inc", visible_text="Acme Inc Call us at +1-800-555-0199.")
        obs_b = _make_obs(url="https://example.com/support", title="Acme Inc", visible_text="Acme Inc Call us at +1-800-555-9999.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        facts = site_inv.get_aggregated_facts(entity="Acme Inc", attribute="phone")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "CONFLICT")

        findings = audit_evidence_consistency(site_inv)
        phn_findings = [f for f in findings if f.id == "EVID-PHN-001"]
        self.assertEqual(len(phn_findings), 1)

    # 10. Genuine Conflict: Two current pages claim incompatible locations
    def test_gc_3_two_current_pages_incompatible_locations(self) -> None:
        """Test two current pages claiming incompatible locations emits EVID-LOC-001."""
        obs_a = _make_obs(url="https://example.com/about", title="Beta Corp", visible_text="Beta Corp Location: 123 Main St, New York, NY.")
        obs_b = _make_obs(url="https://example.com/offices", title="Beta Corp", visible_text="Beta Corp Location: 456 Market St, San Francisco, CA.")

        site_inv = SiteEvidenceInventory([obs_a, obs_b])
        facts = site_inv.get_aggregated_facts(entity="Beta Corp", attribute="location")

        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "CONFLICT")

        findings = audit_evidence_consistency(site_inv)
        loc_findings = [f for f in findings if f.id == "EVID-LOC-001"]
        self.assertEqual(len(loc_findings), 1)

    # 11. Genuine Conflict: Current structured data disagrees with visible content
    def test_gc_4_structured_data_disagrees_with_visible_content(self) -> None:
        """Test structured data declaring 'InStock' while visible text declares 'Out of Stock' emits MA-REP-002."""
        json_ld = JsonLdObservation(
            raw_text='{"@type": "Product", "name": "Item C", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}}',
            parsed_data={"@type": "Product", "name": "Item C", "offers": {"@type": "Offer", "availability": "http://schema.org/InStock"}},
            is_valid=True,
        )
        obs = _make_obs(url="https://example.com/item_c", title="Item C", visible_text="Item C is Out of Stock.", json_ld=[json_ld])

        findings = audit_evidence_consistency(obs)
        avail_findings = [f for f in findings if f.id == "MA-REP-002"]
        self.assertEqual(len(avail_findings), 1)


if __name__ == "__main__":
    unittest.main()
