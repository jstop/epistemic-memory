"""Acceptance tests for the async reconciler.

Invariants under test: reconciliation runs strictly after capture and never
touches it; each pass is one atomic canonical event; UNRELATED verdicts stop
re-proposal; judgments are layered interpretations (proposer/judge recorded),
never overwrites; CONTRADICTS relates without contesting; replay reconstructs
reconciliation state exactly.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
import reconciler  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class ReconcilerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["EPISTEMIC_DB_PATH"] = str(root / "canonical.db")
        os.environ["EPISTEMIC_ACTOR"] = "test:fixture"
        os.environ["EPISTEMIC_CONTENT_DIR"] = str(root / "evidence_store")
        os.environ["EPISTEMIC_BELIEFS_DIR"] = str(root / "beliefs")
        os.environ["EPISTEMIC_EVENTS_JSONL"] = str(root / "events.jsonl")
        os.environ["EPISTEMIC_INDEX_PATH"] = str(root / "MEMORY.md")
        for bid, claim, observed in [
            ("b-sqlite", "SQLite is enough for the v0 prototype database", "2026-01-01"),
            ("b-nograph", "We do not need a graph database for the v0 prototype", "2026-02-01"),
            ("b-dogfood", "Homemade dog food recipe uses chicken and rice", "2026-03-01"),
        ]:
            engine.capture({"id": bid, "claim": claim, "method": "observed",
                            "volatility": "structural", "cluster": "Architecture",
                            "observed_at": observed},
                           evidence_content=f"source for {bid}")

    def tearDown(self):
        for log in engine._LOGS.values():
            log.close()
        engine._LOGS.clear()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _pairs(self, **kw):
        return {(c["subject_id"], c["object_id"]) for c in reconciler.candidates(**kw)}

    # -- candidate generation -------------------------------------------------

    def test_candidates_rank_related_claims(self):
        pairs = self._pairs()
        self.assertIn(("b-nograph", "b-sqlite"), pairs)  # newer belief is subject

    def test_candidates_exclude_already_related(self):
        engine.get_log().record_relationship("b-nograph", "RESTATES", "b-sqlite")
        self.assertNotIn(("b-nograph", "b-sqlite"), self._pairs())

    def test_capture_is_untouched_by_reconciliation(self):
        # Capture works with zero reconciliation machinery involved and no
        # candidates examined — the store simply has unexamined pairs.
        state = engine.get_log().state()
        self.assertEqual(state["reconciled_pairs"], [])
        self.assertEqual(len(state["beliefs"]), 3)

    # -- applying a pass -------------------------------------------------------

    def test_apply_records_one_atomic_event(self):
        n_before = len(engine.get_log().events())
        reconciler.apply([
            {"subject_id": "b-nograph", "object_id": "b-sqlite",
             "verdict": "RESTATES", "note": "same architectural stance"},
            {"subject_id": "b-nograph", "object_id": "b-dogfood",
             "verdict": "UNRELATED", "note": ""},
        ], judge="test-judge")
        events = engine.get_log().events()
        self.assertEqual(len(events), n_before + 1)  # one event for the whole pass
        run = events[-1]
        self.assertEqual(run["event_type"], "ReconciliationRun")
        self.assertEqual(len(run["payload"]["run"]["judgments"]), 2)

    def test_relationships_project_with_provenance(self):
        reconciler.apply([{"subject_id": "b-nograph", "object_id": "b-sqlite",
                           "verdict": "RESTATES", "note": "reworded"}], judge="test-judge")
        rels = engine.get_log().state()["relationships"]
        self.assertEqual(rels[0]["rel"], "RESTATES")
        self.assertEqual(rels[0]["judge"], "test-judge")
        self.assertEqual(rels[0]["proposer"], f"{reconciler.RECONCILER}@{reconciler.VERSION}")

    def test_unrelated_verdict_stops_reproposal(self):
        reconciler.apply([{"subject_id": "b-nograph", "object_id": "b-sqlite",
                           "verdict": "UNRELATED", "note": "actually different topics"}],
                         judge="test-judge")
        self.assertNotIn(("b-nograph", "b-sqlite"), self._pairs())
        rels = engine.get_log().state()["relationships"]
        self.assertEqual(rels, [])  # examined, but no relationship created

    def test_contradicts_relates_without_contesting(self):
        reconciler.apply([{"subject_id": "b-nograph", "object_id": "b-sqlite",
                           "verdict": "CONTRADICTS", "note": "hypothetical"}],
                         judge="test-judge")
        v, _ = engine.find("b-sqlite")
        self.assertFalse(v["contested"])  # relationship only; contesting is explicit

    def test_invalid_judgments_rejected(self):
        with self.assertRaises(ValueError):
            reconciler.apply([{"subject_id": "b-nograph", "object_id": "b-sqlite",
                               "verdict": "SOUNDS_SIMILAR"}], judge="j")
        with self.assertRaises(ValueError):
            reconciler.apply([{"subject_id": "b-ghost", "object_id": "b-sqlite",
                               "verdict": "RESTATES"}], judge="j")
        with self.assertRaises(ValueError):
            reconciler.apply([{"subject_id": "b-sqlite", "object_id": "b-sqlite",
                               "verdict": "RESTATES"}], judge="j")
        with self.assertRaises(ValueError):
            reconciler.apply([], judge="j")

    # -- replay ----------------------------------------------------------------

    def test_replay_reconstructs_reconciliation_state(self):
        reconciler.apply([
            {"subject_id": "b-nograph", "object_id": "b-sqlite",
             "verdict": "RESTATES", "note": "x"},
            {"subject_id": "b-sqlite", "object_id": "b-dogfood",
             "verdict": "UNRELATED", "note": ""},
        ], judge="test-judge")
        log = engine.get_log()
        fresh = log.replay_into(Path(self.tmp.name) / "fresh.db")
        try:
            self.assertEqual(fresh.state(), log.state())
            self.assertTrue(fresh.verify_chain())
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
