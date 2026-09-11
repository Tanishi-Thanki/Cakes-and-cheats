"""Unit tests for the Evidence & Consistency layer.

Covers EvidenceUnit provenance, FactInventory grouping/merging, value
normalization, consistency classification, and evidence-backed finding
generation (EVID-* findings).
"""

import unittest

from src.audits.evidence_consistency import (
    EvidenceUnit,
    FactInventory,
    build_fact_inventory,
    classify_consistency,
    classify_inventory,
    audit_evidence_consistency,
    normalize_value,
)
from src.audits.models import Finding
from src.crawler.crawler import FetchedPage
from src.observation.observation import observe_html, observe_page

ENTITY = "Acme Running Shoes"


def _unit(entity, attribute, value, source, source_type="visible_text", **kw):
    return EvidenceUnit(
        entity=entity,
        attribute=attribute,
        value=value,
        source=source,
        source_type=source_type,
        **kw,
    )


def _merge(*inventories: FactInventory) -> FactInventory:
    merged = FactInventory()
    for inv in inventories:
        merged.merge(inv)
    return merged


def _product_page(price, url, name=ENTITY, *, availability=None, extra_json=None, body_extra=""):
    offer = {"price": price, "priceCurrency": "USD"}
    if availability:
        offer["availability"] = availability
    entity_json = {"@context": "https://schema.org", "@type": "Product", "name": name, "offers": offer}
    if extra_json:
        entity_json.update(extra_json)
    return observe_html(
        f"""<!DOCTYPE html>
        <html>
        <head><title>{name}</title>
        <script type="application/ld+json">{__import__("json").dumps(entity_json)}</script>
        </head>
        <body><h1>{name}</h1><p>{price}</p>{body_extra}</body>
        </html>""",
        requested_url=url,
        final_url=url,
    )


class TestEvidenceConsistencyAudit(unittest.TestCase):
    """Test suite for audit_evidence_consistency / consistency classification."""

    def test_01_matching_price_visible_and_jsonld_no_finding(self) -> None:
        """Test 1: $999 visible + 999 JSON-LD agree -> MATCH, no finding."""
        obs = _product_page("$999", "https://site.test/p")
        findings = audit_evidence_consistency(obs)
        self.assertEqual([f.id for f in findings], [])

        price = [r for r in classify_inventory(build_fact_inventory(obs)) if r.attribute == "price"]
        self.assertEqual(len(price), 1)
        self.assertEqual(price[0].classification, "MATCH")

    def test_02_conflicting_price_yields_finding_with_provenance(self) -> None:
        """Test 2: $999 visible vs 949 JSON-LD -> CONFLICT -> EVID-PRC-001."""
        obs = _product_page("$949", "https://site.test/p", name=ENTITY)
        obs2 = observe_html(
            """<!DOCTYPE html>
            <html><head><title>Acme Running Shoes</title>
            <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes","offers":{"price":"949","priceCurrency":"USD"}}</script>
            </head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>""",
            requested_url="https://site.test/p",
            final_url="https://site.test/p",
        )
        findings = audit_evidence_consistency(obs2)
        price_findings = [f for f in findings if f.id == "EVID-PRC-001"]
        self.assertEqual(len(price_findings), 1)
        f = price_findings[0]
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.priority, 1)
        self.assertEqual(f.source_audit, "evidence_consistency")
        self.assertIn("999", f.evidence)
        self.assertIn("949", f.evidence)
        units = f.details["evidence_units"]
        self.assertEqual(len(units), 2)
        self.assertEqual({u["source_type"] for u in units}, {"visible_text", "json_ld"})
        self.assertTrue(all(u["source"] == "https://site.test/p" for u in units))
        self.assertTrue(all(u["entity"] == ENTITY and u["attribute"] == "price" for u in units))
        self.assertIsInstance(f.suggested_action.summary, str)
        self.assertTrue(f.suggested_action.summary)

    def test_03_price_format_normalization_dollar_against_code(self) -> None:
        """Test 3: '$999' and '999 USD' normalize to the same comparable value."""
        units = [
            _unit(ENTITY, "price", "$999", "https://site.test/visible", "visible_text", raw_value="$999"),
            _unit(ENTITY, "price", "999 USD", "https://site.test/structured", "json_ld", raw_value="999 USD"),
        ]
        res = classify_consistency(units, entity=ENTITY, attribute="price")
        self.assertEqual(res.classification, "MATCH")
        self.assertEqual(normalize_value("price", "$999").comparable, ("price", "USD", 99900))
        self.assertEqual(normalize_value("price", "999 USD").comparable, ("price", "USD", 99900))

    def test_04_dated_older_price_is_historical_not_conflict(self) -> None:
        """Test 4: explicitly dated older 1,199 vs newer 999 -> HISTORICAL, not CONFLICT."""
        old_json = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": ENTITY,
            "offers": {
                "price": "1199",
                "priceCurrency": "USD",
                "priceValidUntil": "2025-12-31",
            },
        }
        old_obs = observe_html(
            f"""<!DOCTYPE html><html><head><title>{ENTITY}</title>
            <script type="application/ld+json">{__import__("json").dumps(old_json)}</script>
            </head><body><h1>{ENTITY}</h1><p>$1,199</p></body></html>""",
            requested_url="https://site.test/products/old",
            final_url="https://site.test/products/old",
        )
        cur_obs = _product_page("$999", "https://site.test/products/current")
        merged = _merge(build_fact_inventory(old_obs), build_fact_inventory(cur_obs))

        prices = [r for r in classify_inventory(merged) if r.attribute == "price"]
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].classification, "HISTORICAL")
        self.assertNotEqual(prices[0].classification, "CONFLICT")

    def test_05_different_entities_never_conflict(self) -> None:
        """Test 5: price differences on separate entities are not compared."""
        inv = FactInventory()
        inv.add_unit(_unit("Entity A", "price", "$999", "https://site.test/a", "visible_text"))
        inv.add_unit(_unit("Entity A", "price", "999 USD", "https://site.test/a", "json_ld"))
        inv.add_unit(_unit("Entity B", "price", "$799", "https://site.test/b", "visible_text"))
        inv.add_unit(_unit("Entity B", "price", "799 USD", "https://site.test/b", "json_ld"))
        results = classify_inventory(inv)
        self.assertEqual({r.attribute for r in results}, {"price"})
        self.assertTrue(all(r.classification != "CONFLICT" for r in results))
        self.assertEqual({r.entity for r in results}, {"Entity A", "Entity B"})

    def test_06_ambiguous_entity_is_not_a_conflict(self) -> None:
        """Test 6: unclear/ambiguous identity resolves to AMBIGUOUS, never CONFLICT."""
        res = classify_consistency(
            [
                _unit("Acme Co", "name", "Acme Co", "https://site.test/p", "heading"),
                _unit("Acme Co", "name", "Beta Ltd", "https://site.test/p", "json_ld"),
            ],
            entity="Acme Co",
            attribute="name",
        )
        self.assertEqual(res.classification, "AMBIGUOUS")

        html = """<!DOCTYPE html><html><head><title>Acme Co</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Beta Ltd"}</script>
        </head><body><h1>Acme Co</h1><p>Everyday footwear.</p></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        self.assertEqual(audit_evidence_consistency(obs), [])

    def test_07_single_source_is_insufficient_not_conflict(self) -> None:
        """Test 7: one evidence unit -> INSUFFICIENT_EVIDENCE, no finding."""
        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes"}</script>
        </head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        self.assertEqual(audit_evidence_consistency(obs), [])

        inv = build_fact_inventory(obs)
        price = [r for r in classify_inventory(inv) if r.attribute == "price"]
        self.assertEqual(len(price), 1)
        self.assertEqual(price[0].classification, "INSUFFICIENT_EVIDENCE")

    def test_08_three_source_conflict_preserves_all_provenance(self) -> None:
        """Test 8: three internal pages disagreeing -> CONFLICT with all provenance."""
        url_products = "https://site.test/products/x"
        url_compare = "https://site.test/compare"
        url_pricing = "https://site.test/pricing"

        inv = _merge(
            build_fact_inventory(_product_page("$999", url_products)),
            build_fact_inventory(_product_page("$999", url_compare)),
            build_fact_inventory(_product_page("$949", url_pricing)),
        )
        prices = [r for r in classify_inventory(inv) if r.attribute == "price"]
        self.assertEqual(len(prices), 1)
        res = prices[0]
        self.assertEqual(res.classification, "CONFLICT")
        sources = sorted({u.source for u in res.units})
        self.assertEqual(sources, sorted([url_products, url_compare, url_pricing]))
        for u in res.units:
            self.assertEqual(u.entity, ENTITY)
            self.assertEqual(u.attribute, "price")
            self.assertTrue(u.raw_value)
            self.assertIn(u.source_type, {"visible_text", "json_ld"})

    def test_09_sale_price_variant_is_not_a_conflict(self) -> None:
        """Test 9: original + sale prices are legitimate variants, not CONFLICT."""
        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes","offers":{"price":"999","priceCurrency":"USD"}}</script>
        </head><body><h1>Acme Running Shoes</h1><p>Was $1,199</p><p>Sale $999</p></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        self.assertTrue(all(f.id != "EVID-PRC-001" for f in audit_evidence_consistency(obs)))

    def test_10_availability_match_no_finding(self) -> None:
        """Test 10: 'In Stock' visible vs InStock JSON-LD -> MATCH, no finding."""
        obs = _product_page(
            "$999",
            "https://site.test/p",
            availability="https://schema.org/InStock",
            body_extra="<p>In Stock</p>",
        )
        findings = audit_evidence_consistency(obs)
        self.assertTrue(all(f.id != "EVID-AVT-001" for f in findings))

        avail = [r for r in classify_inventory(build_fact_inventory(obs)) if r.attribute == "availability"]
        self.assertEqual(len(avail), 1)
        self.assertEqual(avail[0].classification, "MATCH")

    def test_11_availability_conflict_yields_finding(self) -> None:
        """Test 11: 'Out of Stock' visible vs InStock JSON-LD -> EVID-AVT-001."""
        obs = _product_page(
            "$999",
            "https://site.test/p",
            availability="https://schema.org/InStock",
            body_extra="<p>Out of Stock</p>",
        )
        findings = [f for f in audit_evidence_consistency(obs) if f.id == "EVID-AVT-001"]
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.priority, 1)
        unit_types = {u["source_type"] for u in f.details["evidence_units"]}
        self.assertEqual(unit_types, {"visible_text", "json_ld"})

    def test_12_unit_normalization_mass_equivalence(self) -> None:
        """Test 12: '1.2 kg' and '1200 g' are the same quantity -> MATCH."""
        units = [
            _unit(ENTITY, "weight", "1.2 kg", "https://site.test/specs", "table"),
            _unit(ENTITY, "weight", "1200 g", "https://site.test/structured", "json_ld"),
        ]
        res = classify_consistency(units, entity=ENTITY, attribute="weight")
        self.assertEqual(res.classification, "MATCH")

        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes","additionalProperty":[{"name":"Weight","value":"1200 g"}]}</script>
        </head><body><h1>Acme Running Shoes</h1><table><tr><td>Weight</td><td>1.2 kg</td></tr></table></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        weights = [r for r in classify_inventory(build_fact_inventory(obs)) if r.attribute == "weight"]
        self.assertEqual(len(weights), 1)
        self.assertEqual(weights[0].classification, "MATCH")

    def test_13_provenance_fields_preserved_in_finding(self) -> None:
        """Test 13: entity/attribute/raw_value/source/source_type survive to the finding."""
        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes","offers":{"price":"949","priceCurrency":"USD"}}</script>
        </head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>"""
        obs = observe_html(
            html,
            requested_url="https://site.test/p",
            final_url="https://site.test/p",
        )
        findings = audit_evidence_consistency(obs)
        self.assertEqual(len(findings), 1)
        units = findings[0].details["evidence_units"]
        self.assertEqual(len(units), 2)
        for u in units:
            self.assertEqual(u["entity"], ENTITY)
            self.assertEqual(u["attribute"], "price")
            self.assertIn(u["raw_value"], ("$999", "949"))
            self.assertEqual(u["source"], "https://site.test/p")
            self.assertIn(u["source_type"], ("visible_text", "json_ld"))
            self.assertIn(u["confidence"], ("high", "medium"))
            self.assertIn("date", u)
            self.assertIn("qualifier", u)

    def test_14_no_dates_never_historical(self) -> None:
        """Test 14: without explicit dates a difference is CONFLICT, never HISTORICAL."""
        no_date_json = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": ENTITY,
            "offers": {"price": "1199", "priceCurrency": "USD"},
        }
        p1 = observe_html(
            f"""<!DOCTYPE html><html><head><title>{ENTITY}</title>
            <script type="application/ld+json">{__import__("json").dumps(no_date_json)}</script>
            </head><body><h1>{ENTITY}</h1><p>$1,199</p></body></html>""",
            requested_url="https://site.test/a",
            final_url="https://site.test/a",
        )
        p2 = _product_page("$949", "https://site.test/b")
        merged = _merge(build_fact_inventory(p1), build_fact_inventory(p2))
        prices = [r for r in classify_inventory(merged) if r.attribute == "price"]
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].classification, "CONFLICT")
        self.assertFalse(any(r.classification == "HISTORICAL" for r in classify_inventory(merged)))

    def test_15_malformed_json_ld_fails_safe(self) -> None:
        """Test 15: broken JSON-LD is ignored without raising or fabricating evidence."""
        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{ not valid json </script>
        </head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        self.assertEqual(audit_evidence_consistency(obs), [])

    def test_16_empty_or_failed_page_yields_no_findings(self) -> None:
        """Test 16: empty and non-success pages return no findings."""
        failed = observe_page(
            FetchedPage(
                requested_url="https://site.test/p",
                final_url="https://site.test/p",
                status_code=500,
                content_type="text/html",
                html_content="<html><body>oops</body></html>",
                is_success=False,
                error_message="boom",
            )
        )
        self.assertFalse(failed.is_success)
        self.assertEqual(audit_evidence_consistency(failed), [])

        empty = observe_html("<html><head></head><body></body></html>", requested_url="https://site.test/empty")
        self.assertEqual(audit_evidence_consistency(empty), [])

    def test_17_neighboring_audits_are_unaffected(self) -> None:
        """Test 17: neighboring audit contracts still import and run cleanly."""
        from src.audits.freshness_corroboration import audit_freshness_corroboration
        from src.audits.machine_answerability import audit_machine_answerability
        from src.audits.task_readiness import audit_task_readiness

        html = """<!DOCTYPE html><html><head><title>Acme Running Shoes</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes","offers":{"price":"999","priceCurrency":"USD"}}</script>
        </head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>"""
        obs = observe_html(html, requested_url="https://site.test/p")
        for audit in (
            audit_evidence_consistency,
            audit_freshness_corroboration,
            audit_machine_answerability,
            audit_task_readiness,
        ):
            result = audit(obs)
            self.assertIsInstance(result, list)
            self.assertTrue(all(isinstance(f, Finding) for f in result))


if __name__ == "__main__":
    unittest.main()