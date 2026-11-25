# Ladder Auto-Wire

## What this skill is

This skill wires a newly verified free resource into a model ladder
automatically. A model ladder is an ordered list of providers, stored
as JSON, sorted so the soonest-expiring active provider is tried
first. Doing that by hand is slow and error-prone: entries get
duplicated, the order drifts, and expired providers stay listed as
usable long after they have stopped working.

`wire.py` (in this directory) does the whole step in one command:
validate the verified-resource record, merge it into the ladder,
re-sort by expiry, and auto-degrade anything whose expiry has passed.

## When to use it

Use this skill when:

- A free model offer has just been verified as genuinely free and
  should be added to the ladder.
- A provider already on the ladder has new terms (a different model,
  quota, or expiry) and its entry needs updating.
- The ladder needs a clean-up pass: re-sort it and degrade expired
  entries. (Re-run `wire.py` with any valid resource record, or with
  the same record again — merging is idempotent for an unchanged
  provider — and the sort/degrade pass runs every time.)

Do not use it to verify an offer in the first place. Verification
happens before this skill runs; this skill only trusts a record that
already carries evidence of verification (see below).

## The verified-resource record

One JSON object, passed with `--resource`:

```json
{
  "name": "Example Free Models",
  "model": "example-model-free",
  "free_quota": {"amount": 50, "unit": "requests/day"},
  "expiry": "2027-06-30",
  "evidence": {
    "type": "live_test",
    "detail": "Called the list-models endpoint and got HTTP 200; sent one test chat completion and got a reply.",
    "at": "2026-11-20"
  },
  "notes": "Optional free-text note."
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Provider name. This is the merge key: a record whose `name` matches an existing ladder entry updates that entry instead of adding a duplicate. |
| `model` | string | yes | Model identifier. |
| `free_quota` | object or string | yes | Object form: `{"amount": <number>, "unit": "<string>"}`. String form: `"50 requests/day"` — a leading number plus unit text; it is normalized to the object form. |
| `expiry` | string | yes | Expiry date, exactly `YYYY-MM-DD`. |
| `evidence` | object | yes | See below. |
| `notes` | string | no | Optional free-text note. |

A full example lives at `examples/resource.example.json`.

### What counts as acceptable verification evidence

`evidence` has three fields:

- `type` — one of `live_test`, `official_page`, `email`, `other`.
- `detail` — what was actually verified, in plain words. It must be
  non-empty and specific.
- `at` — the date the check was done, `YYYY-MM-DD` (optional, but
  include it whenever you know it).

The four types, in order of strength:

1. **`live_test`** (strongest) — the offer was actually exercised:
   an endpoint was called and answered, a test request succeeded, a
   quota page showed the free balance. The detail should say what
   was called and what came back, e.g. `"called the list-models
   endpoint and got HTTP 200"`.
2. **`official_page`** — the provider's own pricing or terms page
   was read and states the free terms. The detail must say **which
   official page and what it states** — a bare `"official page"` or
   `"pricing page says free"` is not enough. A marketing page claim
   with no live check is not acceptable on its own: the detail must
   quote or paraphrase the specific free terms (amount, unit,
   expiry) found on that page.
3. **`email`** — a confirmation email from the provider states the
   free terms. The detail should say who sent it and what it
   confirmed.
4. **`other`** — anything else that was actually checked. The detail
   must still say what was checked and what it showed.

What is **not** acceptable evidence, and will be rejected or must
never be written into a record:

- A directory listing, a search-result snippet, or a third-party
  roundup as the only evidence — none of them shows the provider
  actually granting the free terms.
- A marketing claim with no live check and no specific page cited.
- An empty or vague detail (`"verified"`, `"looks free"`, `"see
  website"`). If the detail does not let a second person repeat the
  check, it is not evidence.

The script enforces the enforceable part of this rule: `type` must
be one of the four values and `detail` must be non-empty. The
specificity part (which page, what it states, what was called) is
the human's job when writing the record — write the detail as if
someone else has to re-run the check from it alone.

## Validation rules (what gets rejected)

`wire.py` validates strictly and exits with code 2, listing **every**
problem it found, if any of these hold:

- `name`, `model`, `expiry`, or `evidence` is missing or empty.
- `expiry` is not a valid `YYYY-MM-DD` date (e.g. `2027-02-30`,
  `30/06/2027`, `2027-6-30`).
- `evidence` is missing `type` or `detail`, or `type` is not one of
  `live_test`, `official_page`, `email`, `other`.
- `evidence.at`, when present, is not a valid `YYYY-MM-DD` date.
- `free_quota` is missing, or is not an object with a numeric
  `amount` and a non-empty `unit`, and is not a string starting with
  a number (e.g. `"50 requests/day"`).
- `notes`, when present, is not a string.
- The resource file is missing or is not valid JSON.

Nothing is written to the ladder when validation fails.

## How merging and expiry sorting work

The ladder file has this shape:

```json
{
  "providers": [
    {
      "name": "Example Free Models",
      "model": "example-model-free",
      "free_quota": {"amount": 50, "unit": "requests/day"},
      "expiry": "2027-06-30",
      "evidence": {"type": "live_test", "detail": "...", "at": "2026-11-20"},
      "status": "active"
    }
  ]
}
```

- **Missing ladder file:** if the ladder file does not exist, the
  script starts from an empty ladder and says so. It does not fail.
- **Merge:** if a provider with the same `name` already exists, its
  `model`, `free_quota`, `expiry`, `evidence`, and `notes` are
  updated in place. The entry is **not** duplicated, and the script
  reports it as `Updated:` rather than `Added:`.
- **Sort:** after merging, providers are ordered active-first, then
  degraded, each group sorted by `expiry` ascending —
  soonest-expiring first. Ties fall back to name order so the output
  is deterministic.
- **Output:** the ladder is written back pretty-printed (2-space
  indent) with keys in a stable order:
  `name, model, free_quota, expiry, evidence, status, notes`.

## What "degraded" means

`status` is recomputed on every run from `expiry` against the
reference date (`--today`, defaulting to today's local date):

- `expiry` is **before** the reference date → `status: "degraded"`.
- `expiry` is on or after the reference date → `status: "active"`.

"Degraded" means: do not treat this provider as a usable free
route. It stays in the ladder (so its history and evidence are not
lost) but sorts after every active provider. A degraded entry is not
deleted — if the provider later renews its free terms, wiring a new
record with a later expiry restores it to `active` automatically.

The newly added provider is degraded by the same rule: wiring an
already-expired resource still adds it, but as `degraded`, and the
summary says so.

## Dry-run mode

`--dry-run` prints exactly what would change — which entries would
be added, updated, or degraded, and the resulting order — and writes
nothing. Use it before every real run when the ladder matters, and
whenever you want to check a record without touching the ladder.

## Example commands

```bash
# Add a verified resource to the ladder (creates the ladder if missing)
python3 wire.py --resource examples/resource.example.json --ladder ladder.json

# Check first, change nothing
python3 wire.py --resource examples/resource.example.json --ladder ladder.json --dry-run

# Deterministic date for testing or replay (a resource expiring
# "tomorrow" relative to --today stays active; "yesterday" is degraded)
python3 wire.py --resource resource.json --ladder ladder.json --today 2026-11-25

# Run the tests (standard library only, no network)
python3 -m unittest discover -s tests -v
```

## Exit codes

- `0` — success (including a dry run).
- `2` — validation or input error; the error message lists every
  problem found, and the ladder file is left untouched.
