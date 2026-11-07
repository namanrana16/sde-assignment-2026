# Decaying-variant diagnosis: nw_renewal W3 on public_01

Source: the controller's own `decisions` table (`experiment_look` rows, `scope = all`) in the final graded run's state DB, `state/final1/public_01_dev-b730e0b5.db`, at commit c556fce. That run is the one graded in `reports/public_01.md`. The share row comes from the grader's `decaying_variant_share_after_onset` in `reports/public_01.json`.

## What the controller saw (welcome_message, W3 vs control W1)

| Look | Look index | Action | W3 cumulative n / lift / z | W3 last-24h n / lift / z / 80% lower bound |
|---|---|---|---|---|
| D0 21:00 | - | none | 42 / +0.17 / +1.30 | 42 / +0.17 / +1.30 / -0.04 |
| D1 21:00 | 1 | **ramp W2, W3 to 20%** | 113 / +0.25 / +3.34 | 71 / +0.30 / +3.23 / +0.14 |
| D2 21:00 | 2 | none | 261 / +0.25 / +4.45 | 148 / +0.24 / +2.99 / +0.10 |
| D3 21:00 | 3 | none | 472 / +0.18 / +4.02 | 211 / +0.08 / +1.07 / -0.04 |
| D4 21:00 | 4 (last) | none, looks exhausted | 690 / +0.12 / +3.22 | 218 / -0.01 / -0.11 / -0.12 |
| D5 21:00 | - | none | 885 / +0.10 / +3.08 | 195 / +0.04 / +0.54 / -0.08 |
| D6 21:00 | - | W2 promoted (not W3) | 1093 / +0.10 / +3.16 | 208 / +0.07 / +0.87 / -0.06 |

Grader W3 share after onset: D3 0.196, D4 0.214, D5 0.196, D6 0.206, D7 0.086. The share falls on D7 because W2 is promoted and applied at D7 14:30.

## Reading

The last-24h lift dropped from +0.30 to roughly 0, but it never went significantly negative. The recent z stayed between -0.11 and +1.07 on about 200 connects a day. The standard error of a relative lift on about 200 connects is roughly 0.1. A real decay to a lift of a few percent below control is therefore inside the noise of one day.

The two rules that remove a ramped arm both need evidence of harm:
- the ramped-arm decay drop (`EXP_RAMPED_DROP_RECENT_Z = -1.5`);
- the apply-time ramp block (`EXP_RECENT_MIN_CONNECTS = 50`, recent z <= -1).

A rule loose enough to drop W3 here (for example, any arm whose recent point lift is <= 0) drops most null arms. On the null_01 state DBs, 8 of 8 and 5 of 5 ramped null arms had a recent point lift below 0 at some look; 3 of 8 and 2 of 5 crossed the current -1.5 bound. This comes from the PM-requested read-only diagnosis of the `fixerB_null` and `null_01` DBs. A false drop costs learning, not harm, but at that rate the experiment would stop learning anything.

## Separate bug found and fixed (commits cb45c2a, c556fce)

In earlier runs the drop *was* decided (D5 21:00, recent z of about -1.7 to -1.9), but it was then withheld at apply time or rejected by the approver. `_set_desired` shadowed its `prev` snapshot argument, so `_withhold` never restored the arm. The state said "dropped" while the live config kept it at 20% until the experiment ended.

The fix has two parts:
- the snapshot is kept, so a withheld change restores state;
- `exp_reconcile.lost_change` compares the experiment state with the live config every tick and re-files a lost change.

Apply-time drop vetoes now need a real recovery (`EXP_DROP_VETO_RECOVERED_Z = -1.0`), not noise at the bound.

## Verdict

On the final public_01 run, the decaying W3 held at its ramp share because of detection power, not a bug. The cost is about 4 days at a 20% share of a variant that was a few percent worse than control. The grader's harmful line is per change, and none of our changes was graded harmful. Making the rule more sensitive would trade this for a higher false-drop rate on null arms. `SUBMISSION.md` lists this as a weakness.
