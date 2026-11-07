#!/usr/bin/env python
"""Profile the per-tick detector/planner work against a finished load-test state DB (dev tool).

    python scripts/profile_detectors.py <state.db copy> <sim_url> [--from-minute 6900] [--ticks 6] [--cprofile out.prof]

The simulator (any public_03 world, only used for static workflow metadata) must be up. The DB is modified
(detector state), so pass a copy.
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import levers  # noqa: E402,F401
from controller.client import SimClient  # noqa: E402
from controller.desired import DEFAULT_REGISTRY  # noqa: E402
from controller.governance import Governance  # noqa: E402
from controller.planner import Planner  # noqa: E402
from controller.store import Store  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("url")
    ap.add_argument("--from-minute", type=int, default=6900)
    ap.add_argument("--ticks", type=int, default=6)
    ap.add_argument("--cprofile")
    a = ap.parse_args()
    store = Store(Path(a.db))
    client = SimClient(base_url=a.url)
    wfs = {w["workflow_id"]: w for w in client.workflows()}
    gov = Governance(client, store, wfs)
    planner = Planner(client, store, gov, DEFAULT_REGISTRY)
    prof = cProfile.Profile() if a.cprofile else None
    for i in range(a.ticks):
        now = a.from_minute + 15 * i
        t = {}
        for det in DEFAULT_REGISTRY.detectors:
            t0 = time.perf_counter()
            if prof:
                prof.enable()
            det(planner.context(now))
            if prof:
                prof.disable()
            t[det.__module__ + "." + det.__name__] = time.perf_counter() - t0
        t0 = time.perf_counter()
        if prof:
            prof.enable()
        planner.plan(now)
        if prof:
            prof.disable()
        t["planner.plan"] = time.perf_counter() - t0
        print(f"tick now={now}: total {sum(t.values()):.2f}s  " + "  ".join(
            f"{k.split('.')[-2] + '.' + k.split('.')[-1]}={v:.2f}" for k, v in t.items() if v > 0.05), flush=True)
    if prof:
        prof.dump_stats(a.cprofile)
        pstats.Stats(prof).sort_stats("cumulative").print_stats(35)


if __name__ == "__main__":
    main()
