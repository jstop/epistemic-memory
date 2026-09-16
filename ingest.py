#!/usr/bin/env python3
"""Stream ingestion — feed the living library from a data stream.

First source adapter: a Claude chat-history export (conversations.json).
The design generalizes: an item is anything with a stable URI and content.

The flow keeps the library's division of labor:
  - STAGE (mechanical, here): find stream items not yet in canonical history,
    snapshot each as content-addressed evidence. Registering the evidence IS
    the ingestion cursor — a version is ingested iff its URI and digest exist, so
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
claims (one fact per belief); skip pleasantries, transient tasks, and anything
the repo/git already records.

ATTRIBUTION — the hard part. A human-sender message is NOT automatically the
owner's own assertion. Human turns routinely contain pasted material: AI
output from another session, other people's emails, articles, Reddit/LinkedIn
posts, drafts written with an AI. Before attributing a claim to the owner,
decide who authored the words: (a) the owner's own statement -> 'asserted';
(b) pasted third-party text -> a belief ABOUT that party, or skip; (c) pasted
AI-drafted text -> not the owner's position unless they explicitly endorse it
in their own words; (d) the owner's answers to Q/A prompts are their own.
When unsure, phrase the claim as what the owner DID ('pasted/considered X')
rather than what they BELIEVE.

CLI:
    python ingest.py pending <export.json> [--limit N]
    python ingest.py stage   <export.json> [--limit N] [--out staged.json]
    python ingest.py apply   proposals.json --extractor <name>

proposals.json: [{"evidence_id", "belief_id", "claim", "volatility", "cluster",
                  "method"?, "observed_at"?, "note"?,
                  "grounding"?: [{"quote", "message"?, "evidence_id"?}]}]
grounding spans are provenance facts: each quote must occur verbatim in the
evidence; the literal author (message sender) is derived on read. Prefer to
ground every proposal — it is what lets a reviewer see exactly what a belief
rests on. What a span MEANS (speech act, endorsement, whether an action
happened) is interpretation, not recorded here.
"""
from __future__ import annotations

import json
import sys

import engine
from substrate import canonical_json, sha256_hex

SOURCE = "claude-export"
STAGE_EXCERPT_CHARS = 3500


def item_uri(conversation_uuid: str) -> str:
    return f"claude-export://conversation/{conversation_uuid}"


def read_export(path: str) -> list[dict]:
    """Stream items from a Claude export, most recently updated first."""
    data = json.load(open(path))
    items = []
    for c in data:
        messages = [
            {"role": m.get("sender") or "?", "at": (m.get("created_at") or "")[:10],
             "text": m.get("text") or ""}
            for m in c.get("chat_messages", []) if m.get("text")
        ]
        humans = [{"at": m["at"], "text": m["text"]} for m in messages if m["role"] == "human"]
        if not humans:
            continue
        items.append({
            "uri": item_uri(c["uuid"]),
            "uuid": c["uuid"],
            "name": c.get("name") or "(untitled)",
            "created_at": (c.get("created_at") or "")[:10],
            "updated_at": (c.get("updated_at") or "")[:10],
            "messages": messages,
            "human_messages": humans,
        })
    items.sort(key=lambda i: i["updated_at"], reverse=True)
    return items


def full_transcript(item: dict) -> dict:
    """The complete conversation, both roles — what gets snapshotted."""
    return {
        "uuid": item["uuid"], "name": item["name"],
        "created_at": item["created_at"], "updated_at": item["updated_at"],
        "messages": item["messages"],
    }


def resnapshot(path: str) -> dict:
    """Preservation repair: earlier staging snapshotted only human-sender
    messages. Register the FULL transcript as additional evidence for every
    conversation that lacks one (same URI; metadata.supersedes points at the
    partial snapshot). Nothing is modified or removed; the cursor is unchanged."""
    log = engine.get_log()
    state = log.state()
    have_full = {e["uri"] for e in state["evidence"].values()
                 if (e.get("metadata") or {}).get("transcript") == "full"}
    partial_by_uri = {e["uri"]: e["evidence_id"] for e in state["evidence"].values()
                      if (e.get("metadata") or {}).get("role") == "stream-item"
                      and (e.get("metadata") or {}).get("transcript") != "full"}
    added = 0
    for item in read_export(path):
        if item["uri"] in have_full or item["uri"] not in partial_by_uri:
            continue
        log.register_evidence(
            media_type="application/json",
            uri=item["uri"],
            content=canonical_json(full_transcript(item)),
            metadata={"role": "stream-item", "source": SOURCE, "transcript": "full",
                      "name": item["name"], "updated_at": item["updated_at"],
                      "conversation_uuid": item["uuid"],
                      "supersedes": partial_by_uri[item["uri"]],
                      "note": "full-transcript re-snapshot; earlier evidence held human messages only"},
        )
        added += 1
    return engine._after_write({"full_transcripts_added": added})


def ingested_uris() -> set[str]:
    """The cursor, derived from canonical history: URIs that have evidence."""
    state = engine.get_log().state()
    return {e["uri"] for e in state["evidence"].values() if e.get("uri")}


def pending(path: str, limit: int = 10) -> list[dict]:
    # Compare preserved content, not just identity or update dates. Older
    # snapshots remain valid evidence; an edit creates another version.
    done = {(e.get("uri"), e.get("digest"))
            for e in engine.get_log().state()["evidence"].values()
            if (e.get("metadata") or {}).get("role") == "stream-item"}
    return [i for i in read_export(path)
            if (i["uri"], "sha256:" + sha256_hex(
                canonical_json(full_transcript(i)).encode("utf-8"))) not in done][:limit]


def stage(path: str, limit: int = 10) -> list[dict]:
    """Snapshot new or changed items as evidence; return them for extraction."""
    log = engine.get_log()
    staged = []
    for item in pending(path, limit):
        content = canonical_json(full_transcript(item))
        evidence_id = log.register_evidence(
            media_type="application/json",
            uri=item["uri"],
            content=content,
            metadata={"role": "stream-item", "source": SOURCE, "transcript": "full",
                      "name": item["name"], "updated_at": item["updated_at"],
                      "conversation_uuid": item["uuid"]},
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


def apply(proposals: list[dict], extractor: str) -> dict:
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
            grounding = [dict(g, evidence_id=g.get("evidence_id") or p["evidence_id"])
                         for g in (p.get("grounding") or [])]
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
                grounding=grounding,
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
    r = sub.add_parser("resnapshot")
    r.add_argument("export")
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
    elif args.cmd == "resnapshot":
        print(json.dumps(resnapshot(args.export), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
