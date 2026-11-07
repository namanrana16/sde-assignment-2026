# Per-person call time: analysis only (C4b)

Produced by `python -m controller person-analysis --db <store>` (module `controller/person_analysis.py`). Nothing is written
to config; the run writes a `person_analysis` table and one `decisions` row (kind `person_analysis`) per workflow.

**Method.** Returning persons = seen in >= 2 campaigns of the workflow. Eligible = >= 4 dials over >= 2 hour blocks (the dialling
window split into 4 blocks, 3 for a window under 10 h). Per block a Beta posterior of pickup with the person's modal cohort's block
pickup as the prior, strength 20 pseudo-dials per block (`PERSON_PRIOR_STRENGTH`). A preferred block would be assigned only if
P(pickup of the best-posterior block > the cohort's average pickup) >= 0.8. "Lift" = posterior mean of that block over the cohort
average, averaged over qualifying persons, ignoring capacity. The null check redraws every eligible person's connects from the
cohort block rates (no person effect at all), keeps their dials per block and reruns the same rule.

## Numbers

Stores: `state/public_01_dev/public_01_dev-b730e0b5.db` (public_01, 7 days) and `state/public_02_incidents/public_02_incidents-afcb9425.db` (public_02, 9 days). Analysis ran on copies.

| store / workflow | eligible | qualify | on cohort prior alone | qualify under NO person effect | data weight | lift ignoring capacity (null) | of which cohort prior | workflow upper bound |
|---|---|---|---|---|---|---|---|---|
| public_01 ks_cancel | 6,516 | 397 | 0 | 319 | 9.7% | +41.7% (+40.2%) | +13.1% | +1.31% |
| public_01 nw_kyc | 2,055 | 122 | 0 | 110 | 9.0% | +40.7% (+41.7%) | +12.5% | +0.80% |
| public_01 nw_renewal | 5,513 | 445 | 0 | 381 | 10.4% | +39.2% (+38.5%) | +10.9% | +2.08% |
| public_02 ks_cancel | 7,076 | 593 | 0 | 455 | 9.7% | +39.8% (+40.1%) | +11.2% | +1.66% |
| public_02 nw_kyc | 2,319 | 145 | 0 | 109 | 9.0% | +37.5% (+37.8%) | +9.6% | +0.75% |
| public_02 nw_renewal | 6,069 | 530 | 163 | 438 | 10.4% | +40.9% (+39.9%) | +16.6% | +2.30% |

Data weight = mean n/(n+20) over the person's dialled blocks: the posterior is about 90% cohort prior. Qualifying persons carry 2-6% of dials,
so the workflow-level ceiling (lift x their dial share) is +0.8% to +2.3%, and that is before any capacity effect.

## Reading it honestly

1. **The headline per-person lift (about +40%) is largely selection.** A rule that picks the best of 4 blocks per person inflates the estimate;
   with no person effect at all the same rule qualifies 75-90% as many persons with the same +40% lift. The excess over the null is 12-138 persons per workflow
   (public_01 ks_cancel 397 vs 319, nw_kyc 122 vs 110, nw_renewal 445 vs 381; public_02 593 vs 455, 145 vs 109, 530 vs 438), a weak signal on top of a large noise floor.
2. **Little data per person.** 4+ dials over a 10-12 h window is too few to separate a block effect from noise; the prior dominates (data weight about 10%).
3. **Realised lift is lower still.** `lead_preferred_hour` is x6 in the chosen hour and x0.25 in all others under a fixed hourly capacity, so treated persons take
   dial slots from others. The figures above ignore that. For the same reason a person-level holdout is biased: treated persons displace controls, so even a deployed trial could not
   measure the effect cleanly.
4. The cohort-level hour tables (C4a) already capture the part of the gain that is predictable from the cohort (the "cohort prior" column).

## Conclusion (for the "thing we cut" discussion and the Product memo reply)

We built per-person call time as an analysis and deliberately did not deploy it. On public_01 and public_02 only 6-9% of returning persons (122-593 per workflow) would clear a
P >= 0.8 bar, the posterior is about 90% the cohort prior, and a no-signal control run through the same rule produces 75-90% as many "qualifiers" with the same apparent +40% lift,
so the figure Product would put in a headline is mostly the winner's curse. Even taken at face value qualifying persons carry 2-6% of dials, an upper bound of +0.8% to +2.3% pickup per workflow
before the x0.25 off-hour penalty and fixed hourly capacity (treated persons displace others' dials) cut it further, and a person-level holdout cannot measure it because treated persons displace controls. Per-cohort hour weights (C4a) deliver
the defensible share of the call-time gain with a measurable before/after check; per-person assignment stays an auditable analysis that is run on demand (`python -m controller person-analysis`; the daily job is wired into the tick but the flag `ENABLE_PERSON_ANALYSIS` is off), and we would revisit it only with much more history per person
or a per-person hour mechanism that does not compete for capacity.
