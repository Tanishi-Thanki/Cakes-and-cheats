"""P2 regression tests: price/identity/entity evidence gating.

These tests lock in the false-positive/recall contract for the evidence layer:

    - evidence_consistency.audit_evidence_consistency (per-page audit)
    - p2_audit.audit_p2_evidence (site-level, always-on audit)

Scenarios (10) mirror the P2 requirement:
    1. Multiple products on one page must not produce a price contradiction.
    2. Genuine same-entity price conflicts must still produce findings.
    3. Monthly vs annual / per-unit prices are not one price.
    4. Original vs sale (promotional) prices are legitimate variants.
    5. Prices in different currencies are never blindly compared.
    6. title != h1 must not be an entity identity conflict.
    7. Genuine strong (structured) identity conflicts still produce findings.
    8. Unrelated numbers (percentages, years, counts) are not prices.
    9. Multiple pricing cards must not produce cross-card contradictions.
    10. A price without entity context is never invented into a contradiction.

Fixtures are realistic HTML observed through the real observation layer.
"""

from __future__ import annotations

import unittest

from src.audits.evidence_consistency import audit_evidence_consistency
from src.audits.fact_inventory import extract_fact_inventory
from src.audits.p2_audit import audit_p2_evidence
from src.observation.observation import observe_html

PRICE_FINDINGS = {"EVID-PRC-001", "EVID-001", "EVID-002", "EVID-003", "EVID-004", "EVID-005", "EVID-006"}


def _ec(html: str) -> list:
    return audit_evidence_consistency(observe_html(html))


def _p2(*htmls: str) -> list:
    obs = [
        observe_html(
            html,
            requested_url=f"https://page-{i}.example.com/",
            final_url=f"https://page-{i}.example.com/",
        )
        for i, html in enumerate(htmls)
    ]
    return audit_p2_evidence(obs)


def _price_ids(findings: list) -> set[str]:
    return {f.id for f in findings if f.id in PRICE_FINDINGS}


class TestNoFalsePriceContradiction(unittest.TestCase):
    """Requirements 1, 3, 4, 5, 9: legitimate price variety must never be a contradiction."""

    def test_01_multiple_products_on_one_page(self) -> None:
        html = """
        <html><head><title>Acme Pricing</title></head><body>
        <h1>Acme Plans</h1>
        <div class="pricing-card"><h2>Starter</h2><span>$10</span></div>
        <div class="pricing-card"><h2>Pro</h2><span>$20</span></div>
        <div class="pricing-card"><h2>Enterprise</h2><span>$30</span></div>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())

    def test_03_monthly_vs_annual_on_same_page(self) -> None:
        html = """
        <html><head><title>Acme Pro Plan</title></head><body>
        <h1>Acme Pro Plan</h1><p>$10/month or $100/year.</p>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())

    def test_03b_monthly_vs_annual_across_pages(self) -> None:
        page_a = "<html><head><title>Acme Pro</title></head><body><h1>Acme Pro</h1><p>$10/month</p></body></html>"
        page_b = "<html><head><title>Acme Pro</title></head><body><h1>Acme Pro</h1><p>$100/year</p></body></html>"
        self.assertEqual(_price_ids(_p2(page_a, page_b)), set())

    def test_04_promotional_vs_original_price(self) -> None:
        html = """
        <html><head><title>Acme Gadget</title></head><body>
        <h1>Acme Gadget</h1><p>Original price $50. Now $30.</p>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())

    def test_05_different_currencies_not_blindly_compared(self) -> None:
        html = """
        <html><head><title>Global Store</title></head><body>
        <h1>Global Store</h1><p>US $10 or €10.</p>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())

    def test_09_multiple_pricing_cards_no_cross_card_conflict(self) -> None:
        html = """
        <html><head><title>Hosting Plans</title></head><body>
        <h1>Hosting Plans</h1>
        <div class="card"><h2>Basic Hosting</h2><p>$5/month</p></div>
        <div class="card"><h2>Pro Hosting</h2><p>$15/month</p></div>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())


class TestGenuinePriceConflictStillDetected(unittest.TestCase):
    """Requirement 2: recall for real same-entity conflicts must be preserved."""

    _CONFLICT = """
    <html>
    <head><title>Acme Running Shoes</title>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Acme Running Shoes",
         "offers":{"@type":"Offer","price":949,"priceCurrency":"USD"}}
        </script>
    </head>
    <body>
        <h1>Acme Running Shoes</h1>
        <p>Buy the Acme Running Shoes for $999 with free shipping.</p>
    </body>
    </html>
    """

    def test_02a_per_page_audit_detects_same_entity_conflict(self) -> None:
        findings = _ec(self._CONFLICT)
        self.assertEqual([f.id for f in findings], ["EVID-PRC-001"])

    def test_02b_site_audit_detects_same_entity_conflict_across_pages(self) -> None:
        page_a = "<html><head><title>Acme Running Shoes</title></head><body><h1>Acme Running Shoes</h1><p>$949</p></body></html>"
        page_b = "<html><head><title>Acme Running Shoes</title></head><body><h1>Acme Running Shoes</h1><p>$999</p></body></html>"
        findings = _p2(page_a, page_b)
        self.assertEqual([f.id for f in findings], ["EVID-001"])


class TestIdentityClassification(unittest.TestCase):
    """Requirements 6 & 7: weak claims never conflict; strong claims still do."""

    def test_06_title_vs_h1_is_not_an_identity_conflict(self) -> None:
        html = """
        <html><head>
        <title>GitHub. Where the world builds software.</title>
        </head><body>
        <h1>prasvi-brahmbhatt</h1>
        <p>Personal account and repositories.</p>
        </body></html>
        """
        findings = _ec(html)
        self.assertEqual([f.id for f in findings if f.id == "EVID-ENT-001"], [])

    def test_07_genuine_structured_identity_conflict_detected(self) -> None:
        html = """
        <html><head><title>Acme</title></head><body>
        <h1>Acme</h1>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"Acme Logistics Corp"}
        </script>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"Zeta Financial Services"}
        </script>
        </body></html>
        """
        findings = [f for f in _ec(html) if f.id == "EVID-ENT-001"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "medium")


class TestNumberContext(unittest.TestCase):
    """Requirement 8: only currency-prefixed numbers are prices."""

    def test_08_unrelated_numbers_are_not_prices(self) -> None:
        html = """
        <html><head><title>Acme About</title></head><body>
        <h1>Acme About</h1>
        <p>99% satisfaction rating. 13,000 happy customers. Serving customers since 2026.
        Plans start at $20/month.</p>
        </body></html>
        """
        obs = observe_html(html)
        inventory = extract_fact_inventory(obs)
        price_units = inventory.get_units(attribute="price")
        self.assertEqual(len(price_units), 1)
        self.assertEqual(price_units[0].value, ("USD", 20))
        self.assertEqual(price_units[0].billing_period, "month")
        self.assertEqual(_price_ids(audit_p2_evidence([obs])), set())
        self.assertEqual(_price_ids(_ec(html)), set())


class TestNoInventedEntityAssociation(unittest.TestCase):
    """Requirement 10: evidence outside a clear entity context is not attributed."""

    def test_10a_plain_price_without_entity_context(self) -> None:
        html = """
        <html><head><title>Acme Store</title></head><body>
        <h1>Acme Store</h1><p>Price: $20.</p>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set())
        self.assertEqual(_price_ids(_p2(html)), set())

    def test_10b_json_ld_entity_does_not_consume_other_visible_product_price(self) -> None:
        html = """
        <html><head><title>Product A - Acme Store</title></head><body>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Product A",
         "offers":{"price":10,"priceCurrency":"USD"}}
        </script>
        <h1>Product B</h1>
        <p>Product B is available for $20.</p>
        </body></html>
        """
        self.assertEqual(_price_ids(_ec(html)), set(), "must not compare Product A price with Product B price")
        self.assertEqual(_price_ids(_p2(html)), set())


if __name__ == "__main__":
    unittest.main()