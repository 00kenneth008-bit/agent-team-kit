#!/usr/bin/env python3
"""Enforcement gates: a deliverable may not be submitted until it passes
three gates.

Gate 1 — self-check: deterministic checks from a 100-point standard.
    A failed veto check, or a score below the pass mark, BLOCKS submission.
Gate 2 — manager re-check: an independent second score. If the two scores
    disagree by more than 10 points the work is SENT BACK for rework.
Gate 3 — final review: the deliverable must actually be openable/usable,
    and gate 1 and gate 2 records must exist for it. Otherwise BLOCKED.

The 100-point score is only the measurement layer inside the gates. The
product is the gate workflow: a gate blocks, a scorecard does not.

Usage:
    python3 gates.py --standard S.json --deliverable D
        [--manager-standard M.json] [--self-score S1.json]
        [--manager-score S2.json] [--record record.json]
        [--format text|json]

A standard is a JSON file (or YAML, if PyYAML is installed)::

    {
      "name": "Short article",
      "pass_score": 80,
      "dimensions": [
        {"id": "completeness", "name": "Completeness", "weight": 40,
         "checks": [{"type": "text_min_length", "min": 300}]}
      ],
      "veto": [
        {"id": "file-exists", "description": "Deliverable file must exist",
         "check": {"type": "file_exists"}}
      ]
    }

Score files (--self-score / --manager-score) are JSON objects of the form
{"score": <number>}.

Exit codes: 0 = PASS (all three gates passed), 1 = BLOCKED or SEND BACK,
2 = usage / config error.

An optional second-model review hook is available through the environment
variables REVIEW_API_URL, REVIEW_API_KEY and REVIEW_MODEL. The hook is
advisory only: it never changes any gate verdict. See SKILL.md.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REVIEW_TIMEOUT_SECONDS = 10
REVIEW_SKIPPED_NOTE = "second-model review skipped: REVIEW_API_URL not set"
SCORE_GAP_LIMIT = 10

VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "mkv", "avi"}
TEXT_EXTENSIONS = {"txt", "md"}

CHECK_TYPES = {
    "file_exists",
    "min_size_bytes",
    "extension",
    "text_min_length",
    "text_contains_all",
    "text_forbidden_any",
    "video_probe",
}


class ConfigError(Exception):
    """Raised when a standard file, score file or CLI usage is invalid."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_int(value):
    """True for real ints (booleans do not count as ints here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool))


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _read_text(path):
    """Read *path* as UTF-8 text.

    Returns (text, error). On success error is None; on failure text is None
    and error is a short human-readable reason.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), None
    except FileNotFoundError:
        return None, "file does not exist"
    except IsADirectoryError:
        return None, "path is a directory, not a file"
    except UnicodeDecodeError as exc:
        return None, "file is not readable as UTF-8 text (%s)" % exc.reason
    except OSError as exc:
        return None, "could not read file: %s" % exc


# ---------------------------------------------------------------------------
# Standard loading and validation (the measurement layer)
# ---------------------------------------------------------------------------

def load_standard(path):
    """Load and validate a standard file. Returns the standard dict.

    Raises ConfigError on any problem.
    """
    if not os.path.exists(path):
        raise ConfigError("standard file not found: %s" % path)
    suffix = os.path.splitext(path)[1].lower()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ConfigError("could not read standard file %s: %s" % (path, exc))

    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise ConfigError(
                "standard file %s is YAML but PyYAML is not installed; "
                "convert the standard to JSON and try again" % path
            )
        try:
            data = yaml.safe_load(raw)
        except Exception as exc:  # yaml.YAMLError and friends
            raise ConfigError("invalid YAML in standard file %s: %s" % (path, exc))
    elif suffix == ".json":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("invalid JSON in standard file %s: %s" % (path, exc))
    else:
        raise ConfigError(
            "standard file %s must end in .json, .yaml or .yml" % path
        )

    return validate_standard(data)


def _validate_check(check, where):
    if not isinstance(check, dict):
        raise ConfigError("%s: each check must be an object" % where)
    check_type = check.get("type")
    if check_type not in CHECK_TYPES:
        raise ConfigError(
            "%s: unknown check type %r (known types: %s)"
            % (where, check_type, ", ".join(sorted(CHECK_TYPES)))
        )
    if check_type == "min_size_bytes":
        if not _is_int(check.get("min")) or check["min"] < 0:
            raise ConfigError("%s: min_size_bytes needs a non-negative int 'min'" % where)
    elif check_type == "extension":
        allowed = check.get("allowed")
        if (not isinstance(allowed, list) or not allowed
                or not all(isinstance(a, str) and a and "." not in a for a in allowed)):
            raise ConfigError(
                "%s: extension needs a non-empty 'allowed' list of lowercase "
                "extensions without a dot" % where
            )
    elif check_type == "text_min_length":
        if not _is_int(check.get("min")) or check["min"] < 0:
            raise ConfigError("%s: text_min_length needs a non-negative int 'min'" % where)
    elif check_type in ("text_contains_all", "text_forbidden_any"):
        keywords = check.get("keywords")
        if (not isinstance(keywords, list) or not keywords
                or not all(isinstance(k, str) and k for k in keywords)):
            raise ConfigError(
                "%s: %s needs a non-empty 'keywords' list of non-empty strings"
                % (where, check_type)
            )
    elif check_type == "video_probe":
        for key in ("duration", "bitrate_kbps"):
            if key not in check:
                continue
            value = check[key]
            if (not isinstance(value, list) or len(value) != 2
                    or not all(_is_number(v) for v in value)
                    or value[0] > value[1]):
                raise ConfigError(
                    "%s: video_probe '%s' must be a [min, max] pair with min <= max"
                    % (where, key)
                )
        if "resolution" in check:
            value = check["resolution"]
            if (not isinstance(value, list) or len(value) != 2
                    or not all(_is_number(v) and v >= 0 for v in value)):
                raise ConfigError(
                    "%s: video_probe 'resolution' must be a [min_width, min_height] "
                    "pair of non-negative numbers" % where
                )


def validate_standard(data):
    """Validate a parsed standard dict. Returns it unchanged.

    Raises ConfigError on any problem.
    """
    if not isinstance(data, dict):
        raise ConfigError("standard must be a JSON object")
    if "name" in data and not isinstance(data["name"], str):
        raise ConfigError("standard 'name' must be a string")
    pass_score = data.get("pass_score", 80)
    if not _is_int(pass_score) or not 0 <= pass_score <= 100:
        raise ConfigError("standard 'pass_score' must be an int between 0 and 100")

    dimensions = data.get("dimensions")
    if not isinstance(dimensions, list) or not dimensions:
        raise ConfigError("standard needs a non-empty 'dimensions' list")
    total_weight = 0
    for dim in dimensions:
        where = "dimension %r" % (dim.get("id") if isinstance(dim, dict) else dim)
        if not isinstance(dim, dict):
            raise ConfigError("%s: each dimension must be an object" % where)
        for field in ("id", "name"):
            if not isinstance(dim.get(field), str) or not dim[field]:
                raise ConfigError("%s: '%s' must be a non-empty string" % (where, field))
        weight = dim.get("weight")
        if not _is_int(weight) or weight < 0:
            raise ConfigError("%s: 'weight' must be a non-negative int" % where)
        total_weight += weight
        checks = dim.get("checks")
        if not isinstance(checks, list):
            raise ConfigError("%s: 'checks' must be a list" % where)
        for i, check in enumerate(checks):
            _validate_check(check, "%s check %d" % (where, i))
    if total_weight != 100:
        raise ConfigError(
            "dimension weights must sum to 100, got %d" % total_weight
        )

    veto = data.get("veto", [])
    if not isinstance(veto, list):
        raise ConfigError("standard 'veto' must be a list")
    for i, item in enumerate(veto):
        where = "veto %d" % i
        if not isinstance(item, dict):
            raise ConfigError("%s: each veto must be an object" % where)
        for field in ("id", "description"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise ConfigError("%s: '%s' must be a non-empty string" % (where, field))
        if "check" not in item:
            raise ConfigError("%s: missing 'check'" % where)
        _validate_check(item["check"], "%s check" % where)

    return data


def load_score_file(path):
    """Load a {"score": number} file. Returns the score as a float.

    Raises ConfigError on any problem.
    """
    if not os.path.exists(path):
        raise ConfigError("score file not found: %s" % path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("could not read score file %s: %s" % (path, exc))
    if not isinstance(data, dict) or not _is_number(data.get("score")):
        raise ConfigError(
            "score file %s must be a JSON object with a numeric 'score'" % path
        )
    score = float(data["score"])
    if not 0 <= score <= 100:
        raise ConfigError("score file %s: score %s is outside 0..100" % (path, score))
    return score


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------

def run_check(check, deliverable):
    """Run one check against the deliverable path.

    Returns a dict with keys: type, passed (bool), skipped (bool),
    status ("passed" | "failed" | "skipped") and detail (str).
    """
    check_type = check["type"]

    if check_type == "file_exists":
        ok = os.path.isfile(deliverable)
        return _result(check_type, ok, False,
                       "file exists" if ok else "file does not exist: %s" % deliverable)

    if check_type == "min_size_bytes":
        minimum = check["min"]
        try:
            size = os.path.getsize(deliverable)
        except OSError:
            return _result(check_type, False, False, "file does not exist")
        return _result(check_type, size >= minimum, False,
                       "size %d bytes (minimum %d)" % (size, minimum))

    if check_type == "extension":
        allowed = [a.lower() for a in check["allowed"]]
        ext = os.path.splitext(deliverable)[1].lower().lstrip(".")
        ok = ext in allowed
        return _result(check_type, ok, False,
                       "extension '.%s' (allowed: %s)"
                       % (ext if ext else "(none)", ", ".join("." + a for a in allowed)))

    if check_type == "text_min_length":
        minimum = check["min"]
        text, error = _read_text(deliverable)
        if error is not None:
            return _result(check_type, False, False, error)
        return _result(check_type, len(text) >= minimum, False,
                       "length %d characters (minimum %d)" % (len(text), minimum))

    if check_type == "text_contains_all":
        keywords = check["keywords"]
        text, error = _read_text(deliverable)
        if error is not None:
            return _result(check_type, False, False, error)
        lowered = text.lower()
        missing = [k for k in keywords if k.lower() not in lowered]
        ok = not missing
        detail = ("all %d required keywords present" % len(keywords) if ok
                  else "missing keywords: %s" % ", ".join(missing))
        return _result(check_type, ok, False, detail)

    if check_type == "text_forbidden_any":
        keywords = check["keywords"]
        text, error = _read_text(deliverable)
        if error is not None:
            return _result(check_type, False, False, error)
        lowered = text.lower()
        found = [k for k in keywords if k.lower() in lowered]
        ok = not found
        detail = ("no forbidden keywords present" if ok
                  else "forbidden keywords found: %s" % ", ".join(found))
        return _result(check_type, ok, False, detail)

    if check_type == "video_probe":
        return _run_video_probe(check, deliverable)

    raise ConfigError("unknown check type %r" % check_type)  # pragma: no cover


def _result(check_type, passed, skipped, detail):
    status = "skipped" if skipped else ("passed" if passed else "failed")
    return {
        "type": check_type,
        "passed": bool(passed),
        "skipped": bool(skipped),
        "status": status,
        "detail": detail,
    }


def _run_video_probe(check, deliverable):
    """Probe a video file with ffprobe.

    If the ffprobe binary is not found, the check is skipped (not failed)
    and excluded from scoring.
    """
    if shutil.which("ffprobe") is None:
        return _result(
            "video_probe", False, True,
            "skipped: ffprobe binary not found; install ffmpeg/ffprobe "
            "to run video checks",
        )
    if not os.path.isfile(deliverable):
        return _result("video_probe", False, False, "file does not exist: %s" % deliverable)
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", deliverable],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return _result("video_probe", False, False, "ffprobe timed out")
    except OSError as exc:
        return _result("video_probe", False, False, "could not run ffprobe: %s" % exc)
    if proc.returncode != 0:
        reason = (proc.stderr or "").strip().splitlines()
        return _result(
            "video_probe", False, False,
            "ffprobe failed: %s" % (reason[-1] if reason else "unknown error"),
        )
    try:
        probe = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _result("video_probe", False, False, "ffprobe returned invalid JSON")

    fmt = probe.get("format", {}) or {}
    video_stream = None
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") == "video":
            video_stream = stream
            break
    if video_stream is None:
        return _result("video_probe", False, False, "no video stream found")

    problems = []
    measured = []
    try:
        duration = float(fmt.get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    width = video_stream.get("width") or 0
    height = video_stream.get("height") or 0
    try:
        bitrate_kbps = float(fmt.get("bit_rate", 0) or 0) / 1000.0
    except (TypeError, ValueError):
        bitrate_kbps = 0.0
    measured.append("duration %.1fs, resolution %dx%d, bitrate %.0f kbps"
                    % (duration, width, height, bitrate_kbps))

    if "duration" in check:
        lo, hi = check["duration"]
        if not lo <= duration <= hi:
            problems.append("duration %.1fs outside [%s, %s]s" % (duration, lo, hi))
    if "resolution" in check:
        min_w, min_h = check["resolution"]
        if width < min_w or height < min_h:
            problems.append("resolution %dx%d below minimum %dx%d"
                            % (width, height, min_w, min_h))
    if "bitrate_kbps" in check:
        lo, hi = check["bitrate_kbps"]
        if not lo <= bitrate_kbps <= hi:
            problems.append("bitrate %.0f kbps outside [%s, %s] kbps"
                            % (bitrate_kbps, lo, hi))

    if problems:
        return _result("video_probe", False, False,
                       "; ".join(measured + problems))
    return _result("video_probe", True, False, "; ".join(measured))


# ---------------------------------------------------------------------------
# Scoring (the measurement layer inside the gates)
# ---------------------------------------------------------------------------

def score_deliverable(standard, deliverable):
    """Score *deliverable* against *standard*. Returns a detail dict with
    keys: score, pass_score, dimensions, deductions, veto, skipped."""
    dimensions_out = []
    deductions = []
    skipped = []
    total = 0.0

    for dim in standard["dimensions"]:
        results = [run_check(c, deliverable) for c in dim["checks"]]
        counted = [r for r in results if not r["skipped"]]
        passed = sum(1 for r in counted if r["passed"])
        if counted:
            earned = round(dim["weight"] * passed / len(counted), 1)
        else:
            # Every check in this dimension was skipped: nothing failed,
            # so the dimension keeps its full weight (skipped checks are
            # excluded from scoring, never penalised).
            earned = float(dim["weight"])
        total += earned
        dimensions_out.append({
            "id": dim["id"],
            "name": dim["name"],
            "weight": dim["weight"],
            "earned": earned,
            "checks": results,
        })
        for result in results:
            if result["skipped"]:
                skipped.append({
                    "dimension": dim["id"],
                    "check": result["type"],
                    "detail": result["detail"],
                })
            elif not result["passed"]:
                deductions.append({
                    "dimension": dim["id"],
                    "check": result["type"],
                    "detail": result["detail"],
                })

    veto_out = []
    veto_failed = False
    for item in standard.get("veto", []):
        result = run_check(item["check"], deliverable)
        if result["skipped"]:
            skipped.append({
                "dimension": "veto",
                "check": result["type"],
                "detail": result["detail"],
            })
            passed = True  # a skipped veto cannot confirm a violation
        else:
            passed = result["passed"]
        if not passed:
            veto_failed = True
        veto_out.append({
            "id": item["id"],
            "description": item["description"],
            "passed": passed,
            "skipped": result["skipped"],
            "detail": result["detail"],
        })

    return {
        "score": round(total, 1),
        "pass_score": standard.get("pass_score", 80),
        "dimensions": dimensions_out,
        "deductions": deductions,
        "veto": veto_out,
        "skipped": skipped,
        "veto_failed": veto_failed,
    }


# ---------------------------------------------------------------------------
# The three gates
# ---------------------------------------------------------------------------

def _gate_record(stage, score, verdict, reason):
    return {
        "stage": stage,
        "score": score,
        "verdict": verdict,  # "pass" | "blocked" | "send_back"
        "reason": reason,
        "at": _now_iso(),
    }


def run_gate1(standard, deliverable):
    """Gate 1 — self-check. Returns (gate_record, score_detail).

    BLOCKED when any veto check failed, or when the score is below the
    pass mark. Nothing may proceed past a blocked gate 1.
    """
    detail = score_deliverable(standard, deliverable)
    if detail["veto_failed"]:
        failed = [v for v in detail["veto"] if not v["passed"]]
        reason = "veto failed: %s" % "; ".join(
            "%s — %s" % (v["id"], v["description"]) for v in failed)
        return _gate_record("self", detail["score"], "blocked", reason), detail
    if detail["score"] < detail["pass_score"]:
        reason = ("score %.1f below pass mark %d"
                  % (detail["score"], detail["pass_score"]))
        return _gate_record("self", detail["score"], "blocked", reason), detail
    reason = ("score %.1f meets pass mark %d"
              % (detail["score"], detail["pass_score"]))
    return _gate_record("self", detail["score"], "pass", reason), detail


def run_gate2(deliverable, gate1_score, manager_standard=None,
              manager_score=None, self_score=None):
    """Gate 2 — manager re-check. Returns (gate_record, manager_detail).

    The second score S2 comes from, in order of preference:
      (a) a fresh re-run of the checks with --manager-standard;
      (b) a --manager-score file ({"score": number});
      (c) the gate-1 score itself, when only --self-score was given or
          when no second score was supplied at all.
    The first score S1 is the --self-score file when given, else gate 1's.
    |S1 - S2| > 10 sends the work back; otherwise it proceeds.
    manager_detail is the re-run detail for (a), else None.
    """
    s1 = self_score if self_score is not None else gate1_score
    manager_detail = None
    if manager_standard is not None:
        manager_detail = score_deliverable(manager_standard, deliverable)
        if manager_detail["veto_failed"]:
            failed = [v for v in manager_detail["veto"] if not v["passed"]]
            reason = "manager re-check veto failed: %s" % "; ".join(
                "%s — %s" % (v["id"], v["description"]) for v in failed)
            return (_gate_record("manager", manager_detail["score"],
                                 "send_back", reason),
                    manager_detail)
        s2 = manager_detail["score"]
        source_note = "manager standard re-run"
    elif manager_score is not None:
        s2 = manager_score
        source_note = "manager score file"
    else:
        s2 = gate1_score
        source_note = ("gate-1 score reused (no independent second score "
                       "supplied)")

    gap = round(abs(s1 - s2), 1)
    if gap > SCORE_GAP_LIMIT:
        reason = ("score gap %.1f > %d (self %.1f, manager %.1f; %s)"
                  % (gap, SCORE_GAP_LIMIT, s1, s2, source_note))
        return _gate_record("manager", s2, "send_back", reason), manager_detail
    reason = ("score gap %.1f within %d (self %.1f, manager %.1f; %s)"
              % (gap, SCORE_GAP_LIMIT, s1, s2, source_note))
    return _gate_record("manager", s2, "pass", reason), manager_detail


def _check_openable(deliverable):
    """Gate 3's usability check. Returns None when the deliverable is
    openable/usable, else a reason string explaining why it is not."""
    if not os.path.isfile(deliverable):
        return "deliverable does not exist: %s" % deliverable
    try:
        if os.path.getsize(deliverable) == 0:
            return "deliverable is empty: %s" % deliverable
    except OSError as exc:
        return "deliverable cannot be read: %s" % exc

    ext = os.path.splitext(deliverable)[1].lower().lstrip(".")
    if ext in VIDEO_EXTENSIONS:
        # A gate must verify, not assume: if ffprobe is missing the video
        # cannot be shown openable, so it blocks instead of skipping.
        if shutil.which("ffprobe") is None:
            return ("cannot verify video is openable: ffprobe binary not found")
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", deliverable],
                capture_output=True, text=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return "ffprobe could not open video: %s" % exc
        if proc.returncode != 0:
            return "ffprobe could not open video (not a readable video file)"
        try:
            probe = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return "ffprobe returned invalid data for video"
        if not any(s.get("codec_type") == "video"
                   for s in probe.get("streams", []) or []):
            return "video file has no video stream"
        return None
    if ext in TEXT_EXTENSIONS:
        _, error = _read_text(deliverable)
        if error is not None:
            return "deliverable is not readable as UTF-8 text: %s" % error
        return None
    return None


def run_gate3(deliverable, prior_gates):
    """Gate 3 — final review. Returns a gate record.

    Passes only when the deliverable is actually openable/usable AND
    gate 1 and gate 2 records with verdict "pass" exist for it.
    *prior_gates* is the list of gate records from this run (and, for the
    CLI, any matching records already in the record file).
    """
    openable_problem = _check_openable(deliverable)
    if openable_problem is not None:
        return _gate_record("final", None, "blocked", openable_problem)
    missing = [stage for stage in ("self", "manager")
               if not any(g.get("stage") == stage and g.get("verdict") == "pass"
                          for g in prior_gates)]
    if missing:
        return _gate_record(
            "final", None, "blocked",
            "missing passing gate record(s): %s" % ", ".join(missing))
    return _gate_record(
        "final", None, "pass",
        "deliverable openable; gate 1 and gate 2 records present")


def run_all(standard, deliverable, manager_standard=None,
            manager_score=None, self_score=None, prior_gates=None):
    """Run the gates in order, stopping at the first gate that does not
    pass. Returns (record, detail) where record is the review record
    {"deliverable", "standard", "gates", "final"} and detail holds the
    scoring detail, manager detail, gap and verdict."""
    gates = []
    gate1, detail = run_gate1(standard, deliverable)
    gates.append(gate1)
    manager_detail = None
    gap = None
    verdict = "BLOCKED"
    if gate1["verdict"] == "pass":
        gate2, manager_detail = run_gate2(
            deliverable, detail["score"], manager_standard,
            manager_score, self_score)
        gates.append(gate2)
        s1 = self_score if self_score is not None else detail["score"]
        gap = round(abs(s1 - gate2["score"]), 1) if gate2["score"] is not None else None
        if gate2["verdict"] == "pass":
            prior = list(gates) + list(prior_gates or [])
            gate3 = run_gate3(deliverable, prior)
            gates.append(gate3)
            if gate3["verdict"] == "pass":
                verdict = "PASS"
        else:
            verdict = "SEND_BACK"

    record = {
        "deliverable": deliverable,
        "standard": standard.get("name", "unnamed standard"),
        "gates": gates,
        "final": "pass" if verdict == "PASS" else "blocked",
    }
    full = dict(record)
    full.update({
        "verdict": verdict,
        "score": detail["score"],
        "pass_score": detail["pass_score"],
        "manager_score": gates[1]["score"] if len(gates) > 1 else None,
        "score_gap": gap,
        "dimensions": detail["dimensions"],
        "deductions": detail["deductions"],
        "veto": detail["veto"],
        "skipped": detail["skipped"],
        "manager": manager_detail,
    })
    return record, full


# ---------------------------------------------------------------------------
# Review record file
# ---------------------------------------------------------------------------

def load_prior_gates(record_path, deliverable):
    """Gate records already stored for this deliverable, or [].

    A missing or unreadable record file simply means no prior gates.
    """
    if not record_path or not os.path.exists(record_path):
        return []
    try:
        with open(record_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    if (not isinstance(data, dict)
            or data.get("deliverable") != deliverable
            or not isinstance(data.get("gates"), list)):
        return []
    return [g for g in data["gates"] if isinstance(g, dict)]


def write_record(record_path, record):
    """Write the review record to *record_path* (replacing any previous
    record for the same deliverable). Raises ConfigError on failure."""
    try:
        with open(record_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except OSError as exc:
        raise ConfigError("could not write record file %s: %s" % (record_path, exc))


# ---------------------------------------------------------------------------
# Second-model review hook (advisory only)
# ---------------------------------------------------------------------------

def second_model_review(report):
    """Return the second-model review note for *report*.

    Uses REVIEW_API_URL / REVIEW_API_KEY / REVIEW_MODEL. If REVIEW_API_URL
    is unset the hook is skipped. The note is advisory only and never
    changes any gate verdict. The API key is never logged.
    """
    url = os.environ.get("REVIEW_API_URL", "").strip()
    if not url:
        return REVIEW_SKIPPED_NOTE
    api_key = os.environ.get("REVIEW_API_KEY", "")
    model = os.environ.get("REVIEW_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

    summary_lines = [
        "Standard: %s" % report["standard"],
        "Deliverable: %s" % report["deliverable"],
        "Gate 1 score: %s / 100 (pass mark %s)"
        % (report["score"], report["pass_score"]),
        "Final verdict: %s" % report["verdict"],
    ]
    for gate in report["gates"]:
        summary_lines.append(
            "- Gate %s: %s — %s"
            % (gate["stage"], gate["verdict"], gate["reason"])
        )
    for item in report["deductions"]:
        summary_lines.append(
            "- Deduction in %s (%s): %s"
            % (item["dimension"], item["check"], item["detail"])
        )
    prompt = (
        "You are reviewing a deliverable that just went through three "
        "enforcement gates (self-check, manager re-check, final review). "
        "Read the summary below and return one short advisory note (no "
        "more than 3 sentences): anything the gates may have missed, or "
        "one concrete improvement. Do not give a numeric score and do not "
        "try to change the verdict.\n\n" + "\n".join(summary_lines)
    )
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REVIEW_TIMEOUT_SECONDS) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"].strip()
        if not content:
            return "second-model review unavailable: empty response"
        return content
    except Exception as exc:  # timeout, HTTP error, bad JSON, network error
        return "second-model review unavailable: %s" % exc


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_text(report):
    """Render the report as a readable gate-by-gate summary."""
    lines = [
        "Enforcement gates: %s" % report["standard"],
        "Deliverable: %s" % report["deliverable"],
        "Final verdict: %s" % report["verdict"],
        "",
        "Gates:",
    ]
    for gate in report["gates"]:
        score = ("score %s" % gate["score"]
                 if gate["score"] is not None else "no score")
        lines.append("  Gate %s (%s): %s — %s"
                     % (gate["stage"], score, gate["verdict"], gate["reason"]))
    if report["deductions"]:
        lines.append("")
        lines.append("Deductions (gate 1):")
        for item in report["deductions"]:
            lines.append("  - %s (%s): %s"
                         % (item["dimension"], item["check"], item["detail"]))
    if report["veto"]:
        lines.append("")
        lines.append("Veto checks (gate 1):")
        for item in report["veto"]:
            mark = "ok" if item["passed"] else "FAIL"
            if item.get("skipped"):
                mark = "SKIP"
            lines.append("  [%s] %s — %s"
                         % (mark, item["id"], item["description"]))
    if report["skipped"]:
        lines.append("")
        lines.append("Skipped:")
        for item in report["skipped"]:
            lines.append("  - %s (%s): %s"
                         % (item["dimension"], item["check"], item["detail"]))
    note = report.get("second_model_review")
    if note:
        lines.append("")
        lines.append("Second-model review (advisory only, does not affect any gate):")
        lines.append("  %s" % note)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the three enforcement gates on a deliverable."
    )
    parser.add_argument("--standard", required=True,
                        help="path to the standard file (.json, .yaml or .yml)")
    parser.add_argument("--deliverable", required=True,
                        help="path to the deliverable file to gate")
    parser.add_argument("--manager-standard",
                        help="path to the manager's standard file; "
                             "gate 2 re-runs the checks with it")
    parser.add_argument("--self-score",
                        help="path to a {\"score\": number} file from an "
                             "earlier self-check run, to use as S1 in gate 2")
    parser.add_argument("--manager-score",
                        help="path to a {\"score\": number} file from an "
                             "earlier manager run, to use as S2 in gate 2")
    parser.add_argument("--record",
                        help="path to a review-record JSON file to update")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                        help="output format (default: text)")
    args = parser.parse_args(argv)

    try:
        standard = load_standard(args.standard)
        manager_standard = (load_standard(args.manager_standard)
                            if args.manager_standard else None)
        self_score = (load_score_file(args.self_score)
                      if args.self_score else None)
        manager_score = (load_score_file(args.manager_score)
                         if args.manager_score else None)
        prior_gates = load_prior_gates(args.record, args.deliverable)
    except ConfigError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2

    record, report = run_all(
        standard, args.deliverable, manager_standard,
        manager_score, self_score, prior_gates)
    report["second_model_review"] = second_model_review(report)

    if args.record:
        try:
            write_record(args.record, record)
        except ConfigError as exc:
            print("Error: %s" % exc, file=sys.stderr)
            return 2

    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_text(report), end="")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
