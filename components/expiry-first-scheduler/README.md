# Expiry-First Free-Quota Scheduler

**This is NOT another generic failover router.** It is a thin, expiry-first
scheduling layer that sits **in front of** a router and hands it an ordered
candidate list. It does not probe endpoints, does not do failover itself,
and has no circuit breaker — that is the router's job.

## Why this exists

Generic failover routers already exist and are good at the router's job:

- **gb99xbear/llm-failover-evo** (2026-09-17, MIT) — pools endpoints, health
  checks, auto failover, circuit breaker, 4 strategies.
- **Pr0fess0rOP/free-llm-router v0.6.0** — 26 providers, auto failover,
  circuit breaker, half-open probe, monthly budgets.
- **piyush-tyagi-13/llm-keypool** — free key pool, rotation, 429 cooldown,
  proxy, audit.
- **LiteLLM Router** — the industry-standard router.

What none of them has is **expiry-first ordering and daily free-budget
awareness**: llm-keypool only counts "today's request numbers" — no budget
blocking and no expiry sorting; free-llm-router has only monthly budgets.
So a free key can sit unused until it lapses while a long-lived key burns
down, and a provider whose daily free allowance is spent keeps being
offered to callers.

This component supplies exactly the missing piece, as pure local logic:
order candidates soonest-expiring first, and block a provider for the rest
of the day once its daily free budget is spent (or it is marked dead, or
its expiry has passed). Feed its output into any router above; the router
keeps doing probing, failover, and breaker work.

## Files

- **`schedule.py`** — the module + CLI. Python standard library only,
  zero network.
- **`examples/providers.example.json`** — example provider config
  (fictional placeholders only).
- **`tests/test_schedule.py`** — `unittest` suite, stdlib only, temp files
  only, no network.

## Config schema

`examples/providers.example.json`:

```json
{
  "providers": [
    {
      "name": "example-free-early",
      "model": "example-model-a",
      "endpoint": "https://api.example.invalid/v1",
      "expiry": "2026-12-05",
      "daily_free_budget": {"amount": 20, "unit": "requests/day"},
      "notes": "Fictional example: small budget, expires soonest."
    }
  ]
}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Unique provider name, used in output and the state file. |
| `model` | no* | Model name, for reference. |
| `endpoint` | no* | Endpoint, **reference only — never called** by this code. |
| `expiry` | yes | `YYYY-MM-DD`. A provider expiring before today is excluded; a provider expiring today is still usable. |
| `daily_free_budget` | yes | `{"amount": int >= 0, "unit": str}`, e.g. `{"amount": 20, "unit": "requests/day"}`. A bare integer is accepted as shorthand for `{"amount": <int>, "unit": "requests/day"}`. Once today's counted uses reach `amount`, the provider is excluded until tomorrow. |
| `notes` | no | Free-form note. |

\* `model` / `endpoint` are standard fields in example files but default to
`""` if absent; only `name`, `expiry`, and `daily_free_budget` are strictly
required. Bad config (missing file, invalid JSON, missing field, bad date,
negative budget, duplicate name) raises a clear error; the CLI exits 2.

No credentials appear anywhere here. Provider records name a model and an
endpoint for reference only; keys, if any, live with the router / caller,
never in this config or state file.

## State file format

The state file is the shared, per-provider usage record (default
`./scheduler-state.json`, configurable with `--state`):

```json
{
  "example-free-early": {
    "today_used": 3,
    "last_used_date": "2026-11-25",
    "dead": false,
    "dead_since": null
  }
}
```

| Field | Meaning |
|---|---|
| `today_used` | Uses counted on `last_used_date`. |
| `last_used_date` | `YYYY-MM-DD` the count belongs to. A stale date (not today) counts as 0 — yesterday's usage never blocks today. |
| `dead` | `true` while the provider is marked dead (e.g. the router's probe failed). |
| `dead_since` | `YYYY-MM-DD` the provider was marked dead, or `null`. |

Reads tolerate a missing or corrupt file: they start fresh with a warning
on stderr and never crash. Writes are atomic — a temp file in the same
directory is written, then renamed over the state file — so a crash
mid-write cannot leave a half-written state file.

## How scheduling works

`schedule(providers, state, today)` returns:

```json
{
  "usable": ["example-free-early", "example-free-mid"],
  "degraded": [{"name": "example-free-expired", "reason": "expired"}]
}
```

- **Usable** — provider names sorted by **expiry ascending**
  (soonest-expiring first); ties broken by name, so the order is
  deterministic.
- **Degraded** — every excluded provider with one reason:
  - `expired` — `expiry < today`.
  - `dead` — marked dead in the state file.
  - `over_budget` — today's counted uses `>= daily_free_budget.amount`.
  - When several apply, the reason priority is expired, then dead, then
    over_budget.

After a call, report the outcome so the next chooser sees it:

- `record_use(provider, n=1)` — count `n` uses. If the last recorded use
  was on an earlier day, the count resets to `n` (date rollover).
- `report(provider, outcome)` — `outcome` is one of:
  - `used` — alias for `record_use(provider, 1)`.
  - `quota_exceeded` — sets `today_used` to the budget amount, so the
    provider is out for the rest of today.
  - `dead` — sets `dead=true`, `dead_since=today`.
  - `alive` — clears `dead` (recovery; the provider returns to the order
    on the next `schedule()` call).

"Today" is injectable — pass a `date`, a `"YYYY-MM-DD"` string, or a
callable returning a date to `Scheduler(..., today=...)` / `today_fn=...`,
or per call — so tests (and deterministic runs) control the date instead
of depending on the real local date. The default is the real local date.

## CLI usage

```bash
# Ordered usable providers as JSON + degraded list with reasons
python3 schedule.py --providers examples/providers.example.json --state state.json order
# {"usable": ["example-free-early", "example-free-mid", "example-free-late"],
#  "degraded": [{"name": "example-free-expired", "reason": "expired"}]}

# Record one use of a provider (or --n N uses)
python3 schedule.py --providers examples/providers.example.json --state state.json use --provider example-free-early
python3 schedule.py --providers examples/providers.example.json --state state.json use --provider example-free-early --n 3

# Report an outcome
python3 schedule.py --providers examples/providers.example.json --state state.json report --provider example-free-mid --outcome dead
python3 schedule.py --providers examples/providers.example.json --state state.json report --provider example-free-mid --outcome alive
python3 schedule.py --providers examples/providers.example.json --state state.json report --provider example-free-mid --outcome quota_exceeded

# Pin "today" for a deterministic run
python3 schedule.py --providers examples/providers.example.json --state state.json --today 2026-11-25 order

# Built-in demo (fictional providers, temp state file, no --providers needed)
python3 schedule.py demo
```

Exit code `0` on success, `2` on usage / config errors, with a clear
message on stderr (unknown provider, bad outcome, missing file, bad JSON,
bad date, negative budget, duplicate name).

## How multi-agent sharing works

The state file is the multi-agent shared record. The contract is simple:

1. **Read before choosing** — every agent/process creates its `Scheduler`
   on the same state-file path (or calls `reload()` to re-read it) and
   runs `schedule()` before picking a provider.
2. **Write back usage** — after a call, the agent reports the outcome
   (`record_use` / `report`), which saves the state file immediately.
3. **One file, one timezone** — expiry and budget resets use the local
   date. Agents sharing a state file should agree on the machine / timezone
   that owns it, or they will disagree about "today".

Because `schedule()` is pure (no I/O, no network), a router can also call
the module-level `schedule(providers, state, today)` directly on the
provider list and the state dict it loaded itself, and pass the returned
`usable` order to its own failover loop.

## Running the demo and the tests

```bash
# Demo: fictional providers; expiry sort, budget exhaustion dropping a
# provider out, a dead provider excluded, recovery after mark-alive.
python3 schedule.py demo

# Tests (unittest, stdlib only, temp files only, no network)
python3 -m unittest discover -s tests -v
```

## Limitations

- **Not a router.** No endpoint probing, no failover, no circuit breaker,
  no retries, no request proxying. It orders providers and tracks
  expiry / budget / dead state; the router (or calling agent) makes the
  actual call and picks the next provider in the list on failure.
- **Budget counts requests, not tokens or cost.** A "use" is one
  `record_use` / `report(..., "used")` call. Token-weighted budgets are
  out of scope.
- **State file is not locked.** Two processes writing the same state file
  at the same instant can race; the atomic rename prevents a torn file,
  but the last writer wins. For agents taking turns (or one writer) this
  is fine; it is not a distributed store. If you need multi-writer safety,
  add file locking in front of it or give each writer its own state file.
- **Local date only.** Expiry and budget resets use the machine's local
  date; see the sharing note above.
- **Dead is a report, not a probe.** This layer never checks whether a
  provider is actually alive; it only records what the router / caller
  reports. A provider stays `dead` until someone reports it `alive`.
