# Grade report — policy_strict

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.369 | +3.3% | 0.252 | +1.5% | 0.513 | +1.0% | 10% | 64 (54) |
| nw_kyc | 0.372 | +0.8% | 0.246 | +1.9% | 0.592 | -1.5% | 0% | 23 (20) |
| nw_renewal | 0.450 | +4.5% | 0.274 | +4.0% | 0.556 | -0.4% | 3% | 43 (37) |
| ALL | 0.396 | +3.2% | 0.258 | +2.5% | 0.543 | +0.0% | 5% | 130 (111) |

## Changes applied by the controller

9 applied — neutral: 4, beneficial: 5

- nw_kyc v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nw_renewal v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.012 didΔ=+0.004 → **beneficial**
- ks_cancel v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.001 didΔ=+0.004 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.004 didΔ=+0.038 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.051 → **beneficial**
- nw_kyc v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.230 → **beneficial**
- nw_renewal v4 (controller_approved) dims=['voice'] engageΔ=+0.001 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 16,
  "decisions": {
    "approved": 10,
    "rejected": 6,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 6
  },
  "applied_via_approval": 9,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1,
    "nw_kyc": 1,
    "nw_renewal": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.105,
    "4": 0.105,
    "5": 0.098,
    "6": 0.105,
    "7": 0.106
  }
}
```

Credits used: 30 · validation runs: 12 · LLM fix calls: 0