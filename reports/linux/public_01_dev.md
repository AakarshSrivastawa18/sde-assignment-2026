# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.394 | +15.3% | 0.260 | +6.3% | 0.526 | +6.9% | 34% | 73 (57) |
| nw_kyc | 0.433 | +18.0% | 0.281 | +16.3% | 0.596 | -0.2% | 7% | 28 (21) |
| nw_renewal | 0.452 | +8.6% | 0.281 | +6.4% | 0.548 | +1.2% | 8% | 58 (54) |
| ALL | 0.421 | +13.4% | 0.271 | +8.2% | 0.548 | +3.4% | 18% | 159 (132) |

## Changes applied by the controller

24 applied — neutral: 14, beneficial: 10

- nw_renewal v2 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=-0.007 → **neutral**
- ks_cancel v2 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.001 didΔ=+0.005 → **neutral**
- ks_cancel v3 (controller_auto) dims=['welcome_message', 'voice', 'caller_id'] engageΔ=+0.001 didΔ=+0.002 → **neutral**
- nw_renewal v3 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'schedule'] engageΔ=+0.021 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['cohorts', 'welcome_message', 'voice', 'caller_id', 'schedule'] engageΔ=+0.015 didΔ=+0.037 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['cohorts', 'schedule'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- ks_cancel v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.035 → **beneficial**
- ks_cancel v6 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.016 → **neutral**
- nw_renewal v4 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.034 → **beneficial**
- nw_renewal v5 (controller_auto) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.017 → **neutral**
- ks_cancel v7 (controller_auto) dims=['voice'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- ks_cancel v8 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- ks_cancel v9 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- ks_cancel v10 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.018 didΔ=+0.000 → **beneficial**
- ks_cancel v11 (controller_auto) dims=['welcome_message', 'voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nw_renewal v6 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.007 didΔ=+0.000 → **neutral**
- ks_cancel v12 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- ks_cancel v13 (controller_auto) dims=['welcome_message'] engageΔ=+0.001 didΔ=+0.000 → **neutral**
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=-0.001 didΔ=+0.000 → **neutral**
- ks_cancel v14 (controller_approved) dims=['cohorts', 'welcome_message', 'schedule'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_kyc v3 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.318 → **beneficial**
- ks_cancel v15 (controller_auto) dims=['welcome_message'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- ks_cancel v16 (controller_auto) dims=['welcome_message'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_kyc v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 9,
  "decisions": {
    "approved": 8,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 8,
  "auto_applied": 16,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 1,
    "nw_kyc": 1,
    "ks_cancel": 2
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
    "3": 0.104,
    "4": 0.105,
    "5": 0.099,
    "6": 0.105,
    "7": 0.101
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0