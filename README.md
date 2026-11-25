# agent-team-kit

Three small, dependency-free tools for running a team of AI agents on free-tier
model providers — without the team falling over when one free tier dies.

They were built from scratch for one specific pain: a multi-agent team where
several agents share free model quotas. The tools are generic: no accounts,
no keys, no team-specific names or paths, nothing to sign up for. Python
standard library only, no network calls in the tools themselves.

## The three components

| Component | What it does | Who needs it |
|---|---|---|
| [expiry-first-scheduler](components/expiry-first-scheduler/) | A thin scheduling layer: given a list of providers with expiry dates and daily free budgets, plus a shared state file, it hands back the usable providers soonest-expiring first, and auto-degrades the expired, over-budget, or dead ones. | Any team of agents sharing free tiers, sitting in front of a router. |
| [enforcement-gates](components/enforcement-gates/) | A gate that **blocks submission** — not a scorecard. A deliverable passes three gates (self-check → manager re-check → final review) against a 100-point standard; a veto item or a score below 80 blocks it, and a self/manager score gap over 10 sends it back. | Anyone who has a quality standard that is never actually enforced. |
| [ladder-auto-wire](components/ladder-auto-wire/) | Wires a newly verified free resource into a model ladder automatically: validates the record, merges it into the ladder sorted by expiry, and auto-degrades expired entries. Dry-run mode included. | Anyone whose "ladder" of free models is still updated by hand. |

## Why this exists (and what it is not)

Generic failover routers already exist and are good — see the
expiry-first-scheduler README for the list. This kit does not re-build them.
Each component is deliberately the one layer those projects do not have:

- **Expiry-first scheduling.** Routers do failover, health checks, and
  circuit breakers. None of them orders by expiry date or enforces a daily
  free budget. The scheduler is the layer that goes in front of a router.
- **Enforcement, not scoring.** Self-evaluation skills already exist and
  tell you how good something is. The gates make a low score stop the work
  from going out.
- **Verified-resource intake.** The auto-wire skill is the intake flow
  "verified → into the ladder → expired → degraded", which no key-pool
  skill we found covers.

Each component README states the comparison in detail, with the existing
projects named.

## Requirements

- Python 3.10 or newer. No third-party packages.
- `ffprobe` / `ffmpeg` only for the video checks in enforcement-gates
  (if `ffprobe` is absent, video checks are skipped inside gates 1–2 and
  gate 3 blocks video deliverables, both with a clear note).

## Quickstart

```bash
# 1. Expiry-first scheduler — what can we use right now?
cd components/expiry-first-scheduler
python3 schedule.py --providers examples/providers.example.json --state /tmp/sched-state.json order
python3 schedule.py --providers examples/providers.example.json --state /tmp/sched-state.json demo

# 2. Enforcement gates — does this deliverable pass?
cd ../enforcement-gates
python3 gates.py --standard examples/standard-text.example.json \
  --deliverable examples/sample-article.txt \
  --manager-standard examples/standard-manager.example.json

# 3. Ladder auto-wire — add a verified free resource (dry-run first)
cd ../ladder-auto-wire
python3 wire.py --resource examples/resource.example.json \
  --ladder examples/ladder.example.json --dry-run
```

## Layout

```
agent-team-kit/
├── README.md                  # this file
├── TEST-RESULTS.md            # full test outputs from the independent re-run
└── components/
    ├── expiry-first-scheduler/
    │   ├── README.md          # incl. comparison with existing routers
    │   ├── schedule.py        # module + CLI (stdlib only)
    │   ├── examples/providers.example.json
    │   └── tests/test_schedule.py
    ├── enforcement-gates/
    │   ├── README.md          # incl. comparison with existing self-scoring skills
    │   ├── SKILL.md           # the skill document
    │   ├── gates.py           # gate runner (stdlib only)
    │   ├── examples/          # text + video standards, manager variant, sample deliverables
    │   └── tests/test_gates.py
    └── ladder-auto-wire/
        ├── README.md
        ├── SKILL.md           # the skill document
        ├── wire.py            # CLI (stdlib only)
        ├── examples/          # sample resource record + sample ladder
        └── tests/test_wire.py
```

## Testing

Every component has unit tests (stdlib `unittest`, temp files only, no
network). Run them from the kit root:

```bash
for d in components/*/; do (cd "$d" && python3 -m unittest discover -s tests); done
```

Results of the last independent re-run: **55 passed, 0 failed**
(25 scheduler + 19 gates + 11 wire). Full outputs: [TEST-RESULTS.md](TEST-RESULTS.md).

## Limitations

- Budgets count requests, not tokens or money. If a provider bills by
  tokens, convert to an approximate request budget first.
- The scheduler's shared state file has no locking; it assumes one writer
  at a time (or writers that take turns). Atomic rename prevents torn
  writes.
- Dates use the local date. All agents sharing one state file should agree
  on a timezone.
- The gates score what can be checked deterministically (file, size,
  format, length, keywords, ffprobe facts). They do not judge quality of
  prose or footage — that is what the optional second-model review hook is
  for, and it is advisory only.
- The auto-wire skill trusts the evidence field as written; it validates
  shape and presence, not prose specificity. A record whose only evidence
  is a marketing claim should not be written in the first place (see its
  SKILL.md).

## License

MIT — see [LICENSE](LICENSE).
