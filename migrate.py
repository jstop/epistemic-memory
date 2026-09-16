"""One-time migration: import pre-log YAML beliefs into the canonical event log.

Honesty rules (the migration must not misrepresent how state came to exist):
  - Each source YAML file is snapshotted as evidence with role "pre-log-record".
    That snapshot is evidence of *what the pre-log store said* — it is not
    passed off as the belief's original grounding.
  - Every imported belief is marked metadata.reconstructed = true and carries
    its complete original envelope (events array, verified_at, links, ...) in
    metadata.original. Pre-log history was not cryptographically recorded when
    it occurred, and the log does not pretend otherwise: recorded_at is the
    (true) import time; original dates live in the payload.
  - Nothing in the YAML store is modified or deleted by this migration.

Usage:
    python migrate.py            # dry run: report what would be imported
    python migrate.py --commit   # perform the import into canonical.db
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from substrate import CanonicalLog

REPO = Path(__file__).resolve().parent
BELIEFS_DIR = REPO / "beliefs"
DB_PATH = REPO / "canonical.db"
CONTENT_DIR = REPO / "evidence_store"


def jsonsafe(value):
    """YAML parses bare dates into datetime.date; canonical JSON needs strings."""
    if isinstance(value, dict):
        return {k: jsonsafe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonsafe(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def load_files() -> list[tuple[Path, list[dict]]]:
    out = []
    for path in sorted(BELIEFS_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text()) or []
        beliefs = doc.get("beliefs", []) if isinstance(doc, dict) else doc
        out.append((path, beliefs))
    return out


def migrate(commit: bool) -> None:
    files = load_files()
    total = sum(len(bs) for _, bs in files)
    print(f"found {total} beliefs in {len(files)} files")
    if not commit:
        for path, bs in files:
            for b in bs:
                print(f"  would import {b['id']:36} ({b.get('method')}, {b.get('volatility')})")
        print("dry run — pass --commit to import")
        return

    log = CanonicalLog(DB_PATH, CONTENT_DIR, actor="migration:yaml-import")
    try:
        if log.state()["beliefs"]:
            raise SystemExit("canonical.db already contains beliefs — refusing to re-import")
        imported = 0
        for path, beliefs in files:
            record_ev = log.register_evidence(
                media_type="application/yaml",
                uri=f"file://{path}",
                content=path.read_bytes(),
                metadata={"role": "pre-log-record",
                          "note": "snapshot of the pre-log YAML store at import time"},
            )
            for b in beliefs:
                original = jsonsafe(b)
                # Pre-log derived/inferred beliefs recorded no premise refs;
                # importing them as 'unsupported' states that plainly instead
                # of fabricating premises the old store never had.
                needs_premises = b["method"] in ("derived", "inferred")
                log.form_belief(
                    unsupported=needs_premises,
                    belief_id=b["id"],
                    claim=b["claim"],
                    method=b["method"],
                    volatility=b["volatility"],
                    cluster=b.get("cluster"),
                    anchor=b.get("anchor"),
                    anchor_cost=b.get("anchor_cost"),
                    evidence_ids=[record_ev],
                    observed_at=str(b.get("observed_at", "")),
                    metadata={
                        "reconstructed": True,
                        "original": original,
                    },
                )
                imported += 1
        # Original `links` become explicit DEPENDS_ON relationships, appended
        # after all beliefs exist (reconciliation never blocks capture).
        state = log.state()
        for _, beliefs in files:
            for b in beliefs:
                for target in b.get("links") or []:
                    if target in state["beliefs"]:
                        log.record_relationship(
                            b["id"], "DEPENDS_ON", target,
                            note="reconstructed from pre-log links field",
                            method="derived",
                        )
        assert log.verify_chain()
        n = len(log.state()["beliefs"])
        r = len(log.state()["relationships"])
        print(f"imported {imported} beliefs, {r} relationships -> {DB_PATH}")
        print(f"chain valid: {log.verify_chain()}; total events: {len(log.events())}")
        assert n == imported
    finally:
        log.close()


if __name__ == "__main__":
    migrate(commit="--commit" in sys.argv)
