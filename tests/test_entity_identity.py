"""Tests for entity identity analysis (src/audits/entity_identity.py)."""
import json
import unittest
from unittest.mock import MagicMock, patch

from src.audits.entity_identity import compare_identity, IdentityClassification
from src.observation.observation import JsonLdObservation, PageObservation

# Reuse helper from existing test file by redefining minimal version
def _make_obs(
    url: str = "https://example.com",
    title: str = "",
    visible_text: str = "",
    json_ld=None,
    is_success: bool = True,
) -> PageObservation:
    # Simple PageObservation construction sufficient for identity signals
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
        signals=None,
    )


def _org_obs(
    url: str,
    name: str,
    phone: str | None = None,
    street_address: str | None = None,
    visible_text: str = "",
) -> PageObservation:
    """Build a PageObservation carrying an Organization JSON-LD entity."""
    ent: dict = {"@context": "https://schema.org", "@type": "Organization", "name": name}
    if phone is not None:
        ent["telephone"] = phone
    if street_address is not None:
        ent["address"] = {"streetAddress": street_address, "addressCountry": "US"}
    block = JsonLdObservation(
        raw_text=json.dumps(ent),
        parsed_data=ent,
        is_valid=True,
    )
    return _make_obs(url=url, title=name, json_ld=[block], visible_text=visible_text)

class TestEntityIdentityAnalysis(unittest.TestCase):
    """Validate the deterministic multi‑signal identity comparison logic."""

    def test_match_same_domain_and_phone(self) -> None:
        """Two pages with identical domain and phone should be a MATCH."""
        obs_a = _make_obs(
            url="https://acme.com/contact",
            title="Acme Corp",
            visible_text="Call us at +1 (800) 555-0199.",
        )
        obs_b = _make_obs(
            url="https://acme.com/about",
            title="Acme Corporation",
            visible_text="Phone: +1-800-555-0199",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "MATCH")
        self.assertIn("domain", result.matching_signals)
        self.assertIn("phone", result.matching_signals)

    def test_ambiguous_same_name_different_domain_no_relationship(self) -> None:
        """Same normalized name but different domains without relationship text yields AMBIGUOUS."""
        obs_a = _make_obs(
            url="https://store1.com",
            title="Beta Widgets",
            visible_text="",
        )
        obs_b = _make_obs(
            url="https://store2.com",
            title="Beta Widgets",
            visible_text="",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "AMBIGUOUS")
        self.assertIn("org_name_partial", result.matching_signals)
        self.assertIn("domain", result.conflicting_signals)

    def test_ambiguous_same_name_different_phone(self) -> None:
        """Same name but different phone numbers should be AMBIGUOUS (potential conflict)."""
        obs_a = _make_obs(
            url="https://gamma.com",
            title="Gamma Services",
            visible_text="Phone: +1-800-111-2222",
        )
        obs_b = _make_obs(
            url="https://gamma.com",
            title="Gamma Services",
            visible_text="Call us at +1-800-333-4444",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "AMBIGUOUS")
        self.assertIn("org_name_partial", result.matching_signals)
        self.assertIn("phone", result.conflicting_signals)

    def test_ambiguous_same_name_different_domain_with_relationship(self) -> None:
        """Relationship language (e.g., "branch of") downgrades a conflict to AMBIGUOUS."""
        obs_a = _make_obs(
            url="https://alpha.com",
            title="Alpha Corp",
            visible_text="Alpha Corp – branch of Beta Holdings",
        )
        obs_b = _make_obs(
            url="https://beta.com",
            title="Alpha Corp",
            visible_text="",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "AMBIGUOUS")
        self.assertIn("org_name_partial", result.matching_signals)
        self.assertIn("domain", result.conflicting_signals)
        self.assertTrue(sig_a.has_relationship_evidence or sig_b.has_relationship_evidence)

    def test_match_different_names_same_domain(self) -> None:
        """Distinct names but identical domain are still a MATCH (domain is strongest)."""
        obs_a = _make_obs(
            url="https://delta.com",
            title="Delta Solutions",
            visible_text="",
        )
        obs_b = _make_obs(
            url="https://delta.com/about",
            title="Delta Solutions Ltd",
            visible_text="",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "MATCH")
        self.assertIn("domain", result.matching_signals)

    def test_insufficient_evidence(self) -> None:
        """When fewer than two hard signals are present across both pages we get INSUFFICIENT_EVIDENCE."""
        obs_a = _make_obs(
            url="https://lonely.org",
            title="Lonely Org",
            visible_text="",
        )
        # Second observation missing critical signals
        obs_b = _make_obs(
            url="https://lonely.org",
            title="",
            visible_text="",
        )
        from src.audits.entity_identity import extract_identity_signals
        sig_a = extract_identity_signals(obs_a)
        sig_b = extract_identity_signals(obs_b)
        result = compare_identity(sig_a, sig_b)
        self.assertEqual(result.classification, "INSUFFICIENT_EVIDENCE")


class TestEntityIdentityAudit(unittest.TestCase):
    """Validate audit_entity_identity turns identity analysis into Findings."""

    def test_conflicting_addresses_emit_conflict_finding(self) -> None:
        """Same domain + same name + contradicting addresses emits ENT-ID-001."""
        from src.audits.entity_identity import audit_entity_identity
        obs_a = _org_obs("https://acme.com/contact", "Acme Corp", street_address="123 First Street")
        obs_b = _org_obs("https://acme.com/office", "Acme Corp", street_address="456 Second Avenue")
        findings = audit_entity_identity([obs_a, obs_b])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.id, "ENT-ID-001")
        self.assertEqual(f.severity, "high")
        self.assertEqual(f.priority, 1)
        self.assertEqual(f.confidence, "high")

    def test_conflicting_phones_emit_ambiguity_finding(self) -> None:
        """Same name but different phone numbers emits ENT-ID-002 (potential ambiguity)."""
        from src.audits.entity_identity import audit_entity_identity
        obs_a = _org_obs("https://gamma.com", "Gamma Services", phone="+1-800-111-2222")
        obs_b = _org_obs("https://gamma.com/contact", "Gamma Services", phone="+1-800-333-4444")
        findings = audit_entity_identity([obs_a, obs_b])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.id, "ENT-ID-002")
        self.assertEqual(f.severity, "medium")
        self.assertEqual(f.priority, 2)
        self.assertEqual(f.confidence, "medium")
        self.assertIn("phone", f.details["conflicting_signals"])

    def test_matching_pages_produce_no_findings(self) -> None:
        """Consistent domain + name + phone across pages is a MATCH, not a defect."""
        from src.audits.entity_identity import audit_entity_identity
        obs_a = _org_obs("https://acme.com", "Acme Corp", phone="+1-800-555-0199")
        obs_b = _org_obs("https://acme.com/contact", "Acme Corp", phone="+1-800-555-0199")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_empty_and_single_observation_produce_no_findings(self) -> None:
        """No conclusions without at least two comparable sources."""
        from src.audits.entity_identity import audit_entity_identity
        self.assertEqual(audit_entity_identity([]), [])
        self.assertEqual(audit_entity_identity([_org_obs("https://acme.com", "Acme Corp")]), [])

    def test_failed_observations_are_ignored(self) -> None:
        """Unsuccessful observations do not contribute signals."""
        from src.audits.entity_identity import audit_entity_identity
        obs_failed = _org_obs("https://acme.com/contact", "Acme Corp", phone="+1-800-111-2222")
        obs_failed = PageObservation(
            requested_url=obs_failed.requested_url,
            final_url=obs_failed.final_url,
            status_code=None,
            is_success=False,
            title=obs_failed.title,
            h1=[],
            h2=[],
            visible_text="",
            links=[],
            images=[],
            tables=[],
            json_ld=obs_failed.json_ld,
            signals=None,
        )
        obs_ok = _org_obs("https://acme.com/contact", "Acme Corp", phone="+1-800-333-4444")
        self.assertEqual(audit_entity_identity([obs_failed, obs_ok]), [])


class TestEntityIdentityPipelineIntegration(unittest.TestCase):
    """Validate entity identity flows end-to-end through run_audit_full."""

    _LANDING = """
    <html><head><title>Northwind Coffee Roasters</title></head>
    <body><h1>Northwind Coffee Roasters</h1>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Organization",
     "name": "Northwind Coffee Roasters", "telephone": "+1 555 010 2030"}
    </script>
    <a href="/contact">Contact</a></body></html>
    """

    _CONTACT = """
    <html><head><title>Northwind Coffee Roasters</title></head>
    <body><h1>Northwind Coffee Roasters</h1>
    <script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "Organization",
     "name": "Northwind Coffee Roasters", "telephone": "+1 555 010 9999"}
    </script></body></html>
    """

    @staticmethod
    def _fake_get(pages: dict[str, str]):
        def _get(url: str, **kwargs: object) -> MagicMock:
            response = MagicMock()
            if url in pages:
                response.status_code = 200
                response.text = pages[url]
            else:
                response.status_code = 404
                response.text = "<html><body></body></html>"
            response.url = url
            response.headers = {"Content-Type": "text/html; charset=utf-8"}
            return response
        return _get

    @patch("src.crawler.crawler.requests.get")
    def test_identity_finding_reaches_report_with_objective_mapping(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = self._fake_get({
            "https://example.com/": self._LANDING,
            "https://example.com/contact": self._CONTACT,
        })
        from src.audits.audit_request import AuditContext, AuditRequest
        from src.audits.entity_identity import ENTITY_AMBIGUITY_FINDING_ID
        from src.orchestrator import AuditOptions, run_audit_full

        request = AuditRequest(
            target_url="https://example.com/",
            context=AuditContext(task_intent="Verify brand identity", target_objectives=["brand"]),
        )
        result, report = run_audit_full(request, AuditOptions(check_robots=False, audits=()))

        self.assertTrue(result.is_success)
        ent = [f for f in report.findings if f.id == ENTITY_AMBIGUITY_FINDING_ID]
        self.assertEqual(len(ent), 1, f"expected an ENT-ID-002 finding, got {[f.id for f in report.findings]}")
        self.assertEqual(ent[0].source_audit, "entity_identity")
        self.assertEqual(ent[0].severity, "medium")

        mapping = [m for m in report.objective_mappings if m.finding_id == ENTITY_AMBIGUITY_FINDING_ID]
        self.assertEqual(len(mapping), 1)
        self.assertEqual(mapping[0].source_audit, "entity_identity")
        self.assertIn("entity_identity", mapping[0].affected_objectives)

    @patch("src.crawler.crawler.requests.get")
    def test_consistent_site_produces_no_identity_findings(self, mock_get: MagicMock) -> None:
        mock_get.side_effect = self._fake_get({
            "https://example.com/": self._LANDING.replace("+1 555 010 9999", "+1 555 010 2030"),
            "https://example.com/contact": self._CONTACT.replace("+1 555 010 9999", "+1 555 010 2030"),
        })
        from src.audits.entity_identity import (
            ENTITY_AMBIGUITY_FINDING_ID,
            ENTITY_CONFLICT_FINDING_ID,
        )
        from src.orchestrator import AuditOptions, run_audit_full

        _, report = run_audit_full("https://example.com/", AuditOptions(check_robots=False, audits=()))
        ids = {f.id for f in report.findings}
        self.assertNotIn(ENTITY_AMBIGUITY_FINDING_ID, ids)
        self.assertNotIn(ENTITY_CONFLICT_FINDING_ID, ids)


if __name__ == "__main__":
    unittest.main()
