#!/usr/bin/env python3
"""Async reconciler — proposes epistemic relationships between beliefs.

Runs strictly AFTER capture, never as part of it: capture appends beliefs with
no retrieval; this pass later examines the store and records how beliefs
relate (RESTATES / REFINES / QUALIFIES / SUPPORTS / CONTRADICTS / ...), each
pass as one atomic ReconciliationRun event.

Division of labor:
  - candidate generation (here) is mechanical: lexical similarity over claim
    text plus a same-cluster bonus. Cheap, local, no network.
  - judging what relationship actually holds is semantic interpretation, so
    the judge is an LLM client (any MCP surface) or a human. The judge's
    identity and the reconciler version are recorded in the canonical event —
    a judgment is an interpretation that a later pass may revise, layered,
    never erased.
  - UNRELATED is a first-class verdict: it records that a pair was examined
    so it is never proposed again.

A CONTRADICTS verdict records the relationship only — it does NOT contest the
target belief. Contesting is a stance-changing act reserved for an explicit
ContradictionRecorded decision by the owner or an anchor.

CLI:
    python reconciler.py candidates [--min-score 0.15] [--limit 20]
    python reconciler.py apply judgments.json --judge <name>

judgments.json: [{"subject_id": ..., "object_id": ..., "verdict":
"RESTATES|...|UNRELATED", "note": "why"}]  (subject is the newer belief)
"""
from __future__ import annotations

import json
import re
import sys

import engine
from substrate import RELATIONSHIPS

RECONCILER = "lexical-candidates"
VERSION = "1"

_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from",
    "has", "have", "in", "is", "it", "its", "not", "of", "on", "or", "s",
    "that", "the", "their", "there", "this", "to", "under", "was", "we",
    "were", "with", "yet",
}


def tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9~/.-]+", (text or "").lower())
            if t not in _STOP and len(t) > 1}


def similarity(a: dict, b: dict) -> float:
    ta, tb = tokens(a["claim"]), tokens(b["claim"])
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    bonus = 0.10 if (a.get("cluster") and a.get("cluster") == b.get("cluster")) else 0.0
    return round(jaccard + bonus, 4)


def candidates(min_score: float = 0.15, limit: int = 50) -> list[dict]:
    """Belief pairs worth judging: similar enough, not already related in
    either direction, not previously examined by any reconciliation run.
    Subject is the newer belief (the one that layers onto history)."""
    state = engine.get_log().state()
    views = [v for v, _ in engine.load_all()]  # active beliefs only
    related = {frozenset((r["subject_id"], r["object_id"]))
               for r in state["relationships"]}
    examined = {frozenset(p) for p in state["reconciled_pairs"]}
    out = []
    for i in range(len(views)):
        for j in range(i + 1, len(views)):
            a, b = views[i], views[j]
            pair = frozenset((a["id"], b["id"]))
            if pair in related or pair in examined:
                continue
            score = similarity(a, b)
            if score < min_score:
                continue
            newer, older = sorted(
                (a, b), key=lambda v: (v.get("observed_at") or "", v["id"]), reverse=True)
            out.append({
                "subject_id": newer["id"], "subject_claim": newer["claim"],
                "object_id": older["id"], "object_claim": older["claim"],
                "score": score, "cluster": newer.get("cluster"),
            })
    out.sort(key=lambda c: -c["score"])
    return out[:limit]


def apply(judgments: list[dict], judge: str, metadata: dict | None = None,
          actor: str = "owner") -> dict:
    """Record one reconciliation pass atomically, then refresh projections.
    Projection failure never un-does the canonical write."""
    engine.get_log().record_reconciliation(
        reconciler=RECONCILER, version=VERSION, judge=judge,
        judgments=judgments, metadata=metadata, actor=actor)
    summary = {
        "recorded": len(judgments),
        "relationships": sum(1 for j in judgments if j["verdict"] != "UNRELATED"),
        "unrelated": sum(1 for j in judgments if j["verdict"] == "UNRELATED"),
        "by_verdict": {},
    }
    for j in judgments:
        summary["by_verdict"][j["verdict"]] = summary["by_verdict"].get(j["verdict"], 0) + 1
    return engine._after_write(summary)


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="reconciler")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("candidates")
    c.add_argument("--min-score", type=float, default=0.15)
    c.add_argument("--limit", type=int, default=50)
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("--judge", required=True)
    args = p.parse_args()

    if args.cmd == "candidates":
        cands = candidates(args.min_score, args.limit)
        if not cands:
            print("no unexamined candidate pairs above threshold")
            return 0
        print(f"{len(cands)} candidate pairs (verdicts: {', '.join(RELATIONSHIPS)}, UNRELATED):")
        for c_ in cands:
            print(f"\n  score {c_['score']:.2f}  [{c_['cluster']}]")
            print(f"    subject {c_['subject_id']}: {c_['subject_claim']}")
            print(f"    object  {c_['object_id']}: {c_['object_claim']}")
    elif args.cmd == "apply":
        judgments = json.load(open(args.file))
        out = apply(judgments, judge=args.judge)
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
