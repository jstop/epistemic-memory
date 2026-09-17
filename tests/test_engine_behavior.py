"""Behavior tests for the log-backed engine: the pre-cutover surface semantics
(capture / recall / verify / reconcile / health / index) must survive, plus the
cutover invariants: canonical.db is the sole source of truth, projections are
regenerable and never feed back, capture never blocks on projection failure.
"""
import os
import sys
import unittest
import subprocess
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class EngineBehaviorTest(unittest.TestCase):
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
        # written by test:fixture, not the owner: fresh and evidenced, yet capped
        # at NOTE until a person stands behind it (corrigibility rule)
        self.assertEqual(out["stance"], "NOTE")
        self.assertIn("not yet stood behind", out["degraded_reason"])
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

    def test_unsupported_beliefs_never_instruct_silent_reliance(self):
        for method in ("observed", "asserted", "derived", "inferred"):
            with self.subTest(method=method):
                out = self._capture(id=method, method=method, volatility="historical")
                self.assertTrue(out["unsupported"])
                self.assertNotEqual(out["stance"], "RELY")
                self.assertNotEqual(out["guidance"], "use silently")
                self.assertEqual(engine.stamped(engine.find(method)[0])["stance"], out["stance"])

    def test_failed_command_cannot_be_recorded_as_verified(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "printf 'failure'; exit 7"})
        observed = engine.run_anchor("b-1")
        self.assertEqual(observed["execution"]["returncode"], 7)
        out = engine.record_verification("b-1", "verified", note="caller judgment")
        self.assertEqual(out["stance"], "SUSPECT")
        self.assertTrue(out["verification_failed"])
        self.assertFalse(out["contested"])
        self.assertIsNone(out["verified_at"])
        verification = engine.why("b-1")["verifications"][-1]
        self.assertEqual(verification["verdict"], "failed")
        self.assertEqual(verification["requested_verdict"], "verified")
        self.assertEqual(verification["execution"]["returncode"], 7)
        self.assertEqual(verification["note"], "caller judgment")
        log = engine.get_log()
        self.assertEqual(log.evidence_content(verification["evidence"]["evidence_id"]), b"failure")
        replay = log.replay_into(Path(self.tmp.name) / "replayed.db")
        try:
            self.assertEqual(replay.state(), log.state())
        finally:
            replay.close()

    def test_failed_execution_does_not_erase_existing_contradiction(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "exit 1"})
        engine.get_log().record_contradiction("b-1", "b-1", note="counterevidence")
        self.assertEqual(engine.record_verification("b-1", "verified")["stance"], "CONTESTED")

    def test_timeout_preserves_partial_output_and_blocks_reliance(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "check"})
        error = subprocess.TimeoutExpired("check", 120, output=b"partial", stderr=b" error")
        with patch.object(engine.subprocess, "run", side_effect=error):
            out = engine.record_verification("b-1", "verified")
        self.assertEqual(out["stance"], "SUSPECT")
        verification = engine.why("b-1")["verifications"][-1]
        self.assertTrue(verification["execution"]["timed_out"])
        self.assertEqual(engine.get_log().evidence_content(verification["evidence"]["evidence_id"]),
                         b"partial error")

    def test_successful_recheck_restores_reliance(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "check"})
        with patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess("check", 1, "bad", "")):
            engine.record_verification("b-1", "verified")
        with patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess("check", 0, "ok", "")):
            out = engine.record_verification("b-1", "verified")
        self.assertEqual(out["stance"], "NOTE")  # reliance restored, still unreviewed by the owner
        self.assertFalse(out["verification_failed"])
        self.assertIsNotNone(out["verified_at"])

    def test_invalid_verdict_does_not_run_anchor_or_append_events(self):
        self._capture(evidence_content="x", belief_extra={"anchor": "check"})
        before = engine.get_log().events()
        with patch.object(engine.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                engine.record_verification("b-1", "typo")
            run.assert_not_called()
        self.assertEqual(engine.get_log().events(), before)

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
        self.assertIn("NOTE", content)

    # -- corrigibility: unreviewed is never RELY ---------------------------------

    def test_unreviewed_belief_is_capped_at_note_until_owner_stands_behind_it(self):
        out = self._capture(evidence_content="x")
        self.assertEqual(out["stance"], "NOTE")
        self.assertEqual(out["guidance"], "use, state the basis")
        self.assertIsNone(out["authorship"]["stood_behind_by"])
        # the owner restating it from their own channel lifts the cap
        engine._LOGS.clear()
        engine.CHANNEL_ACTOR = "owner"
        try:
            out = engine.reconcile("b-1", "Test claim.", note="confirmed by the owner")
        finally:
            engine.CHANNEL_ACTOR = None
            engine._LOGS.clear()
        self.assertEqual(out["authorship"]["stood_behind_by"], "owner")
        self.assertEqual(out["stance"], "RELY")
        self.assertIsNone(out["degraded_reason"])

    def test_gate_check_can_record_its_outcome(self):
        self._capture(evidence_content="x")
        engine.regenerate_projections()
        report = engine.check(run_anchors=False, record=True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["unreviewed_beliefs"], 1)
        run = engine.get_log().state()["runs"][report["run_id"]]
        self.assertEqual(run["kind"], "gate-check")
        self.assertTrue(run["outputs"][0]["ok"])
        self.assertEqual(run["params"]["unreviewed"], 1)

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
