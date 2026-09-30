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
import sys
from pathlib import Path

import yaml

from substrate import METHODS, OWNER, VOLATILITIES, CanonicalLog, canonical_json, now_iso

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


# ------------------------------------------------------------- branches
#
# The library is a BUILD ARTIFACT of archived sources × versioned code ×
# interpreter runs (devops rule, 2026-09-16). `main` is the build the MCP
# servers serve and whose index lands in ~/.claude. Any other branch lives
# under builds/<branch>/ with its own log, evidence store, projections and
# index, so a rebuild or an experiment never touches what is being served.
# Explicit EPISTEMIC_*_PATH/DIR variables still override (tests use them).

def branch() -> str:
    return (os.environ.get("EPISTEMIC_BRANCH") or "main").strip()


def data_root() -> str:
    """Where the library's DATA lives: canonical.db, evidence_store, builds/
    and the projections — never inside the code repo. Resolution order:
    EPISTEMIC_DATA_DIR; else ../data beside the repo if it exists (the platform
    layout); else the repo directory (legacy)."""
    explicit = os.environ.get("EPISTEMIC_DATA_DIR")
    if explicit:
        return explicit
    sibling = os.path.join(os.path.dirname(REPO_DIR), "data")
    return sibling if os.path.isdir(sibling) else REPO_DIR


def builds_root() -> str:
    return _env("EPISTEMIC_BUILDS_DIR", os.path.join(data_root(), "builds"))


def build_dir(name: str | None = None) -> str:
    b = name or branch()
    return data_root() if b == "main" else os.path.join(builds_root(), b)


def db_path() -> str:
    return _env("EPISTEMIC_DB_PATH", os.path.join(build_dir(), "canonical.db"))


def content_dir() -> str:
    return _env("EPISTEMIC_CONTENT_DIR", os.path.join(build_dir(), "evidence_store"))


def beliefs_dir() -> str:
    return _env("EPISTEMIC_BELIEFS_DIR", os.path.join(build_dir(), "beliefs"))


def events_jsonl_path() -> str:
    return _env("EPISTEMIC_EVENTS_JSONL", os.path.join(build_dir(), "events.jsonl"))


def index_path() -> str:
    if branch() != "main":
        return _env("EPISTEMIC_INDEX_PATH", os.path.join(build_dir(), "MEMORY.md"))
    return _env(
        "EPISTEMIC_INDEX_PATH",
        os.path.join(os.path.expanduser("~"), ".claude", "projects", "-Users-jstein", "memory", "MEMORY.md"),
    )


def code_version() -> str:
    """The library code's git revision, for run identity and build records."""
    try:
        import subprocess
        sha = subprocess.run(["git", "-C", REPO_DIR, "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "-C", REPO_DIR, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5).stdout.strip() != ""
        return f"{sha}{'+dirty' if dirty else ''}" if sha else "unknown"
    except Exception:
        return "unknown"


# ------------------------------------------------------------------ actor
#
# Who is writing is decided by the CHANNEL this process is, never by a field
# in a request. The MCP server declares itself an agent at import; a script
# or an agent's shell tool has no terminal and is named as such; only a
# person at an interactive terminal writes as the owner. This does not resist
# forgery — an in-process caller can set anything — it resists the failure
# that actually happens: a machine filling the owner's slot by default.

CHANNEL_ACTOR: str | None = None  # set by the process that IS a channel (server.py)


def agent_actor(name: str | None) -> str:
    """Normalize an agent's self-description into an actor id. Never the owner."""
    n = (name or "").strip().removeprefix("agent:").strip()
    if not n or n == OWNER:
        n = "unknown"
    return f"agent:{n}"


def resolve_actor() -> str:
    if CHANNEL_ACTOR:
        return CHANNEL_ACTOR
    explicit = os.environ.get("EPISTEMIC_ACTOR", "").strip()
    if explicit and explicit != OWNER:
        return explicit
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    return OWNER if interactive else "agent:cli"


def require_owner(action: str) -> str:
    actor = resolve_actor()
    if actor != OWNER:
        raise PermissionError(
            f"{action} is the owner's word alone; this channel writes as '{actor}'. "
            "Run it from an interactive terminal.")
    return actor


_LOGS: dict[tuple[str, str], CanonicalLog] = {}


def get_log() -> CanonicalLog:
    """The canonical log, opened as the principal this process resolves to."""
    key = (db_path(), resolve_actor())
    if key not in _LOGS:
        _LOGS[key] = CanonicalLog(key[0], content_dir(), actor=key[1])
    return _LOGS[key]


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
        "ambient": bool(b.get("ambient", True)),
        "unsupported": bool(b.get("unsupported")),
        "verification_failed": bool(b.get("verification_failed")),
        "reconstructed": bool((b.get("metadata") or {}).get("reconstructed")),
        "links": links,
        "evidence_ids": list(b.get("evidence_ids") or []),
        "premise_ids": list(b.get("premise_ids") or []),
        "claim_history": b["claim_history"],
        "events": [{"at": h["at"][:10], "op": h["event_type"], "actor": h.get("actor")}
                   for h in b["history"]],
        "grounding": list(b.get("grounding") or []),
        "authorship": dict(b.get("authorship") or {}),
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
    if b.get("unsupported") or b.get("verification_failed"):
        return "SUSPECT"
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
        if p.get("unsupported"):
            return f"premise unsupported: {pid}"
        if p.get("verification_failed"):
            return f"premise verification failed: {pid}"
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
    retired premise caps dependents at SUSPECT, with the reason stated.
    Omitted context is loaded from canonical history for consistent write responses."""
    ref = ref or today()
    if by_id is None:
        by_id = all_views_by_id()
    st = stance(b, ref)
    degraded_reason = None
    if by_id is not None and st != "CONTESTED":
        degraded_reason = dependency_degradation(b, by_id)
        if degraded_reason and STANCE_ORDER[st] > STANCE_ORDER["SUSPECT"]:
            st = "SUSPECT"
    # Corrigibility rule (2026-09-17): the system may be wrong, but it must not
    # be silently wrong. A claim no person has stood behind — a model's
    # extraction, a migration, an agent's capture — is never RELY, however fresh
    # and well-evidenced. It is capped at NOTE ("use, state the basis") until the
    # owner confirms, rephrases, or restates it from their own channel, which is
    # what lifts the cap. Reviewing is therefore the act that makes a belief
    # usable silently, and an unreviewed belief always announces itself.
    if st == "RELY" and (b.get("authorship") or {}).get("stood_behind_by") != OWNER:
        st = "NOTE"
        degraded_reason = degraded_reason or "not yet stood behind by the owner (unreviewed)"
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
        "verification_failed": bool(b.get("verification_failed")),
        "reconstructed": bool(b.get("reconstructed")),
        "ambient": bool(b.get("ambient", True)),
        "anchor": b.get("anchor"),
        "anchor_cost": b.get("anchor_cost"),
        "links": b.get("links", []),
        "degraded_reason": degraded_reason,
        # Stance says whether to rely on the claim; authorship says whose
        # claim it is. A belief the owner has not stood behind is a draft,
        # whatever its stance, and must not be presented as the owner's word.
        "authorship": dict(b.get("authorship") or {}),
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
            premise_ids: list[str] | None = None,
            grounding: list[dict] | None = None) -> dict:
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
        grounding=grounding,
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


def _execute_anchor(command: str) -> tuple[str, dict]:
    """Keep command execution status separate from a caller's truth judgment."""
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
        return (proc.stdout + proc.stderr).strip(), {
            "returncode": proc.returncode, "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        def decode(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        return (decode(exc.stdout) + decode(exc.stderr)).strip(), {
            "returncode": None, "timed_out": True,
        }


def record_verification(belief_id: str, result: str, note: str = "") -> dict:
    """Record judgment and execution separately; a failed anchor cannot verify a claim."""
    if result not in ("verified", "contradicted"):
        raise ValueError("result must be 'verified' or 'contradicted'")
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    output, command, execution = None, v.get("anchor"), None
    verdict = result
    if command:
        output, execution = _execute_anchor(command)
        if execution["timed_out"] or execution["returncode"] != 0:
            verdict = "failed"
    elif note:
        output = note
    get_log().record_verification(
        belief_id, verdict, output=output, command=command,
        execution=execution, requested_verdict=result, note=note)
    v, _ = find(belief_id)
    out = stamped(v)
    if verdict == "failed":
        out["warning"] = "anchor execution failed; judgment was not applied"
    return _after_write(out)


def run_anchor(belief_id: str) -> dict:
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    if not v.get("anchor"):
        raise ValueError(f"belief '{belief_id}' has no anchor")
    output, execution = _execute_anchor(v["anchor"])
    return {"belief": stamped(v), "observed": output, "execution": execution}


def reconcile(belief_id: str, new_claim: str, note: str,
              grounding: list[dict] | None = None) -> dict:
    """Restate a claim — with complete lineage, never silently. The previous
    text is preserved in full in canonical history; if the belief was
    contested, the reconciliation resolves the contradiction on record."""
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    log = get_log()
    log.restate_belief(belief_id, new_claim, note=note, grounding=grounding)
    if v["contested"]:
        log.resolve_contradiction(belief_id, note=f"reconciled: {note}")
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def ground(belief_id: str, *, evidence_content: str | None = None,
           evidence_uri: str | None = None, evidence_media_type: str = "text/plain",
           evidence_ids: list[str] | None = None, grounding: list[dict] | None = None,
           note: str = "", evidence_metadata: dict | None = None) -> dict:
    """Add support under an existing belief. Registers new evidence if content
    or a uri is given, then appends a GroundingAdded event. Claim text and
    authorship are untouched."""
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    log = get_log()
    ids = list(evidence_ids or [])
    if evidence_content is not None or evidence_uri is not None:
        ids.append(log.register_evidence(
            media_type=evidence_media_type, uri=evidence_uri, content=evidence_content,
            metadata=dict(evidence_metadata or {}, role="grounding", note=note),
        ))
    log.add_grounding(belief_id, evidence_ids=ids, grounding=grounding, note=note)
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def record_run(*, kind: str, interpreter: str, inputs: list[str] | None = None,
               outputs: list[dict] | None = None, params: dict | None = None,
               note: str = "", run_id: str | None = None,
               started_at: str | None = None) -> str:
    """Record an interpreter run (run identity). Returns the run id. The
    library code revision is stamped into params so a run can be reproduced
    against the code that made it."""
    params = dict(params or {})
    params.setdefault("library_code", code_version())
    params.setdefault("branch", branch())
    rid = get_log().record_run(kind=kind, interpreter=interpreter, inputs=inputs,
                               outputs=outputs, params=params, note=note,
                               run_id=run_id, started_at=started_at)
    # a run is a write: projections follow it, so the next gate does not read drift
    try:
        regenerate_projections()
    except Exception:
        pass
    return rid


def runs(kind: str | None = None, interpreter: str | None = None) -> list[dict]:
    """Recorded runs, oldest first, optionally filtered."""
    out = list(get_log().state()["runs"].values())
    if kind:
        out = [r for r in out if r.get("kind") == kind]
    if interpreter:
        out = [r for r in out if (r.get("interpreter") or "").startswith(interpreter)]
    return out


def new_run_id() -> str:
    from substrate import new_id
    return new_id("run")


def set_anchor(belief_id: str, anchor: str | None, anchor_cost: str | None = None,
               note: str = "") -> dict:
    """Set or replace a belief's re-check command."""
    v, _ = find(belief_id)
    if v is None:
        raise ValueError(f"no belief '{belief_id}'")
    get_log().set_anchor(belief_id, anchor, anchor_cost=anchor_cost, note=note)
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def interpret(*, kind: str, statement: str, grounding: list[dict], interpreter: str,
              subjects: list[str] | None = None, supersedes: str | None = None,
              note: str = "") -> dict:
    """Record a derived interpretation of history (hypothesis, theme, relation,
    inferred intention...). Grounded in exact spans, named interpreter, no
    stance, never in the ambient index, supersedable. Understanding, not fact."""
    log = get_log()
    iid = log.record_interpretation(kind=kind, statement=statement, grounding=grounding,
                                    interpreter=interpreter, subjects=subjects,
                                    supersedes=supersedes, note=note)
    i = log.state()["interpretations"][iid]
    return _after_write(dict(i, grounding=[log.span_with_author(sp) for sp in i["grounding"]]))


def interpretations(belief_id: str | None = None, include_superseded: bool = False) -> list[dict]:
    """Derived interpretations, optionally about one belief; current ones by default."""
    log = get_log()
    state = log.state()
    out = []
    for i in state["interpretations"].values():
        if belief_id and belief_id not in i.get("subjects", []):
            continue
        if i["superseded_by"] and not include_superseded:
            continue
        out.append(dict(i, grounding=[log.span_with_author(sp, state) for sp in i["grounding"]]))
    return out


def retire(belief_id: str, reason: str) -> dict:
    """Retire a belief — a recorded state, not destruction. The belief and its
    full history remain in canonical history and are recoverable."""
    get_log().retire_belief(belief_id, reason)
    v, _ = find(belief_id)
    return _after_write(stamped(v))


def set_visibility(belief_id: str, ambient: bool, note: str = "") -> dict:
    """Keep a belief in canonical history but in/out of the ambient index."""
    get_log().set_visibility(belief_id, ambient, note=note)
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
    out["authorship"] = authorship_census()
    return out


def authorship_census() -> dict:
    """Who composed the record, and how much of it the owner stands behind.
    The number to watch is `stood_behind_by_owner` against `total`: when it
    drifts toward zero unnoticed, the log has become a machine's draft
    wearing the owner's name."""
    state = get_log().state()
    composers: dict[str, int] = {}
    stood = 0
    for b in state["beliefs"].values():
        if b.get("retired"):
            continue
        a = b.get("authorship") or {}
        composers[a.get("composed_by", "?")] = composers.get(a.get("composed_by", "?"), 0) + 1
        if a.get("stood_behind_by") == OWNER:
            stood += 1
    raw: dict[str, int] = {}
    for ev in get_log().events():
        raw[ev["actor"]] = raw.get(ev["actor"], 0) + 1
    return {
        "beliefs_by_composer": composers,
        "stood_behind_by_owner": stood,
        "events_by_recorded_actor": raw,
        "attribution_corrections": [
            {"at": c["recorded_at"][:10], "range": [c["from_sequence"], c["through_sequence"]],
             "recorded_actor": c["recorded_actor"], "actual_actor": c["actual_actor"],
             "reason": c["reason"]}
            for c in state["attribution_corrections"]],
        "writing_as": resolve_actor(),
    }


def correct_attribution(through_sequence: int, recorded_actor: str, actual_actor: str,
                        reason: str, from_sequence: int = 1) -> dict:
    """The owner's statement that a range of past events was written by
    someone other than the actor column says. Owner channel only: a
    correction of who spoke is itself speech, and a machine may not make it
    on the owner's behalf."""
    require_owner("attribution correction")
    get_log().correct_attribution(
        through_sequence=through_sequence, recorded_actor=recorded_actor,
        actual_actor=actual_actor, reason=reason, from_sequence=from_sequence)
    return _after_write(authorship_census())


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
            if not v["ambient"]:
                b["ambient"] = False
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
        if not b.get("ambient", True):
            continue  # private: in canonical history and explicit recall, not the ambient index
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

# ------------------------------------------------------------ build / check
#
# A data pipeline has a build and a gate. `rebuild` reconstructs a branch from
# canonical events alone (deterministic: same events, same state, chain
# verified while importing) and regenerates its projections. `check` is the
# gate a branch must pass before it is promoted to main: chain intact, replay
# reproduces the state, projections match, anchors run (cheap ones), and the
# authorship census is reported so misattribution is visible, never silent.

def rebuild(into_branch: str, source_db: str | None = None) -> dict:
    """Rebuild branch `into_branch` from the events of `source_db` (default:
    the current branch's db). Never touches main: refusing to rebuild into it."""
    if into_branch == "main":
        raise ValueError("refusing to rebuild into main; rebuild into a branch, check it, then promote")
    src = CanonicalLog(source_db or db_path(), content_dir(), actor=resolve_actor())
    target_dir = build_dir(into_branch)
    os.makedirs(target_dir, exist_ok=True)
    target_db = os.path.join(target_dir, "canonical.db")
    rebuilt = src.replay_into(target_db)
    same = rebuilt.state() == src.state()
    # The evidence store is content-addressed and append-only, so a branch
    # shares it by symlink rather than copying 37 MB per build: a branch that
    # registers new evidence adds objects main does not reference, never
    # overwrites one.
    store_link = os.path.join(target_dir, "evidence_store")
    link_target = os.path.relpath(os.path.abspath(content_dir()), target_dir)  # relative: survives moving the data dir
    if os.path.islink(store_link) or not os.path.exists(store_link):
        if os.path.islink(store_link):
            os.unlink(store_link)
        os.symlink(link_target, store_link)
    elif os.path.isdir(store_link) and not os.listdir(store_link):
        os.rmdir(store_link)
        os.symlink(link_target, store_link)
    # projections for the branch, without disturbing this process's paths
    env = {"EPISTEMIC_BRANCH": into_branch, "EPISTEMIC_BUILDS_DIR": builds_root(),
           "EPISTEMIC_ACTOR": os.environ.get("EPISTEMIC_ACTOR", "")}
    for k in ("EPISTEMIC_DB_PATH", "EPISTEMIC_CONTENT_DIR", "EPISTEMIC_BELIEFS_DIR",
              "EPISTEMIC_EVENTS_JSONL", "EPISTEMIC_INDEX_PATH"):
        env[k] = ""
    import subprocess, sys as _sys
    proj = subprocess.run([_sys.executable, os.path.join(REPO_DIR, "engine.py"), "project"],
                          env={**{k: v for k, v in os.environ.items() if not k.startswith("EPISTEMIC_")}, **{k: v for k, v in env.items() if v}},
                          capture_output=True, text=True, cwd=REPO_DIR)
    idx = subprocess.run([_sys.executable, os.path.join(REPO_DIR, "engine.py"), "stamp", "--write-index"],
                         env={**{k: v for k, v in os.environ.items() if not k.startswith("EPISTEMIC_")}, **{k: v for k, v in env.items() if v}},
                         capture_output=True, text=True, cwd=REPO_DIR)
    return {"branch": into_branch, "build_dir": target_dir, "events": len(rebuilt.events()),
            "state_identical": same, "chain_ok": rebuilt.verify_chain(),
            "evidence_store": content_dir(), "code": code_version(),
            "projections": proj.returncode == 0, "index": idx.returncode == 0,
            "note": "evidence store is shared with the source branch (content-addressed, append-only)"}


def main_db_path() -> str:
    """Where main's database is. Honours EPISTEMIC_DB_PATH when this process
    is on main (tests, relocated stores); otherwise the repo's canonical.db."""
    if branch() == "main":
        return db_path()
    return os.path.join(build_dir("main"), "canonical.db")


def branches() -> list[dict]:
    """Every build: main plus builds/<name>/, with event count, last gate and code."""
    out = []
    names = ["main"] + sorted(d for d in os.listdir(builds_root()) if os.path.isdir(os.path.join(builds_root(), d)) and not d.startswith("_")) if os.path.isdir(builds_root()) else ["main"]
    for name in names:
        db = main_db_path() if name == "main" else os.path.join(build_dir(name), "canonical.db")
        row = {"name": name, "db": db, "exists": os.path.exists(db), "served": name == branch()}
        if row["exists"]:
            try:
                log = CanonicalLog(db, content_dir(), actor=resolve_actor())
                st = log.state()
                gates = sorted((r for r in st["runs"].values() if r.get("kind") == "gate-check"),
                               key=lambda r: r.get("recorded_at") or "")
                g = gates[-1] if gates else None
                row.update(events=len(log.events()), beliefs=len(st["beliefs"]), evidence=len(st["evidence"]),
                           interpretations=len(st["interpretations"]), runs=len(st["runs"]),
                           last_gate=({"ok": (g.get("outputs") or [{}])[0].get("ok"), "recorded_at": g.get("recorded_at"),
                                       "unreviewed": (g.get("params") or {}).get("unreviewed")} if g else None),
                           last_event_at=(log.events()[-1]["recorded_at"] if log.events() else None))
                log.close()
            except Exception as e:  # noqa: BLE001
                row["error"] = str(e)
        out.append(row)
    return out


def _chain_head(db: str) -> tuple[int, str] | None:
    import sqlite3
    c = sqlite3.connect(db)
    try:
        row = c.execute("SELECT sequence, event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        return (int(row[0]), row[1]) if row else None
    finally:
        c.close()


def _has_event(db: str, sequence: int, event_hash: str) -> bool:
    import sqlite3
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT 1 FROM events WHERE sequence=? AND event_hash=?",
                         (sequence, event_hash)).fetchone() is not None
    finally:
        c.close()


def promote(from_branch: str, require_gate: bool = True, force: bool = False) -> dict:
    """Make a checked branch the served build. Preconditions: the branch exists,
    (by default) its most recent gate run passed, and its log CONTAINS main's
    current chain head — otherwise events written to main since the branch was
    cut would silently vanish; `force=True` overrides that, on record.
    main's current database is kept under builds/_backups/ (a consistent sqlite
    backup) and then overwritten IN PLACE through the sqlite backup API, so the
    file keeps its identity: processes holding main open see the new content on
    their next transaction instead of writing into a deleted inode. The
    promotion is recorded as a run in the new main. Refuses main onto itself."""
    if from_branch == "main":
        raise ValueError("promote takes a non-main branch")
    src_db = os.path.join(build_dir(from_branch), "canonical.db")
    if not os.path.exists(src_db):
        raise ValueError(f"no build for branch '{from_branch}'")
    rows = {b["name"]: b for b in branches()}
    g = rows[from_branch].get("last_gate")
    if require_gate and not (g and g.get("ok")):
        raise ValueError(f"branch '{from_branch}' has no passing gate run; run the gate first")
    main_db = main_db_path()
    head = _chain_head(main_db) if os.path.exists(main_db) else None
    if head and not _has_event(src_db, *head):
        if not force:
            raise ValueError(
                f"branch '{from_branch}' does not contain main's chain head (sequence {head[0]}); "
                f"main has events the branch lacks — rebuild the branch from main first, or promote with force=True")
    backups = os.path.join(builds_root(), "_backups")
    os.makedirs(backups, exist_ok=True)
    stamp = now_iso().replace(":", "").replace("-", "")[:15]
    backup = os.path.join(backups, f"main-{stamp}.db")
    import sqlite3
    if os.path.exists(main_db):
        cur = sqlite3.connect(main_db); bk = sqlite3.connect(backup)
        cur.backup(bk); bk.close(); cur.close()
    # overwrite main's content in place (never os.replace under open connections)
    src = sqlite3.connect(src_db); dst = sqlite3.connect(main_db)
    try:
        src.backup(dst)
    finally:
        src.close(); dst.close()
    for key in list(_LOGS):                      # keyed (db_path, content_dir)
        if os.path.abspath(str(key[0])) == os.path.abspath(main_db):
            try:
                _LOGS[key].close()
            except Exception:
                pass
            _LOGS.pop(key, None)
    # projections + index for main, in a subprocess (this process may be on a
    # branch). Explicit path overrides (tests, relocated stores) are kept; only
    # the branch selection is dropped so the subprocess is on main.
    import subprocess, sys as _sys
    env = {k: v for k, v in os.environ.items() if k not in ("EPISTEMIC_BRANCH",)}
    env["EPISTEMIC_ACTOR"] = os.environ.get("EPISTEMIC_ACTOR", "")
    for args in (["project"], ["stamp", "--write-index"]):
        subprocess.run([_sys.executable, os.path.join(REPO_DIR, "engine.py"), *args],
                       env={k: v for k, v in env.items() if v}, capture_output=True, text=True, cwd=REPO_DIR)
    # record the promotion in the new main
    log = CanonicalLog(main_db, content_dir(), actor=resolve_actor())
    rid = log.record_run(kind="promote", interpreter=f"engine.promote@{code_version()}",
                         outputs=[{"type": "build", "id": "main", "from": from_branch}],
                         params={"from": from_branch, "backup": backup, "gate_run_at": (g or {}).get("recorded_at"),
                                 "main_head_before": head, "forced": bool(force and head and not _has_event(src_db, *head))},
                         note=f"branch {from_branch} promoted to main")
    log.close()
    return {"ok": True, "from": from_branch, "backup": backup, "run_id": rid}


def check(run_anchors: bool = True, anchor_cost: str = "cheap", record: bool = False) -> dict:
    """The promotion gate. Returns a report; `ok` is False if any hard check fails.
    With record=True the outcome is appended as a RunRecorded (kind gate-check),
    so "the system might be stale" becomes "the gate said so, on this date"."""
    import tempfile
    log = get_log()
    report = {"branch": branch(), "db": db_path(), "code": code_version(), "hard": {}, "soft": {}}
    report["hard"]["chain"] = log.verify_chain()
    with tempfile.TemporaryDirectory() as d:
        try:
            rebuilt = log.replay_into(os.path.join(d, "replay.db"))
            report["hard"]["replay_reproduces_state"] = rebuilt.state() == log.state()
            rebuilt.close()
        except Exception as e:
            report["hard"]["replay_reproduces_state"] = False
            report["hard"]["replay_error"] = str(e)
    drift = projection_drift()
    report["hard"]["projections_match"] = not drift
    if drift:
        report["hard"]["projection_drift"] = drift[:10]
    # evidence content present for every snapshotted evidence
    state = log.state()
    missing = [eid for eid, e in state["evidence"].items()
               if e.get("durability") == "SNAPSHOTTED" and not log.store.has(e.get("digest") or "")]
    report["hard"]["evidence_content_present"] = not missing
    if missing:
        report["hard"]["missing_evidence"] = missing[:10]
    # soft: anchors, unsupported beliefs, authorship census
    views = [v for v, _ in load_all()]
    if run_anchors:
        results = {"verified": 0, "failed": 0, "skipped": 0, "failures": []}
        for v in views:
            if not v.get("anchor") or (anchor_cost == "cheap" and (v.get("anchor_cost") or "cheap") != "cheap"):
                results["skipped"] += 1
                continue
            observed, meta = _execute_anchor(v["anchor"])
            if meta.get("returncode") == 0 and not meta.get("timed_out"):
                results["verified"] += 1
            else:
                results["failed"] += 1
                results["failures"].append({"id": v["id"], "exit": meta.get("returncode"),
                                            "timed_out": meta.get("timed_out"),
                                            "observed": (observed or "")[:160]})
        report["soft"]["anchors"] = results
    report["soft"]["unsupported_beliefs"] = [v["id"] for v in views if v.get("unsupported")]
    report["soft"]["contested_beliefs"] = [v["id"] for v in views if v.get("contested")]
    report["soft"]["authorship"] = authorship_census()
    report["ok"] = all(bool(x) for k, x in report["hard"].items()
                       if k in ("chain", "replay_reproduces_state", "projections_match", "evidence_content_present"))
    report["unreviewed_beliefs"] = sum(
        1 for v in views if (v.get("authorship") or {}).get("stood_behind_by") != OWNER and not v.get("retired"))
    if record:
        anchors = report["soft"].get("anchors") or {}
        report["run_id"] = record_run(
            kind="gate-check", interpreter=f"engine.check@{code_version()}",
            outputs=[{"type": "gate-report", "ok": report["ok"], **{k: v for k, v in report["hard"].items() if isinstance(v, bool)}}],
            params={"anchors_verified": anchors.get("verified"), "anchors_failed": anchors.get("failed"),
                    "anchor_failures": [f["id"] for f in anchors.get("failures", [])][:20],
                    "unsupported": len(report["soft"]["unsupported_beliefs"]),
                    "contested": len(report["soft"]["contested_beliefs"]),
                    "unreviewed": report["unreviewed_beliefs"],
                    "beliefs": len(views)},
            note="scheduled or manual gate run")
    return report


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
    sub.add_parser("whoami", help="the actor this channel writes as")
    ca = sub.add_parser("correct-attribution",
                        help="owner only: state that past events were written by someone else")
    ca.add_argument("--through", type=int, required=True, help="last event sequence covered")
    ca.add_argument("--from", dest="from_seq", type=int, default=1)
    ca.add_argument("--recorded", required=True, help="actor as the column says (e.g. owner)")
    ca.add_argument("--actual", required=True, help="who actually wrote them (e.g. agent:claude-code)")
    ca.add_argument("--reason", required=True)
    rb = sub.add_parser("rebuild", help="rebuild a branch from canonical events (never main)")
    rb.add_argument("--branch", required=True)
    rb.add_argument("--from-db", default=None, help="source canonical.db (default: current branch)")
    ck = sub.add_parser("check", help="the promotion gate: chain, replay, projections, evidence, anchors")
    ck.add_argument("--no-anchors", action="store_true")
    ck.add_argument("--anchor-cost", default="cheap", choices=["cheap", "all"])
    ck.add_argument("--record", action="store_true", help="append the outcome to the log as a gate-check run")
    sub.add_parser("branches", help="every build with its last gate")
    pm = sub.add_parser("promote", help="make a checked branch the served build (main)")
    pm.add_argument("--from", dest="from_branch", required=True)
    pm.add_argument("--skip-gate", action="store_true")
    args = p.parse_args()

    if args.cmd == "branches":
        print(_json.dumps(branches(), indent=2, default=str)); return 0
    if args.cmd == "promote":
        print(_json.dumps(promote(args.from_branch, require_gate=not args.skip_gate), indent=2)); return 0

    if args.cmd == "rebuild":
        out = rebuild(args.branch, args.from_db)
        print(_json.dumps(out, indent=2))
        return 0 if (out["state_identical"] and out["chain_ok"]) else 1
    if args.cmd == "check":
        out = check(run_anchors=not args.no_anchors, anchor_cost=args.anchor_cost, record=args.record)
        print(_json.dumps(out, indent=2, default=str))
        return 0 if out["ok"] else 1

    if args.cmd == "whoami":
        print(resolve_actor())
    elif args.cmd == "correct-attribution":
        out = correct_attribution(args.through, args.recorded, args.actual, args.reason,
                                  from_sequence=args.from_seq)
        print(_json.dumps(out, indent=2))
    elif args.cmd == "stamp":
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
