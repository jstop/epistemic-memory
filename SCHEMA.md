# Epistemic Memory — envelope schema (v1)

Every belief is an atomic proposition wrapped in an envelope. Belief clusters live in
`~/.claude/epistemic/beliefs/*.yaml` (each file = a YAML list of belief mappings, or a
mapping with a `beliefs:` list). The `epistemic.py` tool reads them, computes each belief's
**stance** at read-time, and emits the thin stamped index (`MEMORY.md`).

## Fields

| field | required | values | notes |
|---|---|---|---|
| `id` | ✅ | kebab-case, unique | stable handle |
| `claim` | ✅ | one sentence | the proposition — must be independently true/false |
| `cluster` | – | label | grouping for the index |
| `kind` | – | user · feedback · project · reference | the old memory "type" |
| `method` | ✅ | **observed · asserted · derived · inferred** | how it was learned — the integrity firewall (Tier 1: presence enforced) |
| `observed_at` | ✅ | `YYYY-MM-DD` | when the grounding observation happened |
| `volatility` | ✅ | historical · structural · preference · metric · status | decay class → half-life |
| `verified_at` | – | `YYYY-MM-DD` | last successful re-verify; resets the freshness clock |
| `anchor` | – | shell command | the check that re-grounds it; null if unanchored |
| `anchor_cost` | – | cheap · expensive | gates how eagerly it's verified |
| `contested` | – | bool (default false) | a live contradiction blocks reliance |
| `links` | – | list of ids | depends-on / part-of |
| `events` | – | append-only list | `{at, op, method?, note?}` — op ∈ formed/corroborated/contradicted/verified/superseded/checked |

**Split-at-capture rule:** claims may share a cluster, but split into separate beliefs
wherever `method` differs — you cannot launder an inference into an observation by fusing it
with an observed claim.

## Half-life table (seeded — Phase 3 learns these from `events`)

| volatility | half-life | example |
|---|---|---|
| historical | ∞ (never decays) | "consolidation happened 2026-06-07" |
| structural | 365 d | dir layout, repo identity |
| preference | 180 d | "prefers `source ~/python/global`" |
| metric | 21 d | "38 workspaces", "31/32 tests" |
| status | 3 d | "active branch is X", "pending deletion" |

## Freshness (from age = today − max(observed_at, verified_at))

`r = age / half_life` → **fresh** `r < 0.5` · **aging** `0.5 ≤ r < 1` · **stale** `r ≥ 1`.
Historical beliefs are always fresh (age is irrelevant to an immutable past fact).

## Stance (what the read-time stamp says; drives the USE gate)

| condition | stance | gate action |
|---|---|---|
| `contested` | 🔴 CONTESTED | do not assert; reconcile or ask |
| `method: inferred` | 🔵 HYPOTHESIS | never as fact; offer as guess |
| historical, or fresh | 🟢 RELY | use silently (fresh+derived → NOTE) |
| aging | 🟡 NOTE | use, state the basis |
| stale | 🟠 SUSPECT | verify if cheap, else hedge |

This is a **legible heuristic, not arithmetic** — coarse buckets, no false-precision floats.
