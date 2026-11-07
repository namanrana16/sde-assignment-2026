#!/usr/bin/env python
"""R5 determinism check: compare two finished controller runs (decisions, proposals, config versions, grade).

    python scripts/determinism_check.py <stateA> <stateB> [--grade-a runA --grade-b runB --baseline base]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


def db(state: str) -> sqlite3.Connection:
    p = max(Path(state).glob("*.db"), key=lambda x: x.stat().st_mtime)
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def rows(con, sql):
    return con.execute(sql).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--run-a")
    ap.add_argument("--run-b")
    ap.add_argument("--baseline")
    args = ap.parse_args()
    ca, cb = db(args.a), db(args.b)
    ok = True
    queries = {
        "decisions": "SELECT sim_minute, workflow_id, kind, summary, inputs FROM decisions ORDER BY id",
        "proposals": "SELECT proposal_id, workflow_id, kind, state, base_version, created_minute, body, route,"
                     " priority, plan_seq, filed_minute, expires_minute, applied_version, applied_minute"
                     " FROM proposals ORDER BY proposal_id",
        "config_versions": "SELECT * FROM config_versions ORDER BY 1, 2",
        "alerts": "SELECT sim_minute, severity, code, workflow_id FROM alerts ORDER BY id",
    }
    for name, sql in queries.items():
        try:
            ra, rb = rows(ca, sql), rows(cb, sql)
        except sqlite3.Error as exc:
            print(f"{name}: skipped ({exc})")
            continue
        same = ra == rb
        ok &= same
        h = hashlib.sha256(repr(ra).encode()).hexdigest()[:12]
        print(f"{name}: A={len(ra)} B={len(rb)} {'IDENTICAL' if same else 'DIFFERENT'} sha={h}")
        if not same:
            n = 0
            for i, (x, y) in enumerate(zip(ra, rb)):
                if x != y:
                    print(f"  first diff at row {i}:\n   A={str(x)[:400]}\n   B={str(y)[:400]}")
                    n += 1
                    if n >= 3:
                        break
    if args.run_a and args.run_b and args.baseline:
        outs = []
        for run in (args.run_a, args.run_b):
            r = subprocess.run([sys.executable, "-m", "sim", "grade", "--run", run, "--baseline", args.baseline,
                                "--json"], capture_output=True, text=True, env={**__import__("os").environ, "PYTHONUTF8": "1"})
            outs.append(r.stdout)
        same = outs[0] == outs[1]
        ok &= same
        print(f"grade --json: {'IDENTICAL' if same else 'DIFFERENT'} ({len(outs[0])} bytes)")
        if not same:
            ja, jb = json.loads(outs[0]), json.loads(outs[1])
            def walk(x, y, p=""):
                if isinstance(x, dict) and isinstance(y, dict):
                    for k in sorted(set(x) | set(y)):
                        walk(x.get(k), y.get(k), f"{p}/{k}")
                elif x != y:
                    print(f"  {p}: A={x} B={y}")
            walk(ja, jb)
    print("RESULT:", "DETERMINISTIC" if ok else "DIFFERS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
