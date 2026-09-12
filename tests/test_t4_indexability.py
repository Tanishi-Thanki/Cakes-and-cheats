import unittest
from src.audits.t4_indexability import audit_t4_indexability
from src.observation.observation import PageObservation, JsonLdObservation

class TestT4Indexability(unittest.TestCase):
    def test_fetch_failure(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=0,
            is_success=False,
            error_message="Network error",
            content_type="text/html",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-001", ids)

    def test_non_html(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="application/pdf",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-002", ids)

    def test_noindex_directive(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            robots_meta_directives=("noindex",),
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-003", ids)

    def test_canonical_missing(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            canonical_url=None,
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-020", ids)

    def test_canonical_self(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            canonical_url="http://example.com",
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-021", ids)

    def test_jsonld_missing(self):
        obs = PageObservation(
            requested_url="http://example.com",
            final_url="http://example.com",
            status_code=200,
            is_success=True,
            content_type="text/html",
            json_ld=[],
        )
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-030", ids)

    def test_jsonld_valid(self):
        json_ld_block = JsonLdObservation(
            raw_text='{"@type": "Product", "@context": "http://schema.org"}',
            parsed_data={"@type": "Product", "@context": "http://schema.org"},
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
        self.assertIn("T4-031", ids)
        self.assertIn("T4-032", ids)

if __name__ == '__main__':
    unittest.main()
