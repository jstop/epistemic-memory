"""Authorship tests: who wrote an event is a property of the channel that
opened the log, never of a request payload; misattributed history is
corrected by appending, never by rewriting; reads always say whose word a
belief is.

Background: before this, every write path defaulted to actor "owner", so an
agent capturing through the MCP server was recorded as the owner speaking.
The record could not tell the owner's word from a machine's draft of it.
"""
import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
from substrate import OWNER, CanonicalLog, effective_actor  # noqa: E402

ENV_KEYS = ("EPISTEMIC_ACTOR", "EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR",
            "EPISTEMIC_BELIEFS_DIR", "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH")


class SubstrateAttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _log(self, actor="agent:test"):
        return CanonicalLog(self.root / "canonical.db", self.root / "evidence", actor=actor)

    def _belief(self, log, bid="b-1", claim="I prefer tabs."):
        eid = log.register_evidence(media_type="text/plain", content=b"src", uri="test://s")
        log.form_belief(belief_id=bid, claim=claim, method="asserted",
                        volatility="preference", evidence_ids=[eid])
        return bid

    def test_log_must_be_opened_as_a_named_actor(self):
        with self.assertRaises(TypeError):
            CanonicalLog(self.root / "c.db", self.root / "e")  # no actor at all
        with self.assertRaises(ValueError):
            CanonicalLog(self.root / "c.db", self.root / "e", actor="  ")

    def test_no_write_method_accepts_an_actor(self):
        """The payload can never name the principal."""
        log = self._log()
        with self.assertRaises(TypeError):
            log.register_evidence(media_type="text/plain", content=b"x", actor=OWNER)
        with self.assertRaises(TypeError):
            log.form_belief(belief_id="b", claim="c", method="asserted",
                            volatility="preference", unsupported=True, actor=OWNER)
        log.close()

    def test_every_event_carries_the_handle_principal(self):
        log = self._log("agent:claude-code")
        bid = self._belief(log)
        log.restate_belief(bid, "I prefer tabs, strongly.")
        log.retire_belief(bid, "test")
        self.assertEqual({ev["actor"] for ev in log.events()}, {"agent:claude-code"})
        log.close()

    def test_authorship_projected_on_beliefs_and_history(self):
        log = self._log("agent:claude-code")
        bid = self._belief(log)
        b = log.state()["beliefs"][bid]
        self.assertEqual(b["authorship"], {
            "composed_by": "agent:claude-code", "recorded_as": "agent:claude-code",
            "corrected": False, "stood_behind_by": None})
        self.assertEqual(b["claim_history"][0]["actor"], "agent:claude-code")
        log.close()
        owner_log = self._log(OWNER)
        owner_log.restate_belief(bid, "I prefer tabs.")
        b = owner_log.state()["beliefs"][bid]
        self.assertEqual(b["authorship"]["composed_by"], OWNER)
        self.assertEqual(b["authorship"]["stood_behind_by"], OWNER)
        self.assertEqual(b["claim_history"][-1]["actor"], OWNER)
        self.assertEqual(b["history"][-1]["actor"], OWNER)
        self.assertEqual(owner_log.why(bid)["authorship"]["stood_behind_by"], OWNER)
        owner_log.close()

    def test_correction_is_appended_and_projected_not_rewritten(self):
        # The historical failure: an agent wrote as the owner.
        misattributed = self._log(OWNER)
        self._belief(misattributed, "b-old", "The sky was overcast.")
        head = len(misattributed.events())
        misattributed.close()
        # Then the fix: the owner says so, from their own channel.
        owner = self._log(OWNER)
        owner.correct_attribution(through_sequence=head, recorded_actor=OWNER,
                                  actual_actor="agent:claude-code",
                                  reason="captured through the MCP channel before actors were channel-derived")
        # and a belief written after the cutover is genuinely the owner's.
        self._belief(owner, "b-new", "The sky is clear.")
        st = owner.state()
        old, new = st["beliefs"]["b-old"], st["beliefs"]["b-new"]
        self.assertEqual(old["authorship"], {
            "composed_by": "agent:claude-code", "recorded_as": OWNER,
            "corrected": True, "stood_behind_by": None})
        self.assertEqual(new["authorship"]["composed_by"], OWNER)
        self.assertEqual(new["authorship"]["stood_behind_by"], OWNER)
        self.assertEqual(len(st["attribution_corrections"]), 1)
        # raw column untouched; chain intact; replay identical
        raw = [ev["actor"] for ev in owner.events()]
        self.assertEqual(raw[:head], [OWNER] * head)
        self.assertTrue(owner.verify_chain())
        replayed = owner.replay_into(self.root / "replay.db")
        self.assertEqual(replayed.state()["beliefs"]["b-old"]["authorship"], old["authorship"])
        self.assertEqual(replayed.events(), owner.events())
        replayed.close()
        owner.close()

    def test_correction_range_and_arguments_are_validated(self):
        log = self._log(OWNER)
        self._belief(log)
        head = len(log.events())
        for kwargs in (
            dict(through_sequence=head + 1, recorded_actor=OWNER, actual_actor="agent:x", reason="r"),
            dict(through_sequence=0, recorded_actor=OWNER, actual_actor="agent:x", reason="r"),
            dict(through_sequence=head, from_sequence=head + 1, recorded_actor=OWNER,
                 actual_actor="agent:x", reason="r"),
            dict(through_sequence=head, recorded_actor=OWNER, actual_actor=OWNER, reason="r"),
            dict(through_sequence=head, recorded_actor=OWNER, actual_actor="agent:x", reason=" "),
            dict(through_sequence=head, recorded_actor="", actual_actor="agent:x", reason="r"),
        ):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                log.correct_attribution(**kwargs)
        self.assertEqual(len(log.events()), head)  # nothing appended
        log.close()

    def test_corrections_apply_in_order_and_only_backward(self):
        c1 = {"sequence": 10, "from_sequence": 1, "through_sequence": 9,
              "recorded_actor": "owner", "actual_actor": "agent:a"}
        c2 = {"sequence": 20, "from_sequence": 1, "through_sequence": 15,
              "recorded_actor": "agent:a", "actual_actor": "agent:b"}
        ev = lambda seq, actor="owner": {"sequence": seq, "actor": actor}  # noqa: E731
        self.assertEqual(effective_actor(ev(5), [c1, c2]), "agent:b")   # chained
        self.assertEqual(effective_actor(ev(12), [c1, c2]), "owner")    # c1 range ends at 9
        self.assertEqual(effective_actor(ev(12, "agent:a"), [c1, c2]), "agent:b")
        self.assertEqual(effective_actor(ev(25), [c1, c2]), "owner")    # after both
        self.assertEqual(effective_actor(ev(10), [c1]), "owner")        # not itself


class ChannelResolutionTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self._channel = engine.CHANNEL_ACTOR
        os.environ.pop("EPISTEMIC_ACTOR", None)
        engine.CHANNEL_ACTOR = None

    def tearDown(self):
        engine.CHANNEL_ACTOR = self._channel
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_channel_declaration_wins(self):
        engine.CHANNEL_ACTOR = "agent:claude-code"
        os.environ["EPISTEMIC_ACTOR"] = "owner"
        self.assertEqual(engine.resolve_actor(), "agent:claude-code")

    def test_env_names_a_non_owner_actor(self):
        os.environ["EPISTEMIC_ACTOR"] = "test:fixture"
        self.assertEqual(engine.resolve_actor(), "test:fixture")

    def test_env_cannot_claim_owner_without_a_terminal(self):
        os.environ["EPISTEMIC_ACTOR"] = "owner"
        with patch.object(sys, "stdin", io.StringIO()):
            self.assertEqual(engine.resolve_actor(), "agent:cli")

    def test_interactive_terminal_is_the_owner(self):
        class TTY(io.StringIO):
            def isatty(self):
                return True
        with patch.object(sys, "stdin", TTY()):
            self.assertEqual(engine.resolve_actor(), OWNER)
        with patch.object(sys, "stdin", io.StringIO()):
            self.assertEqual(engine.resolve_actor(), "agent:cli")

    def test_agent_actor_never_normalizes_to_owner(self):
        self.assertEqual(engine.agent_actor("claude-code"), "agent:claude-code")
        self.assertEqual(engine.agent_actor("agent:desktop"), "agent:desktop")
        self.assertEqual(engine.agent_actor(None), "agent:unknown")
        self.assertEqual(engine.agent_actor("owner"), "agent:unknown")

    def test_server_import_declares_the_agent_channel(self):
        os.environ["EPISTEMIC_AGENT"] = "owner"  # even a hostile name
        sys.modules.pop("server", None)
        try:
            import server  # noqa: F401
        except ImportError as e:  # mcp package absent in this environment
            self.skipTest(f"server import unavailable: {e}")
        self.assertEqual(engine.CHANNEL_ACTOR, "agent:unknown")
        sys.modules.pop("server", None)
        os.environ.pop("EPISTEMIC_AGENT", None)


class EngineAttributionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        self._channel = engine.CHANNEL_ACTOR
        engine.CHANNEL_ACTOR = None
        os.environ["EPISTEMIC_ACTOR"] = "agent:test"
        os.environ["EPISTEMIC_DB_PATH"] = str(root / "canonical.db")
        os.environ["EPISTEMIC_CONTENT_DIR"] = str(root / "evidence_store")
        os.environ["EPISTEMIC_BELIEFS_DIR"] = str(root / "beliefs")
        os.environ["EPISTEMIC_EVENTS_JSONL"] = str(root / "events.jsonl")
        os.environ["EPISTEMIC_INDEX_PATH"] = str(root / "MEMORY.md")

    def tearDown(self):
        for log in engine._LOGS.values():
            log.close()
        engine._LOGS.clear()
        engine.CHANNEL_ACTOR = self._channel
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def _capture(self, bid="b-1"):
        return engine.capture({"id": bid, "claim": "I like tea.", "method": "asserted",
                               "volatility": "preference", "cluster": "test"},
                              evidence_content="I like tea.")

    def test_capture_is_recorded_under_the_channel_actor(self):
        out = self._capture()
        self.assertEqual(out["authorship"]["composed_by"], "agent:test")
        self.assertIsNone(out["authorship"]["stood_behind_by"])
        self.assertEqual({ev["actor"] for ev in engine.get_log().events()}, {"agent:test"})

    def test_reads_carry_authorship(self):
        self._capture()
        v, _ = engine.find("b-1")
        self.assertEqual(v["authorship"]["composed_by"], "agent:test")
        self.assertEqual(v["events"][0]["actor"], "agent:test")
        self.assertEqual(engine.stamped(v)["authorship"]["composed_by"], "agent:test")
        self.assertEqual(engine.why("b-1")["authorship"]["composed_by"], "agent:test")

    def test_health_census_counts_composers_and_owner_backing(self):
        self._capture()
        census = engine.health()["authorship"]
        self.assertEqual(census["beliefs_by_composer"], {"agent:test": 1})
        self.assertEqual(census["stood_behind_by_owner"], 0)
        self.assertEqual(census["writing_as"], "agent:test")
        self.assertEqual(census["attribution_corrections"], [])

    def test_attribution_correction_is_owner_only(self):
        self._capture()
        with self.assertRaises(PermissionError):
            engine.correct_attribution(1, "agent:test", "agent:other", "not my call")
        engine.CHANNEL_ACTOR = OWNER  # the owner's channel
        out = engine.correct_attribution(2, "agent:test", "agent:other", "it was the other one")
        self.assertEqual(out["beliefs_by_composer"], {"agent:other": 1})
        self.assertEqual(len(out["attribution_corrections"]), 1)
        self.assertEqual(out["writing_as"], OWNER)

    def test_get_log_is_keyed_by_principal(self):
        a = engine.get_log()
        engine.CHANNEL_ACTOR = OWNER
        b = engine.get_log()
        self.assertIsNot(a, b)
        self.assertEqual((a.actor, b.actor), ("agent:test", OWNER))


if __name__ == "__main__":
    unittest.main()
