"""Unit tests for the task-aware Objective Mapping layer.

Covers:
1. TR-INT-001 + purchase intent -> transaction-related objective
2. EVID-PRC-001 + price comparison intent -> price_accuracy / price_evaluation
3. EVID-AVT-001 + inventory intent -> inventory_verification
4. Explicit target objective match
5. Unrelated finding does not receive false objective relevance
6. Empty/no context safely produces general/no affected objective
7. Different findings do not accidentally inherit relevance
8. Mapping is deterministic
"""


import sys
from pathlib import Path

_RUNTIME_DIR = Path(__file__).resolve().parent.parent / "skills" / "audit-orchestrator" / "scripts" / "runtime"
if _RUNTIME_DIR.is_dir() and str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))

import unittest

from src.audits.models import Finding, SuggestedAction
from src.audits.objective_mapping import (
    ObjectiveMapping,
    map_findings_to_objectives,
)


def _make_finding(
    finding_id: str,
    title: str = "Test finding",
    severity: str = "high",
    priority: int = 1,
    source_audit: str = "test_audit",
    evidence: str = "Sample evidence text.",
) -> Finding:
    return Finding(
        id=finding_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Fix issue", priority=priority),
        priority=priority,
        confidence="high",
        source_audit=source_audit,
    )


class TestObjectiveMapping(unittest.TestCase):
    """Tests for deterministic finding -> objective mapping."""

    def test_tr_int_purchase_intent_maps_to_transaction_completion(self) -> None:
        """1. TR-INT-001 + purchase intent -> transaction_completion objective."""
        finding = _make_finding("TR-INT-001", source_audit="task_readiness")
        mappings = map_findings_to_objectives(
            [finding],
            task_intent="purchase running shoes",
        )
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.finding_id, "TR-INT-001")
        self.assertIn("transaction_completion", m.affected_objectives)
        self.assertEqual(m.task_relevance, "direct_blocker")
        self.assertTrue(len(m.relevance_reason) > 0)

    def test_evid_prc_price_comparison_intent_maps_to_price_objectives(self) -> None:
        """2. EVID-PRC-001 + price comparison intent -> price_accuracy / price_evaluation."""
        finding = _make_finding("EVID-PRC-001", source_audit="evidence_consistency")
        mappings = map_findings_to_objectives(
            [finding],
            task_intent="compare prices across stores",
        )
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.finding_id, "EVID-PRC-001")
        self.assertTrue(
            "price_accuracy" in m.affected_objectives or "price_evaluation" in m.affected_objectives
        )
        self.assertIn(m.task_relevance, ("direct_blocker", "task_impairing"))
        self.assertTrue(len(m.relevance_reason) > 0)

    def test_evid_avt_inventory_intent_maps_to_inventory_verification(self) -> None:
        """3. EVID-AVT-001 + inventory intent -> inventory_verification."""
        finding = _make_finding("EVID-AVT-001", source_audit="evidence_consistency")
        mappings = map_findings_to_objectives(
            [finding],
            task_intent="verify stock availability before ordering",
        )
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.finding_id, "EVID-AVT-001")
        self.assertIn("inventory_verification", m.affected_objectives)
        self.assertIn(m.task_relevance, ("direct_blocker", "task_impairing"))

    def test_explicit_target_objective_match(self) -> None:
        """4. Explicit target objective match links finding to objective."""
        finding = _make_finding("EVID-PRC-001", source_audit="evidence_consistency")
        mappings = map_findings_to_objectives(
            [finding],
            target_objectives=["price_evaluation"],
        )
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertIn("price_evaluation", m.affected_objectives)
        self.assertIn(m.task_relevance, ("direct_blocker", "task_impairing"))

    def test_unrelated_finding_no_false_objective_relevance(self) -> None:
        """5. Unrelated finding does not receive false objective relevance."""
        finding = _make_finding("MA-IMG-001", source_audit="machine_answerability")
        mappings = map_findings_to_objectives(
            [finding],
            task_intent="compare prices across stores",
            target_objectives=["price_accuracy"],
        )
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.affected_objectives, [])
        self.assertEqual(m.task_relevance, "general")

    def test_empty_context_produces_general_and_no_affected_objectives(self) -> None:
        """6. Empty/no context safely produces general/no affected objective."""
        finding = _make_finding("TR-INT-001", source_audit="task_readiness")
        mappings = map_findings_to_objectives([finding])
        self.assertEqual(len(mappings), 1)
        m = mappings[0]
        self.assertEqual(m.affected_objectives, [])
        self.assertEqual(m.task_relevance, "general")

    def test_different_findings_do_not_inherit_relevance(self) -> None:
        """7. Different findings in same run maintain isolated relevance."""
        f_relevant = _make_finding("TR-INT-001", source_audit="task_readiness")
        f_unrelated = _make_finding("MA-IMG-001", source_audit="machine_answerability")
        mappings = map_findings_to_objectives(
            [f_relevant, f_unrelated],
            task_intent="purchase shoes online",
        )
        self.assertEqual(len(mappings), 2)
        m_rel = next(m for m in mappings if m.finding_id == "TR-INT-001")
        m_unrel = next(m for m in mappings if m.finding_id == "MA-IMG-001")
        self.assertEqual(m_rel.task_relevance, "direct_blocker")
        self.assertEqual(m_unrel.task_relevance, "general")
        self.assertEqual(m_unrel.affected_objectives, [])

    def test_mapping_is_deterministic(self) -> None:
        """8. Mapping is deterministic across multiple calls with same inputs."""
        finding = _make_finding("EVID-AVT-001", source_audit="evidence_consistency")
        kwargs = {
            "task_intent": "order product in stock",
            "target_objectives": ["inventory_verification"],
        }
        res1 = map_findings_to_objectives([finding], **kwargs)
        res2 = map_findings_to_objectives([finding], **kwargs)
        self.assertEqual(res1[0].as_dict(), res2[0].as_dict())


if __name__ == "__main__":
    unittest.main()
