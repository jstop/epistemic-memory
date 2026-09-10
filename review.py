#!/usr/bin/env python3
"""Belief review UI — a local page for the owner to review every belief.

Every decision is an engine call that appends a canonical event; the page
never edits projections. Actions:
  confirm  -> VerificationRecorded (verified; note kept as evidence)
  rephrase -> BeliefRestated (full previous text preserved)
  retire   -> BeliefRetired (recorded state, not destruction)
  private  -> VisibilityChanged (out of the ambient MEMORY.md index; still in
              canonical history and explicit recall)
  public   -> VisibilityChanged (back into the index)

Run:  python review.py [--port 8765]   then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine

HERE = Path(__file__).resolve().parent
PAGE = HERE / "review.html"
EXCERPT_CHARS = 900


import re

_EMAIL_RE = re.compile(r"^On .{5,80} wrote:|^From: |^Subject: ", re.M)
_HEADING_RE = re.compile(r"^#{1,4} |^\*\*[^*]{3,80}\*\*$|^-{3,}$", re.M)


def pasted_flags(text: str) -> list[str]:
    """Heuristic attribution signals for a human-sender message. Advisory
    only — the owner adjudicates. Long, formatted, or quoted blocks are
    frequently pasted AI output, emails, or articles rather than the owner's
    own words."""
    flags = []
    if _EMAIL_RE.search(text):
        flags.append("quoted email")
    if _HEADING_RE.search(text):
        flags.append("formatted document")
    if text.startswith("http"):
        flags.append("link share")
    if re.search(r"(^|\n)\s*1\. .+\n\s*2\. ", text) and len(text) > 600:
        flags.append("structured list — possibly pasted")
    if len(text) > 900 and not any(f in flags for f in ("quoted email", "structured list — possibly pasted")):
        flags.append("long block — possibly pasted")
    if re.search(r"^Q: .+\nA: ", text, re.M):
        flags.append("Q/A answers (owner's)")
    return flags


def _decode(log, e: dict):
    if not e.get("digest"):
        return None
    raw = log.store.get(e["digest"])
    if raw is None:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def transcript_of(log, e: dict) -> list[dict] | None:
    """Messages with roles for a conversation snapshot, or None for other media."""
    obj = _decode(log, e)
    if not isinstance(obj, dict):
        return None
    if "messages" in obj:
        msgs = obj["messages"]
    elif "human_messages" in obj:
        msgs = [dict(m, role="human") for m in obj["human_messages"]]
    else:
        return None
    return [{"role": m.get("role", "?"), "at": m.get("at", ""), "text": m.get("text", ""),
             "flags": pasted_flags(m.get("text", "")) if m.get("role") == "human" else []}
            for m in msgs]


def _evidence_excerpt(log, e: dict) -> str:
    obj = _decode(log, e)
    if obj is None:
        return f"(no snapshot) {e.get('uri') or ''}"
    if isinstance(obj, dict) and ("messages" in obj or "human_messages" in obj):
        msgs = obj.get("messages") or [dict(m, role="human") for m in obj["human_messages"]]
        text = "\n".join(f"[{m.get('at','')}] {m.get('text','')}" for m in msgs if m.get("role") == "human")
    else:
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def belief_payload(v: dict, by_id: dict, state: dict, log) -> dict:
    s = engine.stamped(v, by_id=by_id)
    full_by_uri = {}
    for e in state["evidence"].values():
        if (e.get("metadata") or {}).get("transcript") == "full" and e.get("uri"):
            full_by_uri[e["uri"]] = e
    ev = []
    attribution_warning = False
    for eid in v["evidence_ids"]:
        e = state["evidence"].get(eid)
        if not e:
            continue
        full = full_by_uri.get(e.get("uri")) or e
        meta = full.get("metadata") or {}
        transcript = transcript_of(log, full)
        n_msgs = len(transcript) if transcript else 0
        human_chars = sum(len(m["text"]) for m in (transcript or []) if m["role"] == "human") or 1
        pasted_chars = sum(len(m["text"]) for m in (transcript or [])
                           if m["role"] == "human" and any(f != "Q/A answers (owner's)" for f in m["flags"]))
        if pasted_chars / human_chars > 0.6:
            attribution_warning = True
        ev.append({
            "id": eid, "full_id": full["evidence_id"],
            "name": meta.get("name") or e.get("uri") or eid,
            "uri": e.get("uri"), "role": meta.get("role"),
            "conversation_uuid": meta.get("conversation_uuid")
                or (e.get("uri") or "").rsplit("/", 1)[-1],
            "recorded_at": e.get("recorded_at", "")[:10],
            "messages": n_msgs, "full_transcript": transcript is not None and "messages" in json.dumps(_decode(log, full) or {})[:200],
            "excerpt": _evidence_excerpt(log, full),
        })
    rels = [r for r in state["relationships"] if v["id"] in (r["subject_id"], r["object_id"])]
    raw = state["beliefs"][v["id"]]
    return {
        **s,
        "retired": v["retired"], "retired_reason": v["retired_reason"],
        "ambient": v["ambient"], "premise_ids": v["premise_ids"],
        "claim_history": [{"at": h["at"][:10], "claim": h["claim"], "origin": h["origin"]}
                          for h in v["claim_history"]],
        "history": [{"at": h["at"][:10], "type": h["event_type"]} for h in raw["history"]],
        "evidence": ev,
        "relationships": [{"subject": r["subject_id"], "rel": r["rel"], "object": r["object_id"],
                           "note": r.get("note", "")} for r in rels],
        "extractor": (raw.get("metadata") or {}).get("extractor"),
        "grounding": [log.span_with_author(sp, state) for sp in raw.get("grounding", [])],
        "interpretations": engine.interpretations(v["id"]),
        "note": (raw.get("metadata") or {}).get("note"),
        "attribution_warning": attribution_warning,
    }


def all_beliefs() -> list[dict]:
    log = engine.get_log()
    state = log.state()
    by_id = engine.all_views_by_id()
    out = [belief_payload(v, by_id, state, log) for v, _ in engine.load_all(include_retired=True)]
    order = {"CONTESTED": 0, "SUSPECT": 1, "HYPOTHESIS": 2, "NOTE": 3, "RELY": 4}
    out.sort(key=lambda b: (b["retired"], order.get(b["stance"], 9), b.get("cluster") or "", b["id"]))
    return out


def apply_decision(d: dict) -> dict:
    bid = d["id"]
    action = d["action"]
    note = (d.get("note") or "").strip()
    stamp = f"review UI {dt.date.today().isoformat()}"
    if action == "confirm":
        engine.record_verification(bid, "verified", note or f"confirmed by owner ({stamp})")
    elif action == "rephrase":
        text = (d.get("text") or "").strip()
        if not text:
            raise ValueError("rephrase needs text")
        engine.reconcile(bid, text, note or f"rephrased by owner ({stamp})")
    elif action == "retire":
        engine.retire(bid, note or f"retired by owner ({stamp})")
    elif action == "private":
        engine.set_visibility(bid, False, note or f"made private by owner ({stamp})")
    elif action == "public":
        engine.set_visibility(bid, True, note or f"made public by owner ({stamp})")
    else:
        raise ValueError(f"unknown action: {action}")
    log = engine.get_log()
    state = log.state()
    by_id = engine.all_views_by_id()
    v, _ = engine.find(bid)
    return belief_payload(v, by_id, state, log)


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/beliefs":
            self._json(200, {"beliefs": all_beliefs(), "health": engine.health()})
        elif self.path.startswith("/api/evidence/"):
            eid = self.path.rsplit("/", 1)[-1]
            log = engine.get_log()
            e = log.state()["evidence"].get(eid)
            if not e:
                return self._json(404, {"error": "no such evidence"})
            t = transcript_of(log, e)
            raw = None if t is not None else (_decode(log, e) if e.get("digest") else None)
            # spans of every belief grounded in this evidence (any snapshot of the same uri)
            uris = {e.get("uri")}
            same = {x["evidence_id"] for x in log.state()["evidence"].values() if x.get("uri") in uris}
            spans = []
            for b in log.state()["beliefs"].values():
                for sp in b.get("grounding", []):
                    if sp["evidence_id"] in same:
                        spans.append(dict(sp, belief_id=b["belief_id"]))
            self._json(200, {"evidence": {k: e.get(k) for k in ("evidence_id", "uri", "digest", "media_type", "durability", "metadata")},
                             "transcript": t, "spans": spans,
                             "raw": raw if isinstance(raw, str) else (json.dumps(raw, ensure_ascii=False, indent=1) if raw else None)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/decision":
            return self._json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
            self._json(200, {"belief": apply_decision(d)})
        except Exception as e:  # surface engine rejections to the page
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):  # quiet
        sys.stderr.write("review: " + (fmt % args) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(prog="review")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"belief review UI at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
