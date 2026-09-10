# Epistemic Memory — the living library

Canonical, provenance-carrying belief store shared by every AI surface Josh uses.
Each belief is an atomic claim wrapped in an epistemic envelope (how it was learned,
how fast it decays, how to re-check it, and its full append-only history). No client
owns memory; they all borrow it through the MCP server.

- `beliefs/*.yaml` — the store (one file per cluster; schema in `SCHEMA.md`)
- `engine.py` — stance computation, validation, verification, reconciliation + CLI
- `server.py` — MCP server (stdio): `memory_recall / capture / verify / reconcile / health / reindex`
- `substrate.py` — canonical event substrate (v2, in progress): append-only hash-chained
  SQLite event log + content-addressed evidence store; `tests/test_substrate.py` are its
  behavioral acceptance tests
- Projection: `~/.claude/projects/-Users-jstein/memory/MEMORY.md` (auto-generated, stamped)

## Invariants
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
python server.py                 # MCP stdio server
```

Design lineage: `~/.claude/plans/i-don-t-think-we-re-recursive-blossom.md`.
Phase next: remote endpoint for claude.ai / ChatGPT connectors; silo import.
