"""P3-4: Finding-ID hardening tests.

Validates that finding IDs are deterministic, unique within a report, stable
across repeated runs and separate processes, independent of generation order
and Python hash randomisation, and preserved through aggregation, serialization,
and reporting — while existing conventions (ENT-ID-001/002, T4-*, and every
other established rule ID) stay intact for single-instance findings.

Covers the 14 mandatory behaviours from the P3-4 task.
"""

import ast
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from src.audits.finding_aggregator import aggregate_findings, deduplicate_findings
from src.audits.finding_ids import disambiguate_finding_ids
from src.audits.models import Finding, SuggestedAction
from src.audits.reporting import build_report
from src.audits.t4_indexability import audit_t4_indexability
from src.crawler.crawler import FetchedPage
from src.observation.observation import PageObservation, observe_page

_REPO = Path(__file__).resolve().parents[1]


def _mk(base_id: str, evidence: str, severity: str = "medium", priority: int = 2) -> Finding:
    """Build a finding with the given rule id and semantic evidence."""
    return Finding(
        id=base_id,
        title=f"Rule {base_id}",
        severity=severity,
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Fix it.", priority=priority),
        priority=priority,
        confidence="high",
    )


def _canonical_page(url: str, canonical_href: str) -> PageObservation:
    """Build a PageObservation with a canonical link (drives T4 canonical audits)."""
    page = FetchedPage(
        requested_url=url,
        final_url=url,
        status_code=200,
        content_type="text/html",
        html_content=(
            b"<html><head><link rel='canonical' href='" + canonical_href.encode() + b"'/>"
            b"<title>Pricing</title></head><body><h1>Pricing</h1>"
            b"<p>Plan details for customers.</p></body></html>"
        ),
        is_success=True,
        error_message=None,
    )
    obs = observe_page(page)
    if not isinstance(obs, PageObservation):
        raise AssertionError("observe_page must produce a PageObservation")
    return obs


class TestSameInputSameIds(unittest.TestCase):
    """Requirement A (mandatory test 1): same input twice -> identical IDs."""

    def test_aggregate_twice_yields_identical_ids(self) -> None:
        findings = [
            _mk("T4-024", "Canonical A differs from final URL B but shares host."),
            _mk("T4-024", "Canonical C differs from final URL D but shares host."),
            _mk("MA-IMG-001", "Page contains images without descriptive alt text."),
            _mk("ENT-ID-001", "Two pages present conflicting addresses."),
        ]
        ids1 = [f.id for f in aggregate_findings(list(findings))]
        ids2 = [f.id for f in aggregate_findings(list(findings))]
        self.assertEqual(ids1, ids2)

    def test_disambiguation_helper_is_deterministic(self) -> None:
        f1 = _mk("T4-024", "First canonical differs internally.")
        f2 = _mk("T4-024", "Second canonical differs internally.")
        first = [f.id for f in disambiguate_finding_ids([f1, f2])]
        second = [f.id for f in disambiguate_finding_ids([f1, f2])]
        self.assertEqual(first, second)


class TestGenerationOrderIndependence(unittest.TestCase):
    """Requirement B (mandatory test 2): order of generation must not change IDs."""

    def _evidence_to_id_map(self, findings) -> dict[str, str]:
        return {f.evidence: f.id for f in findings}

    def test_reordering_colliding_findings_keeps_ids_per_finding(self) -> None:
        a = _mk("T4-024", "Canonical A differs from final but shares host.")
        b = _mk("T4-024", "Canonical B differs from final but shares host.")
        c = _mk("TR-INT-001", "Checkout link uses javascript:void(0).")

        order_abc = self._evidence_to_id_map(aggregate_findings([a, b, c]))
        order_cba = self._evidence_to_id_map(aggregate_findings([c, b, a]))
        self.assertEqual(order_abc, order_cba)

    def test_reordering_three_colliding_findings(self) -> None:
        a = _mk("EVID-001", "Conflict for entity Acme attribute price.")
        b = _mk("EVID-001", "Conflict for entity Acme attribute availability.")
        c = _mk("EVID-001", "Conflict for entity Beta attribute brand.")
        ids_1 = self._evidence_to_id_map(aggregate_findings([a, b, c]))
        ids_2 = self._evidence_to_id_map(aggregate_findings([c, b, a]))
        self.assertEqual(ids_1, ids_2)
        self.assertEqual(len(ids_1), 3)


class TestDifferentTargetsDistinguishable(unittest.TestCase):
    """Requirement C (mandatory test 3): same rule + different target -> different IDs."""

    def test_same_rule_two_targets_differ(self) -> None:
        target_a = _mk("T4-024", "Canonical https://x.test/pricing differs from final URL https://x.test/ but shares host.")
        target_b = _mk("T4-024", "Canonical https://x.test/contact differs from final URL https://x.test/ but shares host.")
        ids = {f.id for f in aggregate_findings([target_a, target_b])}
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(i.startswith("T4-024-") for i in ids))


class TestDifferentRulesDistinguishable(unittest.TestCase):
    """Requirement D (mandatory test 4): different audit rules -> different IDs."""

    def test_rules_are_distinct(self) -> None:
        ma = _mk("MA-IMG-001", "Image-dependent page.")
        tr = _mk("TR-DATA-001", "Missing data.")
        t4 = _mk("T4-005", "No index directive.")
        ids = [f.id for f in aggregate_findings([ma, tr, t4])]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids, ["MA-IMG-001", "TR-DATA-001", "T4-005"])


class TestAggregationPreservesIds(unittest.TestCase):
    """Requirement E (mandatory test 5): aggregation preserves original IDs."""

    def test_single_instance_ids_pass_through_unchanged(self) -> None:
        original = [
            _mk("MA-IMG-001", "evidence A"),
            _mk("TR-DATA-001", "evidence B"),
            _mk("T4-005", "evidence C"),
        ]
        aggregated = aggregate_findings(list(original))
        self.assertEqual({f.id for f in aggregated}, {"MA-IMG-001", "TR-DATA-001", "T4-005"})

    def test_colliding_ids_keep_rule_prefix(self) -> None:
        a = _mk("ENT-ID-001", "Conflict pair 1.")
        b = _mk("ENT-ID-001", "Conflict pair 2.")
        aggregated = aggregate_findings([a, b])
        self.assertEqual(len(aggregated), 2)
        for f in aggregated:
            self.assertTrue(f.id.startswith("ENT-ID-001-"))


class TestSerializationReportingPreservesIds(unittest.TestCase):
    """Requirement E (mandatory test 6): serialization/reporting preserves IDs."""

    def test_report_ids_match_findings(self) -> None:
        findings = aggregate_findings([
            _mk("T4-024", "Canonical A differs from final URL B but shares host."),
            _mk("T4-024", "Canonical C differs from final URL D but shares host."),
            _mk("ENG-004", "Checkout flow is discoverable only via an image."),
        ])
        report = build_report(
            requested_url="https://x.test/",
            final_url="https://x.test/",
            status_code=200,
            is_success=True,
            error_message=None,
            findings=findings,
            audit_errors={},
            audits_run=["t4_indexability", "engagement"],
        )
        final = report.as_final_dict()
        self.assertEqual([f["id"] for f in final["findings"]], [f.id for f in findings])
        parsed = json.loads(report.to_json())
        self.assertEqual([f["id"] for f in parsed["findings"]], [f.id for f in findings])


class TestDuplicateFindingsDeterministic(unittest.TestCase):
    """Requirement F (mandatory test 7): duplicates behave deterministically."""

    def test_exact_duplicates_deduplicate(self) -> None:
        f = _mk("TR-INT-001", "Identical broken interaction evidence.")
        self.assertEqual(len(deduplicate_findings([f, f])), 1)

    def test_distinct_evidence_same_rule_deterministic(self) -> None:
        f1 = _mk("TR-INT-001", "First broken link evidence.")
        f2 = _mk("TR-INT-001", "Second broken link evidence, different content.")
        ids1 = {f.id for f in aggregate_findings([f1, f2])}
        ids2 = {f.id for f in aggregate_findings([f2, f1])}
        self.assertEqual(ids1, ids2)
        self.assertEqual(len(ids1), 2)

    def test_same_evidence_different_details_still_deduplicate(self) -> None:
        f1 = _mk("MA-IMG-001", "Same evidence text.")
        f2 = _mk("MA-IMG-001", "Same evidence text.")
        self.assertEqual(len(deduplicate_findings([f1, f2])), 1)


class TestEntityIdentityIdsStable(unittest.TestCase):
    """Requirement G (mandatory tests 8 & 9): ENT-ID-001/002 remain stable."""

    def _obs(self, url: str, name: str, phone: str | None = None,
             street: str | None = None) -> object:
        from src.observation.observation import JsonLdObservation, PageObservation

        ent: dict = {"@context": "https://schema.org", "@type": "Organization", "name": name}
        if phone is not None:
            ent["telephone"] = phone
        if street is not None:
            ent["address"] = {"streetAddress": street, "addressCountry": "US"}
        block = JsonLdObservation(raw_text=json.dumps(ent), parsed_data=ent, is_valid=True)
        return PageObservation(
            requested_url=url, final_url=url, status_code=200, is_success=True,
            title=name, h1=[name], h2=[], visible_text="", links=[], images=[],
            tables=[], json_ld=[block], signals=None,
        )

    def test_conflict_emits_ent_id_001(self) -> None:
        from src.audits.entity_identity import audit_entity_identity

        a = self._obs("https://acme.com/contact", "Acme Corp", street="123 First Street")
        b = self._obs("https://acme.com/office", "Acme Corp", street="456 Second Avenue")
        findings = audit_entity_identity([a, b])
        self.assertEqual([f.id for f in findings], ["ENT-ID-001"])

    def test_ambiguity_emits_ent_id_002(self) -> None:
        from src.audits.entity_identity import audit_entity_identity

        a = self._obs("https://gamma.com", "Gamma Services", phone="+1-800-111-2222")
        b = self._obs("https://gamma.com/contact", "Gamma Services", phone="+1-800-333-4444")
        findings = audit_entity_identity([a, b])
        self.assertEqual([f.id for f in findings], ["ENT-ID-002"])

    def test_single_ent_id_preserved_through_aggregation(self) -> None:
        from src.audits.entity_identity import audit_entity_identity

        a = self._obs("https://acme.com/contact", "Acme Corp", street="123 First Street")
        b = self._obs("https://acme.com/office", "Acme Corp", street="456 Second Avenue")
        findings = audit_entity_identity([a, b])
        aggregated = aggregate_findings(findings)
        self.assertEqual([f.id for f in aggregated], ["ENT-ID-001"])


class TestT4IdsStable(unittest.TestCase):
    """Requirement H (mandatory test 10): T4 finding IDs remain stable."""

    def test_single_instance_t4_ids_preserved(self) -> None:
        obs = self._page_without_index_meta("https://x.test/")
        findings = audit_t4_indexability(obs)
        ids = {f.id for f in findings}
        self.assertIn("T4-005", ids)

    def _page_without_index_meta(self, url: str):
        page = FetchedPage(
            requested_url=url, final_url=url, status_code=200,
            content_type="text/html",
            html_content=b"<html><head><title>X</title></head><body><p>hi</p></body></html>",
            is_success=True, error_message=None,
        )
        return observe_page(page)

    def test_internal_canonical_single_instance_stable(self) -> None:
        obs = _canonical_page("https://x.test/pricing", "https://x.test/pricing?variant=abc")
        findings = [f for f in audit_t4_indexability(obs) if f.id == "T4-024"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].id, "T4-024")

    def test_internal_canonical_multi_instance_disambiguated(self) -> None:
        a = _canonical_page("https://x.test/pricing", "https://x.test/pricing?variant=1")
        b = _canonical_page("https://x.test/contact", "https://x.test/contact?variant=1")
        findings = audit_t4_indexability(a) + audit_t4_indexability(b)
        aggregated = aggregate_findings(findings)
        t4_024 = [f for f in aggregated if f.id.startswith("T4-024")]
        ids = {f.id for f in t4_024}
        self.assertEqual(len(ids), 2)
        self.assertTrue(all(i != "T4-024" for i in ids))


class TestExistingConventionsPreserved(unittest.TestCase):
    """Mandatory test 11: existing valid finding IDs/conventions keep working."""

    def test_single_instance_conventions_preserved(self) -> None:
        conventions = [
            ("MA-IMG-001", "image evidence"),
            ("MA-TXT-001", "text evidence"),
            ("TR-FORM-001", "form evidence"),
            ("TR-INT-001", "interaction evidence"),
            ("CR-001", "render evidence"),
            ("ENG-001", "engagement evidence"),
            ("FC-FRSH-001", "freshness evidence"),
            ("EVID-PRC-001", "price conflict evidence"),
            ("T4-005", "indexability evidence"),
            ("ENT-ID-001", "identity conflict evidence"),
            ("ENT-ID-002", "identity ambiguity evidence"),
        ]
        findings = [_mk(base, ev) for base, ev in conventions]
        aggregated = aggregate_findings(findings)
        self.assertEqual(
            sorted(f.id for f in aggregated),
            sorted(base for base, _ in conventions),
        )


class TestFullAuditRunNoIdCollisions(unittest.TestCase):
    """Mandatory test 12: a full audit run produces no finding-ID collisions."""

    def test_two_page_run_is_collision_free(self) -> None:
        from src.audits.entity_identity import audit_entity_identity
        from src.audits.p2_audit import audit_p2_evidence
        from src.audits.finding_aggregator import tag_findings
        from src.orchestrator import _DEFAULT_AUDITS, AUDIT_REGISTRY, _ensure_optional_audits_registered

        _ensure_optional_audits_registered()
        observations = [
            _canonical_page("https://x.test/pricing", "https://x.test/pricing?variant=1"),
            _canonical_page("https://x.test/contact", "https://x.test/contact?variant=1"),
        ]
        all_findings = tag_findings(audit_p2_evidence(observations), source_audit="p2_evidence")
        all_findings += tag_findings(audit_entity_identity(observations), source_audit="entity_identity")
        for name in _DEFAULT_AUDITS:
            fn = AUDIT_REGISTRY[name]
            for obs in observations:
                all_findings += tag_findings(fn(obs), source_audit=name)

        aggregated = aggregate_findings(all_findings)
        ids = [f.id for f in aggregated]
        self.assertGreater(len(ids), 0)
        self.assertEqual(len(ids), len(set(ids)), f"colliding IDs: {ids}")

    def test_orchestrator_pipeline_results_are_unique(self) -> None:
        from unittest.mock import patch

        from src.orchestrator import run_audit_full

        def fake_fetch_page(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            if url.rstrip("/").endswith("/robots.txt"):
                return FetchedPage(
                    requested_url=url, final_url=url, status_code=404,
                    content_type="text/plain", html_content=b"",
                    is_success=False, error_message="not found",
                )
            canonical_href = ("https://x.test/pricing?variant=9"
                              if url.rstrip("/").endswith("/pricing") else "https://x.test/?variant=9")
            body = (
                b"<html><head><link rel='canonical' href='" + canonical_href.encode() + b"'/>"
                b"<title>Acme</title></head><body><h1>Acme</h1>"
                b"<a href='https://x.test/pricing'>Pricing</a></body></html>"
            )
            return FetchedPage(
                requested_url=url, final_url=url, status_code=200,
                content_type="text/html", html_content=body,
                is_success=True, error_message=None,
            )

        def fake_fetch_text(url: str, timeout: float, user_agent: str, check_robots: bool) -> FetchedPage:
            return FetchedPage(
                requested_url=url, final_url=url, status_code=404,
                content_type="text/plain", html_content=b"",
                is_success=False, error_message="not found",
            )

        with patch("src.crawler.crawl_session.fetch_page", side_effect=fake_fetch_page), \
             patch("src.crawler.robots_sitemaps.fetch_text", side_effect=fake_fetch_text):
            result, _report = run_audit_full("https://x.test/")
        self.assertTrue(result.is_success)
        ids = [f.id for f in result.findings]
        self.assertEqual(len(ids), len(set(ids)), f"colliding IDs: {ids}")


class TestCrossProcessStability(unittest.TestCase):
    """Mandatory test 13: IDs are stable across separate Python processes."""

    _SCRIPT = "\n".join([
        "import sys",
        "sys.path.insert(0, {runtime!r})",
        "from src.audits.models import Finding, SuggestedAction",
        "from src.audits.finding_aggregator import aggregate_findings",
        "def mk(i, e):",
        "    return Finding(id=i, title='t', severity='medium', evidence=e,",
        "                   suggested_action=SuggestedAction('s', 2), priority=2, confidence='high')",
        "findings = [",
        "    mk('T4-024', 'A differs B but shares host'),",
        "    mk('T4-024', 'C differs D but shares host'),",
        "    mk('ENT-ID-001', 'Pair 1'),",
        "    mk('ENT-ID-001', 'Pair 2'),",
        "    mk('MA-IMG-001', 'image txt'),",
        "]",
        "print('|'.join(f.id for f in aggregate_findings(findings)))",
    ])

    def _run_process(self, hash_seed: str) -> str:
        runtime = str(_REPO / "skills" / "audit-orchestrator" / "scripts" / "runtime")
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = hash_seed
        code = self._SCRIPT.format(runtime=runtime)
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True, env=env,
        )
        return proc.stdout.strip()

    def test_two_processes_different_seeds_same_ids(self) -> None:
        out_1 = self._run_process("1")
        out_2 = self._run_process("2")
        self.assertEqual(out_1, out_2)


class TestNoForbiddenIdSources(unittest.TestCase):
    """Mandatory test 14: no UUID4/timestamps/id()/hash()/random for finding identity."""

    def test_finding_id_construction_uses_no_forbidden_generators(self) -> None:
        src_dir = _REPO / "skills" / "audit-orchestrator" / "scripts" / "runtime" / "src"
        modules = sorted(src_dir.glob("audits/*.py")) + [
            src_dir / "orchestrator.py",
        ]
        violations: list[str] = []
        for path in modules:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "Finding":
                    for kw in node.keywords:
                        if kw.arg == "id":
                            if isinstance(kw.value, ast.Call):
                                func = kw.value.func
                                name = (func.attr if isinstance(func, ast.Attribute)
                                        else (func.id if isinstance(func, ast.Name) else "?"))
                                if name in self._BANNED:
                                    violations.append(f"{path.name}:{node.lineno}:{name}")
                        elif kw.arg in ("details",):
                            pass
        self.assertEqual(violations, [])

    def test_finding_id_module_has_no_volatile_sources(self) -> None:
        path = _REPO / "skills" / "audit-orchestrator" / "scripts" / "runtime" / "src" / "audits" / "finding_ids.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.add(node.func.attr)
        # The volatile generators below must never appear as called functions;
        # `id` restricted to the builtin call (Finding.id attributes are fine).
        self.assertFalse(imports & {"uuid", "time", "random", "secrets"}, sorted(imports))
        self.assertFalse(calls & {"uuid4", "time", "random", "id", "hash", "object", "memoryview"},
                         sorted(calls))
        self.assertIn("sha256", calls)
        self.assertIn("hashlib", imports)


if __name__ == "__main__":
    unittest.main()