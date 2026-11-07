# Grade report — renamed_ids

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| kf_reconfirm | 0.382 | +10.7% | 0.264 | +9.7% | 0.503 | -0.5% | 10% | 58 (49) |
| nl_docs | 0.388 | +9.3% | 0.257 | +6.9% | 0.587 | +0.6% | 0% | 32 (23) |
| nl_persist | 0.451 | +9.1% | 0.278 | +6.6% | 0.549 | +0.9% | 8% | 49 (57) |
| ALL | 0.406 | +9.8% | 0.268 | +8.1% | 0.535 | +0.1% | 7% | 139 (129) |

## Changes applied by the controller

15 applied — neutral: 9, beneficial: 6

- kf_reconfirm v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nl_persist v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nl_persist v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kf_reconfirm v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nl_docs v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.010 → **neutral**
- nl_persist v4 (controller_auto) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kf_reconfirm v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.030 → **beneficial**
- kf_reconfirm v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.019 → **neutral**
- nl_persist v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.047 → **beneficial**
- nl_persist v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- kf_reconfirm v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kf_reconfirm v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- kf_reconfirm v8 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nl_docs v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.185 → **beneficial**
- nl_persist v7 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 9,
  "decisions": {
    "approved": 9,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 9,
  "auto_applied": 6,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nl_docs": 1,
    "nl_persist": 1,
    "kf_reconfirm": 1
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
  "nl_persist:welcome_message=WM3": {
    "3": 0.207,
    "4": 0.188,
    "5": 0.199,
    "6": 0.198,
    "7": 0.2
  }
}
```

Credits used: 30 · validation runs: 12 · LLM fix calls: 0