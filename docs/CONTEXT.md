# Context Document — Outcome-First Agent Loop (Formi SDE assignment)

> **Audience:** the candidate, plus any engineer or AI agent who picks up this work later.
> **Status:** implemented; see SUBMISSION.md for what shipped and THRESHOLDS.md for deviations.
> **Decisions taken** (§14): Python + SQLite; scope "Bigger" (A/B/Bigger offered), with per-person call time reduced to *analysis-only* after review;
> conservative exploration; a budget of about 20–24 hours.

---

## 0. Glossary and notation (read first)

| Term | Meaning |
|---|---|
| **Day N HH:MM** | Simulated day N, IST. Day 0 = 2027-01-04. History days are negative. *(The sim docs write "D3 14:00"; this doc writes "Day 3 14:00".)* |
| **W\*/V\*/D\*/P\*** | Option IDs for welcome message, voice, caller ID and prompt version (e.g. W2, V3, D4, P1). Each workflow lists its own in `GET /workflows`. |
| **DID** | Caller ID, the phone number the call comes from. "DID D2" means caller-ID option D2. |
| **Arm / control / challenger** | One option in an experiment. The control is the current production option; challengers are the alternatives. |
| **Canary / ramp** | A small first traffic share for a challenger, then a larger share if it doesn't look harmful. |
| **Holdout** | After promotion, a small share kept on the old control so we can measure whether the winner is still winning. |
| **Cell** | One combination of lead features (lang × city_tier × age_band × value_band); at most 252. |
| **Cohort** | A named, versioned predicate over cells. It is **one shared object** used by config, detection, experiments and the UI. |
| **Cube** | Our aggregate table: counts of dials, connects, engaged and flags, keyed by workflow, day, hour, cell, variant and prompt. |
| **TV distance** | Total-variation distance between two allocations: ½·Σ\|old−new\|. Auto-apply allows at most `max_shift` per cohort per dim. |
| **MDE / n_min** | Minimum detectable effect (relative lift we size for) / the minimum connects per arm needed to detect it. |
| **Holm, O'Brien–Fleming (OBF)** | Holm: a multiple-comparison correction. OBF: a schedule of strict early looks and a near-nominal final look, so we can check daily without inflating false positives. |
| **CUSUM** | Cumulative-sum change detector. Used only for decay (C7). |
| **Fatigue (approver)** | A request counted against `max_requests_per_24h`. Rejected requests still count. |
| **Hygiene** | The grader's governance counts: rejections by reason, lapsed, apply conflicts, auto-apply refusals, peak requests per 24h. |
| **In-flight approval** | Our approval that is pending, or approved but not yet applied. |
| **Incident mode** | A workflow with an open incident: a DID judged flagged (step-change test, C5) or a hallucination alert (C8). It is the only state that loosens pacing or the version lock. |
| **Harm-stop** | Removing a challenger early because it's significantly worse than control. |

Capabilities are referred to as C1–C15 (README §6; see §6 below).

---

## 1. Project rundown

Formi runs outbound voice agents (insurance renewals, KYC reminders, loan-cancellation reconfirmations). We build a
**controller**: an always-on service that, for every workflow, continuously does five things:

1. **Detect:** drifting KPIs, bad caller IDs and hallucination spikes. Detection uses thresholds on populations, never single calls.
2. **Experiment:** form cohorts, split traffic, and decide when the evidence is enough.
3. **Propose:** each change carries a summary, a diff, affected leads, a dry run, an impact score and a rationale, and goes through governance.
4. **Apply and learn:** apply approved changes, keep measuring, promote winners and roll back decaying ones.
5. **Explain:** answer *"why did workflow X change at T, who approved it, and what happened next?"* from our own store.

The controller talks **only over HTTP** to the simulator in `sim/`. Never modify `sim/`; graders run their own copy
against **hidden scenarios** that share the schema but differ in truth, incidents and policy.

KPIs are **never blended**. Pickup = connected ÷ dials. Engagement = engaged ÷ connected. The business outcome is
leads concluded ÷ leads uploaded. Each workflow declares a `primary` KPI and a `guardrail` on the other one.

---

## 2. Hard rules

| # | Rule | Consequence |
|---|---|---|
| R1 | The controller knows only what the HTTP API returns | Never import `sim`, read `runs/`, or parse `scenarios/*.yaml` from controller code. Tests and dev tooling may. |
| R2 | The governance boundary is absolute | Every change goes through `POST /approvals` → `/apply`, or a policy-permitted `PUT /config`. Supervisor edits are never silently overwritten. |
| R3 | The business owns whether and when to call | Cadence, window and capacity are off limits. We control *who* is called in a given hour and *how* the agent shows up. |
| R4 | KPIs stay separate | Optimise the primary and protect the guardrail. No blended score anywhere. |
| R5 | Statistical and deterministic winners | The LLM may only *suggest* prompts. The same data always gives the same decision. |
| R6 | Act on populations | Every trigger has a minimum-sample rule. |
| R7 | Audit from **our** store | The sim's logs don't count. |
| R8 | Credits are finite | Keep a reserve for incidents and spend only where it changes a decision. |
| R9 | Crash safety | Kill at any point, restart, and it resumes: no double-apply, no lost approval, no forgotten learning. |
| R10 | `./run.sh <SIM_URL>` runs unattended to the end | Must work on 4 vCPU / 8 GB Linux. We develop on Windows, so keep it POSIX bash + Python. |

**Development boundary.** We read `sim/` to understand the *contract*: API semantics, validation and grading. The
controller must **learn** hidden quantities from events: DID load sensitivity, hour-of-day patterns, detector error rates,
prompt quality and variant effects. Any number in this document that comes from public truth is marked *(illustrative)*
and must never become a constant in code.

---

## 3. Contract facts that drive the design (verified in `sim/world.py`, `sim/grader.py`, `sim/server.py`)

**Time and events**
- One tick is 15 sim-minutes, and time moves only on `POST /sim/advance`. **Advance is not idempotent**: never retry it blindly. Re-read
  `GET /sim/state` `sim_minute` first. Returns `409 finished` at the end.
- History (`history_days`, possibly 0) is generated under the business default config only. Fetch it with `GET /events/chunks?kind=history`;
  **the default `kind` is `events`**. Chunk numbers are global and rise across both kinds. An advance may produce zero chunks.
- `call` events carry lead/person IDs, `config_version`, `cohort_id` and the **actual variant**. They carry **no lead features**, so
  they must be joined to `lead` events (emitted when the campaign is created, before any dial).
- Under `next_campaign_only`, a call's `config_version` is the **campaign's creation version** (cohorts and allocations,
  *including caller_id*), but its prompt is the *current* one. The audit join must take allocations from `config_version`
  and the prompt and schedule from the version active at the call's `ts`.
- `GET /config/history` returns **no config bodies**, and approvals don't return `proposed_config`. So we mirror bodies:
  store our own normalised body before sending, and `GET /workflows/{id}/config` after every `config_changed` event. A supervisor
  version superseded within the same advance can't be recovered. This is a stated limitation, kept small by 15-minute ticks.

**Config semantics**
- At most 20 cohorts, first match wins. `cohorts` is compared as a whole **ordered** list, so copy it verbatim when changing other parts.
- Weights are normalised and zero weights dropped. Compare *normalised* allocations.
- **Hour weights only rank eligible leads within an hour; capacity per hour is fixed.** So a single default table does nothing,
  except that an omitted hour gets weight 0, which starves that hour. Call-time gains need **per-cohort** tables. An empty `{}` table means
  weight 1. Scheduling uses the **current** cohorts even under `next_campaign_only`. `lead_preferred_hour` gives ×6 in the chosen hour
  and ×0.25 in every other, with a floor of 0.05 under the score.

**Governance**
- **Approval.** Order of checks:
  1. Duplicate id → 409.
  2. Config invalid → 422.
  3. Stale base → 409.
  4. No change → 422.
  5. The id is **consumed**.

  "Soft" problems do not error. These are an empty summary, diff, dry run or rationale; a non-integer `affected_leads`;
  an `impact_score` outside [0,1]; missing or weak prompt evidence; and fatigue. The request is still **created**, uses a fatigue slot, and is
  rejected only at decision time, with the reasons revealed then. → **Validate all of these locally before sending.**
- Decision time = next approver-working minute after `now + U[latency]`. It becomes `expired` if that's after `expires_at`
  (`expires_in_hours` may shorten the TTL). The apply window (2 h on all public workflows) starts at the decision time.
- **Calling `/apply` after `apply_by` logs a graded lapse, and calling it on a stale base logs a graded `apply_conflict`.** → Check
  `apply_by` and the current version locally first, and don't call `/apply` when either check fails. `409 not_approved` is what a retried apply returns.
- **Auto-apply** (`PUT /config`) requires all of: enabled; every changed dim in `allowed_dims`; per-cohort-per-dim TV ≤ `max_shift`; a non-empty
  rationale; and a real change. Otherwise it returns 403 and logs it (graded) → we pre-check locally. `cohorts` is never in a public `allowed_dims`,
  so **defining cohorts always needs approval**.
- Any config change between propose and apply makes an approval stale. That includes our own auto-apply, another approval of ours being applied, and a supervisor edit.

**Hidden model, insofar as it shapes strategy** (the controller must learn all of this from events)
- **DID health falls with that DID's own recent load and recovers as the load decays.** Per-dial randomisation equalises *people
  and hours* across DIDs, **not load**. A heavily used DID can look worse than a fresh one purely from load. History starts with
  zero load, so the **history ramp reveals each workflow's load-fatigue pattern**.
- Welcome and voice affect engagement only. Effects can be cohort-specific, and some **decay**. The decay clock is per (dim, option) and
  workflow-wide, and starts at first exposure. Default arms are already exposed during history; challengers start their clock when we first explore them.
- Hallucination flags are noisy, and the controller doesn't know the error rates. Validation `pass_rate` measures safety only. Passing the
  approver's bar (≥0.97) **does not** imply a low true hallucination rate.
- `suggest-fixes` returns other catalog prompts as unranked candidates (3 credits); some are worse than the problem.

**Grader** (`sim/grader.py`) — each controller change is judged *at apply time*:
- Engagement delta = new ÷ old − 1 for expected engagement over active leads. DID delta = the same for expected DID health.
- **Harmful** if the engagement delta < −1% **or** the DID delta < −2%. This check takes precedence.
- **Beneficial** if the engagement delta > +1% or the DID delta > +2%. Otherwise neutral.
- Schedule-only changes always grade neutral.
- Prompt MTTR = time until the **first version applied after the incident whose true hallucination rate is ≤1%**.
- It also reports flagged-DID share by day, decaying-variant share by day after onset, KPI deltas, engagement gap captured, governance counts, and credits used.

**Workflow policies** (public; hidden ones will differ, so everything is read from `GET /workflows` at runtime)

| Workflow | Primary | Dials/day | Auto-apply dims (max_shift) | Req/24h | Approver hrs | Latency | TTL | Reject rate | Mid-flight |
|---|---|---|---|---|---|---|---|---|---|
| nw_renewal | engagement | 3,600 | schedule, caller_id (0.5) | 3 | 10–19 | 30–240 m | 24 h | 10% | allowed |
| nw_kyc | pickup | 2,000 | **none** | 2 | 10–18 | 60–480 m | 24 h | 15% | next_campaign_only |
| ks_cancel | engagement | 4,800 | schedule, caller_id, welcome, voice (0.2) | 4 | 10–19 | 20–120 m | 12 h | 5% | allowed |

All public workflows have a 2 h apply window and min validation pass 0.97. Credits: 200 for public_01 and 02, 400 for public_03. public_03 has 12 workflows,
~648k dials/day and ~850k events/day; its KYC copies dial 9–21, unlike the 10–20 window in public_01.

---

## 4. Requirements and deliverables

- **Floor (mandatory):** public_01 and public_02 run end-to-end with no manual steps; audit trail (C12); no constraint broken; `SUBMISSION.md` complete.
- **Deliverables:** controller + `run.sh`; `SUBMISSION.md` (template headings); `reports/` (grade output for 01 and 02, load numbers for 03);
  crash-recovery evidence; an honest small-commit history; an optional ≤5-min video.
- **Evaluation:** 25% hidden-scenario outcomes · 25% design/statistics · 20% scope + stakeholders · 20% engineering · 10% communication.
- **Live review (60 min):** explain one hidden-scenario decision from our audit store; answer "what breaks if constraint X changes"; defend "the thing you cut".
- **SRE targets:** one sim day of public_03 in **<10 wall-clock minutes**, *including the sim's own compute*; loop health visible without logs; a kill/restart test.

---

## 5. Stakeholders — claims checked against the actual policies

| Memo | Accept | Push back / decline | **Wrong or mismatched claims** |
|---|---|---|---|
| 01 Sales (Nikhil) | Batching (≤1 routine request per workflow per day); a per-workflow lift reported **separately per KPI** | A blended score (R4). "Winning by lunch → live by evening" (no minimum sample; approver latency; nw_kyc can't change mid-campaign). Rolling P4 out everywhere. | "Kestrel turned on auto-apply for **everything**": ks_cancel allows only schedule/caller_id/welcome/voice at max_shift 0.2, with no prompt and no cohort changes. "Northwind is fine with auto-apply on KYC": nw_kyc has `auto_apply.enabled: false`. P4 "tested well in another account": that isn't evidence for this workflow, and P4 isn't an option on nw_kyc. |
| 02 Compliance (Meera, Northwind) | Named approval for customer-facing changes; validation evidence for scripts; the per-customer "why did they hear X" answer (per-call store + config-version join, §3); "slow and right"; no night-time requests; a weekly summary of operational auto-applies (`/weekly` view) | — | "Call timing and caller-ID rotation are covered by the policy we signed": **true for nw_renewal, false for nw_kyc** (auto-apply disabled, so even DID rotation needs approval). Approver hours are stated correctly. The sim doesn't expose approver names (A4). |
| 03 SRE (Kabir) | Kill/restart test; idempotency; actionable alerts only; health visible without logs; the <10 min/day target and a 10× analysis | Postgres for the assignment. SQLite is one embedded file with no daemon; the schema ports to their Postgres (ops paragraph in SUBMISSION). | None found. Deploying at 21:30 is after every dialling window closes. |
| 04 Ops supervisor (Anita, Kestrel) | Never silently undo her edits; one screen; a *soft* cap of **≤2 requests/day, none before 11:00** (policy allows 4 from 10:00); no strict-script "fix" (fixes aren't chosen on validation alone, and engagement is verified after apply) | One reserved incident slot beyond her 2/day. Operational DID/schedule changes happen without advance notice and are listed in "changed today". | She asks to be **told before** changes on a running campaign (not to approve them). We meet that by sending customer-facing changes to her approver, which also gives her notice. Her ask **contradicts Sales'** claim that Kestrel wants everything automatic. Her own prompt edit is the public_02 incident: we propose the revert *through her approver, with evidence*, so it is not done behind her back. |
| 05 Product (Tara) | Separate KPIs; one shared cohort object; a documented minimum cohort size and window (§8 C2/C6); every proposal carries impact, diff, affected leads and a dry run; **expired = rejection, re-queue with fresh data**; only real arms count; per-workflow promotion only | Three P0s can't all ship at full depth. **Per-cohort call time ships; per-person call time is analysis-only**, with the evidence for why, and it's our "thing we cut". | None factual. Caveat: prompt changes have no historical dry run (one prompt per workflow), so they're labelled "validation-only". |
| 06 CTO (Aryamann) | Explainable over impressive; credits only where they change a decision; design for the scale envelope; write down every "no"; on-call docs | — | Warns that the memos contain errors (see above). |

The reply drafts (≤120 words each, ready to paste) are written in Phase 10 from this table.

---

## 6. Scope — tiered (choosing principle: spine first, then the levers with the most defensible lift per approval spent, then reach)

| Tier | # | Capability | Plan | Why |
|---|---|---|---|---|
| 1 Spine | C1 | Ingestion at load scale | **Built** | Crash safety and the SRE target depend on it. |
| 1 | C9 | Governance client + planner | **Built** | Every change goes through it; hygiene is graded. |
| 1 | C12 | Audit store | **Built** | Floor requirement plus the live-review question. |
| 1 | C13 | Crash recovery, demonstrated | **Built** | SRE non-negotiable; deliverable 4. |
| 1 | C3 | History bootstrap | **Built** | Cheap. Supplies the baselines every lever needs. |
| 2 Levers | C5 | Caller-ID health (load-aware) | **Built** | The biggest pickup lever (nw_kyc's primary KPI); the public_02 incident. |
| 2 | C8 | Hallucination incidents | **Built** | MTTR is graded; compliance-critical. |
| 2 | C2 | Shared cohort object | **Built** | Prerequisite for per-cohort schedules and analysis. |
| 2 | C4a | Per-cohort call time | **Built** | Uses history only and can't be graded harmful. |
| 2 | C6+C7 | Experiments, promotion, decay, rollback | **Built** (workflow-level first; per-cohort deploy only if time allows) | The main engagement lever; decay is graded. |
| 2 | C10 | Impact, affected leads, dry run | **Built (simple, honest)** | Required fields; Product ranks the queue by impact. |
| 3 Reach | C11 | Supervisor view | **Built as a separate command** (`controller view` CLI + `controller ui` web page over the DB). **Never started by run.sh.** | Anita and Product asked for it. Kept out of graded runs to avoid port and exit risks. |
| 3 | C14 | Loop observability | **Partial**: metrics table, `status` CLI, alert rules (no pager integration) | SRE wish. |
| 3 | C4b | Per-person call time | **Analysis-only**: estimate per person and show what we'd do with expected lift; don't push `lead_preferred_hour` | Thin evidence (~1–2 connects per person). A person-level holdout is biased because treated and control compete for the same capacity. It would compete with DID fixes for nw_kyc's 2 approvals/day. |
| Cut | C15 | Fair allocation across tenants | **Cut** (only a per-workflow incident credit reserve) | Budgets are already per workflow; global fairness matters at hundreds of tenants (future work). |
| Cut | — | Prompt experimentation outside incidents | **Cut** | One prompt per workflow means no concurrent control; a before/after test is confounded. |
| Cut | — | Cross-workflow promotion; welcome/voice tests on pickup-primary workflows | **Cut / deferred** | Product says later. Optimise the primary KPI. |

Every lever sits behind a feature flag in `settings.py` and stays **off until its phase is green**. A half-built engine
must never ship enabled.

---

## 7. Architecture

```
 SIM (HTTP)                       controller run  (single process, single SQLite writer)
 ──────────                       ───────────────────────────────────────────────────────
 /sim/advance ◄──────────────── loop.py  (fixed per-tick order, §8 C9)
 /events/chunks ───────────────► ingest.py ── one txn per chunk: leads, calls, cube, cursor ──► state/<scenario>-<fp>.db (WAL)
 /workflows/*/config ──────────► config mirror (fetched on every config_changed)                    ▲      ▲
                                  detectors/did.py · detectors/halluc.py ── read cube ───────────────┤      │
                                  cohorts.py · schedule.py · experiments.py · person_analysis.py ── stats.py
                                  planner.py ── desired changes, priorities, in-flight rules
 /approvals, /apply, PUT config ◄ governance.py ◄ proposals.py (full config, local validation, dry run, impact)
 /llm, /validation ◄──────────── incidents.py        (write-ahead intent row before every outbound mutation)

 controller view|why|status|ui  ── separate read-only processes over the same DB (never started by run.sh)
```

**Package `controller/`** (each file ≤400 lines): `__main__.py` (CLI: run, status, why, view, ui), `settings.py` (tunables + flags),
`client.py`, `store.py` + `schema.sql`, `ingest.py`, `cohorts.py`, `cube.py`, `stats.py`, `detectors/did.py`,
`detectors/halluc.py`, `schedule.py`, `experiments.py`, `person_analysis.py`, `proposals.py`, `governance.py`,
`planner.py`, `incidents.py`, `loop.py`, `audit.py`, `view.py`, `ui.py`, `metrics.py`.

**State** (SQLite, WAL mode, `synchronous=NORMAL`):

| Table | Contents |
|---|---|
| `meta` | World fingerprint, schema version |
| `ingest_cursor` | Last chunk and last event_id |
| `leads` | lead_id, person_id, workflow, packed cell code, campaign, minute |
| `calls` | Compact per-call rows; indexed on `lead_id` and on (`workflow_id`, day) for audit and per-person reads |
| `cube` | Aggregates (see glossary) |
| `config_versions` | Full bodies + actor + our proposal link |
| `cohort_defs` | Versioned cohort definitions |
| `experiments` + `arms` | Experiment state; every statistical look is stored as a `decisions` row |
| `proposals` + `proposal_events` | Full state machine history |
| `decisions` | Every detect / propose / hold / skip / apply / rollback, with an inputs snapshot as JSON |
| `incidents` | |
| `validation_runs`, `llm_calls` | Together they form the credit ledger |
| `jobs` | Last sim-time each scheduled job ran, so jobs catch up after a restart |
| `detector_state` | Persisted, or recomputable from `cube` |
| `alerts`, `metrics` | |

**World identity.** The fingerprint is the scenario name from `/sim/state` plus a hash of the first history chunk's
first line plus its event-id range. If the fingerprint doesn't match, or the sim's `last_event_id` is behind our cursor,
the sim has restarted as a new world → open a fresh DB (`state/<scenario>-<fp>.db`).

**Restart sequence:**
1. Open the DB and check the fingerprint.
2. Re-read `sim_minute`.
3. Reconcile governance (C9).
4. Re-list chunks after the cursor.
5. Rebuild the in-memory caches from SQLite (seconds).
6. Continue the loop.

---

## 8. Capability design (how + why)

### C1 Ingestion
- **How:** process `GET /events/chunks?kind=…&after=cursor` in chunk order. For each chunk, in **one transaction**:
  - insert leads and calls;
  - upsert the cube;
  - record config and approval events;
  - advance the cursor.

  An `event_id > last_event_id` guard is a second line of defence.
- **Why:** chunks are immutable files on the sim side, so the sim is a durable log and re-reading after a crash costs nothing.
  One transaction per chunk gives exactly-once effects.
- **Performance:** batched `executemany`; stdlib json first, with `orjson` only if measurement shows a need (and justified). Phase 1 includes a
  timed one-day public_03 ingest, so we find out early whether the spine is fast enough.

### C2 Shared cohort object
- **Cohorts are a partition, so first-match order can't matter.** At Day 0 we pick the feature, or pair of features, that best explains
  **hour-of-day pickup variation in history** (largest likelihood gain), keeping only groups whose expected volume is ≥ n_min (below).
  The selection is data-driven, never taken from scenario truth. Cap: 20.
- **Experiment analysis groups** are at most **2 pre-registered unions** of those cohorts, each ≥35% of traffic.
- **n_min:** a cohort is *deployable* for an experiment decision only if it can reach n_min connects per arm within the run,
  using the time-to-verdict table in C6. Otherwise it is pooled. The cost of this rule is fewer per-cohort verdicts; the benefit is a lower false-winner rate. That trade-off is our answer to Product's open question.
- **Self-check:** for every call, our cohort assignment must equal the sim's `cohort_id`, using the cohorts of *that event's* `config_version`. A mismatch alerts.
- **Cold start** (`history_days = 0`): no cohorts on Day 0. Build them from Day 0 data and propose on Day 1.

### C3 History bootstrap
Before Day 0 we build these baselines:
- pickup by hour × candidate feature;
- per-DID pickup over the history ramp, which gives the **load-fatigue pattern**;
- flag rate and flow-adherence for the production prompt;
- engagement level and variance, for power calculations;
- volume per cell.

### C4a Per-cohort call time
- **How:** estimate each cohort's pickup multiplier by hour from history with shrinkage, controlling for attempt_no and campaign_day.
  This controls for survivorship: good responders conclude early, which biases later hours.
- **Weights:** proportional to the multiplier, normalised to mean 1 and clipped to [0.3, 3]. **Every in-window hour is present in every table.**
- **Guardrail:** a dry run re-weights recent dials to estimate the engagement-composition shift. Block the change if it exceeds the guardrail.
- **Routing:** the first change goes in the Day-0 approval bundle, because the tables need the cohorts that bundle defines. Later refreshes auto-apply where `schedule` is allowed.
  Under next_campaign_only the schedule applies immediately.
- **Verify:** there is no concurrent control, so compare hour-mix-adjusted pickup before and after. Roll back if pickup drops beyond the guardrail.
  This comparison is confounded, and we say so.

### C4b Per-person call time (analysis-only)
- **How:** for returning persons, compute a Beta posterior of pickup per hour block, using the cohort profile as the prior.
  Report coverage (how many persons qualify), the expected lift, and the entries we *would* write.
- **Why not deploy:** see §6. This is the "cut" we defend in the review, with numbers.

### C5 Caller-ID health (load-aware)
- **Learn fatigue.** Fit a monotone curve from history and live data: pickup ratio as a function of each DID's trailing decayed dial volume,
  with the decay window chosen by fit. No functional form is taken from the sim.
- **Two kinds of "bad".**
  - **Flagged:** a sudden **step drop** in a DID's pickup ratio against its peers while its share and load are unchanged. Two-proportion test,
    p < 0.001, ≥150 dials each side, over a rolling 3-hour window against the prior day. This opens incident mode.
  - **Weak base:** worse than peers *at comparable load*. A low-load DID that picks up no better than loaded peers counts.
  - A loaded DID that merely looks worse than a fresh one is **not** bad. We rebalance instead of removing it.
- **Spread.** The default target is an even split across non-bad DIDs. That's standard load spreading against spam-flagging, confirmed by the fitted curve.
- **Probe new DIDs safely.** One DID at a time, with share
  `s ≤ 0.015 · h̄ / (h̄ − h_floor)`, typically about 3%. Here h̄ is the current traffic-weighted relative health from the curve, and
  h_floor (default 0.35) is a tunable prior for the worst plausible DID. This keeps the worst-case DID-health step above the −2% harmful line.
  Ramp to an even share after ≥1 day of passing.
- **Evacuate a flagged DID** in steps of TV ≤ max_shift: one step per tick in incident mode, otherwise one per hour (A5). Move traffic first
  to tested-healthy DIDs. Untested ones may take evacuated traffic beyond the probe cap, because their worst case is no worse than the flagged source.
- **Re-entry.** A DID removed as *loaded* comes back once its trailing load falls below its peers'. A DID removed as *flagged* is not re-probed
  within a run, because moving traffic back is graded harmful; production would probe weekly.
- **nw_kyc** (single DID, no auto-apply, next_campaign_only):
  1. The history ramp shows D1's fatigue.
  2. Bundle 1 proposes probes of the other DIDs.
  3. Bundle 2 spreads traffic evenly.
  4. Each step takes effect at the next campaign, and old campaigns keep their DIDs until their cadence ends.

### C6 Welcome × voice experiments (conservative; engagement-primary workflows only)
- **Design.** The sim randomises every dial independently, which gives a full factorial. We analyse main effects per dim. Interactions are reported
  from the 2-way table and acted on only if significant after correction.
- **Unit of analysis.** The connected call, with **per-lead clustered standard errors**, because repeat connects by the same lead understate variance.
- **Allocation.**
  - Entry: each challenger gets 7.5%; total challengers ≤22.5% per dim; control keeps the rest.
  - Ramp: after ≥1 day and ≥100 connects, arms with point estimate ≥ control ramp to 20%, keeping control ≥40%. Arms at z ≤ −1 are dropped in the same bundle.
  - Every step is one judged change.
- **Honest limit.** We **cannot** both learn and guarantee "never harmful": if every challenger is bad, the entry step can grade harmful.
  We cap the worst-plausible step at roughly 2–3% and stop harmful arms as soon as the evidence allows.
- **Harm-stop.** Check every 4 h:
  - z ≤ −3 with ≥150 connects → remove now, via auto-apply where allowed or the reserved approval slot.
  - z ≤ −2.5 → fold into the next daily bundle, which avoids spending an approval on a likely-null false stop.
  - Expected false stops: about 5% per null arm across the run. That costs only learning.
- **Winner.** One-sided z-test against control at **daily looks** after the window closes, with **OBF boundaries over ≤4 looks** and **Holm across
  arms within each dim** (one family per dim). Promotion requires:
  - lower-bound lift > 0;
  - point lift ≥ 3%;
  - **the last 24 h agree** (recent-window lift lower bound > 0), so we never promote a variant that has already started decaying.

  This is deterministic. The false-winner rate is measured, not assumed (§10).
- **Guardrail.** A non-inferiority test on the guardrail KPI with margin `max_drop` (A3): the upper bound of the drop must be below the margin.
- **Customer-facing changes always go through approval**, even where auto-apply is allowed. Auto-apply is used only for operational
  changes (DID, schedule) and risk-reducing ones (harm-stop, rollback).
- **Supervisor edit mid-experiment.** Mark the affected dim *interrupted*; data from before the edit stays valid for arms that ran. Never re-impose.
  The next proposal treats the supervisor's choice as the new control and **says so** in `change_summary`.

**Time to verdict.** These are estimates; the controller recomputes them from history volumes at Day 0 and logs them as a decision row.

| Workflow (≈connects/day, *est.*) | Challenger share | 15% lift needs ≈300–400 connects | 10% lift needs ≈650–900 connects |
|---|---|---|---|
| nw_renewal (~900) | 7.5% → 20% after Day 1 | ≈Day 3 | ≈Day 5–6 |
| ks_cancel (~1,050) | 7.5% → 20% after Day 1 | ≈Day 2–3 | ≈Day 4–5 |

Effects under 10%, and cohorts under 35% of traffic, generally won't reach a verdict in an 8–10-day run. That is by design: it's what a false-winner rate of ≤5% costs.

### C7 Promotion, decay, rollback
- **Promote.** The winner takes most of its group's traffic and a **10% holdout** stays on the old control. This is one approval with full evidence;
  if a dropped arm or a ramp is due, it goes in the same bundle.
- **Decay detection.**
  - *During* an experiment, the concurrent control catches decay: the recent-window lift is compared with the cumulative lift.
  - *After* promotion, the primary trigger is a **CUSUM on the winner's own composition-adjusted engagement** (hour × cell mix from the cube)
    against its level just after promotion. Because the winner carries ~90% of traffic, it is high-powered.
  - The holdout acts as directional confirmation: its point estimate is ≥ the winner.
- **Rollback.** Triggered when the CUSUM alarms and the holdout confirms, or when the winner is significantly worse than the holdout. Use the reserved slot,
  or auto-apply where allowed (a rollback is risk-reducing).

### C8 Hallucination incidents
- **Day 0 validation of each workflow's live prompt** (n=300, 3 credits each). This gives an absolute safety baseline that doesn't depend
  on the detector's unknown error rates, so a *bad default prompt* is caught at Day 0. It also **pre-computes the revert evidence**: a
  later revert to this prompt needs no new validation, which shortens MTTR.
- **Detect.** Flag rate for the *current prompt segment*, against the last known-good prompt's flag rate (history or Day 0). One-sided
  two-proportion test, p < 0.001, ≥200 connects. Re-run on a rolling 1-day window to catch drift. A drop in flow-adherence corroborates. A
  `config_changed` event that touches the prompt starts a new segment, so detection begins immediately.
- **Fix.**
  1. Revert to the **last known-good prompt in this workflow's own record** (clean flags, known engagement), using the Day-0 validation run as evidence.
  2. If none exists, call `suggest-fixes` (3 credits), validate the candidates (n=300 each), and rank by the **lowest failure upper bound**,
     not merely ≥0.97. Then verify engagement after apply, which catches the strict-script trap Anita describes (safe, but nobody stays on the call).
- **Credits.** Keep a reserve of ≥25 per workflow in incident mode. Make no speculative calls.
- **Verify after apply.** The flag rate returns to baseline and engagement stays within the guardrail; otherwise try the next candidate.
- **The proposal names the supervisor's edit**, for example: "Revert P2 (set by supervisor Day 3 11:30) to P1: flags x% vs y% baseline (n=…, p=…); validation val_… pass z".

### C9 Governance client + planner
**Per-tick order:**
1. Reconcile in-flight approvals (poll and adopt decisions).
2. Ingest new chunks and mirror configs.
3. Apply approved changes, if `apply_by` hasn't passed and the base equals the current version. Otherwise re-plan **without** calling `/apply`.
4. Run detectors.
5. Planner.
6. Submit, with an intent row first.
7. Write the metrics row.
8. Advance: 15 min, or 60 min when outside the dialling and approver windows with nothing in flight. Record the target first; on error, re-read `sim_minute`.

**Planner rules:**
- **P1.** At most one in-flight approval per workflow. The single exception is a superseding incident proposal (P2).
- **P2. Priority:** incident fix > severe harm-stop or rollback > daily bundle. A higher-priority need while something is in flight produces a
  **superseding proposal** containing the new change *plus every still-wanted change* from the in-flight one; it uses the reserved slot.
  Whichever is approved first *and* still valid is applied. The other is marked `SUPERSEDED` and never applied, so no lapse or conflict is logged.
  If two higher-priority needs collide on a 2-requests/day workflow, they merge into one proposal.
- **P3.** Operational auto-applies (DID, schedule) run only when nothing is in flight on that workflow, with one exception: a flagged-DID
  evacuation whose estimated ratio is < 0.6. That evacuation acts at once and the in-flight approval is re-planned. Any other auto-apply waits until the approval resolves.
- **P4. Budget:** a rolling ledger keyed on `created_at`, three tiers. The LAST slot is reserved exclusively for safety incidents (hallucination fix,
  flagged-DID evacuation on non-auto workflows); harm-stop/rollback may use the second-to-last slot (`max − 1`); routine bundles get `max(1, max − 2)`.
  A rollback therefore can never block a safety fix, and a rollback colliding with a safety need is merged into the one superseding proposal (P2).
  ks_cancel has a soft cap of 2/day and none before 11:00.
- **P5. Timing:** file at approver-open, or 11:00 for ks_cancel, only if the worst-case decision time `next_working(now + max_latency)` falls before
  `expires_at`. Never at night. Scheduled jobs (daily bundle, daily looks, hourly DID evaluation) are keyed by sim-time, with `last_run` persisted.
- **P6. Day-0 sequence:**
  1. At the first tick after the history ingest, auto-apply any DID spreading that the policy allows (nw_renewal, ks_cancel).
  2. Run the Day-0 validations (C8).
  3. At approver-open, file one bundle per workflow with:
     - the cohort definitions;
     - the per-cohort schedules;
     - the experiment entry allocations (engagement-primary workflows);
     - the DID probes or spread where auto-apply isn't allowed (nw_kyc).
- **P7. Local validation:** every field the approver checks (§3) is checked before sending, and so is the auto-apply policy.

**State machine:** `DRAFT → INTENT (proposal_id fixed, body stored) → PENDING → APPROVED → APPLIED → MONITORING → CLOSED`. Failure branches:
- `REJECTED` (reasons): learn from them.
- `EXPIRED`: treated as a rejection. Re-plan with fresh data, a new id and the new base.
- `STALE`: the base changed. Re-plan and re-read any supervisor edit.
- `LAPSED`: should be impossible; alert.
- `SUPERSEDED` (see P2).
- `SUBMIT_FAILED`: a hard 409 or 422; alert.

The auto path is `INTENT → AUTO_APPLIED | STALE`. A 403 means a pre-check bug; alert.

**Idempotency:** `proposal_id = "{workflow}:{plan_seq}:{kind}"`, written before sending. On restart:
- **INTENT with no approval_id:** `GET /approvals?workflow_id=`, match on proposal_id. If it isn't there, resend the identical stored body.
- **APPROVED:** apply if `apply_by` hasn't passed and the base is current.
- **Auto INTENT:** look for the proposal_id in `config/history`.

### C10 Impact, affected leads, dry run
- **dry_run:**
  - Replay the last 2–3 days of the cube: **observed per-arm rates × new weights**, per cohort, with a CI.
  - Arms never observed are labelled `no_evidence` (Product's rule).
  - Schedule changes are replayed by hour re-weighting.
  - Prompt changes are labelled "validation-only".
- **impact_score** = clip(lower-bound relative lift on the primary KPI × share of traffic affected ÷ 0.10, 0, 1). A score of 1.0 means a lift of
  ≥10% across the whole workflow. The same formula applies everywhere, so the queue ranking is comparable. Safety fixes use min(1, excess hallucination rate ÷ 1 pp).
- **affected_leads:** active, in-cadence leads whose effective distribution changes, plus the expected next-batch share where the workflow is next_campaign_only.

### C11 Supervisor view (separate command)
- `controller view --db … --workflow ks_cancel` prints the one-screen summary. `controller ui --db …` serves the same content as server-rendered HTML
  (FastAPI, already a dependency) from a **separate read-only process**. WAL mode allows it to read alongside a live run.
- **Sections:** what's being tested (arms, shares, n, estimate ± CI) · what's winning · **what we need from you** (pending approvals with summary and impact) ·
  **what changed today** (actor, reason, link to `why`) · alerts.
- `/weekly` lists operational auto-applies for Compliance. The per-person analysis (C4b) is shown here too.

### C12 Audit
- `controller why --workflow nw_renewal --at "Day 3 14:00"` returns:
  1. the active version;
  2. our decision row (inputs snapshot, tests, rationale);
  3. the proposal and approval (id, decided_at, approver = the workflow's policy approver);
  4. post-apply monitoring;
  5. whatever superseded it.
- `controller why --lead <id> --day 2` returns the customer's calls with their variant, plus the decisions behind their allocation version and their prompt version (split join, §3). This answers Compliance's IRDAI ask.

### C13 Crash recovery
- **Fault injection:** `CRASH_AT=<point>:<n>` calls `os._exit` at named points: mid-ingest-txn, after-submit-before-record,
  after-apply-before-record, after-advance-before-ingest. Random `kill -9`s on top of that.
- `scripts/crash_test.py` runs every point plus N random kills to completion.
- **Invariants checked:** each config version maps to a unique proposal_id; no double-apply; the grade shows `lapsed = 0` and `apply_conflicts = 0`;
  every in-flight approval is reconciled; the cursor is monotonic.
- Output goes to `reports/crash_test.log`.

### C14 Observability
- **Metrics per tick:** events ingested; ingest wall-time; lag (wall-time from advance-return to decisions-done, and sim-minutes since the newest event);
  decisions per hour; credits burn; in-flight approvals and their deadlines; locally counted 422/409s. Shown via `controller status`.
- **Page:** ingest stalled; an approved change within 30 min of `apply_by` and not applied; credits below the incident reserve; repeated sim 5xx;
  any fatigue rejection or 403 (our accounting is wrong); a cohort self-check mismatch.
- **Morning digest only:** judgement rejections; starved experiments; a weak-base DID; unattributed KPI drift (daily z-test against the trailing 3-day hour-adjusted baseline).

### Error handling (`client.py`)
| Response | Action |
|---|---|
| 5xx / timeout | Bounded exponential backoff, then alert. **Never for advance**: re-read `sim_minute` instead. |
| 409 duplicate | Reconcile. |
| 409 stale | Re-plan. |
| 409 not_approved | Reconcile. |
| 409 finished | Exit 0. |
| 410 | Should be impossible; alert. |
| 402 | Alert (reserve breached). |
| 422 / 403 | Record and alert; never retry blindly. |

Tests inject a `fastapi.testclient.TestClient` (an httpx.Client subclass) wrapping the real sim app.

---

## 9. Action plan — phases with hour budgets (≈24 h total) and a floor checkpoint

Working method (from your global rules):
- TDD: tests first for each module.
- Run the `python-reviewer` / `code-reviewer` agents after each phase.
- Small conventional commits.
- Files ≤400 lines.
- Never touch `sim/`.
- Keep a `docs/TIMELOG.md`, which supplies "hours actually spent".
- Each phase also updates its SUBMISSION section, so the write-up never piles up at the end.

| Phase (budget) | Actions | Done when (measurable) |
|---|---|---|
| **0 Setup (0.5 h)** | 0.1 Save this doc as `docs/CONTEXT.md` · 0.2 `controller/` skeleton + `settings.py` flags · 0.3 `run.sh`: pick `python3` or `python`; wait on `/health`; restart loop around `python -m controller run --url $1 --state-dir state` until `/sim/state.finished` (restart cap 20; exit 0 only when finished); no UI · 0.4 `.gitattributes` (`*.sh eol=lf`), `git update-index --chmod=+x run.sh`; `.gitignore` `state/`; Makefile target `run` · 0.5 baselines for 01/02/03 | `./run.sh` advances public_01 to the end with no changes, and the grade equals the baseline |
| **1 Spine (3 h)** | client.py (error taxonomy above); store + schema + fingerprint; ingest (both kinds, one txn per chunk); lead join; cube; config mirror; decisions table; loop with per-tick order + adaptive step | Re-ingesting a chunk changes nothing (test); fingerprint mismatch opens a new DB (test); **timed one-day public_03 ingest recorded**; the cohort self-check passes on default-only calls |
| **2 Governance + planner (3 h)** | proposals.py (full-config builder, verbatim cohorts, hour-table completeness, local field and auto-apply validation, TV); governance.py (state machine, rolling ledger, timing, reconciliation); planner.py (P1–P7); minimal C10 (dry run from the cube, impact formula, affected leads) | Integration test against the real sim: submit → approve → apply; stale → re-plan without `/apply`; expired → re-plan; supersede path; restart mid-INTENT and mid-APPROVED reconciles correctly |
| **3 Bootstrap + DID + incidents (3 h)** | C3 baselines; detectors/did.py (fatigue curve, step test, probe cap, evacuation, re-entry); detectors/halluc.py; incidents.py (Day-0 validation, revert, LLM fallback, verify); credit reserve | public_02 grade: `flagged_did_share_by_day` ≤0.2 on Day 2 and ≤0.01 from Day 3; prompt-incident `mttr_minutes` < 360 with `fixed_by = controller_approved`; `llm_calls = 0`; no harmful DID changes |
| **4 FLOOR CHECKPOINT (1.5 h, ≈hour 11)** | `why --at` working; minimal crash test (3 fault points + 3 random kills); commit `reports/public_01_dev.md` and `public_02_incidents.md` (`make grade` naming); draft SUBMISSION §0–4, §7, §9 | A gradeable floor exists. Grade shows `auto_apply_rejected = 0`, `lapsed = 0`, `apply_conflicts = 0`, no fatigue or field rejection reasons; committed |
| **5 Cohorts + per-cohort schedule (2 h)** | cohorts.py (data-driven partition, self-check under next_campaign_only); schedule.py; included in the Day-0 bundle | nw_kyc `pickup_rate` Δ > 0 vs baseline on public_01; guardrail respected (engagement drop ≤0.02 absolute) |
| **6 Experiments + decay (4 h)** · *cut trigger: not green by hour ≈18 → workflow-level only; no per-cohort deploys* | experiments.py (entry → ramp → OBF/Holm winner; harm-stop; clustered SE; non-inferiority guardrail; recent-window check); promotion with holdout; CUSUM decay + rollback; supervisor-edit handling | public_01: `engagement_rate` Δ > 0 on nw_renewal and ks_cancel; `harmful` ≤ 1, each explained in SUBMISSION; decaying-variant share ≤0.1 by onset + 2 days. public_02: the Day-5 supervisor voice edit is never reverted without an approval that names it |
| **7 View, UI, status, alerts (2 h)** | view.py / ui.py (separate process); metrics + `status`; alert rules; finish the C10 dry-run labels | The UI shows all five sections for each workflow; `status` shows lag, credits and deadlines; `why` reproduces a real change end to end |
| **8 Reliability, scale, robustness (2 h)** | Full crash test (every fault point + 10 random kills) → `reports/crash_test.log`; `scripts/load_test.py` on public_03 (events/s, decision lag, peak RSS via `resource` on Linux; wall-time per sim day including sim compute) → `reports/public_03_load.md`; `devscenarios/` (dev-only copies: ≥5 null-effect seeds, one policy variant with auto-apply disabled everywhere and different approver hours, one bad default prompt); Monte Carlo of the decision rule (≥1,000 null runs); determinism test | Crash invariants hold; <10 wall-min per public_03 day; null runs give **0 promotions** and the Monte Carlo false-winner rate is ≤5%; two public_01 runs produce identical `decisions` rows and `sim grade --json` |
| **9 Per-person analysis (1 h)** | person_analysis.py; shown in the view and the SUBMISSION numbers | Coverage, expected lift and the bias caveat are quantified |
| **10 Write-up (2 h)** | SUBMISSION complete: all 15 sections; stakeholder replies ≤120 words; assumptions; trade-offs; 10× analysis; Postgres ops paragraph; public_02 timeline via `controller why --workflow ks_cancel --from "Day 2" --to "Day 4"`; DID detector false-positive rate from the null runs. Plus an on-call runbook in `docs/` | A teammate can run, debug and extend it from the docs alone |

If time runs short, Phases 7–9 shrink first; the floor (Phase 4) is never at risk.

---

## 10. Verification — expected outcomes

| Check | How | Expected |
|---|---|---|
| Unit | `pytest tests/controller` covering: stats (sample size, OBF, Holm, clustered SE, CUSUM); cohort first-match and partition; config builder (verbatim cohorts, hour completeness, TV); local validation; planner rules; governance transitions; reconciliation; ingest idempotency; fingerprint | Green; ≥80% coverage of `controller/` |
| Integration | Real sim app in-process via the TestClient, on a 2-day shortened scenario (tests may use `sim`) | End to end, no constraint broken |
| public_01 | `make baseline` / `serve`, `./run.sh`, `make grade` | Unattended finish; the Phase 5–6 targets; governance counts all zero (above) |
| public_02 | same | The Phase 3 targets; the supervisor edit respected |
| public_03 | `scripts/load_test.py` | <10 wall-min per sim day; numbers recorded |
| Crash | `scripts/crash_test.py` | All invariants hold; run completes; log saved |
| False winners | `devscenarios` null seeds + Monte Carlo | 0 promotions; rate ≤5% |
| Determinism (R5) | Two identical public_01 runs | Identical decisions and grade |
| Audit | `controller why …` on a real change and a real lead | Answers what, why, who approved, and what happened next |

---

## 11. Assumptions (numbered for SUBMISSION §1)

- **A1.** The controller knows only the HTTP API. Sim code was read solely to understand the contract.
- **A2.** IST throughout; "Day 3 14:00" = sim minute 3·1440 + 840.
- **A3.** `*_max_drop: 0.02` is an **absolute** 2-percentage-point drop in the guardrail rate versus the concurrent control (or the pre-change baseline
  when there's no control). The relative change is also reported.
- **A4.** "Who approved" = the workflow's policy approver, identified by approval_id. The sim exposes no personal names.
- **A5.** max_shift limits speed of change: at most one auto-apply step per workflow per hour, or per tick in incident mode. Steps are never chained within a tick.
- **A6.** Supervisor edits are legitimate. We never revert one except through an approval that names the edit and gives the evidence.
- **A7.** Anita's limits (2/day, nothing before 11:00) are a soft preference layered on top of policy. Incidents may use the reserved slot.
- **A8.** Hidden scenarios may change options, policies, incidents, prompts and history length (including 0). Every capability has a cold-start rule.
- **A9.** h_floor = 0.35 (worst plausible DID health) and the ±12% worst plausible variant effect are *priors*. They are tunable and documented as such.

## 12. Risks

- **Exploration can still grade harmful** if every challenger is bad. Mitigated by small entry shares, harm-stop and ramping only good arms. Disclosed.
- **Decay can begin before promotion.** Handled by the recent-window check and the concurrent control.
- **Schedule changes have no concurrent control.** Verification is a confounded before/after comparison. Disclosed.
- **SQLite at 10×:** one writer and the size of `calls` (to be measured). The production plan: Postgres partitioned by day; workers sharded by
  tenant/workflow; a global coordinator for credits and approver fairness. The parse and insert cost scales linearly, so it is not the first thing to break.
- **Windows development vs Linux grading:** keep run.sh POSIX with LF endings; run it from Git Bash; the crash test uses `subprocess`.

## 13. Critical files

- **New:**
  - `controller/` (package above)
  - `tests/controller/`
  - `scripts/crash_test.py`, `scripts/load_test.py`
  - `devscenarios/`
  - `docs/CONTEXT.md`, `docs/RUNBOOK.md`, `docs/TIMELOG.md`
  - `reports/`, `SUBMISSION.md`, `.gitattributes`
- **Modified:** `run.sh`, `Makefile` (`run` target), `.gitignore` (`state/`).
- **Untouched:** `sim/`, `scenarios/`, `docs/SIMULATOR.md`.
- **Reused:** `examples/naive_controller.py` shows the API round-trip only, and its anti-patterns are listed in its docstring. The `Makefile` targets baseline, serve and grade.
  `requirements.txt` already covers httpx, numpy, fastapi, uvicorn and pytest, so we add no new runtime dependency unless a measurement justifies one.

## 14. Decision log

| Decision | Choice | Source |
|---|---|---|
| Stack | Python + SQLite (WAL) | Candidate |
| Scope | "Bigger" (C11 UI + per-person on top of the core). Per-person later reduced to analysis-only; UI moved to a separate command. | Candidate + review |
| Exploration risk | Conservative (entry 7.5%/challenger; customer-facing changes always approved) | Candidate |
| Time | 20+ h; plan budgets ≈24 h with a floor checkpoint at ≈hour 11 | Candidate + review |

## 15. Elephant Goldfish review log (five fresh-context reviewers, run on draft 1)

| Reviewer | Verdict / key findings | Changes made |
|---|---|---|
| Technical accuracy | 3 blockers: DID load confound; a 10% probe can be harmful; the proposal-id consumption rules and soft-rejection path were wrong. Plus: no config bodies in history; advance isn't idempotent; apply_conflict/lapse logging; schedule semantics; the next_campaign_only audit join; hidden-truth numbers | §3 rewritten; load-aware C5 with a probe-cap formula; P7 local validation; config mirror; C9 apply pre-checks; C4a per-cohort only; split audit join; illustrative numbers removed |
| Implementation readiness | READY WITH FIXES: run.sh robustness; planner unspecified; D0 cohorts vague; unrealistic decay target; unmeasurable done-criteria | run.sh spec; C9 tick order + P1–P7; data-driven C2; recent-window and CUSUM decay; done criteria keyed to grader fields; determinism and Monte Carlo checks; fault injection |
| Design / statistics | Underpowered C6 (α split too thin); canary vs harm lines; holdout too weak for decay; no cold start; run identity; per-person biased | Holm per dim + OBF ≤4 looks + ≤2 groups; time-to-verdict table; CUSUM decay with holdout confirmation; Day-0 validation; fingerprint; per-person analysis-only |
| Scope / simplicity | Floor reached too late; over budget; table didn't say "no"; UI inside the graded process; too many stats layers | Floor checkpoint at Phase 4; tiers + cuts + feature flags; budgets with a cut trigger; UI as a separate process; drift module and halluc CUSUM dropped; schedule before experiments |
| Comprehension | "D" overloaded; undefined jargon; contradiction between canary and harm; reserved-slot conflict; status and next step unclear | Glossary + notation; honest harm limit stated; P2 priority/merge rule; header with status and next step |
