#!/usr/bin/env python3
"""Epistemic Memory MCP server — the living library's single door.

Every AI surface (Claude Code, Claude Desktop, and later claude.ai / ChatGPT via a
remote endpoint) reads and writes beliefs ONLY through these tools. Tier-1
enforcement lives here, not in any client:

  - recall  → beliefs always arrive wearing their stance (never naked)
  - capture → rejects any belief missing the envelope (method is non-negotiable)
  - verify  → runs a belief's anchor; a failed check records a contradiction
  - reconcile → supersedes with lineage; silent overwrite is impossible
  - health  → stance census + what needs attention

Run: python server.py   (stdio transport)
"""
from __future__ import annotations

import datetime as dt

from mcp.server.fastmcp import FastMCP

import engine

mcp = FastMCP("epistemic-memory")


@mcp.tool()
def memory_recall(query: str = "", cluster: str = "", include_lineage: bool = False) -> list[dict]:
    """Recall beliefs, each stamped with its epistemic stance (RELY/NOTE/SUSPECT/CONTESTED/
    HYPOTHESIS), provenance method, freshness, and usage guidance. Optional substring
    `query` matches claim/id; `cluster` filters by cluster name. Set include_lineage=true
    to also get each belief's append-only event history."""
    ref = dt.date.today()
    out = []
    q = query.lower()
    for b, _ in engine.load_all():
        if cluster and cluster.lower() not in (b.get("cluster", "").lower()):
            continue
        if q and q not in (b.get("claim", "") + " " + b.get("id", "")).lower():
            continue
        st = engine.stamped(b, ref)
        if include_lineage:
            st["events"] = b.get("events", [])
        out.append(st)
    out.sort(key=lambda s: engine.STANCE_ORDER.get(s["stance"], 9))
    return out


@mcp.tool()
def memory_capture(
    id: str,
    claim: str,
    method: str,
    volatility: str,
    cluster: str,
    kind: str = "project",
    anchor: str = "",
    anchor_cost: str = "",
    links: list[str] | None = None,
    note: str = "",
) -> dict:
    """Capture a NEW atomic belief. Envelope is enforced: `method` must be one of
    observed/asserted/derived/inferred (how it was learned — never upgrade an inference
    to an observation), `volatility` one of historical/structural/preference/metric/status
    (sets its decay clock). One claim per belief — split compound facts. Provide `anchor`
    (a cheap shell check) when one exists. Fails if the id already exists — use
    memory_reconcile to change an existing belief."""
    belief = {
        "id": id, "claim": claim, "method": method, "volatility": volatility,
        "cluster": cluster, "kind": kind,
        "anchor": anchor or None, "anchor_cost": anchor_cost or None,
        "links": links or [], "note": note or "captured via MCP",
    }
    return engine.capture(belief)


@mcp.tool()
def memory_verify(id: str, result: str = "", note: str = "") -> dict:
    """Run a belief's anchor command and report what reality says vs. the claim.
    Call first with no `result` to observe; then call again with result='verified' or
    result='contradicted' to record the judgment (a failed check IS a contradiction —
    it flips the belief to CONTESTED and blocks reliance until reconciled)."""
    if result:
        return {"recorded": engine.record_verification(id, result, note)}
    return engine.run_anchor(id)


@mcp.tool()
def memory_reconcile(id: str, new_claim: str, note: str) -> dict:
    """Supersede a belief's claim with lineage — the only way to change one. Appends a
    `superseded` event recording the old claim and why, resets freshness, clears
    contested. Silent overwrite is structurally impossible."""
    return engine.reconcile(id, new_claim, note)


@mcp.tool()
def memory_health() -> dict:
    """Census of the belief store: totals by stance and which beliefs need attention
    (SUSPECT/CONTESTED), so staleness is loud instead of silent."""
    return engine.health()


@mcp.tool()
def memory_reindex() -> str:
    """Regenerate the thin stamped index (MEMORY.md projection for Claude Code).
    Returns the path written."""
    return engine.write_index()


if __name__ == "__main__":
    mcp.run()
