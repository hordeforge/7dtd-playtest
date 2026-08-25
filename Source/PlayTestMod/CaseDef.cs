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
        /// now every provider hand-rolled it — emit a bespoke
        /// <see cref="Report.Info"/> line, run a hold timer, assert a flag —
        /// which is how screenshot loops ended up grepping a different sentence
        /// per project. Here it is once: <paramref name="stage"/> does the work
        /// and returns whether the scene really is up; this emits
        /// <see cref="Report.Staged"/> with <paramref name="id"/> the instant it
        /// returns, holds for <paramref name="holdSeconds"/>, and fails the case
        /// if staging did not succeed.</para>
        ///
        /// <para>The assert deliberately only establishes that there was
        /// something to photograph. A staging case must never claim the scene
        /// looked right — no fixture in this harness can see. Pair it with
        /// <c>scripts/capture_frames.sh</c>, and leave the verdict to a person.</para>
        /// </summary>
        /// <param name="stage">
        /// Stages the scene and returns true when it is genuinely on screen.
        /// Its optional string return is passed to <see cref="Report.Staged"/> as
        /// detail for the human reading the frame.
        /// </param>
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
            string[] tags,
            Func<CaseCtx, bool> stage,
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

        /// <summary>Build a deferred case (recorded as SKIP with <paramref name="reason"/>).</summary>
        public static CaseDef Defer(string suite, string id, string[] tags, string reason)
        {
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
