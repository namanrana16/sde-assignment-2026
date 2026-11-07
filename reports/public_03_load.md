# public_03_load: load test (12 workflows, 840,000 events per sim day, 5 sim days)

> Day 0, ingest rate, RSS and DB size superseded by `public_03_load_review2.md` (history fast path); days 1-4 stand as measured before it.

Command: `python scripts/load_test.py` (serves the scenario, runs `python -m controller run` with every feature on, samples
wall clock, controller RSS and DB size every 10 s, then summarises the controller's own `metrics` and `tick_phases` tables).
Windows 10, 8 cores, Python 3.13, simulator and controller on the same laptop, SQLite on local disk. The run went to the END
of the scenario (7,200 sim minutes); no extrapolation. Raw data: `runs/load_public_03_load/{summary.json,samples.csv}`
(final run) and `runs/load_before/` (same test before the optimisations below); both are git-ignored.

## Headline

| Target / metric | Result |
|---|---|
| Wall per sim day, including sim compute | **380 / 422 / 499 / 504 s for days 1-4 (6.3 to 8.4 min): under the 10 min target.** Day 0 is 739 s because it also carries the one-off history ingest (about 6.4 min). Before the optimisations days 3 and 4 were 700 and 772 s (11.7 and 12.9 min): over target. |
| Total | 2,565 s (42.8 min) of controller wall from start to `finished`, plus 102 to 158 s of simulator start-up (history build) |
| Ingest | 5,382 events/s (4.20M events in the 301 ticks, 780 s inside ingest); 840,03x events per sim day in 60 ticks |
| Controller time per tick | p50 1.74 s, p95 13.5 s, max 69.5 s (mean 4.9 s). Split: ingest p50 0.61 / p95 10.6 / max 62.4 s; detectors p50 0.90 / p95 5.4 / max 26.9 s; planner p50 0.02 / p95 3.1 / max 7.7 s; reconcile and apply negligible |
| Decision lag (advance returned until decisions written) | p50 1.7 s, p95 13.5 s, max 69.5 s. A tick is 15 sim minutes and the sim does not run in real time, so lag only matters against the 24 h apply windows (seconds against hours); no `ingest_stalled` alert fired |
| Peak RSS (controller, `GetProcessMemoryInfo` peak working set) | 1,422 MB. It is reached while ingesting the history chunks at start-up and never exceeded afterwards; steady-state RSS was not sampled separately |
| DB size | 892 MB after the history ingest, then +107, +118, +152, +113 MB per sim day (about 120 MB/day; 1.38 GB at the end). 7.43M `calls` rows, 2.30M `leads`, 0.67M `cube` rows, 5.7k decisions, 76 proposals |
| Controller exit | rc 0, no restarts, `lapsed` 0, `apply_conflicts` 0, `auto_apply_rejected` 0 |

Per sim day (controller seconds inside ticks, of which ingest, and wall including sim compute):

| day | tick seconds | ingest s | detectors + planner + apply s | sim compute (wall - tick) | wall s |
|---|---|---|---|---|---|
| 0 | 242 | 142 | 100 | n/a (carries history ingest) | 739 |
| 1 | 290 | 167 | 123 | 90 | 380 |
| 2 | 290 | 152 | 138 | 132 | 422 |
| 3 | 311 | 161 | 150 | 188 | 499 |
| 4 | 336 | 159 | 177 | 168 | 504 |

Before and after the optimisations (same test, same scenario):

| | before | after |
|---|---|---|
| detector time, 5 days | 1,144 s | 570 s |
| tick p95 / max | 27.9 s / 82.6 s | 13.5 s / 69.5 s |
| controller seconds per sim day, days 1 to 4 | 339, 392, 535, 582 | 290, 290, 311, 336 |
| wall per sim day, days 1 to 4 | 439, 527, 700, 772 s | 380, 422, 499, 504 s |

The wall per day still rises with the data (sim compute grows with enrolled leads; ingest grows with DB size), but the
controller-side slope fell from about +80 s/day to about +15 s/day over days 2 to 4.

## What the profile showed, and what I changed

`scripts/profile_detectors.py` runs the registered detectors and planner against a copy of the finished state DB (cProfile).
The per-tick floor is cheap (halluc 0.9 s + DID 0.7 s for 12 workflows). The cost sat in three spike patterns: 21:00
(end-of-day experiment looks), hourly (experiment harm and decay checks) and 00:00 (schedule re-plan), growing with the days.
Cause in each case: raw scans of the `calls` table.

1. **`lid IN (SELECT lid FROM leads WHERE cell IN ...)`**, the cohort filter of the experiment arm queries, scanned the whole
   `leads` table (2.3M rows, all workflows) for every query: 1.4 to 1.65 s per query versus 0.13 s without the filter. Rewritten as
   a correlated `EXISTS` on the primary key (one probe per call row): about 9x faster. On the same data the end-of-day experiment
   detector went from 43 s to 4.6 s.
2. **Completed-day cache** for experiment arm counts (`exp_data.load_arms`, table `exp_day_cache`): days that are finished and
   wholly inside the experiment window are served from the cache and only the partial first day and today are scanned, so the
   cost stops growing with the length of the experiment. Equality with a raw scan is tested (including the cell filter).
   On its own this did NOT reduce the spikes (a second full run showed detector time 1,253 s vs 1,144 s): the cell-filter query was the
   real cost; the cache is kept because it removes the day-count dependence once the filter is cheap.
3. **Per-day cache of the schedule strata** (`schedule.strata_rows`): the 14-day join of calls, leads and campaigns used for
   per-cohort call times, once per day per workflow (1.35 s each at 5 days, growing towards 14 days). Equality with the single SQL is tested.

Remaining spikes in the final run: 09:00 ingest (40 to 62 s: the daily batch brings about 190k new leads plus the first morning
calls in one 54 MB chunk), 21:00 detectors 12 to 27 s, 00:00 detectors about 19 s (12 schedule plans).

## What breaks first at 10x (about 8.4M events/day, 120 workflows or 10x the dial rate)

Ordered by what hits the 10 min per sim day budget first, with the measurement behind each.

1. **SQLite commit and checkpoint of random index pages in ingest, not JSON parsing.** Ingest is 156 s/day now, so 10x is
   26 min/day from ingest alone. Micro-benchmark (`scripts/ingest_bench.py`): replay of 67,500 real events (5 chunks) into a copy of the
   1.15 GB production DB runs at 5.6k events/s and about 85% of the time (about 11 of 13 s) is inside `COMMIT`: every call row dirties a
   random leaf page of the `calls_lead (lid)` index, so a 13.5k-row chunk writes about 13k scattered pages through the WAL and the
   20,000-page auto-checkpoint copies them back. Same replay with `wal_autocheckpoint` at 200,000 pages: 23.8k events/s (deferred,
   not removed: the random writes still happen at the checkpoint, de-duplicated, and this 5-chunk replay never reaches it, so it
   overstates the gain); with the `calls_lead` index dropped: 27.7k events/s. `json.loads` alone is 124k events/s per core (46% of
   ingest only on an empty DB). So the order is: page write amplification of one writer first, JSON parse second (at 10x about
   68 s/day of parse per core, fine).
2. **Per-tick detector queries over a growing `calls` table.** After the fixes the floor is 0.9 s p50 for 12 workflows; it is
   per-workflow work, so 120 workflows is about 9 s p50 and an end-of-day tick of minutes. The experiment tests need per-lead counts
   (cluster-robust variance), so the day cache holds one row per lead and day: it bounds recomputation, not size. `calls` itself
   (7.4M rows, 1.38 GB; 74M rows and about 14 GB per 5 days at 10x) no longer fits the 200 MB page cache, so scans become disk bound.
3. **Memory.** 1.42 GB peak is reached while ingesting the history chunks at start-up and comes from materialising a whole chunk as parsed dicts (the 54 MB daily morning chunk is the largest steady-state chunk; it was not shown to be the peak). A 10x chunk is
   about 540 MB of text and tens of GB of dicts: chunks must be streamed (the client already splits `first_line` and `iter_chunk_lines`)
   with the transaction spanning sub-batches only.
4. **The simulator** (90 to 190 s per sim day, single process, growing with enrolled leads) is not ours to fix but is a third of the
   wall time and would be the next wall at 10x.
5. **`calls` retention**: nothing reads per-call rows older than the longest window (14 days for the schedule model, the experiment
   window for arms). `cube` (0.67M rows) carries everything else.

Not the bottleneck (measured): planner and apply (under 100 s over 5 days), reconcile, `decisions` growth (5.7k rows), the approval path
(76 proposals), JSON parse, detector memory.

### How I would partition

* **Shard by workflow (group by tenant).** All state is per workflow: fatigue ledger, approvals, DID pool (caller IDs are workflow
  options and the sim keeps `did_load` per workflow), experiment and halluc state, cohorts. The only cross-workflow state is the
  credits budget (400 here, one pool): give each shard a reserved share plus a shared top-up, with the incident reserve kept per shard.
* One controller process and one SQLite DB per shard of 2 to 4 workflows (4 tenants: 4 processes, or finer). Each shard still
  reads the global chunk stream, so it pre-filters lines on `"workflow_id": "..."` before `json.loads` (the code already does this
  for `config_changed`), cutting parse work to its share. A tenant boundary also gives a natural blast radius.
* Within a shard: batch commits across chunks, set `wal_autocheckpoint` high and run an explicit `wal_checkpoint` on idle ticks
  (the loop already knows them: the 60 min steps outside dialling and approver windows), replace the random-insert `calls_lead`
  index by a lead-day aggregate for the experiment queries, keep per-call rows only for the longest window.
* Beyond SQLite: Postgres or a column store for `calls` with the same partitioning keys; controller processes stay stateless apart
  from `jobs` and `detector_state`.

I did not apply the ingest-side changes in this pass: the replay benchmark shows the headroom, but the checkpoint trade
(larger WAL, slower crash recovery, a long checkpoint tick) deserves its own crash-test run.

## Grade of the load run (`reports/dev/public_03_grade.md`, vs a no-op baseline, `runs/base_public_03_load`)

Across the 12 workflows: conclusion rate +13.6%, pickup +9.3%, engagement +2.2%, hallucinated calls 5,190 vs 4,729 baseline (+10%, in
proportion to the extra connects; the scenario has no incidents). 70 changes applied: 33 beneficial, 37 neutral, **0 harmful**; 48
approval requests (43 approved, 5 declined as judgement calls), 28 auto-applies, `lapsed` 0, `apply_conflicts` 0,
`auto_apply_rejected` 0, at most 2 requests in 24 h per workflow. Credits used 100 of 400 (41 validation runs).

## Reproduce

```
python scripts/load_test.py                           # about 43 min on this laptop (+ about 2.5 min sim start-up)
python scripts/load_test.py --max-sim-days 2          # shorter, for iteration
python -m sim baseline --scenario scenarios/public_03_load.yaml --run-dir runs/base_public_03_load   # 4.6 min
PYTHONUTF8=1 python -m sim grade --run runs/load_public_03_load/sim_run --baseline runs/base_public_03_load
python scripts/profile_detectors.py <copy of a state db> <sim url> --from-minute 7020 --cprofile out.prof
CONTROLLER_PROFILE=out.prof python -m controller run ...   # cProfile of a whole run
```
