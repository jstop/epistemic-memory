#!/usr/bin/env python3
"""Epistemic Memory engine — read/logic layer over the canonical event log.

As of the v2 cutover, `canonical.db` (see substrate.py) is the single source of
truth. Everything else — beliefs/*.yaml, events.jsonl, MEMORY.md — is a
projection regenerated from canonical history. Hand-edits to projections never
become canonical state; changes enter only through capture / verify /
reconcile, which append canonical events.

Design invariants (v1 kept, v2 added):
  - a belief is never read naked: every read path returns it wearing its stance
  - a belief is never written without `method` (the integrity firewall);
    observed/asserted want evidence, derived/inferred want premises — a write
    that cannot say where it came from is recorded explicitly `unsupported`
  - a belief is never silently overwritten: every change is a canonical event
  - confidence is a legible stance derived at read time, never stored as truth
  - capture never blocks on retrieval, reconciliation, or projection failure
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

from substrate import METHODS, VOLATILITIES, CanonicalLog, canonical_json

REPO_DIR = os.path.dirname(os.path.abspath(__file__))

HALF_LIFE_DAYS = {  # None => never decays
    "historical": None,
    "structural": 365,
    "preference": 180,
    "metric": 21,
    "status": 3,
}

STANCE_ORDER = {"CONTESTED": 0, "SUSPECT": 1, "HYPOTHESIS": 2, "NOTE": 3, "RELY": 4}
STANCE_EMOJI = {"CONTESTED": "🔴", "SUSPECT": "🟠", "HYPOTHESIS": "🔵", "NOTE": "🟡", "RELY": "🟢"}

GENERATED_YAML_HEADER = (
    "# AUTO-GENERATED projection of canonical.db — DO NOT HAND-EDIT.\n"
    "# Edits here never become canonical state; write through the MCP tools or\n"
    "# the engine CLI, then regenerate with: python engine.py project\n"
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def db_path() -> str:
    return _env("EPISTEMIC_DB_PATH", os.path.join(REPO_DIR, "canonical.db"))


def content_dir() -> str:
    return _env("EPISTEMIC_CONTENT_DIR", os.path.join(REPO_DIR, "evidence_store"))


def beliefs_dir() -> str:
    return _env("EPISTEMIC_BELIEFS_DIR", os.path.join(REPO_DIR, "beliefs"))


def events_jsonl_path() -> str:
    return _env("EPISTEMIC_EVENTS_JSONL", os.path.join(REPO_DIR, "events.jsonl"))


def index_path() -> str:
    return _env(
        "EPISTEMIC_INDEX_PATH",
        os.path.join(os.path.expanduser("~"), ".claude", "projects", "-Users-jstein", "memory", "MEMORY.md"),
    )


_LOGS: dict[str, CanonicalLog] = {}


def get_log() -> CanonicalLog:
    path = db_path()
    if path not in _LOGS:
        _LOGS[path] = CanonicalLog(path, content_dir())
    return _LOGS[path]


def today() -> dt.date:
    return dt.date.today()


def _as_date(v) -> dt.date | None:
    if v in (None, ""):
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


# ------------------------------------------------------------------ views

def _view(bid: str, b: dict, relationships: list[dict]) -> dict:
    """Map a canonical-log belief into the flat dict shape the stance engine
    and every read surface consume. Purely derived."""
    restates = [h for h in b["claim_history"] if h["origin"] == "restated"]
    links = sorted({r["object_id"] for r in relationships
                    if r["subject_id"] == bid and r["rel"] == "DEPENDS_ON"})
    dates = [d for d in (
        (b.get("observed_at") or "")[:10],
        (b.get("verified_at") or "")[:10],
        (restates[-1]["at"][:10] if restates else ""),
    ) if d]
    return {
        "id": bid,
        "claim": b["claim"],
        "method": b["method"],
        "volatility": b["volatility"],
        "cluster": b.get("cluster"),
        "anchor": b.get("anchor"),
        "anchor_cost": b.get("anchor_cost"),
        "observed_at": (b.get("observed_at") or "")[:10] or None,
        "verified_at": (b.get("verified_at") or "")[:10] or None,
        "freshness_base": max(dates) if dates else None,
        "contested": bool(b.get("contested")),
        "retired": bool(b.get("retired")),
        "retired_reason": b.get("retired_reason"),
        "unsupported": bool(b.get("unsupported")),
        "reconstructed": bool((b.get("metadata") or {}).get("reconstructed")),
        "links": links,
        "evidence_ids": list(b.get("evidence_ids") or []),
        "premise_ids": list(b.get("premise_ids") or []),
        "claim_history": b["claim_history"],
        "events": [{"at": h["at"][:10], "op": h["event_type"]} for h in b["history"]],
    }


def load_all(include_retired: bool = False) -> list[tuple[dict, str]]:
    """All beliefs as stance-ready views. Second tuple element kept for
    backward compatibility (was the YAML path; now the canonical db path)."""
    state = get_log().state()
    out = []
    for bid in sorted(state["beliefs"]):
        v = _view(bid, state["beliefs"][bid], state["relationships"])
        if v["retired"] and not include_retired:
            continue
        out.append((v, db_path()))
    return out


def find(belief_id: str) -> tuple[dict | None, str | None]:
    for v, path in load_all(include_retired=True):
        if v["id"] == belief_id:
            return v, path
    return None, None


# ------------------------------------------------------------- stance engine

def age_days(b: dict, ref: dt.date) -> int | None:
    base = _as_date(b.get("freshness_base")) or _as_date(b.get("verified_at")) or _as_date(b.get("observed_at"))
    return None if base is None else (ref - base).days


def freshness(b: dict, ref: dt.date) -> tuple[str, int | None]:
    vol = b.get("volatility")
    hl = HALF_LIFE_DAYS.get(vol)
    a = age_days(b, ref)
    if vol == "historical":
        return "historical", a
    if a is None or hl is None:
        return "unknown", a
    r = a / hl
    return ("fresh" if r < 0.5 else "aging" if r < 1.0 else "stale"), a


def stance(b: dict, ref: dt.date) -> str:
    if b.get("contested"):
        return "CONTESTED"
    if b.get("method") == "inferred":
        return "HYPOTHESIS"
    bucket, _ = freshness(b, ref)
    if bucket in ("historical", "fresh"):
        return "NOTE" if b.get("method") == "derived" and bucket == "fresh" else "RELY"
    if bucket == "aging":
        return "NOTE"
    if bucket == "stale":
        return "SUSPECT"
    return "NOTE"  # unknown => cautious


def dependency_degradation(b: dict, by_id: dict[str, dict],
                           _stack: frozenset = frozenset()) -> str | None:
    """Reason a belief's support is compromised: a premise (premise_ids or
    DEPENDS_ON links) is contested or retired — transitively, cycle-safe.
    Epistemic relationship propagation; process provenance is untouched."""
    bid = b.get("id")
    if bid in _stack:
        return None
    stack = _stack | {bid}
    for pid in sorted(set(b.get("premise_ids", [])) | set(b.get("links", []))):
        p = by_id.get(pid)
        if p is None:
            continue
        if p.get("retired"):
            return f"premise retired: {pid}"
        if p.get("contested"):
            return f"premise contested: {pid}"
        deeper = dependency_degradation(p, by_id, stack)
        if deeper:
            return f"premise degraded: {pid} ({deeper})"
    return None


def all_views_by_id() -> dict[str, dict]:
    return {v["id"]: v for v, _ in load_all(include_retired=True)}


def stamped(b: dict, ref: dt.date | None = None,
            by_id: dict[str, dict] | None = None) -> dict:
    """A belief wearing its stance — the ONLY shape reads return. Pass `by_id`
    (all belief views) to make the stance dependency-aware: a contested or
    retired premise caps dependents at SUSPECT, with the reason stated."""
    ref = ref or today()
    st = stance(b, ref)
    degraded_reason = None
    if by_id is not None and st != "CONTESTED":
        degraded_reason = dependency_degradation(b, by_id)
        if degraded_reason and STANCE_ORDER[st] > STANCE_ORDER["SUSPECT"]:
            st = "SUSPECT"
    bucket, a = freshness(b, ref)
    return {
        "id": b.get("id"),
        "stance": st,
        "claim": b.get("claim"),
        "cluster": b.get("cluster"),
        "method": b.get("method"),
        "volatility": b.get("volatility"),
        "freshness": "immutable" if bucket == "historical" else bucket,
        "age_days": a,
        "observed_at": b.get("observed_at") or "",
        "verified_at": b.get("verified_at"),
        "contested": bool(b.get("contested")),
        "unsupported": bool(b.get("unsupported")),
        "reconstructed": bool(b.get("reconstructed")),
        "anchor": b.get("anchor"),
        "anchor_cost": b.get("anchor_cost"),
        "links": b.get("links", []),
        "degraded_reason": degraded_reason,
        "guidance": {
            "RELY": "use silently",
            "NOTE": "use, state the basis",
            "SUSPECT": "verify before high-stakes use (anchor above), else hedge",
            "CONTESTED": "do not assert; reconcile or ask the user",
            "HYPOTHESIS": "never assert as fact; offer as a guess",
        }[st],
    }


# --------------------------------------------------------------- validation

def validate_belief(b: dict) -> list[str]:
    problems = []
    if not isinstance(b, dict):
        return ["belief is not a mapping"]
    tag = b.get("id", "?")
    for f in ("id", "claim", "method", "volatility"):
        if b.get(f) in (None, ""):
            problems.append(f"{tag}: missing required field '{f}'")
    if b.get("method") and b["method"] not in METHODS:
        problems.append(f"{tag}: method '{b['method']}' not in {METHODS}")
    if b.get("volatility") and b["volatility"] not in VOLATILITIES:
        problems.append(f"{tag}: volatility '{b['volatility']}' not in {VOLATILITIES}")
    if b.get("observed_at"):
        try:
            _as_date(b["observed_at"])
        except Exception:
            problems.append(f"{tag}: observed_at '{b['observed_at']}' not YYYY-MM-DD")
    return problems


# ---------------------------------------------------------------- mutations
#
# All mutations append canonical events, then best-effort regenerate the
# projections. Projection failure NEVER un-does or blocks the canonical write.

def _after_write(result: dict) -> dict:
    try:
        regenerate_projections()
    except Exception as e:  # capture must not fail because a projection did
        result["projection_warning"] = f"projections not regenerated: {e}"
    return result


def capture(belief: dict, evidence_content: str | None = None,
            evidence_uri: str | None = None,
            evidence_media_type: str = "text/plain",
            premise_ids: list[str] | None = None) -> dict:
    """Validated write of a NEW belief as canonical events.

    Structural firewall: observed/asserted beliefs get their grounding from
    `evidence_content`/`evidence_uri` (registered as evidence first);
    derived/inferred from `premise_ids`. If the required references are
    absent, the belief is still captured — explicitly marked `unsupported`,
    never silently."""
    belief = dict(belief)
    belief.setdefault("observed_at", today().isoformat())
    problems = validate_belief(belief)
    if problems:
        raise ValueError("envelope violation: " + "; ".join(problems))
    log = get_log()

    evidence_ids: list[str] = []
    if evidence_content is not None or evidence_uri is not None:
        evidence_ids.append(log.register_evidence(
            media_type=evidence_media_type,
            uri=evidence_uri,
            content=evidence_content,
            metadata={"role": "capture-grounding", "note": belief.get("note", "")},
        ))
    premise_ids = list(premise_ids or [])
    method = belief["method"]
    unsupported = (
        (method in ("observed", "asserted") and not evidence_ids)
        or (method in ("derived", "inferred") and not premise_ids)
    )
    log.form_belief(
        belief_id=belief["id"],
        claim=belief["claim"],
        method=method,
        volatility=belief["volatility"],
        cluster=belief.get("cluster"),
        anchor=belief.get("anchor"),
        anchor_cost=belief.get("anchor_cost"),
        evidence_ids=evidence_ids,
        premise_ids=premise_ids,
        unsupported=unsupported,
        observed_at=str(belief["observed_at"]),
        metadata={"kind": belief.get("kind"), "note": belief.get("note")},
    )
    for target in belief.get("links") or []:
        try:
            log.record_relationship(belief["id"], "DEPENDS_ON", target,
                                    note="links field at capture")
        except ValueError:
            pass  # unknown link target must not block capture
    v, _ = find(belief["id"])
    result = stamped(v)
    if unsupported:
        result["warning"] = (
            f"captured as unsupported: method '{method}' had no "
            + ("evidence" if method in ("observed", "asserted") else "premises")
            + " — provide evidence_content/evidence_uri or premises to ground it"
        )
    return _after_write(result)


def record_verification(belief_id: str, result: str, note: str = "") -> dict:
    """Record a verification verdict. If the belief has an anchor, it is run
    and its actual output becomes evidence linked to the verification."""
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    output, command = None, v.get("anchor")
    if command:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
        output = (proc.stdout + proc.stderr).strip()
    elif note:
        output = note
    get_log().record_verification(
        belief_id, "verified" if result == "verified" else "contradicted",
        output=output, command=command)
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def run_anchor(belief_id: str) -> dict:
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    if not v.get("anchor"):
        raise ValueError(f"belief '{belief_id}' has no anchor")
    proc = subprocess.run(v["anchor"], shell=True, capture_output=True, text=True, timeout=120)
    return {"belief": stamped(v), "observed": (proc.stdout + proc.stderr).strip()}


def reconcile(belief_id: str, new_claim: str, note: str) -> dict:
    """Restate a claim — with complete lineage, never silently. The previous
    text is preserved in full in canonical history; if the belief was
    contested, the reconciliation resolves the contradiction on record."""
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    log = get_log()
    log.restate_belief(belief_id, new_claim, note=note)
    if v["contested"]:
        log.resolve_contradiction(belief_id, note=f"reconciled: {note}")
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def retire(belief_id: str, reason: str) -> dict:
    """Retire a belief — a recorded state, not destruction. The belief and its
    full history remain in canonical history and are recoverable."""
    get_log().retire_belief(belief_id, reason)
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def why(belief_id: str) -> dict:
    """Provenance: belief -> evidence (content availability) -> premises."""
    return get_log().why(belief_id)


# ------------------------------------------------------------------- health

def health() -> dict:
    ref = today()
    by_id = all_views_by_id()
    counts: dict[str, int] = {}
    stalest: list[dict] = []
    for b, _ in load_all():
        s = stamped(b, ref, by_id=by_id)
        st = s["stance"]
        counts[st] = counts.get(st, 0) + 1
        if st in ("SUSPECT", "CONTESTED"):
            item = {"id": b["id"], "stance": st, "anchor_cost": b.get("anchor_cost")}
            if s["degraded_reason"]:
                item["reason"] = s["degraded_reason"]
            stalest.append(item)
    out = {"total": sum(counts.values()), "by_stance": counts, "needs_attention": stalest}
    out["chain_valid"] = get_log().verify_chain()
    return out


# -------------------------------------------------------------- projections

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "misc").lower()).strip("-") or "misc"


def yaml_projection() -> dict[str, str]:
    """Deterministic {filename: content} projection of beliefs by cluster.
    Retired beliefs are included, marked — projection hides nothing."""
    clusters: dict[str, list[dict]] = {}
    for v, _ in load_all(include_retired=True):
        clusters.setdefault(_slug(v.get("cluster")), []).append(v)
    files = {}
    for slug in sorted(clusters):
        beliefs = []
        for v in sorted(clusters[slug], key=lambda x: x["id"]):
            b = {k: v[k] for k in (
                "id", "claim", "method", "volatility", "cluster", "anchor",
                "anchor_cost", "observed_at", "verified_at", "contested",
                "unsupported", "links") if v.get(k) not in (None, [], "")}
            if v["reconstructed"]:
                b["reconstructed"] = True
            if v["retired"]:
                b["retired"] = True
                b["retired_reason"] = v["retired_reason"]
            if len(v["claim_history"]) > 1:
                b["claim_history"] = [
                    {"at": h["at"][:10], "claim": h["claim"]} for h in v["claim_history"]]
            b["events"] = v["events"]
            beliefs.append(b)
        body = yaml.safe_dump({"generated": True, "beliefs": beliefs},
                              sort_keys=False, allow_unicode=True, width=100)
        files[f"{slug}.yaml"] = GENERATED_YAML_HEADER + body
    return files


def events_jsonl() -> str:
    """Deterministic export of canonical events. Strictly a projection —
    canonical.db remains the only canonical log."""
    return "".join(canonical_json(ev) + "\n" for ev in get_log().events())


def regenerate_projections() -> dict:
    """Regenerate beliefs/*.yaml and events.jsonl from canonical history.
    Removes stale generated YAML files that no longer correspond to a cluster."""
    bdir = beliefs_dir()
    os.makedirs(bdir, exist_ok=True)
    files = yaml_projection()
    for name, content in files.items():
        with open(os.path.join(bdir, name), "w") as fh:
            fh.write(content)
    for existing in os.listdir(bdir):
        if existing.endswith(".yaml") and existing not in files:
            os.remove(os.path.join(bdir, existing))  # stale projection only; canonical history is untouched
    with open(events_jsonl_path(), "w") as fh:
        fh.write(events_jsonl())
    return {"yaml_files": sorted(files), "events_jsonl": events_jsonl_path()}


def projection_drift() -> list[str]:
    """Report projection files that differ from canonical history (e.g. hand
    edits). Drift never feeds back into canonical state."""
    drift = []
    files = yaml_projection()
    bdir = beliefs_dir()
    for name, content in files.items():
        path = Path(bdir) / name
        if (path.read_text() if path.exists() else None) != content:
            drift.append(name)
    for existing in sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []:
        if existing.endswith(".yaml") and existing not in files:
            drift.append(existing)
    jsonl = Path(events_jsonl_path())
    if jsonl.exists() and jsonl.read_text() != events_jsonl():
        drift.append(jsonl.name)
    return drift


def index_markdown(ref: dt.date | None = None) -> str:
    ref = ref or today()
    by_id = all_views_by_id()
    rows = []
    for b, _ in load_all():
        s = stamped(b, ref, by_id=by_id)
        rows.append((b, s["stance"], *freshness(b, ref), s["degraded_reason"]))
    rows.sort(key=lambda x: (x[0].get("cluster") or "", STANCE_ORDER.get(x[1], 9)))
    lines, cur = [], None
    for b, st, bucket, a, degraded in rows:
        cl = b.get("cluster") or "(uncategorized)"
        if cl != cur:
            lines.append(f"\n### {cl}")
            cur = cl
        agestr = "immutable" if bucket == "historical" else (f"{bucket} {a}d" if a is not None else bucket)
        verify = f" — verify: `{b['anchor']}`" if b.get("anchor") and st in ("SUSPECT", "CONTESTED") else ""
        why_bad = f" — ⚠ {degraded}" if degraded else ""
        lines.append(
            f"- {STANCE_EMOJI[st]} **{st}** — {b['claim']} "
            f"⟨{b['id']}, {b.get('method','?')} {b.get('observed_at','?')}, {b.get('volatility','?')}, {agestr}⟩{verify}{why_bad}"
        )
    return (
        "<!-- AUTO-GENERATED by epistemic memory engine — a projection of canonical.db in "
        "~/workspace/epistemic/memory; edits here never become canonical -->\n"
        f"# Memory — epistemic index ({len(rows)} beliefs, stamped {ref.isoformat()})\n\n"
        "Each belief arrives wearing its stance. 🟢 RELY · 🟡 NOTE · 🟠 SUSPECT (verify) · "
        "🔴 CONTESTED · 🔵 HYPOTHESIS. Canonical store: `~/workspace/epistemic/memory/canonical.db`.\n"
        + "\n".join(lines) + "\n"
    )


def write_index() -> str:
    body = index_markdown()
    os.makedirs(os.path.dirname(index_path()), exist_ok=True)
    with open(index_path(), "w") as fh:
        fh.write(body)
    return index_path()


# ------------------------------------------------------------------ CLI

def main() -> int:
    import argparse
    import json as _json

    p = argparse.ArgumentParser(prog="epistemic-engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stamp"); s.add_argument("--write-index", action="store_true")
    ve = sub.add_parser("verify"); ve.add_argument("id")
    ve.add_argument("--result", choices=["verified", "contradicted"]); ve.add_argument("--note", default="")
    w = sub.add_parser("why"); w.add_argument("id")
    pr = sub.add_parser("project"); pr.add_argument("--check", action="store_true")
    sub.add_parser("list"); sub.add_parser("health")
    args = p.parse_args()

    if args.cmd == "stamp":
        print(write_index() if args.write_index else index_markdown())
    elif args.cmd == "verify":
        out = run_anchor(args.id)
        print(f"claim:    {out['belief']['claim']}\nobserved: {out['observed']}")
        if args.result:
            record_verification(args.id, args.result, args.note or out["observed"])
            print(f"recorded '{args.result}' (anchor output captured as evidence)")
    elif args.cmd == "why":
        print(_json.dumps(why(args.id), indent=2))
    elif args.cmd == "project":
        if args.check:
            drift = projection_drift()
            if drift:
                print("DRIFT (hand edits or stale projections — never canonical):")
                for d in drift:
                    print(f"  - {d}")
                return 1
            print("projections match canonical history")
        else:
            out = regenerate_projections()
            print(f"regenerated {len(out['yaml_files'])} yaml files + {out['events_jsonl']}")
    elif args.cmd == "list":
        for b, _ in load_all():
            st = stamped(b)
            print(f"{STANCE_EMOJI[st['stance']]} {st['stance']:10} {st['id']:32} {st['freshness']}")
    elif args.cmd == "health":
        print(_json.dumps(health(), indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
