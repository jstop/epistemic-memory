#!/usr/bin/env python3
"""Epistemic Memory engine — canonical store logic.

The single source of truth for belief storage, stance computation, validation,
verification, and reconciliation. Every surface (Claude Code, Claude Desktop,
claude.ai, ChatGPT) reaches this through the MCP server in server.py; the CLI
below is for maintenance.

Design invariants (from the plan, "i-don-t-think-we-re-recursive-blossom"):
  - a belief is never read naked: every read path returns it wearing its stance
  - a belief is never written without `method` (the integrity firewall)
  - a belief is never silently overwritten: changes append events
  - confidence is a legible stance derived from coarse axes, not a float
"""
from __future__ import annotations

import datetime as dt
import glob
import os
import re
import subprocess

import yaml

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
BELIEFS_DIR = os.environ.get("EPISTEMIC_BELIEFS_DIR", os.path.join(REPO_DIR, "beliefs"))
INDEX_PATH = os.environ.get(
    "EPISTEMIC_INDEX_PATH",
    os.path.join(os.path.expanduser("~"), ".claude", "projects", "-Users-jstein", "memory", "MEMORY.md"),
)

REQUIRED = ("id", "claim", "method", "observed_at", "volatility")
METHODS = ("observed", "asserted", "derived", "inferred")
VOLATILITIES = ("historical", "structural", "preference", "metric", "status")

HALF_LIFE_DAYS = {  # None => never decays
    "historical": None,
    "structural": 365,
    "preference": 180,
    "metric": 21,
    "status": 3,
}

STANCE_ORDER = {"CONTESTED": 0, "SUSPECT": 1, "HYPOTHESIS": 2, "NOTE": 3, "RELY": 4}
STANCE_EMOJI = {"CONTESTED": "🔴", "SUSPECT": "🟠", "HYPOTHESIS": "🔵", "NOTE": "🟡", "RELY": "🟢"}


def today() -> dt.date:
    return dt.date.today()


def _as_date(v) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v))


# ------------------------------------------------------------------ store I/O

def _files() -> list[str]:
    return sorted(glob.glob(os.path.join(BELIEFS_DIR, "*.yaml")))


def load_all(strict: bool = False) -> list[tuple[dict, str]]:
    out = []
    for path in _files():
        try:
            doc = yaml.safe_load(open(path)) or []
            beliefs = doc.get("beliefs", []) if isinstance(doc, dict) else doc
            for b in beliefs:
                out.append((b, path))
        except Exception:
            if strict:
                raise
    return out


def _rewrite(path: str, belief: dict) -> None:
    doc = yaml.safe_load(open(path)) or []
    beliefs = doc.get("beliefs", []) if isinstance(doc, dict) else doc
    for i, bb in enumerate(beliefs):
        if bb.get("id") == belief.get("id"):
            beliefs[i] = belief
    with open(path, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, width=100)


def find(belief_id: str) -> tuple[dict, str] | tuple[None, None]:
    for b, path in load_all(strict=True):
        if b.get("id") == belief_id:
            return b, path
    return None, None


# ------------------------------------------------------------- stance engine

def age_days(b: dict, ref: dt.date) -> int | None:
    base = _as_date(b.get("verified_at")) or _as_date(b.get("observed_at"))
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


def stamped(b: dict, ref: dt.date | None = None) -> dict:
    """A belief wearing its stance — the ONLY shape reads return."""
    ref = ref or today()
    st = stance(b, ref)
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
        "observed_at": str(b.get("observed_at", "")),
        "verified_at": str(b.get("verified_at", "")) or None,
        "contested": bool(b.get("contested")),
        "anchor": b.get("anchor"),
        "anchor_cost": b.get("anchor_cost"),
        "links": b.get("links", []),
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
    for f in REQUIRED:
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


def validate_file(path: str) -> list[str]:
    try:
        doc = yaml.safe_load(open(path)) or []
    except Exception as e:
        return [f"unparseable YAML: {e}"]
    beliefs = doc.get("beliefs", []) if isinstance(doc, dict) else doc
    if not isinstance(beliefs, list):
        return ["expected a list of beliefs"]
    problems = []
    for b in beliefs:
        problems.extend(validate_belief(b))
    return problems


# ---------------------------------------------------------------- mutations

def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "misc").lower()).strip("-") or "misc"


def capture(belief: dict) -> dict:
    """Validated write. Rejects envelope violations; appends a `formed` event."""
    belief = dict(belief)
    belief.setdefault("observed_at", today().isoformat())
    problems = validate_belief(belief)
    if problems:
        raise ValueError("envelope violation: " + "; ".join(problems))
    existing, _ = find(belief["id"])
    if existing is not None:
        raise ValueError(
            f"belief '{belief['id']}' already exists — use reconcile() to change it "
            "(no silent overwrite)"
        )
    belief.setdefault("events", []).append(
        {"at": today().isoformat(), "op": "formed", "method": belief["method"],
         "note": belief.pop("note", "captured via MCP")}
    )
    path = os.path.join(BELIEFS_DIR, f"{_slug(belief.get('cluster'))}.yaml")
    doc = {"beliefs": []}
    if os.path.exists(path):
        doc = yaml.safe_load(open(path)) or {"beliefs": []}
        if isinstance(doc, list):
            doc = {"beliefs": doc}
    doc.setdefault("beliefs", []).append(belief)
    os.makedirs(BELIEFS_DIR, exist_ok=True)
    with open(path, "w") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True, width=100)
    return stamped(belief)


def record_verification(belief_id: str, result: str, note: str = "") -> dict:
    """Append a verified/contradicted event. A failed verify IS a contradiction."""
    b, path = find(belief_id)
    if b is None:
        raise ValueError(f"no belief '{belief_id}'")
    ev = {"at": today().isoformat(),
          "op": "verified" if result == "verified" else "contradicted",
          "method": "observed", "note": (note or "")[:300]}
    b.setdefault("events", []).append(ev)
    if result == "verified":
        b["verified_at"] = today().isoformat()
        b["contested"] = False
    else:
        b["contested"] = True
    _rewrite(path, b)
    return stamped(b)


def run_anchor(belief_id: str) -> dict:
    b, _ = find(belief_id)
    if b is None:
        raise ValueError(f"no belief '{belief_id}'")
    if not b.get("anchor"):
        raise ValueError(f"belief '{belief_id}' has no anchor")
    proc = subprocess.run(b["anchor"], shell=True, capture_output=True, text=True, timeout=120)
    return {"belief": stamped(b), "observed": (proc.stdout + proc.stderr).strip()}


def reconcile(belief_id: str, new_claim: str, note: str) -> dict:
    """Supersede a claim — with lineage, never silently."""
    b, path = find(belief_id)
    if b is None:
        raise ValueError(f"no belief '{belief_id}'")
    old = b.get("claim")
    b.setdefault("events", []).append(
        {"at": today().isoformat(), "op": "superseded", "method": b.get("method"),
         "note": f"{note} (was: {old})"[:300]}
    )
    b["claim"] = new_claim
    b["verified_at"] = today().isoformat()
    b["contested"] = False
    _rewrite(path, b)
    return stamped(b)


# ------------------------------------------------------------------- health

def health() -> dict:
    ref = today()
    counts: dict[str, int] = {}
    stalest: list[dict] = []
    for b, _ in load_all():
        st = stance(b, ref)
        counts[st] = counts.get(st, 0) + 1
        if st in ("SUSPECT", "CONTESTED"):
            stalest.append({"id": b["id"], "stance": st, "anchor_cost": b.get("anchor_cost")})
    return {"total": sum(counts.values()), "by_stance": counts, "needs_attention": stalest}


# -------------------------------------------------------------- projections

def index_markdown(ref: dt.date | None = None) -> str:
    ref = ref or today()
    rows = [(b, stance(b, ref), *freshness(b, ref)) for b, _ in load_all()]
    rows.sort(key=lambda x: (x[0].get("cluster", ""), STANCE_ORDER.get(x[1], 9)))
    lines, cur = [], None
    for b, st, bucket, a in rows:
        cl = b.get("cluster", "(uncategorized)")
        if cl != cur:
            lines.append(f"\n### {cl}")
            cur = cl
        agestr = "immutable" if bucket == "historical" else (f"{bucket} {a}d" if a is not None else bucket)
        verify = f" — verify: `{b['anchor']}`" if b.get("anchor") and st in ("SUSPECT", "CONTESTED") else ""
        lines.append(
            f"- {STANCE_EMOJI[st]} **{st}** — {b['claim']} "
            f"⟨{b['id']}, {b.get('method','?')} {b.get('observed_at','?')}, {b.get('volatility','?')}, {agestr}⟩{verify}"
        )
    return (
        "<!-- AUTO-GENERATED by epistemic memory engine — edit beliefs/*.yaml in "
        "~/workspace/epistemic/memory -->\n"
        f"# Memory — epistemic index ({len(rows)} beliefs, stamped {ref.isoformat()})\n\n"
        "Each belief arrives wearing its stance. 🟢 RELY · 🟡 NOTE · 🟠 SUSPECT (verify) · "
        "🔴 CONTESTED · 🔵 HYPOTHESIS. Canonical store: `~/workspace/epistemic/memory/`.\n"
        + "\n".join(lines) + "\n"
    )


def write_index() -> str:
    body = index_markdown()
    with open(INDEX_PATH, "w") as fh:
        fh.write(body)
    return INDEX_PATH


# ------------------------------------------------------------------ CLI

def main() -> int:
    import argparse
    import json as _json
    import sys

    p = argparse.ArgumentParser(prog="epistemic-engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("stamp"); s.add_argument("--write-index", action="store_true")
    v = sub.add_parser("validate"); v.add_argument("file")
    ve = sub.add_parser("verify"); ve.add_argument("id")
    ve.add_argument("--result", choices=["verified", "contradicted"]); ve.add_argument("--note", default="")
    sub.add_parser("list"); sub.add_parser("health")
    args = p.parse_args()

    if args.cmd == "stamp":
        print(write_index() if args.write_index else index_markdown())
    elif args.cmd == "validate":
        problems = validate_file(args.file)
        if problems:
            print(f"REJECTED {args.file}:"); [print(f"  - {x}") for x in problems]; return 1
        print(f"OK {args.file}")
    elif args.cmd == "verify":
        out = run_anchor(args.id)
        print(f"claim:    {out['belief']['claim']}\nobserved: {out['observed']}")
        if args.result:
            record_verification(args.id, args.result, args.note or out["observed"])
            print(f"recorded '{args.result}'")
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
