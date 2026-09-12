"""Unit tests for Provider-Neutral External Evidence Abstraction (Task 4).

Test contract:
  - ExternalFact and ExternalEvidence retain full provenance.
  - ExternalEvidenceClient degrades gracefully on all failure modes.
  - Budget limits are honoured: queries stop when budget is exhausted.
  - No provider configured → ExternalEvidence.not_configured() returned.
  - Malformed evidence from provider is absorbed, not propagated as exceptions.
  - source_domain deduplication tracks unique corroborating domains correctly.
  - external_fact_to_evidence_unit() bridges to EvidenceUnit with EXTERNAL classification.
  - All 7 required scenarios covered.
"""

from __future__ import annotations

import time
import unittest
from typing import Any, Optional

from src.audits.external_evidence import (
    BudgetTracker,
    ExternalEvidence,
    ExternalEvidenceBudget,
    ExternalEvidenceClient,
    ExternalEvidenceProvider,
    ExternalFact,
    _extract_domain,
    external_fact_to_evidence_unit,
)


# ---------------------------------------------------------------------------
# Fake / deterministic providers for testing
# ---------------------------------------------------------------------------


class _OkProvider(ExternalEvidenceProvider):
    """Provider that always returns a fixed list of ExternalFacts."""

    def __init__(self, facts: list[ExternalFact]) -> None:
        self._facts = facts

    @property
    def name(self) -> str:
        return "fake_ok_provider"

    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        if tracker.is_exhausted:
            return ExternalEvidence.from_error(
                query, self.name, "budget_exhausted", "Budget exhausted."
            )
        start = time.monotonic()
        facts = tuple(self._facts[: budget.max_results_per_query])
        elapsed = time.monotonic() - start
        tracker.record_query(elapsed)
        return ExternalEvidence(
            query=query,
            status="ok",
            facts=facts,
            provider_name=self.name,
            elapsed_seconds=elapsed,
        )


class _EmptyProvider(ExternalEvidenceProvider):
    """Provider that always returns an empty result."""

    @property
    def name(self) -> str:
        return "fake_empty_provider"

    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        start = time.monotonic()
        elapsed = time.monotonic() - start
        tracker.record_query(elapsed)
        return ExternalEvidence.insufficient(query, self.name)


class _TimeoutProvider(ExternalEvidenceProvider):
    """Provider that simulates a timeout by raising TimeoutError."""

    @property
    def name(self) -> str:
        return "fake_timeout_provider"

    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        raise TimeoutError("Connection timed out.")


class _ExceptionProvider(ExternalEvidenceProvider):
    """Provider that raises a generic unexpected exception."""

    @property
    def name(self) -> str:
        return "fake_exception_provider"

    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        raise RuntimeError("Unexpected provider crash.")


class _MalformedProvider(ExternalEvidenceProvider):
    """Provider that returns ExternalFacts with partial/empty values."""

    @property
    def name(self) -> str:
        return "fake_malformed_provider"

    def fetch_evidence(
        self,
        query: str,
        *,
        budget: ExternalEvidenceBudget,
        tracker: BudgetTracker,
        entity: Optional[str] = None,
        fields: Optional[list[str]] = None,
    ) -> ExternalEvidence:
        start = time.monotonic()
        # Return a fact with empty raw_value and None value — 'malformed'
        malformed_fact = ExternalFact.build(
            field="phone",
            value=None,
            raw_value="",
            source_url="https://malformed.example/page",
        )
        elapsed = time.monotonic() - start
        tracker.record_query(elapsed)
        return ExternalEvidence(
            query=query,
            status="ok",
            facts=(malformed_fact,),
            provider_name=self.name,
            elapsed_seconds=elapsed,
        )


# ---------------------------------------------------------------------------
# Helper factory
# ---------------------------------------------------------------------------


def _make_fact(
    field: str = "phone",
    value: Any = "8005550199",
    raw_value: str = "(800) 555-0199",
    source_url: str = "https://yelp.example/abc-dental",
    source_domain: Optional[str] = None,
) -> ExternalFact:
    return ExternalFact.build(
        field=field,
        value=value,
        raw_value=raw_value,
        source_url=source_url,
        source_domain=source_domain,
        extraction_method="fetch",
        source_type="external_page",
        confidence="medium",
    )


# ---------------------------------------------------------------------------
# Tests: ExternalFact
# ---------------------------------------------------------------------------


class TestExternalFact(unittest.TestCase):

    def test_build_auto_derives_source_domain(self) -> None:
        fact = ExternalFact.build(
            field="phone",
            value="8005550199",
            raw_value="(800) 555-0199",
            source_url="https://www.yelp.com/biz/abc-dental",
        )
        self.assertEqual(fact.source_domain, "yelp.com")

    def test_build_explicit_source_domain_overrides(self) -> None:
        fact = ExternalFact.build(
            field="location",
            value="new york ny",
            raw_value="New York, NY",
            source_url="https://maps.google.com/place/xyz",
            source_domain="custom-domain.example",
        )
        self.assertEqual(fact.source_domain, "custom-domain.example")

    def test_fact_is_frozen(self) -> None:
        fact = _make_fact()
        with self.assertRaises((AttributeError, TypeError)):
            fact.field = "location"  # type: ignore[misc]

    def test_fact_retains_all_provenance_fields(self) -> None:
        fact = ExternalFact.build(
            field="price",
            value=("USD", 99),
            raw_value="$99",
            source_url="https://external.example/product/x",
            extraction_method="structured_data",
            source_type="external_structured",
            confidence="high",
            timestamp="2026",
            entity="Product X",
            details={"provider": "fake_provider"},
        )
        self.assertEqual(fact.field, "price")
        self.assertEqual(fact.value, ("USD", 99))
        self.assertEqual(fact.raw_value, "$99")
        self.assertEqual(fact.source_url, "https://external.example/product/x")
        self.assertEqual(fact.source_domain, "external.example")
        self.assertEqual(fact.extraction_method, "structured_data")
        self.assertEqual(fact.source_type, "external_structured")
        self.assertEqual(fact.confidence, "high")
        self.assertEqual(fact.timestamp, "2026")
        self.assertEqual(fact.entity, "Product X")
        self.assertEqual(fact.details["provider"], "fake_provider")


# ---------------------------------------------------------------------------
# Tests: ExternalEvidence
# ---------------------------------------------------------------------------


class TestExternalEvidence(unittest.TestCase):

    def test_not_configured_returns_correct_status(self) -> None:
        ev = ExternalEvidence.not_configured("ABC Dental")
        self.assertEqual(ev.status, "provider_not_configured")
        self.assertEqual(ev.facts, ())
        self.assertFalse(ev.is_ok)

    def test_insufficient_returns_empty_facts(self) -> None:
        ev = ExternalEvidence.insufficient("ABC Dental", "fake_provider")
        self.assertEqual(ev.status, "empty")
        self.assertEqual(ev.facts, ())
        self.assertTrue(ev.is_empty)

    def test_from_error_preserves_detail(self) -> None:
        ev = ExternalEvidence.from_error(
            "ABC Dental", "test_provider", "timeout", "Connection timed out.", elapsed=5.0
        )
        self.assertEqual(ev.status, "timeout")
        self.assertIn("timed out", ev.error_detail)
        self.assertEqual(ev.elapsed_seconds, 5.0)

    def test_unique_domains_deduplicates_same_domain(self) -> None:
        """Two URLs from yelp.com should count as one corroborating domain."""
        f1 = _make_fact(source_url="https://yelp.com/biz/abc-1", source_domain="yelp.com")
        f2 = _make_fact(source_url="https://yelp.com/biz/abc-2", source_domain="yelp.com")
        f3 = _make_fact(source_url="https://yellowpages.com/abc", source_domain="yellowpages.com")
        ev = ExternalEvidence(
            query="ABC Dental",
            status="ok",
            facts=(f1, f2, f3),
            provider_name="test",
        )
        self.assertEqual(ev.unique_domains, frozenset({"yelp.com", "yellowpages.com"}))
        self.assertEqual(ev.corroborating_domain_count, 2)

    def test_corroborating_domain_count_single_domain(self) -> None:
        f1 = _make_fact(source_url="https://yelp.com/biz/abc-1", source_domain="yelp.com")
        f2 = _make_fact(source_url="https://yelp.com/biz/abc-2", source_domain="yelp.com")
        ev = ExternalEvidence(
            query="ABC Dental",
            status="ok",
            facts=(f1, f2),
            provider_name="test",
        )
        self.assertEqual(ev.corroborating_domain_count, 1)

    def test_is_ok_true_for_ok_status(self) -> None:
        ev = ExternalEvidence(
            query="ABC Dental", status="ok", facts=(_make_fact(),), provider_name="test"
        )
        self.assertTrue(ev.is_ok)

    def test_is_ok_false_for_error_status(self) -> None:
        for bad_status in ("timeout", "empty", "network_error", "provider_not_configured"):
            ev = ExternalEvidence(
                query="q", status=bad_status, facts=(), provider_name="test"  # type: ignore[arg-type]
            )
            self.assertFalse(ev.is_ok, f"Expected is_ok=False for status={bad_status}")


# ---------------------------------------------------------------------------
# Tests: ExternalEvidenceBudget and BudgetTracker
# ---------------------------------------------------------------------------


class TestBudget(unittest.TestCase):

    def test_default_budget_is_valid(self) -> None:
        b = ExternalEvidenceBudget()
        self.assertTrue(b.is_valid())

    def test_zero_max_queries_is_valid(self) -> None:
        b = ExternalEvidenceBudget(max_queries=0)
        self.assertTrue(b.is_valid())

    def test_negative_timeout_is_invalid(self) -> None:
        b = ExternalEvidenceBudget(timeout_seconds=-1.0)
        self.assertFalse(b.is_valid())

    def test_tracker_starts_unexhausted(self) -> None:
        t = BudgetTracker(ExternalEvidenceBudget(max_queries=5))
        self.assertFalse(t.is_exhausted)
        self.assertEqual(t.queries_remaining, 5)

    def test_tracker_exhausts_after_max_queries(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=2)
        t = BudgetTracker(budget)
        t.record_query(0.1)
        t.record_query(0.1)
        self.assertTrue(t.is_exhausted)
        self.assertEqual(t.queries_remaining, 0)

    def test_tracker_exhausts_on_time_budget(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=100, total_time_budget_seconds=1.0)
        t = BudgetTracker(budget)
        t.record_query(1.5)  # exceeds total_time_budget_seconds
        self.assertTrue(t.is_exhausted)

    def test_tracker_snapshot(self) -> None:
        t = BudgetTracker(ExternalEvidenceBudget(max_queries=5))
        t.record_query(0.25)
        snap = t.snapshot()
        self.assertEqual(snap["queries_used"], 1)
        self.assertEqual(snap["queries_remaining"], 4)
        self.assertAlmostEqual(snap["elapsed_seconds"], 0.25, places=2)


# ---------------------------------------------------------------------------
# Tests: ExternalEvidenceClient — Scenario 1: Successful result
# ---------------------------------------------------------------------------


class TestClientSuccessfulResult(unittest.TestCase):
    """Scenario 1: Provider returns facts successfully."""

    def setUp(self) -> None:
        self._fact = _make_fact(
            field="phone",
            value="8005550199",
            source_url="https://yelp.com/abc-dental",
        )
        self._client = ExternalEvidenceClient(
            provider=_OkProvider([self._fact]),
            budget=ExternalEvidenceBudget(max_queries=5),
        )

    def test_returns_ok_status(self) -> None:
        ev = self._client.fetch_evidence("ABC Dental")
        self.assertTrue(ev.is_ok)
        self.assertEqual(ev.status, "ok")

    def test_returns_expected_facts(self) -> None:
        ev = self._client.fetch_evidence("ABC Dental")
        self.assertEqual(len(ev.facts), 1)
        self.assertEqual(ev.facts[0].value, "8005550199")

    def test_provenance_retained(self) -> None:
        ev = self._client.fetch_evidence("ABC Dental")
        f = ev.facts[0]
        self.assertEqual(f.source_url, "https://yelp.com/abc-dental")
        self.assertEqual(f.source_domain, "yelp.com")


# ---------------------------------------------------------------------------
# Tests: Scenario 2: Empty result
# ---------------------------------------------------------------------------


class TestClientEmptyResult(unittest.TestCase):
    """Scenario 2: Provider returns empty result."""

    def test_empty_result_is_not_ok_and_has_no_facts(self) -> None:
        client = ExternalEvidenceClient(provider=_EmptyProvider())
        ev = client.fetch_evidence("Unknown Entity")
        self.assertFalse(ev.is_ok)
        self.assertEqual(len(ev.facts), 0)
        self.assertEqual(ev.status, "empty")

    def test_empty_result_is_insufficient_evidence_not_failure(self) -> None:
        """Absent external result means INSUFFICIENT_EVIDENCE, not 'bad website'."""
        client = ExternalEvidenceClient(provider=_EmptyProvider())
        ev = client.fetch_evidence("Some Entity")
        # The audit layer must treat this as INSUFFICIENT_EVIDENCE
        self.assertTrue(ev.is_empty)
        self.assertNotEqual(ev.status, "provider_unavailable")  # not a crash


# ---------------------------------------------------------------------------
# Tests: Scenario 3: Timeout
# ---------------------------------------------------------------------------


class TestClientTimeout(unittest.TestCase):
    """Scenario 3: Provider raises TimeoutError."""

    def test_timeout_returns_provider_unavailable_status(self) -> None:
        client = ExternalEvidenceClient(provider=_TimeoutProvider())
        ev = client.fetch_evidence("ABC Dental")
        self.assertFalse(ev.is_ok)
        self.assertEqual(ev.status, "provider_unavailable")

    def test_timeout_does_not_raise(self) -> None:
        client = ExternalEvidenceClient(provider=_TimeoutProvider())
        try:
            ev = client.fetch_evidence("ABC Dental")
        except Exception as exc:
            self.fail(f"fetch_evidence raised an exception: {exc}")

    def test_timeout_error_detail_is_set(self) -> None:
        client = ExternalEvidenceClient(provider=_TimeoutProvider())
        ev = client.fetch_evidence("ABC Dental")
        self.assertIsNotNone(ev.error_detail)
        self.assertIn("exception", ev.error_detail.lower())


# ---------------------------------------------------------------------------
# Tests: Scenario 4: Provider exception
# ---------------------------------------------------------------------------


class TestClientProviderException(unittest.TestCase):
    """Scenario 4: Provider raises a generic unexpected exception."""

    def test_exception_returns_provider_unavailable(self) -> None:
        client = ExternalEvidenceClient(provider=_ExceptionProvider())
        ev = client.fetch_evidence("ABC Dental")
        self.assertEqual(ev.status, "provider_unavailable")

    def test_exception_does_not_propagate(self) -> None:
        client = ExternalEvidenceClient(provider=_ExceptionProvider())
        try:
            ev = client.fetch_evidence("ABC Dental")
        except Exception as exc:
            self.fail(f"Exception leaked from provider: {exc}")


# ---------------------------------------------------------------------------
# Tests: Scenario 5: Malformed evidence
# ---------------------------------------------------------------------------


class TestClientMalformedEvidence(unittest.TestCase):
    """Scenario 5: Provider returns malformed / empty facts."""

    def test_malformed_facts_are_returned_not_filtered(self) -> None:
        """Malformed facts pass through — the caller decides what to do."""
        client = ExternalEvidenceClient(provider=_MalformedProvider())
        ev = client.fetch_evidence("Some Entity")
        self.assertTrue(ev.is_ok)
        self.assertEqual(len(ev.facts), 1)
        self.assertIsNone(ev.facts[0].value)
        self.assertEqual(ev.facts[0].raw_value, "")

    def test_malformed_fact_still_has_source_domain(self) -> None:
        client = ExternalEvidenceClient(provider=_MalformedProvider())
        ev = client.fetch_evidence("Some Entity")
        self.assertNotEqual(ev.facts[0].source_domain, "")


# ---------------------------------------------------------------------------
# Tests: Scenario 6: Budget exhaustion
# ---------------------------------------------------------------------------


class TestClientBudgetExhaustion(unittest.TestCase):
    """Scenario 6: Budget is exhausted before or after first query."""

    def test_budget_exhausted_before_first_query_returns_budget_exhausted(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=0)
        fact = _make_fact()
        client = ExternalEvidenceClient(provider=_OkProvider([fact]), budget=budget)
        ev = client.fetch_evidence("ABC Dental")
        self.assertEqual(ev.status, "budget_exhausted")

    def test_budget_exhausted_after_max_queries(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=2)
        fact = _make_fact()
        client = ExternalEvidenceClient(provider=_OkProvider([fact]), budget=budget)
        ev1 = client.fetch_evidence("Entity A")
        ev2 = client.fetch_evidence("Entity B")
        ev3 = client.fetch_evidence("Entity C")  # budget exhausted
        self.assertTrue(ev1.is_ok)
        self.assertTrue(ev2.is_ok)
        self.assertEqual(ev3.status, "budget_exhausted")

    def test_budget_snapshot_shows_correct_consumption(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=5)
        fact = _make_fact()
        client = ExternalEvidenceClient(provider=_OkProvider([fact]), budget=budget)
        client.fetch_evidence("Query 1")
        client.fetch_evidence("Query 2")
        snap = client.budget_snapshot()
        self.assertEqual(snap["queries_used"], 2)
        self.assertEqual(snap["queries_remaining"], 3)

    def test_results_per_query_limit_respected(self) -> None:
        """max_results_per_query=2 from a 5-fact provider returns only 2."""
        budget = ExternalEvidenceBudget(max_queries=5, max_results_per_query=2)
        facts = [_make_fact(field=f"field_{i}") for i in range(5)]
        client = ExternalEvidenceClient(provider=_OkProvider(facts), budget=budget)
        ev = client.fetch_evidence("ABC Dental")
        self.assertTrue(ev.is_ok)
        self.assertEqual(len(ev.facts), 2)


# ---------------------------------------------------------------------------
# Tests: Scenario 7: No provider configured
# ---------------------------------------------------------------------------


class TestClientNoProvider(unittest.TestCase):
    """Scenario 7: No external provider configured."""

    def test_no_provider_returns_not_configured_status(self) -> None:
        client = ExternalEvidenceClient()
        ev = client.fetch_evidence("ABC Dental")
        self.assertEqual(ev.status, "provider_not_configured")

    def test_no_provider_has_no_provider(self) -> None:
        client = ExternalEvidenceClient()
        self.assertFalse(client.has_provider)
        self.assertEqual(client.provider_name, "none")

    def test_no_provider_returns_empty_facts(self) -> None:
        client = ExternalEvidenceClient()
        ev = client.fetch_evidence("ABC Dental")
        self.assertEqual(ev.facts, ())

    def test_no_provider_does_not_crash_audit_pipeline(self) -> None:
        """Core audit must work with no provider: empty facts → INSUFFICIENT_EVIDENCE."""
        from src.audits.fact_inventory import FactInventory
        client = ExternalEvidenceClient()
        ev = client.fetch_evidence("ABC Dental")
        # Consumer would not add any units, leaving FactInventory empty
        inventory = FactInventory()
        for fact in ev.facts:
            unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
            inventory.add_unit(unit)
        state, _ = inventory.evaluate_consistency("ABC Dental", "phone")
        self.assertEqual(state, "INSUFFICIENT_EVIDENCE")


# ---------------------------------------------------------------------------
# Tests: Source independence / domain deduplication
# ---------------------------------------------------------------------------


class TestSourceIndependence(unittest.TestCase):
    """Source domain deduplication: same-domain URLs count as one corroborating domain."""

    def test_two_same_domain_urls_count_as_one(self) -> None:
        f1 = ExternalFact.build(
            field="phone", value="1234567890", raw_value="(123) 456-7890",
            source_url="https://yelp.com/biz/abc-dental-downtown",
        )
        f2 = ExternalFact.build(
            field="location", value="new york ny", raw_value="New York, NY",
            source_url="https://yelp.com/biz/abc-dental-uptown",
        )
        ev = ExternalEvidence(
            query="ABC Dental", status="ok", facts=(f1, f2), provider_name="test"
        )
        self.assertEqual(ev.corroborating_domain_count, 1)

    def test_two_different_domains_count_as_two(self) -> None:
        f1 = ExternalFact.build(
            field="phone", value="1234567890", raw_value="(123) 456-7890",
            source_url="https://yelp.com/biz/abc-dental",
        )
        f2 = ExternalFact.build(
            field="location", value="new york ny", raw_value="New York, NY",
            source_url="https://yellowpages.com/abc-dental",
        )
        ev = ExternalEvidence(
            query="ABC Dental", status="ok", facts=(f1, f2), provider_name="test"
        )
        self.assertEqual(ev.corroborating_domain_count, 2)

    def test_www_prefix_stripped_from_domain(self) -> None:
        d = _extract_domain("https://www.yelp.com/biz/abc")
        self.assertEqual(d, "yelp.com")

    def test_domain_extraction_handles_port(self) -> None:
        d = _extract_domain("https://api.example.com:8080/v1/search")
        self.assertEqual(d, "api.example.com")

    def test_domain_extraction_empty_url(self) -> None:
        d = _extract_domain("")
        self.assertEqual(d, "")


# ---------------------------------------------------------------------------
# Tests: external_fact_to_evidence_unit bridge
# ---------------------------------------------------------------------------


class TestExternalFactToEvidenceUnit(unittest.TestCase):
    """Bridge from ExternalFact → EvidenceUnit with EXTERNAL source_classification."""

    def test_source_classification_is_external(self) -> None:
        fact = _make_fact(field="phone", value="8005550199")
        unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
        self.assertEqual(unit.source_classification, "EXTERNAL")

    def test_entity_is_set_from_argument(self) -> None:
        fact = _make_fact()
        unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
        self.assertEqual(unit.entity, "ABC Dental")

    def test_entity_falls_back_to_fact_entity(self) -> None:
        fact = ExternalFact.build(
            field="phone", value="8005550199", raw_value="(800) 555-0199",
            source_url="https://yelp.com/abc", entity="Fact Entity"
        )
        unit = external_fact_to_evidence_unit(fact)
        self.assertEqual(unit.entity, "Fact Entity")

    def test_source_domain_preserved_in_details(self) -> None:
        fact = _make_fact(source_url="https://yelp.com/abc-dental")
        unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
        self.assertEqual(unit.details["source_domain"], "yelp.com")

    def test_raw_value_preserved(self) -> None:
        fact = _make_fact(raw_value="(800) 555-0199")
        unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
        self.assertEqual(unit.raw_value, "(800) 555-0199")

    def test_external_unit_integrates_with_fact_inventory(self) -> None:
        """External facts can be added to FactInventory and evaluated."""
        from src.audits.fact_inventory import FactInventory

        f1 = ExternalFact.build(
            field="phone", value="8005550199", raw_value="(800) 555-0199",
            source_url="https://yelp.com/abc-dental",
        )
        f2 = ExternalFact.build(
            field="phone", value="8005550199", raw_value="800-555-0199",
            source_url="https://yellowpages.com/abc-dental",
        )

        inventory = FactInventory()
        for fact in (f1, f2):
            unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
            inventory.add_unit(unit)

        state, explanation = inventory.evaluate_consistency("ABC Dental", "phone")
        self.assertEqual(state, "MATCH")
        self.assertIn("agree", explanation)

    def test_conflicting_external_facts_produce_conflict(self) -> None:
        """Conflicting external facts from different sources → CONFLICT."""
        from src.audits.fact_inventory import FactInventory

        f1 = ExternalFact.build(
            field="phone", value="8005550199", raw_value="(800) 555-0199",
            source_url="https://yelp.com/abc-dental",
        )
        f2 = ExternalFact.build(
            field="phone", value="8005559999", raw_value="(800) 555-9999",
            source_url="https://yellowpages.com/abc-dental",
        )

        inventory = FactInventory()
        for fact in (f1, f2):
            unit = external_fact_to_evidence_unit(fact, entity="ABC Dental")
            inventory.add_unit(unit)

        state, _ = inventory.evaluate_consistency("ABC Dental", "phone")
        self.assertEqual(state, "CONFLICT")


# ---------------------------------------------------------------------------
# Tests: Provider has_provider / provider_name
# ---------------------------------------------------------------------------


class TestClientMetadata(unittest.TestCase):

    def test_has_provider_true_when_provider_set(self) -> None:
        client = ExternalEvidenceClient(provider=_OkProvider([_make_fact()]))
        self.assertTrue(client.has_provider)

    def test_has_provider_false_when_none(self) -> None:
        client = ExternalEvidenceClient()
        self.assertFalse(client.has_provider)

    def test_provider_name_returned_correctly(self) -> None:
        client = ExternalEvidenceClient(provider=_OkProvider([]))
        self.assertEqual(client.provider_name, "fake_ok_provider")

    def test_budget_attribute_accessible(self) -> None:
        budget = ExternalEvidenceBudget(max_queries=3)
        client = ExternalEvidenceClient(budget=budget)
        self.assertEqual(client.budget.max_queries, 3)


if __name__ == "__main__":
    unittest.main()
