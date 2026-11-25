# Ladder Auto-Wire

Wires a newly verified free resource into a model ladder automatically:
validate the record, merge it into the ladder, sort by expiry
(soonest-expiring active provider first), and auto-degrade expired
entries. See `SKILL.md` for the full skill document.

## Files

- `SKILL.md` — the skill: when to use it, the record schema, what
  counts as verification evidence, validation rules, merging,
  sorting, degraded status, and dry-run mode.
- `wire.py` — the script (Python standard library only, no network).
- `examples/resource.example.json` — a sample verified-resource
  record (fictional placeholder values).
- `examples/ladder.example.json` — a sample ladder with three
  fictional providers, one already expired and degraded.
- `tests/test_wire.py` — unit tests (standard library only, temp
  files only, no network).

## Record schema

```json
{
  "name": "Example Free Models",
  "model": "example-model-free",
  "free_quota": {"amount": 50, "unit": "requests/day"},
  "expiry": "2027-06-30",
  "evidence": {
    "type": "live_test",
    "detail": "Called the list-models endpoint and got HTTP 200.",
    "at": "2026-11-20"
  },
  "notes": "Optional."
}
```

`free_quota` may also be a plain string such as `"50 requests/day"`;
it is normalized to the object form. `evidence.type` must be one of
`live_test`, `official_page`, `email`, `other`, and `detail` must say
what was actually verified (which page and what it states, or what
was called and what came back). A directory listing or a bare
marketing claim is not acceptable evidence.

## How to run it

```bash
# Add a verified resource (creates the ladder file if it does not exist)
python3 wire.py --resource examples/resource.example.json --ladder ladder.json

# Dry run: print what would change, write nothing
python3 wire.py --resource examples/resource.example.json --ladder ladder.json --dry-run

# Pin the reference date (default: today's local date).
# Useful for deterministic tests and replays.
python3 wire.py --resource resource.json --ladder ladder.json --today 2026-11-25
```

Merging: a resource whose `name` matches an existing provider
updates that entry in place instead of duplicating it. Sorting:
active providers first by expiry ascending, degraded providers
after, also by expiry ascending. Any provider whose expiry is before
the reference date gets `status: "degraded"` (it stays in the ladder,
it is just no longer treated as a usable free route). Exit 0 on
success, 2 on validation errors (the error lists every problem, and
the ladder is left untouched).

## How to run the tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary files only and touch no network.
