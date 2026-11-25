# Test results — agent-team-kit

All tests run by the coordinator (independent re-run, 2026-11-25) with Python 3.12.3, stdlib unittest only. No test uses the network: the scheduler and wire tests use temp files only; the gates video test generates a clip locally with ffmpeg and probes it with ffprobe.

## 1. expiry-first-scheduler — 25 tests

```
.......warning: state file not found: /tmp/tmpm1borocg/scheduler-state.json -- starting fresh
.....warning: state file not found: /tmp/tmp8sibw46j/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpukofnr1k/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpxulvjoek/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpsd5a1g1o/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpn9fg1ii9/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpui4ez3dw/scheduler-state.json -- starting fresh
..warning: state file not found: /tmp/tmpxajurolf/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpz0tyaihf/scheduler-state.json -- starting fresh
.warning: state file not found: /tmp/tmpm8kigz61/scheduler-state.json -- starting fresh
..warning: state file not found: /tmp/tmp2h20wlsd/scheduler-state.json -- starting fresh
..
----------------------------------------------------------------------
Ran 25 tests in 0.788s

OK
```

## 2. enforcement-gates — 19 tests

```
...................
----------------------------------------------------------------------
Ran 19 tests in 2.323s

OK
```

## 3. ladder-auto-wire — 11 tests

```
...........
----------------------------------------------------------------------
Ran 11 tests in 1.017s

OK
```

## Totals

- expiry-first-scheduler: 25 passed, 0 failed
- enforcement-gates: 19 passed, 0 failed
- ladder-auto-wire: 11 passed, 0 failed
- **Total: 55 passed, 0 failed**
