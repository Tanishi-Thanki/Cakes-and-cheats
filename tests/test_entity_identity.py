"""Tests for entity identity analysis (src/audits/entity_identity.py)."""
import unittest
from src.audits.entity_identity import compare_identity, IdentityClassification
from src.observation.observation import PageObservation

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

if __name__ == "__main__":
    unittest.main()
