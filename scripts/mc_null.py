"""Monte Carlo of the C6/C7 experiment rules at 4-hour tick resolution.

Applies the controller's own decision code unchanged: ``exp_rules.daily_look`` (ramp / drop / Lan-DeMets OBF winner
test with Holm, point-lift, recent-window and pickup gates, over Mantel-Haenszel strata of share regime x day),
``exp_alloc`` (entry shares, two-stage promotion weights), ``exp_decay`` (stage-2 gate, sequential winner-vs-control
decay test, CUSUM + holdout), the apply-time predicates (``exp_decay.veto_lower_bound``, ``ramp_vetoed``,
``drop_vetoed``) and the end-of-experiment revert rule. Approval is a latency (parameter, 2-20 h) after which the
change is re-checked and applied or withheld.

Scenarios: null; 10% / 15% true lift; ramp mix (+10% / 0 / -10%); calendar drift of the base rate (+-5%, stratified
and unstratified); and a decaying arm (true lift +15% falling linearly to -10% over 2 days from a random onset) at
several approval latencies, with a risk-reducing rollback by approval and by stepped auto-apply.

Per-lead clustering comes from a Beta lead-propensity model (intra-lead correlation 0.10). The 4-hourly harm-stop only
ever removes arms, so leaving it out is conservative for the false-winner rate.

usage: python scripts/mc_null.py [--runs 1000] [--power-runs 400] [--decay-runs 300] [--out reports/mc_null.md]
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller import exp_alloc, exp_decay as xd, exp_rules as er, settings  # noqa: E402

DIALS_PER_DAY = 3000
TICKS_PER_DAY = 6
TICK_HOURS = 24 // TICKS_PER_DAY
DIALS_PER_TICK = DIALS_PER_DAY // TICKS_PER_DAY
PICKUP = 0.30
ENGAGE = 0.50
LEADS = 4500
ICC = 0.10
MAX_DROP = 0.02
CONTROL = "C"
DRIFT = 0.05
DRIFT_PERIOD_DAYS = 5.0
DECAY_FROM, DECAY_TO, DECAY_DAYS = 0.15, -0.10, 2.0
AUTO_STEP_HOURS = 1  # stepped auto-apply: one step (<= 0.95 x max_shift 0.2) per hour
MAX_SHIFT = 0.2


@dataclass(frozen=True)
class Scenario:
    label: str
    lifts: dict = field(default_factory=dict)  # arm -> constant true lift
    challengers: tuple = ("A", "B")
    days: int = 8
    decay: bool = False  # arm A decays from DECAY_FROM to DECAY_TO over DECAY_DAYS, from a random onset
    drift: bool = False
    stratified: bool = True
    groups: bool = True
    latency_h: float = 8.0
    rollback: str = "approval"  # approval | auto
    full_pipeline: bool = False  # keep going after a promotion (stages, decay alarm, rollback)


class Window:
    """Engaged rows per (arm, stratum, lead) and pickup rows per (arm, lead) for one scope since a start tick."""

    def __init__(self) -> None:
        self.eng: dict[str, dict[str, dict[int, tuple[float, float]]]] = {}
        self.pick: dict[str, dict[int, tuple[float, float]]] = {}

    def add(self, arm: str, stratum: str, eng: dict, pick: dict) -> None:
        dst = self.eng.setdefault(arm, {}).setdefault(stratum, {})
        for lid, (n, x) in eng.items():
            a = dst.get(lid, (0.0, 0.0))
            dst[lid] = (a[0] + n, a[1] + x)
        pdst = self.pick.setdefault(arm, {})
        for lid, (n, x) in pick.items():
            a = pdst.get(lid, (0.0, 0.0))
            pdst[lid] = (a[0] + n, a[1] + x)


def _flat(strata: dict) -> dict:
    out: dict[int, tuple[float, float]] = {}
    for rows in strata.values():
        for lid, (n, x) in rows.items():
            a = out.get(lid, (0.0, 0.0))
            out[lid] = (a[0] + n, a[1] + x)
    return out


def arm_data(cum: Window, recent: deque, arms, stratified: bool) -> dict:
    rec = Window()
    for tick_rows in recent:
        for arm, (stratum, eng, pick) in tick_rows.items():
            rec.add(arm, stratum, eng, pick)
    out = {}
    for arm in arms:
        e_s, r_s = cum.eng.get(arm, {}), rec.eng.get(arm, {})
        out[arm] = er.ArmData(_flat(e_s), _flat(r_s), cum.pick.get(arm, {}), e_s if stratified else None,
                              r_s if stratified else None)
    return out


def true_lift(sc: Scenario, arm: str, t_days: float, onset: float) -> float:
    if sc.decay and arm == "A":
        if t_days <= onset:
            return DECAY_FROM
        f = min(1.0, (t_days - onset) / DECAY_DAYS)
        return DECAY_FROM + f * (DECAY_TO - DECAY_FROM)
    return sc.lifts.get(arm, 0.0)


class Run:
    """One simulated experiment (and, for decay scenarios, its promotion lifecycle)."""

    def __init__(self, seed: int, sc: Scenario) -> None:
        self.sc, self.rng = sc, np.random.default_rng(seed)
        a = b = (1 - ICC) / ICC / 2
        self.propensity = self.rng.beta(a, b, size=LEADS)
        self.group = self.rng.integers(0, 2, size=LEADS)
        self.onset = float(self.rng.uniform(1.0, 6.0)) if sc.decay else math.inf
        self.phase = float(self.rng.uniform(0, 2 * math.pi))
        self.arms = (CONTROL, *sc.challengers)
        self.scopes = ["all"] + (["g0", "g1"] if sc.groups else [])
        alphas = {"all": settings.EXP_ALPHA_WORKFLOW, "g0": settings.EXP_ALPHA_GROUP, "g1": settings.EXP_ALPHA_GROUP}
        self.alpha = {s: alphas[s] for s in self.scopes}
        self.cum = {s: Window() for s in self.scopes}
        self.recent = {s: deque(maxlen=TICKS_PER_DAY) for s in self.scopes}
        self.st = {s: {"arm_t": {}, "looks": 0} for s in self.scopes}
        self.live = exp_alloc.entry_shares(sc.challengers)  # shares actually served
        self.plan = dict(self.live)  # shares the controller state believes (applied only after approval)
        self.active, self.ramped = list(sc.challengers), []
        self.regime = 0
        self.pending: dict | None = None
        self.phase_name = "exp"
        self.flags: dict[str, list[tuple[int, list[int]]]] = {a: [] for a in self.arms}  # (tick, engaged flags of connects)
        self.log: dict = {"promoted": None, "stage1": None, "stage2": None, "alarm": None, "rolled_back": None,
                          "winner": None, "scope": None, "exposure": 0.0, "share_at_onset2": None,
                          "ramped": set(), "dropped": set(), "withheld": 0}

    # ------------------------------------------------------------------ data
    def _drift(self, t_days: float) -> float:
        return 1.0 + DRIFT * math.sin(2 * math.pi * t_days / DRIFT_PERIOD_DAYS + self.phase) if self.sc.drift else 1.0

    def _tick_data(self, tick: int) -> dict:
        t_days = tick / TICKS_PER_DAY
        arms = list(self.arms)
        probs = np.array([self.live.get(a, 0.0) for a in arms])
        probs[0] = 1.0 - probs[1:].sum()
        arm_idx = self.rng.choice(len(arms), size=DIALS_PER_TICK, p=probs)
        lead = self.rng.integers(0, LEADS, size=DIALS_PER_TICK)
        connected = self.rng.random(DIALS_PER_TICK) < PICKUP
        mult = np.array([(1.0 + true_lift(self.sc, a, t_days, self.onset)) for a in arms])[arm_idx] * self._drift(t_days)
        engaged = connected & (self.rng.random(DIALS_PER_TICK) < np.minimum(1.0, self.propensity[lead] * mult))
        stratum = f"{tick // TICKS_PER_DAY}:{self.regime}"
        out: dict = {}
        for scope in self.scopes:
            sel = np.ones(DIALS_PER_TICK, bool) if scope == "all" else self.group[lead] == int(scope[1])
            per_arm = {}
            for k, arm in enumerate(arms):
                m = sel & (arm_idx == k)
                eng: dict = {}
                pick: dict = {}
                for l, c, e in zip(lead[m].tolist(), connected[m].tolist(), engaged[m].tolist()):
                    p = pick.get(l, (0, 0))
                    pick[l] = (p[0] + 1, p[1] + c)
                    if c:
                        q = eng.get(l, (0, 0))
                        eng[l] = (q[0] + 1, q[1] + e)
                per_arm[arm] = (stratum, eng, pick)
            out[scope] = per_arm
        for k, arm in enumerate(arms):
            self.flags[arm].append((tick, engaged[(arm_idx == k) & connected].astype(int).tolist()))
        return out

    def _ingest(self, rows: dict) -> None:
        for scope, per_arm in rows.items():
            for arm, (stratum, eng, pick) in per_arm.items():
                self.cum[scope].add(arm, stratum, eng, pick)
            self.recent[scope].append(per_arm)

    def _data(self, scope: str = "all") -> dict:
        return arm_data(self.cum[scope], self.recent[scope], self.arms, self.sc.stratified)

    # ------------------------------------------------------------------ decisions
    def _snapshot(self) -> dict:
        return {"plan": dict(self.plan), "active": list(self.active), "ramped": list(self.ramped)}

    def _restore(self, snap: dict) -> None:
        self.plan, self.active, self.ramped = snap["plan"], snap["active"], snap["ramped"]

    def _daily_look(self, tick: int) -> None:
        days = (tick + 1) / TICKS_PER_DAY
        winners, main = [], None
        for scope in self.scopes:
            data = self._data(scope)
            out = er.daily_look(data, CONTROL, self.active, len(self.sc.challengers), self.alpha[scope],
                                arm_t=self.st[scope]["arm_t"], looks_taken=self.st[scope]["looks"], ramped=self.ramped,
                                exposure_days=days, max_drop=MAX_DROP)
            if out.took_look:
                self.st[scope]["looks"] += 1
                self.st[scope]["arm_t"] = out.arm_t
            if out.winner:
                winners.append((scope, out.winner))
            if scope == "all":
                main = (out, data)
        out, data = main
        latency = int(math.ceil(self.sc.latency_h / TICK_HOURS))
        snap = self._snapshot()
        if winners:
            self.log.update(winner=winners[0][1], scope=winners[0][0], promoted=tick)
            self.pending = {"kind": "promote", "at": tick + 1 + latency, "snap": snap, "stage": 1}
            self.phase_name = "decided"
            return
        ended = self.st["all"]["looks"] >= len(settings.EXP_LOOK_FRACTIONS)
        drops = list(out.drop)
        if ended:
            drops += [a for a in er.end_of_experiment_reverts(out.records, self.plan, CONTROL) if a not in drops]
        self.log["dropped"] |= set(drops)
        self.log["ramped"] |= set(out.ramp)
        new_plan = exp_alloc.next_shares(self.plan, out.ramp, drops)
        self.active = [a for a in self.active if a not in drops]
        self.ramped = sorted(set(self.ramped) | set(out.ramp))
        if new_plan != self.plan:
            self.pending = {"kind": "update", "at": tick + 1 + latency, "snap": snap, "ramp": list(out.ramp),
                            "drop": drops, "plan": new_plan}
        self.plan = new_plan

    def _apply_update(self, tick: int) -> None:
        p = self.pending
        rows = er.summarise(self._data("all"), CONTROL, p["ramp"] + p["drop"])
        if any(er.ramp_vetoed(rows[a]) for a in p["ramp"]) or any(er.drop_vetoed(rows[a], a in self.ramped) for a in p["drop"]):
            self._restore(p["snap"])
            self.log["withheld"] += 1
        else:
            self.live = dict(p["plan"])
            self.regime += 1
        self.pending = None

    def _recent_contrast(self, winner: str):
        data = self._data("all")
        return er.pair_contrast(data[winner], data[CONTROL], recent=True)

    # ------------------------------------------------------------------ promoted phase
    def _apply_promotion(self, tick: int) -> bool:
        winner = self.log["winner"]
        ok, _ = xd.veto_lower_bound(self._recent_contrast(winner))
        if not ok:
            self._restore(self.pending["snap"])
            self.log.update(winner=None, promoted=None, scope=None)
            self.log["withheld"] += 1
            self.pending, self.phase_name = None, "exp"
            return False
        self.live = exp_alloc.promotion_weights(winner, CONTROL, 1)
        self.live.pop(CONTROL)
        self.regime += 1
        self.pending, self.phase_name = None, "stage1"
        self.log["stage1"] = tick
        self._new_stage(tick)
        return True

    def _new_stage(self, tick: int) -> None:
        self.stage_start, self.epoch = tick, {"start": tick, "ts": []}
        self.post = {s: Window() for s in ("stage", "epoch")}
        self.post_ticks: list[dict] = []
        self.blocks: list = []
        self.block = [0, 0]
        ref_rows = _flat(self.cum["all"].eng.get(CONTROL, {}))
        n = sum(v[0] for v in ref_rows.values())
        self.p_ref = sum(v[1] for v in ref_rows.values()) / n if n else ENGAGE
        pre = [f for (t, f) in self.flags[self.log["winner"]] if tick - 2 * TICKS_PER_DAY <= t < tick]
        flat = [x for f in pre for x in f]
        p = self.p_ref
        self.level = (float(len(flat)), float(sum(flat)), len(flat) * p, len(flat) * p * (1 - p)) \
            if len(flat) >= settings.EXP_CUSUM_BLOCK else None

    def _post_tick(self, rows: dict, tick: int) -> None:
        self.post_ticks.append({a: rows["all"][a] for a in (self.log["winner"], CONTROL)})
        for x in self.flags[self.log["winner"]][-1][1]:
            self.block[0] += 1
            self.block[1] += x
            if self.block[0] >= settings.EXP_CUSUM_BLOCK:
                n, xe = self.block
                self.blocks.append((float(n), float(xe), n * self.p_ref, n * self.p_ref * (1 - self.p_ref)))
                self.block = [0, 0]

    def _window_data(self, from_tick: int, last_ticks: int | None = None):
        win, rec = Window(), deque(maxlen=TICKS_PER_DAY)
        winner = self.log["winner"]
        for i, per_arm in enumerate(self.post_ticks):
            t = self.stage_start + 1 + i  # post_ticks start the tick AFTER the stage went live
            if t >= from_tick:
                for arm, (stratum, eng, pick) in per_arm.items():
                    win.add(arm, stratum, eng, pick)
            rec.append(per_arm)
        return arm_data(win, rec, (winner, CONTROL), self.sc.stratified)

    def _decay_tick(self, tick: int) -> None:
        winner = self.log["winner"]
        latency = int(math.ceil(self.sc.latency_h / TICK_HOURS))
        data_epoch = self._window_data(self.epoch["start"] + 1)
        c = er.pair_contrast(data_epoch[winner], data_epoch[CONTROL])
        seq = xd.sequential_alarm(c, self.epoch["ts"])
        self.epoch["ts"] = seq["ts"]
        data_stage = self._window_data(self.stage_start + 1)
        recent = er.pair_contrast(data_stage[winner], data_stage[CONTROL], recent=True)
        held = {"recent_lift": recent.lift} if recent is not None else {}
        cus = xd.cusum(self.blocks, self.level)
        roll, _ = xd.decide(cus, held, seq)
        if seq["epoch_done"] and not roll:
            self.epoch = {"start": tick, "ts": []}
        if roll:
            if self.sc.rollback == "auto":
                shift = 0.9 if self.phase_name == "stage2" else 0.5
                delay = int(math.ceil(shift / (0.95 * MAX_SHIFT))) * AUTO_STEP_HOURS // TICK_HOURS  # steps are hourly
            else:
                delay = latency
            self.log["alarm"] = tick
            self.pending = {"kind": "rollback", "at": tick + delay}
            self.phase_name = "rolling"
        elif self.phase_name == "stage1" and self.pending is None:
            since = self._window_data(self.stage_start + 1)
            sc_, rc_ = er.pair_contrast(since[winner], since[CONTROL]), er.pair_contrast(since[winner], since[CONTROL], recent=True)
            ok, _ = xd.stage2_ready((tick - self.stage_start) * TICK_HOURS * 60, sc_, rc_)
            if ok:
                self.pending = {"kind": "stage2", "at": tick + 1 + latency}

    def _apply_stage2(self, tick: int) -> None:
        winner = self.log["winner"]
        ok, _ = xd.veto_lower_bound(self._recent_contrast(winner))
        self.pending = None
        if not ok:
            self.log["withheld"] += 1
            return
        self.live = {winner: 1.0 - settings.EXP_HOLDOUT}
        self.regime += 1
        self.phase_name = "stage2"
        self.log["stage2"] = tick
        self._new_stage_keep_cusum(tick)

    def _new_stage_keep_cusum(self, tick: int) -> None:
        keep_blocks, keep_block, keep_level, keep_p = self.blocks, self.block, self.level, self.p_ref
        self._new_stage(tick)
        self.blocks, self.block, self.level, self.p_ref = keep_blocks, keep_block, keep_level, keep_p

    # ------------------------------------------------------------------ main loop
    def _track_exposure(self, tick: int) -> None:
        t_days = tick / TICKS_PER_DAY
        share = self.live.get("A", 0.0)
        if t_days >= self.onset:
            self.log["exposure"] += share / TICKS_PER_DAY
        if self.log["share_at_onset2"] is None and t_days >= self.onset + 2.0:
            self.log["share_at_onset2"] = share

    def run(self) -> dict:
        total = self.sc.days * TICKS_PER_DAY
        for tick in range(total):
            rows = self._tick_data(tick)
            self._ingest(rows)
            if self.phase_name in ("stage1", "stage2", "rolling"):
                self._post_tick(rows, tick)
            if self.sc.decay:
                self._track_exposure(tick)
            p = self.pending
            if p and tick >= p["at"]:
                if p["kind"] == "update":
                    self._apply_update(tick)
                elif p["kind"] == "promote":
                    self._apply_promotion(tick)
                elif p["kind"] == "stage2":
                    self._apply_stage2(tick)
                elif p["kind"] == "rollback":
                    self.live = {}
                    self.regime += 1
                    self.log["rolled_back"] = tick
                    if self.log["share_at_onset2"] is None:
                        self.log["share_at_onset2"] = 0.0 if tick / TICKS_PER_DAY < self.onset + 2.0 else None
                    break
            if self.phase_name == "decided" and not self.sc.full_pipeline:
                break
            if self.phase_name == "exp" and self.pending is None and tick % TICKS_PER_DAY == TICKS_PER_DAY - 1 and self.active:
                self._daily_look(tick)
            elif self.phase_name in ("stage1", "stage2") and self.pending is None:
                self._decay_tick(tick)
            elif self.phase_name == "stage1" and self.pending is not None and self.pending["kind"] != "rollback":
                self._decay_tick(tick)  # a stage-2 request in flight does not pause the decay test
        return self.log


def simulate(seed: int, sc: Scenario) -> dict:
    log = Run(seed, sc).run()
    log["ramped"], log["dropped"] = sorted(log["ramped"]), sorted(log["dropped"])
    log["onset"] = None
    return log


def _job(args):
    seed, sc = args
    return simulate(seed, sc)


def _ticks(a, b):
    return None if a is None or b is None else b - a


def run(sc: Scenario, runs: int, base_seed: int, pool) -> dict:
    res = list(pool.map(_job, [(base_seed + i, sc) for i in range(runs)], chunksize=4))
    n = len(res)
    out = {"label": sc.label, "runs": n}
    won = [r for r in res if r["winner"]]
    out["any"] = len(won) / n
    for arm in sc.challengers:
        out[f"win_{arm}"] = sum(r["winner"] == arm for r in res) / n
    out["median_day"] = float(np.median([r["promoted"] / TICKS_PER_DAY for r in won])) if won else None
    out["ramped"] = {a: sum(a in r["ramped"] for r in res) / n for a in sc.challengers}
    out["dropped"] = {a: sum(a in r["dropped"] for r in res) / n for a in sc.challengers}
    if sc.full_pipeline:
        live = [r for r in res if r["stage1"] is not None]
        out["stage1"] = len(live) / n
        out["stage2"] = sum(r["stage2"] is not None for r in res) / n
        out["rolled_back"] = sum(r["rolled_back"] is not None for r in res) / n
        out["exposure"] = float(np.mean([r["exposure"] for r in live])) if live else None
        sh = [r["share_at_onset2"] for r in res if r["share_at_onset2"] is not None]
        out["share_onset2"] = float(np.mean(sh)) if sh else None
        out["share_onset2_le_01"] = float(np.mean([s <= 0.1 for s in sh])) if sh else None
        lat = [r["rolled_back"] - r["stage1"] for r in live if r["rolled_back"] is not None]
        out["median_rb_ticks"] = float(np.median(lat)) if lat else None
    return out


def scenarios(latencies=(2.0, 8.0, 20.0)) -> list[tuple[str, Scenario, str]]:
    base = [("null", Scenario("null (all arms 0%)"), "power"),
            ("p10", Scenario("10% lift on A, B null", {"A": 0.10}), "power"),
            ("p15", Scenario("15% lift on A, B null", {"A": 0.15}), "power"),
            ("mix", Scenario("ramp mix: A +10%, B 0%, D -10%", {"A": 0.10, "D": -0.10}, challengers=("A", "B", "D")), "power"),
            ("drift_s", Scenario("null + base-rate drift +-5%, stratified", drift=True), "power"),
            ("drift_u", Scenario("null + base-rate drift +-5%, NOT stratified", drift=True, stratified=False), "power"),
            ("drift15", Scenario("15% lift on A + drift, stratified", {"A": 0.15}, drift=True), "power")]
    decay = []
    for lat in latencies:
        decay.append((f"decay_a{lat:g}", Scenario(f"decaying A, approval {lat:g} h, rollback by approval", challengers=("A", "B"),
                                                  days=16, decay=True, groups=False, latency_h=lat, full_pipeline=True), "decay"))
    decay.append(("decay_auto", Scenario("decaying A, approval 8 h, rollback by stepped auto-apply", days=16, decay=True,
                                         groups=False, latency_h=8.0, rollback="auto", full_pipeline=True), "decay"))
    decay.append(("decay_drift", Scenario("decaying A + drift, approval 8 h, rollback by approval", days=16, decay=True,
                                          drift=True, groups=False, latency_h=8.0, full_pipeline=True), "decay"))
    return base + decay


def render(rows: list[dict]) -> list[str]:
    lines = ["# Monte Carlo of the experiment decision rule (C6) and decay response (C7)", "",
             "Generated by `python scripts/mc_null.py`; applies `controller.exp_rules`, `exp_alloc` and `exp_decay` unchanged "
             "at 4-hour tick resolution (looks at each day's end).", "",
             f"Setup: {DIALS_PER_DAY} dials/day at pickup {PICKUP} (about {int(DIALS_PER_DAY * PICKUP)} connects/day), "
             f"engagement {ENGAGE}, {LEADS} leads with intra-lead correlation {ICC}; one control and two (ramp mix: three) "
             "challengers entering at 7.5% (ramp to 20% after >= 1 day and >= 100 connects at point >= control; drop on the "
             "whole-experiment z <= -1.5 below 300 connects, <= -1 above); one workflow test (alpha "
             f"{settings.EXP_ALPHA_WORKFLOW}) and two half-traffic analysis groups (alpha {settings.EXP_ALPHA_GROUP} each, "
             f"{settings.EXP_ALPHA_DIM} per dim). Contrasts are Mantel-Haenszel pooled over strata of day x share regime. "
             "Winner: <= 4 Lan-DeMets OBF looks at information 0.25/0.5/0.75/1, Holm, point lift >= 3%, recent-24h one-sided "
             "95% lower bound > 0, pickup non-inferiority. Promotion is two-stage: stage 1 winner 50% / old control 50%; stage 2 "
             f"90/10 after >= 24 h at stage 1 with a positive recent lower bound and concurrent contrast. Every approved change "
             "waits an approval latency, then is re-checked (promotion stages: recent-window one-sided 80% lower bound > 0; ramp: "
             "z and recent z; drop: still at the drop bound) and applied or withheld. Decay: sequential winner-vs-control test "
             f"(alpha {settings.EXP_DECAY_ALPHA} per epoch, Lan-DeMets OBF, epoch = information for a -15% effect at 80% power) plus "
             "CUSUM confirmed by the holdout direction. The harm-stop (removal only) is omitted, which is conservative.", "",
             "## Selection (no decay)", "",
             "| scenario | runs | any winner | A | B | D | median day | ramped A/B/D | dropped A/B/D |", "|---|---|---|---|---|---|---|---|---|"]
    for r in [x for x in rows if x["kind"] == "power"]:
        arms = ("A", "B", "D")
        wins = " | ".join(f"{r['win_' + a]:.1%}" if f"win_{a}" in r else "-" for a in arms)
        md = "-" if r["median_day"] is None else f"{r['median_day']:.1f}"
        rp = "/".join(f"{r['ramped'][a]:.0%}" if a in r["ramped"] else "-" for a in arms)
        dp = "/".join(f"{r['dropped'][a]:.0%}" if a in r["dropped"] else "-" for a in arms)
        lines.append(f"| {r['label']} | {r['runs']} | {r['any']:.1%} | {wins} | {md} | {rp} | {dp} |")
    null = next(r for r in rows if r["label"].startswith("null (all"))
    lines += ["", f"False-winner rate under the null (any arm, any scope): **{null['any']:.1%}** (target <= 5%). A null scenario "
              "has 4 experiments (2 workflows x 2 dims), so the chance of at least one false promotion is about 4x this. The "
              "unstratified drift row is the comparison: at a +-5% drift the two false-winner rates are indistinguishable (the drift averages out over day-end looks and the shares move little before a winner is declared), so stratification costs no size or power here and only guards the contrast against confounding when shares and the base rate move together.", "",
              "## Decaying arm (true lift +15% falling to -10% over 2 days from a random onset in days 1-6)", "",
              "Columns: promotion = stage 1 live; stage 2 = 90/10 live; rolled back = A removed again within 16 days; "
              "exposure-days = sum over ticks after onset of A's live share x tick length, over runs where A was promoted; "
              "A's share at onset+2 d = mean live share at that moment (all runs; target <= 0.1); rollback ticks = median "
              "ticks (4 h) from stage-1 live to the rollback being applied.", "",
              "| scenario | runs | promotion (stage 1) | stage 2 | rolled back | exposure-days | A share at onset+2 d | share <= 0.1 at onset+2 d | median ticks stage 1 -> rolled back |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in [x for x in rows if x["kind"] == "decay"]:
        f = lambda v, p=False: "-" if v is None else (f"{v:.1%}" if p else f"{v:.2f}")  # noqa: E731
        lines.append(f"| {r['label']} | {r['runs']} | {r['stage1']:.1%} | {r['stage2']:.1%} | {r['rolled_back']:.1%} | "
                     f"{f(r['exposure'])} | {f(r['share_onset2'])} | {f(r['share_onset2_le_01'], True)} | "
                     f"{'-' if r['median_rb_ticks'] is None else f'{r['median_rb_ticks']:.0f}'} |")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--power-runs", type=int, default=400)
    ap.add_argument("--decay-runs", type=int, default=300)
    ap.add_argument("--out", default="reports/mc_null.md")
    ns = ap.parse_args()
    rows = []
    with ProcessPoolExecutor() as pool:
        for i, (key, sc, kind) in enumerate(scenarios()):
            n = ns.runs if key == "null" else (ns.decay_runs if kind == "decay" else ns.power_runs)
            row = run(sc, n, 10_000 * (i + 1), pool)
            row["kind"] = kind
            rows.append(row)
            print(row, flush=True)
    lines = render(rows)
    Path(ns.out).parent.mkdir(parents=True, exist_ok=True)
    Path(ns.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
