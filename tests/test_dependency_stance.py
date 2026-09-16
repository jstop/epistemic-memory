"""Dependency-aware stance: contested/retired premises degrade dependents.

The v0.5/handoff behavior in stance vocabulary: premise defeated -> dependent
loses reliance. Degradation caps at SUSPECT with an explicit reason; it is
transitive and cycle-safe; resolving the premise restores the dependent.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class DependencyStanceTest(unittest.TestCase):
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
        # premise: grounded observation; dependent: derived from it
        engine.capture({"id": "b-api", "claim": "The API supports X.",
                        "method": "observed", "volatility": "structural",
                        "cluster": "Test"}, evidence_content="docs excerpt")
        engine.capture({"id": "b-plan", "claim": "We should build Y on X.",
                        "method": "derived", "volatility": "structural",
                        "cluster": "Test"}, premise_ids=["b-api"])

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

    def _stamp(self, bid):
        v, _ = engine.find(bid)
        return engine.stamped(v, by_id=engine.all_views_by_id())

    def test_contested_premise_degrades_dependent(self):
        self.assertIsNone(self._stamp("b-plan")["degraded_reason"])
        engine.get_log().record_contradiction(
            "b-api", "b-api", note="live test returned 400")  # self-ref contradicting evidence noted
        s = self._stamp("b-plan")
        self.assertEqual(s["stance"], "SUSPECT")
        self.assertEqual(s["degraded_reason"], "premise contested: b-api")

    def test_resolution_restores_dependent(self):
        engine.get_log().record_contradiction("b-api", "b-api", note="flaky check")
        engine.get_log().resolve_contradiction("b-api", note="check was against staging")
        s = self._stamp("b-plan")
        self.assertNotEqual(s["stance"], "SUSPECT")
        self.assertIsNone(s["degraded_reason"])

    def test_retired_premise_degrades_dependent(self):
        engine.retire("b-api", reason="API removed")
        s = self._stamp("b-plan")
        self.assertEqual(s["stance"], "SUSPECT")
        self.assertEqual(s["degraded_reason"], "premise retired: b-api")

    def test_degradation_is_transitive(self):
        engine.capture({"id": "b-top", "claim": "Therefore we should ship Z.",
                        "method": "derived", "volatility": "structural",
                        "cluster": "Test"}, premise_ids=["b-plan"])
        engine.get_log().record_contradiction("b-api", "b-api", note="broken")
        s = self._stamp("b-top")
        self.assertEqual(s["stance"], "SUSPECT")
        self.assertIn("premise degraded: b-plan", s["degraded_reason"])
        self.assertIn("premise contested: b-api", s["degraded_reason"])

    def test_write_responses_match_dependency_aware_reads(self):
        engine.get_log().record_contradiction("b-api", "b-api", note="broken")
        captured = engine.capture({"id": "b-new", "claim": "New conclusion",
                                   "method": "derived", "volatility": "historical"},
                                  premise_ids=["b-api"])
        self.assertEqual(captured["stance"], "SUSPECT")
        for out in (captured,
                    engine.record_verification("b-new", "verified", note="owner judgment"),
                    engine.reconcile("b-new", "Restated conclusion", note="clarification"),
                    engine.set_visibility("b-new", False, note="private")):
            self.assertEqual(out["stance"], self._stamp("b-new")["stance"])
            self.assertEqual(out["degraded_reason"], "premise contested: b-api")

    def test_unsupported_and_failed_verification_premises_degrade_dependents(self):
        engine.capture({"id": "b-unsupported", "claim": "No evidence", "method": "observed",
                        "volatility": "historical"})
        out = engine.capture({"id": "b-child", "claim": "Conclusion", "method": "derived",
                              "volatility": "historical"}, premise_ids=["b-unsupported"])
        self.assertEqual(out["stance"], "SUSPECT")
        engine.get_log().record_verification("b-api", "failed", output="failure",
                                            execution={"returncode": 1, "timed_out": False})
        self.assertEqual(self._stamp("b-plan")["stance"], "SUSPECT")
        self.assertEqual(self._stamp("b-plan")["degraded_reason"], "premise verification failed: b-api")

    def test_cycles_are_safe(self):
        engine.get_log().record_relationship("b-api", "DEPENDS_ON", "b-plan",
                                             note="artificial cycle for test")
        s = self._stamp("b-plan")  # must terminate
        self.assertIn(s["stance"], engine.STANCE_ORDER)

    def test_contested_beats_degraded(self):
        engine.get_log().record_contradiction("b-api", "b-api", note="broken")
        engine.get_log().record_contradiction("b-plan", "b-api", note="directly disputed")
        self.assertEqual(self._stamp("b-plan")["stance"], "CONTESTED")

    def test_health_reports_degradation_reason(self):
        engine.get_log().record_contradiction("b-api", "b-api", note="broken")
        h = engine.health()
        reasons = {i["id"]: i.get("reason") for i in h["needs_attention"]}
        self.assertEqual(reasons.get("b-plan"), "premise contested: b-api")


if __name__ == "__main__":
    unittest.main(verbosity=2)
