#!/bin/zsh
# Nightly gate: run the promotion checks on main and on dev, record each outcome
# in that branch's log as a gate-check run, and keep a plain log for humans.
set -uo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
cd "$(dirname "$0")"
PY="${EPIST_PYTHON:-$HOME/python/global/bin/python}"
LOG="$HOME/Library/Logs/epistemic-gate.log"
mkdir -p "$(dirname "$LOG")"
export EPISTEMIC_ACTOR="agent:gate"
for b in main dev; do
  [[ "$b" == main || -f "builds/$b/canonical.db" ]] || continue
  report="$(mktemp)"
  EPISTEMIC_BRANCH=$b "$PY" engine.py check --record > "$report" 2>&1; rc=$?
  summary="$("$PY" gate_summary.py "$report")"
  rm -f "$report"
  echo "$(date '+%Y-%m-%d %H:%M:%S') branch=$b exit=$rc $summary" | tee -a "$LOG"
done
