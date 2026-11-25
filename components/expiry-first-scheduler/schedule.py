#!/usr/bin/env python3
"""Expiry-first free-quota scheduler.

A thin scheduling layer that sits IN FRONT of a failover router. It does
not probe endpoints, does not do failover itself, and has no circuit
breaker -- that is the router's job. It does one thing: given a list of
free providers, hand the router an ordered candidate list, soonest-expiring
first, skipping providers that are expired, out of today's free budget, or
marked dead.

Why this exists
---------------
Generic failover routers already do the probing / failover / breaker work
well. What they do not do is order candidates by expiry first, or block a
provider for the rest of the day once its *daily free budget* is spent.
This module supplies exactly that ordering and blocking, as pure local
logic, so any router can consume its output.

How it is used
--------------
1. Every agent/process that picks a provider reads the same shared state
   file before choosing, and writes usage back to it afterwards.
2. ``schedule()`` returns ``{"usable": [...], "degraded": [...]}``:
   usable provider names, expiry-ascending (ties broken by name), plus
   every excluded provider with a reason (``expired`` / ``over_budget`` /
   ``dead``).
3. After a call, report the outcome with ``record_use()`` / ``report()``
   so the next agent sees the updated budget and liveness.

Design notes
------------
* Python standard library only. Zero network: the ``endpoint`` field in a
  provider record is reference text only and is never called.
* Dates use the local date. ``today`` can be injected (a ``date``, a
  ``"YYYY-MM-DD"`` string, or a callable returning one) so tests can
  control "today" deterministically.
* The state file is the multi-agent shared record: every agent/process
  reads it before choosing and writes back usage. Reads tolerate a
  missing or corrupt file (start fresh with a warning, never crash).
  Writes are atomic (write a temp file in the same directory, then
  rename over the state file).
* No credentials appear anywhere here. Provider records name a model and
  an endpoint for reference only; keys, if any, live with the router /
  caller, never in this config or state file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "Scheduler",
    "SchedulerError",
    "schedule",
    "load_providers",
    "load_providers_file",
    "load_state_file",
    "save_state_file",
    "DEFAULT_STATE_FILE",
]

DEFAULT_STATE_FILE = "./scheduler-state.json"

REASON_EXPIRED = "expired"
REASON_OVER_BUDGET = "over_budget"
REASON_DEAD = "dead"

VALID_OUTCOMES = ("used", "quota_exceeded", "dead", "alive")


class SchedulerError(Exception):
    """Raised for bad provider config, bad state usage, or bad CLI input."""


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------

def _coerce_date(value, field: str = "today") -> date:
    """Coerce a date / "YYYY-MM-DD" string / callable to a ``date``."""
    if callable(value):
        return _coerce_date(value(), field)
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise SchedulerError(
                f"{field} {value!r} is not a valid YYYY-MM-DD date"
            ) from None
    raise SchedulerError(f"{field} must be a date or YYYY-MM-DD string, got {value!r}")


def _today_or_default(today=None) -> date:
    if today is None:
        return date.today()
    return _coerce_date(today)


# ----------------------------------------------------------------------
# Provider records
# ----------------------------------------------------------------------
#
# Provider record schema (JSON):
#   {
#     "name": "example-free-alpha",            # required, unique
#     "model": "example-model-a",              # optional in code, standard in files
#     "endpoint": "https://api.example.invalid/v1",  # optional, reference only, never called
#     "expiry": "2027-01-15",                  # required, YYYY-MM-DD
#     "daily_free_budget": {"amount": 50, "unit": "requests/day"},  # required
#     "notes": "free-form note"                # optional
#   }
#
# ``daily_free_budget`` also accepts a bare integer as shorthand, which is
# normalised to ``{"amount": <int>, "unit": "requests/day"}``.

def _normalise_budget(raw, name: str) -> dict:
    if isinstance(raw, bool):
        raise SchedulerError(f"provider {name!r}: daily_free_budget must not be a boolean")
    if isinstance(raw, int):
        if raw < 0:
            raise SchedulerError(f"provider {name!r}: daily_free_budget amount must be >= 0")
        return {"amount": raw, "unit": "requests/day"}
    if not isinstance(raw, dict):
        raise SchedulerError(
            f"provider {name!r}: daily_free_budget must be an object "
            f'{{"amount": int, "unit": str}} or a non-negative integer'
        )
    amount = raw.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
        raise SchedulerError(
            f"provider {name!r}: daily_free_budget.amount must be a non-negative integer"
        )
    unit = raw.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        raise SchedulerError(
            f"provider {name!r}: daily_free_budget.unit must be a non-empty string"
        )
    return {"amount": amount, "unit": unit.strip()}


def _normalise_provider(data: dict) -> dict:
    """Validate one provider record; return a normalised copy.

    Normalised form: ``expiry`` is a ``datetime.date``; ``daily_free_budget``
    is a ``{"amount": int, "unit": str}`` dict; ``model`` / ``endpoint`` /
    ``notes`` default to ``""`` when absent.
    """
    if not isinstance(data, dict):
        raise SchedulerError(f"provider entry must be an object, got {type(data).__name__}")
    for field in ("name", "expiry", "daily_free_budget"):
        if field not in data:
            raise SchedulerError(f"provider entry missing required field: {field!r}")
    name = str(data["name"])
    if not name.strip():
        raise SchedulerError("provider entry has an empty name")
    try:
        expiry = date.fromisoformat(str(data["expiry"]))
    except ValueError:
        raise SchedulerError(
            f"provider {name!r}: expiry {data['expiry']!r} is not a valid YYYY-MM-DD date"
        ) from None
    return {
        "name": name,
        "model": str(data.get("model") or ""),
        "endpoint": str(data.get("endpoint") or ""),
        "expiry": expiry,
        "daily_free_budget": _normalise_budget(data["daily_free_budget"], name),
        "notes": str(data.get("notes") or ""),
    }


def _provider_public(provider: dict) -> dict:
    """Normalised provider record in JSON-friendly form (expiry as string)."""
    return {
        "name": provider["name"],
        "model": provider["model"],
        "endpoint": provider["endpoint"],
        "expiry": provider["expiry"].isoformat(),
        "daily_free_budget": dict(provider["daily_free_budget"]),
        "notes": provider["notes"],
    }


def load_providers(data) -> list[dict]:
    """Validate provider data and return normalised provider records.

    Accepts a list of provider dicts, or a dict with a ``"providers"`` list.
    Raises ``SchedulerError`` on duplicate names or invalid entries.
    """
    if isinstance(data, dict):
        raw_list = data.get("providers")
        if not isinstance(raw_list, list):
            raise SchedulerError("config must be an object with a 'providers' list")
    elif isinstance(data, list):
        raw_list = data
    else:
        raise SchedulerError("providers must be a list or an object with a 'providers' list")
    if not raw_list:
        raise SchedulerError("config 'providers' list is empty")
    providers: list[dict] = []
    seen: set[str] = set()
    for entry in raw_list:
        provider = _normalise_provider(entry)
        if provider["name"] in seen:
            raise SchedulerError(f"duplicate provider name: {provider['name']!r}")
        seen.add(provider["name"])
        providers.append(provider)
    return providers


def load_providers_file(path) -> list[dict]:
    """Load and validate a providers JSON file. Raises ``SchedulerError``."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SchedulerError(f"providers file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"providers file {path} is not valid JSON: {exc}") from None
    except OSError as exc:
        raise SchedulerError(f"cannot read providers file {path}: {exc}") from None
    return load_providers(raw)


# ----------------------------------------------------------------------
# Shared state file
# ----------------------------------------------------------------------
#
# State file format (JSON): one entry per provider name:
#   {
#     "example-free-alpha": {
#       "today_used": 3,
#       "last_used_date": "2026-11-25",
#       "dead": false,
#       "dead_since": null
#     }
#   }
#
# ``today_used`` counts usage on ``last_used_date`` only. A stale
# ``last_used_date`` (not today) means the count belongs to an earlier day
# and is treated as 0 until new usage is recorded.

def _fresh_entry(today: date) -> dict:
    return {
        "today_used": 0,
        "last_used_date": today.isoformat(),
        "dead": False,
        "dead_since": None,
    }


def _normalise_entry(raw, name: str, today: date) -> dict:
    """Coerce one loaded state entry into shape; garbage fields reset."""
    if not isinstance(raw, dict):
        return _fresh_entry(today)
    used = raw.get("today_used")
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        used = 0
    last = raw.get("last_used_date")
    if not isinstance(last, str):
        last = today.isoformat()
    else:
        try:
            date.fromisoformat(last)
        except ValueError:
            last = today.isoformat()
    dead = raw.get("dead") is True
    since = raw.get("dead_since")
    if since is not None:
        if not isinstance(since, str):
            since = today.isoformat() if dead else None
        else:
            try:
                date.fromisoformat(since)
            except ValueError:
                since = today.isoformat() if dead else None
    if not dead:
        since = None
    return {"today_used": used, "last_used_date": last, "dead": dead, "dead_since": since}


def load_state_file(path, today=None) -> tuple[dict, str | None]:
    """Load a state file, tolerating a missing or corrupt file.

    Returns ``(state, warning)``: ``warning`` is None on a clean load, else
    a human-readable message. A missing, corrupt, or wrong-shaped file
    starts fresh -- it never raises.
    """
    today = _today_or_default(today)
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, f"state file not found: {path} -- starting fresh"
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return {}, f"state file {path} unreadable ({exc}) -- starting fresh"
    if not isinstance(raw, dict):
        return {}, f"state file {path} has wrong shape (expected an object) -- starting fresh"
    # Tolerate a {"providers": {...}} wrapper, for robustness.
    inner = raw.get("providers") if isinstance(raw.get("providers"), dict) else raw
    state: dict = {}
    for name, entry in inner.items():
        state[str(name)] = _normalise_entry(entry, str(name), today)
    return state, None


def save_state_file(path, state: dict) -> None:
    """Write the state file atomically (temp file in the same dir + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(str(path) + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, path)


# ----------------------------------------------------------------------
# Core scheduling (pure: no I/O, no network)
# ----------------------------------------------------------------------

def _effective_used(entry: dict | None, today: date) -> int:
    """Usage counted for today; a stale last_used_date counts as 0."""
    if not entry:
        return 0
    if entry.get("last_used_date") != today.isoformat():
        return 0
    used = entry.get("today_used", 0)
    return used if isinstance(used, int) and used >= 0 else 0


def schedule(providers, state=None, today=None) -> dict:
    """Order providers expiry-first, excluding expired / over-budget / dead.

    ``providers``: list of provider records (dicts; ``expiry`` may be a
    ``"YYYY-MM-DD"`` string or a ``date``; ``daily_free_budget`` a dict or
    a bare int -- see ``_normalise_provider``).
    ``state``: mapping of provider name -> state entry (as in the state
    file), or None for an empty state.
    ``today``: ``date`` / ``"YYYY-MM-DD"`` / callable / None (real local date).

    Returns ``{"usable": [names, expiry-ascending, ties by name],
    "degraded": [{"name": ..., "reason": "expired"|"over_budget"|"dead"}]}``.
    Exclusion priority when several apply: expired, then dead, then
    over_budget. A provider expiring today is still usable.
    """
    today = _today_or_default(today)
    state = state or {}
    normalised = [_normalise_provider(p) for p in providers]
    usable: list[dict] = []
    degraded: list[dict] = []
    for provider in normalised:
        name = provider["name"]
        entry = state.get(name)
        if provider["expiry"] < today:
            degraded.append({"name": name, "reason": REASON_EXPIRED})
        elif entry and entry.get("dead") is True:
            degraded.append({"name": name, "reason": REASON_DEAD})
        elif _effective_used(entry, today) >= provider["daily_free_budget"]["amount"]:
            degraded.append({"name": name, "reason": REASON_OVER_BUDGET})
        else:
            usable.append(provider)
    usable.sort(key=lambda p: (p["expiry"], p["name"]))
    degraded.sort(key=lambda d: next(
        (p["expiry"], p["name"]) for p in normalised if p["name"] == d["name"]
    ))
    return {
        "usable": [p["name"] for p in usable],
        "degraded": degraded,
    }


def _record_use_in_state(state: dict, provider: dict, n: int, today: date) -> dict:
    """Increment usage in an in-memory state dict; return the updated entry."""
    name = provider["name"]
    entry = state.get(name)
    if entry is None:
        entry = _fresh_entry(today)
        state[name] = entry
    if entry.get("last_used_date") != today.isoformat():
        # Date rollover: yesterday's count does not carry over.
        entry["today_used"] = n
        entry["last_used_date"] = today.isoformat()
    else:
        entry["today_used"] = int(entry.get("today_used", 0)) + n
    return entry


def _report_in_state(state: dict, provider: dict, outcome: str, today: date) -> dict:
    """Apply an outcome to an in-memory state dict; return the updated entry."""
    name = provider["name"]
    entry = state.get(name)
    if entry is None:
        entry = _fresh_entry(today)
        state[name] = entry
    if outcome == "used":
        return _record_use_in_state(state, provider, 1, today)
    if outcome == "quota_exceeded":
        # Exhausted for the rest of today, whatever the count was.
        entry["today_used"] = provider["daily_free_budget"]["amount"]
        entry["last_used_date"] = today.isoformat()
    elif outcome == "dead":
        entry["dead"] = True
        entry["dead_since"] = today.isoformat()
    elif outcome == "alive":
        entry["dead"] = False
        entry["dead_since"] = None
    else:
        raise SchedulerError(
            f"unknown outcome: {outcome!r} (use one of: {', '.join(VALID_OUTCOMES)})"
        )
    return entry


# ----------------------------------------------------------------------
# Scheduler: providers + shared state file, with persistence
# ----------------------------------------------------------------------

class Scheduler:
    """Expiry-first scheduler bound to a provider list and a state file.

    Parameters
    ----------
    providers:
        A providers JSON path, a list of provider dicts, or a dict with a
        ``"providers"`` list.
    state_path:
        Path to the shared JSON state file. Default ``./scheduler-state.json``.
        Every agent/process using the same path shares one record: read it
        before choosing, write usage back after.
    today / today_fn:
        Inject "today" for deterministic behaviour: a ``date``, a
        ``"YYYY-MM-DD"`` string (``today``), or a callable returning a
        ``date`` (``today_fn``). Default is the real local date. A per-call
        ``today`` argument overrides both.
    """

    def __init__(self, providers, state_path=DEFAULT_STATE_FILE, today=None, today_fn=None):
        if isinstance(providers, (str, Path)):
            self.providers = load_providers_file(providers)
        else:
            self.providers = load_providers(providers)
        self.state_path = Path(state_path)
        self._today = today
        self._today_fn = today_fn
        self.state, self.state_warning = load_state_file(
            self.state_path, today=self._resolve_today()
        )
        if self.state_warning:
            print(f"warning: {self.state_warning}", file=sys.stderr)

    # -- today ---------------------------------------------------------
    def _resolve_today(self, override=None) -> date:
        if override is not None:
            return _coerce_date(override)
        if self._today_fn is not None:
            return _coerce_date(self._today_fn())
        if self._today is not None:
            return _coerce_date(self._today)
        return date.today()

    # -- providers -----------------------------------------------------
    def get_provider(self, name_or_provider) -> dict:
        """Resolve a provider name (or provider dict) to a normalised record."""
        name = name_or_provider["name"] if isinstance(name_or_provider, dict) else str(name_or_provider)
        for provider in self.providers:
            if provider["name"] == name:
                return provider
        raise SchedulerError(f"unknown provider: {name!r}")

    # -- persistence ---------------------------------------------------
    def save(self) -> None:
        save_state_file(self.state_path, self.state)

    def reload(self, today=None) -> None:
        """Re-read the state file (e.g. another agent just wrote to it)."""
        self.state, warning = load_state_file(self.state_path, today=self._resolve_today(today))
        if warning:
            print(f"warning: {warning}", file=sys.stderr)

    # -- core operations ----------------------------------------------
    def schedule(self, today=None) -> dict:
        """Ordered usable providers + degraded list; see module ``schedule()``."""
        return schedule(self.providers, self.state, today=self._resolve_today(today))

    def record_use(self, provider, n: int = 1, today=None) -> dict:
        """Record ``n`` uses of a provider; resets to ``n`` on date rollover.

        Returns the updated state entry for the provider. Persists the
        state file.
        """
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise SchedulerError(f"n must be a positive integer, got {n!r}")
        today = self._resolve_today(today)
        record = self.get_provider(provider)
        entry = _record_use_in_state(self.state, record, n, today)
        self.save()
        return dict(entry)

    def report(self, provider, outcome: str, today=None) -> dict:
        """Report an outcome for a provider; persists the state file.

        outcome: ``"used"`` (alias for ``record_use(provider, 1)``),
        ``"quota_exceeded"`` (today_used set to the budget amount, so the
        provider is out for the rest of today), ``"dead"`` (dead=true,
        dead_since=today), ``"alive"`` (clears dead).
        Returns the updated state entry for the provider.
        """
        if outcome not in VALID_OUTCOMES:
            raise SchedulerError(
                f"unknown outcome: {outcome!r} (use one of: {', '.join(VALID_OUTCOMES)})"
            )
        today = self._resolve_today(today)
        record = self.get_provider(provider)
        entry = _report_in_state(self.state, record, outcome, today)
        self.save()
        return dict(entry)


# ----------------------------------------------------------------------
# Demo (built-in, fictional providers, temp state file, no network)
# ----------------------------------------------------------------------

def build_demo_providers(today: date) -> list[dict]:
    """Fictional providers, with expiries relative to ``today`` so the demo
    always runs the same way whatever day it is run on."""
    def iso(delta_days: int) -> str:
        return (today + timedelta(days=delta_days)).isoformat()

    return load_providers([
        {
            "name": "example-free-early",
            "model": "example-model-a",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": iso(5),
            "daily_free_budget": {"amount": 2, "unit": "requests/day"},
            "notes": "Small budget, expires soonest -- should be used first.",
        },
        {
            "name": "example-free-mid",
            "model": "example-model-b",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": iso(20),
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
            "notes": "Workhorse: large budget, mid expiry.",
        },
        {
            "name": "example-free-late",
            "model": "example-model-c",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": iso(60),
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
            "notes": "Expires last -- only reached after the earlier ones.",
        },
        {
            "name": "example-free-expired",
            "model": "example-model-a",
            "endpoint": "https://api.example.invalid/v1",
            "expiry": iso(-1),
            "daily_free_budget": {"amount": 50, "unit": "requests/day"},
            "notes": "Expired yesterday -- never usable.",
        },
    ])


def run_demo() -> int:
    """Run the built-in demo. Returns 0. Uses a temp state file."""
    today = date.today()
    print(f"Expiry-first scheduler demo (today: {today.isoformat()})")
    print("Fictional providers only; no network, state lives in a temp dir.")
    with tempfile.TemporaryDirectory(prefix="expiry-scheduler-demo-") as tmp:
        state_path = Path(tmp) / "scheduler-state.json"
        sched = Scheduler(build_demo_providers(today), state_path=state_path, today=today)

        print("\n[1] Initial order -- soonest-expiring first:")
        print(json.dumps(sched.schedule(), indent=2))

        print("\n[2] Use 'example-free-early' twice (its whole daily budget of 2):")
        sched.record_use("example-free-early")
        sched.record_use("example-free-early")
        print(json.dumps(sched.schedule(), indent=2))
        print("-> 'example-free-early' dropped out with reason 'over_budget'.")

        print("\n[3] Mark 'example-free-mid' dead (e.g. the router's probe failed):")
        sched.report("example-free-mid", "dead")
        print(json.dumps(sched.schedule(), indent=2))
        print("-> 'example-free-mid' dropped out with reason 'dead'.")

        print("\n[4] Mark 'example-free-mid' alive again (recovery):")
        sched.report("example-free-mid", "alive")
        print(json.dumps(sched.schedule(), indent=2))
        print("-> 'example-free-mid' is back in the order.")
        print("\nNote: 'example-free-expired' stayed out throughout with reason 'expired'.")
    print("\nDemo done.")
    return 0


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="schedule.py",
        description="Expiry-first free-quota scheduler: order providers "
                    "soonest-expiring first, skipping expired, over-budget "
                    "or dead ones. Pure local logic; no network.",
    )
    parser.add_argument("--providers", default=None,
                        help="path to providers JSON file (required for order/use/report)")
    parser.add_argument("--state", default=DEFAULT_STATE_FILE,
                        help=f"path to shared state file (default: {DEFAULT_STATE_FILE})")
    parser.add_argument("--today", default=None,
                        help="override today as YYYY-MM-DD (for deterministic runs)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_order = sub.add_parser("order", help="print usable order (JSON) + degraded list")
    p_order.set_defaults(func=_cmd_order)

    p_use = sub.add_parser("use", help="record usage of a provider")
    p_use.add_argument("--provider", required=True, help="provider name")
    p_use.add_argument("--n", type=int, default=1, help="uses to record (default: 1)")
    p_use.set_defaults(func=_cmd_use)

    p_report = sub.add_parser("report", help="report an outcome for a provider")
    p_report.add_argument("--provider", required=True, help="provider name")
    p_report.add_argument("--outcome", required=True, choices=list(VALID_OUTCOMES),
                          help="used | quota_exceeded | dead | alive")
    p_report.set_defaults(func=_cmd_report)

    p_demo = sub.add_parser("demo", help="run the built-in demo (fictional providers)")
    p_demo.set_defaults(func=_cmd_demo)

    return parser


def _scheduler_for_cli(args) -> Scheduler:
    if not args.providers:
        print("error: --providers is required for this command", file=sys.stderr)
        sys.exit(2)
    try:
        today = _coerce_date(args.today) if args.today else None
        return Scheduler(args.providers, state_path=args.state, today=today)
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def _cmd_order(args) -> int:
    sched = _scheduler_for_cli(args)
    print(json.dumps(sched.schedule(), indent=2))
    return 0


def _cmd_use(args) -> int:
    sched = _scheduler_for_cli(args)
    try:
        entry = sched.record_use(args.provider, n=args.n)
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    provider = sched.get_provider(args.provider)
    print(json.dumps({
        "provider": args.provider,
        "today_used": entry["today_used"],
        "daily_free_budget": provider["daily_free_budget"],
        "today": sched._resolve_today().isoformat(),
    }, indent=2))
    return 0


def _cmd_report(args) -> int:
    sched = _scheduler_for_cli(args)
    try:
        entry = sched.report(args.provider, args.outcome)
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "provider": args.provider,
        "outcome": args.outcome,
        "entry": entry,
    }, indent=2))
    return 0


def _cmd_demo(args) -> int:
    return run_demo()


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
