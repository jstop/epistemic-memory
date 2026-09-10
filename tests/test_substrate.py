"""Acceptance tests for the canonical event substrate.

Each test maps to a behavioral acceptance criterion from the architecture
handoff (§21) plus the owner's design invariants:
  - faithful historical preservation and replay, not deletion;
  - retirement/restriction distinguished from destruction;
  - capture must never depend on retrieval/reconciliation.
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from substrate import CanonicalLog  # noqa: E402


class SubstrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = CanonicalLog(root / "canonical.db", root / "evidence")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _observed_belief(self, belief_id="b-test", claim="The sky was overcast today.",
                         content=b"weather report: overcast"):
        eid = self.log.register_evidence(media_type="text/plain", content=content,
                                         uri="test://source")
        self.log.form_belief(belief_id=belief_id, claim=claim, method="observed",
                             volatility="status", evidence_ids=[eid])
        return belief_id, eid

    # -- provenance ---------------------------------------------------------

    def test_provenance_reaches_actual_evidence(self):
        """'Why do you believe B?' must reach evidence with recoverable content."""
        bid, eid = self._observed_belief()
        why = self.log.why(bid)
        self.assertEqual(why["evidence"][0]["evidence_id"], eid)
        self.assertTrue(why["evidence"][0]["content_available"])
        self.assertEqual(self.log.evidence_content(eid), b"weather report: overcast")

    def test_provenance_walks_premises_recursively(self):
        bid, _ = self._observed_belief()
        self.log.form_belief(belief_id="b-derived", claim="It will likely rain.",
                             method="derived", volatility="status", premise_ids=[bid])
        why = self.log.why("b-derived")
        self.assertEqual(why["premises"][0]["belief_id"], bid)
        self.assertTrue(why["premises"][0]["evidence"][0]["content_available"])

    # -- capture without reconciliation --------------------------------------

    def test_capture_without_reconciliation(self):
        """Valid information is durably captured with no retrieval/relationship
        step; relationship inference happens afterward and separately."""
        a, _ = self._observed_belief("b-a", "SQLite is enough for v0.")
        # Capture of a second, related belief succeeds with no reconciliation.
        b, _ = self._observed_belief("b-b", "We don't need a graph database yet.",
                                     content=b"chat excerpt about graph dbs")
        state = self.log.state()
        self.assertIn("b-a", state["beliefs"])
        self.assertIn("b-b", state["beliefs"])
        self.assertEqual(state["relationships"], [])  # captured before any reconciliation
        # Reconciliation appends a relationship later, as its own event.
        self.log.record_relationship("b-b", "RESTATES", "b-a", note="same architectural stance")
        rels = self.log.state()["relationships"]
        self.assertEqual((rels[0]["subject_id"], rels[0]["rel"], rels[0]["object_id"]),
                         ("b-b", "RESTATES", "b-a"))
        # Both formulations coexist, unmerged.
        self.assertEqual(self.log.state()["beliefs"]["b-a"]["claim"], "SQLite is enough for v0.")
        self.assertEqual(self.log.state()["beliefs"]["b-b"]["claim"], "We don't need a graph database yet.")

    # -- method firewall ------------------------------------------------------

    def test_firewall_observed_requires_evidence(self):
        with self.assertRaises(ValueError):
            self.log.form_belief(belief_id="b-naked", claim="X is true.",
                                 method="observed", volatility="status")

    def test_firewall_derived_requires_premises(self):
        with self.assertRaises(ValueError):
            self.log.form_belief(belief_id="b-naked", claim="X follows.",
                                 method="derived", volatility="status")

    def test_firewall_unsupported_is_explicit_not_silent(self):
        self.log.form_belief(belief_id="b-unsup", claim="X is probably true.",
                             method="inferred", volatility="status", unsupported=True)
        self.assertTrue(self.log.state()["beliefs"]["b-unsup"]["unsupported"])
        self.assertTrue(self.log.why("b-unsup")["unsupported"])

    # -- correction / restatement --------------------------------------------

    def test_restatement_preserves_full_previous_text(self):
        bid, _ = self._observed_belief(claim="Repo has 38 workspaces.")
        self.log.restate_belief(bid, "Repo has 41 workspaces.", note="recount")
        b = self.log.state()["beliefs"][bid]
        self.assertEqual(b["claim"], "Repo has 41 workspaces.")
        texts = [h["claim"] for h in b["claim_history"]]
        self.assertIn("Repo has 38 workspaces.", texts)  # complete, not truncated

    def test_correction_never_destroys_original(self):
        old, old_ev = self._observed_belief("b-old", "The server runs on port 3000.")
        new_ev = self.log.register_evidence(media_type="text/plain",
                                            content=b"netstat: listening on 8080")
        self.log.form_belief(belief_id="b-new", claim="The server runs on port 8080.",
                             method="observed", volatility="status", evidence_ids=[new_ev])
        self.log.record_relationship("b-new", "SUPERSEDES", "b-old")
        self.log.retire_belief("b-old", reason="superseded by b-new")
        state = self.log.state()
        # Retired, not destroyed: claim, evidence, and history all recoverable.
        self.assertTrue(state["beliefs"]["b-old"]["retired"])
        self.assertEqual(state["beliefs"]["b-old"]["claim"], "The server runs on port 3000.")
        self.assertEqual(self.log.evidence_content(old_ev), b"weather report: overcast")
        self.assertTrue(any(h["event_type"] == "BeliefRetired"
                            for h in state["beliefs"]["b-old"]["history"]))

    # -- contradiction ---------------------------------------------------------

    def test_contradiction_keeps_both_sides_inspectable(self):
        a, _ = self._observed_belief("b-claims-x", "The API supports X.")
        b, _ = self._observed_belief("b-claims-not-x", "The API rejected X in testing.",
                                     content=b"curl output: 400 unsupported")
        self.log.record_contradiction(a, b, note="live test contradicts docs")
        state = self.log.state()
        self.assertTrue(state["beliefs"][a]["contested"])
        self.assertEqual(state["beliefs"][b]["claim"], "The API rejected X in testing.")
        self.log.resolve_contradiction(a, note="docs were for v2 API; belief scoped")
        state = self.log.state()
        self.assertFalse(state["beliefs"][a]["contested"])
        # The fact that the contradiction occurred remains in history.
        ops = [h["event_type"] for h in state["beliefs"][a]["history"]]
        self.assertIn("ContradictionRecorded", ops)
        self.assertIn("ContradictionResolved", ops)

    # -- verification -----------------------------------------------------------

    def test_verification_output_becomes_evidence(self):
        bid, _ = self._observed_belief()
        ev_id = self.log.record_verification(bid, "verified",
                                             output="38 workspaces\n", command="ls | wc -l")
        self.assertIsNotNone(ev_id)
        self.assertEqual(self.log.evidence_content(ev_id), b"38 workspaces\n")
        b = self.log.state()["beliefs"][bid]
        self.assertIsNotNone(b["verified_at"])

    def test_failed_verification_contests_belief(self):
        bid, _ = self._observed_belief()
        self.log.record_verification(bid, "contradicted", output="41 workspaces\n")
        self.assertTrue(self.log.state()["beliefs"][bid]["contested"])

    # -- replay / integrity -------------------------------------------------------

    def test_fresh_replay_reconstructs_state(self):
        bid, _ = self._observed_belief()
        self.log.restate_belief(bid, "Restated claim.")
        self.log.record_verification(bid, "verified", output="ok")
        fresh = self.log.replay_into(Path(self.tmp.name) / "fresh.db")
        try:
            self.assertEqual(fresh.events(), self.log.events())
            self.assertEqual(fresh.state(), self.log.state())
            self.assertTrue(fresh.verify_chain())
        finally:
            fresh.close()

    def test_tampering_breaks_chain_verification(self):
        bid, _ = self._observed_belief(claim="Original claim text.")
        self.assertTrue(self.log.verify_chain())
        # Simulate history rewriting behind the API's back.
        raw = sqlite3.connect(str(self.log.db_path))
        raw.execute("UPDATE events SET payload_json = replace(payload_json, 'Original', 'Edited')"
                    " WHERE event_type = 'BeliefFormed'")
        raw.commit(); raw.close()
        self.assertFalse(self.log.verify_chain())

    def test_no_duplicate_belief_ids(self):
        bid, _ = self._observed_belief("b-dup")
        with self.assertRaises(ValueError):
            self._observed_belief("b-dup")

    # -- evidence separation -------------------------------------------------------

    def test_evidence_content_is_outside_the_log(self):
        """Content lives in the content-addressed store; the log holds only the
        digest. Chain verification never depends on the content store."""
        bid, eid = self._observed_belief()
        e = self.log.state()["evidence"][eid]
        stored = self.log.store.root / e["digest"].removeprefix("sha256:")
        self.assertTrue(stored.exists())
        for ev in self.log.events():
            self.assertNotIn("weather report", str(ev["payload"]))
        self.assertTrue(self.log.verify_chain())


if __name__ == "__main__":
    unittest.main(verbosity=2)
