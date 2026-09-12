"""Unit tests for the task-aware Priority Engine.

Covers:
9. Direct task blocker ranks above unrelated general finding
10. Task-impairing finding ranks appropriately
11. High confidence deterministically breaks otherwise equal ties
12. priority_reason is generated and evidence-based
13. No-context ranking remains backward compatible
14. Same inputs produce same ordering
"""

import unittest

from src.audits.models import Finding, SuggestedAction
from src.audits.objective_mapping import ObjectiveMapping
from src.audits.priority_engine import (
    PriorityEngine,
    PrioritySignal,
    collect_signals,
    compute_priorities,
)


def _make_finding(
    finding_id: str,
    title: str = "Test finding",
    severity: str = "medium",
    priority: int = 2,
    confidence: str = "medium",
    source_audit: str = "test_audit",
    evidence: str = "Test evidence.",
) -> Finding:
    return Finding(
        id=finding_id,
        title=title,
        severity=severity,  # type: ignore[arg-type]
        evidence=evidence,
        suggested_action=SuggestedAction(summary="Remediation", priority=priority),
        priority=priority,
        confidence=confidence,  # type: ignore[arg-type]
        source_audit=source_audit,
    )


class TestPriorityEngineTaskAware(unittest.TestCase):
    """Tests for task-aware priority computation and signal ordering."""

    def test_direct_blocker_ranks_above_unrelated_critical(self) -> None:
        """9. Direct task blocker ranks above an unrelated general finding even if general has critical severity."""
        f_blocker = _make_finding("TR-INT-001", severity="high", priority=1)
        f_general = _make_finding("MA-IMG-001", severity="critical", priority=1)

        m_blocker = ObjectiveMapping(
            finding_id="TR-INT-001",
            source_audit="task_readiness",
            affected_objectives=["transaction_completion"],
            task_relevance="direct_blocker",
            relevance_reason="Non-navigable checkout link blocks purchase task.",
        )
        m_general = ObjectiveMapping(
            finding_id="MA-IMG-001",
            source_audit="machine_answerability",
            affected_objectives=[],
            task_relevance="general",
            relevance_reason="General site issue; no requested task/objective relationship was established.",
        )

        signals = collect_signals(
            [f_general, f_blocker],
            affected_url="https://example.com/",
            objective_mappings=[m_general, m_blocker],
        )
        ranked = compute_priorities(signals)

        self.assertEqual(len(ranked), 2)
        # Direct blocker must rank first
        self.assertEqual(ranked[0].finding_id, "TR-INT-001")
        self.assertEqual(ranked[0].task_relevance, "direct_blocker")
        self.assertEqual(ranked[1].finding_id, "MA-IMG-001")
        self.assertEqual(ranked[1].task_relevance, "general")

    def test_task_impairing_ranks_between_blocker_and_general(self) -> None:
        """10. Task-impairing finding ranks between direct_blocker and general."""
        f_blocker = _make_finding("TR-INT-001", priority=1)
        f_impairing = _make_finding("EVID-PRC-001", priority=1)
        f_general = _make_finding("MA-IMG-001", priority=1)

        m_blocker = ObjectiveMapping(
            finding_id="TR-INT-001",
            source_audit="task_readiness",
            affected_objectives=["transaction_completion"],
            task_relevance="direct_blocker",
        )
        m_impairing = ObjectiveMapping(
            finding_id="EVID-PRC-001",
            source_audit="evidence_consistency",
            affected_objectives=["price_accuracy"],
            task_relevance="task_impairing",
        )
        m_general = ObjectiveMapping(
            finding_id="MA-IMG-001",
            source_audit="machine_answerability",
            affected_objectives=[],
            task_relevance="general",
        )

        signals = collect_signals(
            [f_general, f_impairing, f_blocker],
            objective_mappings=[m_general, m_impairing, m_blocker],
        )
        ranked = compute_priorities(signals)

        self.assertEqual(ranked[0].finding_id, "TR-INT-001")
        self.assertEqual(ranked[1].finding_id, "EVID-PRC-001")
        self.assertEqual(ranked[2].finding_id, "MA-IMG-001")

    def test_high_confidence_deterministically_breaks_equal_ties(self) -> None:
        """11. High confidence deterministically breaks ties among same-tier findings."""
        f_med = _make_finding("FIND-001", severity="high", priority=1, confidence="medium")
        f_high = _make_finding("FIND-002", severity="high", priority=1, confidence="high")

        signals = collect_signals([f_med, f_high])
        ranked = compute_priorities(signals)

        self.assertEqual(ranked[0].finding_id, "FIND-002")
        self.assertEqual(ranked[0].confidence, "high")
        self.assertEqual(ranked[1].finding_id, "FIND-001")
        self.assertEqual(ranked[1].confidence, "medium")

    def test_priority_reason_is_generated_and_evidence_based(self) -> None:
        """12. priority_reason is generated and cites task context or general status."""
        f = _make_finding("TR-INT-001", priority=1)
        m = ObjectiveMapping(
            finding_id="TR-INT-001",
            source_audit="task_readiness",
            affected_objectives=["transaction_completion"],
            task_relevance="direct_blocker",
            relevance_reason="Directly affects the requested transaction_completion objective because the purchase action is not navigable.",
        )
        signals = collect_signals([f], objective_mappings=[m])
        self.assertTrue(len(signals[0].priority_reason) > 0)
        self.assertIn("transaction_completion", signals[0].priority_reason)

    def test_no_context_ranking_remains_backward_compatible(self) -> None:
        """13. When no objective mapping is provided, ranking preserves legacy (priority, severity) ordering."""
        f1 = _make_finding("A", severity="low", priority=3)
        f2 = _make_finding("B", severity="high", priority=1)
        f3 = _make_finding("C", severity="critical", priority=1)

        signals = collect_signals([f1, f2, f3])
        ranked = compute_priorities(signals)

        # C is priority 1 critical, B is priority 1 high, A is priority 3 low
        self.assertEqual(ranked[0].finding_id, "C")
        self.assertEqual(ranked[1].finding_id, "B")
        self.assertEqual(ranked[2].finding_id, "A")
        # Every signal defaults to general
        for s in ranked:
            self.assertEqual(s.task_relevance, "general")

    def test_same_inputs_produce_same_ordering(self) -> None:
        """14. Repeated ranking with same input yields identical order."""
        f1 = _make_finding("A", severity="medium", priority=2, confidence="medium")
        f2 = _make_finding("B", severity="medium", priority=2, confidence="high")

        sig1 = collect_signals([f1, f2])
        sig2 = collect_signals([f1, f2])

        r1 = compute_priorities(sig1)
        r2 = compute_priorities(sig2)

        self.assertEqual([s.finding_id for s in r1], [s.finding_id for s in r2])


if __name__ == "__main__":
    unittest.main()
