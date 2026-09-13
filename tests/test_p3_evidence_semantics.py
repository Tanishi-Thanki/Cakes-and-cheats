"""P3-3 regression tests: evidence semantics / false-positive hardening.

Contract tested here:
    1.  AMBIGUOUS / INSUFFICIENT_EVIDENCE / UNKNOWN evidence states are not defects.
    2.  Temporal states (HISTORICAL / EXPIRED / FUTURE) describe uncertainty,
        not defects — they never surface as findings.
    3.  Only a genuine value contradiction (CONFLICT) produces a finding.
    4.  Missing or weak signals (incomplete pages, formatting variants, close
        identity names) do not auto-violate.
    5.  A genuine site-wide contradiction is still detected end-to-end.
    6.  Entity identity (P3-2) and aggregate/reporting behaviour are unchanged.
"""

import unittest
from unittest.mock import MagicMock, patch

from src.audits.entity_identity import audit_entity_identity
from src.audits.fact_inventory import (
    AggregatedFact,
    EvidenceUnit,
    FactInventory,
    SiteEvidenceInventory,
    _is_genuine_identity_conflict,
    audit_evidence_consistency as fact_audit_evidence_consistency,
)
from src.audits.models import Finding
from src.audits.p2_audit import _fact_to_finding, audit_p2_evidence
from src.observation.observation import JsonLdObservation, PageObservation, PageSignals


def _make_obs(
    url: str,
    title: str = "Product X",
    visible_text: str = "",
    json_ld=None,
    is_success: bool = True,
) -> PageObservation:
    """Helper to create PageObservation objects for evidence-semantics tests."""
    return PageObservation(
        requested_url=url,
        final_url=url,
        status_code=200 if is_success else None,
        is_success=is_success,
        title=title,
        h1=[title] if title else [],
        h2=[],
        visible_text=visible_text,
        links=[],
        images=[],
        tables=[],
        json_ld=json_ld or [],
        signals=PageSignals(
            has_title=bool(title),
            has_meta_description=False,
            has_canonical=False,
            num_links=0,
            num_images=0,
            num_tables=0,
            num_headings=1 if title else 0,
        ),
    )


def _product_obs(url: str, name: str, price: int, visible_text: str | None = None):
    """Build a product page carrying a JSON-LD price plus optional visible price."""
    ent = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "offers": {"@type": "Offer", "price": price, "priceCurrency": "USD"},
    }
    block = JsonLdObservation(raw_text=str(ent), parsed_data=ent, is_valid=True)
    if visible_text is None:
        visible_text = f"{name} for ${price}."
    return _make_obs(url=url, title=name, visible_text=visible_text, json_ld=[block])


def _agg_fact(status: str, entity: str = "Acme", attribute: str = "price") -> AggregatedFact:
    """Build a minimal AggregatedFact carrying the given cross_page_status."""
    unit = EvidenceUnit(
        entity=entity,
        attribute=attribute,
        value="99",
        raw_value="99",
        source="https://example.com/a",
        source_type="visible_text",
        confidence="high",
    )
    return AggregatedFact(
        entity=entity,
        attribute=attribute,
        normalized_value="99",
        units=(unit,),
        sources=("https://example.com/a",),
        source_types=("visible_text",),
        cross_page_status=status,  # type: ignore[arg-type]
    )


def _ctx_for(observations: list[PageObservation]):
    """Build a mocked site-audit context around the given observations."""
    ctx = MagicMock()
    ctx.normalized_start_url = observations[0].requested_url
    ctx.observations = {obs.requested_url: obs for obs in observations}
    page_by_url = {}
    for obs in observations:
        record = MagicMock()
        record.observation = obs
        page_by_url[obs.requested_url] = record
    ctx.page_by_url = page_by_url
    return ctx


class TestP2EvidenceSemanticsGate(unittest.TestCase):
    """The p2 gate: only a genuine CONFLICT is ever converted into a finding."""

    def test_explicit_conflict_produces_finding(self) -> None:
        """A genuine value contradiction still maps to EVID-001 (severity high)."""
        fact = _agg_fact("CONFLICT")
        finding = _fact_to_finding(fact)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.id, "EVID-001")
        self.assertEqual(finding.details["cross_page_status"], "CONFLICT")

    def test_uncertainty_states_never_produce_findings(self) -> None:
        """AMBIGUOUS / INSUFFICIENT / temporal states are not defects."""
        for status in (
            "AMBIGUOUS",
            "INSUFFICIENT_EVIDENCE",
            "HISTORICAL",
            "EXPIRED",
            "FUTURE",
            "SINGLE_PAGE",
            "MATCH",
        ):
            self.assertIsNone(
                _fact_to_finding(_agg_fact(status)),
                f"status '{status}' must never be converted into a finding",
            )

    def test_none_value_evidence_is_agreement_not_finding(self) -> None:
        """None-valued evidence across pages compares as agreement, never a defect."""
        inventory = FactInventory()
        for url in ("https://example.com/a", "https://example.com/b"):
            inventory.add_unit(
                EvidenceUnit(
                    entity="Acme",
                    attribute="price",
                    value=None,
                    raw_value="$?",
                    source=url,
                    source_type="visible_text",
                    confidence="low",
                )
            )
        facts = SiteEvidenceInventory.from_fact_inventory(inventory).facts_across_pages
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0].cross_page_status, "MATCH")
        self.assertIsNone(_fact_to_finding(facts[0]))

    def test_insufficient_evidence_state_is_not_a_finding(self) -> None:
        """A single source is INSUFFICIENT at the fact level, never a defect."""
        inventory = FactInventory()
        inventory.add_unit(
            EvidenceUnit(
                entity="Acme",
                attribute="price",
                value=("USD", 999),
                raw_value="$999",
                source="https://example.com/a",
                source_type="json_ld",
                confidence="high",
            )
        )
        state, _ = inventory.evaluate_consistency("Acme", "price")
        self.assertEqual(state, "INSUFFICIENT_EVIDENCE")
        facts = SiteEvidenceInventory.from_fact_inventory(inventory).facts_across_pages
        self.assertEqual(facts[0].cross_page_status, "SINGLE_PAGE")
        self.assertIsNone(_fact_to_finding(facts[0]))

    def test_unknown_temporal_evidence_matches_without_finding(self) -> None:
        """UNKNOWN-timestamp units still compare values; agreement is not a defect."""
        inventory = FactInventory()
        for url in ("https://example.com/a", "https://example.com/b"):
            inventory.add_unit(
                EvidenceUnit(
                    entity="Acme",
                    attribute="price",
                    value=("USD", 999),
                    raw_value="$999",
                    source=url,
                    source_type="visible_text",
                    confidence="high",
                    temporal_qualifier="UNKNOWN",
                )
            )
        facts = SiteEvidenceInventory.from_fact_inventory(inventory).facts_across_pages
        self.assertEqual(facts[0].cross_page_status, "MATCH")
        self.assertIsNone(_fact_to_finding(facts[0]))

    def test_csv_style_formatting_variant_is_not_a_conflict(self) -> None:
        """'$999' vs json price 999 is a formatting variant, not a contradiction."""
        page = _product_obs(
            "https://example.com/a",
            "Acme Widget",
            999,
            visible_text="Acme Widget for $999.",
        )
        site = SiteEvidenceInventory([page])
        facts = site.facts_across_pages
        self.assertEqual(facts[0].cross_page_status, "SINGLE_PAGE")
        self.assertEqual(audit_p2_evidence([page]), [])


class TestP2EvidenceAuditSemantics(unittest.TestCase):
    """Multi-page aggregation: genuine contradictions survive, uncertainty never does."""

    def test_consistent_multi_page_site_has_no_findings(self) -> None:
        """Two pages agreeing on price across JSON-LD and visible text: no findings."""
        obs_a = _product_obs("https://example.com/a", "Acme Widget", 999)
        obs_b = _product_obs("https://example.com/b", "Acme Widget", 999)
        self.assertEqual(audit_p2_evidence([obs_a, obs_b]), [])

    def test_genuine_multi_page_conflict_still_detected(self) -> None:
        """A real price contradiction across pages still yields EVID-001."""
        obs_a = _product_obs("https://example.com/a", "Acme Widget", 999)
        obs_b = _product_obs("https://example.com/b", "Acme Widget", 1199)
        findings = audit_p2_evidence([obs_a, obs_b])
        self.assertEqual([f.id for f in findings], ["EVID-001"])
        for f in findings:
            self.assertEqual(f.priority, 2)
            self.assertEqual(f.severity, "high")
            self.assertEqual(f.confidence, "high")

    def test_cross_page_price_difference_across_years_is_not_a_defect(self) -> None:
        """Differently-priced pages from different years classify HISTORICAL, no finding."""
        obs_a = _make_obs(
            "https://example.com/a",
            title="Acme Widget",
            visible_text="Acme Widget for $999, 2023 model.",
        )
        obs_b = _make_obs(
            "https://example.com/b",
            title="Acme Widget",
            visible_text="Acme Widget for $1199, 2026 model.",
        )
        facts = SiteEvidenceInventory([obs_a, obs_b]).facts_across_pages
        price_facts = [f for f in facts if f.attribute == "price"]
        self.assertTrue(price_facts, "expected a price fact to be aggregated")
        self.assertEqual(price_facts[0].cross_page_status, "HISTORICAL")
        self.assertEqual(audit_p2_evidence([obs_a, obs_b]), [])

    def test_one_incomplete_page_does_not_create_site_wide_defect(self) -> None:
        """Healthy pages + one page with no price evidence => zero findings."""
        obs_a = _product_obs("https://example.com/a", "Acme Widget", 999)
        obs_c = _product_obs("https://example.com/c", "Acme Widget", 999)
        incomplete = _make_obs(
            "https://example.com/b",
            title="Acme Widget",
            visible_text="The widget line has been retired.",
        )
        self.assertEqual(audit_p2_evidence([obs_a, obs_c]), [])
        self.assertEqual(audit_p2_evidence([obs_a, incomplete, obs_c]), [])

    def test_price_range_and_prefix_do_not_auto_violate(self) -> None:
        """Ranges/prefix prices are filtered from exact comparison and never conflict."""
        obs_a = _make_obs(
            "https://example.com/a",
            title="Acme Widget",
            visible_text="Acme Widget from $999.",
        )
        obs_b = _make_obs(
            "https://example.com/b",
            title="Acme Widget",
            visible_text="Acme Widget for $999.",
        )
        self.assertEqual(audit_p2_evidence([obs_a, obs_b]), [])


class TestWeakIdentitySignals(unittest.TestCase):
    """EVID-ENT-001 boundary: close identity variants never violate; real ones do."""

    def _obs_with_names(self, names: list[str]) -> PageObservation:
        blocks = []
        for i, name in enumerate(names):
            ent = {"@context": "https://schema.org", "@type": "Organization", "name": name}
            blocks.append(
                JsonLdObservation(raw_text=str(ent), parsed_data=ent, is_valid=True)
            )
        return _make_obs("https://example.com/about", title="Acme", json_ld=blocks)

    def test_close_name_variants_are_not_a_conflict(self) -> None:
        """'Acme Running Shoes' vs 'Acme' is a weak signal, not a contradiction."""
        self.assertFalse(
            _is_genuine_identity_conflict("Acme Running Shoes", "Acme", domain="example.com")
        )
        obs = self._obs_with_names(["Acme Running Shoes", "Acme"])
        ids = [f.id for f in fact_audit_evidence_consistency(obs)]
        self.assertNotIn("EVID-ENT-001", ids)

    def test_genuinely_distinct_names_still_conflict(self) -> None:
        """Clearly different names on the same entity type remain a contradiction."""
        self.assertTrue(
            _is_genuine_identity_conflict("Acme", "Globex Technologies", domain="example.com")
        )
        obs = self._obs_with_names(["Acme", "Globex Technologies"])
        ids = [f.id for f in fact_audit_evidence_consistency(obs)]
        self.assertIn("EVID-ENT-001", ids)


class TestEntityIdentityUnchanged(unittest.TestCase):
    """P3-2 contract is preserved: MATCH/INSUFFICIENT -> none, AMBIGUOUS -> ENT-ID-002."""

    def _org_obs(self, url: str, name: str, phone: str | None = None) -> PageObservation:
        ent: dict = {"@context": "https://schema.org", "@type": "Organization", "name": name}
        if phone is not None:
            ent["telephone"] = phone
        block = JsonLdObservation(raw_text=str(ent), parsed_data=ent, is_valid=True)
        return _make_obs(url=url, title=name, visible_text="", json_ld=[block])

    def test_matching_pages_produce_no_identity_finding(self) -> None:
        obs_a = self._org_obs("https://example.com/a", "Acme", "555-0100")
        obs_b = self._org_obs("https://example.com/b", "Acme", "555-0100")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_similar_names_different_phone_remain_ambiguous_not_conflict(self) -> None:
        obs_a = self._org_obs("https://example.com/a", "Acme", "555-0100")
        obs_b = self._org_obs("https://example.com/b", "Acme", "555-0199")
        ids = [f.id for f in audit_entity_identity([obs_a, obs_b])]
        self.assertEqual(ids, ["ENT-ID-002"])

    def test_incomplete_page_does_not_elevate_identity_to_conflict(self) -> None:
        obs_a = self._org_obs("https://example.com/a", "Acme", "555-0100")
        empty = _make_obs("https://example.com/b", title="", visible_text="nothing here")
        self.assertEqual(audit_entity_identity([obs_a, empty]), [])


class TestEvidenceSemanticsIntegration(unittest.TestCase):
    """run_audit runs p2 + entity identity over the full collection; neither
    converts uncertainty into defects, and aggregation/reporting preserves only
    genuine findings."""

    def _run_with(self, observations: list[PageObservation]):
        from src.orchestrator import AuditOptions, run_audit, run_audit_full

        with (
            patch("src.orchestrator.build_site_audit_context") as mock_build,
            patch("src.orchestrator.crawl_site") as mock_crawl,
        ):
            mock_crawl.return_value = MagicMock(is_success=True)
            mock_build.return_value = _ctx_for(observations)
            options = AuditOptions(audits=(), check_robots=False)
            result = run_audit("https://example.com/", options=options)
            _, report = run_audit_full("https://example.com/", options=options)
        return result, report

    def test_consistent_metadata_light_site_has_zero_findings(self) -> None:
        """Missing optional metadata (description/canonical) creates no defect."""
        obs_a = _make_obs(
            "https://example.com/a",
            title="About Acme",
            visible_text="Acme has produced widgets since 1999 for customers everywhere.",
        )
        obs_b = _make_obs(
            "https://example.com/b",
            title="About Acme",
            visible_text="Acme has produced widgets since 1999 for customers everywhere.",
        )
        result, report = self._run_with([obs_a, obs_b])
        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])
        self.assertEqual(report.as_final_dict()["summary"]["total_findings"], 0)

    def test_one_incomplete_page_does_not_poison_healthy_site(self) -> None:
        obs_a = _product_obs("https://example.com/a", "Acme Widget", 999)
        obs_c = _product_obs("https://example.com/c", "Acme Widget", 999)
        incomplete = _make_obs("https://example.com/b", title="", visible_text="plain")
        result, _ = self._run_with([obs_a, incomplete, obs_c])
        self.assertTrue(result.is_success)
        self.assertEqual(result.findings, [])

    def test_genuine_site_wide_conflict_reaches_report(self) -> None:
        """A real contradiction still flows through run_audit -> report as EVID-001."""
        obs_a = _product_obs("https://example.com/a", "Acme Widget", 999)
        obs_b = _product_obs("https://example.com/b", "Acme Widget", 1199)
        result, report = self._run_with([obs_a, obs_b])
        self.assertTrue(result.is_success)
        self.assertEqual([f.id for f in result.findings], ["EVID-001"])
        self.assertEqual(report.as_final_dict()["summary"]["total_findings"], 1)


if __name__ == "__main__":
    unittest.main()