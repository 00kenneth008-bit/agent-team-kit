#!/usr/bin/env python3
"""Wire a verified free resource into a model ladder.

A model ladder is an ordered list of providers, stored as JSON:

    {"providers": [{"name": ..., "model": ..., "free_quota": ...,
                    "expiry": "YYYY-MM-DD", "evidence": {...},
                    "status": "active" | "degraded", "notes": ...}, ...]}

This script takes one verified-resource record, validates it strictly,
merges it into the ladder (updating in place when a provider with the
same name already exists, never duplicating), sorts the ladder so
active providers come first ordered by expiry ascending
(soonest-expiring first) with degraded providers after, also by
expiry ascending, and marks any provider whose expiry is before
today as "degraded".

Usage:
    python3 wire.py --resource resource.json --ladder ladder.json
                     [--dry-run] [--today YYYY-MM-DD]

Exit codes: 0 on success, 2 on validation / input errors.

Python standard library only. No network access.
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ALLOWED_EVIDENCE_TYPES = ("live_test", "official_page", "email", "other")

_QUOTA_STRING_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(.+?)\s*$")


class ValidationError(Exception):
    """Raised when a resource record (or ladder file) fails validation.

    Carries every problem found, so the caller can list them all
    instead of failing on the first one.
    """

    def __init__(self, problems):
        super().__init__("; ".join(problems))
        self.problems = list(problems)


def parse_date(value, field):
    """Parse a YYYY-MM-DD string, returning a date or None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    # strptime accepts e.g. "2026-1-5"; require the exact zero-padded form.
    if parsed.isoformat() != value:
        return None
    return parsed


def parse_free_quota(value):
    """Normalize a free_quota value to {"amount": number, "unit": str}.

    Accepts a dict {"amount": number, "unit": str} or a plain string
    such as "50 requests/day" (leading number + unit text).
    Returns (normalized_dict, error_message_or_None).
    """
    if isinstance(value, dict):
        amount = value.get("amount")
        unit = value.get("unit")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None, "free_quota.amount must be a number"
        if not isinstance(unit, str) or not unit.strip():
            return None, "free_quota.unit must be a non-empty string"
        return {"amount": amount, "unit": unit.strip()}, None
    if isinstance(value, str):
        match = _QUOTA_STRING_RE.match(value)
        if not match:
            return None, (
                'free_quota string must start with a number, '
                'e.g. "50 requests/day"'
            )
        amount_text, unit = match.groups()
        amount = float(amount_text) if "." in amount_text else int(amount_text)
        return {"amount": amount, "unit": unit.strip()}, None
    return None, "free_quota must be an object {amount, unit} or a string"


def validate_resource(record):
    """Validate a verified-resource record, returning a list of problems.

    An empty list means the record is valid. Every problem is collected
    so the caller can report them all at once.
    """
    problems = []
    if not isinstance(record, dict):
        return ["resource must be a JSON object"]

    name = record.get("name")
    if not isinstance(name, str) or not name.strip():
        problems.append("name is missing or empty")

    model = record.get("model")
    if not isinstance(model, str) or not model.strip():
        problems.append("model is missing or empty")

    expiry = record.get("expiry")
    if not isinstance(expiry, str) or not expiry.strip():
        problems.append("expiry is missing or empty")
    elif parse_date(expiry, "expiry") is None:
        problems.append(f"expiry is not a valid YYYY-MM-DD date: {expiry!r}")

    if "free_quota" not in record or record.get("free_quota") is None:
        problems.append("free_quota is missing")
    else:
        _, quota_error = parse_free_quota(record["free_quota"])
        if quota_error:
            problems.append(quota_error)

    evidence = record.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        problems.append("evidence is missing or empty")
    else:
        ev_type = evidence.get("type")
        if not isinstance(ev_type, str) or not ev_type.strip():
            problems.append("evidence.type is missing or empty")
        elif ev_type not in ALLOWED_EVIDENCE_TYPES:
            problems.append(
                f"evidence.type is not allowed: {ev_type!r} "
                f"(allowed: {', '.join(ALLOWED_EVIDENCE_TYPES)})"
            )
        detail = evidence.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            problems.append("evidence.detail is missing or empty")
        at = evidence.get("at")
        if at is not None and parse_date(at, "evidence.at") is None:
            problems.append(
                f"evidence.at is not a valid YYYY-MM-DD date: {at!r}"
            )

    notes = record.get("notes")
    if notes is not None and not isinstance(notes, str):
        problems.append("notes must be a string")

    return problems


def normalize_resource(record):
    """Return a clean provider dict (stable key order) from a valid record."""
    quota, _ = parse_free_quota(record["free_quota"])
    evidence = {"type": record["evidence"]["type"],
                "detail": record["evidence"]["detail"].strip()}
    if record["evidence"].get("at") is not None:
        evidence["at"] = record["evidence"]["at"]
    provider = {
        "name": record["name"].strip(),
        "model": record["model"].strip(),
        "free_quota": quota,
        "expiry": record["expiry"],
        "evidence": evidence,
        # Status is decided by merge_and_sort() against --today.
        "status": "active",
    }
    if record.get("notes") is not None:
        provider["notes"] = record["notes"]
    return provider


def normalize_provider(provider):
    """Normalize an existing ladder entry into stable key order.

    Unknown extra keys are dropped; missing optional keys stay missing.
    Entries that fail basic shape checks raise ValidationError.
    """
    problems = []
    if not isinstance(provider, dict):
        raise ValidationError(["ladder provider entry must be a JSON object"])
    if not isinstance(provider.get("name"), str) or not provider["name"].strip():
        problems.append("ladder provider is missing a name")
    if not isinstance(provider.get("model"), str) or not provider["model"].strip():
        problems.append(
            f"ladder provider {provider.get('name')!r} is missing a model"
        )
    if parse_date(provider.get("expiry"), "expiry") is None:
        problems.append(
            f"ladder provider {provider.get('name')!r} has an invalid "
            f"expiry: {provider.get('expiry')!r}"
        )
    if "free_quota" in provider and provider["free_quota"] is not None:
        _, quota_error = parse_free_quota(provider["free_quota"])
        if quota_error:
            problems.append(
                f"ladder provider {provider.get('name')!r}: {quota_error}"
            )
    if problems:
        raise ValidationError(problems)
    quota = None
    if provider.get("free_quota") is not None:
        quota, _ = parse_free_quota(provider["free_quota"])
    evidence = provider.get("evidence") or {}
    normalized_evidence = {}
    if isinstance(evidence, dict):
        if evidence.get("type") is not None:
            normalized_evidence["type"] = evidence["type"]
        if evidence.get("detail") is not None:
            normalized_evidence["detail"] = evidence["detail"]
        if evidence.get("at") is not None:
            normalized_evidence["at"] = evidence["at"]
    out = {
        "name": provider["name"].strip(),
        "model": provider["model"].strip(),
        "free_quota": quota,
        "expiry": provider["expiry"],
        "evidence": normalized_evidence,
        "status": provider.get("status", "active"),
    }
    if provider.get("notes") is not None:
        out["notes"] = provider["notes"]
    return out


def load_json_file(path, what):
    """Load a JSON file, raising ValidationError with a clear message."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError([f"cannot read {what} file {path}: {exc}"])
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError([f"{what} file {path} is not valid JSON: {exc}"])


def load_ladder(path):
    """Load a ladder file; return ([providers], existed_bool).

    A missing file starts from an empty ladder. A present file must
    be {"providers": [...]}.
    """
    if not Path(path).exists():
        return [], False
    data = load_json_file(path, "ladder")
    if not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        raise ValidationError(
            [f"ladder file {path} must be an object with a "
             f'"providers" list']
        )
    return [normalize_provider(p) for p in data["providers"]], True


def merge_and_sort(providers, new_provider, today):
    """Merge new_provider in, sort, and degrade expired entries.

    Returns (sorted_providers, added_names, updated_names,
             newly_degraded_names, restored_names).
    Sorting: active first by expiry ascending, then degraded by
    expiry ascending. Degraded means expiry is before today.
    """
    before = {p["name"]: dict(p) for p in providers}
    added = []
    updated = []
    existing = next((p for p in providers if p["name"] == new_provider["name"]), None)
    if existing is None:
        providers.append(dict(new_provider))
        added.append(new_provider["name"])
    else:
        changed = any(
            existing.get(key) != new_provider.get(key)
            for key in ("model", "free_quota", "expiry", "evidence", "notes")
        )
        # Update in place; keep the existing status for now, it is
        # recomputed below from the (possibly new) expiry.
        for key in ("model", "free_quota", "expiry", "evidence", "notes"):
            if key in new_provider:
                existing[key] = new_provider[key]
            elif key in existing and key == "notes":
                del existing["notes"]
        if changed:
            updated.append(new_provider["name"])

    newly_degraded = []
    restored = []
    for provider in providers:
        expiry = parse_date(provider["expiry"], "expiry")
        old_status = provider.get("status", "active")
        new_status = "degraded" if expiry is not None and expiry < today else "active"
        provider["status"] = new_status
        if new_status == "degraded" and old_status != "degraded":
            newly_degraded.append(provider["name"])
        elif new_status == "active" and old_status == "degraded":
            restored.append(provider["name"])

    providers.sort(
        key=lambda p: (
            0 if p["status"] == "active" else 1,
            parse_date(p["expiry"], "expiry") or date.max,
            p["name"],
        )
    )
    return providers, added, updated, newly_degraded, restored


def format_order(providers):
    """One line per provider: '  1. Name (model) — expiry YYYY-MM-DD, status'."""
    lines = []
    for i, provider in enumerate(providers, 1):
        lines.append(
            f"  {i}. {provider['name']} ({provider['model']}) — "
            f"expiry {provider['expiry']}, {provider['status']}"
        )
    return lines


def build_parser():
    parser = argparse.ArgumentParser(
        description="Wire a verified free resource into a model ladder."
    )
    parser.add_argument("--resource", required=True,
                        help="path to the verified-resource JSON record")
    parser.add_argument("--ladder", required=True,
                        help="path to the ladder JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would change and write nothing")
    parser.add_argument("--today", default=None,
                        help="reference date YYYY-MM-DD "
                             "(default: today's local date)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.today is None:
        today = date.today()
    else:
        today = parse_date(args.today, "--today")
        if today is None:
            print(f"error: --today is not a valid YYYY-MM-DD date: "
                  f"{args.today!r}", file=sys.stderr)
            return 2

    # --- Load and validate the resource record. ---
    try:
        record = load_json_file(args.resource, "resource")
    except ValidationError as exc:
        for problem in exc.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2
    problems = validate_resource(record)
    if problems:
        print("error: resource record is invalid:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    new_provider = normalize_resource(record)

    # --- Load the ladder (missing file = empty ladder). ---
    try:
        providers, ladder_existed = load_ladder(args.ladder)
    except ValidationError as exc:
        for problem in exc.problems:
            print(f"error: {problem}", file=sys.stderr)
        return 2

    before_names = {p["name"] for p in providers}
    providers, added, updated, newly_degraded, restored = merge_and_sort(
        providers, new_provider, today
    )
    degraded_now = [p["name"] for p in providers if p["status"] == "degraded"]

    # --- Report. ---
    if args.dry_run:
        print("DRY RUN — no files written.")
    if not ladder_existed:
        print(f"Ladder file {args.ladder} did not exist; "
              f"starting from an empty ladder.")
    if added:
        print(f"Added: {', '.join(added)}")
    if updated:
        print(f"Updated: {', '.join(updated)}")
    if not added and not updated:
        print("No provider added or updated.")
    if newly_degraded:
        print(f"Degraded: {', '.join(newly_degraded)} "
              f"(expiry before {today.isoformat()})")
    if restored:
        print(f"Restored to active: {', '.join(restored)}")
    if degraded_now:
        print(f"Degraded in ladder: {', '.join(degraded_now)}")
    print(f"Resulting order ({len(providers)} provider(s)):")
    print("\n".join(format_order(providers)) if providers else "  (empty)")

    if args.dry_run:
        return 0

    ladder_data = {"providers": providers}
    try:
        Path(args.ladder).write_text(
            json.dumps(ladder_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"error: cannot write ladder file {args.ladder}: {exc}",
              file=sys.stderr)
        return 2
    print(f"Wrote ladder to {args.ladder}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
