# Audit examples (C12): `controller why` on the final graded runs

Runs: runs/final3_mine01 + state/final3_01/public_01_dev-b730e0b5.db; runs/final3_mine02 + state/final3_02/public_02_incidents-afcb9425.db (STATE_DIR state/final3_01 and state/final3_02; `state/` is git-ignored, regenerate with `scripts/gen_audit_examples.py`). `--at` Day numbers are 0-based, as the grader prints them. Every heading is built from the output below it.

## 1. public_01 - approved change: v3 (controller_approved)

```
$ python -m controller why --db <db> --workflow ks_cancel --at 'Day 0 12:30'
SUMMARY for ks_cancel at Day 0 12:30 (plain English; details below)
  WHAT: v3 by controller_approved, dims ["cohorts", "welcome_message", "voice", "schedule"]: welcome_message: W1 100%->85%, W2 0%->7.5%, W3 0%->7.5% (same in 5 cohort keys); voice: V1 100%->77.5%, V2 0%->7.5%, V3 0%->7.5%, V4 0%->7.5% (same in 5 cohort keys); cohorts +4/-0; schedule: 5 hour tables changed
  WHY: experiment_entry at Day 0 00:00: Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%) | Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%)
  WHO: approval apr_00003 approved, decided_at Day 0 11:59 by the policy approver of ks_cancel; the sim exposes no approver names
  WHEN: took effect Day 0 12:15; we observed the version at Day 0 12:15; approval decided_at Day 0 11:59 is the sim's clock, observed_at Day 0 12:15 is the next 15-min tick that saw it (tick granularity)
  NEXT: rolled back by v4 at Day 0 16:45 (schedule_rollback, touching only schedule: hour-mix-adjusted engagement fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.06515, z 2.128, p=1.7e-02]); monitoring: state CLOSED, verdict: rolled back

why ks_cancel at Day 0 12:30
  active version v3 by controller_approved (observed Day 0 12:15, dims ["cohorts", "welcome_message", "voice", "schedule"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.489, "D2": 0.489, "D3": 0.021}, "voice": {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}, "welcome_message": {"W1": 0.85, "W2": 0.075, "W3": 0.075}}}
  proposal ks_cancel:2:experiment_entry [approval] state=CLOSED base=v2 applied=v3 at Day 0 12:15
    summary: Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%); Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%); Define 4 cohorts and per-cohort call-time tables: est. pickup 0.248 -> 0.250 (+0.5%), est. engage
    rationale: Controlled exploration of voice: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 2.2% of expected engagement; harm-stop every 4 h at z <= -3.0. | Controlled exploration of welcome_message: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 1.5% of expected engagement; harm-stop every 4 h at z <= -3.0. | Define 4 cohorts and per-cohort call-time tables: est. pickup 0.248 -> 0.250 (+0.5%), est. engagement shift -0.000 (guardrail 0.02); weights only re-rank leads within an hour
    impact=0.225 affected_leads=4662 max_shift 0.225
    approval apr_00003: APPROVED (decided_at Day 0 11:59, observed_at Day 0 12:15) by policy approver of ks_cancel; the sim exposes no approver names
    policy: approver hours [10, 19], latency [20, 120] min, ttl 12 h, max 4 requests/24h; objective {'guardrail': {'pickup_rate_max_drop': 0.02}, 'primary': 'engagement_rate'}
  proposal lifecycle:
    Day 0 11:00 DRAFT
    Day 0 11:00 INTENT
    Day 0 11:00 PENDING approval created
    Day 0 11:59 APPROVED decided_at Day 0 11:59, observed_at Day 0 12:15 approved
    Day 0 12:15 apply_intent
    Day 0 12:15 APPLIED applied
    Day 0 12:30 MONITORING monitoring after apply
    Day 1 12:15 CLOSED monitoring window over (MONITORING -> CLOSED); rolled back by v4 at Day 0 16:45: schedule_rollback, hour-mix-adjusted engagement fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.06515, z 2.128, p=1.7e-02]
  our decisions for this change:
    Day 0 11:00 proposal_pending: ks_cancel:2:experiment_entry: PENDING
    Day 0 12:15 proposal_approved: ks_cancel:2:experiment_entry: APPROVED
    Day 0 12:15 proposal_apply_intent: ks_cancel:2:experiment_entry: apply_intent
    Day 0 12:15 proposal_applied: ks_cancel:2:experiment_entry: APPLIED
    Day 0 12:15 config_observed: v3 by controller_approved changed ['cohorts', 'welcome_message', 'voice', 'schedule']
  evidence (newest detector/test rows in the 24h before the proposal):
    Day 0 00:00 validation_run: ks_cancel P4: pass_rate 0.955 (n=200, val_00006)
    Day 0 00:00 did_curve_fit: fatigue curve refit: tau=120h fitted=True points=28
    Day 0 00:00 did_tested: D2 passes: not worse than peers at its load
    Day 0 11:00 halluc_eval: P1: flags 2.04% vs 1.37% (n=1077, p=3.6e-02, ceiling 3.0%)
    Day 0 11:00 did_eval: caller-ID evaluation: D1=tested, D2=tested, D3=untested
  post-apply monitoring:
    Day 0 12:15 config_observed: v3 by controller_approved changed ['cohorts', 'welcome_message', 'voice', 'schedule']
    Day 0 12:30 schedule_applied: per-cohort call times live since Day 0 12:15 (config v3); verifying hour-mix-adjusted pickup
    Day 0 12:30 experiment_applied: experiment_entry for welcome_message is live (Day 0 12:15): W1 85.0%, W2 7.5%, W3 7.5%
    Day 0 12:30 experiment_applied: experiment_entry for voice is live (Day 0 12:15): V1 77.5%, V2 7.5%, V3 7.5%, V4 7.5%
    Day 0 16:00 halluc_eval: P1: flags 1.81% vs 1.37% (n=1050, p=1.5e-01, ceiling 3.0%)
    Day 0 16:45 did_eval: caller-ID evaluation: D1=tested, D2=tested, D3=untested
    Day 0 16:45 schedule_rollback: hour-mix-adjusted engagement fell beyond the guardrail after the call-time change: rolling back (confounded before/after check)
    Day 0 16:45 config_observed: v4 by controller_auto changed ['schedule']
  next change to these dims: v4 (controller_auto) at Day 0 16:45, dims ["schedule"], proposal ks_cancel:3:schedule_rollback: Roll back per-cohort call times: hour-mix-adjusted pickup/engagement fell beyond the guardrail
```

## 2. public_01 - auto-applied change: v7 (controller_auto)

```
$ python -m controller why --db <db> --workflow nw_renewal --at 'Day 5 01:00'
SUMMARY for nw_renewal at Day 5 01:00 (plain English; details below)
  WHAT: v7 by controller_auto, dims ["caller_id"]: caller_id: D1 32.6%->33.3%, D2 32.6%->33.3%, D3 32.6%->33.3%, D4 2.2%->0% (same in 5 cohort keys)
  WHY: did_weak_base at Day 5 00:00: WEAK BASE D4: pickup 0.40x expected at comparable load (z=-4.0, n=153) on 2 days
  WHO: auto-applied under policy ['schedule', 'caller_id'], max_shift 0.5; no approval needed
  WHEN: took effect Day 5 00:00; we observed the version at Day 5 00:00 (we poll the sim every 15 sim-minutes, so observed time can lag the real change by up to one tick)
  NEXT: no later change to these dims in the recorded run; monitoring: state CLOSED, verdict: no rollback or harm-stop

why nw_renewal at Day 5 01:00
  active version v7 by controller_auto (observed Day 5 00:00, dims ["caller_id"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.333, "D2": 0.333, "D3": 0.333}, "voice": {"V1": 0.6, "V2": 0.2, "V3": 0.2}, "welcome_message": {"W1": 0.6, "W2": 0.2, "W3": 0.2}}}
  proposal nw_renewal:6:did_rebalance [auto] state=CLOSED base=v6 applied=v7 at Day 5 00:00
    summary: Move traffic off weak-base caller ID (h-bar 0.89): default: D1 32.6%->33.3%, D2 32.6%->33.3%, D3 32.6%->33.3%, D4 2.2%->0.0%
    rationale: caller-ID load spreading; statuses: D1=tested, D2=tested, D3=tested, D4=weak; default: D1 32.6%->33.3%, D2 32.6%->33.3%, D3 32.6%->33.3%, D4 2.2%->0.0%
    impact=0.0 affected_leads=1564 max_shift 0.022
    auto-applied under the workflow's auto_apply policy (no approval)
  proposal lifecycle:
    Day 5 00:00 DRAFT
    Day 5 00:00 INTENT
    Day 5 00:00 AUTO_APPLIED auto-applied
    Day 5 01:00 MONITORING monitoring after apply
    Day 6 00:00 CLOSED monitoring window over
  our decisions for this change:
    Day 5 00:00 proposal_auto_applied: nw_renewal:6:did_rebalance: AUTO_APPLIED
    Day 5 00:00 config_observed: v7 by controller_auto changed ['caller_id']
  evidence (newest detector/test rows in the 24h before the proposal):
    Day 4 20:45 did_eval: caller-ID evaluation: D1=tested, D2=tested, D3=tested, D4=suspect
    Day 5 00:00 halluc_eval: P1: flags 2.39% vs 1.33% (n=1006, p=2.9e-03, ceiling 3.0%)
    Day 5 00:00 did_curve_fit: fatigue curve refit: tau=192h fitted=True points=46
    Day 5 00:00 did_weak_base: WEAK BASE D4: pickup 0.40x expected at comparable load (z=-4.0, n=153) on 2 days
  post-apply monitoring:
    Day 5 00:00 did_weak_candidate: D4 looks weak (pickup 0.40x expected, z=-4.0, n=153); day 2 of 2 needed before acting
    Day 5 00:00 did_weak_base: WEAK BASE D4: pickup 0.40x expected at comparable load (z=-4.0, n=153) on 2 days
    Day 5 00:00 schedule_plan: per-cohort call times: est. pickup 0.267 -> 0.267 (-0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
    Day 5 00:00 config_observed: v7 by controller_auto changed ['caller_id']
    Day 5 21:00 experiment_exhausted: voice: all 4 planned looks used without a winner; no promotion will be made. A surviving challenger stays only while its point estimate is >= control; otherwise it reverts (when that is not expected-harmful)
    Day 5 21:00 experiment_revert: voice: after the last look ['V2'] have a point estimate below control and reverting them is not expected-harmful on the observed arm rates: back to control in the next bundle
    Day 6 00:00 schedule_plan: per-cohort call times: est. pickup 0.267 -> 0.267 (+0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
    Day 6 13:00 halluc_eval: P1: flags 1.11% vs 1.33% (n=898, p=7.5e-01, ceiling 3.0%)
    (+2 more rows; --verbose or --json)
    (6 experiment look rows hidden; --verbose or --json)
  next change to these dims: v8 (controller_approved) at Day 6 13:00, dims ["voice"], proposal nw_renewal:7:experiment_update: voice experiment: drop V2 (lift -0.4%, z -0.11, recent z -1.37); new split V1 80.0%, V3 20.0%
```

## 3. public_01 - same approved change with --verbose (first 28 lines): v3 (controller_approved)

```
$ python -m controller why --db <db> --workflow ks_cancel --at 'Day 0 12:30' --verbose
SUMMARY for ks_cancel at Day 0 12:30 (plain English; details below)
  WHAT: v3 by controller_approved, dims ["cohorts", "welcome_message", "voice", "schedule"]: welcome_message: W1 100%->85%, W2 0%->7.5%, W3 0%->7.5% (same in 5 cohort keys); voice: V1 100%->77.5%, V2 0%->7.5%, V3 0%->7.5%, V4 0%->7.5% (same in 5 cohort keys); cohorts +4/-0; schedule: 5 hour tables changed
  WHY: experiment_entry at Day 0 00:00: Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%) | Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%)
  WHO: approval apr_00003 approved, decided_at Day 0 11:59 by the policy approver of ks_cancel; the sim exposes no approver names
  WHEN: took effect Day 0 12:15; we observed the version at Day 0 12:15; approval decided_at Day 0 11:59 is the sim's clock, observed_at Day 0 12:15 is the next 15-min tick that saw it (tick granularity)
  NEXT: rolled back by v4 at Day 0 16:45 (schedule_rollback, touching only schedule: hour-mix-adjusted engagement fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.06515, z 2.128, p=1.7e-02]); monitoring: state CLOSED, verdict: rolled back

why ks_cancel at Day 0 12:30
  active version v3 by controller_approved (observed Day 0 12:15, dims ["cohorts", "welcome_message", "voice", "schedule"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.48929382311941066, "D2": 0.48929382311941066, "D3": 0.02141235376117863}, "voice": {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}, "welcome_message": {"W1": 0.85, "W2": 0.075, "W3": 0.075}}}
  proposal ks_cancel:2:experiment_entry [approval] state=CLOSED base=v2 applied=v3 at Day 0 12:15
    summary: Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%); Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%); Define 4 cohorts and per-cohort call-time tables: est. pickup 0.248 -> 0.250 (+0.5%), est. engage
    rationale: Controlled exploration of voice: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 2.2% of expected engagement; harm-stop every 4 h at z <= -3.0. | Controlled exploration of welcome_message: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 1.5% of expected engagement; harm-stop every 4 h at z <= -3.0. | Define 4 cohorts and per-cohort call-time tables: est. pickup 0.248 -> 0.250 (+0.5%), est. engagement shift -0.000 (guardrail 0.02); weights only re-rank leads within an hour
    impact=0.225 affected_leads=4662 max_shift 0.225 tv={'age_band=18-30/voice': 0.225, 'age_band=18-30/welcome_message': 0.15, 'age_band=31-45/voice': 0.225, 'age_band=31-45/welcome_message': 0.15, 'age_band=46-60/voice': 0.225, 'age_band=46-60/welcome_message': 0.15, 'age_band=60+/voice': 0.225, 'age_band=60+/welcome_message': 0.15, 'default/voice': 0.225, 'default/welcome_message': 0.15}
    dry_run: {"allocations": [{"arms": {"W1": {"n": 632, "rate": 0.4164, "status": "observed"}, "W2": {"status": "no_evidence"}, "W3": {"status": "no_evidence"}}, "ci95_new": [0.378, 0.4548], "cohort": "age_band=18-30", "dim": "welcome_message", "evidence": "partial", "exp...(truncated; see --json)
    approval apr_00003: APPROVED (decided_at Day 0 11:59, observed_at Day 0 12:15) by policy approver of ks_cancel; the sim exposes no approver names
    policy: approver hours [10, 19], latency [20, 120] min, ttl 12 h, max 4 requests/24h; objective {'guardrail': {'pickup_rate_max_drop': 0.02}, 'primary': 'engagement_rate'}
  proposal lifecycle:
    Day 0 11:00 DRAFT
    Day 0 11:00 INTENT
    Day 0 11:00 PENDING approval created
    Day 0 11:59 APPROVED decided_at Day 0 11:59, observed_at Day 0 12:15 approved
    Day 0 12:15 apply_intent
    Day 0 12:15 APPLIED applied
    Day 0 12:30 MONITORING monitoring after apply
    Day 1 12:15 CLOSED monitoring window over (MONITORING -> CLOSED); rolled back by v4 at Day 0 16:45: schedule_rollback, hour-mix-adjusted engagement fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.06515, z 2.128, p=1.7e-02]
  our decisions for this change:
    Day 0 11:00 proposal_pending: ks_cancel:2:experiment_entry: PENDING
...
```

## 4. public_02 - approved change: the prompt revert (halluc_fix): v9 (controller_approved)

```
$ python -m controller why --db <db> --workflow ks_cancel --at 'Day 3 14:00'
SUMMARY for ks_cancel at Day 3 14:00 (plain English; details below)
  WHAT: v9 by controller_approved, dims ["welcome_message", "voice", "prompt_version"]: prompt P2->P1; welcome_message: W1 85%->72.5%, W2 7.5%->20% (same in 5 cohort keys); voice: V1 77.5%->92.5%, V3 7.5%->0%, V4 7.5%->0% (same in 5 cohort keys)
  WHY: incident_opened at Day 3 11:45: halluc incident opened: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987; voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%; welcome_message experiment: ra
  WHO: approval apr_00008 approved, decided_at Day 3 13:26 by the policy approver of ks_cancel; the sim exposes no approver names
  WHEN: took effect Day 3 13:45; we observed the version at Day 3 13:45; approval decided_at Day 3 13:26 is the sim's clock, observed_at Day 3 13:45 is the next 15-min tick that saw it (tick granularity)
  NEXT: later changes to these dims: v11 Day 6 11:45 (controller_approved experiment_conclude); monitoring: state CLOSED, verdict: no rollback or harm-stop

why ks_cancel at Day 3 14:00
  active version v9 by controller_approved (observed Day 3 13:45, dims ["welcome_message", "voice", "prompt_version"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.5, "D3": 0.5}, "voice": {"V1": 0.925, "V2": 0.075}, "welcome_message": {"W1": 0.725, "W2": 0.2, "W3": 0.075}}}
  proposal ks_cancel:9:halluc_fix [approval] state=CLOSED base=v8 applied=v9 at Day 3 13:45
    summary: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987; voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%; welcome_message experiment: ramp W2 (lift +26.5%, z 3.41, n 150); 
    rationale: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987. Fix source: known_good; engagement is verified after apply. | voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%. Ramp: after >= 1 day and >= 100 connects, point estimate >= control; drop at z <= -1. | welcome_message experiment: ramp W2 (lift +26.5%, z 3.41, n 150); new split W1 72.5%, W2 20.0%, W3 7.5%. Ramp: after >= 1 day and >= 100 connects, point estimate >= control; drop at z <= -1.
    impact=1.0 affected_leads=3671 max_shift 0.150
    approval apr_00008: APPROVED (decided_at Day 3 13:26, observed_at Day 3 13:45) by policy approver of ks_cancel; the sim exposes no approver names
    policy: approver hours [10, 19], latency [20, 120] min, ttl 12 h, max 4 requests/24h; objective {'guardrail': {'pickup_rate_max_drop': 0.02}, 'primary': 'engagement_rate'}
  proposal lifecycle:
    Day 3 11:45 DRAFT
    Day 3 11:45 INTENT
    Day 3 11:45 PENDING approval created
    Day 3 13:26 APPROVED decided_at Day 3 13:26, observed_at Day 3 13:45 approved
    Day 3 13:45 apply_intent
    Day 3 13:45 APPLIED applied
    Day 3 14:00 MONITORING monitoring after apply
    Day 4 13:45 CLOSED monitoring window over
  our decisions for this change:
    Day 3 11:45 proposal_pending: ks_cancel:9:halluc_fix: PENDING
    Day 3 13:45 proposal_approved: ks_cancel:9:halluc_fix: APPROVED
    Day 3 13:45 proposal_apply_intent: ks_cancel:9:halluc_fix: apply_intent
    Day 3 13:45 proposal_applied: ks_cancel:9:halluc_fix: APPLIED
    Day 3 13:45 config_observed: v9 by controller_approved changed ['welcome_message', 'voice', 'prompt_version']
  evidence (newest detector/test rows in the 24h before the proposal):
    Day 3 00:00 did_curve_fit: fatigue curve refit: tau=192h fitted=True points=37
    Day 3 11:00 halluc_eval: P1: flags 1.46% vs 1.37% (n=1163, p=4.3e-01, ceiling 3.0%)
    Day 3 11:45 incident_opened: halluc incident opened
    Day 3 11:45 did_eval: caller-ID evaluation: D1=tested, D2=flagged, D3=tested
  post-apply monitoring:
    Day 3 13:45 config_observed: v9 by controller_approved changed ['welcome_message', 'voice', 'prompt_version']
    Day 3 14:00 experiment_applied: experiment_update for welcome_message is live (Day 3 13:45): W1 72.5%, W2 20.0%, W3 7.5%
    Day 3 14:00 experiment_applied: experiment_update for voice is live (Day 3 13:45): V1 92.5%, V2 7.5%
    Day 3 17:30 incident_verified: P1: flags 1.23%, engagement ok
    Day 3 17:30 incident_closed: halluc incident closed: fixed by P1; flag rate non-inferior to baseline, engagement inside guardrail
    Day 4 00:00 schedule_plan: per-cohort call times: est. pickup 0.249 -> 0.249 (-0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
    Day 4 11:00 halluc_eval: P1: flags 1.46% vs 1.37% (n=957, p=4.4e-01, ceiling 3.0%)
    Day 4 11:45 did_flag_expired: flag on D2 expired after the cooldown; re-probing it at the re-probe cap
    (+2 more rows; --verbose or --json)
    (6 experiment look rows hidden; --verbose or --json)
  next change to these dims: v11 (controller_approved) at Day 6 11:45, dims ["welcome_message", "voice"], proposal ks_cancel:11:experiment_conclude: End voice experiment: no challenger left; all traffic back to control V1; welcome_message experiment: drop W3 (lift -3.3%, z -0.69, recent z -0.51); new split W1 80.0%, W2 20.0%
```

## 5. public_02 - auto-applied change: caller-ID evacuation of a flagged DID, with its impact: v7 (controller_auto)

```
$ python -m controller why --db <db> --workflow ks_cancel --at 'Day 2 12:30'
SUMMARY for ks_cancel at Day 2 12:30 (plain English; details below)
  WHAT: v7 by controller_auto, dims ["caller_id"]: caller_id: D2 14.3%->0%, D3 35.7%->50% (same in 5 cohort keys)
  WHY: did_flagged at Day 2 11:45: FLAGGED D2: pickup ratio vs peers fell 69% (p=3.6e-07, 244 dials in 2h vs 1916 in the reference); incident mode
  WHO: auto-applied under policy ['schedule', 'caller_id', 'welcome_message', 'voice'], max_shift 0.2; no approval needed
  WHEN: took effect Day 2 12:00; we observed the version at Day 2 12:00 (we poll the sim every 15 sim-minutes, so observed time can lag the real change by up to one tick)
  NEXT: later changes to these dims: v10 Day 4 11:45 (controller_auto did_probe); v12 Day 9 00:00 (controller_auto did_rebalance); monitoring: state CLOSED, verdict: no rollback or harm-stop

why ks_cancel at Day 2 12:30
  active version v7 by controller_auto (observed Day 2 12:00, dims ["caller_id"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.5, "D3": 0.5}, "voice": {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}, "welcome_message": {"W1": 0.85, "W2": 0.075, "W3": 0.075}}}
  proposal ks_cancel:7:did_evacuation [auto] state=CLOSED base=v6 applied=v7 at Day 2 12:00
    summary: Evacuate flagged caller ID D2 (pickup ratio vs peers x0.31): default: D1 50.0%->50.0%, D2 14.3%->0.0%, D3 35.7%->50.0%
    rationale: caller-ID load spreading; statuses: D1=tested, D2=flagged, D3=tested; default: D1 50.0%->50.0%, D2 14.3%->0.0%, D3 35.7%->50.0%
    impact=0.9936 affected_leads=4578 max_shift 0.143
    auto-applied under the workflow's auto_apply policy (no approval)
  proposal lifecycle:
    Day 2 12:00 DRAFT
    Day 2 12:00 INTENT
    Day 2 12:00 AUTO_APPLIED auto-applied
    Day 2 12:15 MONITORING monitoring after apply
    Day 3 12:00 CLOSED monitoring window over
  our decisions for this change:
    Day 2 12:00 proposal_auto_applied: ks_cancel:7:did_evacuation: AUTO_APPLIED
    Day 2 12:00 config_observed: v7 by controller_auto changed ['caller_id']
  evidence (newest detector/test rows in the 24h before the proposal):
    Day 1 14:00 did_tested: D3 passes: not worse than peers at its load
    Day 2 00:00 did_curve_fit: fatigue curve refit: tau=192h fitted=True points=34
    Day 2 11:45 incident_opened: did_flagged incident opened
    Day 2 11:45 did_flagged: FLAGGED D2: pickup ratio vs peers fell 69% (p=3.6e-07, 244 dials in 2h vs 1916 in the reference); incident mode
    Day 2 12:00 halluc_eval: P1: flags 1.34% vs 1.37% (n=1123, p=5.7e-01, ceiling 3.0%)
    Day 2 12:00 did_eval: caller-ID evaluation: D1=tested, D2=flagged, D3=tested
  post-apply monitoring:
    Day 2 12:00 config_observed: v7 by controller_auto changed ['caller_id']
    Day 2 23:00 incident_closed: did_flagged incident closed: D2 evacuated: observed share 0.00% over the trailing window, config v7 share 0 in every key
    Day 3 00:00 schedule_plan: per-cohort call times: est. pickup 0.249 -> 0.249 (-0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
    Day 3 11:00 halluc_eval: P1: flags 1.46% vs 1.37% (n=1163, p=4.3e-01, ceiling 3.0%)
    Day 3 11:30 did_eval: caller-ID evaluation: D1=tested, D2=flagged, D3=tested
    Day 3 11:30 config_observed: v8 by supervisor changed ['prompt_version']
    (6 experiment look rows hidden; --verbose or --json)
  next change to these dims: v10 (controller_auto) at Day 4 11:45, dims ["caller_id"], proposal ks_cancel:10:did_probe: Probe/spread caller IDs within the safe probe cap (h-bar 0.80): default: D1 50.0%->49.6%, D2 0.0%->0.7%, D3 50.0%->49.6%
```

## 6. public_02 - change later rolled back: v3 (controller_approved)

```
$ python -m controller why --db <db> --workflow nw_renewal --at 'Day 0 14:00'
SUMMARY for nw_renewal at Day 0 14:00 (plain English; details below)
  WHAT: v3 by controller_approved, dims ["cohorts", "welcome_message", "voice", "schedule"]: welcome_message: W1 100%->85%, W2 0%->7.5%, W3 0%->7.5% (same in 5 cohort keys); voice: V1 100%->77.5%, V2 0%->7.5%, V3 0%->7.5%, V4 0%->7.5% (same in 5 cohort keys); cohorts +4/-0; schedule: 5 hour tables changed
  WHY: experiment_entry at Day 0 00:00: Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%) | Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%)
  WHO: approval apr_00002 approved, decided_at Day 0 12:40 by the policy approver of nw_renewal; the sim exposes no approver names
  WHEN: took effect Day 0 13:00; we observed the version at Day 0 13:00; approval decided_at Day 0 12:40 is the sim's clock, observed_at Day 0 13:00 is the next 15-min tick that saw it (tick granularity)
  NEXT: rolled back by v4 at Day 0 19:15 (schedule_rollback, touching only schedule: hour-mix-adjusted pickup fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.02546, z 1.975, p=2.4e-02]); monitoring: state CLOSED, verdict: rolled back

why nw_renewal at Day 0 14:00
  active version v3 by controller_approved (observed Day 0 13:00, dims ["cohorts", "welcome_message", "voice", "schedule"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.489, "D2": 0.489, "D3": 0.022}, "voice": {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}, "welcome_message": {"W1": 0.85, "W2": 0.075, "W3": 0.075}}}
  proposal nw_renewal:2:experiment_entry [approval] state=CLOSED base=v2 applied=v3 at Day 0 13:00
    summary: Start voice experiment: control V1 keeps 77.5%, challengers V2 7.5%, V3 7.5%, V4 7.5% (total 22.5%); Start welcome_message experiment: control W1 keeps 85.0%, challengers W2 7.5%, W3 7.5% (total 15.0%); Define 4 cohorts and per-cohort call-time tables: est. pickup 0.273 -> 0.274 (+0.3%), est. engage
    rationale: Controlled exploration of voice: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 2.2% of expected engagement; harm-stop every 4 h at z <= -3.0. | Controlled exploration of welcome_message: every challenger enters at 7.5% (total <= 22.5%), so even a challenger that is 10% worse costs under 1.5% of expected engagement; harm-stop every 4 h at z <= -3.0. | Define 4 cohorts and per-cohort call-time tables: est. pickup 0.273 -> 0.274 (+0.3%), est. engagement shift +0.000 (guardrail 0.02); weights only re-rank leads within an hour
    impact=0.225 affected_leads=3293 max_shift 0.225
    approval apr_00002: APPROVED (decided_at Day 0 12:40, observed_at Day 0 13:00) by policy approver of nw_renewal; the sim exposes no approver names
    policy: approver hours [10, 19], latency [30, 240] min, ttl 24 h, max 3 requests/24h; objective {'guardrail': {'pickup_rate_max_drop': 0.02}, 'primary': 'engagement_rate'}
  proposal lifecycle:
    Day 0 10:00 DRAFT
    Day 0 10:00 INTENT
    Day 0 10:00 PENDING approval created
    Day 0 12:40 APPROVED decided_at Day 0 12:40, observed_at Day 0 13:00 approved
    Day 0 13:00 apply_intent
    Day 0 13:00 APPLIED applied
    Day 0 13:15 MONITORING monitoring after apply
    Day 1 13:00 CLOSED monitoring window over (MONITORING -> CLOSED); rolled back by v4 at Day 0 19:15: schedule_rollback, hour-mix-adjusted pickup fell beyond the guardrail after the call-time change: rolling back (confounded before/after check) [drop 0.02546, z 1.975, p=2.4e-02]
  our decisions for this change:
    Day 0 10:00 proposal_pending: nw_renewal:2:experiment_entry: PENDING
    Day 0 13:00 proposal_approved: nw_renewal:2:experiment_entry: APPROVED
    Day 0 13:00 proposal_apply_intent: nw_renewal:2:experiment_entry: apply_intent
    Day 0 13:00 proposal_applied: nw_renewal:2:experiment_entry: APPLIED
    Day 0 13:00 config_observed: v3 by controller_approved changed ['cohorts', 'welcome_message', 'voice', 'schedule']
  evidence (newest detector/test rows in the 24h before the proposal):
    Day 0 00:00 validation_run: nw_renewal P5: pass_rate 0.99 (n=300, val_00011)
    Day 0 00:00 did_curve_fit: fatigue curve refit: tau=72h fitted=True points=28
    Day 0 00:00 did_tested: D2 passes: not worse than peers at its load
    Day 0 10:00 halluc_eval: P1: flags 1.42% vs 1.30% (n=844, p=4.2e-01, ceiling 3.0%)
    Day 0 10:00 did_eval: caller-ID evaluation: D1=tested, D2=tested, D3=untested, D4=untested
  post-apply monitoring:
    Day 0 13:00 config_observed: v3 by controller_approved changed ['cohorts', 'welcome_message', 'voice', 'schedule']
    Day 0 13:15 schedule_applied: per-cohort call times live since Day 0 13:00 (config v3); verifying hour-mix-adjusted pickup
    Day 0 13:15 experiment_applied: experiment_entry for welcome_message is live (Day 0 13:00): W1 85.0%, W2 7.5%, W3 7.5%
    Day 0 13:15 experiment_applied: experiment_entry for voice is live (Day 0 13:00): V1 77.5%, V2 7.5%, V3 7.5%, V4 7.5%
    Day 0 19:00 halluc_eval: P1: flags 1.50% vs 1.30% (n=869, p=3.5e-01, ceiling 3.0%)
    Day 0 19:15 did_eval: caller-ID evaluation: D1=tested, D2=tested, D3=untested, D4=untested
    Day 0 19:15 schedule_rollback: hour-mix-adjusted pickup fell beyond the guardrail after the call-time change: rolling back (confounded before/after check)
    Day 0 19:15 config_observed: v4 by controller_auto changed ['schedule']
  next change to these dims: v4 (controller_auto) at Day 0 19:15, dims ["schedule"], proposal nw_renewal:3:schedule_rollback: Roll back per-cohort call times: hour-mix-adjusted pickup/engagement fell beyond the guardrail
```

## 7. public_02 - supervisor edit (prompt): v8 (supervisor)

```
$ python -m controller why --db <db> --workflow ks_cancel --at 'Day 3 12:00'
SUMMARY for ks_cancel at Day 3 12:00 (plain English; details below)
  WHAT: v8 by supervisor, dims ["prompt_version"]: prompt P1->P2
  WHY: a supervisor changed the config directly; the sim records no reason
  WHO: supervisor edit (bypasses governance; we never overwrite it silently)
  WHEN: took effect Day 3 11:30; we observed the version at Day 3 11:30 (we poll the sim every 15 sim-minutes, so observed time can lag the real change by up to one tick)
  NEXT: rolled back by v9 at Day 3 13:45 (halluc_fix, touching only prompt_version, voice, welcome_message: halluc incident opened: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987; voice experiment: drop V3 (lift -11.3)

why ks_cancel at Day 3 12:00
  active version v8 by supervisor (observed Day 3 11:30, dims ["prompt_version"])
    prompt P2, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.5, "D3": 0.5}, "voice": {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}, "welcome_message": {"W1": 0.85, "W2": 0.075, "W3": 0.075}}}
  made by the client's supervisor (bypasses governance; never silently overwritten)
  our decisions for this change:
    Day 3 11:30 config_observed: v8 by supervisor changed ['prompt_version']
  post-apply monitoring:
    (none)
  next change to these dims: v9 (controller_approved) at Day 3 13:45, dims ["welcome_message", "voice", "prompt_version"], proposal ks_cancel:9:halluc_fix: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987; voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%; welcome_message experiment: ramp W2 (lift +26.5%, z 3.41, n 150); 
```

## 8. public_02 - supervisor edit (voice): v11 (supervisor)

```
$ python -m controller why --db <db> --workflow nw_renewal --at 'Day 5 15:30'
SUMMARY for nw_renewal at Day 5 15:30 (plain English; details below)
  WHAT: v11 by supervisor, dims ["voice"]: voice: V1 50%->0%, V2 50%->100% (same in 5 cohort keys)
  WHY: a supervisor changed the config directly; the sim records no reason
  WHO: supervisor edit (bypasses governance; we never overwrite it silently)
  WHEN: took effect Day 5 15:00; we observed the version at Day 5 15:00 (we poll the sim every 15 sim-minutes, so observed time can lag the real change by up to one tick)
  NEXT: no later change to these dims in the recorded run

why nw_renewal at Day 5 15:30
  active version v11 by supervisor (observed Day 5 15:00, dims ["voice"])
    prompt P1, cohorts 4, allocations {"default": {"caller_id": {"D1": 0.25, "D2": 0.25, "D3": 0.25, "D4": 0.25}, "voice": {"V2": 1.0}, "welcome_message": {"W1": 0.6, "W2": 0.2, "W3": 0.2}}}
  made by the client's supervisor (bypasses governance; never silently overwritten)
  our decisions for this change:
    Day 5 15:00 config_observed: v11 by supervisor changed ['voice']
  post-apply monitoring:
    (none)
  next change to these dims: v12 (controller_approved) at Day 6 11:00, dims ["welcome_message"], proposal nw_renewal:10:experiment_update: welcome_message experiment: drop W3 (lift +0.2%, z 0.05, recent z -1.52); new split W1 80.0%, W2 20.0%
```

## 9. public_02 - `why --lead`: one customer's calls and the decisions behind them

```
$ python -m controller why --db <db> --lead ks_cancel:l00024079 --day 3
lead ks_cancel:l00024079 person ks_cancel:p0009326 workflow ks_cancel campaign ks_cancel:c0017 (created Day 2 08:00, cell 131330), day 3
  Day 3 13:56 attempt 1 welcome W1 voice V1 DID D1 prompt P1 cohort age_band=18-30 connected=False engaged=False
    allocation from config v9; prompt from config v9 active at the call time
  Day 3 19:50 attempt 2 welcome W1 voice V1 DID D3 prompt P1 cohort age_band=18-30 connected=False engaged=False
    allocation from config v9; prompt from config v9 active at the call time
  decisions behind config v9:
    by controller_approved at Day 3 13:45 dims ["welcome_message", "voice", "prompt_version"]
    proposal ks_cancel:9:halluc_fix: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987; voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%; welcome_message experiment: ramp W2 (lift +26.5%, z 3.41, n 150); 
    rationale: Revert P2 (set by supervisor Day 3 11:30) to P1: flags 5.5% vs failed Day-0 validation (n=200, p<1e-300); validation val_00001 pass 0.987. Fix source: known_good; engagement is verified after apply. | voice experiment: drop V3 (lift -11.3%, z -1.70, recent z -1.20); new split V1 92.5%, V2 7.5%. Ramp: after >= 1 day and >= 100 connects, point estimate >= control; drop at z <= -1. | welcome_message experiment: ramp W2 (lift +26.5%, z 3.41, n 150); new split W1 72.5%, W2 20.0%, W3 7.5%. Ramp: after >= 1 day and >= 100 connects, point estimate >= control; drop at z <= -1.
    approval apr_00008 APPROVED decided_at Day 3 13:26 observed_at Day 3 13:45 by policy approver of ks_cancel
    Day 3 11:45 proposal_pending: ks_cancel:9:halluc_fix: PENDING
    Day 3 13:45 proposal_approved: ks_cancel:9:halluc_fix: APPROVED
    Day 3 13:45 proposal_apply_intent: ks_cancel:9:halluc_fix: apply_intent
    Day 3 13:45 proposal_applied: ks_cancel:9:halluc_fix: APPLIED
    Day 3 13:45 config_observed: v9 by controller_approved changed ['welcome_message', 'voice', 'prompt_version']
```

## 10. public_02 - timeline `--from 'Day 2' --to 'Day 3'` (ks_cancel)

```
$ python -m controller why --db <db> --workflow ks_cancel --from 'Day 2' --to 'Day 3'
SUMMARY for ks_cancel from Day 2 00:00 to Day 4 00:00 (plain English; details below)
  WHAT: 4 config versions observed ({'controller_auto': 2, 'supervisor': 1, 'controller_approved': 1}); 3 proposals applied
  WHY: most frequent events: experiment_look x12, config_observed x4, proposal_pending x3, schedule_plan x2, incident_opened x2
  WHO: 1 approved, 0 rejected, 2 auto-applied, 1 supervisor edits
  WHEN: event times are sim time; a version's time is the 15-min tick that observed it
  NEXT: rollbacks / reverts in range: none

timeline ks_cancel from Day 2 00:00 to Day 4 00:00 (exclusive)
  Day 2 00:00  schedule_plan: per-cohort call times: est. pickup 0.250 -> 0.250 (-0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
  Day 2 11:00  proposal_pending: ks_cancel:5:experiment_update: PENDING
  Day 2 11:45  incident_opened: did_flagged incident opened
  Day 2 11:45  did_flagged: FLAGGED D2: pickup ratio vs peers fell 69% (p=3.6e-07, 244 dials in 2h vs 1916 in the reference); incident mode
  Day 2 11:45  proposal_auto_applied: ks_cancel:6:did_evacuation: AUTO_APPLIED
  Day 2 11:45  config_observed: v6 by controller_auto changed ['caller_id']
  Day 2 11:45  alert:did_flagged: FLAGGED D2: pickup ratio vs peers fell 69% (p=3.6e-07, 244 dials in 2h vs 1916 in the reference); incident mode
  Day 2 12:00  proposal_stale: ks_cancel:5:experiment_update: STALE
  Day 2 12:00  proposal_auto_applied: ks_cancel:7:did_evacuation: AUTO_APPLIED
  Day 2 12:00  config_observed: v7 by controller_auto changed ['caller_id']
  Day 2 23:00  incident_closed: did_flagged incident closed: D2 evacuated: observed share 0.00% over the trailing window, config v7 share 0 in every key
  Day 3 00:00  schedule_plan: per-cohort call times: est. pickup 0.249 -> 0.249 (-0.0%), est. engagement shift +0.0001 (max drop 0.02): not material
  Day 3 11:00  proposal_pending: ks_cancel:8:experiment_update: PENDING
  Day 3 11:30  config_observed: v8 by supervisor changed ['prompt_version']
  Day 3 11:45  proposal_stale: ks_cancel:8:experiment_update: STALE
  Day 3 11:45  incident_opened: halluc incident opened
  Day 3 11:45  proposal_pending: ks_cancel:9:halluc_fix: PENDING
  Day 3 11:45  alert:halluc_incident: bad_prompt_day0: P2 flags 5.5% vs 0.0% (n=200, p<1e-300)
  Day 3 13:45  proposal_approved: ks_cancel:9:halluc_fix: APPROVED
  Day 3 13:45  proposal_applied: ks_cancel:9:halluc_fix: APPLIED
  Day 3 13:45  config_observed: v9 by controller_approved changed ['welcome_message', 'voice', 'prompt_version']
  Day 3 14:00  experiment_applied: experiment_update for welcome_message is live (Day 3 13:45): W1 72.5%, W2 20.0%, W3 7.5%
  Day 3 14:00  experiment_applied: experiment_update for voice is live (Day 3 13:45): V1 92.5%, V2 7.5%
  Day 3 17:30  incident_verified: P1: flags 1.23%, engagement ok
  Day 3 17:30  incident_closed: halluc incident closed: fixed by P1; flag rate non-inferior to baseline, engagement inside guardrail
  (12 experiment look rows hidden; --verbose or --json)
```
