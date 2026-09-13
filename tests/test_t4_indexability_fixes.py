# -*- coding: utf-8 -*-
"""Regression tests for T4 fixes:
- robots "none" handling
- canonical fragment comparison
- JSON‑LD @graph parsing
"""

import unittest
from src.audits.t4_indexability import audit_t4_indexability
from src.observation.observation import PageObservation, JsonLdObservation

class TestT4Fixes(unittest.TestCase):
    # ---------- robots none handling ----------
    def test_meta_none_directive(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            robots_meta_directives=("none",),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-003", ids)
        # evidence should mention meta source
        evidence = next(f.evidence for f in findings if f.id == "T4-003")
        self.assertIn("meta", evidence)

    def test_header_none_directive(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            x_robots_tag_directives=("none",),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-003", ids)
        evidence = next(f.evidence for f in findings if f.id == "T4-003")
        self.assertIn("header", evidence)

    def test_both_meta_and_header_none(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            robots_meta_directives=("none",),
            x_robots_tag_directives=("none",),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-003", ids)
        evidence = next(f.evidence for f in findings if f.id == "T4-003")
        self.assertIn("meta", evidence)
        self.assertIn("header", evidence)

    def test_none_plus_index(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            robots_meta_directives=("none",),
            x_robots_tag_directives=("index",),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        # noindex (derived from none) should win over index
        self.assertIn("T4-003", ids)
        self.assertNotIn("T4-004", ids)

    def test_none_plus_noindex(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            robots_meta_directives=("none", "noindex"),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-003", ids)
        self.assertNotIn("T4-004", ids)

    # ---------- canonical fragment comparison ----------
    def test_canonical_fragment_self(self):
        """Self-canonical with fragment is healthy; no finding should be emitted."""
        obs = PageObservation(
            requested_url="https://example.com/page",
            final_url="https://example.com/page",
            status_code=200,
            is_success=True,
            content_type="text/html",
            canonical_url="https://example.com/page#section",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertNotIn("T4-021", ids)
        self.assertNotIn("T4-024", ids)
        self.assertNotIn("T4-023", ids)

    def test_canonical_different_query(self):
        obs = PageObservation(
            requested_url="https://example.com/page?a=1",
            final_url="https://example.com/page?a=1",
            status_code=200,
            is_success=True,
            content_type="text/html",
            canonical_url="https://example.com/page?a=2",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        # same host but different query -> internal alternate
        self.assertIn("T4-024", ids)
        self.assertNotIn("T4-021", ids)
        self.assertNotIn("T4-023", ids)

    def test_canonical_external(self):
        obs = PageObservation(
            requested_url="https://example.com/page",
            final_url="https://example.com/page",
            status_code=200,
            is_success=True,
            content_type="text/html",
            canonical_url="https://other.com/other",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-023", ids)
        self.assertNotIn("T4-021", ids)
        self.assertNotIn("T4-024", ids)

    # ---------- JSON‑LD @graph handling ----------
    def test_jsonld_graph_types_and_context(self):
        json_ld_block = JsonLdObservation(
            raw_text='{"@graph": [{"@type": "Organization", "@context": "http://schema.org"}, {"@type": "WebSite"}]}',
            parsed_data={
                "@graph": [
                    {"@type": "Organization", "@context": "http://schema.org"},
                    {"@type": "WebSite"},
                ]
            },
            is_valid=True,
        )
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            json_ld=[json_ld_block],
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-031", ids)  # block detected
        self.assertIn("T4-032", ids)  # @type values detected
        # both @type values should be present in evidence
        evidence = next(f.evidence for f in findings if f.id == "T4-032")
        self.assertIn("Organization", evidence)
        self.assertIn("WebSite", evidence)
        # @context missing warning should NOT appear because @context present in one graph item
        self.assertNotIn("T4-033", ids)

    def test_jsonld_graph_missing_type(self):
        json_ld_block = JsonLdObservation(
            raw_text='{"@graph": [{"@context": "http://schema.org"}]}',
            parsed_data={"@graph": [{"@context": "http://schema.org"}]},
            is_valid=True,
        )
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            json_ld=[json_ld_block],
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-033", ids)  # missing @context warning should NOT fire (context exists)
        self.assertIn("T4-034", ids)  # missing @type warning

    def test_jsonld_malformed_preserved(self):
        # Simulate a malformed block as observed by T3 (is_valid=False)
        json_ld_block = JsonLdObservation(
            raw_text="{bad json",
            parsed_data=None,
            is_valid=False,
            error_message="Invalid JSON",
        )
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            json_ld=[json_ld_block],
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-035", ids)  # malformed JSON‑LD should be reported because observation flagged it

if __name__ == "__main__":
    unittest.main()
