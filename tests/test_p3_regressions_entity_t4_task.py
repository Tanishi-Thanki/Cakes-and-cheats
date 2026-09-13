"""P2 regression tests: production false-positive elimination.

Pins the fixes for the live semrush.com / nps.gov / zooniverse.org findings:

Phase 1 – Entity identity: registrable-domain comparison so first-party
           subdomains are never treated as different entities; phone/email
           conflicts are only counted when attributable to a named entity
           (structured JSON-LD), never from free-text body phones.
Phase 3 – T4 indexability: healthy signals (T4-004/005/010/021/024/030/031/032)
           are reported as observations, not defects.
Phase 4 – Canonical normalization: tracking-parameter-only differences collapse.
Phase 5 – TR-PROC-001: a generic keyword like "Requirements" alone never fires.

Each test builds observations from raw HTML or structured PageObservations —
no network, no gold dataset.
"""

from __future__ import annotations

import json
import unittest

from src.audits.entity_identity import audit_entity_identity
from src.audits.finding_aggregator import tag_findings
from src.audits.reporting import build_report
from src.audits.t4_indexability import audit_t4_indexability
from src.audits.task_readiness import audit_task_readiness
from src.observation.observation import (
    JsonLdObservation,
    PageObservation,
    observe_html,
)


def _json_ld_obs(entity: dict[str, object]) -> JsonLdObservation:
    return JsonLdObservation(
        raw_text=json.dumps(entity),
        parsed_data=entity,
        is_valid=True,
    )


def _obs(
    url: str,
    title: str,
    visible_text: str = "",
    json_ld: list[JsonLdObservation] | None = None,
) -> PageObservation:
    return PageObservation(
        requested_url=url,
        final_url=url,
        status_code=200,
        is_success=True,
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


class TestEntityIdentityNoEntityFalsePositives(unittest.TestCase):
    """Phase 1: entity identity must not fire on real-site FP classes."""

    def test_same_org_different_visible_text_phones_no_finding(self) -> None:
        """NPS case: two pages of the same org list different free-text phones."""
        obs_a = _obs(
            "https://www.nps.gov/bith/planyourvisit/hiking.htm",
            "Big Thicket National Preserve",
            visible_text="Park phone: +1-409-951-6700.",
        )
        obs_b = _obs(
            "https://www.nps.gov/bith/planyourvisit/hunting.htm",
            "Big Thicket National Preserve",
            visible_text="For hunting permits call +1-409-951-6699.",
        )
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_first_party_subdomains_no_entity_finding(self) -> None:
        """Subdomains of one registrable domain are the same site, not separate."""
        obs_a = _obs("https://developer.semrush.com/", "Semrush")
        obs_b = _obs("https://www.semrush.com/", "Semrush")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_same_org_different_content_pages_no_finding(self) -> None:
        """Different pages of the same org are a MATCH, never a finding."""
        obs_a = _obs("https://example.com/", "Example Corp")
        obs_b = _obs("https://example.com/about", "Example Corp")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_help_subdomain_same_org_no_finding(self) -> None:
        """help.example.com vs www.example.com share a registrable domain."""
        obs_a = _obs("https://help.example.com/", "Zooniverse")
        obs_b = _obs("https://www.example.com/projects", "Zooniverse")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_filter_urls_same_org_no_finding(self) -> None:
        """Query-only variations (discipline filter) are the same entity."""
        obs_a = _obs("https://www.zooniverse.org/projects?discipline=arts", "Zooniverse")
        obs_b = _obs("https://www.zooniverse.org/projects?discipline=biology", "Zooniverse")
        self.assertEqual(audit_entity_identity([obs_a, obs_b]), [])

    def test_json_ld_phone_conflict_still_detected(self) -> None:
        """Attributable JSON-LD phone conflicts keep producing ENT-ID-002."""
        ent_a = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "Gamma Services",
            "telephone": "+1-800-111-2222",
        }
        ent_b = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": "Gamma Services",
            "telephone": "+1-800-333-4444",
        }
        obs_a = _obs(
            "https://gamma.com/",
            "Gamma Services",
            json_ld=[_json_ld_obs(ent_a)],
        )
        obs_b = _obs(
            "https://gamma.com/contact",
            "Gamma Services",
            json_ld=[_json_ld_obs(ent_b)],
        )
        findings = audit_entity_identity([obs_a, obs_b])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "ENT-ID-002")


class TestT4HealthySignalsAreObservations(unittest.TestCase):
    """Phase 3: healthy indexability signals are observations, not defects."""

    def test_self_canonical_is_observation(self) -> None:
        html = (
            "<html><head><title>Pricing</title>"
            "<link rel='canonical' href='https://x.test/pricing#section'></head>"
            "<body><h1>Pricing</h1></body></html>"
        )
        obs = observe_html(html, final_url="https://x.test/pricing")
        findings = audit_t4_indexability(obs)
        self_canonical = [f for f in findings if f.id == "T4-021"]
        self.assertEqual(len(self_canonical), 1)
        self.assertEqual(self_canonical[0].classification, "observation")

    def test_self_canonical_with_tracking_params_is_observation(self) -> None:
        """Phase 4: utm-only differences collapse to self-canonical."""
        html = (
            "<html><head><title>Pricing</title>"
            "<link rel='canonical' href='https://x.test/pricing?utm_source=abc&amp;utm_medium=email'></head>"
            "<body><h1>Pricing</h1></body></html>"
        )
        obs = observe_html(html, final_url="https://x.test/pricing")
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-021", ids)
        self.assertNotIn("T4-024", ids)

    def test_tracking_param_only_canonical_difference_is_not_internal_alternate(self) -> None:
        html = (
            "<html><head><title>Pricing</title>"
            "<link rel='canonical' href='https://x.test/pricing?utm_campaign=x'></head>"
            "<body><h1>Pricing</h1></body></html>"
        )
        obs = observe_html(html, final_url="https://x.test/pricing")
        findings = audit_t4_indexability(obs)
        self.assertNotIn("T4-024", {f.id for f in findings})

    def test_content_param_canonical_difference_stays_internal_alternate(self) -> None:
        html = (
            "<html><head><title>Pricing</title>"
            "<link rel='canonical' href='https://x.test/pricing?a=1'></head>"
            "<body><h1>Pricing</h1></body></html>"
        )
        obs = observe_html(html, final_url="https://x.test/pricing?a=2")
        findings = audit_t4_indexability(obs)
        self.assertIn("T4-024", {f.id for f in findings})

    def test_json_ld_presence_is_observation(self) -> None:
        html = (
            "<html><head><title>Home</title>"
            "<script type='application/ld+json'>"
            '{"@context":"https://schema.org","@type":"WebSite","name":"X"}'
            "</script></head><body><h1>Home</h1></body></html>"
        )
        obs = observe_html(html)
        findings = audit_t4_indexability(obs)
        by_id = {f.id: f for f in findings}
        self.assertEqual(by_id["T4-031"].classification, "observation")
        self.assertEqual(by_id["T4-032"].classification, "observation")

    def test_explicit_index_is_observation(self) -> None:
        html = (
            "<html><head><title>Home</title>"
            "<meta name='robots' content='index'></head>"
            "<body><h1>Home</h1></body></html>"
        )
        obs = observe_html(html)
        findings = audit_t4_indexability(obs)
        index_findings = [f for f in findings if f.id == "T4-004"]
        self.assertEqual(len(index_findings), 1)
        self.assertEqual(index_findings[0].classification, "observation")

    def test_robots_none_does_not_report_directives_present(self) -> None:
        """Phase 3/4: content='none' is absence of directives, not T4-010."""
        html = (
            "<html><head><title>Home</title>"
            "<meta name='robots' content='none'></head>"
            "<body><h1>Home</h1></body></html>"
        )
        obs = observe_html(html)
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertNotIn("T4-010", ids)
        self.assertTrue(all("Meta directives: none" not in f.evidence for f in findings))


class TestTaskReadinessProcessGate(unittest.TestCase):
    """Phase 5: TR-PROC-001 requires process-specific evidence."""

    def test_requirements_keyword_alone_does_not_fire(self) -> None:
        html = """
        <html><body>
            <h1>Application Requirements</h1>
            <p>Requirements: a valid email address and government-issued ID.</p>
            <p>Prerequisites: none.</p>
        </body></html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)
        self.assertNotIn("TR-PROC-001", {f.id for f in findings})

    def test_genuine_multistep_prose_fires_when_unsupported(self) -> None:
        html = """
        <html><body>
            <h1>Apply Now</h1>
            <p>Step 1: create an account. Step 2: upload your documents.
            Step 3: submit and pay the fee.</p>
            <p>Registration steps must be completed in order.</p>
        </body></html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)
        proc = [f for f in findings if f.id == "TR-PROC-001"]
        self.assertEqual(len(proc), 1)

    def test_structured_steps_with_navigation_dont_fire(self) -> None:
        html = """
        <html><body>
            <h1>Registration Steps</h1>
            <h2>Step 1: Create Account</h2>
            <a href="/register/step1">Continue</a>
        </body></html>
        """
        obs = observe_html(html)
        findings = audit_task_readiness(obs)
        self.assertNotIn("TR-PROC-001", {f.id for f in findings})


class TestClassificationSerialization(unittest.TestCase):
    """Phase 3: classification surfaces in the final report; observations counted."""

    def test_observation_serializes_and_is_counted(self) -> None:
        from src.audits.models import Finding, SuggestedAction

        finding = Finding(
            id="T4-030",
            title="No JSON-LD structured data",
            severity="low",
            evidence="No JSON-LD blocks found.",
            suggested_action=SuggestedAction(summary="Optional.", priority=3),
            priority=3,
            confidence="medium",
            classification="observation",
        )
        tagged = tag_findings([finding], "t4_indexability")
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=tagged,
            audit_errors={},
            audits_run=["t4_indexability"],
        )
        final = report.as_final_dict()
        self.assertEqual(final["summary"]["observations"], 1)
        self.assertEqual(final["findings"][0]["classification"], "observation")

    def test_issue_never_serializes_classification(self) -> None:
        from src.audits.models import Finding, SuggestedAction

        finding = Finding(
            id="TR-INT-001",
            title="Non-navigable action",
            severity="high",
            evidence="broken href",
            suggested_action=SuggestedAction(summary="Fix href.", priority=1),
            priority=1,
            confidence="high",
        )
        tagged = tag_findings([finding], "task_readiness")
        report = build_report(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=tagged,
            audit_errors={},
            audits_run=["task_readiness"],
        )
        final = report.as_final_dict()
        self.assertNotIn("classification", final["findings"][0])
        self.assertNotIn("observations", final["summary"])


if __name__ == "__main__":
    unittest.main()