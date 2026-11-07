# Robustness evaluation (developer runs)

Final code, public scenarios plus seed variants of them (same truth, different random draws — created only for
testing; the controller never reads scenario files). `c`=conclusion rate, `p`=pickup, `e`=engagement, Δ vs the
no-op baseline of the same seed; harmful/beneficial/neutral = the grader's hidden-truth verdict per applied change.

```
public_01_dev          ALL c+13.4% p+8.2% e+3.4% | harmful=0 benef=10 neutral=14 mttr=[] credits=0 | ks_cancel:c+15/p+6/e+6.9 nw_kyc:c+18/p+16/e-0.2 nw_renewal:c+9/p+6/e+1.2
public_02_incidents    ALL c+26.8% p+23.9% e+0.1% | harmful=0 benef=11 neutral=20 mttr=[180] credits=6 | ks_cancel:c+58/p+39/e+7.3 nw_kyc:c+15/p+19/e-3.9 nw_renewal:c+11/p+12/e-2.3
dev01_s102             ALL c+14.6% p+10.5% e+2.6% | harmful=0 benef=10 neutral=16 mttr=[] credits=0 | ks_cancel:c+14/p+11/e+2.9 nw_kyc:c+18/p+13/e+1.3 nw_renewal:c+13/p+8/e+2.8
dev01_s103             ALL c+9.2% p+7.2% e+1.1% | harmful=0 benef=12 neutral=10 mttr=[] credits=0 | ks_cancel:c+11/p+6/e+3.7 nw_kyc:c+11/p+10/e-0.1 nw_renewal:c+6/p+7/e-1.5
dev02_s203             ALL c+27.3% p+22.8% e+1.9% | harmful=0 benef=9 neutral=15 mttr=[270] credits=6 | ks_cancel:c+53/p+39/e+6.4 nw_kyc:c+22/p+23/e-1.7 nw_renewal:c+11/p+9/e+1.2
dev02_s204             ALL c+28.7% p+23.8% e+2.2% | harmful=1 benef=10 neutral=14 mttr=[165] credits=6 | ks_cancel:c+57/p+39/e+9.5 nw_kyc:c+25/p+22/e+0.8 nw_renewal:c+10/p+11/e-1.5
```
