#!/usr/bin/env bash
# Dev tooling: one clean controller run of a scenario into its own run/state dirs (no baseline build).
# usage: scripts/det_run.sh <tag> <scenario.yaml> <port>
set -u
TAG="$1"; SCEN="$2"; PORT="$3"
cd "$(dirname "$0")/.."
mkdir -p runs state
rm -rf "runs/det_$TAG" "state/det_$TAG"
PYTHONHASHSEED="${SIMSEED:-random}" python -m sim serve --scenario "$SCEN" --run-dir "runs/det_$TAG" --port "$PORT" --force > "runs/det_$TAG.serve.log" 2>&1 &
SPID=$!
PYTHONHASHSEED="${CTLSEED:-random}" STATE_DIR="state/det_$TAG" bash run.sh "http://127.0.0.1:$PORT" > "runs/det_$TAG.ctl.log" 2>&1
echo "rc=$?"
kill "$SPID" 2>/dev/null
