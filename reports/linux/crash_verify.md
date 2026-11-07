# Crash-recovery test

## Invariants

- scenario `public_02_incidents` finished: **True** at 2027-01-14T00:00:00+05:30
- controller restarts recorded in our audit log: 4; governance intents reconciled on restart: 0
- changes applied twice: **0** 
- approvals known to the simulator but missing from our store: **0** 
- approvals whose status disagrees: **0** 
- call events in the simulator's chunks: 245008; dials in our aggregates: 245008 → **exactly once**

## Same decisions as an uninterrupted run?

- controller-made config versions: crashed run 31, uninterrupted run 31 → **identical (version, actor, dims, minute)**

**RESULT: PASS**
