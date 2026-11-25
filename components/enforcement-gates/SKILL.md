# Enforcement Gates

## What this skill is

A gate that **blocks submission**. A deliverable may not go out until it
has passed three gates in order: self-check, manager re-check, final
review. A gate that fails stops the flow right there — the work goes back
for rework, it is not submitted with a low score attached.

The 100-point score is only the measurement layer inside the gates. It is
not the product. A scorecard tells you how good something is; a gate
decides whether it may leave.

Use this skill whenever work is about to be submitted: finished drafts,
generated files, videos, or anything with a written standard to meet.
Do not use it as a casual quality rating — if nothing is blocked when a
gate fails, you are not using the gates.

## The three gates

### Gate 1 — self-check

Run the deterministic checks from the standard (`gates.py --standard
S.json --deliverable D`). Each dimension scores 0..weight, proportional
to the fraction of its non-skipped checks that passed. The total is
0..100, rounded to 1 decimal.

Gate 1 BLOCKS when either of these is true:

- **A veto check failed.** Veto checks are not part of the score. Each
  veto that fails makes the whole verdict BLOCKED regardless of the
  score. Typical vetoes: the file must exist; the file must not contain a
  placeholder word. A veto is an interception, not a deduction.
- **Score < pass mark.** The default pass mark is 80 (`pass_score` in
  the standard). A score of 79.9 does not pass.

When gate 1 blocks, the run stops. Gates 2 and 3 do not run. Fix the
work and run gate 1 again.

### Gate 2 — manager re-check

An independent second score, S2, is compared with the self-check score
S1. The second score comes from one of, in order of preference:

1. `--manager-standard M.json` — the checks are re-run with the
   manager's standard (usually the same checks with different weights,
   so a disagreement shows up as a score gap).
2. `--manager-score S2.json` — a `{"score": number}` file from an
   earlier manager run.
3. Nothing supplied — the gate-1 score is reused, the gap is 0, and the
   record says so. This keeps the flow moving, but it is not a real
   second opinion: prefer (1) or (2).

S1 is the `--self-score` file when given, otherwise the gate-1 score.
When only one of `--self-score` / `--manager-score` is given, the other
score is the gate-1 score.

**Score-gap rule:** if |S1 − S2| > 10, the work is SENT BACK for rework
(verdict `send_back`, exit code 1). The two scorers disagree too much
for the score to be trusted, so the deliverable is not submitted even
though gate 1 passed. If the gap is 10 or less, gate 2 passes and the
flow proceeds to gate 3. A failed veto in the manager re-run also sends
the work back.

### Gate 3 — final review

Gate 3 checks two things, and both must hold:

1. **The deliverable is actually openable/usable, right now.** It must
   exist and be non-empty. A text file (`.txt`, `.md`) must read as
   UTF-8. A video file (`.mp4`, `.mov`, `.webm`, `.mkv`, `.avi`) must
   open in ffprobe and contain a video stream. Other file types only
   need to exist and be non-empty.
2. **Gate 1 and gate 2 records exist for this deliverable**, both with
   verdict `pass`. A deliverable with no passing gate 1 and gate 2
   cannot pass gate 3, even if the file itself is fine.

If either fails, gate 3 BLOCKS with the reason. When gate 3 passes, the
final verdict is PASS and the work may be submitted.

Note the difference from the scoring checks: gate 3 does **not** skip
when ffprobe is missing. If ffprobe is absent, a video cannot be shown
openable, so gate 3 blocks instead of passing something unverified.
Inside gates 1 and 2, a `video_probe` check with no ffprobe binary is
skipped and excluded from scoring (see below).

## The standard file

JSON is canonical; YAML (`.yaml` / `.yml`) also works when PyYAML is
installed, otherwise the runner tells you to convert to JSON.

```json
{
  "name": "Short article",
  "pass_score": 80,
  "dimensions": [
    {"id": "completeness", "name": "Completeness", "weight": 40,
     "checks": [
       {"type": "text_min_length", "min": 300},
       {"type": "text_contains_all", "keywords": ["Harbor", "lantern"]}
     ]}
  ],
  "veto": [
    {"id": "file-exists", "description": "Deliverable file must exist",
     "check": {"type": "file_exists"}}
  ]
}
```

Rules: `pass_score` defaults to 80. Dimension weights must sum to 100.
Every veto has an `id`, a `description`, and one `check`.

### Check types

| Type | Params | Passes when |
| --- | --- | --- |
| `file_exists` | — | the deliverable path exists and is a file |
| `min_size_bytes` | `min` (int) | file size ≥ `min` bytes |
| `extension` | `allowed` (list, lowercase, no dot) | file extension is in the list |
| `text_min_length` | `min` (int) | file reads as UTF-8 and has ≥ `min` characters |
| `text_contains_all` | `keywords` (list) | every keyword appears (case-insensitive) |
| `text_forbidden_any` | `keywords` (list) | no keyword appears (case-insensitive) |
| `video_probe` | `duration` [min_s, max_s], `resolution` [min_w, min_h], `bitrate_kbps` [min, max] — all optional | ffprobe reports values inside every given range |

Text checks that cannot read the file as UTF-8 fail with a clear reason
instead of crashing. A `video_probe` check runs ffprobe (JSON output)
for duration, width, height and bit rate; **if the ffprobe binary is not
found, the check is marked `skipped` with a note and excluded from
scoring** — it is not failed. A dimension whose checks are all skipped
keeps its full weight, because nothing failed.

## The review record

`--record record.json` writes the review record for the run:

```json
{
  "deliverable": "article.txt",
  "standard": "Short article",
  "gates": [
    {"stage": "self", "score": 100.0, "verdict": "pass",
     "reason": "score 100.0 meets pass mark 80",
     "at": "2026-11-25T05:01:12.438171+00:00"},
    {"stage": "manager", "score": 100.0, "verdict": "pass",
     "reason": "score gap 0.0 within 10 (self 100.0, manager 100.0; ...)",
     "at": "..."},
    {"stage": "final", "score": null, "verdict": "pass",
     "reason": "deliverable openable; gate 1 and gate 2 records present",
     "at": "..."}
  ],
  "final": "pass"
}
```

Gate verdicts are `pass`, `blocked`, or `send_back`; `final` is `pass`
or `blocked`. The timestamp `at` is UTC ISO 8601. The record is written
even for blocked runs, so a block is auditable instead of silent. A
later run replaces the record for the same deliverable. Without
`--record`, the gate-by-gate result is still printed.

## How to run

```bash
# Full three-gate run, manager re-check with its own standard:
python3 gates.py --standard examples/standard-text.example.json \
  --deliverable examples/sample-article.txt \
  --manager-standard examples/standard-manager.example.json \
  --record record.json

# Manager score from an earlier run instead of a re-run:
python3 gates.py --standard S.json --deliverable D \
  --manager-score manager-score.json

# JSON output (the full record object plus scoring detail):
python3 gates.py --standard S.json --deliverable D --format json
```

Exit codes: `0` = PASS (all three gates passed, may submit), `1` =
BLOCKED or SEND BACK (do not submit), `2` = usage/config error (bad
standard, bad score file, missing argument) with a message on stderr.

Text mode prints a gate-by-gate summary: each gate's score, verdict and
reason, then gate-1 deductions, veto results, skipped notes, and the
final verdict. JSON mode prints the full record object plus scoring
detail (`score`, `pass_score`, `dimensions`, `deductions`, `veto`,
`skipped`, `manager`, `score_gap`, `second_model_review`).

## Second-model review hook (advisory only)

Environment variables: `REVIEW_API_URL`, `REVIEW_API_KEY`,
`REVIEW_MODEL` (defaults to `gpt-4o-mini` when unset).

- If `REVIEW_API_URL` is unset, the hook is skipped with the note
  "second-model review skipped: REVIEW_API_URL not set".
- If set, the runner POSTs an OpenAI-compatible chat-completions
  request to it with a short prompt asking for a review note on the
  gate summary. The returned note is included in the report as advisory
  only — **it never changes any gate verdict**.
- It times out after 10 seconds. On any error the report records
  "second-model review unavailable: \<reason\>" and the run continues.
- The API key is never logged. The hook is off by default: nothing is
  sent anywhere unless `REVIEW_API_URL` is set.

## Workflow rules for the operator

1. Never submit work that has not passed all three gates in one run.
2. A blocked gate 1 means fix the work, not the standard. Do not lower
   the pass mark or delete a veto to make a failing deliverable pass.
3. A send-back at gate 2 means the two scorers disagree — rework the
   deliverable (or reconcile the standards), then re-run from gate 1.
4. Keep the record file. If there is no record, the gates did not run.
5. The score is a measurement, not an argument: "it scored 79" is not a
   reason to submit, and "it scored 95" is not a substitute for gate 3.
