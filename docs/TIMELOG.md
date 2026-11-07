# Time log

Rough and honest. **Hours are estimates written by the AI assistant from the git history, not a stopwatch:
the candidate must correct them before submitting.** Development was AI-assisted throughout (Claude Code wrote
most of the code and tests from the plan in `docs/CONTEXT.md`; the candidate set scope, reviewed output and
made the decisions listed in CONTEXT section 14). Commit timestamps are compressed (the history is a few
hours of wall time) because several agents worked in parallel on reviewed slices.

| Phase | What | Est. hours | Evidence |
|---|---|---|---|
| Pre-0 | Read README/SIMULATOR/memos, verify contract against `sim/`, write the plan and have it reviewed by five fresh-context reviewers | 3.0 | `docs/CONTEXT.md` (sections 3, 14, 15) |
| 0 Setup | Package skeleton, `settings.py` flags, supervising `run.sh` | 0.5 | `3f2bd30` |
| 1 Spine | HTTP client with error taxonomy, SQLite store, ingest (one txn per chunk), cube, cohort self-check, loop; timed public_03 day | 3.0 | `adef241`, `efbee01`, `b4c95f7`, `reports/phase1_ingest_timing.md` |
| 2 Governance + planner | Proposal state machine, local validation, ledger, reconcile, minimal dry run / impact, fault-injection hooks, crash tests in-process | 3.0 | `9b39549` ... `179ac57` |
| 3 DID + incidents | Fatigue curve, step test, probes/evacuation, hallucination detector, Day-0 validation, revert path, baselines; stricter per-look levels after a false-flag review | 3.0 | `2efcf59`, `6af6c76`, `618ca50` |
| 4 Floor checkpoint | Full `why` (3 modes), `scripts/crash_test.py` and its real run on public_02, reports, SUBMISSION draft | 1.5 | this phase's commits, `reports/crash_test.log` |
| Review / debugging | Reviewer passes after each phase and fixes; waiting on sim runs | 2.0 | `fix:` commits |
| 5 Cohorts + schedule | Data-driven partition, per-cohort hour model, guardrail dry run, sibling-prompt safety check, solo-DID test | 2.5 | `284f364` ... `b6255ab` |
| 6 Experiments | Clustered/MH contrasts, OBF + Holm, entry/ramp/drop/harm-stop, Monte Carlo of the rule | 4.0 | `ec52916` ... `c22727c`, `reports/mc_null.md` |
| 7 Decay + view | Two-stage promotion, sequential decay test, CUSUM, apply-time veto; alert rules, `view`/`ui`, runbook; determinism fix | 4.5 | `2da43f4` ... `a27e8f3`, `reports/determinism.md` |
| 8 Scale + crash | Load test on public_03, profiling and three query fixes, credit-call crash point, full crash test (10 kills) | 2.5 | `83bcfca` ... `2f4c704`, `reports/public_03_load.md` |
| 9 Per-person analysis | Analysis-only module, report, loop wiring and timing run (flag left off, +22% tick time) | 1.5 | `25276d0`, wiring commit, `reports/person_analysis.md` |
| 10 Write-up | SUBMISSION, stakeholder replies, runbook, harmful-change investigation | 2.0 | this commit |
| **Total** | | **~33** | |

The figures are estimates from the git history (AI-assisted; commit times are compressed). The candidate must confirm
or correct the total before submitting.
