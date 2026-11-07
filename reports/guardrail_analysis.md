# Guardrail selection-effect analysis (devscenario null_01)

> Run on an earlier `null_01` run (ALL -0.9%), not the final one (`reports/dev/null_01.md`, ALL -1.3%); the decomposition was not re-run.

Question: in `null_01` the engagement guardrail metric (engaged / connected) fell versus the no-op baseline although no
harmful change was applied (ks_cancel -1.0%, nw_kyc -1.9%, nw_renewal -1.2%, ALL -0.9% in this run; -2.6% / -0.3% / 0.0%
in an earlier run of the same scenario). Hypothesis: higher pickup puts more repeat connects of low-propensity leads in
the denominator, so engaged/connected falls although the conversation is no worse.

Method: `python scripts/guardrail_analysis.py runs/mine_null_01 runs/base_null_01` (event files of our run and of the
baseline; same scenario, same seed). Run of 2026-11-07 after the determinism and planner fixes.

## Mechanism that makes the hypothesis plausible (read from the simulator)

In the simulator an engaged lead is concluded (`status = 1`) and never dialled again. So every second or later connect is,
by construction, a lead whose previous connect did NOT engage. Engagement on connect #2 is therefore lower than on connect #1
for any controller (baseline ks_cancel: 0.509 first, 0.495 second, 0.471 third). A controller that raises pickup produces
more connects per lead, i.e. a larger share of those lower-engagement repeat connects.

## Result: the stated hypothesis is directionally true but far too small to explain the dip

Decomposition of the change in engaged/connected into (a) engagement on the first connect of each lead, (b) engagement on
repeat connects, (c) the larger share of repeats (binomial standard error of the total in brackets):

| workflow | total | first-connect engagement | repeat-connect engagement | more repeats (share base -> ours) | z of total |
|---|---|---|---|---|---|
| ks_cancel | -0.52 pp (0.72) | +0.08 pp | -0.59 pp | -0.02 pp (16.3% -> 17.3%) | -0.7 |
| nw_kyc | -1.16 pp (1.06) | +0.00 pp | -1.10 pp | -0.06 pp (12.9% -> 16.1%) | -1.1 |
| nw_renewal | -0.66 pp (0.80) | -0.43 pp | -0.18 pp | -0.04 pp (17.9% -> 19.2%) | -0.8 |
| earlier run, same scenario: ks_cancel | -1.33 pp | -0.53 pp | -0.77 pp | -0.03 pp | -1.9 |
| earlier run: nw_kyc | -0.15 pp | +0.22 pp | -0.32 pp | -0.06 pp | -0.1 |
| earlier run: nw_renewal | +0.03 pp | +0.48 pp | -0.41 pp | -0.04 pp | +0.0 |

* Share effect (c): 0.02 to 0.06 pp. The hypothesis as stated explains under 5% of the dip.
* First connect per lead (a): unchanged within noise (mean -0.03 pp over the six runs; first-connect engagement
  0.5092 -> 0.5102 ks, 0.6090 -> 0.6090 kyc, 0.5558 -> 0.5504 renewal). The same population answers the same way on its first
  call, so conversations are not worse.
* Engagement within our run by caller_id (randomised per dial, same population): ks_cancel D1/D2/D3 = 0.493 / 0.506 /
  0.506, nw_kyc = 0.586 / 0.607 / 0.590, nw_renewal = 0.554 / 0.535 / 0.543 (D4: 0.474 on 226 dials). The differences are
  inside binomial noise (about 1 pp at 3k connects per DID) and have no relation to the DID's pickup. Spreading across DIDs
  raises pickup (D1 0.246 vs D2 0.334 in nw_kyc) without changing what happens once the call connects.
* Connects per connected lead rose (nw_kyc 1.148 -> 1.192) and dials per lead did not (2.52 -> 2.49): the extra connects are
  second connects of the extra people reached, as expected.

## What is left: repeat-connect engagement is 0.2 to 1.1 pp lower in all six comparisons

Connect #2 in nw_kyc: 0.587 (n=448) -> 0.518 (n=639), -6.9 pp (about 2.3 sigma); ks_cancel 0.495 -> 0.457; nw_renewal
0.519 -> 0.530. It is negative in 6 of 6 workflow-runs but each is within about 1 sigma of the total and the same code gave
nw_kyc -0.15 pp in one run and -1.16 pp in another (the run-to-run swing is of the order of the effect). The most economical
explanation consistent with everything above is a different selection effect from the one hypothesised: in the baseline the
people who connect twice are selected for high pickup propensity (they got through twice); when DID spreading lifts pickup
for everyone, the second-connect pool is less selected on that trait, and if pickup propensity is mildly positively
correlated with engagement propensity in the simulator's person model the pool engages a little less. I could not test the
correlation from controller-visible data (person propensities are hidden), so this is a candidate, not a finding.

## Conclusion

1. Not a worse conversation: first-connect engagement and engagement by caller ID are unchanged; the grader labels no
   applied change harmful.
2. The dip (-0.5 to -1.2 pp, z between -0.7 and -1.1 per workflow; Stouffer z about -1.5 over the three) is statistically
   indistinguishable from noise, and part of it, if real, is a composition effect in who is on the repeat calls, not
   something a config change did to the call.
3. Repeat share (the hypothesised driver) explains under 5% of it.
4. Consequence for the guardrail: engaged/connected is confounded whenever pickup changes. The metric to trust for "did the
   conversation get worse" is engagement on the FIRST connect per lead (or engagement stratified by connect ordinal). The
   experiment tests already stratify by lead (cluster-robust) and compare arms within the same period, so they are not exposed
   to this; the Day-N KPI-vs-baseline views are. A guardrail trip on engaged/connected alone while pickup is rising should be
   read against first-connect engagement before it is treated as harm.

## Raw output

```

## ks_cancel
| metric | baseline | ours |
|---|---|---|
| dials | 38,400 | 38,400 |
| pickup | 0.2441 | 0.2555 |
| engaged / connected | 0.5063 | 0.5011 |
| distinct leads dialled | 13,861 | 13,875 |
| leads connected at least once | 7,848 | 8,115 |
| dials per lead | 2.7704 | 2.7676 |
| connects per connected lead | 1.1946 | 1.2091 |
| engaged on FIRST connect per lead | 0.5092 | 0.5102 |

Engagement by connect ordinal (rate, n)  baseline | ours
  connect #1: 0.5092 (7848) | 0.5102 (8115)
  connect #2: 0.4951 (1319) | 0.4570 (1466)
  connect #3: 0.4709 (189) | 0.4833 (209)
  connect #4+: 0.4737 (19) | 0.2727 (22)
Repeat connects: engaged rate given the lead's previous connect was (not) engaged (rate, n)
  previous not engaged: 0.4918 (1527) | 0.4579 (1697)
By attempt_no (pickup, engaged/connected, dials)  baseline | ours
  attempt 1: 0.255 0.5142 11957 | 0.266 0.5129 11956
  attempt 2: 0.248 0.5054 10098 | 0.260 0.5092 10085
  attempt 3: 0.235 0.5061 7992 | 0.245 0.4861 7953
  attempt 4+: 0.232 0.4954 8353 | 0.246 0.4869 8406
Decomposition of the change in engaged/connected (-0.52 pp, binomial SE 0.72 pp, z -0.7):
  first-connect engagement itself : +0.08 pp
  engagement of repeat connects   : -0.59 pp
  more repeats (share 0.163 -> 0.173): -0.02 pp
Our run by caller_id (pickup, engaged/connected, dials)
  D1: pickup 0.253  engaged/connected 0.4926  dials 13808
  D2: pickup 0.249  engaged/connected 0.5060  dials 13977
  D3: pickup 0.267  engaged/connected 0.5056  dials 10615

## nw_kyc
| metric | baseline | ours |
|---|---|---|
| dials | 16,000 | 16,000 |
| pickup | 0.2384 | 0.2979 |
| engaged / connected | 0.6064 | 0.5948 |
| distinct leads dialled | 6,345 | 6,422 |
| leads connected at least once | 3,322 | 4,000 |
| dials per lead | 2.5217 | 2.4914 |
| connects per connected lead | 1.1481 | 1.1915 |
| engaged on FIRST connect per lead | 0.6090 | 0.6090 |

Engagement by connect ordinal (rate, n)  baseline | ours
  connect #1: 0.6090 (3322) | 0.6090 (4000)
  connect #2: 0.5871 (448) | 0.5180 (639)
  connect #3: 0.5854 (41) | 0.5487 (113)
  connect #4+: 1.0000 (3) | 0.4286 (14)
Repeat connects: engaged rate given the lead's previous connect was (not) engaged (rate, n)
  previous not engaged: 0.5894 (492) | 0.5209 (766)
By attempt_no (pickup, engaged/connected, dials)  baseline | ours
  attempt 1: 0.252 0.6120 5544 | 0.330 0.6212 5617
  attempt 2: 0.232 0.5932 4444 | 0.291 0.5737 4364
  attempt 3: 0.231 0.6228 3152 | 0.268 0.5788 3124
  attempt 4+: 0.230 0.5973 2860 | 0.278 0.5844 2895
Decomposition of the change in engaged/connected (-1.16 pp, binomial SE 1.06 pp, z -1.1):
  first-connect engagement itself : +0.00 pp
  engagement of repeat connects   : -1.10 pp
  more repeats (share 0.129 -> 0.161): -0.06 pp
Our run by caller_id (pickup, engaged/connected, dials)
  D1: pickup 0.246  engaged/connected 0.5858  dials 5812
  D2: pickup 0.334  engaged/connected 0.6067  dials 5196
  D3: pickup 0.322  engaged/connected 0.5900  dials 4992

## nw_renewal
| metric | baseline | ours |
|---|---|---|
| dials | 28,800 | 28,800 |
| pickup | 0.2622 | 0.2781 |
| engaged / connected | 0.5504 | 0.5438 |
| distinct leads dialled | 10,040 | 10,047 |
| leads connected at least once | 6,202 | 6,473 |
| dials per lead | 2.8685 | 2.8665 |
| connects per connected lead | 1.2175 | 1.2371 |
| engaged on FIRST connect per lead | 0.5558 | 0.5504 |

Engagement by connect ordinal (rate, n)  baseline | ours
  connect #1: 0.5558 (6202) | 0.5504 (6473)
  connect #2: 0.5187 (1151) | 0.5301 (1279)
  connect #3: 0.5978 (179) | 0.4661 (221)
  connect #4+: 0.2632 (19) | 0.3143 (35)
Repeat connects: engaged rate given the lead's previous connect was (not) engaged (rate, n)
  previous not engaged: 0.5256 (1349) | 0.5160 (1535)
By attempt_no (pickup, engaged/connected, dials)  baseline | ours
  attempt 1: 0.280 0.5471 8737 | 0.290 0.5481 8744
  attempt 2: 0.261 0.5564 7260 | 0.272 0.5323 7232
  attempt 3: 0.253 0.5395 5643 | 0.278 0.5679 5663
  attempt 4+: 0.249 0.5572 7160 | 0.270 0.5303 7161
Decomposition of the change in engaged/connected (-0.66 pp, binomial SE 0.80 pp, z -0.8):
  first-connect engagement itself : -0.43 pp
  engagement of repeat connects   : -0.18 pp
  more repeats (share 0.179 -> 0.192): -0.04 pp
Our run by caller_id (pickup, engaged/connected, dials)
  D1: pickup 0.278  engaged/connected 0.5539  dials 10960
  D2: pickup 0.268  engaged/connected 0.5347  dials 10880
  D3: pickup 0.299  engaged/connected 0.5432  dials 6734
  D4: pickup 0.168  engaged/connected 0.4737  dials 226
```
