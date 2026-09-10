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


def _evidence_excerpt(log, e: dict) -> str:
    if not e.get("digest"):
        return f"(referenced, no snapshot) {e.get('uri') or ''}"
    raw = log.store.get(e["digest"])
    if raw is None:
        return "(content not available)"
    text = raw.decode("utf-8", "replace")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "human_messages" in obj:
            text = "\n".join(f"[{m.get('at','')}] {m.get('text','')}" for m in obj["human_messages"])
    except ValueError:
        pass
    return text[:EXCERPT_CHARS] + ("…" if len(text) > EXCERPT_CHARS else "")


def belief_payload(v: dict, by_id: dict, state: dict, log) -> dict:
    s = engine.stamped(v, by_id=by_id)
    ev = []
    for eid in v["evidence_ids"]:
        e = state["evidence"].get(eid)
        if e:
            ev.append({
                "id": eid, "name": (e.get("metadata") or {}).get("name") or e.get("uri") or eid,
                "uri": e.get("uri"), "role": (e.get("metadata") or {}).get("role"),
                "excerpt": _evidence_excerpt(log, e),
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
        "note": (raw.get("metadata") or {}).get("note"),
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
