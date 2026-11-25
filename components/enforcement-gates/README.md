# Enforcement Gates

A gate that **blocks submission** — not a scorecard.

## How this differs from existing self-scoring skills

Self-scoring skills already exist: **affaan-m/ECC agent-self-evaluation**
(5-dimension self-rating, each item needs evidence, 1–5 score cards,
~2M installs) and **johnfkoo951/akm-eval** (AKM Index v1.2, 25 criteria,
100-point weighted, needs evidence). They are self-scoring cards: they
tell you how good something is.

This is different: a gate that blocks submission. A low score does not
just get written down — the work does not go out. Three mechanisms make
it a gate instead of a card:

1. **Three-gate flow** — self-check, then manager re-check, then final
   review. The gates run in order and the flow stops at the first gate
   that does not pass.
2. **Veto interception** — veto items (for example: the file must exist,
   no placeholder text) fail the whole run regardless of the score.
3. **Score-gap send-back** — if the self-check score and the manager
   re-check score disagree by more than 10 points, the work is sent back
   for rework instead of being submitted on a disputed score.

The 100-point score is kept only as the measurement layer inside the
gates, not as the product.

## The three gates

### Gate 1 — self-check

Deterministic checks from a standard file. Each dimension scores
0..weight, proportional to the fraction of its non-skipped checks that
passed; the total is 0..100 (1 decimal). Gate 1 BLOCKS when a veto check
fails **or** the score is below the pass mark (default 80). A blocked
gate 1 stops the run: gates 2 and 3 do not run.

### Gate 2 — manager re-check

An independent second score S2 is compared with the self-check score S1.
S2 comes from `--manager-standard` (re-run the checks with the
manager's standard), from a `--manager-score` file (`{"score": number}`),
or — when nothing is supplied — the gate-1 score is reused (gap 0, and
the record says so). S1 is the `--self-score` file when given, otherwise
the gate-1 score. **If |S1 − S2| > 10, the work is SENT BACK** (verdict
`send_back`, exit 1). A failed veto in the manager re-run also sends the
work back. A gap of 10 or less proceeds to gate 3.

### Gate 3 — final review

Passes only when the deliverable is actually openable/usable right now
(exists, non-empty; text reads as UTF-8; video opens in ffprobe and has
a video stream) **and** gate 1 and gate 2 records with verdict `pass`
exist for this deliverable. Otherwise BLOCKED with the reason. When gate
3 passes, the final verdict is PASS and the work may be submitted.

Note: gate 3 does not skip when ffprobe is missing. If ffprobe is
absent, a video cannot be shown openable, so gate 3 blocks instead of
passing something unverified. (Inside gates 1 and 2, a `video_probe`
check with no ffprobe binary is skipped and excluded from scoring.)

## Standard schema

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

Rules: `pass_score` defaults to 80; dimension weights must sum to 100;
each veto has an `id`, a `description`, and one `check`.

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

Text checks that cannot read the file as UTF-8 fail with a clear reason.
A `video_probe` check runs ffprobe (JSON output); **if the ffprobe
binary is not found, the check is marked `skipped` with a note and
excluded from scoring** — it is not failed. A dimension whose checks are
all skipped keeps its full weight, because nothing failed.

## CLI usage

```bash
# Full three-gate run, manager re-check with its own standard:
python3 gates.py --standard examples/standard-text.example.json \
  --deliverable examples/sample-article.txt \
  --manager-standard examples/standard-manager.example.json \
  --record record.json

# Manager score from an earlier run instead of a re-run:
python3 gates.py --standard S.json --deliverable D \
  --manager-score manager-score.json

# JSON output:
python3 gates.py --standard S.json --deliverable D --format json
```

Exit codes: `0` = PASS (all three gates passed, may submit), `1` =
BLOCKED or SEND BACK (do not submit), `2` = usage/config error (bad
standard, bad score file, missing argument) with a message on stderr.

Text mode prints a gate-by-gate summary (each gate's score, verdict and
reason, then gate-1 deductions, veto results, skipped notes, final
verdict). JSON mode prints the full record object plus scoring detail
(`score`, `pass_score`, `dimensions`, `deductions`, `veto`, `skipped`,
`manager`, `score_gap`, `second_model_review`).

## Examples

- `examples/standard-text.example.json` — short-article standard
  (completeness 40 / formatting 30 / language 30, pass mark 80, two
  vetoes: file must exist, no `PLACEHOLDER`).
- `examples/standard-manager.example.json` — manager variant of the text
  standard with different weights (completeness 50 / formatting 20 /
  language 30), so the score-gap mechanic is demonstrable.
- `examples/standard-video.example.json` — short-video standard
  (duration 1–10 s, minimum 640×360, bitrate 100–5000 kbps, ffprobe).
- `examples/sample-article.txt` — passes the text standard (100).
- `examples/sample-article-short.txt` — fails it (too short, missing a
  required keyword; scores 45).

## Review record

`--record record.json` writes the review record for the run, so the flow
is auditable:

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
or `blocked`; `at` is a UTC ISO 8601 timestamp. The record is written
even for blocked runs. A later run replaces the record for the same
deliverable. Gate 3 also reads matching prior records from an existing
record file for the same deliverable. Without `--record`, the
gate-by-gate result is still printed.

## Second-model review hook (advisory only)

Environment variables: `REVIEW_API_URL`, `REVIEW_API_KEY`,
`REVIEW_MODEL` (defaults to `gpt-4o-mini` when unset). Off by default.

- `REVIEW_API_URL` unset → the hook is skipped with the note
  "second-model review skipped: REVIEW_API_URL not set".
- Set → the runner POSTs an OpenAI-compatible chat-completions request
  with a short prompt asking for a review note on the gate summary. The
  note is included in the report as advisory only — **it never changes
  any gate verdict**.
- 10-second timeout. On any error the report records "second-model
  review unavailable: \<reason\>" and the run continues. The API key is
  never logged.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

Stdlib only, no real network. The video test generates a 2-second
640×360 clip with ffmpeg in a temp dir and scores it against the video
standard; if ffmpeg/ffprobe is missing, that test skips with a clear
message. The hook tests only cover the skipped state and an unreachable
localhost address.

## Limitations

- The gates are only as good as the standard: a weak standard passes
  weak work. Write vetoes for the failures that must never ship, and
  keep the pass mark at 80 or higher.
- Deterministic checks cannot judge taste, correctness of ideas, or
  factual truth — only the measurable properties in the standard. The
  second-model hook is advisory for exactly that reason, and it never
  overrides a gate.
- Gate 2 with no independent second score reuses the gate-1 score (gap
  0). That keeps the flow moving but is not a real second opinion.
- Gate 3 verifies a video is openable via ffprobe, not that it looks
  right; a human still needs to watch it.
- `video_probe` needs ffprobe installed; without it, video checks are
  skipped in gates 1–2 and gate 3 blocks video deliverables.
