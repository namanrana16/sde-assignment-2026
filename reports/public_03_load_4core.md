# public_03_load, full 5-day run pinned to 4 CPU cores (after the history fast path)

**Command:** `cmd /c "start /b /wait /affinity F python scripts\load_test.py --work-dir runs\load_4core"`, with `PYTHONUTF8=1`.

**Setup:** the processor affinity mask 0xF pins `load_test.py` and every child process it starts (simulator and controller) to 4 cores.
- The machine is the same Windows laptop, with nothing else running.
- This approximates the 4 vCPU target. It does not reproduce the 8 GB memory limit or Linux.
- Simulator and controller share the 4 cores.

**Raw data:** `runs/load_4core/summary.json`, which is git-ignored. The figures below are copied from it.

| Sim day | Ticks | Wall (incl. sim compute) | Controller tick time | Of which ingest | Events |
|---|---:|---:|---:|---:|---:|
| 0 (includes start-up and history ingest) | 60 | **362 s** | 175 s | 79 s | 840,044 |
| 1 | 60 | **268 s** | 253 s | 130 s | 840,041 |
| 2 | 60 | **344 s** | 311 s | 152 s | 840,039 |
| 3 | 69 | **358 s** | 343 s | 161 s | 840,030 |
| 4 | 96 | **423 s** | 413 s | 156 s | 840,028 |

**Result.** Every sim day is under the 10-minute (600 s) target. The slowest day is Day 4 at 423 s, with 177 s of margin. The controller ran 1,765 s in total, exited with rc 0 and reached sim minute 7200 (the end).

| Measure | Value |
|---|---|
| Ingest inside ticks | 6,190 events/s (4,200,182 events) |
| Tick wall (= decision lag: advance returned to decisions written) | p50 1.74 s, p95 13.1 s, max 64.5 s |
| Phases p95: ingest / detect / plan / apply / observer | 10.7 / 5.9 / 1.8 / 0.24 / 0.04 s |
| Peak controller RSS | 1,485 MB (reached during history ingest) |
| DB at the end (sampled while running, WAL included) | 2,018 MB; 7,433,295 `calls` rows |

**Trend.** The wall time still grows with the run: Day 1 took 268 s and Day 4 took 423 s.
- Ingest time per day rises from 130 s on Day 1 to 156-161 s on days 3-4 as `calls` and its index grow.
- Detector time also rises.
- At this slope, a run several times longer would cross 600 s per day. The fix for that is the partitioning in SUBMISSION section 8.

This run was not graded. Its outcome counts come from the earlier 5-day run in `public_03_load.md`.
