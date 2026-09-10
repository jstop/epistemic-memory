#!/usr/bin/env python3
"""Stream ingestion — feed the living library from a data stream.

First source adapter: a Claude chat-history export (conversations.json).
The design generalizes: an item is anything with a stable URI and content.

The flow keeps the library's division of labor:
  - STAGE (mechanical, here): find stream items not yet in canonical history,
    snapshot each as content-addressed evidence. Registering the evidence IS
    the ingestion cursor — an item is 'ingested' iff its URI has evidence, so
    incrementality is derived from canonical history itself, needs no state
    file, and survives replay. Preservation-first: the snapshot is kept even
    if extraction later finds nothing worth believing in it.
  - EXTRACT (semantic, an LLM client or human): read each staged item and
    propose atomic natural-language beliefs — claims worth remembering, in
    the belief envelope, each grounded in the staged evidence.
  - APPLY (mechanical, here): capture the proposals as BeliefFormed events
    referencing their evidence. Grounded by construction — never unsupported,
    never fabricated. Capture stays independent of reconciliation: relating
    new beliefs to old ones is the reconciler's later pass.

Extraction guidance for the judge: only durable, personally relevant, atomic
claims (one fact per belief); statements the owner made are method 'asserted';
skip pleasantries, transient tasks, and anything the repo/git already records.

CLI:
    python ingest.py pending <export.json> [--limit N]
    python ingest.py stage   <export.json> [--limit N] [--out staged.json]
    python ingest.py apply   proposals.json --extractor <name>

proposals.json: [{"evidence_id", "belief_id", "claim", "volatility",
                  "cluster", "method"?, "observed_at"?, "note"?}]
"""
from __future__ import annotations

import json
import sys

import engine
from substrate import canonical_json

SOURCE = "claude-export"
STAGE_EXCERPT_CHARS = 3500


def item_uri(conversation_uuid: str) -> str:
    return f"claude-export://conversation/{conversation_uuid}"


def read_export(path: str) -> list[dict]:
    """Stream items from a Claude export, most recently updated first."""
    data = json.load(open(path))
    items = []
    for c in data:
        humans = [
            {"at": (m.get("created_at") or "")[:10], "text": m.get("text") or ""}
            for m in c.get("chat_messages", [])
            if m.get("sender") == "human" and m.get("text")
        ]
        if not humans:
            continue
        items.append({
            "uri": item_uri(c["uuid"]),
            "uuid": c["uuid"],
            "name": c.get("name") or "(untitled)",
            "created_at": (c.get("created_at") or "")[:10],
            "updated_at": (c.get("updated_at") or "")[:10],
            "human_messages": humans,
        })
    items.sort(key=lambda i: i["updated_at"], reverse=True)
    return items


def ingested_uris() -> set[str]:
    """The cursor, derived from canonical history: URIs that have evidence."""
    state = engine.get_log().state()
    return {e["uri"] for e in state["evidence"].values() if e.get("uri")}


def pending(path: str, limit: int = 10) -> list[dict]:
    done = ingested_uris()
    return [i for i in read_export(path) if i["uri"] not in done][:limit]


def stage(path: str, limit: int = 10, actor: str = "owner") -> list[dict]:
    """Snapshot pending items as evidence; return them for extraction."""
    log = engine.get_log()
    staged = []
    for item in pending(path, limit):
        content = canonical_json({
            "uuid": item["uuid"], "name": item["name"],
            "created_at": item["created_at"], "updated_at": item["updated_at"],
            "human_messages": item["human_messages"],
        })
        evidence_id = log.register_evidence(
            media_type="application/json",
            uri=item["uri"],
            content=content,
            metadata={"role": "stream-item", "source": SOURCE,
                      "name": item["name"], "updated_at": item["updated_at"]},
            actor=actor,
        )
        text = "\n".join(f"[{m['at']}] {m['text']}" for m in item["human_messages"])
        staged.append({
            "evidence_id": evidence_id,
            "uri": item["uri"],
            "name": item["name"],
            "date": item["created_at"],
            "excerpt": text[:STAGE_EXCERPT_CHARS],
        })
    return staged


def apply(proposals: list[dict], extractor: str, actor: str = "owner") -> dict:
    """Capture extraction proposals as grounded beliefs. Every proposal must
    reference staged evidence; duplicates are skipped and reported, and one
    bad proposal never blocks the rest (each capture is its own event)."""
    log = engine.get_log()
    state = log.state()
    captured, skipped, errors = [], [], []
    for p in proposals:
        try:
            for key in ("evidence_id", "belief_id", "claim", "volatility", "cluster"):
                if not p.get(key):
                    raise ValueError(f"proposal missing '{key}'")
            if p["evidence_id"] not in state["evidence"]:
                raise ValueError(f"unknown evidence: {p['evidence_id']}")
            if p["belief_id"] in state["beliefs"]:
                skipped.append(p["belief_id"])
                continue
            log.form_belief(
                belief_id=p["belief_id"],
                claim=p["claim"],
                method=p.get("method", "asserted"),
                volatility=p["volatility"],
                cluster=p["cluster"],
                evidence_ids=[p["evidence_id"]],
                observed_at=p.get("observed_at"),
                metadata={"extractor": extractor, "source": SOURCE,
                          "note": p.get("note", "")},
                actor=actor,
            )
            state = log.state()
            captured.append(p["belief_id"])
        except ValueError as e:
            errors.append(str(e))
    result = {"captured": captured, "skipped_existing": skipped, "errors": errors,
              "next_step": "run a reconciliation pass to relate new beliefs to history"}
    return engine._after_write(result)


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="ingest")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("pending", "stage"):
        s = sub.add_parser(name)
        s.add_argument("export")
        s.add_argument("--limit", type=int, default=10)
        if name == "stage":
            s.add_argument("--out", default="")
    a = sub.add_parser("apply")
    a.add_argument("file")
    a.add_argument("--extractor", required=True)
    args = p.parse_args()

    if args.cmd == "pending":
        items = pending(args.export, args.limit)
        print(f"{len(items)} pending items (of stream, newest first):")
        for i in items:
            print(f"  {i['updated_at']}  {i['name'][:60]}  ({i['uri']})")
    elif args.cmd == "stage":
        staged = stage(args.export, args.limit)
        if args.out:
            with open(args.out, "w") as fh:
                json.dump(staged, fh, indent=1, ensure_ascii=False)
            print(f"staged {len(staged)} items -> {args.out}")
        else:
            print(json.dumps(staged, indent=1, ensure_ascii=False))
    elif args.cmd == "apply":
        out = apply(json.load(open(args.file)), extractor=args.extractor)
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
