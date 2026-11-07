"""The experiment decision rule (C6), pure and deterministic: used by the controller AND by scripts/mc_null.py.

One *scope* is one population analysed on its own (the whole workflow, or a pre-registered analysis group). For a
scope the rule has four parts:

* harm check (every 4 h):  z <= -3 with >= 150 connects        -> remove now; z <= -2.5 folds into the daily look;
* ramp / drop (daily):      point >= control after >=1 day and >=100 connects -> ramp;  whole-experiment z <= -1.5
                            (<300 connects) or <= -1 (>=300 connects) -> drop, otherwise defer; an arm already ramped
                            is also dropped at last-24h z <= -1.5 (in-experiment decay check);
* winner (daily, <= 4 looks): one-sided z against control past a Lan-DeMets O'Brien-Fleming boundary at the arm's own
                            information fraction, Holm across the dim's arms, AND point lift >= 3%, AND recent-window
                            lift lower bound > 0, AND pickup non-inferiority within ``max_drop``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist
from typing import Mapping, Sequence

from . import exp_stats as es
from . import settings

_N = NormalDist()


@dataclass(frozen=True)
class ArmData:
    engaged: es.Rows  # {lid: (connects, engaged)} since the experiment started
    recent: es.Rows  # same, last EXP_RECENT_MINUTES only
    pickup: es.Rows  # {lid: (dials, connects)} since the experiment started
    engaged_s: Mapping[str, es.Rows] | None = None  # the same split by stratum (share regime x day), when known
    recent_s: Mapping[str, es.Rows] | None = None


EMPTY = ArmData({}, {}, {})


def pair_contrast(d: ArmData, ctl: ArmData, recent: bool = False) -> es.Contrast | None:
    """Arm vs control: Mantel-Haenszel pooled over strata when both sides carry them, else the plain contrast."""
    a_s, c_s = (d.recent_s, ctl.recent_s) if recent else (d.engaged_s, ctl.engaged_s)
    if a_s is not None and c_s is not None:
        keys = sorted(set(a_s) | set(c_s))
        return es.contrast_strata([(a_s.get(k, {}), c_s.get(k, {})) for k in keys])
    return es.contrast(d.recent, ctl.recent) if recent else es.contrast(d.engaged, ctl.engaged)


def drop_holds(s: Mapping, is_ramped: bool) -> bool:
    """Drop predicate of the decision look: whole-experiment z at the drop bound; an arm already carrying the ramp
    share (20%: keeping it costs ~3x a 7.5% entry arm) is also dropped when its last-24h z is <= EXP_RAMPED_DROP_RECENT_Z
    (the in-experiment decay check)."""
    if not s.get("ok"):
        return False
    recent_bad = (is_ramped and s["recent_n"] >= settings.EXP_RAMP_MIN_CONNECTS
                  and s.get("recent_z", 0.0) <= settings.EXP_RAMPED_DROP_RECENT_Z)
    return s["z"] <= drop_threshold(s["n_arm"]) or recent_bad


def drop_threshold(n_arm: float) -> float:
    """Whole-experiment drop bound: a stricter z below EXP_DROP_BIG_N connects (a 7.5% arm costs <= 0.075 |effect|)."""
    return settings.EXP_DROP_Z if n_arm >= settings.EXP_DROP_BIG_N else settings.EXP_DROP_Z_SMALL


def design_information() -> float:
    """Information (1/se^2 of the log ratio) at which the design MDE has the design power."""
    za = _N.inv_cdf(1 - settings.EXP_DESIGN_ALPHA)
    zb = _N.inv_cdf(settings.EXP_DESIGN_POWER)
    return (za + zb) ** 2 / math.log(1 + settings.EXP_DESIGN_MDE) ** 2


def summarise(data: Mapping[str, ArmData], control: str, arms: Sequence[str]) -> dict[str, dict]:
    """Cumulative and recent contrasts of every arm against control (records are plain, JSON-able dicts)."""
    ctl = data.get(control, EMPTY)
    out: dict[str, dict] = {}
    for arm in arms:
        d = data.get(arm, EMPTY)
        c = pair_contrast(d, ctl)
        r = pair_contrast(d, ctl, recent=True)
        rec = {"n_arm": es.totals(d.engaged)[0], "n_ctrl": es.totals(ctl.engaged)[0], "ok": c is not None}
        if c is not None:
            rec.update(rate_arm=c.rate_a, rate_ctrl=c.rate_c, lift=c.lift, z=c.z, se=c.se, info=c.info,
                       clusters=c.clusters)
        rec["recent_n"] = es.totals(d.recent)[0]
        if r is not None:
            rec.update(recent_lift=r.lift, recent_z=r.z, recent_lb=r.lift_lower(settings.EXP_RECENT_Z))
        out[arm] = rec
    return out


def harm_stops(summary: Mapping[str, dict]) -> dict[str, str]:
    """Arms to remove now (severe) or in the next daily bundle (fold), by arm."""
    out: dict[str, str] = {}
    for arm, s in summary.items():
        if not s["ok"] or s["n_arm"] < settings.EXP_HARM_MIN_CONNECTS:
            continue
        if s["z"] <= settings.EXP_HARM_Z:
            out[arm] = "now"
        elif s["z"] <= settings.EXP_HARM_Z / 1.2:  # -2.5
            out[arm] = "fold"
    return out


def pickup_guard(data: Mapping[str, ArmData], control: str, arm: str, max_drop: float) -> tuple[bool, dict]:
    c = es.contrast(data.get(arm, EMPTY).pickup, data.get(control, EMPTY).pickup, scale="diff")
    if c is None:
        return False, {"status": "no_data"}
    upper_drop = -c.effect + settings.EXP_PICKUP_Z * c.se
    return upper_drop <= max_drop, {"pickup_arm": c.rate_a, "pickup_ctrl": c.rate_c,
                                    "drop_upper": upper_drop, "margin": max_drop, "dials_arm": c.n_a}


def ramp_blocked(s: Mapping) -> bool:
    """A bad recent window (last 24 h z <= EXP_DROP_Z on >= EXP_RECENT_MIN_CONNECTS) blocks a ramp at the look and at apply.

    The window needs only EXP_RECENT_MIN_CONNECTS: a 7.5% arm sees ~85 connects a day, below the 100 a ramp needs
    cumulatively, so a threshold of 100 never let a decaying entry arm's recent window count (C5)."""
    return bool(s.get("ok")) and s["recent_n"] >= settings.EXP_RECENT_MIN_CONNECTS and s.get("recent_z", 0.0) <= settings.EXP_DROP_Z


def ramp_vetoed(s: Mapping) -> bool:
    """Apply-time predicate for a ramp: cumulative z at the drop bound, or a bad recent window."""
    if not s.get("ok"):
        return False
    return s["z"] <= drop_threshold(s["n_arm"]) or ramp_blocked(s)


def drop_vetoed(s: Mapping, is_ramped: bool = False) -> bool:
    """Apply-time predicate for a drop: vetoed only when the arm has clearly RECOVERED (hysteresis).

    Re-running ``drop_holds`` at the same bound hours later flips on noise at the bound, and a drop only moves traffic
    back to control, so the apply-time bar is looser: the cumulative z must be above the drop bound by
    EXP_DROP_VETO_Z_MARGIN, and (for a ramped arm) the last-24h z above EXP_DROP_VETO_RECOVERED_Z."""
    if not s.get("ok"):
        return False
    cum_holds = s["z"] <= drop_threshold(s["n_arm"]) + settings.EXP_DROP_VETO_Z_MARGIN
    recent_holds = (is_ramped and s["recent_n"] >= settings.EXP_RAMP_MIN_CONNECTS
                    and s.get("recent_z", 0.0) <= settings.EXP_DROP_VETO_RECOVERED_Z)
    return not (cum_holds or recent_holds)


def revert_expected_change(shares: Mapping[str, float], rates: Mapping[str, float], control: str, arm: str) -> float:
    """Expected relative engagement change of moving ``arm``'s share back to control, on OBSERVED arm rates."""
    before = sum(s * rates.get(a, rates[control]) for a, s in shares.items()) + (1.0 - sum(shares.values())) * rates[control]
    return shares[arm] * (rates[control] - rates[arm]) / before if before > 0 else 0.0


def end_of_experiment_reverts(summary: Mapping[str, dict], shares: Mapping[str, float], control: str) -> list[str]:
    """After the last look without a winner: arms whose point estimate is below control revert to control, but only
    if that revert is not itself expected-harmful on the observed arm rates (a surviving arm needs point >= 0)."""
    rates = {control: next((s["rate_ctrl"] for s in summary.values() if s.get("ok")), 0.0)}
    for arm, s in summary.items():
        if s.get("ok"):
            rates[arm] = s["rate_arm"]
    out = []
    for arm, s in summary.items():
        if not s.get("ok") or arm not in shares or s["lift"] >= 0 or arm not in rates or not rates[control]:
            continue
        if revert_expected_change(shares, rates, control, arm) >= -settings.EXP_REVERT_MAX_EXPECTED_DROP:
            out.append(arm)
    return sorted(out)


@dataclass
class LookOutcome:
    took_look: bool = False
    look_index: int = 0
    drop: list[str] = field(default_factory=list)
    ramp: list[str] = field(default_factory=list)
    winner: str | None = None
    records: dict[str, dict] = field(default_factory=dict)
    arm_t: dict[str, list[float]] = field(default_factory=dict)


def daily_look(data: Mapping[str, ArmData], control: str, active: Sequence[str], family: int, alpha: float,
               *, arm_t: Mapping[str, Sequence[float]], looks_taken: int, ramped: Sequence[str],
               exposure_days: float, max_drop: float, test_winner: bool = True) -> LookOutcome:
    """One daily look over the ACTIVE challengers of one scope."""
    out = LookOutcome(arm_t={a: list(t) for a, t in arm_t.items()})
    summary = summarise(data, control, active)
    out.records = summary
    imax = design_information()
    for arm, s in summary.items():
        s["t"] = min(1.0, s["info"] / imax) if s["ok"] else 0.0
        if not s["ok"]:
            continue
        enough = exposure_days >= settings.EXP_RAMP_MIN_DAYS and s["n_arm"] >= settings.EXP_RAMP_MIN_CONNECTS
        if enough and drop_holds(s, arm in ramped):
            out.drop.append(arm)
        elif enough and s["lift"] >= 0 and arm not in ramped and not ramp_blocked(s):
            out.ramp.append(arm)
    if not test_winner or looks_taken >= len(settings.EXP_LOOK_FRACTIONS):
        return out
    testable = {a: s for a, s in summary.items() if s["ok"] and a not in out.drop
                and s["n_arm"] >= settings.EXP_MIN_ARM_CONNECTS_FOR_TEST}
    if not testable or max(s["t"] for s in testable.values()) < settings.EXP_LOOK_FRACTIONS[looks_taken]:
        return out
    out.took_look, out.look_index = True, looks_taken + 1
    for arm, s in testable.items():
        out.arm_t[arm] = list(out.arm_t.get(arm, [])) + [s["t"]]
    holm = es.holm_order({a: s["z"] for a, s in testable.items()}, family, alpha,
                         lambda a_adj, arm: es.obf_boundary(out.arm_t[arm], a_adj))
    passing = []
    for arm, h in holm.items():
        s = summary[arm]
        s.update(rank=h["rank"], alpha_adj=h["alpha_adj"], boundary=h["boundary"], rejected=h["rejected"])
        s["point_ok"] = s["lift"] >= settings.EXP_MIN_POINT_LIFT
        s["recent_ok"] = s["recent_n"] >= settings.EXP_RECENT_MIN_CONNECTS and s.get("recent_lb", -1) > 0
        ok_pickup, detail = pickup_guard(data, control, arm, max_drop)
        s["pickup_ok"], s["pickup"] = ok_pickup, detail
        s["winner_ok"] = bool(h["rejected"] and s["point_ok"] and s["recent_ok"] and ok_pickup)
        if s["winner_ok"]:
            passing.append(arm)
    if passing:
        out.winner = max(passing, key=lambda a: (summary[a]["z"], a))
    return out
