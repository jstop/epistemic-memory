# Epistemic Memory — the living library

The provenance substrate and personal-memory projection of **Episteme** — a system
meant to live alongside a person's information continuously, preserve how their
understanding develops, and reason over that history without collapsing it into a
static profile. This repo is Episteme's first real-world implementation layer, not
its ceiling.

```
Episteme
├── Provenance substrate        <- substrate.py (canonical history, evidence, spans)
├── Personal memory projection  <- engine.py / server.py (beliefs, stance, MEMORY.md)
├── Interpretation compiler     <- beginning: interpretations, reconciler, ingest
├── Epistemic graph             <- emerging: relationships, dependency-aware stance
├── Inquiry engine              <- later
├── Attention allocation        <- later
└── Agency / authorization      <- later
```

Governing principle, applied recursively: **history is canonical; understanding is
derived.** The more interpretive something is, the less eager we are to put it in the
substrate. Canonical: evidence (full transcripts with literal authorship), events,
exact source spans. Derived and revisable: beliefs, stance, relationships, themes,
speech acts, attribution beyond literal authorship, intentions, "did this happen".
Episteme should preserve enough history that increasingly capable interpreters can
revisit the past and derive better understanding from it later.

Each belief is an atomic claim wrapped in an epistemic envelope (how it was learned,
how fast it decays, how to re-check it, and its full append-only history). No client
owns memory; they all borrow it through the MCP server.

- `canonical.db` + `evidence_store/` — the source of truth: append-only hash-chained
  event log + content-addressed evidence (gitignored; fully replayable)
- `substrate.py` — the canonical event substrate (log, evidence store, replay, chain
  verification, atomic reconciliation runs, **source spans** — exact quotes located in
  evidence with literal author derived on read — and **interpretations**: grounded,
  attributed, supersedable derived objects that are not beliefs)
- `engine.py` — read/logic layer: stance computation (dependency-aware), capture/verify/
  reconcile/retire as canonical events, projections + CLI
- `reconciler.py` — async reconciliation: mechanical candidate generation (lexical),
  semantic judging by an LLM client or human, one atomic ReconciliationRun event per
  pass; UNRELATED verdicts stop re-proposal. Never touches the capture path.
- `server.py` — MCP server (stdio): `memory_recall / capture / verify / reconcile /
  health / reindex / why / reconcile_pass / reconcile_apply / ingest_pass / ingest_apply`
- `ingest.py` — stream ingestion: stage unseen stream items as snapshotted evidence
  (registered URI/content-digest pairs ARE the cursor — changed conversations get new
  snapshots; unchanged versions are skipped, including after replay), an
  LLM/human extracts beliefs from staged items, apply captures them grounded in their
  source. First adapter: Claude chat-history export. An item may yield zero beliefs;
  its snapshot is preserved either way.
- `review.py` + `review.html` — local belief-review UI (`python review.py`): browse every belief
  with stance, evidence excerpts, and history; Confirm / Rephrase / Retire / Private are all
  canonical events through the engine. Private (VisibilityChanged) keeps a belief in history
  and explicit recall but out of the ambient MEMORY.md index.
- `migrate.py` — one-time pre-log YAML import (done; beliefs marked `reconstructed`)
- Projections (all regenerable; hand-edits never become canonical — check drift with
  `python engine.py project --check`): `beliefs/*.yaml`, `events.jsonl`,
  `~/.claude/projects/-Users-jstein/memory/MEMORY.md`

## Invariants
0. (2026-09-17) The system may be wrong; it may not be silently wrong. A belief no
   person has stood behind — a model's extraction, a migration, an agent's capture —
   is capped at NOTE ("use, state the basis") however fresh and evidenced. Only the
   owner confirming, rephrasing or restating it from their own channel lifts the cap.
   Reviewing is the act that makes a belief usable silently.
1. Reads are never naked — every belief arrives wearing its stance.
2. Writes without `method` are rejected — the integrity firewall.
3. Changes never overwrite silently — they append lineage events.
4. Confidence is a legible stance (🟢🟡🟠🔴🔵), not a float.
5. (v2) History is canonical; understanding is a projection over history. The event log
   never misrepresents how current state came to exist: corrections, supersessions, and
   contradictions layer onto history rather than rewriting it, and retirement is a
   recorded state, not destruction. Evidence content lives outside the log
   (content-addressed, referenced by digest) for future flexibility — preservation is
   the default and no deletion operation exists.
6. (v2) Capture never blocks on retrieval or reconciliation — a valid belief write needs
   only its own envelope and references; relationship inference is appended afterward as
   its own events.

## Run / maintain
```bash
source ~/python/global/bin/activate
python engine.py list            # stances now
python engine.py health          # what needs attention
python engine.py verify <id>     # run an anchor
python engine.py stamp --write-index
python server.py                 # MCP stdio server (this library alone)
python ../workbench/episteme_server.py   # the unified server (belief_* / argue_* / trace_*)
```

## The library is a build (devops rule, 2026-09-16)
Everything derived here is rebuildable from the archived sources and the log.
- `EPISTEMIC_BRANCH=dev` selects a build under `builds/dev/` (own log,
  projections, index; evidence store shared by symlink). `main` is what is
  served and what writes `~/.claude/.../MEMORY.md`.
- `make rebuild BRANCH=dev` rebuilds a branch from canonical events (never main).
- `make check` is the promotion gate: chain, replay reproduces state,
  projections match, evidence present; plus anchors, unsupported/contested,
  unreviewed count, authorship census. `engine.py check --record` appends the
  outcome as a `gate-check` run. `gate.sh` runs it on main and dev nightly
  (launchd `com.jstop.epistemic-gate`, 03:30; log `~/Library/Logs/epistemic-gate.log`).
- `recall_import.py` folds recall into a branch as grounded interpretations.
- Sources are archived by `../workspaces-backup/archive-sources.sh` (S3).

Design lineage: `~/.claude/plans/i-don-t-think-we-re-recursive-blossom.md`.
Phase next: remote endpoint for claude.ai / ChatGPT connectors; silo import.

## Authorship: who is writing

Who wrote an event is a property of the **channel** that opened the log, never of a
field in a request. `CanonicalLog(..., actor=)` is opened *as* a principal and no write
method accepts an actor; the engine resolves the principal once per process:

- `server.py` (the MCP door) declares itself `agent:<EPISTEMIC_AGENT>` at import
  (`agent:unknown` if unset; never `owner`, whatever the env says). Everything an AI
  surface writes is attributed to that agent.
- a non-interactive process (a script, an agent's shell tool) is `agent:cli`, or the
  non-owner name in `EPISTEMIC_ACTOR` (tests use `test:fixture`).
- only an interactive terminal resolves to `owner`. `python engine.py whoami` shows
  what the current channel writes as.

Every belief carries `authorship`: `composed_by` (who wrote the claim text as it now
stands), `recorded_as` (the raw actor column), `corrected`, and `stood_behind_by`
(`owner` only when the owner wrote or restated it from their own channel). Stance says
whether to rely on a claim; authorship says whose claim it is. A belief the owner has not
stood behind is a draft in the owner's record and must not be presented as the owner's
word. `memory_health` reports the census: beliefs by composer, how many the owner stands
behind, events by recorded actor, and any corrections.

**Misattributed history is corrected by appending, never by rewriting.** Before this
rule every write path defaulted to `owner`, so every agent capture was recorded as the
owner speaking. The owner states that from their own terminal:

```bash
python engine.py correct-attribution --through <seq> --recorded owner \
    --actual agent:claude-code --reason "captured via MCP before actors were channel-derived"
```

This appends one `AttributionCorrected` event (owner channel only — a correction of who
spoke is itself speech); attribution of the covered events is re-projected on read, the raw
column and the hash chain are untouched, and replay reproduces the same result.

This does not resist forgery — an in-process caller can declare any channel. It resists
the failure that actually happens: a machine filling the owner's slot because the default
let it. What is still missing is ratification: an owner act, bound to a claim's exact
text, that the composing system cannot perform on the owner's behalf.

## Reliance and verification behavior

Unsupported observations and derivations are retained but marked SUSPECT; inferred
claims remain HYPOTHESIS. No unsupported belief receives silent-reliance guidance.
Write responses and recalls both evaluate dependencies from canonical history;
unsupported premises and failed premise checks also prevent reliance on dependents.

Verification preserves the caller's requested verdict separately from command
execution status. Nonzero exits and timeouts record a `failed` verification with
output and execution details, without updating the verification date or asserting
that the claim is false. A later successful verification clears this failure state.
Successful execution alone does not establish truth: the caller still judges the
output. Existing events remain unchanged and replay remains backward compatible.
