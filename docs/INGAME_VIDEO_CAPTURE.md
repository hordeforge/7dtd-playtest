# In-game video capture: motion evidence beside the still frame

## Status

Implemented (2026-08-25). `CaseDef.StagedClip` (CaseDef.cs, beside `Staged`),
`Helpers.CaptureClipFrame` (Helpers.Ui.cs, beside `CaptureFrame`), the
`clip frame`/`clip complete` log line pair, `scripts/capture_video.sh`,
and the on-demand recorder `ClipRecorder` / `Helpers.BeginClip` / `EndClip`
(recording decoupled from staging, ticked on the same gmUpdate hook) all
shipped. The structural tests in `scripts/test_scenario_provider_surface.py`
pin the public surface and the log contract; `make test` covers the rest
offline. The acceptance boxes below that need a real client stay unchecked:
no live run has been photographed and muxed by a person yet. The cross-repo
consumer of the capability, `7dtd-asset-pipeline`'s
[docs/prds/0002-video-based-asset-review.md](https://github.com/hordeforge/7dtd-asset-pipeline/blob/main/docs/prds/0002-video-based-asset-review.md),
is implemented and generates `StagedClip` cases for assets declared with a
motion kind in the mod's `.shamway.toml` (`[acceptance] motion_kinds`).
Feeds [VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md)
(what a clip is reviewed for) and
[ASSET_VIDEO_FEEDBACK_LOOP.md](ASSET_VIDEO_FEEDBACK_LOOP.md) (what a clip is
used for once reviewed).

## Problem

README's "Visual confirmation" section already establishes the rule for a
single frame, and states the reason plainly: a desktop or window screen grab
is unreliable (the window may be unfocused, occluded, or not mapped, so it
shows a stale or empty frame) and, on a host running more than one client,
unsound (it photographs whatever is in front, which has repeatedly meant
another session's client). `CaseDef.Staged` (CaseDef.cs:139) solves this for
one frame: it calls `Helpers.CaptureFrame` (Helpers.Ui.cs), which uses
Unity's own `ScreenCapture.CaptureScreenshot`, this client process's own
framebuffer, from inside the game.

That fix does not extend to motion. `scripts/capture_frames.sh` is the
existing answer for "more than one frame over a hold" (turntables, a garment
seen from more than one side), and it takes those frames with `spectacle`, a
desktop screenshot tool, cropped afterward with ImageMagick to the client
window's geometry. That is exactly the pattern the same README warns against
one section earlier, for exactly the reason the warning gives: a multi-client
host has more than one window on screen, and a desktop grab shows whichever
one is in front. The script's own comment even acknowledges the adjacent
concern ("whole-desktop shots also catch whatever else is on screen") without
addressing the more serious one (wrong session's window entirely). This has
not caused a documented incident yet only because nothing has said so; the
"another session's client" failure mode named in README's rationale is a
description of what `spectacle -b -n -f` (active/focused window) is exposed
to on this exact host shape.

The gap, concretely: the in-game path proves one moment. The desktop-grab
path proves several moments but is exposed to the same unsoundness the
in-game path exists to avoid. Nothing today gives a turntable, a walk-cycle,
or a timed VFX the same "this is this process's own rendering" guarantee a
single staged frame already has.

## Non-goals

- **No desktop, window, or compositor capture of any kind.** Not `spectacle`,
  not `ffmpeg -f x11grab`/`-f pipewiresrc`, not a capture card, not OBS. Every
  frame in a clip is written by the client process's own `ScreenCapture` call,
  the same primitive the single-shot path already uses.
- **No machine-learning upscaling.** "Super resolution" here means exactly
  what `Helpers.CaptureFrame`'s `superSize` already means: Unity's built-in
  supersampled capture (render at N times the pixels, `superSize=2` is four
  times the pixels), not a trained upscaler. A clip capturing at
  `superSize=2` produces the same kind of frame the existing screenshot
  tooling already produces, just many of them.
- **No real-time-rate video.** A clip is a sampled sequence at a chosen
  cadence (see budget below), not a 30-60fps recording. It answers "does the
  motion look right", not "does it feel smooth at native frame rate";
  smoothness judgements belong to a person playing the build, not a review
  artefact.
- **No change to live-suite capture.** Clips are only ever taken during a
  `CaseDef.Staged`-style hold, the same fixture-not-proof boundary the
  existing single-shot path already draws. Nothing here samples frames during
  ordinary `Live` cases or unattended play.
- **No replacement of `capture_frames.sh` for its current single-frame use.**
  That script and `Helpers.CaptureFrame`'s single in-game shot both still
  work for a case that only needs one photograph. This plan gives the
  multi-frame case an in-game path; whether `capture_frames.sh`'s spectacle
  loop is then retired is a decision for whoever owns that migration, not
  this document.

## Design

### Reuse, not a new capture backend

`Helpers.CaptureFrame(name, superSize)` already does the one thing that
matters: it is this process's own rendering, written by Unity, named and
logged in a fixed, greppable shape. A clip is that same call, made
repeatedly on a timer, into a per-clip subdirectory instead of the flat
`playtest-shots/` folder, so frame files never collide with an ordinary
single-shot capture running elsewhere. This needs one small addition to
`Helpers`, not a new capture API:

```csharp
// Helpers.Ui.cs, beside CaptureFrame
public static string CaptureClipFrame(string clipId, int frameIndex, int superSize = 2)
{
    // Same profile/dir resolution as CaptureFrame, under
    // playtest-shots/clips/<clipId>/frame-XXXX.png instead of the flat folder.
}
```

`CaptureFrame` itself is untouched; every existing single-shot caller is
unaffected.

### `CaseDef.StagedClip`

A new factory beside `CaseDef.Staged` (CaseDef.cs:139), built the same way
`Staged` is built: on top of `Live`, with the same `Report.Staged` marker
emitted the instant staging succeeds (never at result time, for the same
reason the doc comment on `Staged` already gives: a screenshot loop keyed on
the case's result photographs the disconnect dialog, not the scene) and the
same "assert only establishes there was something to photograph" boundary.

```csharp
public static CaseDef StagedClip(
    string suite,
    string id,
    string[] tags,
    Func<CaseCtx, bool> stage,
    float holdSeconds = 10f,
    float clipFps = 4f,
    int captureSuperSize = 2,
    string fail = null,
    float pause = 0.5f,
    Action<CaseCtx, float> onHold = null)
```

Internally this is `Staged`'s `wait` callback with one change: instead of a
single `if (ctx.IntA == 1 && ctx.IntB == 0 && elapsed >= holdSeconds * 0.25f)`
check that fires once, the clip variant tracks a frame counter in `ctx.IntB`
and fires whenever `elapsed >= nextFrameTime`:

```csharp
wait: ctx =>
{
    float elapsed = Time.unscaledTime - ctx.FloatA;
    if (onHold != null) { /* same turn-the-subject hook as Staged */ }
    if (ctx.IntA == 1)
    {
        float interval = 1f / clipFps;
        int due = Mathf.FloorToInt(elapsed / interval);
        while (ctx.IntB < due)
        {
            Helpers.CaptureClipFrame(id, ctx.IntB, captureSuperSize);
            ctx.IntB++;
        }
    }
    bool done = elapsed >= holdSeconds;
    if (done && ctx.IntC == 0)
    {
        ctx.IntC = 1;
        Log.Out("[7dtd-playtest] clip complete " + id + " frames=" + ctx.IntB
            + " -> playtest-shots/clips/" + id);
    }
    return done;
},
```

`onHold`'s existing contract (called every tick with the hold fraction,
throwing does not fail the case, used today to turn a subject into a
turntable) is unchanged and is exactly the mechanism a clip needs to be more
than a static hold with repeated photographs of the same angle: pass an
`onHold` that rotates the subject (or advances a walk/idle animation) across
`holdSeconds`, and the resulting frame sequence is a turntable or a
walk-cycle, taken from inside the game.

`Report.Staged` fires once, at stage time, exactly as it does today; a new
log line marks the frame count only once the hold ends, so a waiting host
process has a single, well-defined completion signal instead of one per
frame (a per-frame log line would be as chatty as the frame count and buys
nothing a completion count doesn't already give).

### Cadence and resolution budget

`ScreenCapture.CaptureScreenshot` encodes and writes at the end of the frame
it was requested on; calling it faster than the game can encode and flush
stalls the render thread, which skews `Time.unscaledTime` and, transitively,
`holdSeconds`. That skew is harmless for correctness (the loop is
wall-clock-driven, so a hitch just means fewer frames land in the same real
time, not a wrong result) but it bounds how aggressive `clipFps` and
`captureSuperSize` can be before a clip stops looking like the thing it is
meant to prove.

| `clipFps` | `superSize` | frames in a 10s hold | per-frame @ crop 1286x992 | clip disk budget |
|---|---|---|---|---|
| 4 (default) | 2 (default) | 40 | ~1-3 MB PNG | ~40-120 MB |
| 8 | 2 | 80 | ~1-3 MB PNG | ~80-240 MB |
| 4 | 1 | 40 | ~0.3-1 MB PNG | ~12-40 MB |
| 12 | 1 | 120 | ~0.3-1 MB PNG | ~36-120 MB |

Default to `clipFps=4, superSize=2`: enough samples over a 10s hold to show
whether a garment clips on a turn or a VFX fires on cue, without asking the
render thread to encode and flush a full-resolution PNG faster than a few
times a second. A case that needs to resolve a fast, specific motion (a
single swing, a short cue) should raise `clipFps` and lower `superSize`
rather than raising both; the table exists so that trade is made
deliberately, not discovered by a disk-full run.

### On-demand recording: `ClipRecorder` / `Helpers.BeginClip` / `EndClip`

`StagedClip` ties a clip to a staged hold. Not every moment worth recording
can be staged: a worn garment needs the player actually walking, a VFX fires
in the world, an item-use animation is the game's own. `ClipRecorder` is the
same frame-sequence guarantee decoupled from staging:

- `Helpers.BeginClip(id, superSize = 2, fps = 4f)` starts a clip; the
  recorder ticks on the same `GameManager.gmUpdate` hook as the scenario
  runner (`Runner.Patch_GameManager_PlayTest.Postfix`), so it captures
  between case callbacks and from any case, `Live` included.
- `Helpers.EndClip(id)` stops it and emits the same
  `clip complete <id> frames=N` line, so `capture_video.sh` muxes the result
  unchanged.
- Frames land in the same `playtest-shots/clips/<id>/frame-XXXX.png` shape
  via `Helpers.CaptureClipFrame`, at the same super-resolution independence
  from the desktop.

Abandonment is explicit: a clip a failed or timed-out case left active is
abandoned when the runner finishes (`clip abandoned <id> frames=N`), and a
hard cap (300 s) abandons a clip nobody stopped. `clip abandoned` is
deliberately not the completion marker, so a collector never muxes a partial
clip as if it were complete.

## Host-side: `scripts/capture_video.sh`

Modeled directly on `scripts/capture_frames.sh`: same suite-run-in-background
shape, same "refuse to start on top of a live run" guard (`pgrep -x` on the
client/server process names, never `-f`), same "keep the client log with the
run" self-containment, same `--runner` contract. It differs only in what it
waits for and what it does once the wait ends:

1. Wait for `clip complete <id>` in the client log (written after this run
   started, same anti-stale-log guard `capture_frames.sh` already uses for
   `scene staged`), rather than sampling on an external timer.
2. Because the write is asynchronous, poll (up to ~1s) for the last expected
   frame file (`frame-<frames-1>.png`) to actually exist on disk before
   treating the clip as complete; the log line names the count, so the last
   index is known without guessing.
3. Mux `playtest-shots/clips/<id>/frame-%04d.png` into
   `<out>/<id>.mp4` with `ffmpeg -framerate <clipFps> -i ... -pix_fmt yuv420p`.
   `ffmpeg` here only ever reads files this process already wrote to disk; it
   never touches a display, a compositor, or a capture device. If `ffmpeg` is
   missing, exit non-zero naming the frame directory as the fallback evidence
   rather than silently shipping only frames with no note (same "prefer
   missing over fakes" posture AGENTS.md already states for this repo).
4. Build a contact sheet from the same frames with `montage`, exactly as
   `capture_frames.sh` already does, so a reviewer who wants a single image
   still gets one.

```bash
./scripts/capture_video.sh --suite <id>
./scripts/capture_video.sh --suite <id> --out ./clips --runner ./my-wrapper.sh
```

`CAPTURE_CLIP_ID` (default: the case id logged in `clip complete`),
`CAPTURE_FPS`, and `CAPTURE_CROP` mirror `capture_frames.sh`'s tuning
environment variables.

## Failure modes

| Condition | Behaviour |
|---|---|
| `stage` never returns true | Same as `Staged` today: case fails, no frames were worth taking |
| `onHold` throws | Logged, hold continues (frames already being taken are not discarded) |
| render thread cannot keep up with `clipFps` | Fewer frames land in `holdSeconds`; `clip complete` reports the real count, never a padded one |
| last frame file missing after the poll window | `capture_video.sh` reports the short count and mux fails loudly rather than muxing a gap as if it were continuous motion |
| `ffmpeg` not installed | Exit non-zero, name the frame directory as the evidence that does exist |
| two staged clips reuse the same `id` in one run | Second `CaptureClipFrame` call overwrites the first's frames (same collision rule flat `playtest-shots/` already has); name clip ids for what they show, same as any other case id |

## Implementation

1. `Helpers.CaptureClipFrame` in `Source/PlayTestMod/Helpers.Ui.cs`, beside
   `CaptureFrame`, with the same profile-resolution and `SafeFileName` reuse.
2. `CaseDef.StagedClip` in `Source/PlayTestMod/CaseDef.cs`, beside `Staged`,
   sharing `Live` the same way `Staged` does.
3. `scripts/capture_video.sh`, copied in shape from `scripts/capture_frames.sh`
   and adapted per Design above.
4. Structural test mirroring `scripts/test_scenario_provider_surface.py`'s
   existing catalog/README/SCENARIOS drift checks: a `StagedClip` case id
   documented in SCENARIOS.md, `clip complete` added to the stable log
   contract table in README.md.
5. One real clip, on a real staged case with an `onHold` turntable, muxed and
   watched by a person, before this is called done. A script that produces a
   technically valid mp4 of the wrong thing is not evidence.

## Acceptance criteria

- [ ] A `StagedClip` case produces N frames written entirely by
  `ScreenCapture.CaptureScreenshot`; no external screenshot tool runs.
- [ ] `capture_video.sh` waits on the `clip complete` marker in a log written
  after its own run started (same anti-stale guard as `capture_frames.sh`).
- [ ] `capture_video.sh` refuses to start over a live client/server, same
  guard, same reason.
- [ ] The muxed clip and its source frames survive in `.local/capture/`
  alongside the run's `client.log`, self-contained the same way a
  `capture_frames.sh` run already is.
- [ ] README's stable log contract table lists `clip complete`.
- [ ] A person watches a real turntable clip of a real staged subject and
  confirms it shows what the case claims it stages.

## Open questions

- Should `capture_frames.sh`'s spectacle-based multi-frame loop be retired
  once `StagedClip` covers its use case, or kept as a fallback for a subject
  that genuinely cannot be staged (a full-screen effect, a loading transition)?
- Does a clip ever need audio muxed in (reusing `capture_audio.sh`'s
  recording for the same hold window), or does
  [VIDEO_MODEL_FEEDBACK.md](VIDEO_MODEL_FEEDBACK.md) treat sight and sound as
  separate reviews, matching the repo's existing separate frame/audio capture
  scripts?
