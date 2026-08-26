using System;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// How much of a live player a case needs. The runner's dead/missing-player
    /// recovery consults this per case instead of hard-coded case ids, so a new
    /// death-observing or world-only case is data on the CaseDef.
    /// </summary>
    public enum PlayerGate
    {
        /// <summary>Default: player must be alive with Health &gt; 0 to run.</summary>
        LivePlayer,
        /// <summary>Death/respawn observation: runs as soon as the player is resolvable.</summary>
        AllowDead,
        /// <summary>Touches only World data (blocks / tile entities); runs without a usable player.</summary>
        WorldOnly,
    }

    /// <summary>
    /// One step in a scripted demo / suite. External <see cref="IScenarioProvider"/>
    /// mods should build cases with <see cref="Live"/> / <see cref="Defer"/> rather
    /// than assigning fields by hand.
    /// </summary>
    public sealed class CaseDef
    {
        public string Suite;
        public string Id;
        public string[] Tags = Array.Empty<string>();
        /// <summary>If true, record skip immediately (deferred / needs admin / fixture).</summary>
        public bool Deferred;
        public string DeferReason = "";
        /// <summary>Player gate: how alive the player must be for this case to run.</summary>
        public PlayerGate Gate = PlayerGate.LivePlayer;
        /// <summary>Never force-respawn/heal mid-case (survival claims would soft-pass).</summary>
        public bool NoAutoHeal;
        public Action<CaseCtx> Act;
        public Func<CaseCtx, bool> Wait;
        public Func<CaseCtx, bool> Assert;
        public float TimeoutSec = 8f;
        public string FailDetail = "timeout";
        /// <summary>Gap between this case and next (demo pacing).</summary>
        public float PauseAfterSec = 0.5f;

        /// <summary>
        /// Build a live case (Act → optional Wait → optional Assert). Used by the
        /// built-in catalog and by external scenario providers. <paramref name="tags"/>
        /// is informational only (catalog listing) and may be omitted.
        /// </summary>
        /// <remarks>
        /// Fail-fast at queue build: a case without <paramref name="act"/>,
        /// <paramref name="wait"/>, or <paramref name="assert"/> would record a
        /// green pass while running nothing, so that combination throws
        /// <see cref="ArgumentException"/> naming the case. Exceptions thrown
        /// from the callbacks fail the case (detail names stage + exception
        /// type); they never take down the runner.
        /// </remarks>
        /// <exception cref="ArgumentException">No callback supplied.</exception>
        /// <exception cref="ArgumentOutOfRangeException"><paramref name="timeout"/> ≤ 0.</exception>
        public static CaseDef Live(
            string suite,
            string id,
            string[] tags = null,
            Action<CaseCtx> act = null,
            Func<CaseCtx, bool> wait = null,
            Func<CaseCtx, bool> assert = null,
            float timeout = 8f,
            string fail = "timeout",
            float pause = 0.5f,
            PlayerGate gate = PlayerGate.LivePlayer,
            bool noAutoHeal = false)
        {
            if (act == null && wait == null && assert == null)
                throw new ArgumentException(
                    "CaseDef.Live(" + (suite ?? "") + "/" + (id ?? "")
                    + ") has no act, wait, or assert callback; it would "
                    + "record a pass while running nothing");
            if (!(timeout > 0f))
                throw new ArgumentOutOfRangeException(nameof(timeout), timeout,
                    "CaseDef.Live(" + (suite ?? "") + "/" + (id ?? "")
                    + ") timeout must be > 0 seconds");
            return new CaseDef
            {
                Suite = suite,
                Id = id,
                Tags = tags ?? Array.Empty<string>(),
                Deferred = false,
                DeferReason = "",
                Act = act,
                Wait = wait,
                Assert = assert,
                TimeoutSec = timeout,
                FailDetail = fail,
                PauseAfterSec = pause,
                Gate = gate,
                NoAutoHeal = noAutoHeal,
            };
        }

        /// <summary>
        /// Build a **staging** case: put a scene on screen, announce it, and hold
        /// it still long enough to be photographed.
        ///
        /// <para>This is the shape every visual-evidence fixture needs, and until
        /// now every provider hand-rolled it: emit a bespoke
        /// <see cref="Report.Info"/> line, run a hold timer, assert a flag,
        /// which is how screenshot loops ended up grepping a different sentence
        /// per project. Here it is once: <paramref name="stage"/> does the work
        /// and returns whether the scene really is up; this emits
        /// <see cref="Report.Staged"/> with <paramref name="id"/> the instant it
        /// returns, holds for <paramref name="holdSeconds"/>, and fails the case
        /// if staging did not succeed.</para>
        ///
        /// <para>The assert deliberately only establishes that there was
        /// something to photograph. A staging case must never claim the scene
        /// looked right, because no fixture in this harness can see. Pair it with
        /// <c>scripts/capture_frames.sh</c>, and leave the verdict to a person.</para>
        /// </summary>
        /// <param name="stage">
        /// Stages the scene and returns true when it is genuinely on screen.
        /// Its optional string return is passed to <see cref="Report.Staged"/> as
        /// detail for the human reading the frame. Required: it carries a
        /// default only so <paramref name="tags"/> ahead of it can be omitted,
        /// and omitting it throws at construction rather than holding an empty
        /// scene.
        /// </param>
        /// <param name="tags">Informational only (catalog listing); may be omitted.</param>
        /// <param name="holdSeconds">How long to hold the scene still.</param>
        /// <param name="onHold">
        /// Optional, called every tick of the hold with the fraction elapsed,
        /// 0 to 1. For a scene that must be seen from more than one side: a
        /// single fixed camera photographs one face of a subject and quietly
        /// says nothing about the others, which is how a garment can look
        /// missing for four runs while covering the chest all along. Turn the
        /// subject here and the frames become a turntable.
        ///
        /// A scene that holds genuinely still leaves this null. Throwing from
        /// it does not fail the case: the scene was already staged and the
        /// frames are already being taken, so the fault is logged and the hold
        /// continues rather than discarding evidence that exists.
        /// </param>
        public static CaseDef Staged(
            string suite,
            string id,
            string[] tags = null,
            Func<CaseCtx, bool> stage = null,
            float holdSeconds = 10f,
            string fail = null,
            float pause = 0.5f,
            Action<CaseCtx, float> onHold = null,
            int captureSuperSize = 2)
        {
            if (stage == null)
                throw new ArgumentException(
                    "CaseDef.Staged(" + (suite ?? "") + "/" + (id ?? "")
                    + ") has no stage callback; it would hold an empty scene and "
                    + "record a pass for photographing nothing");
            if (!(holdSeconds > 0f))
                throw new ArgumentOutOfRangeException(nameof(holdSeconds), holdSeconds,
                    "CaseDef.Staged(" + (suite ?? "") + "/" + (id ?? "")
                    + ") holdSeconds must be > 0: a scene nobody can photograph is "
                    + "not evidence");

            return Live(
                suite,
                id,
                tags,
                act: ctx =>
                {
                    bool ok = false;
                    try { ok = stage(ctx); }
                    catch (Exception e)
                    {
                        ctx.Detail = "stage threw: " + e.Message;
                    }
                    ctx.IntA = ok ? 1 : 0;
                    ctx.FloatA = Time.unscaledTime;
                    // Immediately, not at result time: a screenshot loop keyed on
                    // the result photographs the disconnect dialog.
                    Report.Staged(id, ctx.Detail);
                },
                wait: ctx =>
                {
                    float elapsed = Time.unscaledTime - ctx.FloatA;
                    if (onHold != null)
                    {
                        try { onHold(ctx, Mathf.Clamp01(elapsed / holdSeconds)); }
                        catch (Exception e) { Log.Warning("[7dtd-playtest] staged hold callback threw: " + e.Message); }
                    }
                    // Photograph the held scene from inside the game, once, a
                    // little way into the hold so the frame is settled. This is
                    // the whole point of staging: an external desktop grab is
                    // unsound on a host running more than one client, because it
                    // photographs whatever is in front - which has meant another
                    // session's client more than once.
                    if (ctx.IntA == 1 && ctx.IntB == 0 && elapsed >= Mathf.Min(1f, holdSeconds * 0.25f))
                    {
                        ctx.IntB = 1;
                        Helpers.CaptureFrame(id, captureSuperSize);
                    }
                    return elapsed >= holdSeconds;
                },
                assert: ctx => ctx.IntA == 1,
                timeout: holdSeconds + 20f,
                fail: fail ?? ("the " + id + " scene did not stage, so any frames taken during it "
                    + "show something else"),
                pause: pause);
        }

        /// <summary>
        /// Build a **staged clip** case: stage a scene, announce it, and capture
        /// a sampled frame sequence of the hold from inside the game.
        ///
        /// <para>This is <see cref="Staged"/> with the single photograph
        /// replaced by a frame sequence: instead of one
        /// <see cref="Helpers.CaptureFrame"/> a little way into the hold, the
        /// wait callback writes <paramref name="clipFps"/> frames per second of
        /// the hold into <c>playtest-shots/clips/&lt;id&gt;/</c>, and emits
        /// <c>[7dtd-playtest] clip complete &lt;id&gt; frames=N -&gt; playtest-shots/clips/&lt;id&gt;</c>
        /// once the hold ends. <c>scripts/capture_video.sh</c> waits for that
        /// line and muxes the frames.</para>
        /// <param name="clipFps">Frames per second of the hold. The render thread
        /// encodes and flushes at the end of each requested frame, so a cadence
        /// the game cannot keep simply lands fewer frames in the same hold time;
        /// the completion line reports the real count, never a padded one.</param>
        /// <param name="tags">Informational only (catalog listing); may be omitted.</param>
        /// <param name="captureSuperSize">Resolution multiplier, as in
        /// <see cref="Helpers.CaptureFrame"/>.</param>
        /// </summary>
        /// <remarks>
        /// Same boundaries as <see cref="Staged"/>: <paramref name="stage"/>
        /// returns whether the scene is genuinely on screen, the assert only
        /// establishes that there was something to photograph, and
        /// <paramref name="onHold"/> (called every tick with the hold fraction,
        /// throwing does not fail the case) is how a clip becomes more than a
        /// static hold with repeated photographs of the same angle: rotate the
        /// subject here and the frames are a turntable. The verdict belongs to
        /// a person watching the muxed clip, never to the case itself.
        /// </remarks>
        public static CaseDef StagedClip(
            string suite,
            string id,
            string[] tags = null,
            Func<CaseCtx, bool> stage = null,
            float holdSeconds = 10f,
            float clipFps = 4f,
            int captureSuperSize = 2,
            string fail = null,
            float pause = 0.5f,
            Action<CaseCtx, float> onHold = null)
        {
            if (stage == null)
                throw new ArgumentException(
                    "CaseDef.StagedClip(" + (suite ?? "") + "/" + (id ?? "")
                    + ") has no stage callback; it would hold an empty scene and "
                    + "record a pass for photographing nothing");
            if (!(holdSeconds > 0f))
                throw new ArgumentOutOfRangeException(nameof(holdSeconds), holdSeconds,
                    "CaseDef.StagedClip(" + (suite ?? "") + "/" + (id ?? "")
                    + ") holdSeconds must be > 0: a clip nobody can photograph is "
                    + "not evidence");
            if (!(clipFps > 0f))
                throw new ArgumentOutOfRangeException(nameof(clipFps), clipFps,
                    "CaseDef.StagedClip(" + (suite ?? "") + "/" + (id ?? "")
                    + ") clipFps must be > 0");

            return Live(
                suite,
                id,
                tags,
                act: ctx =>
                {
                    bool ok = false;
                    try { ok = stage(ctx); }
                    catch (Exception e)
                    {
                        ctx.Detail = "stage threw: " + e.Message;
                    }
                    ctx.IntA = ok ? 1 : 0;
                    ctx.FloatA = Time.unscaledTime;
                    // Frames number from 0000 into a fixed per-id directory,
                    // so clear any earlier take of this id first: the
                    // completion line names the same path, and stale frames
                    // from a previous run would be muxed and counted as this
                    // take's evidence.
                    Helpers.ResetClipDir(id);
                    // Immediately, not at result time: a collector keyed on the
                    // result photographs the disconnect dialog.
                    Report.Staged(id, ctx.Detail);
                },
                wait: ctx =>
                {
                    float elapsed = Time.unscaledTime - ctx.FloatA;
                    if (onHold != null)
                    {
                        try { onHold(ctx, Mathf.Clamp01(elapsed / holdSeconds)); }
                        catch (Exception e) { Log.Warning("[7dtd-playtest] staged clip hold callback threw: " + e.Message); }
                    }
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
                        // The single, well-defined completion signal a waiting
                        // host process greps for; the count is the real one.
                        Log.Out("[7dtd-playtest] clip complete " + id + " frames=" + ctx.IntB
                            + " -> playtest-shots/clips/" + id);
                    }
                    return done;
                },
                assert: ctx => ctx.IntA == 1,
                timeout: holdSeconds + 20f,
                fail: fail ?? ("the " + id + " scene did not stage, so any clip frames taken during it "
                    + "show something else"),
                pause: pause);
        }

        /// <summary>Build a deferred case (recorded as SKIP with <paramref name="reason"/>).
        /// <paramref name="tags"/> is informational only (catalog listing).</summary>
        public static CaseDef Defer(string suite, string id, string[] tags, string reason)        {
            return new CaseDef
            {
                Suite = suite,
                Id = id,
                Tags = tags ?? Array.Empty<string>(),
                Deferred = true,
                DeferReason = reason ?? "",
                PauseAfterSec = 0.02f,
            };
        }
    }

    public sealed class CaseCtx
    {
        public GameManager Gm;
        public World World;
        public EntityPlayerLocal Player;
        public Vector3 StartPos;
        public Vector3i TargetBlock;
        public int WasBlockType = -1;
        public int PlaceBlockType = -1;
        public int IntA;
        public int IntB;
        public int IntC;
        public float FloatA;
        public float FloatB;
        /// <summary>Optional entity id for combat fixtures (ranged target, etc.).</summary>
        public int TargetEntityId;
        public ulong WorldTime0;
        public float CaseStartUnscaled;
        public string Detail = "";
        public int BenchmarkLap;
    }
}
