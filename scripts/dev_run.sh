#!/usr/bin/env bash
# Dev tooling: baseline + serve + controller + grade for one scenario.
# usage: scripts/dev_run.sh <name> <scenario.yaml> <report.md> [port]
set -u
NAME="$1"; SCEN="$2"; OUT="$3"; PORT="${4:-8700}"
cd "$(dirname "$0")/.."
mkdir -p runs state
[ -d "runs/base_$NAME" ] || python -m sim baseline --scenario "$SCEN" --run-dir "runs/base_$NAME" || exit 1
python -m sim serve --scenario "$SCEN" --run-dir "runs/mine_$NAME" --port "$PORT" --force > "runs/serve_$NAME.log" 2>&1 &
SPID=$!
rm -rf "state/$NAME"
STATE_DIR="state/$NAME" bash run.sh "http://127.0.0.1:$PORT" > "runs/ctl_$NAME.log" 2>&1
RC=$?
PYTHONUTF8=1 python -m sim grade --run "runs/mine_$NAME" --baseline "runs/base_$NAME" --out "$OUT"
kill "$SPID" 2>/dev/null
echo "run.sh exit $RC; report $OUT"
