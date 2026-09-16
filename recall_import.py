#!/usr/bin/env python3
"""Fold recall into the living library (phase 4 of the 2026-09-15 decision).

recall (~/.recall/recall.db) was a second provenance substrate: Claude's chat
export synced into SQLite, with two kinds of thing the library does not have:

  derivation_edge  — "claim X was derived from source S" (or from nothing:
                     pattern_match), recorded by claude while it worked
  attestations     — verdicts on messages: 26 `flagged` by claude/claude-triage
                     (Claude flagging its own likely confabulations), 1
                     proposed_verified, and 1 `refuted` by self — the only
                     human act in the whole store

Everything else in recall (932 conversations, 10,089 messages, 12,513 tool
call records) is the same Anthropic export the library already snapshotted.

The import is an INTERPRETER RUN over archived sources, into whatever branch
EPISTEMIC_BRANCH names (refuse main). Nothing is invented and nothing is
promoted to a belief:

  1. recall.db itself is registered as REFERENCED evidence (its sha256 and the
     S3 archive uri in metadata) — the source of record for everything below.
  2. Plain-text renderings of the attestations table, the derivation_edge
     table, the cited/non-tool source_records and the attested messages are
     registered as SNAPSHOTTED evidence, so every imported object can be
     located verbatim in something the library holds.
  3. Each derivation edge becomes an Interpretation (kind
     recall-derivation/<edge_type>) grounded in its row; when it cites a
     source, also in that source's content. Orphans (pattern_match) stay
     orphans: grounded only in the row that says claude asserted them.
  4. Each attestation becomes an Interpretation (kind recall-attestation/
     <verdict>) grounded in the attested message as recall recorded it, and —
     when the library holds a full transcript of that conversation — also in
     the transcript. attested_by is kept in metadata; the one `self` verdict
     is flagged as a human act so a later export of human acts can find it.
  5. One RunRecorded ties it all together.

Idempotent by construction: a second run finds the same content digests and
skips rows whose interpretation metadata already names the same recall id.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import engine

RECALL_DB = os.environ.get("RECALL_DB", str(Path.home() / ".recall" / "recall.db"))
S3_URI = "s3://epistemic-sources-523888557587/recall/recall.db"
INTERPRETER = "recall-import"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _render(rows: list[dict], key: str, text_field: str) -> str:
    """A plain-text rendering in which every row's text appears verbatim,
    so a span {quote: text} resolves by exact search. JSON would escape it."""
    out = []
    for r in rows:
        head = {k: v for k, v in r.items() if k != text_field}
        out.append(f"=== {key}:{r[key]} {json.dumps(head, default=str, sort_keys=True)}\n{r[text_field]}\n")
    return "\n".join(out)


def _existing_recall_ids(state: dict, kind_prefix: str) -> set[str]:
    return {str((i.get("metadata") or {}).get("recall_id"))
            for i in state["interpretations"].values()
            if i.get("kind", "").startswith(kind_prefix)}


def run(db_path: str = RECALL_DB, dry_run: bool = False) -> dict:
    if engine.branch() == "main":
        raise SystemExit("refusing to import into main; set EPISTEMIC_BRANCH to a dev branch, check it, then promote")
    if not os.path.exists(db_path):
        raise SystemExit(f"no recall db at {db_path}")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    log = engine.get_log()
    state = log.state()
    report = {"branch": engine.branch(), "db": db_path, "dry_run": dry_run,
              "evidence": {}, "interpretations": {}, "skipped": {}, "human_acts": []}

    edges = [dict(r) for r in con.execute("select * from derivation_edge order by id")]
    atts = [dict(r) for r in con.execute("select * from attestations order by id")]
    cited = {e["source_record_id"] for e in edges if e["source_record_id"] is not None}
    sources = [dict(r) for r in con.execute(
        "select id, source_type, external_id, content_hash, content, content_summary, created_at, "
        "ingested_at, ingesting_conversation_uuid from source_record "
        "where source_type not in ('tool_use','tool_result') or id in (%s) order by id"
        % (",".join(str(i) for i in cited) or "-1"))]
    att_msgs = [dict(r) for r in con.execute(
        "select m.uuid, m.conversation_uuid, m.sender, m.text, m.created_at, m.sequence "
        "from messages m where m.uuid in (select target_uuid from attestations) order by m.uuid")]
    report["counts"] = {"edges": len(edges), "attestations": len(atts), "sources": len(sources),
                        "attested_messages": len(att_msgs)}
    if dry_run:
        return report

    started = engine.dt.datetime.now(engine.dt.timezone.utc).isoformat()
    run_id = engine.new_run_id()
    sha = _sha256(db_path)
    reused = {"evidence": 0}

    # Evidence is registered once per (uri, sha of the recall db): a re-run of
    # the same db reuses what is already in the log rather than appending
    # duplicate EvidenceRegistered events.
    by_uri = {}
    for eid, e in state["evidence"].items():
        if (e.get("metadata") or {}).get("recall_sha256") == sha or (e.get("metadata") or {}).get("sha256") == sha:
            by_uri[e.get("uri")] = eid

    def register(uri: str, **kw) -> str:
        if uri in by_uri:
            reused["evidence"] += 1
            return by_uri[uri]
        return log.register_evidence(uri=uri, **kw)

    # 1. the db itself, referenced (archived in S3; too large to snapshot twice)
    db_ev = register(
        f"file://{db_path}", media_type="application/vnd.sqlite3", durability="REFERENCED",
        metadata={"kind": "recall-db", "sha256": sha, "archive": S3_URI, "run_id": run_id,
                  "note": "second provenance substrate, folded into the library 2026-09"})
    report["evidence"]["recall_db"] = db_ev

    # 2. plain-text table snapshots
    def snap(name: str, text: str, meta: dict) -> str:
        return register(f"recall://{name}@{sha[:12]}", media_type="text/plain",
                        content=text, metadata=dict(meta, kind=f"recall-{name}",
                                                    recall_sha256=sha, run_id=run_id))
    ev_edges = snap("derivation_edges", _render(edges, "id", "claim_text"), {"rows": len(edges)})
    ev_atts = snap("attestations", _render(atts, "id", "claim_text"), {"rows": len(atts)})
    ev_msgs = snap("messages", _render(att_msgs, "uuid", "text"), {"rows": len(att_msgs)})
    src_ev = {}
    for s in sources:
        src_ev[s["id"]] = register(
            f"recall://source/{s['id']}", media_type="text/plain", content=s["content"],
            metadata={"kind": "recall-source", "source_type": s["source_type"],
                      "external_id": s["external_id"], "content_hash": s["content_hash"],
                      "summary": s["content_summary"], "created_at": s["created_at"],
                      "ingesting_conversation_uuid": s["ingesting_conversation_uuid"],
                      "recall_sha256": sha, "run_id": run_id})
    report["evidence"]["reused"] = reused["evidence"]
    report["evidence"].update({"derivation_edges": ev_edges, "attestations": ev_atts,
                               "messages": ev_msgs, "sources": len(src_ev)})
    state = log.state()

    # library transcripts by conversation uuid, for second groundings
    transcripts: dict[str, list[str]] = {}
    for eid, e in state["evidence"].items():
        uri = e.get("uri") or ""
        if uri.startswith("claude-export://conversation/"):
            transcripts.setdefault(uri.rsplit("/", 1)[-1], []).append(eid)

    def transcript_span(conv_uuid: str, text: str) -> dict | None:
        probe = text.strip()
        if not probe:
            return None
        for eid in transcripts.get(conv_uuid, []):
            msgs = log.canonical_messages(state["evidence"][eid]) or []
            for i, m in enumerate(msgs):
                if probe in (m.get("text") or ""):
                    return {"evidence_id": eid, "message": i, "quote": probe}
        return None

    # 3. derivation edges → interpretations
    done = _existing_recall_ids(state, "recall-derivation/")
    outputs = []
    n_edges = n_orphans = n_sourced = 0
    for e in edges:
        if str(e["id"]) in done:
            report["skipped"]["edges"] = report["skipped"].get("edges", 0) + 1
            continue
        grounding = [{"evidence_id": ev_edges, "quote": e["claim_text"]}]
        sid = e["source_record_id"]
        if sid is not None and sid in src_ev:
            content = next(s["content"] for s in sources if s["id"] == sid)
            if content.strip():
                grounding.append({"evidence_id": src_ev[sid], "quote": content})
            n_sourced += 1
        else:
            n_orphans += 1
        iid = log.record_interpretation(
            kind=f"recall-derivation/{e['edge_type']}", statement=e["claim_text"],
            grounding=grounding,
            interpreter=f"recall/{e['recorded_by']}@{(e['recorded_at'] or '')[:10]}",
            note=(e.get("notes") or ""),
            metadata={"recall_id": e["id"], "claim_hash": e["claim_hash"],
                      "source_record_id": sid, "orphan": sid is None,
                      "context": e.get("context"), "conversation_uuid": e.get("conversation_uuid"),
                      "recorded_at": e.get("recorded_at"), "run_id": run_id})
        outputs.append({"type": "interpretation", "id": iid, "recall": f"derivation_edge:{e['id']}"})
        n_edges += 1
    report["interpretations"]["derivations"] = {"new": n_edges, "sourced": n_sourced, "orphans": n_orphans}

    # 4. attestations → interpretations
    state = log.state()
    done = _existing_recall_ids(state, "recall-attestation/")
    n_atts = n_with_transcript = 0
    msg_by_uuid = {m["uuid"]: m for m in att_msgs}
    for a in atts:
        if str(a["id"]) in done:
            report["skipped"]["attestations"] = report["skipped"].get("attestations", 0) + 1
            continue
        m = msg_by_uuid.get(a["target_uuid"])
        grounding = []
        if m and (m["text"] or "").strip():
            grounding.append({"evidence_id": ev_msgs, "quote": m["text"]})
            ts = transcript_span(m["conversation_uuid"], m["text"][:400])
            if ts:
                grounding.append(ts)
                n_with_transcript += 1
        grounding.append({"evidence_id": ev_atts, "quote": a["claim_text"] or a["verdict"]})
        human = a["attested_by"] == "self"
        iid = log.record_interpretation(
            kind=f"recall-attestation/{a['verdict']}",
            statement=(a["claim_text"] or f"{a['verdict']} message {a['target_uuid']}"),
            grounding=grounding,
            interpreter=f"recall/{a['attested_by']}@{(a['attested_at'] or '')[:10]}",
            note=(a.get("notes") or ""),
            metadata={"recall_id": a["id"], "verdict": a["verdict"], "attested_by": a["attested_by"],
                      "human_act": human, "target_message": a["target_uuid"],
                      "conversation_uuid": (m or {}).get("conversation_uuid"),
                      "attested_at": a["attested_at"], "run_id": run_id})
        outputs.append({"type": "interpretation", "id": iid, "recall": f"attestation:{a['id']}"})
        if human:
            report["human_acts"].append({"interpretation_id": iid, "verdict": a["verdict"],
                                         "claim": (a["claim_text"] or "")[:120]})
        n_atts += 1
    report["interpretations"]["attestations"] = {"new": n_atts, "also_in_transcript": n_with_transcript}

    # 5. the run — recorded even when nothing was new, so a no-op pass is history
    log.record_run(kind="import-recall", interpreter=f"{INTERPRETER}@{engine.code_version()}",
                   inputs=[db_ev, ev_edges, ev_atts, ev_msgs, *src_ev.values()],
                   outputs=outputs,
                   params={"recall_sha256": sha, "archive": S3_URI, **report["counts"]},
                   note="recall folded into the library as interpretations; nothing promoted to a belief",
                   run_id=run_id, started_at=started)
    report["run_id"] = run_id
    engine._after_write({})
    return report


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(prog="recall_import")
    p.add_argument("--db", default=RECALL_DB)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.db, a.dry_run), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
