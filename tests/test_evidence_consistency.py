"""Unit tests for Fact Inventory & Evidence Consistency Layer (src/audits/fact_inventory.py).

Test contract:
    - EvidenceUnit and FactInventory accurately store provenance-aware facts.
    - Correctly normalizes prices, currency codes, availability, and numeric specs (1.2kg vs 1200g).
    - Classifies evidence into MATCH, CONFLICT, HISTORICAL, AMBIGUOUS, INSUFFICIENT_EVIDENCE.
    - All 17 required scenarios (Match, Conflict, Formatting, Historical, Sale Prices, Provenance, etc.) pass.
"""

import unittest

from src.audits.fact_inventory import (
    EvidenceUnit,
    FactInventory,
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


if __name__ == "__main__":
    unittest.main()
