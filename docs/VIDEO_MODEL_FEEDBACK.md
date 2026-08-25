# Vision-model feedback on staged clips

## Status

Proposal. Specifies an unbuilt `scripts/review_video.py`, an intent/result
schema, and an optional `--attach-reviews` pointer on the host report. No
current code sends a frame or a clip to a model. Modeled directly on the
sibling, also-unbuilt PRD in `7dtd-asset-pipeline`,
[`docs/prds/0001-contextual-model-audio-review.md`](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0001-contextual-model-audio-review.md),
which specifies the same shape for sound. Depends on
[INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md) for the clip itself; feeds
[ASSET_VIDEO_FEEDBACK_LOOP.md](ASSET_VIDEO_FEEDBACK_LOOP.md), whose shamway
counterpart is specified as
[docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md)
in `7dtd-asset-pipeline`.

## Problem

README already states the boundary this plan has to respect: "A suite proves
data, never appearance," and "the verdict on the frame belongs to a person."
That boundary is correct and this plan does not move it. What it addresses is
cost, not authority: a staged clip (turntable, walk-cycle, timed VFX) takes
real time for a person to watch fully, and iteration compounds it. A garment
that clips through a shoulder only on the far side of a turn, a cue that fires
a beat early, a prop that reads wrong only while moving: each of these is
findable by watching, but "watch every clip from every revision, fully,
every time" does not scale the way "read a JSON pass/fail" does.

A vision-capable model can prescreen a clip against explicit context (what
this clip is supposed to show, what to check for) and name concrete moments
worth a person's attention, the same value the audio-review PRD states for
sound: "A model cannot assess that fit from the waveform alone... [but] the
same rising tone may be appropriate for an interface warning and wrong for an
entity-bound, three-dimensional bomb cue." Sight has the same shape: a
turntable is not a used judgement without knowing what it was staged to
prove.

## Non-goals

Same list as the audio-review PRD, restated for sight:

- **No automatic creative approval.** A model verdict cannot mark a clip
  accepted, and cannot satisfy the human-watch gate README already requires.
- **No clip generation or mutation.** This operation critiques a candidate
  clip; capturing one is [INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md),
  a separate step.
- **No frame-only claim if the provider cannot actually receive motion.** A
  provider that only accepts a single still does not meet this capability; a
  claim of "video review" backed by one sampled frame is not honest about
  what was actually judged.
- **No silent upload.** Building, capturing, and ordinary suite runs stay
  offline; nothing here triggers a network call implicitly.
- **No change to a case's PASS/FAIL or the stable log contract.** Model
  review is discoverable, never authoritative; see Design below.
- **No provider-specific result as the stable format.** Vendor payloads may
  be retained as evidence; callers consume one pipeline-owned schema.

## Design

### Command surface

A host script, not a client mod change, since it only ever runs after a
clip already exists on disk:

```bash
uv run scripts/review_video.py <clip-dir> \
    --intent <path> --provider PROVIDER --model MODEL --allow-network --json
```

`<clip-dir>` is exactly what `capture_video.sh` produced: the frame
sequence, the muxed mp4 if `ffmpeg` was available, and the copied
`client.log`. `--intent-text` may supply the same information inline; the
JSON file is the reproducible route, matching the audio-review PRD's own
`--intent`/`--intent-text` split.

### Intent schema

Committed beside the suite definition, or written ad hoc for a one-off
review:

| Field | Meaning |
|---|---|
| `purpose` | What this clip is supposed to demonstrate |
| `subject` | The asset or behavior on screen (a worn garment, a placed prop, a UI row, a VFX) |
| `camera_path` | `fixed`, `turntable`, `walk-cycle`, `first-person`, or a short description |
| `desired_qualities` | Concrete target qualities: proportions, silhouette, timing, readability |
| `avoid` | Failure qualities: clipping, popping, z-fighting, wrong scale, jitter |
| `questions` | Clip-specific concerns the reviewer must answer |
| `suite` / `case` | The 7dtd-playtest suite and case id this clip came from, for traceability |

The command refuses an empty `purpose`, the same refusal the audio-review PRD
specifies for its own intent, and for the same reason: a model told nothing
about what a clip is for cannot tell a reviewer anything actionable about it
either.

### Provider input and frame budget

Providers differ in what they can actually ingest: some accept a video file
directly, most vision-chat APIs cap the number of images per request well
below what a 10s/4fps clip produces (40 frames). The script asks the
provider adapter for its declared limit and samples down to it (even
spacing across the clip, always including the first and last frame) rather
than silently truncating from one end. When frames are dropped to fit a
limit, the evidence document records how many and which sampling was used;
a review that quietly saw only the first eight frames of a forty-frame
turntable is not honest about what it actually judged.

### Result schema

Matches the audio-review PRD's shape, so a caller handling both review kinds
sees one family of results:

- `summary`
- `strengths`
- `issues` (each tied to an approximate timestamp or frame index)
- `recommended_changes`
- `rubric_scores`
- `confidence`
- `limitations`

An honest response may say a property cannot be judged from the sampled
frames alone (motion blur, precise timing between two fast-cut frames);
`limitations` is where that goes, not a guess dressed as a finding.

### Evidence and reproducibility

`--output PATH` (default: `<clip-dir>/review-<provider>-<timestamp>.json`)
writes:

- SHA-256 of every submitted frame/clip file and the intent file;
- provider, model identifier, review timestamp;
- which frames were sampled, if any were dropped to fit a provider limit;
- rubric and prompt versions;
- the structured result, and optionally the raw provider response;
- disclosure confirmation and usage metadata if the provider reports it;
- tool version and parameters, credentials removed.

A later review never overwrites an earlier one by default; disagreement
across repeated reviews is preserved and surfaced, not averaged into false
certainty, matching the audio-review PRD's own rule.

### Provider boundary and credentials

A narrow adapter protocol: capability probe (accepted formats, frame/size
limits), submission of frames or video plus text, structured-response
handling, usage metadata, redaction. Credentials come only from provider
configuration or environment variables, and are never accepted as a command
argument, printed, or written into evidence, matching the audio-review PRD's
rule exactly (and stricter than this repo's own existing
`--telnet-password` argument, which THREAT_MODEL.md already names as R1, a
gap, not a pattern to repeat).

The capability registry (a small module mirroring asset-pipeline's
`capabilities.REGISTRY` shape) reports `unavailable`, `configured`, or `not
probed` without contacting a provider during discovery, `--help`, or an
offline suite run.

### Where this joins the playtesting feedback loop

Three integration points, deliberately the only three:

1. **Convenience chaining.** `make playtest-review-video SUITE=<id>` runs
   `capture_video.sh` then `review_video.py` against the same output
   directory, so a run's clip, its `client.log`, and its review evidence are
   one self-contained folder under `.local/capture/`, matching the
   self-containment `capture_frames.sh` already established for its own
   output.
2. **Discoverability in the report, never authority.** `playtest_run.py`'s
   JSON report gains an optional, additive `visual_reviews` array (evidence
   file paths only, keyed by case id) when `--attach-reviews DIR` is passed.
   No verdict, score, or pass/fail derived from a review ever reaches the
   report; the array exists so a person or an agent auditing a run can find
   the evidence, not so the report can act on it.
3. **Nothing else.** The stable log contract (README's `[7dtd-playtest]`
   line table) is untouched. A case's `PASS`/`FAIL`/`SKIP` is computed
   exactly as it is today. This is the same posture the audio-review PRD
   states for its own gate: "Model review may block promotion only when a
   consuming project explicitly configures that policy. It can never create
   human-acceptance evidence."

## Failure modes

| Condition | Behaviour |
|---|---|
| `--allow-network` absent | Refuse before reading credentials or contacting a provider |
| intent lacks `purpose` | Refuse locally, name the missing field |
| provider/model not configured | Report the capability state and configuration route |
| clip exceeds provider's frame/size limit | Sample down, record what was dropped in the evidence |
| provider cannot ingest actual frames/video | Refuse the adapter; a stills-incapable transcription is not a substitute |
| provider timeout, rate limit, or refusal | Exit non-zero; no partial verdict is preserved as a completed review |
| model returns invalid structure | Preserve a redacted raw response only when requested; fail schema validation |
| usage/cost metadata unavailable | Mark unavailable rather than estimated |
| repeated reviews of the same clip disagree | Preserve each, surface the disagreement |
| model says the clip "looks right" | Record the wording as advisory only; no case's result changes |
| human disagrees with the model | Human sign-off controls acceptance; the disagreement itself is retained as evaluation evidence |

## Implementation

1. Intent/result schema module (`scripts/video_review.py`), offline
   validation and redaction tests, no network dependency to import it.
2. Fake local adapter, proven with a test that the exact sampled frame bytes
   and the complete intent reach the adapter boundary, mirroring the
   audio-review PRD's own first proof step.
3. First real provider adapter, chosen for actual multi-frame or video
   understanding, not a stills-only or transcription-only capability.
4. `capture_video.sh` -> `review_video.py` wiring and the
   `playtest-review-video` make target.
5. `--attach-reviews` on `playtest_run.py`, with a structural test in the
   shape of `scripts/test_report_surface.py`'s existing checks, proving the
   report schema gains only paths, never a verdict field.
6. README section under "Visual confirmation", documenting this as an
   explicitly advisory step, cross-linked from the existing section.
7. Run against one known-good and one deliberately flawed clip (a garment
   clipping on a turn, staged on purpose), then finish with an actual human
   watch. Record where the model's critique matched, missed, or disagreed
   with what the person saw.

## Acceptance criteria

- [ ] A fake adapter test proves the exact sampled frame bytes and complete
  intent reach the provider boundary.
- [ ] At least one real provider reviews a real staged clip and identifies a
  motion-dependent property a single still could not have shown.
- [ ] Output validates against the stable result schema and names
  observations tied to a frame index or timestamp where applicable.
- [ ] Rerunning against a revised clip preserves both hash-addressed evidence
  documents for comparison.
- [ ] No network call occurs without `--allow-network`; credentials never
  appear in stdout, JSON output, logs, or stored evidence.
- [ ] `playtest_run.py --attach-reviews` adds only paths to the report; no
  case's PASS/FAIL changes because a review exists.
- [ ] A human watches a reviewed clip and records whether the model's
  critique matched the experienced motion; only that human review accepts
  anything.

## Open questions

- Same provider as the audio-review PRD chooses, or a different one, given
  video/multi-frame support, structured output, retention controls, and cost
  vary independently across sight and sound?
- Should the versioned rubric live in this repo, or move somewhere shared
  once `7dtd-asset-pipeline`'s `review-video` (specified in its
  [PRD 0002](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md);
  see [ASSET_VIDEO_FEEDBACK_LOOP.md](ASSET_VIDEO_FEEDBACK_LOOP.md)) is built,
  so the two do not drift into incompatible rubrics for the same kind of
  judgement?
- Does `--attach-reviews` belong as a first-class report field eventually, or
  stay a filesystem convention indefinitely? The stable log contract table
  in README.md is intentionally hard to extend; this plan defaults to never
  proposing that extension.
