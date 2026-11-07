# Robustness evaluation (developer runs)

Final code, public scenarios plus seed variants of them (same truth, different random draws — created only for
testing; the controller never reads scenario files). `c`=conclusion rate, `p`=pickup, `e`=engagement, Δ vs the
no-op baseline of the same seed; harmful/beneficial/neutral = the grader's hidden-truth verdict per applied change.

```
public_01_dev          ALL c+13.4% p+8.2% e+3.4% | harmful=0 benef=10 neutral=12 mttr=[] credits=0 | ks_cancel:c+15/p+6/e+6.9 nw_kyc:c+18/p+16/e-0.2 nw_renewal:c+9/p+6/e+1.2
public_02_incidents    ALL c+28.0% p+24.2% e+0.7% | harmful=0 benef=11 neutral=27 mttr=[180] credits=6 | ks_cancel:c+59/p+41/e+6.9 nw_kyc:c+17/p+20/e-3.1 nw_renewal:c+11/p+11/e-0.8
dev01_s102             ALL c+14.3% p+10.4% e+2.5% | harmful=0 benef=9 neutral=16 mttr=[] credits=0 | ks_cancel:c+13/p+10/e+2.9 nw_kyc:c+19/p+14/e+1.5 nw_renewal:c+13/p+9/e+2.3
dev01_s103             ALL c+10.8% p+8.2% e+1.5% | harmful=0 benef=13 neutral=8 mttr=[] credits=0 | ks_cancel:c+12/p+7/e+3.8 nw_kyc:c+12/p+11/e+0.3 nw_renewal:c+9/p+9/e-0.7
dev02_s203             ALL c+28.2% p+22.9% e+2.4% | harmful=0 benef=9 neutral=16 mttr=[270] credits=6 | ks_cancel:c+56/p+40/e+7.2 nw_kyc:c+24/p+22/e-0.4 nw_renewal:c+10/p+8/e+1.2
dev02_s204             ALL c+27.0% p+22.7% e+1.8% | harmful=1 benef=10 neutral=11 mttr=[180] credits=6 | ks_cancel:c+57/p+39/e+9.4 nw_kyc:c+21/p+19/e+0.3 nw_renewal:c+9/p+10/e-2.0
```
