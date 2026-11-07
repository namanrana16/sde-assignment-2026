"""Regenerate reports/audit_examples.md by running ``python -m controller why`` on two state databases.

usage: python scripts/gen_audit_examples.py <public_01_db> <public_02_db> [out.md]

Headings are built from each command's own output (the version in its SUMMARY line), so a heading can never
disagree with the text under it. Dev tooling: it only calls the controller CLI.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# (db key, title, why args, optional line cap, extra flags)
EXAMPLES = [
    ("p1", "approved change", ["--workflow", "ks_cancel", "--at", "Day 0 12:30"], None, []),
    ("p1", "auto-applied change", ["--workflow", "nw_renewal", "--at", "Day 5 01:00"], None, []),
    ("p1", "same approved change with --verbose (first 28 lines)",
     ["--workflow", "ks_cancel", "--at", "Day 0 12:30"], 28, ["--verbose"]),
    ("p2", "approved change: the prompt revert (halluc_fix)", ["--workflow", "ks_cancel", "--at", "Day 3 14:00"], None, []),
    ("p2", "auto-applied change: caller-ID evacuation of a flagged DID, with its impact",
     ["--workflow", "ks_cancel", "--at", "Day 2 12:30"], None, []),
    ("p2", "change later rolled back", ["--workflow", "nw_renewal", "--at", "Day 0 14:00"], None, []),
    ("p2", "supervisor edit (prompt)", ["--workflow", "ks_cancel", "--at", "Day 3 12:00"], None, []),
    ("p2", "supervisor edit (voice)", ["--workflow", "nw_renewal", "--at", "Day 5 15:30"], None, []),
]
LEAD = ("p2", ["--lead", "ks_cancel:l00024079", "--day", "3"])
RANGE = ("p2", ["--workflow", "ks_cancel", "--from", "Day 2", "--to", "Day 3"])


def run(db: str, args: list[str], flags: list[str]) -> str:
    env = {**os.environ, "PYTHONUTF8": "1"}
    cmd = [sys.executable, "-m", "controller", "why", "--db", db, *args, *flags]
    out = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    return out.rstrip("\n")


def version_of(text: str) -> str:
    m = re.search(r"WHAT: v(\d+) by (\w+)", text)
    return f"v{m.group(1)} ({m.group(2)})" if m else "no version"


def main() -> None:
    db1, db2 = sys.argv[1], sys.argv[2]
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "reports" / "audit_examples.md"
    dbs = {"p1": db1, "p2": db2}
    scen = {"p1": "public_01", "p2": "public_02"}
    lines = ["# Audit examples (C12): `controller why` on the final graded runs", "",
             f"Runs: runs/final3_mine01 + {db1}; runs/final3_mine02 + {db2} (STATE_DIR state/final3_01 and"
             " state/final3_02; `state/` is git-ignored, regenerate with `scripts/gen_audit_examples.py`)."
             " `--at` Day numbers are 0-based, as the grader prints them. Every heading is built from the output below it.", ""]
    n = 0
    for key, title, args, cap, flags in EXAMPLES:
        text = run(dbs[key], args, flags)
        if cap:
            text = "\n".join(text.splitlines()[:cap]) + "\n..."
        n += 1
        head = f"## {n}. {scen[key]} - {title}: {version_of(text)}"
        cmd = f"$ python -m controller why --db <db> {' '.join(repr(a) if ' ' in a else a for a in args + flags)}"
        lines += [head, "", "```", cmd, text, "```", ""]
    key, args = LEAD
    n += 1
    lines += [f"## {n}. {scen[key]} - `why --lead`: one customer's calls and the decisions behind them", "", "```",
              "$ python -m controller why --db <db> --lead ks_cancel:l00024079 --day 3", run(dbs[key], args, []), "```", ""]
    key, args = RANGE
    n += 1
    lines += [f"## {n}. {scen[key]} - timeline `--from 'Day 2' --to 'Day 3'` (ks_cancel)", "", "```",
              "$ python -m controller why --db <db> --workflow ks_cancel --from 'Day 2' --to 'Day 3'",
              run(dbs[key], args, []), "```", ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out_path} ({n} examples)")


if __name__ == "__main__":
    main()
