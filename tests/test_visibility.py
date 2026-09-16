"""Visibility: private beliefs stay in canonical history and explicit recall
but leave the ambient MEMORY.md index. Recorded state, never deletion."""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class VisibilityTest(unittest.TestCase):
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
        engine.capture({"id": "b-sensitive", "claim": "A private health detail.",
                        "method": "observed", "volatility": "structural", "cluster": "Health"},
                       evidence_content="source")

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

    def test_private_leaves_index_but_not_history(self):
        self.assertIn("A private health detail.", engine.index_markdown())
        out = engine.set_visibility("b-sensitive", False, note="owner: keep out of ambient context")
        self.assertFalse(out["ambient"])
        self.assertNotIn("A private health detail.", engine.index_markdown())
        v, _ = engine.find("b-sensitive")  # still recallable explicitly, not retired
        self.assertEqual(v["claim"], "A private health detail.")
        self.assertFalse(v["retired"])
        self.assertIn("ambient: false", engine.yaml_projection()["health.yaml"])
        ops = [h["op"] for h in v["events"]]
        self.assertIn("VisibilityChanged", ops)

    def test_public_restores_and_replay_holds(self):
        engine.set_visibility("b-sensitive", False)
        engine.set_visibility("b-sensitive", True, note="fine to surface")
        self.assertIn("A private health detail.", engine.index_markdown())
        log = engine.get_log()
        fresh = log.replay_into(Path(self.tmp.name) / "fresh.db")
        try:
            self.assertEqual(fresh.state(), log.state())
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
