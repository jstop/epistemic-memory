"""Behavior tests for the log-backed engine: the pre-cutover surface semantics
(capture / recall / verify / reconcile / health / index) must survive, plus the
cutover invariants: canonical.db is the sole source of truth, projections are
regenerable and never feed back, capture never blocks on projection failure.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

ENV_KEYS = ("EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class EngineBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["EPISTEMIC_DB_PATH"] = str(root / "canonical.db")
        os.environ["EPISTEMIC_CONTENT_DIR"] = str(root / "evidence_store")
        os.environ["EPISTEMIC_BELIEFS_DIR"] = str(root / "beliefs")
        os.environ["EPISTEMIC_EVENTS_JSONL"] = str(root / "events.jsonl")
        os.environ["EPISTEMIC_INDEX_PATH"] = str(root / "MEMORY.md")

    def tearDown(self):
        for path, log in list(engine._LOGS.items()):
            log.close()
        engine._LOGS.clear()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _capture(self, id="b-1", claim="Test claim.", method="observed",
                 volatility="status", **kw):
        belief = {"id": id, "claim": claim, "method": method,
                  "volatility": volatility, "cluster": "Test cluster"}
        belief.update(kw.pop("belief_extra", {}))
        return engine.capture(belief, **kw)

    # -- capture / recall ------------------------------------------------------

    def test_capture_and_recall_with_stance(self):
        out = self._capture(evidence_content="source excerpt")
        self.assertEqual(out["stance"], "RELY")
        beliefs = {b["id"]: b for b, _ in engine.load_all()}
        self.assertIn("b-1", beliefs)
        self.assertEqual(beliefs["b-1"]["claim"], "Test claim.")

    def test_capture_rejects_envelope_violation(self):
        with self.assertRaises(ValueError):
            engine.capture({"id": "b-x", "claim": "No method."})

    def test_capture_rejects_duplicate_id(self):
        self._capture(evidence_content="x")
        with self.assertRaises(ValueError):
            self._capture(evidence_content="x")

    def test_ungrounded_capture_is_explicitly_unsupported(self):
        out = self._capture()  # observed, no evidence
        self.assertIn("warning", out)
        self.assertTrue(out["unsupported"])

    def test_grounded_capture_reaches_evidence(self):
        self._capture(evidence_content="the actual source text")
        why = engine.why("b-1")
        self.assertTrue(why["evidence"][0]["content_available"])
        self.assertFalse(why["unsupported"])

    # -- verify ----------------------------------------------------------------

    def test_verification_runs_anchor_and_snapshots_output(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "echo anchored-ok"})
        out = engine.record_verification("b-1", "verified")
        self.assertIsNotNone(out["verified_at"])
        log = engine.get_log()
        ev = [e for e in log.state()["evidence"].values()
              if (e["metadata"] or {}).get("role") == "verification-output"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(log.evidence_content(ev[0]["evidence_id"]), b"anchored-ok")

    def test_failed_verification_contests(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "echo reality"})
        out = engine.record_verification("b-1", "contradicted")
        self.assertEqual(out["stance"], "CONTESTED")

    # -- reconcile -------------------------------------------------------------

    def test_reconcile_preserves_history_and_resolves(self):
        self._capture(claim="Port is 3000.", evidence_content="netstat old",
                      belief_extra={"anchor": "echo 8080"})
        engine.record_verification("b-1", "contradicted")
        out = engine.reconcile("b-1", "Port is 8080.", note="anchor showed 8080")
        self.assertEqual(out["claim"], "Port is 8080.")
        self.assertNotEqual(out["stance"], "CONTESTED")
        v, _ = engine.find("b-1")
        self.assertIn("Port is 3000.", [h["claim"] for h in v["claim_history"]])

    # -- retire ----------------------------------------------------------------

    def test_retire_is_recorded_state_not_destruction(self):
        self._capture(evidence_content="x")
        engine.retire("b-1", reason="obsolete")
        self.assertNotIn("b-1", {b["id"] for b, _ in engine.load_all()})
        v, _ = engine.find("b-1")  # find includes retired
        self.assertTrue(v["retired"])
        self.assertEqual(v["claim"], "Test claim.")

    # -- health / index --------------------------------------------------------

    def test_health_counts_and_chain(self):
        self._capture(evidence_content="x")
        h = engine.health()
        self.assertEqual(h["total"], 1)
        self.assertTrue(h["chain_valid"])

    def test_index_written(self):
        self._capture(evidence_content="x")
        path = engine.write_index()
        content = open(path).read()
        self.assertIn("Test claim.", content)
        self.assertIn("RELY", content)

    # -- projections -----------------------------------------------------------

    def test_hand_edits_to_yaml_never_become_canonical(self):
        self._capture(evidence_content="x")
        engine.regenerate_projections()
        yaml_file = Path(engine.beliefs_dir()) / "test-cluster.yaml"
        self.assertTrue(yaml_file.exists())
        # Hand-edit the projection.
        yaml_file.write_text(yaml_file.read_text().replace("Test claim.", "EDITED claim."))
        # Canonical state is untouched; drift is detected; regeneration restores.
        v, _ = engine.find("b-1")
        self.assertEqual(v["claim"], "Test claim.")
        self.assertIn("test-cluster.yaml", engine.projection_drift())
        engine.regenerate_projections()
        self.assertIn("Test claim.", yaml_file.read_text())
        self.assertEqual(engine.projection_drift(), [])

    def test_events_jsonl_is_deterministic_projection(self):
        self._capture(evidence_content="x")
        a = engine.events_jsonl()
        b = engine.events_jsonl()
        self.assertEqual(a, b)
        self.assertEqual(len(a.strip().splitlines()), len(engine.get_log().events()))

    def test_capture_survives_projection_failure(self):
        # Point the jsonl projection at an unwritable path (a directory).
        os.environ["EPISTEMIC_EVENTS_JSONL"] = self.tmp.name
        out = self._capture(evidence_content="x")
        self.assertIn("projection_warning", out)  # write happened, projection failed loudly
        self.assertIn("b-1", {b["id"] for b, _ in engine.load_all()})

    # -- reconstructed history --------------------------------------------------

    def test_reconstructed_flag_survives_to_read_surfaces(self):
        log = engine.get_log()
        eid = log.register_evidence(media_type="application/yaml", content=b"old yaml",
                                    metadata={"role": "pre-log-record"})
        log.form_belief(belief_id="b-old", claim="Imported claim.", method="observed",
                        volatility="structural", cluster="Test cluster",
                        evidence_ids=[eid], metadata={"reconstructed": True})
        v, _ = engine.find("b-old")
        self.assertTrue(v["reconstructed"])
        self.assertTrue(engine.stamped(v)["reconstructed"])
        files = engine.yaml_projection()
        self.assertIn("reconstructed: true", files["test-cluster.yaml"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
