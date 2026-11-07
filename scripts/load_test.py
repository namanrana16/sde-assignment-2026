#!/usr/bin/env python
"""Load test: serve a scenario, run the controller (all features), sample wall/RSS/DB size, summarise.

    python scripts/load_test.py [--scenario scenarios/public_03_load.yaml] [--max-sim-days N]

Writes ``runs/load_<name>/samples.csv`` (wall_s, sim_minute, controller RSS MB, DB MB, last_event_id) and
``runs/load_<name>/summary.json`` (tick-time percentiles split into phases, ingest events/s, decision lag, wall per
sim day, peak RSS). RSS: ctypes GetProcessMemoryInfo on Windows, /proc elsewhere. No third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DAY = 1440
MARKER = ".load_test_workdir"  # written into every work dir this script creates


def prepare_work_dir(work: Path, force: bool) -> None:
    """Empty ``work`` for a fresh run; never delete a non-empty directory this script did not create."""
    if work.exists() and any(work.iterdir()):
        if not (work / MARKER).exists() and not force:
            raise SystemExit(f"refusing to delete non-empty {work}: not created by load_test.py "
                             f"(no {MARKER} marker); pass --force to wipe it or choose another --work-dir")
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / MARKER).write_text("created by scripts/load_test.py\n")


def rss_mb(pid: int) -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.OpenProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
        if not h:
            return 0.0
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return 0.0
            return pmc.PeakWorkingSetSize / 2**20
        finally:
            k32.CloseHandle(h)
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def dist(xs: list[float]) -> dict:
    return {"n": len(xs), "sum": round(sum(xs), 2), "p50": round(pct(xs, .5), 3), "p95": round(pct(xs, .95), 3),
            "max": round(max(xs, default=0), 3), "mean": round(statistics.fmean(xs), 3) if xs else 0}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def summarise(db: Path, samples: list[tuple], t_ctl_start: float, hist_events: int) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    m = con.execute("SELECT sim_minute, events_ingested, ingest_wall_s, lag_s, tick_wall_s, step_minutes FROM metrics"
                    " ORDER BY id").fetchall()
    try:
        ph = con.execute("SELECT sim_minute, reconcile_s, ingest_s, apply_s, detect_s, plan_s, observer_s"
                         " FROM tick_phases ORDER BY id").fetchall()
    except sqlite3.Error:
        try:  # databases from before the observer phase existed
            ph = [r + (None,) for r in con.execute(
                "SELECT sim_minute, reconcile_s, ingest_s, apply_s, detect_s, plan_s FROM tick_phases"
                " ORDER BY id").fetchall()]
        except sqlite3.Error:
            ph = []
    out: dict = {}
    live = [r for r in m if r[1] and r[0] > 0]
    out["ticks"] = len(m)
    out["tick_wall_s"] = dist([r[4] for r in m])
    out["tick_ingest_s"] = dist([r[2] for r in m])
    out["tick_noningest_s"] = dist([max(0.0, r[4] - r[2]) for r in m])
    out["lag_s"] = dist([r[3] for r in m])
    ev, ws = sum(r[1] for r in m), sum(r[2] for r in m)
    out["events_ingested_in_ticks"] = ev
    out["ingest_events_per_s"] = round(ev / ws, 0) if ws else None
    for i, name in enumerate(("reconcile_s", "ingest_s", "apply_s", "detect_s", "plan_s", "observer_s"), 1):
        out[f"phase_{name}"] = dist([r[i] for r in ph if r[i] is not None])
    out["ticks_with_events"] = len(live)
    first = m[0][0] if m else 0
    out["first_tick_sim_minute"] = first
    # per sim day: events, tick time and wall (from the 10 s samples)
    by_day: dict[int, dict] = {}
    for r in m:
        d = by_day.setdefault(r[0] // DAY, {"ticks": 0, "tick_s": 0.0, "events": 0, "ingest_s": 0.0})
        d["ticks"] += 1
        d["tick_s"] += r[4]
        d["events"] += r[1]
        d["ingest_s"] += r[2]
    wall_at: dict[int, float] = {}
    for wall, minute, _rss, _db, _eid in samples:
        wall_at.setdefault(minute // DAY, wall)
    days = sorted(by_day)
    for i, d in enumerate(days):
        nxt = wall_at.get(days[i + 1]) if i + 1 < len(days) else None
        cur = wall_at.get(d)
        by_day[d]["wall_s"] = round(nxt - cur, 1) if nxt is not None and cur is not None else None
        by_day[d]["tick_s"] = round(by_day[d]["tick_s"], 1)
        by_day[d]["ingest_s"] = round(by_day[d]["ingest_s"], 1)
    out["per_sim_day"] = by_day
    out["peak_rss_mb"] = round(max((s[2] for s in samples), default=0), 0)
    out["db_mb_final"] = round(max((s[3] for s in samples), default=0), 0)
    for tbl in ("calls", "leads", "cube", "decisions", "proposals", "config_versions"):
        try:
            out[f"rows_{tbl}"] = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.Error:
            pass
    con.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_03_load.yaml")
    ap.add_argument("--max-sim-days", type=float, default=None, help="stop the controller after N sim days")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--force", action="store_true", help="wipe a non-empty --work-dir not created by this script")
    args = ap.parse_args()
    name = Path(args.scenario).stem
    work = Path(args.work_dir) if args.work_dir else ROOT / "runs" / f"load_{name}"
    prepare_work_dir(work, args.force)
    state_dir = work / "state"
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    t0 = time.monotonic()
    sim = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", args.scenario, "--run-dir",
                            str(work / "sim_run"), "--port", str(port), "--force"], cwd=ROOT, env=env,
                           stdout=(work / "sim.out").open("wb"), stderr=subprocess.STDOUT)
    try:
        while True:
            try:
                httpx.get(url + "/health", timeout=5).raise_for_status()
                break
            except Exception:  # noqa: BLE001
                if time.monotonic() - t0 > 900:
                    raise
                time.sleep(2)
        sim_ready_s = time.monotonic() - t0
        print(f"sim up after {sim_ready_s:.0f}s", flush=True)
        st0 = httpx.get(url + "/sim/state", timeout=30).json()
        cmd = [sys.executable, "-m", "controller", "run", "--url", url, "--state-dir", str(state_dir)]
        if args.max_sim_days:
            cmd += ["--max-sim-minutes", str(int(args.max_sim_days * DAY))]
        t_ctl = time.monotonic()
        ctl = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=(work / "controller.out").open("wb"),
                               stderr=subprocess.STDOUT)
        samples: list[tuple] = []
        while ctl.poll() is None:
            time.sleep(10)
            try:
                s = httpx.get(url + "/sim/state", timeout=30).json()
                dbs = list(state_dir.glob("*.db"))
                dbmb = sum(p.stat().st_size for p in state_dir.glob("*.db*")) / 2**20 if dbs else 0.0
                samples.append((time.monotonic() - t_ctl, s["sim_minute"], rss_mb(ctl.pid), dbmb,
                                s.get("last_event_id", 0)))
                print(f"[{samples[-1][0]:6.0f}s] sim_minute={s['sim_minute']} rss={samples[-1][2]:.0f}MB"
                      f" db={dbmb:.0f}MB", flush=True)
            except Exception:  # noqa: BLE001
                pass
        total_wall = time.monotonic() - t_ctl
        peak = rss_mb(ctl.pid)  # peak working set persists in the handle only while alive; best effort
        with (work / "samples.csv").open("w") as fh:
            fh.write("wall_s,sim_minute,rss_peak_mb,db_mb,last_event_id\n")
            for r in samples:
                fh.write(",".join(f"{x:.1f}" if isinstance(x, float) else str(x) for x in r) + "\n")
        db = max(state_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)
        summ = summarise(db, samples, t_ctl, 0)
        summ.update({"controller_rc": ctl.returncode, "controller_wall_s": round(total_wall, 1),
                     "sim_ready_s": round(sim_ready_s, 1), "start_sim_minute": st0["sim_minute"],
                     "end_sim_minute": httpx.get(url + "/sim/state", timeout=30).json()["sim_minute"],
                     "final_peak_rss_mb": round(max(peak, summ["peak_rss_mb"]), 0)})
        (work / "summary.json").write_text(json.dumps(summ, indent=1, default=str))
        print(json.dumps(summ, indent=1, default=str))
        return 0 if ctl.returncode == 0 else 1
    finally:
        sim.terminate()
        try:
            sim.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sim.kill()


if __name__ == "__main__":
    sys.exit(main())
