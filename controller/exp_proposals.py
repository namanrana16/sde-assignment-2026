"""Human-readable summaries and evidence for experiment changes (the approver and ``why`` read these)."""
from __future__ import annotations

from typing import Mapping

from . import settings


def weights_text(weights: Mapping[str, float]) -> str:
    return ", ".join(f"{o} {w * 100:.1f}%" for o, w in sorted(weights.items(), key=lambda kv: (-kv[1], kv[0])))


def entry(dim: str, control: str, weights: Mapping[str, float], note: str) -> tuple[str, str, float]:
    total = sum(w for o, w in weights.items() if o != control)
    summary = (f"{note}Start {dim} experiment: control {control} keeps {weights[control] * 100:.1f}%, challengers "
               f"{weights_text({o: w for o, w in weights.items() if o != control})} (total {total * 100:.1f}%)")
    rationale = (f"Controlled exploration of {dim}: every challenger enters at {settings.EXP_ENTRY_SHARE:.1%} "
                 f"(total <= {settings.EXP_MAX_CHALLENGER_TOTAL:.1%}), so even a challenger that is 10% worse costs "
                 f"under {total * 0.10:.1%} of expected engagement; harm-stop every 4 h at z <= {settings.EXP_HARM_Z}.")
    return summary[:300], rationale, round(min(1.0, total), 4)  # value-of-information proxy (no estimated lift yet)


def update(dim: str, weights: Mapping[str, float], ramp: list, drop: list, rows: Mapping[str, Mapping],
           note: str = "") -> tuple[str, str]:
    bits = []
    for a in ramp:
        bits.append(f"ramp {a} (lift {rows[a]['lift']:+.1%}, z {rows[a]['z']:.2f}, n {rows[a]['n_arm']:.0f})")
    for a in drop:
        bits.append(f"drop {a} (lift {rows[a]['lift']:+.1%}, z {rows[a]['z']:.2f}, recent z "
                    f"{rows[a].get('recent_z', float('nan')):.2f})")
    text = f"{note}{dim} experiment: {'; '.join(bits)}; new split {weights_text(weights)}"
    return text[:300], text + ". Ramp: after >= 1 day and >= 100 connects, point estimate >= control; drop at z <= -1."


def promotion(dim: str, wf_scope: str, winner: str, control: str, rec: Mapping, weights: Mapping[str, float]) -> tuple[str, str]:
    summary = (f"Promote {winner} for {dim} ({wf_scope}) to stage 1 ({weights_text(weights)}): lift {rec['lift']:+.1%} vs "
               f"{control} (z {rec['z']:.2f} >= O'Brien-Fleming/Holm bound {rec['boundary']:.2f}, "
               f"n {rec['n_arm']:.0f} vs {rec['n_ctrl']:.0f}); {control} keeps {1 - weights[winner]:.0%} as a concurrent control")
    rationale = (f"{summary}. Recent 24h lift lower bound {rec.get('recent_lb', float('nan')):+.1%} > 0 "
                 f"(not decaying); pickup guardrail non-inferior (drop upper bound "
                 f"{rec['pickup']['drop_upper']:+.3f} <= {rec['pickup']['margin']}). Stage 2 ({1 - settings.EXP_HOLDOUT:.0%} / "
                 f"{settings.EXP_HOLDOUT:.0%} holdout) follows only after >= 24 h here with a positive recent lower bound "
                 "and a still-positive concurrent contrast; the decay test runs meanwhile.")
    return summary[:300], rationale


def stage2(dim: str, winner: str, control: str, why: str, weights: Mapping[str, float]) -> tuple[str, str]:
    summary = f"Promote {winner} for {dim} to stage 2 ({weights_text(weights)}): {why}"
    return summary[:300], summary + ". Concurrent holdout stays on the old control for the decay test."


def harm_stop(dim: str, arm: str, rec: Mapping) -> tuple[str, str]:
    text = (f"Remove {dim} challenger {arm} now: engagement {rec['lift']:+.1%} vs control, z {rec['z']:.2f} <= "
            f"{settings.EXP_HARM_Z} over {rec['n_arm']:.0f} connects")
    return text[:300], text + " (risk-reducing harm-stop, 4-hourly check)."


def rollback(dim: str, winner: str, control: str, reason: str) -> tuple[str, str]:
    text = f"Roll back {dim} {winner} to {control}: {reason}"
    return text[:300], text
