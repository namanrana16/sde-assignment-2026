#!/usr/bin/env bash
# Contract: ./run.sh <SIM_URL> runs the controller until the scenario finishes.
# Exits 0 only when the controller exits 0 (it does so only once /sim/state
# reports finished). Crashes are restarted; the controller resumes from SQLite.
# Exit codes: 0 finished, 1 restart cap reached, 2 no usable python, 3 sim never came up.
# Docker: not used. Plain bash + Python 3.13; dependencies come from requirements.txt (see SUBMISSION.md section 15).
set -u
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
SIM_URL="${SIM_URL%/}"
cd "$(dirname "$0")"

MAX_RESTARTS="${MAX_RESTARTS:-20}"
WAIT_TIMEOUT_S="${WAIT_TIMEOUT_S:-300}"
RESTART_WAIT_S="${RESTART_WAIT_S:-120}"   # startup wait given to a RESTARTED controller (dead sim: give up fast)
STATE_DIR="${STATE_DIR:-state}"
# the controller's real runtime imports (see controller/*.py): httpx for the API, numpy for the statistics
DEPS_CHECK="import httpx, numpy"

has_deps() { command -v "$1" >/dev/null 2>&1 && "$1" -c "$DEPS_CHECK" >/dev/null 2>&1; }

find_python() {
  local cand
  for cand in python3 python; do
    if has_deps "$cand"; then echo "$cand"; return 0; fi
  done
  return 1
}

pick_python() {
  if [ -n "${PYTHON:-}" ]; then echo "$PYTHON"; return 0; fi
  find_python && return 0
  local base cand
  for base in python3 python; do command -v "$base" >/dev/null 2>&1 && break; base=""; done
  [ -n "$base" ] || return 1
  echo "run.sh: no interpreter has the dependencies; trying pip install --user" >&2
  if "$base" -m pip install --user -q --timeout 15 --retries 1 --disable-pip-version-check -r requirements.txt >&2 && has_deps "$base"; then
    echo "$base"; return 0
  fi
  echo "run.sh: pip --user failed; trying a virtualenv in .venv" >&2
  if "$base" -m venv .venv >&2 && .venv/bin/pip install -q --timeout 15 --retries 1 --disable-pip-version-check -r requirements.txt >&2 \
      && has_deps .venv/bin/python; then
    echo ".venv/bin/python"; return 0
  fi
  if [ -x .venv/Scripts/python ] && .venv/Scripts/pip install -q --timeout 15 --retries 1 --disable-pip-version-check -r requirements.txt >&2 \
      && has_deps .venv/Scripts/python; then
    echo ".venv/Scripts/python"; return 0
  fi
  return 1
}

PY="$(pick_python)" || { echo "run.sh: no python with the controller dependencies found or installable" >&2; exit 2; }
echo "run.sh: using interpreter '$PY'" >&2

# The simulator builds its history before it opens the port, so wait generously.
deadline=$(( $(date +%s) + WAIT_TIMEOUT_S ))
until "$PY" - "$SIM_URL" <<'PYEOF' >/dev/null 2>&1
import sys, httpx
httpx.get(sys.argv[1] + "/health", timeout=5).raise_for_status()
PYEOF
do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "run.sh: $SIM_URL/health not up after ${WAIT_TIMEOUT_S}s" >&2
    exit 3
  fi
  sleep 2
done
echo "run.sh: simulator is up" >&2

# sim_minute from /sim/state, or -1 when unreadable
sim_minute() {
  "$PY" - "$SIM_URL" <<'PYEOF' 2>/dev/null || echo -1
import sys, httpx
print(int(httpx.get(sys.argv[1] + "/sim/state", timeout=10).json()["sim_minute"]))
PYEOF
}

restarts=0
extra=()
while true; do
  before="$(sim_minute)"
  "$PY" -m controller run --url "$SIM_URL" --state-dir "$STATE_DIR" ${extra[@]+"${extra[@]}"}
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "run.sh: controller finished cleanly" >&2
    exit 0
  fi
  after="$(sim_minute)"
  # only a run that moved the simulation forward was healthy; a deterministic crash must hit the cap
  if [ "$before" -ge 0 ] && [ "$after" -gt "$before" ]; then
    restarts=0
  fi
  restarts=$(( restarts + 1 ))
  echo "run.sh: controller exited rc=$rc (restart $restarts/$MAX_RESTARTS, sim_minute $before -> $after)" >&2
  if [ "$restarts" -gt "$MAX_RESTARTS" ]; then
    echo "run.sh: restart cap reached" >&2
    exit 1
  fi
  extra=(--startup-wait "$RESTART_WAIT_S")
  sleep 2
done
