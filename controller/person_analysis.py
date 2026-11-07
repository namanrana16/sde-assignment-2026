"""C4b per-person call-time analysis (ANALYSIS ONLY).

For each *returning* person (seen in >= ``PERSON_MIN_CAMPAIGNS`` campaigns of a workflow) with enough dials spread over
enough hour blocks, a Beta posterior of pickup per block is formed with the person's cohort hour profile as the prior
(``PERSON_PRIOR_STRENGTH`` pseudo-dials). A preferred block would be assigned only if P(pickup of the best block >
cohort average pickup) >= ``PERSON_PASS_PROB``.

Nothing here is ever proposed or applied: this module imports no planner, governance or client code, writes only
``person_analysis`` and a ``decisions`` row (kind ``person_analysis``) and reports what *would* be assigned. Pushing
``lead_preferred_hour`` is rejected by design (CONTEXT section 6): the x6 / x0.25 hour weights act under a fixed hourly
capacity, so treated persons displace controls and a person-level holdout is biased.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from . import settings
from .store import Store

JOB = "person_analysis"
KIND = "person_analysis"
MINUTES_PER_DAY = settings.MINUTES_PER_DAY

TABLE_DDL = """CREATE TABLE IF NOT EXISTS person_analysis (
    workflow_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    sim_minute INTEGER NOT NULL,
    cohort_id TEXT NOT NULL,
    dials INTEGER NOT NULL,
    blocks_observed INTEGER NOT NULL,
    best_block INTEGER NOT NULL,
    would_assign_hour INTEGER,
    p_best_above_avg REAL NOT NULL,
    prior_only_p REAL NOT NULL,
    post_mean_best REAL NOT NULL,
    prior_mean_best REAL NOT NULL,
    cohort_avg REAL NOT NULL,
    data_weight REAL NOT NULL,
    qualifies INTEGER NOT NULL,
    data_driven INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, person_id)
) WITHOUT ROWID"""

NOTE = ("Analysis only, nothing is written to config. The lift assumes the person is dialled in the preferred block "
        "at no cost to anyone else. In the sim lead_preferred_hour is x6 in the chosen hour and x0.25 in every other "
        "hour under a fixed hourly capacity, so treated persons displace others' dials and the realised lift is lower "
        "than this figure; for the same reason a person-level holdout is biased (treated displace controls), so the "
        "effect could not be measured cleanly either.")


# ---------------------------------------------------------------- pure math
def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-13:
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """Regularised incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_sf(x: float, a: float, b: float) -> float:
    """P(theta > x) for theta ~ Beta(a, b)."""
    if x <= 0.0:
        return 1.0
    if x >= 1.0:
        return 0.0
    return 1.0 - beta_cdf(x, a, b)


def block_edges(lo: int, hi: int) -> list[tuple[int, int]]:
    """Split the dialling window [lo, hi) into 4 blocks (3 for a short window), as even as possible."""
    span = hi - lo
    k = max(1, min(4 if span >= 10 else 3, span))
    base, extra = divmod(span, k)
    edges, start = [], lo
    for i in range(k):
        end = start + base + (1 if i < extra else 0)
        edges.append((start, end))
        start = end
    return edges


@dataclass(frozen=True)
class Posterior:
    alpha: float
    beta: float
    mean: float
    data_weight: float  # n / (n + k): the share of the posterior mean that comes from the person's own dials


def posterior(prior_mean: float, dials: int, connects: int, strength: float | None = None) -> Posterior:
    k = settings.PERSON_PRIOR_STRENGTH if strength is None else strength
    p = min(max(prior_mean, 1e-6), 1 - 1e-6)
    a, b = k * p + connects, k * (1 - p) + dials - connects
    return Posterior(a, b, a / (a + b), dials / (dials + k) if dials + k > 0 else 0.0)


@dataclass(frozen=True)
class CohortProfile:
    rates: Mapping[str, Sequence[float]]  # cohort -> pickup per block (the prior means)
    average: Mapping[str, float]  # cohort -> overall pickup
    hour_rates: Mapping[str, Mapping[int, float]] = field(default_factory=dict)
    edges: Sequence[tuple[int, int]] = ()

    def best_hour(self, cohort: str, block: int) -> int | None:
        if not self.edges:
            return None
        lo, hi = self.edges[block]
        rates = self.hour_rates.get(cohort, {})
        return max(range(lo, hi), key=lambda h: (rates.get(h, 0.0), -h))


@dataclass(frozen=True)
class Assessment:
    person_id: str
    cohort: str
    dials: int
    blocks_observed: int
    best_block: int
    p_best_above_avg: float
    prior_only_p: float  # same probability for the cohort's own best block with no person data
    post_mean_best: float
    prior_mean_best: float
    cohort_avg: float
    data_weight: float  # mean n_b / (n_b + k) over the blocks this person was dialled in
    mean_shift: float  # mean |posterior - prior| / prior over those blocks
    qualifies: bool
    data_driven: bool  # qualifies although the cohort profile alone would not


def assess(person: Mapping, profile: CohortProfile) -> Assessment | None:
    """Posterior per block for one person, or None when the person is not eligible."""
    blocks = person["blocks"]
    dials = sum(n for n, _ in blocks)
    seen = [i for i, (n, _) in enumerate(blocks) if n > 0]
    cohort = person["cohort"]
    if (person["campaigns"] < settings.PERSON_MIN_CAMPAIGNS or dials < settings.PERSON_MIN_DIALS
            or len(seen) < settings.PERSON_MIN_BLOCKS or cohort not in profile.rates):
        return None
    prior, avg = profile.rates[cohort], profile.average[cohort]
    post = [posterior(prior[i], n, x) for i, (n, x) in enumerate(blocks)]
    best = max(range(len(post)), key=lambda i: (post[i].mean, -i))
    p_best = beta_sf(avg, post[best].alpha, post[best].beta)
    pb = max(range(len(prior)), key=lambda i: (prior[i], -i))
    prior_post = posterior(prior[pb], 0, 0)
    prior_only = beta_sf(avg, prior_post.alpha, prior_post.beta)
    qualifies = p_best >= settings.PERSON_PASS_PROB
    shifts = [abs(post[i].mean - prior[i]) / max(prior[i], 1e-6) for i in seen]
    return Assessment(person["person_id"], cohort, dials, len(seen), best, p_best, prior_only, post[best].mean,
                      prior[best], avg, sum(post[i].data_weight for i in seen) / len(seen), sum(shifts) / len(shifts),
                      qualifies, qualifies and prior_only < settings.PERSON_PASS_PROB)


def null_replay(persons: Sequence[Mapping], profile: CohortProfile, seed: int = 0) -> dict:
    """Selection check: redraw every eligible person's connects from the cohort block rates (no person effect at all)
    keeping their dials per block, and run the same rule. What qualifies here qualifies by chance (best-of-blocks),
    so the real qualifying count and lift must be read against it."""
    import numpy as np
    rng = np.random.default_rng(seed)
    q, lifts, avgs = 0, 0.0, 0.0
    for p in persons:
        if p["cohort"] not in profile.rates:
            continue
        rate = profile.rates[p["cohort"]]
        fake = {**p, "blocks": [(n, int(rng.binomial(n, rate[i])) if n else 0) for i, (n, _) in enumerate(p["blocks"])]}
        a = assess(fake, profile)
        if a and a.qualifies:
            q += 1
            lifts += a.post_mean_best
            avgs += a.cohort_avg
    return {"null_qualifying": q, "null_expected_lift": round(lifts / avgs - 1, 5) if avgs else 0.0}


def summarise(rows: Sequence[Assessment], total_dials: int) -> dict:
    """Workflow report. The lift is relative pickup of the best block vs the cohort average, ignoring capacity."""
    q = [r for r in rows if r.qualifies]
    avg_sum = sum(r.cohort_avg for r in q)
    lift = sum(r.post_mean_best for r in q) / avg_sum - 1 if avg_sum else 0.0
    prior_lift = sum(r.prior_mean_best for r in q) / avg_sum - 1 if avg_sum else 0.0
    share = sum(r.dials for r in q) / total_dials if total_dials else 0.0
    mean = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return {"eligible": len(rows), "qualifying": len(q), "qualifying_data_driven": sum(r.data_driven for r in q),
            "qualifying_on_prior_alone": sum(not r.data_driven for r in q),
            "mean_data_weight": round(mean([r.data_weight for r in rows]), 4),
            "mean_shift_from_prior": round(mean([r.mean_shift for r in rows]), 4),
            "expected_lift_ignoring_capacity": round(lift, 5), "lift_from_cohort_prior": round(prior_lift, 5),
            "lift_from_person_data": round(lift - prior_lift, 5), "qualifying_dial_share": round(share, 5),
            "workflow_lift_upper_bound": round(lift * share, 5), "note": NOTE}


# ---------------------------------------------------------------- store layer
def _workflows(store: Store) -> list[tuple[str, int, int, int]]:
    cur = store.conn.execute("SELECT workflow_id, MIN(hour), MAX(hour) + 1, COUNT(*) FROM calls GROUP BY workflow_id")
    return sorted(cur.fetchall())


def load_profile(store: Store, wid: str, edges: Sequence[tuple[int, int]]) -> CohortProfile:
    cells = store.conn.execute("SELECT cohort_id, hour, COUNT(*), SUM(connected) FROM calls WHERE workflow_id=? "
                               "GROUP BY cohort_id, hour", (wid,)).fetchall()
    tot: dict[str, list[int]] = {}
    hours: dict[str, dict[int, tuple[int, int]]] = {}
    for c, h, n, x in cells:
        t = tot.setdefault(c, [0, 0])
        t[0] += n
        t[1] += x
        hours.setdefault(c, {})[h] = (n, x)
    k0 = settings.PERSON_PRIOR_STRENGTH  # sparse cohort blocks are shrunk to the cohort average with the same weight
    avg = {c: t[1] / t[0] for c, t in tot.items() if t[0]}
    rates, hour_rates = {}, {}
    for c, a in avg.items():
        blk = []
        for lo, hi in edges:
            n = sum(hours[c].get(h, (0, 0))[0] for h in range(lo, hi))
            x = sum(hours[c].get(h, (0, 0))[1] for h in range(lo, hi))
            blk.append((x + k0 * a) / (n + k0))
        rates[c] = blk
        hour_rates[c] = {h: nx[1] / nx[0] for h, nx in hours[c].items() if nx[0]}
    return CohortProfile(rates, avg, hour_rates, tuple(edges))


def load_persons(store: Store, wid: str, edges: Sequence[tuple[int, int]]) -> list[dict]:
    """Returning persons with their per-block (dials, connects) and modal cohort. One grouped, index-backed query."""
    rows = store.conn.execute(
        "WITH ret AS (SELECT person_id, COUNT(DISTINCT campaign_id) AS camps FROM leads WHERE workflow_id=? "
        "GROUP BY person_id HAVING camps >= ?) "
        "SELECT l.person_id, ret.camps, c.cohort_id, c.hour, COUNT(*), SUM(c.connected) "
        "FROM calls c JOIN leads l ON l.lid = c.lid JOIN ret ON ret.person_id = l.person_id "
        "WHERE c.workflow_id=? GROUP BY l.person_id, c.cohort_id, c.hour",
        (wid, settings.PERSON_MIN_CAMPAIGNS, wid)).fetchall()
    block_of = {h: i for i, (lo, hi) in enumerate(edges) for h in range(lo, hi)}
    people: dict[str, dict] = {}
    cohort_dials: dict[str, dict[str, int]] = {}
    for pid, camps, cohort, hour, n, x in rows:
        b = block_of.get(hour)
        if b is None:
            continue
        p = people.setdefault(pid, {"person_id": pid, "campaigns": camps, "blocks": [(0, 0)] * len(edges)})
        bn, bx = p["blocks"][b]
        p["blocks"][b] = (bn + n, bx + x)
        cd = cohort_dials.setdefault(pid, {})
        cd[cohort] = cd.get(cohort, 0) + n
    for pid, p in people.items():
        p["cohort"] = max(sorted(cohort_dials[pid]), key=lambda c: cohort_dials[pid][c])
    return list(people.values())


def analyse_workflow(store: Store, wid: str, lo: int, hi: int, total_dials: int) -> tuple[dict, list[Assessment], CohortProfile]:
    edges = block_edges(lo, hi)
    profile = load_profile(store, wid, edges)
    persons = load_persons(store, wid, edges)
    rows = [a for p in persons if (a := assess(p, profile))]
    summary = summarise(rows, total_dials)
    summary.update(null_replay([p for p in persons if assess(p, profile)], profile))
    summary.update({"window": [lo, hi], "blocks": [list(e) for e in edges]})
    return summary, rows, profile


def _persist(store: Store, wid: str, now: int, summary: dict, rows: list[Assessment], profile: CohortProfile) -> None:
    ranked = sorted(rows, key=lambda r: (not r.qualifies, -r.p_best_above_avg, r.person_id))
    ranked = ranked[:settings.PERSON_MAX_ROWS_PER_WORKFLOW]
    data = [(wid, r.person_id, now, r.cohort, r.dials, r.blocks_observed, r.best_block,
             profile.best_hour(r.cohort, r.best_block) if r.qualifies else None, r.p_best_above_avg, r.prior_only_p,
             r.post_mean_best, r.prior_mean_best, r.cohort_avg, r.data_weight, int(r.qualifies), int(r.data_driven))
            for r in ranked]
    inputs = {**summary, "analysis_only": True, "prior_strength": settings.PERSON_PRIOR_STRENGTH,
              "pass_prob": settings.PERSON_PASS_PROB, "min_dials": settings.PERSON_MIN_DIALS,
              "min_blocks": settings.PERSON_MIN_BLOCKS, "min_campaigns": settings.PERSON_MIN_CAMPAIGNS}
    text = (f"person call-time analysis (analysis only): {summary['eligible']} eligible, {summary['qualifying']} would "
            f"qualify, lift ignoring capacity {summary['expected_lift_ignoring_capacity'] * 100:+.1f}%; no config change")
    with store.transaction() as conn:
        conn.execute(TABLE_DDL)
        conn.execute("DELETE FROM person_analysis WHERE workflow_id=?", (wid,))
        conn.executemany("INSERT INTO person_analysis VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
    store.add_decision(now, KIND, text, wid, inputs)


def run(store: Store, now_minute: int, persist: bool = True) -> dict[str, dict]:
    """Analyse every workflow with calls; returns the per-workflow report. Never touches config."""
    out: dict[str, dict] = {}
    for wid, lo, hi, total in _workflows(store):
        summary, rows, profile = analyse_workflow(store, wid, lo, hi, total)
        if persist:
            _persist(store, wid, now_minute, summary, rows, profile)
        out[wid] = summary
    return out


def maybe_run_daily(store: Store, now_minute: int) -> dict[str, dict] | None:
    """Daily job for the loop: flag-gated, at most once per sim day. Returns None when it did not run."""
    if not settings.ENABLE_PERSON_ANALYSIS:
        return None
    last = store.get_job(JOB)
    if last is not None and last // MINUTES_PER_DAY == now_minute // MINUTES_PER_DAY:
        return None
    store.set_job(JOB, "", now_minute)  # mark the attempt first: a failing run must not retry (and alert) every tick
    return run(store, now_minute)


def render(report: Mapping[str, Mapping]) -> str:
    lines = []
    for wid, s in report.items():
        lines += [f"{wid}: eligible {s['eligible']}, would qualify {s['qualifying']} "
                  f"({s['qualifying_data_driven']} from person data, {s['qualifying_on_prior_alone']} on the cohort prior alone)",
                  f"  data weight {s['mean_data_weight'] * 100:.1f}% (mean shift from prior {s['mean_shift_from_prior'] * 100:.1f}%)",
                  f"  expected lift ignoring capacity {s['expected_lift_ignoring_capacity'] * 100:+.1f}% "
                  f"(cohort prior {s['lift_from_cohort_prior'] * 100:+.1f}%, person data {s['lift_from_person_data'] * 100:+.1f}%)",
                  f"  with NO person effect at all the same rule qualifies {s['null_qualifying']} persons "
                  f"(lift {s['null_expected_lift'] * 100:+.1f}%): that part is selection of the best of {len(s['blocks'])} blocks",
                  f"  workflow-level upper bound {s['workflow_lift_upper_bound'] * 100:+.2f}% "
                  f"(qualifying persons carry {s['qualifying_dial_share'] * 100:.1f}% of dials)"]
    return "\n".join(lines + ["", NOTE])
