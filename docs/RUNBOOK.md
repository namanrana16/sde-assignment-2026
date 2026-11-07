# On-call runbook

The controller is one process (`python -m controller run`) wrapped by `run.sh`. All of its memory is one SQLite file per
simulated world. Anything it does is reconstructible from that file plus the simulator.

## 1. Run, stop, restart

| Task | Command |
|---|---|
| Start the simulator (dev) | `python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 --force` (port 8700) |
| Start the controller | `./run.sh http://127.0.0.1:8700` (waits for `/health`, restarts a crashed controller up to 20 times, exits 0 only when the scenario has finished) |
| Stop | Ctrl-C the `run.sh` (or `kill` it, then the `python -m controller run` child). Nothing needs cleanup. |
| Restart | Run `./run.sh` again. The controller reconciles every in-flight proposal against the simulator before it does anything else (INTENT / PENDING / APPROVED are re-read, never blindly re-sent). |
| Fresh start | `rm -rf state/` (or use `STATE_DIR=state/x ./run.sh ...`). Never delete the DB while the simulator is mid-run unless you also restart the simulator: the history before the delete is gone and approvals in flight would be orphaned. |
| Supervisor screen | `python -m controller ui --state-dir state --port 8800` (separate process, read-only, never started by `run.sh`) |

If the **simulator** restarts, it is a new world: the controller detects the regression, archives the old DB as
`<name>.<timestamp>.old` and starts a new one. That is expected, not an incident.

## 2. Where state lives

* `state/<scenario>-<fingerprint8>.db` (+ `-wal`, `-shm`): the only state. WAL mode, one writer (the controller).
  The fingerprint is scenario + hash of the first history line, so a different world gets a different file.
* **The DB file must be on a persistent volume.** It is the only copy of the `decisions` audit trail (why every change was made), detector state and the credit ledger. Re-ingesting from the simulator rebuilds calls and leads, not decisions (the production plan in `SUBMISSION.md` section 14 mirrors `decisions` to Postgres). Back it up (copy while the controller is stopped, or use `sqlite3 .backup`).
* `runs/`: simulator run directories and grade inputs (not controller state).
* `reports/`: grade reports and the crash-test log.

Tables (`controller/schema.sql`; `metrics_ext` is created by `controller/metrics.py`): `meta`, `ingest_cursor`, `campaigns`, `leads`,
`calls`, `cube` (rollups), `config_versions` (every config, actor), `cohort_defs`, `experiments`, `arms`, `proposals` +
`proposal_events` (approval state machine), `decisions` (the audit trail: every detector verdict with inputs), `incidents`,
`validation_runs` + `llm_calls` (credit ledger), `jobs`, `detector_state` (experiment / DID / halluc state machines),
`alerts`, `tick_phases`, `metrics`, `metrics_ext`, `exp_day_cache`.

## 3. Reading the loop without reading logs

```
python -m controller status  --state-dir state            # loop health, latest tick + last 24 sim hours
python -m controller view    --state-dir state --workflow nw_renewal [--at "Day 3 14:00"]
python -m controller view    --state-dir state --weekly   # operational auto-applies for Compliance
python -m controller why     --state-dir state --workflow nw_renewal --at "Day 3 14:00"
python -m controller why     --state-dir state --lead <lead_id> [--day N]
```

`status` shows: events ingested, ingest wall time, lag (wall seconds advance-return to decisions-done, and sim minutes
since the newest event), decisions per sim hour, credits remaining and 24 h burn, approvals in flight with the nearest
`apply_by`/expiry, local 4xx / 409 / 403 / 5xx counts, and how many page/digest alerts fired in the last 24 sim hours.

`view` has the six sections the client supervisor asked for: what's being tested, what's winning, what we need from
you, what changed today (each change has its own `why` command), open incidents and alerts, and KPIs today vs the
trailing three days (pickup and engagement are always separate numbers). `ui` serves the same content as web pages
(`/`, `/wf/<id>`, `/weekly`, `/why?workflow=&at=`, `/status`); it opens the DB `mode=ro`, so it is safe beside a live run.
If the port is busy it exits 1 and says so: pass `--port`.

`why` prints a six-line plain-English header (what, why, who, when, what happened next) before the details; add `--verbose` for raw decision inputs and every experiment look row, `--json` for everything. How to run, configure (env vars), extend and regenerate a state DB: `SUBMISSION.md` section 15.

Add `--db path/to.db` to any of these to point at a specific file (default: the newest `*.db` in `--state-dir`).

## 4. Alerts

Alerts are rows in the `alerts` table (also logged at WARNING) and appear in `view` / `/wf/<id>`. There is no pager
integration: *page* means someone should look now. Each alert fires once when its condition becomes true and again only
after it cleared (event alerts fire once per event); restarts do not re-alert.

### PAGE

| Code | Meaning | What to do |
|---|---|---|
| `ingest_stalled` | No new calls for 3+ sim hours although the same window yesterday had calls, or one tick took >10 min wall. | `status` (lag, events ingested). Is the simulator up (`curl $SIM/sim/state`, compare `last_event_id` with `status --json` cursor)? If the sim is healthy: stop and re-run `./run.sh`. If it is still stalled, look at the controller log for ingest integrity errors. |
| `apply_by_near` | An APPROVED change has 30 sim minutes or less before `apply_by` and is not applied: it lapses if missed. | `view --workflow W` (what we need). Look for `apply_withheld` / `apply_intent` decisions in `why --workflow W --from "Day N" --to "Day N"`. A withheld apply means the evidence went stale on purpose; otherwise check the controller is running at all. |
| `credits_low` | Credits are below 25 x number of workflows (the incident reserve). | `status` for burn. Routine validation spend must already have stopped; if an incident is open, its validation may be unaffordable: decide whether to top up (outside our control) or accept the baseline prompt. |
| `sim_5xx` | 3+ simulator 5xx in the last sim hour. | The controller backs off and re-reads `sim_minute` rather than re-advancing. Check the simulator process and its log. Persisting errors with a healthy sim: restart the controller. |
| `fatigue_rejection` | The approver rejected a request for fatigue: **our request accounting is wrong**. | `why --workflow W` for the proposal and the 24 h ledger (governance). Do not file more requests until understood; this is a bug to fix. |
| `http_403` | The simulator refused an auto-apply: a local policy pre-check is wrong. | `alerts` rows `proposal_invalid_locally` / `auto_apply_refused`, and the proposal in `why`. Bug to fix. |
| `cohort_mismatch` | The cohort the sim assigned to a call differs from ours: per-cohort analysis is unreliable. | `view --workflow W`. Stop cohort-based changes until the cohort definitions are re-checked; the `cohort_check_skipped` alert is a weaker "could not check" signal. |

### DIGEST (read in the morning)

`judgement_rejection` (an approver said no, nothing changed; the planner re-plans), `starved_experiment` (an arm has
under 100 connects after 2 days: it cannot conclude), `weak_base_did` (a number is simply weak, not a casualty of a
probe), `kpi_drift` (pickup or engagement for the finished day is beyond |z| 3 of the hour-adjusted trailing 3-day
level with no config change or incident that day: find out why before the next change).

Also written, with severity `warn` (counted in neither the page nor the digest totals; read it in the `alerts` table only if you enabled the feature): `tick_step_failed`, `did_workflow_error`, `schedule_workflow_error`, `experiments_workflow_error`, `halluc_workflow_error`, `apply_window_unreachable`, `prompt_bar_unreachable` and `approval_missing` (severity `error`, once per sim day: a loop step or one workflow's detector raised, the policy's apply window is shorter than a tick, every catalogue prompt validates below the bar, or an approval vanished at the sim; `view` counts them by code, the controller log has the stack trace, and the loop carries on), and `detector_failed` / `producer_failed` / `veto_failed` (severity `warn`). `person_analysis_failed` (the optional daily per-person analysis raised; the loop carried on, nothing was proposed, and it is attempted at most once per sim day). `observability_rule_failed` (digest, once per sim day) means one alert rule itself raised: its alerts are missing until fixed, the other rules and the metrics are unaffected; the controller log has the stack trace. `hygiene_rejection` (page) is any approver rejection other than the "judgement call" decline (malformed request, missing validation evidence, pass rate too low): our local pre-check is wrong. `apply_by_passed` (page, once) is an APPROVED change still unapplied after its `apply_by`.

### Quick on-call triage

1. `python -m controller status --state-dir state`: is `sim_minute` moving, is lag small, any PAGE alert in the last 24 h?
2. A change you do not understand: `why --workflow W --at "Day N HH:MM"` (active version, proposal, approval, evidence, what superseded it).
3. A customer question: `why --lead <lead_id> --day N` (the plain lead id, not workflow-prefixed).
4. Controller dead: re-run `./run.sh <url>`; it reconciles before acting (section 1). Do not delete `state/` mid-run.
5. Optional feature: `ENABLE_PERSON_ANALYSIS` (`controller/settings.py`) is False because it cost +22% tick time on a short public_01 run (`SUBMISSION.md` section 12). It is analysis only and never writes config; to see its report on demand use `python -m controller person-analysis --db <store>` (read-only by default; add `--persist` to write the `person_analysis` rows and a decision into the store).

## 5. Corrupted or foreign database

Symptoms: `status` / `view` prints `cannot open ... read-only`, `database disk image is malformed`, or the controller
exits at startup with a SQLite error.

1. Stop the controller. Copy the DB and its `-wal`/`-shm` files somewhere safe.
2. Check: `python -c "import sqlite3;print(sqlite3.connect('state/X.db').execute('pragma integrity_check').fetchall())"`.
3. A **foreign** DB (a different scenario or world): nothing to repair. The fingerprint in `meta` does not match the
   simulator's first history line, so the controller archives it (`.old`) and starts a new one by itself.
4. A **corrupted** DB: move it aside (`mv state/X.db state/X.db.corrupt`, also the `-wal`/`-shm`) and re-run
   `./run.sh`. The controller re-ingests the whole history from the simulator's chunks (idempotent by `event_id`), then
   reconciles proposals from `/approvals` and `/config/history` by `proposal_id`. What is lost: our `decisions` audit
   rows, detector state (experiments restart as a new pending experiment with the current live config as control;
   supervisor edits are never reverted) and the local error counters. The live configuration and approvals are in the
   simulator and are untouched.

## 6. Crash test and dev scenarios

```
python scripts/crash_test.py                            # public_02: kill at each fault point + 10 random kills (default seed 7); exit 0 iff invariants hold
                                                        # this exact command produced reports/crash_test.log (defaults: --scenario scenarios/public_02_incidents.yaml --random-kills 10 --seed 7)
python scripts/crash_test.py --random-kills 3           # a shorter run
python scripts/crash_test.py --scenario scenarios/public_01_dev.yaml --log reports/crash_test_public_01.log   # the arguments for a log like reports/crash_test_public_01.log
bash scripts/dev_run.sh <name> <scenario.yaml> <report.md> [port]   # baseline + serve + controller + grade
bash scripts/dev_run.sh null01 devscenarios/null_01.yaml reports/dev/null_01.md 8710
bash scripts/dev_run.sh kyc_flag devscenarios/kyc_flag.yaml reports/dev/kyc_flag.md 8711
bash scripts/dev_run.sh bad_default devscenarios/bad_default.yaml reports/dev/bad_default.md 8712
bash scripts/dev_run.sh policy_strict devscenarios/policy_strict.yaml reports/dev/policy_strict.md 8713   # also null_02, renamed_ids
python scripts/mc_null.py --runs 800 --power-runs 300 --decay-runs 250   # reproduces reports/mc_null.md (script defaults are 1000 / 400 / 300)
python -m pytest -q tests                               # full suite (a few minutes)
```

`dev_run.sh` writes the controller's state to `state/<name>` and logs to `runs/ctl_<name>.log`; use
`python -m controller view --state-dir state/<name> --workflow W` afterwards.
