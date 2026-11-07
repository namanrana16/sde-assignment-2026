# Phase 1 ingest timing - public_03_load, one simulated day

> Superseded: early phase-1 measurement with fewer features enabled; see `public_03_load.md` and `public_03_load_review2.md`.

Command: `python -m controller run --url http://127.0.0.1:8701 --state-dir state --max-sim-minutes 1440`
(Windows 10, Python 3.13, sim server and controller on the same laptop, SQLite on local disk.)

| Item | Value |
|---|---|
| Sim history ingested at startup | 5,537,379 events in 23 chunks (history is ~6.5 days of public_03) |
| Events ingested during sim Day 0 | 840,012 (61 ticks: 15-min steps in the dialling/approver windows, 60-min steps overnight) |
| Controller time inside ticks for Day 0 (ingest + SQLite) | 140.9 s (ingest alone 139.8 s) = about 6,000 events/s |
| Worst single tick | 45 s (one WAL checkpoint on a ~750 MB DB; typical tick 1-3 s) |
| **Total wall time, startup + history ingest + 1 sim day, including sim compute** | **485 s (8.1 min)** |
| Peak working set of the controller process | 1,582 MB (dominated by buffering whole chunks of up to 250k events, as parsed dicts) |
| DB after history + 1 day | 757 MB: 4.84M calls, 1.54M leads, 185k cube rows |
| Alerts raised (cohort self-check etc.) | 0 |

The sim's own history generation (about 124 s before the port opens) is not included; that is
the sim's start-up, not the controller's.

## What this means

* The "<10 wall-minutes per sim day including sim compute" target holds even when the number also
  carries the one-off 5.5M-event history ingest. A steady-state day is well under that: about
  2.3 min of controller time plus the sim's own advance time.
* A first version of the store used SQLite's default WAL auto-checkpoint (1000 pages). Every
  15-minute chunk commit then triggered a checkpoint with fsync, costing about 9 s per tick
  (~1.4k events/s). Raising `wal_autocheckpoint` to 20,000 pages and `cache_size` to 200 MB gave
  about 16x faster commits (0.6 s per 13.5k-event tick). Both are in `controller/settings.py`.
* Peak memory is the number to attack first if the box is 8 GB and the load grows 10x: stream
  chunks instead of materialising them (`first_line`/`iter_chunk_lines` are already separate calls).
* Timing is noisy: I ran the unit tests concurrently for about 25 s during the measured run.
