"""Source spans are provenance facts: exact text, located in canonical
evidence, literal author derived from the message sender. Interpretations
are derived objects: grounded, attributed to an interpreter, supersedable,
never beliefs. What a span means is not recorded in the substrate."""
import json
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


class RunIdentityTest(GroundingTest):
    def test_record_run_requires_known_inputs_and_identity(self):
        log = engine.get_log()
        with self.assertRaises(ValueError):
            log.record_run(kind="x", interpreter="")
        with self.assertRaises(ValueError):
            log.record_run(kind="x", interpreter="a@1", inputs=["evd_ghost"])
        eid = log.register_evidence(media_type="text/plain", content="doc")
        rid = engine.record_run(kind="extract", interpreter="epist-ingest@claude-opus-4-6",
                                inputs=[eid], outputs=[{"type": "proposal", "id": "prop-1"}],
                                params={"model": "claude-opus-4-6"})
        runs = engine.runs(kind="extract")
        self.assertEqual([r["run_id"] for r in runs], [rid])
        self.assertEqual(runs[0]["inputs"], [eid])
        self.assertEqual(runs[0]["actor"], "test:fixture")


class BuildGateTest(GroundingTest):
    def test_rebuild_into_branch_and_check(self):
        import tempfile
        log = engine.get_log()
        eid = log.register_evidence(media_type="text/plain", content="the sky is blue, verbatim")
        log.form_belief(belief_id="b1", claim="the sky is blue", method="asserted",
                        volatility="structural", evidence_ids=[eid], anchor="true", anchor_cost="cheap")
        with tempfile.TemporaryDirectory() as builds:
            os.environ["EPISTEMIC_BUILDS_DIR"] = builds
            try:
                with self.assertRaises(ValueError):
                    engine.rebuild("main")
                out = engine.rebuild("dev")
                self.assertTrue(out["state_identical"] and out["chain_ok"])
                self.assertTrue(os.path.islink(os.path.join(builds, "dev", "evidence_store")))
                self.assertTrue(os.path.exists(os.path.join(builds, "dev", "canonical.db")))
                self.assertTrue(out["projections"] and out["index"])
                self.assertTrue(os.path.exists(os.path.join(builds, "dev", "MEMORY.md")))
            finally:
                os.environ.pop("EPISTEMIC_BUILDS_DIR", None)
        engine.regenerate_projections()  # a build's projections are part of the build
        report = engine.check(run_anchors=True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["soft"]["anchors"]["verified"], 1)
        self.assertEqual(report["hard"]["evidence_content_present"], True)


class RecallImportTest(GroundingTest):
    """recall folds into a dev branch as interpretations; never into main."""

    def _fake_recall(self, path):
        import sqlite3
        con = sqlite3.connect(path)
        con.executescript("""
        create table source_record(id integer primary key, source_type text, external_id text,
            content_hash text, content text, content_summary text, created_at text, ingested_at text,
            ingesting_conversation_uuid text, metadata text);
        create table derivation_edge(id integer primary key, claim_text text, claim_hash text,
            source_record_id integer, edge_type text, recorded_by text, recorded_at text,
            conversation_uuid text, context text, notes text);
        create table attestations(id integer primary key, target_type text, target_uuid text,
            verdict text, claim_text text, notes text, attested_at text, attested_by text);
        create table messages(uuid text primary key, conversation_uuid text, sender text, text text,
            created_at text, sequence integer);
        insert into source_record values (1,'document','doi:1','h1','Ostrom (1990), Governing the Commons.','Ostrom','2026-04-20','2026-04-20',null,null);
        insert into source_record values (2,'tool_use','x','h2','tool noise','','2026-04-20','2026-04-20',null,null);
        insert into derivation_edge values (1,'Commons can be governed without a central authority.','c1',1,'paraphrase','claude','2026-04-20T10:00:00','','ctx','');
        insert into derivation_edge values (2,'Josh coined the Voluntary Polity Stack.','c2',null,'pattern_match','claude','2026-04-20T10:01:00','','','unsourced');
        insert into messages values ('m1','conv-1','assistant','I attributed the Bitcoin analogy to Josh.','2026-03-01',3);
        insert into attestations values (1,'message','m1','flagged','Attribution may be mine, not Josh''s','check it','2026-04-20T23:59:00','claude');
        insert into attestations values (2,'message','m1','refuted','Josh never said that','','2026-04-21T09:00:00','self');
        """)
        con.commit(); con.close()

    def test_refuses_main(self):
        import recall_import, tempfile
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "recall.db"); self._fake_recall(db)
            os.environ.pop("EPISTEMIC_BRANCH", None)
            with self.assertRaises(SystemExit):
                recall_import.run(db)

    def test_imports_as_grounded_interpretations_and_is_idempotent(self):
        import recall_import, tempfile
        log = engine.get_log()
        # a library transcript of conv-1 so the attestation gets a second grounding
        log.register_evidence(media_type="application/json", uri="claude-export://conversation/conv-1",
                              content=json.dumps({"uuid": "conv-1", "messages": [
                                  {"role": "human", "text": "hi"},
                                  {"role": "assistant", "text": "I attributed the Bitcoin analogy to Josh."}]}))
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "recall.db"); self._fake_recall(db)
            os.environ["EPISTEMIC_BRANCH"] = "dev"
            # dev branch paths are overridden by the test env (explicit EPISTEMIC_*_PATH win)
            try:
                r = recall_import.run(db)
                st = log.state()
                self.assertEqual(r["counts"], {"edges": 2, "attestations": 2, "sources": 1, "attested_messages": 1})
                self.assertEqual(r["interpretations"]["derivations"], {"new": 2, "sourced": 1, "orphans": 1})
                self.assertEqual(r["interpretations"]["attestations"], {"new": 2, "also_in_transcript": 2})
                self.assertEqual(len(r["human_acts"]), 1)
                kinds = sorted(i["kind"] for i in st["interpretations"].values())
                self.assertEqual(kinds, ["recall-attestation/flagged", "recall-attestation/refuted",
                                         "recall-derivation/paraphrase", "recall-derivation/pattern_match"])
                orphan = next(i for i in st["interpretations"].values() if i["kind"].endswith("pattern_match"))
                self.assertTrue(orphan["metadata"]["orphan"]); self.assertEqual(len(orphan["grounding"]), 1)
                sourced = next(i for i in st["interpretations"].values() if i["kind"].endswith("paraphrase"))
                self.assertEqual(len(sourced["grounding"]), 2)
                refuted = next(i for i in st["interpretations"].values() if i["kind"].endswith("refuted"))
                self.assertTrue(refuted["metadata"]["human_act"])
                self.assertEqual([g["message"] for g in refuted["grounding"] if g.get("message") is not None], [1])
                self.assertEqual(st["runs"][r["run_id"]]["kind"], "import-recall")
                self.assertFalse(st["beliefs"])  # nothing promoted to a belief
                r2 = recall_import.run(db)
                self.assertEqual(r2["skipped"], {"edges": 2, "attestations": 2})
                self.assertEqual(r2["interpretations"]["derivations"]["new"], 0)
                self.assertTrue(log.verify_chain())
            finally:
                os.environ.pop("EPISTEMIC_BRANCH", None)


class PromoteTest(GroundingTest):
    def test_branches_and_gated_promote(self):
        import tempfile, shutil
        log = engine.get_log()
        eid = log.register_evidence(media_type="text/plain", content="x")
        log.form_belief(belief_id="b1", claim="c", method="asserted", volatility="structural", evidence_ids=[eid])
        engine.regenerate_projections()
        with tempfile.TemporaryDirectory() as builds:
            os.environ["EPISTEMIC_BUILDS_DIR"] = builds
            try:
                engine.rebuild("dev")
                rows = {b["name"]: b for b in engine.branches()}
                self.assertIn("dev", rows); self.assertTrue(rows["dev"]["exists"])
                self.assertIsNone(rows["dev"]["last_gate"])
                with self.assertRaises(ValueError):   # no passing gate yet
                    engine.promote("dev")
                with self.assertRaises(ValueError):   # never main onto itself
                    engine.promote("main")
                # a recorded gate on the dev build (as gate.sh does: subprocess with the branch env)
                import subprocess, sys as _sys
                env = {k: v for k, v in os.environ.items()
                       if k not in ("EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR", "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")}
                env["EPISTEMIC_BRANCH"] = "dev"
                r = subprocess.run([_sys.executable, os.path.join(engine.REPO_DIR, "engine.py"), "check", "--record", "--no-anchors"],
                                   env=env, capture_output=True, text=True, cwd=engine.REPO_DIR)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                rows = {b["name"]: b for b in engine.branches()}
                self.assertTrue(rows["dev"]["last_gate"]["ok"])
                # add something on dev so promotion is observable
                dev_log = engine.CanonicalLog(rows["dev"]["db"], engine.content_dir(), actor="test:fixture")
                dev_log.form_belief(belief_id="b2", claim="only on dev", method="asserted", volatility="structural", evidence_ids=[eid])
                dev_log.close()
                out = engine.promote("dev", require_gate=False)
                self.assertTrue(out["ok"] and os.path.exists(out["backup"]))
                engine._LOGS.clear()
                st = engine.get_log().state()
                self.assertIn("b2", st["beliefs"])
                self.assertEqual([r["kind"] for r in st["runs"].values()][-1], "promote")
                self.assertTrue(engine.get_log().verify_chain())
            finally:
                os.environ.pop("EPISTEMIC_BUILDS_DIR", None)
