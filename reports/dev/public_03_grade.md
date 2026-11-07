# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.380 | +11.4% | 0.259 | +6.3% | 0.518 | +3.4% | 27% | 452 (408) |
| arav_kyc_1 | 0.394 | +11.2% | 0.234 | +9.0% | 0.596 | +0.0% | 0% | 378 (370) |
| arav_renewal_0 | 0.442 | +10.0% | 0.280 | +6.1% | 0.558 | +2.8% | 10% | 455 (440) |
| kest_cancel_2 | 0.374 | +10.3% | 0.258 | +6.0% | 0.511 | +2.4% | 17% | 425 (411) |
| kest_kyc_1 | 0.419 | +18.6% | 0.246 | +15.2% | 0.594 | +0.0% | 0% | 418 (339) |
| kest_renewal_0 | 0.440 | +10.1% | 0.279 | +5.9% | 0.558 | +2.8% | 14% | 462 (433) |
| nort_cancel_2 | 0.385 | +13.6% | 0.260 | +7.1% | 0.523 | +4.9% | 33% | 430 (393) |
| nort_kyc_1 | 0.423 | +19.4% | 0.249 | +16.6% | 0.596 | +0.2% | 0% | 410 (323) |
| nort_renewal_0 | 0.438 | +8.3% | 0.280 | +6.2% | 0.557 | +1.6% | 12% | 437 (411) |
| tide_cancel_2 | 0.378 | +11.5% | 0.258 | +5.8% | 0.517 | +3.6% | 28% | 397 (407) |
| tide_kyc_1 | 0.464 | +31.4% | 0.270 | +25.8% | 0.592 | +0.1% | 0% | 468 (356) |
| tide_renewal_0 | 0.438 | +9.3% | 0.280 | +6.2% | 0.553 | +1.9% | 9% | 458 (438) |
| ALL | 0.415 | +13.6% | 0.263 | +9.3% | 0.556 | +2.2% | 11% | 5190 (4729) |

## Changes applied by the controller

70 applied — neutral: 37, beneficial: 33

- arav_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.003 → **neutral**
- arav_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- kest_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- kest_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nort_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.003 → **neutral**
- nort_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- tide_cancel_2 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- tide_renewal_0 v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.004 → **neutral**
- nort_renewal_0 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nort_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- tide_cancel_2 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- arav_renewal_0 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- tide_renewal_0 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.011 → **neutral**
- kest_renewal_0 v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts', 'caller_id', 'schedule'] engageΔ=+0.000 didΔ=+0.012 → **neutral**
- nort_cancel_2 v4 (controller_auto) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- arav_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- kest_cancel_2 v3 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- kest_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- nort_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.022 → **beneficial**
- nort_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.026 → **beneficial**
- tide_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.021 → **beneficial**
- tide_renewal_0 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.027 → **beneficial**
- arav_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- kest_cancel_2 v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- nort_cancel_2 v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- tide_cancel_2 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.014 → **neutral**
- nort_cancel_2 v7 (controller_approved) dims=['voice'] engageΔ=+0.036 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- tide_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.529 → **beneficial**
- kest_cancel_2 v5 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- kest_kyc_1 v3 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- arav_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- kest_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- nort_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.009 → **neutral**
- tide_renewal_0 v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.008 → **neutral**
- tide_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.028 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- arav_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.040 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.004 didΔ=+0.000 → **neutral**
- nort_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.541 → **beneficial**
- arav_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v6 (controller_auto) dims=['welcome_message'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- tide_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.000 → **neutral**
- kest_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.628 → **beneficial**
- arav_kyc_1 v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.344 → **beneficial**
- kest_cancel_2 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.041 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v9 (controller_approved) dims=['voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v8 (controller_approved) dims=['voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.005 → **neutral**
- arav_cancel_2 v8 (controller_approved) dims=['voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- kest_kyc_1 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_renewal_0 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.032 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v6 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.001 → **neutral**
- kest_cancel_2 v8 (controller_approved) dims=['voice'] engageΔ=+0.033 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v8 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- arav_kyc_1 v4 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.061 → **beneficial**
- kest_kyc_1 v6 (controller_approved) dims=['schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_kyc_1 v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.081 → **beneficial**
- tide_renewal_0 v7 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.015 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 48,
  "decisions": {
    "rejected": 5,
    "approved": 43,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 5
  },
  "applied_via_approval": 42,
  "auto_applied": 28,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "arav_cancel_2": 1,
    "arav_renewal_0": 1,
    "kest_cancel_2": 1,
    "kest_renewal_0": 1,
    "nort_cancel_2": 2,
    "nort_renewal_0": 1,
    "tide_cancel_2": 1,
    "tide_renewal_0": 1,
    "arav_kyc_1": 1,
    "kest_kyc_1": 2,
    "nort_kyc_1": 2,
    "tide_kyc_1": 1
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
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.199,
    "4": 0.199
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.547,
    "4": 0.544
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.314,
    "4": 0.341
  },
  "tide_renewal_0:welcome_message=W3": {
    "3": 0.18,
    "4": 0.223
  }
}
```

Credits used: 100 · validation runs: 41 · LLM fix calls: 0