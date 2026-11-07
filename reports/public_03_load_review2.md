# public_03_load, Day 0 after the history fast path (review 2)

Command: `PYTHONUTF8=1 python scripts/load_test.py --max-sim-days 1 --work-dir runs/load_review2`.
The machine was a Windows laptop with nothing else running, so it is not the 4 vCPU / 8 GB Linux target. This is one run.
The raw data is `runs/load_review2/summary.json`, which is git-ignored. The figures below are copied from it.

**What changed since `public_03_load.md`.**
- History chunks are now ingested without the `calls_lead` index and with a large `wal_autocheckpoint`.
- The index and the `calls_index_built` meta flag are then created in one transaction. Live chunks are never ingested before that flag exists.
- `synchronous` stays NORMAL, so the ingest cursor is exactly as durable as the rows.
- The per-tick alert and metric queries no longer scan `calls`.
- The observer's cost is now timed as its own phase.

| Measure | `public_03_load.md` (before) | This run |
|---|---:|---:|
| Day 0 wall (controller start to the first Day-1 tick, including sim compute and history ingest) | 739 s | **353 s** |
| Controller wall, start to stop at Day 1 00:00 | — | 363 s |
| Day-0 tick time (sum) | 242 s | 172 s |
| Ingest inside ticks | 5,382 events/s | 11,000 events/s (840,047 events, 77 s) |
| Observer phase (alerts + metrics), sum over 61 ticks | not measured | 0.11 s |
| Tick p50 / p95 / max ingest phase | — | 0.49 / 2.68 / 39.8 s |
| Peak controller RSS | 1,422 MB | 1,486 MB |
| DB size at Day 1 | 892 MB | 782 MB (main file, WAL 0 bytes after exit) |

**Result.** Day 0 now fits the under-10-minute target, with about 250 s of margin, on this machine.

**Disk use while running.** `summary.json` reports `db_mb_final` 1,516 MB. That sample was taken while the WAL was still large: the history phase allows a WAL of about 1 GB before checkpointing. Plan for roughly 1 GB of extra free disk during history ingest.

**Crash safety.** `scripts/crash_test.py` was re-run on the same tree, with 11 of 11 fault points, 10 random kills and 22 restarts, and ALL INVARIANTS PASS. The log is `reports/crash_test.log`. The unit tests `tests/controller/test_ingest_perf_review2.py` cover a restart mid-history and a crash after the last history chunk but before the index.
