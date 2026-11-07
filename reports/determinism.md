# Determinism check (R5)

## Final verification (2026-11-07, commit c556fce)

Two clean concurrent runs of `public_01_dev` (`scripts/det_run.sh a|b`, random `PYTHONHASHSEED`s, ports 8961/8962,
own sim + run dir + state dir), compared with `scripts/determinism_check.py` against baseline `runs/final_base1`.
The graded `run.sh` run (`state/final1`, run alone-ish, concurrent with public_02) was also compared with run A.

```
decisions: A=2545 B=2545 IDENTICAL sha=98bbc701d749
proposals: A=24 B=24 IDENTICAL sha=5e9574965b43
config_versions: A=25 B=25 IDENTICAL sha=815213388dfc
alerts: A=3 B=3 IDENTICAL sha=ab03d5454b23
grade --json: IDENTICAL (14139 bytes)
RESULT: DETERMINISTIC
```

Run A vs the graded run (`state/final1`, `runs/final_mine1`): the same four shas and identical grade JSON.

The sections below are the historical record of how the problem was found and fixed.


Method: `scripts/det_run.sh` runs one clean controller run of `public_01_dev` (own simulator, own run dir, own
state dir, no baseline reuse); two runs are started **concurrently** with random `PYTHONHASHSEED`s so CPU timing and
hash ordering both differ. `scripts/determinism_check.py` then compares, row by row, `decisions` (sim_minute,
workflow, kind, summary, inputs: there is no wall-clock column in it), `proposals`, `config_versions`, `alerts`, and the
byte output of `python -m sim grade --json`.

## Result before the fix: NOT deterministic

Two runs diverged at sim minute 2355 (Day 1 15:15): 2571 vs 2553 decisions, 24 vs 22 proposals, 20 vs 24 config
versions, different grade JSON. Bisection:

* In-process, sequential, same interpreter: identical (so no wall-clock or tick-sizing dependence).
* Fixed `PYTHONHASHSEED=0` on both processes: identical.
* Simulator fixed at seed 0, controller seeds 1 vs 2: different; seeds 0 vs 1 identical. So the cause is hash-order
  dependence in the **controller process**, not the simulator.
* The first differing event was a call whose `caller_id` differed although the config version, lead and RNG draw were
  the same. The simulator samples options in the **key order of the allocation dict it receives**
  (`opts = list(dist); rng.choice(len(opts), p=...)`). The DID planner built the stepped allocation as
  `{k: ... for k in set(cur) | set(tgt)}`, i.e. in string-hash (per-process random) order, so identical weights were
  sent with a different key order and mapped the same random draw to a different caller ID. Everything after that
  point diverged (different data, different decisions).

## Fixes

* `configlib.normalise_weights` returns allocation dicts in sorted key order (canonical body; every proposed config
  goes through it), with a regression test.
* `did_plan` iterates `sorted(set(cur) | set(tgt))`.
* Other float sums over sets of strings made order-independent (`sorted(...)`): `exp_stats` cluster-robust variance
  over leads, `configlib.tv_by_cohort`, `did_plan.tv`, `exp_data` arm dict.

## Result after the fix: deterministic

Two concurrent clean runs with random hash seeds: `decisions` 2543 = 2543 rows IDENTICAL, `proposals` 21 = 21
IDENTICAL, `config_versions` 22 = 22 IDENTICAL, `alerts` IDENTICAL, `grade --json` IDENTICAL (12,827 bytes).

```
decisions: A=2543 B=2543 IDENTICAL sha=ef7c7ed8be68
proposals: A=21 B=21 IDENTICAL sha=2963b13d0fb1
config_versions: A=22 B=22 IDENTICAL sha=808bbd20b1a1
alerts: A=1 B=1 IDENTICAL sha=56c94e9d0846
grade --json: IDENTICAL (12827 bytes)
RESULT: DETERMINISTIC
```

Note for graders: the simulator itself is deterministic given a fixed sequence of controller requests (checked
by scripted replays with different hash seeds), so any residual run-to-run difference would have to come from the
controller. Reproduce: `scripts/det_run.sh a scenarios/public_01_dev.yaml 8721 &` and the same with tag `b`/port 8722,
then `python scripts/determinism_check.py state/det_a state/det_b --run-a runs/det_a --run-b runs/det_b --baseline runs/base_public_01_dev`.
