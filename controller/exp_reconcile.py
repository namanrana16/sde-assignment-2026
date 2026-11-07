"""Self-healing: the LIVE config is the truth, experiment state must never silently diverge from it (C2).

An experiment change can fail to land without the experiment learning it (withheld at apply time, proposal STALE /
SUPERSEDED / SUBMIT_FAILED, an approver judgement call). While a change is in flight ``state['desired']`` is set and
re-filed each bundle; once there is none, the intended split (control + ``shares``) must equal the live allocation.
If it does not and the dim was not edited by a supervisor (that is detected earlier, by ``_watch``, and interrupts),
our change never landed: re-raise it so it is re-filed in the next daily bundle with its evidence.
"""
from __future__ import annotations

from typing import Mapping

from . import exp_alloc, exp_proposals as prop, exp_state as xs

EPS = 1e-9


def _restored_from_live(st: Mapping, live: Mapping[str, float]) -> dict:
    """The state a vetoed re-raise falls back to: every arm that is live at a share is active at that share."""
    shares = {a: round(w, 6) for a, w in live.items() if a != st["control"] and w > EPS}
    arms = {a: dict(v) for a, v in st["arms"].items()}
    for a in shares:
        arms[a] = {"state": "active"}
    return {"shares": shares, "arms": arms, "ramped": list(st["ramped"]), "untested": list(st.get("untested", [])),
            "family": sorted(set(st["family"]) | set(shares)), "phase": st["phase"]}


def lost_change(st: Mapping, dim: str, live: Mapping[str, float]) -> dict | None:
    """The desired change that restores the intended split, or None when live already matches (or nothing is intended)."""
    if st["phase"] != xs.RUNNING or st["desired"]:
        return None
    weights = exp_alloc.weights(st["control"], st["shares"]) if st["shares"] else {st["control"]: 1.0}
    if xs.same_dist(live, weights):
        return None
    arms = st["arms"]
    drop = sorted(a for a, v in arms.items() if v["state"] == "dropped" and live.get(a, 0.0) > xs.TOL)
    harm = sorted(a for a, v in arms.items() if v["state"] == "harm_stopped" and live.get(a, 0.0) > xs.TOL)
    ramp = sorted(a for a, w in st["shares"].items() if w > live.get(a, 0.0) + xs.TOL)
    summary = (f"{dim} experiment: restore the intended split {prop.weights_text(weights)}; the earlier change "
               f"(drop {drop or 'none'}, harm-stop {harm or 'none'}, ramp {ramp or 'none'}) is not live")
    return {"weights": weights, "ramp": ramp, "drop": drop, "harm": harm, "severe": bool(harm), "summary": summary[:300],
            "rationale": summary + ". The live config is the truth: re-filing the decision the look already took.",
            "prev": _restored_from_live(st, live)}
