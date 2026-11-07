# Crash-recovery test

## Kills

```
# kill log
kill 1: SIGKILL after 7.1s wall, world at 2027-01-05T13:30:00+05:30
kill 2: SIGKILL after 2.94s wall, world at 2027-01-06T05:45:00+05:30
kill 3: SIGKILL after 2.48s wall, world at 2027-01-06T14:45:00+05:30
kill 4: SIGKILL after 4.07s wall, world at 2027-01-07T12:00:00+05:30
kill 5: SIGKILL after 8.43s wall, world at 2027-01-09T12:00:00+05:30
injected crash right after a successful 'auto' call (exit 137), world at 2027-01-10T10:00:00+05:30
injected crash right after a successful 'submit' call (exit 137), world at 2027-01-11T18:00:00+05:30
injected crash right after a successful 'apply' call (exit 137), world at 2027-01-11T19:15:00+05:30
final run exit code: 0
```

## Invariants

- scenario `public_02_incidents` finished: **True** at 2027-01-14T00:00:00+05:30
- controller restarts recorded in our audit log: 8; governance intents reconciled on restart: 3
- changes applied twice: **0** 
- approvals known to the simulator but missing from our store: **0** 
- approvals whose status disagrees: **0** 
- call events in the simulator's chunks: 245008; dials in our aggregates: 245008 → **exactly once**

## Same decisions as an uninterrupted run?

- controller-made config versions: crashed run 31, uninterrupted run 31 → **identical (version, actor, dims, minute)**

**RESULT: PASS**
