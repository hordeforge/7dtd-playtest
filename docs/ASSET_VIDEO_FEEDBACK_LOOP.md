# Closing the loop: staged clips into asset iteration and the shamway pipeline

## Status

Implemented (2026-08-25). Both dependencies shipped
([INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md)'s `CaseDef.StagedClip`
and capture script, [VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md)'s
`review_video.py`), and the `7dtd-asset-pipeline` half is implemented too:
`shamway review-video` ([PRD 0002](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md))
reviews an adopted clip through the shared **deadeye** vision-model gateway,
`shamway client capture --clip` adopts the clip directory, and
`[acceptance] motion_kinds` in the mod's `.shamway.toml` generates the
`StagedClip` cases. What has not happened yet is the plan's acceptance
evidence: no end-to-end run on a real, intentionally flawed asset has
compared a model critique against a human watch, and no recurring critique
pattern has been written down in `7dtd-asset-pipeline`'s research notes or
digests. The shamway half of this plan (a `shamway review-video` operation)
is specified as its own PRD in that repository,
[docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md),
following that repo's own PRD template and numbering, kept a separate
document because it lives in a separate repository with its own registries,
gates, and release process.

## Problem

`7dtd-asset-pipeline` (shamway) already produces two kinds of look at a
generated asset, and neither shows it the way a player actually sees it.
`shamway render-icon` is a headless Blender clay render: fast, needs no
editor, but no lighting, no animation, no pose, no engine. A staged in-game
frame (`Helpers.CaptureFrame`, one shot) shows the real engine but only one
angle, one instant. README's own worked example names the failure this
causes: "a garment can look missing for four runs while covering the chest
the whole time" because a fixed camera photographs one face and says nothing
about the rest. A clay render and a single frame both under-show a worn,
placed, or moving asset for the same underlying reason: neither turns.

`CaseDef.StagedClip` (from
[INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md)) and a vision-model review
of the result (from [VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md)) close
that gap for playtest suites in general. This plan is about routing that
specific evidence back to the two places able to act on it: whoever is
iterating the asset, and shamway's own generation pipeline.

## Goals

1. A shamway-authored mesh/prefab asset gets a real in-engine turntable (or,
   for wearables, a walk/idle-cycle) clip, not only a headless clay render or
   a single frame.
2. A vision-model critique of that clip is addressed to the same "does this
   fit its purpose" question `shamway review-audio`
   ([docs/prds/0001-contextual-model-audio-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0001-contextual-model-audio-review.md))
   already asks of sound, with the same advisory, never-auto-accepting
   posture.
3. The critique is traceable to the exact generation parameters (mesh seed,
   shape, size, or the source file's hash) that produced the reviewed
   candidate, so one revision's evidence is comparable to the one it
   replaced.
4. Recurring critique patterns across many reviewed assets become an input to
   shamway's documented defaults and art-direction guidance, not just
   per-asset feedback that is read once and discarded.

## Non-goals

- **No automatic mesh regeneration from model critique.** A person, or an
  agent acting on their behalf, reads `issues`/`recommended_changes` and
  decides the next `shamway generate mesh` call. This is not a closed
  autonomous optimization loop; the same non-goal the audio-review PRD states
  ("No automatic creative approval") applies here without change.
- **No replacement of `render-icon`.** The clay render stays the fast,
  editor-optional icon path. This plan adds a live-engine supplement for
  assets worth a closer look, not a replacement for the cheap default.
- **No change to shamway's offline gates.** `check-mesh`, `validate`, and
  `verify-bundle` run exactly as they do today, before and independent of
  anything in this plan.
- **No shared schema forced onto both repositories today.** `7dtd-playtest`
  and `7dtd-asset-pipeline` converge on the same review *result* shape
  (Design, below) so evidence is comparable, but neither repo takes a
  dependency on the other's code.

## Design

### Where a clip request attaches to shamway's existing flow

`shamway acceptance-provider` already generates a 7dtd-playtest scenario
provider with one case per manifest entry, each loading the asset through
`DataLoader.LoadAsset<T>` in a live client (asset-pipeline README,
["Proving it works"](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/README.md#proving-it-works)).
That generated provider is the one place that already knows,
per asset, its stem and kind. It gains one more manifest field a mesh/prefab
entry can carry: a motion kind for the generated case's `onHold` (`turntable`
| `walk-cycle` | `fixed`, defaulting to `turntable` for a bare mesh/prefab
entry, and to `fixed` for anything world-fixed, such as a terrain
decoration, where a spin would not mean anything). With that field present,
the generated case becomes `CaseDef.StagedClip` instead of a plain `Live`
case; without it, generation is unchanged.

### Where the clip and its review land

Same `.local/capture/<suite>-<stamp>/` convention any `7dtd-playtest` clip
already uses; shamway does not need to know that path's shape. What it needs
is the same "adopt an already-captured artefact" primitive it already has:
`capture.py`'s `record_existing(file, label, observable, root)` hashes and
records a screenshot somebody (or something) else already took, rather than
shamway taking its own. This plan asks for the same shape, one level up: a
`--clip DIR` form that adopts an already-captured `7dtd-playtest` clip
directory (frames, mp4, `client.log`) into shamway's own
`.local/acceptance/` evidence tree, labeled and hash-addressed the same way
`shamway client capture`'s single frames already are.

### Where the model critique lands

This is the payload of the sibling PRD,
[7dtd-asset-pipeline/docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md),
which specifies `shamway review-video` following that repository's PRD
template
([docs/prds/TEMPLATE.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/TEMPLATE.md))
exactly the way
[0001-contextual-model-audio-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0001-contextual-model-audio-review.md)
already does for sound: Problem / Goals / Non-goals / Design / Gates /
Registries / Implementation / Failure modes / Acceptance criteria / Open
questions, with `review_video` added to `operations.OPERATIONS` and
`api._DISPATCH`, an optional video-review provider capability added to
`capabilities.REGISTRY`, and `model-video-review` added to `docs.TOPICS`.

`7dtd-playtest`'s `review_video.py` (from
[VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md)) and shamway's
`review-video` are meant to converge on the same result schema (`summary`,
`strengths`, `issues`, `recommended_changes`, `rubric_scores`, `confidence`,
`limitations`), so a critique produced by either surface is comparable by a
reader who does not care which repository ran it. Shamway's version
additionally carries the asset's generation parameters in its evidence
document (mesh seed/shape/size for a synthesized bundle, or the source
file's SHA-256 for an adopted/external one), because shamway is the one
place that actually knows what produced the candidate; `7dtd-playtest`'s own
evidence has no such field to carry, since it reviews a scene, not a
generation call.

### The iteration loop, concretely

```
shamway generate mesh assets-src/bundle/thing.glb --shape cylinder --size ...
shamway build
shamway acceptance-provider --harness-dll ... --install
shamway script playtest-acceptance          # generates a StagedClip case per manifest entry
                                             # -> .local/capture/<suite>-<stamp>/thing/
shamway client capture --clip .local/capture/<suite>-<stamp>/thing \
    --observable "grip reads at the right thickness through a full turn"
shamway review-video thing --allow-network  # critiques the adopted clip
```

A person (or an agent acting on their behalf) reads `issues` and
`recommended_changes`, adjusts the generation call or the source mesh, and
reruns from `shamway generate mesh`. Each revision's evidence is
hash-addressed and never overwritten, so a reviewer can compare the version
that read as "sunk into the hand" against the one that did not, the same
comparability the audio-review PRD already requires for repeated reviews of
one clip.

### What improves in the pipeline itself, not only per-asset

Once enough reviewed revisions exist for a given generator shape (a
`--shape cylinder` default that repeatedly reads as "too thin at the grip"
at the default `--size` ratio, across several unrelated assets), that
pattern is a candidate for shamway's own documented defaults or its
[art-direction guidance](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/authoring/art-direction.md),
the same way any repeated human-listen finding would inform a future
default. This is a documentation and defaults change, not new code: a
[research note](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/research/README.md)
or a [digest](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/digests/README.md)
is where that pattern gets written down once observed more than once, per
that repository's own conventions; it is out of scope for this plan to
propose changing a specific default, only to name where the evidence to
justify one would come from.

## Failure modes

| Condition | Behaviour |
|---|---|
| Asset has no known generation parameters (hand-authored, `bundle_source = "unity"`) | Critique still recorded; the generation-parameters field in shamway's evidence is honestly empty, never guessed |
| Manifest entry requests a motion kind that does not fit the asset (a world-fixed decoration asked to turntable) | `acceptance-provider` defaults that entry to `fixed` and states why in the generated provider, rather than producing a meaningless spin |
| Two reviews of the same revision disagree | Both kept; disagreement surfaced, never averaged, matching the audio-review PRD's rule |
| Model critique is acted on but the next revision is worse | Both revisions' evidence remain, hash-addressed; nothing here prevents or flags a regression automatically, that judgement stays with the person iterating |
| `shamway review-video` unavailable ([PRD 0002](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md)'s operation not yet built) | The loop still works manually: a person watches the adopted clip directly, same as any staged clip today |

## Implementation

1. In `7dtd-playtest`: ship `CaseDef.StagedClip`
   ([INGAME_VIDEO_CAPTURE.md](INGAME_VIDEO_CAPTURE.md)) and
   `review_video.py` ([VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md)).
   Both are prerequisites; this plan adds nothing to `7dtd-playtest` beyond
   what those two already specify.
2. In `7dtd-asset-pipeline`: its PRD,
   [docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md),
   has landed; next build its `review-video` operation, then the
   `acceptance-provider` manifest's motion-kind field and
   `shamway client capture --clip`.
3. Run the full loop once, end to end, on a real modlet asset with a known
   defect (an intentionally clipping garment, matching README's own worked
   example), and record whether the model's critique named the same problem
   a human found watching the same clip. That comparison is this plan's
   acceptance evidence, not a checkbox.

## Acceptance criteria

- [ ] Goal 1: a `shamway`-generated asset's acceptance-provider case captures
  a real in-engine `StagedClip`, not only a clay render or a single frame.
- [ ] Goal 2: `shamway review-video`'s result schema matches
  `review_video.py`'s, verified by a structural test comparing both against
  one shared schema fixture.
- [ ] Goal 3: shamway's evidence document for a reviewed clip names the exact
  generation parameters (or source hash) that produced the candidate.
- [ ] Goal 4: at least one recurring critique pattern, observed across two or
  more unrelated assets, is written down in
  `7dtd-asset-pipeline/docs/research/` or a digest, citing the reviews that
  established it.
- [ ] The loop runs end to end on one real, intentionally flawed asset, and a
  human confirms the model's critique named the real defect.

## Open questions

- Does the acceptance-provider manifest's motion-kind field belong to
  shamway (asset-owned) or to a separate `7dtd-playtest`-owned suite config,
  given the two repositories do not share a schema today and this plan
  deliberately avoids forcing one?
- Should `shamway review-video` accept only an already-muxed mp4, or also a
  raw frame directory, given provider capability for actual video ingestion
  varies and `7dtd-playtest`'s own clip directory always has both?
- Now that
  [docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md)
  exists in `7dtd-asset-pipeline`, should its rubric be versioned jointly
  with `7dtd-playtest`'s, or independently, given the two repos ship on
  separate schedules?
