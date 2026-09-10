"""Canonical event substrate for the epistemic memory server.

The append-only, hash-chained event log is the single source of truth; every
other representation (belief YAML, MEMORY.md, in-memory state) is a projection
derived from it.

Design invariants:
  1. The log never misrepresents how current state came to exist. Events are
     append-only; there is no update or delete path through this API.
  2. Evidence CONTENT lives outside the log in a content-addressed store,
     referenced by digest. The separation exists for future flexibility
     (restriction, externalization) — preservation is the default, and no
     deletion operation is implemented.
  3. Retirement, supersession, and contradiction are recorded states, not
     destruction. Retired/superseded beliefs and their full histories remain
     recoverable.
  4. Capture never requires retrieval or reconciliation. A belief write needs
     only its own envelope and the references in hand; relationship inference
     happens afterward as separate RelationshipRecorded events.
  5. The structural method firewall: observed/asserted beliefs require
     evidence references, derived/inferred beliefs require premise references.
     A write that cannot say where it came from is rejected unless explicitly
     marked unsupported.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METHODS = ("observed", "asserted", "derived", "inferred")
VOLATILITIES = ("historical", "structural", "preference", "metric", "status")

EVENT_TYPES = (
    "EvidenceRegistered",
    "BeliefFormed",
    "BeliefRestated",
    "VerificationRecorded",
    "ContradictionRecorded",
    "ContradictionResolved",
    "BeliefRetired",
    "RelationshipRecorded",
    "ReconciliationRun",
)

RELATIONSHIPS = (
    "RESTATES", "REFINES", "QUALIFIES", "SUPPORTS", "CONTRADICTS",
    "SUPERSEDES", "DEPENDS_ON", "DERIVED_FROM", "VERIFIES",
)

DURABILITIES = ("SNAPSHOTTED", "REFERENCED", "EPHEMERAL")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    sequence            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            TEXT NOT NULL UNIQUE,
    event_type          TEXT NOT NULL,
    actor               TEXT NOT NULL,
    recorded_at         TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    previous_event_hash TEXT,
    event_hash          TEXT NOT NULL
);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compute_event_hash(*, event_id: str, event_type: str, actor: str,
                       recorded_at: str, payload: dict, previous_event_hash: str | None) -> str:
    body = {
        "event_id": event_id,
        "event_type": event_type,
        "actor": actor,
        "recorded_at": recorded_at,
        "payload": payload,
        "previous_event_hash": previous_event_hash,
    }
    return "sha256:" + sha256_hex(canonical_json(body).encode("utf-8"))


class EvidenceStore:
    """Content-addressed evidence store, deliberately outside the event log.

    Files are named by their sha256 digest. Nothing here deletes; the store
    only writes and reads. Chain verification never depends on this store.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes) -> str:
        digest = sha256_hex(content)
        path = self.root / digest
        if not path.exists():
            path.write_bytes(content)
        return f"sha256:{digest}"

    def get(self, digest: str) -> bytes | None:
        hexpart = digest.removeprefix("sha256:")
        path = self.root / hexpart
        if not path.exists():
            return None
        content = path.read_bytes()
        if sha256_hex(content) != hexpart:
            raise ValueError(f"evidence store corruption: {digest}")
        return content

    def has(self, digest: str) -> bool:
        return (self.root / digest.removeprefix("sha256:")).exists()


class CanonicalLog:
    """The append-only canonical event log plus its derived state projection."""

    def __init__(self, db_path: str | Path, content_dir: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = EvidenceStore(content_dir)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- capture

    def register_evidence(self, *, media_type: str, uri: str | None = None,
                          content: bytes | str | None = None,
                          durability: str | None = None,
                          metadata: dict | None = None,
                          actor: str = "owner") -> str:
        if content is None and uri is None:
            raise ValueError("evidence needs content or a uri")
        digest = None
        size = None
        if content is not None:
            data = content.encode("utf-8") if isinstance(content, str) else content
            digest = self.store.put(data)
            size = len(data)
            durability = durability or "SNAPSHOTTED"
        else:
            durability = durability or "REFERENCED"
        if durability not in DURABILITIES:
            raise ValueError(f"durability '{durability}' not in {DURABILITIES}")
        if durability == "SNAPSHOTTED" and digest is None:
            raise ValueError("SNAPSHOTTED evidence requires content (digest)")
        evidence_id = new_id("evd")
        self._append("EvidenceRegistered", actor, {"evidence": {
            "evidence_id": evidence_id,
            "media_type": media_type,
            "uri": uri,
            "digest": digest,
            "size": size,
            "durability": durability,
            "metadata": metadata or {},
        }})
        return evidence_id

    def form_belief(self, *, belief_id: str, claim: str, method: str,
                    volatility: str, cluster: str | None = None,
                    anchor: str | None = None, anchor_cost: str | None = None,
                    evidence_ids: list[str] | None = None,
                    premise_ids: list[str] | None = None,
                    unsupported: bool = False,
                    observed_at: str | None = None,
                    metadata: dict | None = None,
                    actor: str = "owner") -> str:
        evidence_ids = list(evidence_ids or [])
        premise_ids = list(premise_ids or [])
        if method not in METHODS:
            raise ValueError(f"method '{method}' not in {METHODS}")
        if volatility not in VOLATILITIES:
            raise ValueError(f"volatility '{volatility}' not in {VOLATILITIES}")
        # Structural method firewall. NOTE: these checks use only the ids
        # passed in — capture never requires retrieval or reconciliation.
        if not unsupported:
            if method in ("observed", "asserted") and not evidence_ids:
                raise ValueError(
                    f"method '{method}' requires evidence_ids (or unsupported=True)")
            if method in ("derived", "inferred") and not premise_ids:
                raise ValueError(
                    f"method '{method}' requires premise_ids (or unsupported=True)")
        state = self.state()
        if belief_id in state["beliefs"]:
            raise ValueError(f"belief '{belief_id}' already exists — restate or relate, never overwrite")
        for eid in evidence_ids:
            if eid not in state["evidence"]:
                raise ValueError(f"unknown evidence: {eid}")
        for pid in premise_ids:
            if pid not in state["beliefs"]:
                raise ValueError(f"unknown premise belief: {pid}")
        self._append("BeliefFormed", actor, {"belief": {
            "belief_id": belief_id,
            "claim": claim,
            "method": method,
            "volatility": volatility,
            "cluster": cluster,
            "anchor": anchor,
            "anchor_cost": anchor_cost,
            "evidence_ids": evidence_ids,
            "premise_ids": premise_ids,
            "unsupported": unsupported,
            "observed_at": observed_at or now_iso()[:10],
            "metadata": metadata or {},
        }})
        return belief_id

    # ------------------------------------------------- later layers (append)

    def restate_belief(self, belief_id: str, new_claim: str, note: str = "",
                       actor: str = "owner") -> None:
        self._require_belief(belief_id)
        self._append("BeliefRestated", actor, {
            "belief_id": belief_id, "new_claim": new_claim, "note": note,
        })

    def record_verification(self, belief_id: str, verdict: str,
                            output: str | None = None, command: str | None = None,
                            actor: str = "owner") -> str | None:
        if verdict not in ("verified", "contradicted"):
            raise ValueError("verdict must be 'verified' or 'contradicted'")
        self._require_belief(belief_id)
        evidence_id = None
        if output is not None:
            evidence_id = self.register_evidence(
                media_type="text/plain",
                uri=f"anchor://{belief_id}",
                content=output,
                metadata={"command": command, "role": "verification-output"},
                actor=actor,
            )
        self._append("VerificationRecorded", actor, {
            "belief_id": belief_id, "verdict": verdict,
            "evidence_id": evidence_id, "command": command,
        })
        return evidence_id

    def record_contradiction(self, belief_id: str, contradicting_id: str,
                             note: str = "", actor: str = "owner") -> None:
        self._require_belief(belief_id)
        state = self.state()
        if contradicting_id not in state["beliefs"] and contradicting_id not in state["evidence"]:
            raise ValueError(f"unknown contradicting object: {contradicting_id}")
        self._append("ContradictionRecorded", actor, {
            "belief_id": belief_id, "contradicting_id": contradicting_id, "note": note,
        })

    def resolve_contradiction(self, belief_id: str, note: str,
                              actor: str = "owner") -> None:
        self._require_belief(belief_id)
        self._append("ContradictionResolved", actor, {
            "belief_id": belief_id, "note": note,
        })

    def retire_belief(self, belief_id: str, reason: str, actor: str = "owner") -> None:
        self._require_belief(belief_id)
        self._append("BeliefRetired", actor, {
            "belief_id": belief_id, "reason": reason,
        })

    def record_reconciliation(self, *, reconciler: str, version: str, judge: str,
                              judgments: list[dict], metadata: dict | None = None,
                              actor: str = "owner") -> str:
        """Record one reconciliation pass ATOMICALLY: every examined pair with
        its verdict — a relationship type or UNRELATED — in a single canonical
        event. UNRELATED verdicts matter: they record that the pair was
        examined, so it is never re-proposed. Relationships are projected from
        this event; judging is an interpretation and is recorded as one
        (reconciler, version, judge all in the payload) — a later pass may
        judge differently without erasing this one."""
        if not judgments:
            raise ValueError("empty reconciliation run — nothing to record")
        state = self.state()
        seen_pairs = set()
        for j in judgments:
            for key in ("subject_id", "object_id", "verdict"):
                if not j.get(key):
                    raise ValueError(f"judgment missing '{key}': {j}")
            if j["verdict"] not in RELATIONSHIPS and j["verdict"] != "UNRELATED":
                raise ValueError(f"verdict '{j['verdict']}' not in {RELATIONSHIPS} or UNRELATED")
            for bid in (j["subject_id"], j["object_id"]):
                if bid not in state["beliefs"]:
                    raise ValueError(f"unknown belief in judgment: {bid}")
            pair = frozenset((j["subject_id"], j["object_id"]))
            if len(pair) < 2:
                raise ValueError(f"judgment relates a belief to itself: {j['subject_id']}")
            if pair in seen_pairs:
                raise ValueError(f"duplicate pair in run: {sorted(pair)}")
            seen_pairs.add(pair)
        self._append("ReconciliationRun", actor, {"run": {
            "reconciler": reconciler,
            "version": version,
            "judge": judge,
            "judgments": [
                {"subject_id": j["subject_id"], "object_id": j["object_id"],
                 "verdict": j["verdict"], "note": j.get("note", "")}
                for j in judgments
            ],
            "metadata": metadata or {},
        }})
        return f"{reconciler}@{version}"

    def record_relationship(self, subject_id: str, rel: str, object_id: str,
                            note: str = "", method: str = "derived",
                            actor: str = "owner") -> None:
        if rel not in RELATIONSHIPS:
            raise ValueError(f"relationship '{rel}' not in {RELATIONSHIPS}")
        self._require_belief(subject_id)
        self._require_belief(object_id)
        self._append("RelationshipRecorded", actor, {"relationship": {
            "subject_id": subject_id, "rel": rel, "object_id": object_id,
            "note": note, "method": method,
        }})

    # ---------------------------------------------------------- projections

    def state(self) -> dict:
        """Replay all events into current state. Purely derived."""
        beliefs: dict[str, dict] = {}
        evidence: dict[str, dict] = {}
        relationships: list[dict] = []
        reconciled_pairs: list[list[str]] = []
        for ev in self.events():
            t, p, at = ev["event_type"], ev["payload"], ev["recorded_at"]
            if t == "EvidenceRegistered":
                evidence[p["evidence"]["evidence_id"]] = dict(p["evidence"], recorded_at=at)
            elif t == "BeliefFormed":
                b = dict(p["belief"])
                b.update(contested=False, retired=False, retired_reason=None,
                         verified_at=None, recorded_at=at,
                         claim_history=[{"claim": b["claim"], "at": at, "origin": "formed"}],
                         history=[])
                beliefs[b["belief_id"]] = b
            elif t == "BeliefRestated":
                b = beliefs[p["belief_id"]]
                b["claim"] = p["new_claim"]
                b["claim_history"].append({"claim": p["new_claim"], "at": at,
                                           "origin": "restated", "note": p.get("note", "")})
            elif t == "VerificationRecorded":
                b = beliefs[p["belief_id"]]
                if p["verdict"] == "verified":
                    b["verified_at"] = at
                    b["contested"] = False
                else:
                    b["contested"] = True
            elif t == "ContradictionRecorded":
                beliefs[p["belief_id"]]["contested"] = True
            elif t == "ContradictionResolved":
                beliefs[p["belief_id"]]["contested"] = False
            elif t == "BeliefRetired":
                b = beliefs[p["belief_id"]]
                b["retired"] = True
                b["retired_reason"] = p["reason"]
            elif t == "RelationshipRecorded":
                relationships.append(dict(p["relationship"], recorded_at=at))
            elif t == "ReconciliationRun":
                run = p["run"]
                proposer = f"{run['reconciler']}@{run['version']}"
                for j in run["judgments"]:
                    reconciled_pairs.append(sorted([j["subject_id"], j["object_id"]]))
                    if j["verdict"] != "UNRELATED":
                        relationships.append({
                            "subject_id": j["subject_id"], "rel": j["verdict"],
                            "object_id": j["object_id"], "note": j.get("note", ""),
                            "method": "derived", "proposer": proposer,
                            "judge": run["judge"], "recorded_at": at,
                        })
            if t not in ("EvidenceRegistered", "RelationshipRecorded", "ReconciliationRun"):
                bid = p.get("belief_id") or p.get("belief", {}).get("belief_id")
                if bid and bid in beliefs:
                    beliefs[bid]["history"].append(
                        {"sequence": ev["sequence"], "event_type": t, "at": at, "payload": p})
        return {"beliefs": beliefs, "evidence": evidence,
                "relationships": relationships, "reconciled_pairs": reconciled_pairs}

    def why(self, belief_id: str, _seen: set[str] | None = None) -> dict:
        """Provenance walk: belief -> formation evidence and verification
        evidence (with content availability), recursively into premises."""
        seen = _seen or set()
        if belief_id in seen:
            return {"belief_id": belief_id, "cycle": True}
        seen.add(belief_id)
        state = self.state()
        b = state["beliefs"].get(belief_id)
        if b is None:
            raise ValueError(f"no belief '{belief_id}'")

        def enrich(eid):
            e = state["evidence"][eid]
            return dict(e, content_available=bool(e["digest"] and self.store.has(e["digest"])))

        ev = [enrich(eid) for eid in b["evidence_ids"]]
        verifications = [
            {"verdict": h["payload"]["verdict"], "at": h["at"],
             "command": h["payload"].get("command"),
             "evidence": enrich(h["payload"]["evidence_id"])
                         if h["payload"].get("evidence_id") else None}
            for h in b["history"] if h["event_type"] == "VerificationRecorded"
        ]
        return {
            "belief_id": belief_id,
            "claim": b["claim"],
            "method": b["method"],
            "unsupported": b.get("unsupported", False),
            "evidence": ev,
            "verifications": verifications,
            "premises": [self.why(pid, seen) for pid in b["premise_ids"]],
        }

    def evidence_content(self, evidence_id: str) -> bytes | None:
        e = self.state()["evidence"].get(evidence_id)
        if e is None:
            raise ValueError(f"no evidence '{evidence_id}'")
        return self.store.get(e["digest"]) if e["digest"] else None

    # ------------------------------------------------------ log fundamentals

    def events(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        return [{
            "sequence": r["sequence"], "event_id": r["event_id"],
            "event_type": r["event_type"], "actor": r["actor"],
            "recorded_at": r["recorded_at"], "payload": json.loads(r["payload_json"]),
            "previous_event_hash": r["previous_event_hash"], "event_hash": r["event_hash"],
        } for r in rows]

    def verify_chain(self) -> bool:
        previous = None
        for ev in self.events():
            expected = compute_event_hash(
                event_id=ev["event_id"], event_type=ev["event_type"],
                actor=ev["actor"], recorded_at=ev["recorded_at"],
                payload=ev["payload"], previous_event_hash=previous,
            )
            if ev["previous_event_hash"] != previous or ev["event_hash"] != expected:
                return False
            previous = ev["event_hash"]
        return True

    def replay_into(self, new_db_path: str | Path) -> "CanonicalLog":
        """Reconstruct a brand-new database from canonical events alone,
        verifying the chain while importing. Shares the evidence store."""
        target_path = Path(new_db_path)
        if target_path.exists():
            target_path.unlink()
        target = CanonicalLog(target_path, self.store.root)
        previous = None
        expected_seq = 0
        for ev in self.events():
            expected_seq += 1
            if ev["sequence"] != expected_seq:
                raise ValueError("canonical event sequence gap during replay")
            expected = compute_event_hash(
                event_id=ev["event_id"], event_type=ev["event_type"],
                actor=ev["actor"], recorded_at=ev["recorded_at"],
                payload=ev["payload"], previous_event_hash=ev["previous_event_hash"],
            )
            if ev["previous_event_hash"] != previous or expected != ev["event_hash"]:
                raise ValueError("canonical event chain mismatch during replay")
            previous = ev["event_hash"]
            target.conn.execute(
                """INSERT INTO events(sequence, event_id, event_type, actor,
                   recorded_at, payload_json, previous_event_hash, event_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ev["sequence"], ev["event_id"], ev["event_type"], ev["actor"],
                 ev["recorded_at"], canonical_json(ev["payload"]),
                 ev["previous_event_hash"], ev["event_hash"]),
            )
        target.conn.commit()
        return target

    # ------------------------------------------------------------- internals

    def _require_belief(self, belief_id: str) -> None:
        if belief_id not in self.state()["beliefs"]:
            raise ValueError(f"no belief '{belief_id}'")

    def _append(self, event_type: str, actor: str, payload: dict) -> str:
        assert event_type in EVENT_TYPES
        try:
            # Acquire the write lock before reading the chain head.
            self.conn.execute("BEGIN IMMEDIATE")
            last = self.conn.execute(
                "SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
            previous = last["event_hash"] if last else None
            event_id = new_id("evt")
            recorded_at = now_iso()
            event_hash = compute_event_hash(
                event_id=event_id, event_type=event_type, actor=actor,
                recorded_at=recorded_at, payload=payload, previous_event_hash=previous,
            )
            self.conn.execute(
                """INSERT INTO events(event_id, event_type, actor, recorded_at,
                   payload_json, previous_event_hash, event_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_id, event_type, actor, recorded_at,
                 canonical_json(payload), previous, event_hash),
            )
            self.conn.commit()
            return event_id
        except Exception:
            self.conn.rollback()
            raise
