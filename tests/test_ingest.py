"""Acceptance tests for stream ingestion.

Invariants: the cursor is derived from canonical history (no state file);
staging is preservation-first (snapshot kept even when extraction yields
nothing); applied beliefs are grounded by construction; duplicates and bad
proposals never block the rest; replay reconstructs ingestion state.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
import ingest  # noqa: E402

ENV_KEYS = ("EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
            "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")

EXPORT = [
    {
        "uuid": "conv-newer",
        "name": "Diet discussion",
        "created_at": "2026-05-09T10:00:00Z",
        "updated_at": "2026-05-09T10:30:00Z",
        "chat_messages": [
            {"sender": "human", "text": "I'm taking DAO enzyme and following a low histamine diet",
             "created_at": "2026-05-09T10:00:00Z"},
            {"sender": "assistant", "text": "Noted!", "created_at": "2026-05-09T10:01:00Z"},
        ],
    },
    {
        "uuid": "conv-older",
        "name": "Small talk",
        "created_at": "2026-01-01T09:00:00Z",
        "updated_at": "2026-01-01T09:05:00Z",
        "chat_messages": [
            {"sender": "human", "text": "hello there", "created_at": "2026-01-01T09:00:00Z"},
        ],
    },
    {
        "uuid": "conv-empty",
        "name": "Assistant only",
        "created_at": "2026-02-01T09:00:00Z",
        "updated_at": "2026-02-01T09:00:00Z",
        "chat_messages": [{"sender": "assistant", "text": "no human text here"}],
    },
]


class IngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["EPISTEMIC_DB_PATH"] = str(root / "canonical.db")
        os.environ["EPISTEMIC_CONTENT_DIR"] = str(root / "evidence_store")
        os.environ["EPISTEMIC_BELIEFS_DIR"] = str(root / "beliefs")
        os.environ["EPISTEMIC_EVENTS_JSONL"] = str(root / "events.jsonl")
        os.environ["EPISTEMIC_INDEX_PATH"] = str(root / "MEMORY.md")
        self.export = root / "conversations.json"
        self.export.write_text(json.dumps(EXPORT))

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

    # -- pending / cursor ------------------------------------------------------

    def test_pending_is_newest_first_and_skips_humanless_items(self):
        items = ingest.pending(str(self.export))
        self.assertEqual([i["uuid"] for i in items], ["conv-newer", "conv-older"])

    def test_cursor_is_derived_from_canonical_history(self):
        ingest.stage(str(self.export), limit=1)  # stages conv-newer
        remaining = ingest.pending(str(self.export))
        self.assertEqual([i["uuid"] for i in remaining], ["conv-older"])
        # Replay into a fresh db: the cursor survives because it IS history.
        log = engine.get_log()
        fresh = log.replay_into(Path(self.tmp.name) / "fresh.db")
        try:
            self.assertEqual(fresh.state(), log.state())
        finally:
            fresh.close()

    def test_staging_is_preservation_first(self):
        staged = ingest.stage(str(self.export), limit=2)
        self.assertEqual(len(staged), 2)
        log = engine.get_log()
        for s in staged:
            e = log.state()["evidence"][s["evidence_id"]]
            self.assertEqual(e["durability"], "SNAPSHOTTED")
            self.assertEqual(e["metadata"]["role"], "stream-item")
            self.assertIsNotNone(log.evidence_content(s["evidence_id"]))
        # An item that yields no beliefs stays ingested — never re-staged.
        self.assertEqual(ingest.pending(str(self.export)), [])

    # -- apply -----------------------------------------------------------------

    def _stage_and_apply(self):
        staged = ingest.stage(str(self.export), limit=1)
        eid = staged[0]["evidence_id"]
        return eid, ingest.apply([
            {"evidence_id": eid, "belief_id": "chat-dao-enzyme",
             "claim": "Josh takes DAO enzyme supplements", "volatility": "preference",
             "cluster": "Health", "observed_at": "2026-05-09"},
            {"evidence_id": eid, "belief_id": "chat-low-histamine",
             "claim": "Josh follows a low-histamine diet", "volatility": "preference",
             "cluster": "Health", "observed_at": "2026-05-09"},
        ], extractor="test-extractor")

    def test_apply_captures_grounded_beliefs(self):
        eid, out = self._stage_and_apply()
        self.assertEqual(sorted(out["captured"]), ["chat-dao-enzyme", "chat-low-histamine"])
        why = engine.why("chat-dao-enzyme")
        self.assertFalse(why["unsupported"])  # grounded by construction
        self.assertEqual(why["evidence"][0]["evidence_id"], eid)
        content = engine.get_log().evidence_content(eid).decode()
        self.assertIn("low histamine diet", content)  # provenance reaches the chat text
        v, _ = engine.find("chat-dao-enzyme")
        self.assertEqual(v["method"], "asserted")

    def test_duplicates_skipped_and_bad_proposals_dont_block(self):
        eid, _ = self._stage_and_apply()
        out = ingest.apply([
            {"evidence_id": eid, "belief_id": "chat-dao-enzyme",  # duplicate
             "claim": "x", "volatility": "preference", "cluster": "Health"},
            {"evidence_id": "evd_ghost", "belief_id": "chat-bad",  # bad evidence
             "claim": "x", "volatility": "preference", "cluster": "Health"},
            {"evidence_id": eid, "belief_id": "chat-new-fact",  # fine
             "claim": "A further valid claim", "volatility": "status", "cluster": "Health"},
        ], extractor="test-extractor")
        self.assertEqual(out["skipped_existing"], ["chat-dao-enzyme"])
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["captured"], ["chat-new-fact"])

    def test_extractor_recorded_in_provenance(self):
        self._stage_and_apply()
        v, _ = engine.find("chat-low-histamine")
        raw = engine.get_log().state()["beliefs"]["chat-low-histamine"]
        self.assertEqual(raw["metadata"]["extractor"], "test-extractor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
