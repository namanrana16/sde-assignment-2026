#!/usr/bin/env python
"""Crash-recovery test (CONTEXT C13): kill the controller at every fault point and at random, restart, finish.

    python scripts/crash_test.py [--scenario scenarios/public_02_incidents.yaml] [--random-kills 3]

It starts its own ``python -m sim serve`` (free port, own run dir), then runs ``python -m controller run`` as a
subprocess:

* mode A - for each name in ``controller.faults.POINTS`` run with ``CRASH_AT=<point>:<n>`` until the process
  exits via ``os._exit`` (exit code 137), then restart without the variable;
* mode B - N random hard kills (``Popen.kill``) after a random wall-clock delay;
* finally run to completion and check the invariants (all through HTTP, the sim's grader and OUR store).

Every kill, restart and invariant result is written to ``reports/crash_test.log``. Exit code 0 only if every
invariant holds. Cross-platform: subprocess + sockets only, no shell, no signals.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from controller.faults import CRASH_EXIT_CODE, POINTS  # noqa: E402  (dev tooling may import controller)

# n-th hit of each point inside one controller process; spread so the points hit different moments
# Several hits per point: later hits land on later proposals (experiment entries/updates, DID probes, incident fixes).
# Order matters: the credit point needs the Day 3 hallucination incident, so it goes first and the rest fire after it.
POINT_ORDER = ["after-credit-call-before-record", "mid-ingest-txn", "after-submit-before-record",
               "after-apply-before-record", "before-apply-request", "after-advance-before-ingest"]
POINT_HITS = {"mid-ingest-txn": [3, 30], "after-advance-before-ingest": [40],
              "after-submit-before-record": [1, 2], "after-apply-before-record": [1, 2],
              "before-apply-request": [1, 2],
              "after-credit-call-before-record": [1, 4]}
TERMINAL_OK = {"APPLIED": {"applied"}, "MONITORING": {"applied"}, "CLOSED": {"applied", "rejected", "expired"},
               "AUTO_APPLIED": set(), "REJECTED": {"rejected"}, "EXPIRED": {"expired"},
               "STALE": {"approved", "pending", "rejected", "expired", "applied"},
               "SUPERSEDED": {"approved", "pending", "rejected", "expired"}, "SUBMIT_FAILED": set(),
               "PENDING": {"pending"}, "APPROVED": {"approved"}}


class Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("w", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TICK_SLEEP_S = "0"


def py_env(extra: dict | None = None) -> dict:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "CONTROLLER_TICK_SLEEP_S": TICK_SLEEP_S}
    env.pop("CRASH_AT", None)
    env.update(extra or {})
    return env


def wait_health(url: str, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            httpx.get(url + "/health", timeout=5).raise_for_status()
            return
        except Exception:  # noqa: BLE001
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


def db_file(state_dir: Path) -> Path | None:
    dbs = sorted(state_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)
    return dbs[-1] if dbs else None


def read_cursor(state_dir: Path) -> int | None:
    path = db_file(state_dir)
    if path is None:
        return None
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT last_event_id FROM ingest_cursor WHERE id=1").fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    return row[0] if row else None


def last_proposal(state_dir: Path) -> str:
    path = db_file(state_dir)
    if path is None:
        return "-"
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        r = con.execute("SELECT proposal_id, kind, state, route FROM proposals ORDER BY created_minute DESC,"
                        " plan_seq DESC LIMIT 1").fetchone()
        n = con.execute("SELECT COUNT(*) FROM proposals").fetchone()[0]
    except sqlite3.Error:
        r, n = None, 0
    finally:
        con.close()
    return f"{r} (total proposals {n})" if r else "none yet"


class Runner:
    def __init__(self, url: str, state_dir: Path, log: Log, per_run_timeout_s: float) -> None:
        self.url, self.state_dir, self.log, self.timeout = url, state_dir, log, per_run_timeout_s
        self.cursors: list[int] = []
        self.restarts = 0
        self.kills_delivered = 0
        self.fired = 0
        self.attempted = 0

    def _spawn(self, env_extra: dict | None) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "controller", "run", "--url", self.url, "--state-dir", str(self.state_dir)]
        return subprocess.Popen(cmd, cwd=ROOT, env=py_env(env_extra), stdout=subprocess.DEVNULL,
                                stderr=(self.state_dir.parent / "controller.stderr").open("ab"))

    def _after(self, label: str, rc: int | None) -> None:
        cur = read_cursor(self.state_dir)
        if cur is not None:
            self.cursors.append(cur)
        self.restarts += 1
        self.log(f"{label}: exit rc={rc}; ingest cursor now event_id={cur}")

    def run_fault(self, point: str, hits: int) -> int | None:
        spec = f"{point}:{hits}"
        self.log(f"RUN with CRASH_AT={spec}")
        proc = self._spawn({"CRASH_AT": spec})
        try:
            rc = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            self.log(f"  timeout waiting for {spec}; killed")
        fired = rc == CRASH_EXIT_CODE
        self.log(f"KILL POINT {point}: {'FIRED (os._exit)' if fired else 'did NOT fire'}")
        self._after(f"after {spec}", rc)
        self.log(f"  last proposal row: {last_proposal(self.state_dir)}")
        self.fired += int(fired)
        self.attempted += 1
        return rc

    def run_random_kill(self, rng: random.Random, lo: float, hi: float) -> int | None:
        delay = rng.uniform(lo, hi)
        self.log(f"RUN, hard kill (Popen.kill) scheduled after {delay:.1f}s wall")
        proc = self._spawn(None)
        try:
            rc = proc.wait(timeout=delay)
            self.log("  controller finished before the kill fired")
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            self.kills_delivered += 1
            self.log(f"KILL RANDOM after {delay:.1f}s")
        self._after("after random kill", rc)
        return rc

    def run_to_completion(self) -> int:
        for attempt in range(1, 6):
            self.log(f"RUN to completion (attempt {attempt})")
            proc = self._spawn(None)
            rc = proc.wait()
            self._after("completion run", rc)
            if rc == 0:
                return 0
        return 1


def invariants(url: str, state_dir: Path, run_dir: Path, baseline: Path, runner: Runner) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    http = httpx.Client(base_url=url, timeout=60)
    workflows = [w["workflow_id"] for w in http.get("/workflows").json()]
    con = sqlite3.connect(f"file:{db_file(state_dir).as_posix()}?mode=ro", uri=True)
    props = {r[0]: r[1:] for r in con.execute(
        "SELECT proposal_id, state, approval_id, applied_version, workflow_id, route FROM proposals")}

    # 1. each controller_* config version maps to one proposal in OUR store, none twice
    bad, seen = [], {}
    for wid in workflows:
        for v in http.get(f"/workflows/{wid}/config/history").json():
            if not str(v["actor"]).startswith("controller_"):
                continue
            pid = v["proposal_id"]
            key = (wid, pid)
            row = props.get(pid)
            if pid is None or row is None or row[1] != v["approval_id"] or row[2] != v["version"] or key in seen:
                bad.append(f"{wid} v{v['version']} {v['actor']} proposal={pid} ours={row}")
            seen[key] = v["version"]
    out.append(("every controller_* config version maps to a unique proposal_id in our store (applied_version"
                " and approval_id agree)", not bad, f"{len(seen)} versions checked; bad={bad}"))

    # 2. no duplicate applies: one version per approval, one APPLIED event per proposal
    dups = [r for r in con.execute("SELECT proposal_id, COUNT(*) FROM proposal_events WHERE event IN"
                                   " ('APPLIED','AUTO_APPLIED') GROUP BY proposal_id HAVING COUNT(*)>1")]
    out.append(("no duplicate applies in our store", not dups, f"duplicates={dups}"))

    # 3. grade hygiene
    env = py_env()
    res = subprocess.run([sys.executable, "-m", "sim", "grade", "--run", str(run_dir), "--baseline",
                          str(baseline), "--json"], cwd=ROOT, env=env, capture_output=True, text=True, check=False)
    gov: dict = {}
    try:
        gov = json.loads(res.stdout)["governance"]
    except (ValueError, KeyError):
        out.append(("sim grade ran", False, res.stderr[-300:]))
    if gov:
        ok = gov["lapsed"] == 0 and gov["apply_conflicts"] == 0 and gov["auto_apply_rejected"] == 0
        out.append(("grade: lapsed 0, apply_conflicts 0, auto_apply_rejected 0", ok, json.dumps(gov)))

    # 4. every proposal terminal/consistent with GET /approvals; every approval known to us
    sim_ap = {a["approval_id"]: a for a in http.get("/approvals").json()}
    problems = []
    for pid, (state, aid, _v, wid, route) in props.items():
        if state in ("INTENT", "DRAFT"):
            problems.append(f"{pid} stuck in {state}")
        elif route == "auto" or aid is None:
            if state not in ("AUTO_APPLIED", "CLOSED", "STALE", "SUBMIT_FAILED", "SUPERSEDED"):
                problems.append(f"{pid} auto/no-approval in {state}")
        elif aid not in sim_ap:
            problems.append(f"{pid} approval {aid} unknown to sim")
        elif state in TERMINAL_OK and TERMINAL_OK[state] and sim_ap[aid]["status"] not in TERMINAL_OK[state]:
            problems.append(f"{pid} ours={state} sim={sim_ap[aid]['status']}")
    mine = {r[1] for r in props.values() if r[1]}
    orphans = [a for a in sim_ap if a not in mine]
    problems += [f"sim approval {a} has no proposal in our store" for a in orphans]
    out.append(("every proposal is terminal/consistent with GET /approvals; no approval lost",
                not problems, f"{len(props)} proposals, {len(sim_ap)} approvals; problems={problems}"))

    # 5. cursor monotonic across restarts and equal to the sim's last event at the end
    mono = all(a <= b for a, b in zip(runner.cursors, runner.cursors[1:]))
    last = http.get("/sim/state").json()["last_event_id"]
    cur = read_cursor(state_dir)
    out.append(("ingest cursor monotonic across restarts and == sim last_event_id at the end",
                mono and cur == last, f"cursors={runner.cursors} final={cur} sim={last}"))
    # 6. credits ledger reconciles with /sim/state (kills inside credit-costing calls included)
    try:
        initial = float(con.execute("SELECT value FROM meta WHERE key='credits_initial'").fetchone()[0])
        booked = con.execute("SELECT COALESCE(SUM(credits),0) FROM llm_calls").fetchone()[0]
        unrec = con.execute("SELECT COALESCE(SUM(credits),0) FROM llm_calls WHERE response='unrecorded'").fetchone()[0]
        sim_left = http.get("/sim/state").json()["credits_remaining"]
        out.append(("credits ledger reconciles: sum(llm_calls.credits) == initial - sim credits_remaining",
                    abs(initial - sim_left - booked) < 0.01,
                    f"initial={initial} sim_remaining={sim_left} ledger={booked:.2f} of which unrecorded-at-crash={unrec:.2f}"))
    except (TypeError, ValueError, sqlite3.Error) as exc:
        out.append(("credits ledger reconciles", False, f"cannot read ledger: {exc}"))
    kinds = [r[0] for r in con.execute("SELECT kind FROM proposals")]
    out.append(("experiments were active (experiment_* proposals exist for the kills to land on)",
                any(k.startswith("experiment") for k in kinds), f"kinds={sorted(set(kinds))}"))
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--random-kills", type=int, default=10)
    ap.add_argument("--points", nargs="*", default=list(POINTS))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--kill-min-s", type=float, default=2.5)
    ap.add_argument("--kill-max-s", type=float, default=7)
    ap.add_argument("--tick-sleep-s", type=float, default=0.3,
                    help="slow the controller loop (test knob) so kills land at many sim moments")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--work-dir", default=None, help="default runs/crash_<scenario>")
    ap.add_argument("--log", default=str(ROOT / "reports" / "crash_test.log"))
    ap.add_argument("--point-timeout-s", type=float, default=900)
    args = ap.parse_args()
    global TICK_SLEEP_S
    TICK_SLEEP_S = str(args.tick_sleep_s)

    name = Path(args.scenario).stem
    work = Path(args.work_dir) if args.work_dir else ROOT / "runs" / f"crash_{name}"
    run_dir, state_dir, baseline = work / "sim_run", work / "state", ROOT / "runs" / f"base_{name}"
    log = Log(Path(args.log))
    if work.exists():
        import shutil
        shutil.rmtree(work)
    work.mkdir(parents=True)
    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"
    log(f"crash test: scenario={args.scenario} url={url} points={args.points} random_kills={args.random_kills}"
        f" seed={args.seed} platform={sys.platform}")
    if not baseline.exists():
        log(f"building baseline run {baseline}")
        subprocess.run([sys.executable, "-m", "sim", "baseline", "--scenario", args.scenario, "--run-dir",
                        str(baseline)], cwd=ROOT, env=py_env(), check=True, stdout=subprocess.DEVNULL)
    sim = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", args.scenario, "--run-dir",
                            str(run_dir), "--port", str(port), "--force"], cwd=ROOT, env=py_env(),
                           stdout=(work / "sim.out").open("wb"), stderr=subprocess.STDOUT)
    t0 = time.monotonic()
    try:
        wait_health(url, 300)
        log("simulator up")
        runner = Runner(url, state_dir, log, args.point_timeout_s)
        rng = random.Random(args.seed)
        for point in sorted(args.points, key=lambda p: POINT_ORDER.index(p) if p in POINT_ORDER else 99):
            for hits in POINT_HITS.get(point, [1]):
                runner.run_fault(point, hits)
        for _ in range(args.random_kills):
            runner.run_random_kill(rng, args.kill_min_s, args.kill_max_s)
        rc = runner.run_to_completion()
        log(f"controller finished rc={rc}; restarts={runner.restarts}; wall={time.monotonic() - t0:.0f}s")
        state = httpx.get(url + "/sim/state", timeout=30).json()
        log(f"sim state: finished={state['finished']} sim_minute={state['sim_minute']}")
        results = invariants(url, state_dir, run_dir, baseline, runner)
        results.insert(0, ("every named fault point fired", runner.fired == runner.attempted,
                           f"fired={runner.fired} of {runner.attempted}"))
        results.insert(0, (f"all {args.random_kills} random kills were delivered mid-run (test validity)",
                           runner.kills_delivered == args.random_kills, f"delivered={runner.kills_delivered}"))
        results.insert(0, ("run completed: controller exit 0 and sim finished", rc == 0 and state["finished"],
                           f"rc={rc}"))
        for label, ok, detail in results:
            log(f"INVARIANT {'PASS' if ok else 'FAIL'}: {label} -- {detail}")
        passed = all(ok for _, ok, _ in results)
        log(f"RESULT: {'ALL INVARIANTS PASS' if passed else 'FAILURES'}")
        return 0 if passed else 1
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sim.kill()


if __name__ == "__main__":
    sys.exit(main())
