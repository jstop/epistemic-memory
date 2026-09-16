"""Source spans are provenance facts: exact text, located in canonical
evidence, literal author derived from the message sender. Interpretations
are derived objects: grounded, attributed to an interpreter, supersedable,
never beliefs. What a span means is not recorded in the substrate."""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
import ingest  # noqa: E402
from substrate import canonical_json  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")

TRANSCRIPT = {"uuid": "c1", "name": "t", "created_at": "2026-06-08", "updated_at": "2026-06-08",
              "messages": [
                  {"role": "human", "at": "2026-06-08", "text": "Compose a message to open up conversation with Wes about KERI"},
                  {"role": "assistant", "at": "2026-06-08", "text": "Here is a draft: KERI is the substrate Osmio should sit on."},
                  {"role": "human", "at": "2026-06-09", "text": "I did my set at Sportsdrink last night"},
              ]}


class GroundingTest(unittest.TestCase):
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
        self.log = engine.get_log()
        self.eid = self.log.register_evidence(media_type="application/json", uri="t://c1",
                                              content=canonical_json(TRANSCRIPT),
                                              metadata={"transcript": "full"})

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

    def test_quote_must_be_verbatim(self):
        with self.assertRaises(ValueError):
            self.log.form_belief(belief_id="b-x", claim="x", method="asserted", volatility="status",
                                 grounding=[{"evidence_id": self.eid, "quote": "I sent a message to Wes"}])

    def test_span_resolves_offsets_and_literal_author(self):
        self.log.form_belief(belief_id="b-set", claim="Josh performed a set at Sportsdrink.",
                             method="asserted", volatility="historical",
                             grounding=[{"evidence_id": self.eid, "quote": "I did my set at Sportsdrink"}])
        why = self.log.why("b-set")
        g = why["grounding"][0]
        self.assertEqual((g["message"], g["start"], g["author"]), (2, 0, "human"))
        self.assertIn(self.eid, self.log.state()["beliefs"]["b-set"]["evidence_ids"])  # evidence derived from spans

    def test_assistant_authored_span_is_labeled_not_judged(self):
        # The substrate records WHO wrote it; whether Josh adopted it is interpretation.
        self.log.form_belief(belief_id="b-keri", claim="KERI as Osmio's substrate (framing)",
                             method="asserted", volatility="preference",
                             grounding=[{"evidence_id": self.eid, "quote": "KERI is the substrate Osmio should sit on"}])
        self.assertEqual(self.log.why("b-keri")["grounding"][0]["author"], "assistant")

    def test_interpretation_is_derived_grounded_and_supersedable(self):
        i1 = engine.interpret(kind="hypothesis",
                              statement="No evidence in this conversation establishes that outreach to Wes occurred.",
                              grounding=[{"evidence_id": self.eid, "quote": "Compose a message to open up conversation with Wes"}],
                              interpreter="claude-fable-5.1@review")
        self.assertEqual(i1["grounding"][0]["author"], "human")
        i2 = engine.interpret(kind="hypothesis", statement="Outreach still unevidenced as of later sessions.",
                              grounding=[{"evidence_id": self.eid, "quote": "Compose a message"}],
                              interpreter="claude-fable-5.1@review", supersedes=i1["interpretation_id"])
        current = engine.interpretations()
        self.assertEqual([i["interpretation_id"] for i in current], [i2["interpretation_id"]])
        self.assertEqual(len(engine.interpretations(include_superseded=True)), 2)
        self.assertNotIn("No evidence in this conversation", engine.index_markdown())  # never ambient
        with self.assertRaises(ValueError):
            engine.interpret(kind="hypothesis", statement="ungrounded", grounding=[], interpreter="x@1")

    def test_ingest_proposals_carry_grounding(self):
        out = ingest.apply([{"evidence_id": self.eid, "belief_id": "chat-set",
                             "claim": "Josh performed at Sportsdrink", "volatility": "historical",
                             "cluster": "Interests",
                             "grounding": [{"quote": "I did my set at Sportsdrink"}]}], extractor="t")
        self.assertEqual(out["captured"], ["chat-set"])
        self.assertEqual(engine.why("chat-set")["grounding"][0]["message"], 2)

    def test_replay_reconstructs_spans_and_interpretations(self):
        self.log.form_belief(belief_id="b-set", claim="x", method="asserted", volatility="historical",
                             grounding=[{"evidence_id": self.eid, "quote": "I did my set"}])
        engine.interpret(kind="theme", statement="comedy on the road",
                         grounding=[{"evidence_id": self.eid, "quote": "my set"}], interpreter="t@1",
                         subjects=["b-set"])
        fresh = self.log.replay_into(Path(self.tmp.name) / "fresh.db")
        try:
            self.assertEqual(fresh.state(), self.log.state())
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)



# ── GroundingAdded / AnchorSet: support and re-check that arrive later ──

class LateGroundingTest(GroundingTest):
    def test_add_grounding_keeps_claim_and_authorship(self):
        log = engine.get_log()
        log.form_belief(belief_id="b1", claim="the sky is blue", method="asserted",
                        volatility="structural", unsupported=True)
        before = log.state()["beliefs"]["b1"]
        self.assertTrue(before["unsupported"]); self.assertEqual(before["evidence_ids"], [])
        eid = log.register_evidence(media_type="text/plain",
                                    uri="epist-workspace://sky@abc123",
                                    content="THESIS: the sky is blue\n{...}")
        log.add_grounding("b1", grounding=[{"evidence_id": eid, "quote": "the sky is blue"}],
                          note="argued in workspace sky")
        after = log.state()["beliefs"]["b1"]
        self.assertEqual(after["claim"], before["claim"])
        self.assertEqual(after["authorship"], before["authorship"])
        self.assertEqual(after["evidence_ids"], [eid])
        self.assertEqual(after["grounding"][0]["quote"], "the sky is blue")
        self.assertFalse(after["unsupported"])
        self.assertEqual([h["event_type"] for h in after["history"]], ["BeliefFormed", "GroundingAdded"])
        # engine wrapper + stance: an asserted belief with evidence is no longer unsupported
        v = engine.stamped(engine.find("b1")[0])
        self.assertFalse(v["unsupported"])

    def test_add_grounding_rejects_unknown_evidence_and_empty(self):
        log = engine.get_log()
        log.form_belief(belief_id="b1", claim="c", method="asserted",
                        volatility="structural", unsupported=True)
        with self.assertRaises(ValueError):
            log.add_grounding("b1", evidence_ids=["evd_nope"])
        with self.assertRaises(ValueError):
            log.add_grounding("b1")

    def test_set_anchor_replaces_and_keeps_history(self):
        log = engine.get_log()
        log.form_belief(belief_id="b1", claim="c", method="asserted",
                        volatility="structural", unsupported=True, anchor="true")
        engine.set_anchor("b1", "epist verify-thesis sky", anchor_cost="cheap", note="argument is the anchor")
        b = log.state()["beliefs"]["b1"]
        self.assertEqual(b["anchor"], "epist verify-thesis sky"); self.assertEqual(b["anchor_cost"], "cheap")
        self.assertEqual(b["history"][-1]["event_type"], "AnchorSet")
        engine.set_anchor("b1", None, note="unanchored")
        self.assertIsNone(log.state()["beliefs"]["b1"]["anchor"])
        self.assertTrue(log.verify_chain())

    def test_engine_ground_registers_evidence(self):
        log = engine.get_log()
        log.form_belief(belief_id="b1", claim="the sky is blue", method="asserted",
                        volatility="structural", unsupported=True)
        v = engine.ground("b1", evidence_content="THESIS: the sky is blue", evidence_uri="epist-workspace://sky@1",
                          grounding=None, note="n")
        self.assertFalse(v["unsupported"])
        b = log.state()["beliefs"]["b1"]
        self.assertEqual(len(b["evidence_ids"]), 1)
        self.assertTrue(b["evidence_ids"][0].startswith("evd"))
        self.assertEqual(log.state()["evidence"][b["evidence_ids"][0]]["uri"], "epist-workspace://sky@1")
