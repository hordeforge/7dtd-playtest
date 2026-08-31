using System;
using System.Collections.Generic;
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

        static readonly List<GameObject> StagedObjects = new List<GameObject>();

        /// <summary>
        /// Track a camera-staged instance so the next <see cref="Staged"/> case
        /// (and the end of this hold) destroys it. Prefabs instantiated in the
        /// player's face otherwise pile up at the same spot — a particle system,
        /// then a mesh, then a cube, all occupying one point, which is not a
        /// picture anyone can sign off.
        ///
        /// Do not register a block placed on a voxel or a garment grafted onto
        /// the player: those are the feature under test, not a disposable look
        /// instance.
        /// </summary>
        public static void RegisterStaged(GameObject go)
        {
            if (go != null)
                StagedObjects.Add(go);
        }

        /// <summary>Destroy every object <see cref="RegisterStaged"/> tracked.</summary>
        public static void ClearStaged()
        {
            for (int i = 0; i < StagedObjects.Count; i++)
            {
                var go = StagedObjects[i];
                if (go != null)
                    UnityEngine.Object.Destroy(go);
            }
            StagedObjects.Clear();
        }
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
        ///
        /// <para>One concern per run. Consecutive cases of one feature in
        /// this suite are that concern. A particle system that is already
        /// part of the staged prefab is not a second picture. Do not hang a
        /// second, unrelated prefab in the player's face from a suite that
        /// exists to prove something else, and do not comma-list this suite
        /// with an unrelated one.</para>
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
                    ClearStaged();
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
                    if (elapsed >= holdSeconds)
                    {
                        Helpers.AttachCamera(ctx.Player);
                        ClearStaged();
                        return true;
                    }
                    return false;
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
                    ClearStaged();
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
                        Helpers.AttachCamera(ctx.Player);
                        ClearStaged();
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

        /// <summary>
        /// Build a **walking entity** case: spawn a real entity class beside
        /// the player and drive it walking along the ground for the hold,
        /// recording a frame clip.
        ///
        /// <para>The spawn is the game's own (`SpawnEntityNear` →
        /// <c>EntityFactory.CreateEntity</c> + <c>SpawnEntityInWorld</c>), so the
        /// game grounds the entity with its own physics and casts its shadow —
        /// a spawned animal is never a staged prefab hovering in front of the
        /// camera, which is how a static staged look ends up with its feet
        /// measured against a terrain query that disagrees with the collider.
        /// Each tick the case steps the entity forward along the ground (a
        /// position drive, only in x/z so the game's grounding is preserved),
        /// so an AvatarController that plays clips by motion state —
        /// <c>GameObjectAnimalAnimation</c> plays <c>Walk</c> while the entity's
        /// motion is non-zero — animates the gait. The entity is despawned when
        /// the hold ends.</para>
        ///
        /// <para>The assert establishes that the entity spawned and actually
        /// travelled; it never claims the gait looked right. The verdict is the
        /// muxed clip, and a person's.</para>
        /// </summary>
        public static CaseDef WalkEntity(
            string suite,
            string id,
            string className,
            Vector3 spawnOffset,
            float holdSeconds = 12f,
            float clipFps = 4f,
            float speed = 0.8f,
            int captureSuperSize = 2,
            string fail = null,
            float pause = 0.5f)
        {
            if (string.IsNullOrEmpty(className))
                throw new ArgumentException("CaseDef.WalkEntity(" + (suite ?? "") + "/" + (id ?? "")
                    + ") has no className to spawn");
            if (!(holdSeconds > 0f))
                throw new ArgumentOutOfRangeException(nameof(holdSeconds), holdSeconds,
                    "CaseDef.WalkEntity(" + (suite ?? "") + "/" + (id ?? "")
                    + ") holdSeconds must be > 0");
            bool renderProbeLogged = false;
            float nextTraceAt = 1f;
            return Live(suite, id, new[] { "capture", "clip" },
                act: ctx =>
                {
                    var player = ctx.Player;
                    if (player == null) { ctx.Detail = "no player to spawn beside"; ctx.IntA = -1; return; }
                    // Spawn client-side but mark the entity NON-remote: the
                    // client does not simulate a remote entity (gravity, AI, the
                    // gait animation), and GameObjectAnimalAnimation only plays
                    // for a non-remote one. Setting isEntityRemote=false makes
                    // the client run it like a local entity — it grounds, its AI
                    // wanders and the Walk gait plays. That is exactly what a
                    // server-side spawn would give, without orchestrator plumbing.
                    var spawned = Helpers.SpawnEntityNear(player, className, new Vector3(1.5f, 2f, 1.5f));
                    if (spawned == null) { ctx.Detail = "SpawnEntityNear(" + className + ") returned null"; ctx.IntA = -1; return; }
                    var alive = spawned as EntityAlive;
                    if (alive != null)
                    {
                        try { alive.isEntityRemote = false; } catch (Exception ex) { ctx.Detail = "isEntityRemote set threw: " + ex.Message; }
                    }
                    ctx.IntA = spawned.entityId;
                    ctx.StartPos = spawned.GetPosition();
                    ctx.FloatA = Time.unscaledTime;
                    ctx.FloatB = 0f;
                    ctx.IntB = Mathf.RoundToInt(spawned.GetPosition().y * 100f);
                    // Track the entity's Y spread across the walk so a grounding
                    // regression (legs clipping in, floating, riding too high over
                    // a rise) is a measurable number, not a look.
                    ctx.LongA = (long)(spawned.GetPosition().y * 100f);
                    ctx.LongB = (long)(spawned.GetPosition().y * 100f);
                    // Detach the player's FP camera for the clip: its arm/body
                    // overlay is part of the FP controller composite and no
                    // renderer toggle removes it, so a clip recorded with the
                    // player camera attached photographs the arm, never the
                    // creature. The dedicated capture camera the clip recorder
                    // then captures shows the world (creature + terrain) instead.
                    Helpers.DetachCamera(player);
                    Helpers.BeginClip(id, captureSuperSize, clipFps);
                },
                wait: ctx =>
                {
                    var world = ctx.World;
                    var e = (ctx.IntA <= 0 || world == null)
                        ? null : Helpers.FindAliveById(world, ctx.IntA);
                    float elapsed = Time.unscaledTime - ctx.FloatA;
                    if (e != null)
                    {
                        // Keep the creature framed in the player camera while it
                        // walks. The old wait advanced the creature along its own
                        // facing from a fixed world offset, so it walked off the
                        // player's view and the clip photographed bare terrain —
                        // the "walk" passed its assert (position moved, renderer
                        // present) but was never judgeable, and every grounding
                        // verdict drawn from it was unfounded. Here the creature
                        // is repositioned in front of the camera each tick (yaw
                        // slowly changing, to read as a walk) and grounded onto
                        // the terrain surface, so the clip actually shows it
                        // walking on the ground in frame.
                        try
                        {
                            var player = ctx.Player;
                            // Drive the creature in a slow orbit around its spawn,
                            // grounding it each tick, so the clip reads as a walk
                            // on the terrain instead of a creature flung around by
                            // a camera. Decoupled from the player camera: with the
                            // player's FP camera detached for this clip, its
                            // transform position is not a reliable framing anchor.
                            float yaw = Time.unscaledTime * speed;
                            var pivot = ctx.StartPos;
                            var target = pivot + new Vector3(Mathf.Sin(yaw) * 3.0f, 0f, Mathf.Cos(yaw) * 3.0f);
                            float groundY = 0f;
                            try { groundY = GroundYFor(world, e, target.x, target.z); } catch { }
                            target.y = groundY;
                            try { e.SetPosition(target); } catch (Exception ex) { ctx.Detail = "SetPosition threw: " + ex.Message; }
                            // Frame the camera on the entity's rendered bounds,
                            // not GetPosition(). The mesh renders at
                            // transform.position (≈ GetPosition() − World.Origin);
                            // GetPosition() is the save/world frame, Origin away
                            // from where the mesh draws, so framing that framed
                            // empty terrain ~48 m above the creature. A fixed
                            // world -z camera also failed on uneven terrain: its
                            // ray hit terrainCollider at 4.11 m while the body was
                            // 5.44 m away, so the clip photographed the hill and a
                            // nearby car while the renderer and shader were healthy.
                            // Select a nearby third-person position whose camera
                            // point is unoccupied and whose ray reaches the body.
                            var cp = e.transform.position;
                            Bounds frameBounds;
                            if (!Helpers.TryGetRenderedBounds(e.gameObject, out frameBounds))
                                frameBounds = new Bounds(cp + Vector3.up * 0.5f, Vector3.one);
                            try { Helpers.FrameWorldBounds(player, e.transform, frameBounds); } catch { }
                            // One live sample after the animator has had time to
                            // update. Bounds alone cannot separate a rejected
                            // shader pass, an occluding world prop, and malformed
                            // skinned vertices: all three can leave the renderer
                            // enabled with a healthy serialized AABB while the
                            // clip contains no recognizable creature.
                            if ((!renderProbeLogged && elapsed >= 1f)
                                || (Runner.TraceEntity && elapsed >= nextTraceAt))
                            {
                                if (ReportWalkEntityRenderProbe(id, e, elapsed)) ctx.IntC = 1;
                                renderProbeLogged = true;
                                nextTraceAt = Mathf.Floor(elapsed) + 1f;
                            }
                        }
                        catch (Exception ex) { ctx.Detail = "frame-threw: " + ex.Message; }
                        ctx.FloatB = (e.GetPosition() - ctx.StartPos).magnitude;
                        int yNow = Mathf.RoundToInt(e.GetPosition().y * 100f);
                        ctx.IntB = yNow;
                        if (yNow < ctx.LongA) ctx.LongA = yNow;
                        if (yNow > ctx.LongB) ctx.LongB = yNow;
                    }
                    bool done = elapsed >= holdSeconds;
                    if (done && ctx.IntA > 0 && world != null)
                    {
                        var last = Helpers.FindAliveById(world, ctx.IntA);
                        try
                        {
                            if (last != null) world.RemoveEntityFromMap(last, EnumRemoveEntityReason.Despawned);
                        }
                        catch (Exception ex) { ctx.Detail = "despawn threw: " + ex.Message; }
                    }
                    return done;
                },
                assert: ctx =>
                {
                    // Stop the clip first, then re-attach the player's camera so
                    // the case restores the FP view it replaced.
                    Helpers.EndClip(id);
                    Helpers.AttachCamera(ctx.Player);
                    double yMin = ctx.LongA / 100.0;
                    double yMax = ctx.LongB / 100.0;
                    Report.Info(id + ": spawned_id=" + ctx.IntA
                        + " travelled=" + ctx.FloatB + "m y[" + yMin.ToString("0.00")
                        + ".." + yMax.ToString("0.00") + "] clip=playtest-shots/clips/" + id);
                    // A walk is only a walk if the creature rendered. The old
                    // assert checked only that a spawn returned an id and the
                    // position moved >0.5 m, so a creature that never drew (or
                    // that SetPosition teleported somewhere the client does not
                    // cull-in) still "passed" and the frames showed bare terrain.
                    var alive = ctx.IntA > 0 && ctx.World != null
                        ? Helpers.FindAliveById(ctx.World, ctx.IntA) : null;
                    var meshes = (alive != null)
                        ? alive.GetComponentsInChildren<SkinnedMeshRenderer>(true) : null;
                    // Coordinate-frame divergence + mesh health: is the skinned
                    // mesh actually drawing? A degenerate mesh reports a bounds
                    // AABB at the transform but renders nothing. meshSize ≈ 0
                    // means the skin collapsed; verts tells us the shared mesh is
                    // still there; smrEnabled/meshActive/rootActive say whether
                    // the renderer and its GameObjects are on.
                    try
                    {
                        var smr = meshes != null && meshes.Length > 0 ? meshes[0] : null;
                        Vector3 meshCenter = Vector3.zero, meshSize = Vector3.zero;
                        bool smrEnabled = false, meshActive = false, rootActive = false;
                        int verts = -1;
                        if (smr != null)
                        {
                            meshCenter = smr.bounds.center; meshSize = smr.bounds.size;
                            smrEnabled = smr.enabled;
                            verts = smr.sharedMesh != null ? smr.sharedMesh.vertexCount : -1;
                            meshActive = smr.gameObject != null && smr.gameObject.activeInHierarchy;
                        }
                        if (alive != null) rootActive = alive.transform != null
                            && alive.transform.gameObject.activeInHierarchy;
                        Report.Info(id + ": frame-div getpos=" + (alive != null ? alive.GetPosition().ToString("F2") : "n/a")
                            + " tf=" + (alive != null ? alive.transform.position.ToString("F2") : "n/a")
                            + " meshCenter=" + meshCenter.ToString("F2")
                            + " meshSize=" + meshSize.ToString("F2")
                            + " smrEnabled=" + smrEnabled
                            + " verts=" + verts
                            + " meshActive=" + meshActive
                            + " rootActive=" + rootActive);
                    }
                    catch (Exception ex) { Report.Info(id + ": frame-div threw " + ex.Message); }
                    return ctx.IntA > 0 && ctx.FloatB > 0.5f
                        && meshes != null && meshes.Length > 0 && ctx.IntC == 1;
                },
                timeout: holdSeconds + 25f,
                fail: fail ?? ("the spawned " + className + " entity did not complete its live "
                    + "spawn/render/collision checks; inspect render-probe for posed bounds, "
                    + "shader pass, Physics capsule, active colliders, and collision-ray result"),
                pause: pause);
        }

        static bool ReportWalkEntityRenderProbe(string id, EntityAlive alive, float elapsed)
        {
            if (alive == null)
            {
                Report.Info(id + ": render-probe entity=<null>");
                return false;
            }
            var meshes = alive.GetComponentsInChildren<SkinnedMeshRenderer>(true);
            if (meshes == null || meshes.Length == 0)
            {
                Report.Info(id + ": render-probe smr=<none>");
                return false;
            }
            var colliders = alive.GetComponentsInChildren<Collider>(true);
            int activeColliders = 0;
            int activeSolidColliders = 0;
            string collisionRay = "no-active-solid-collider";
            bool collisionHit = false;
            string physicsCapsule = "<none>";
            for (int i = 0; colliders != null && i < colliders.Length; i++)
            {
                var collider = colliders[i];
                var capsule = collider as CapsuleCollider;
                if (capsule != null && collider != null && collider.name == "Physics")
                {
                    physicsCapsule = "center=" + capsule.center.ToString("F2")
                        + " radius=" + capsule.radius.ToString("0.000")
                        + " height=" + capsule.height.ToString("0.000")
                        + " bottom=" + (capsule.center.y - capsule.height * 0.5f).ToString("0.000")
                        + " enabled=" + capsule.enabled
                        + " active=" + capsule.gameObject.activeInHierarchy;
                }
                if (collider == null || !collider.enabled || !collider.gameObject.activeInHierarchy)
                    continue;
                activeColliders++;
                if (!collider.isTrigger) activeSolidColliders++;
                if (!collisionHit && !collider.isTrigger)
                {
                    RaycastHit colliderHit;
                    var ext = collider.bounds.extents;
                    float reach = Mathf.Max(ext.x, ext.y, ext.z) + 0.35f;
                    var origin = collider.bounds.center + Vector3.up * reach;
                    if (Physics.Raycast(origin, Vector3.down, out colliderHit, reach * 2f))
                    {
                        bool target = colliderHit.transform != null
                            && colliderHit.transform.IsChildOf(alive.transform);
                        collisionRay = (colliderHit.transform != null
                            ? colliderHit.transform.name : "<null>")
                            + "@" + colliderHit.distance.ToString("0.00") + " target=" + target;
                        if (target) collisionHit = true;
                    }
                }
            }
            bool collisionReady = activeSolidColliders > 0 && collisionHit
                && physicsCapsule != "<none>";
            Bounds renderedBounds;
            bool hasRenderedBounds = Helpers.TryGetRenderedBounds(
                alive.gameObject, out renderedBounds);
            float terrainTop = float.NaN;
            float surfaceRay = float.NaN;
            string surfaceHit = "<not-run>";
            float visualBottom = float.NaN;
            float groundClearance = float.NaN;
            bool groundReady = false;
            try
            {
                var world = GameManager.Instance != null ? GameManager.Instance.World : null;
                var absolute = alive.GetPosition();
                if (world != null && hasRenderedBounds)
                {
                    terrainTop = world.GetHeight((int)absolute.x, (int)absolute.z) + 1f;
                    TryGroundSurface(
                        alive, absolute.x, absolute.y, absolute.z,
                        out surfaceRay, out surfaceHit);
                    visualBottom = renderedBounds.min.y + Origin.position.y;
                    float measuredSurface = float.IsNaN(surfaceRay) ? terrainTop : surfaceRay;
                    groundClearance = visualBottom - measuredSurface;
                    groundReady = !float.IsNaN(surfaceRay)
                        && groundClearance >= -0.08f && groundClearance <= 0.20f;
                }
            }
            catch { }
            Vector3 cameraPos, cameraForward;
            bool hasCamera = Helpers.TryGetCaptureCameraPose(out cameraPos, out cameraForward);
            for (int i = 0; i < meshes.Length; i++)
            {
                var smr = meshes[i];
                var material = smr != null ? smr.sharedMaterial : null;
                var shader = material != null ? material.shader : null;
                bool setPass = false;
                string setPassError = "none";
                if (material != null)
                {
                    try { setPass = material.SetPass(0); }
                    catch (Exception ex) { setPassError = ex.GetType().Name; }
                }

                string baked = "n/a";
                Mesh bakedMesh = null;
                try
                {
                    bakedMesh = new Mesh();
                    smr.BakeMesh(bakedMesh);
                    baked = bakedMesh.bounds.ToString("F2") + " v=" + bakedMesh.vertexCount;
                }
                catch (Exception ex) { baked = "error:" + ex.GetType().Name; }
                finally
                {
                    if (bakedMesh != null) UnityEngine.Object.Destroy(bakedMesh);
                }

                string camera = "<none>";
                if (hasCamera)
                {
                    var toMesh = smr.bounds.center - cameraPos;
                    float distance = toMesh.magnitude;
                    float facing = distance > 0.001f
                        ? Vector3.Dot(cameraForward.normalized, toMesh / distance) : 1f;
                    string ray = "clear";
                    RaycastHit hit;
                    if (distance > 0.001f && Physics.Raycast(cameraPos, toMesh / distance, out hit, distance + 0.1f))
                    {
                        bool target = hit.transform != null && hit.transform.IsChildOf(alive.transform);
                        ray = (hit.transform != null ? hit.transform.name : "<null>")
                            + "@" + hit.distance.ToString("0.00") + " target=" + target;
                    }
                    camera = "pos=" + cameraPos.ToString("F2")
                        + " forward=" + cameraForward.ToString("F2")
                        + " distance=" + distance.ToString("0.00")
                        + " facing=" + facing.ToString("0.000")
                        + " ray=" + ray;
                }

                Report.Info(id + ": render-probe t=" + elapsed.ToString("0.00")
                    + " smr=" + (smr != null ? smr.name : "<null>")
                    + " enabled=" + (smr != null && smr.enabled)
                    + " mesh=" + (smr != null && smr.sharedMesh != null ? smr.sharedMesh.name : "<null>")
                    + " meshBounds=" + (smr != null && smr.sharedMesh != null ? smr.sharedMesh.bounds.ToString("F2") : "n/a")
                    + " worldBounds=" + (smr != null ? smr.bounds.ToString("F2") : "n/a")
                    + " bakedBounds=" + baked
                    + " material=" + (material != null ? material.name : "<null>")
                    + " shader=" + (shader != null ? shader.name : "<null>")
                    + " supported=" + (shader != null ? shader.isSupported.ToString() : "n/a")
                    + " passes=" + (shader != null ? shader.passCount.ToString() : "n/a")
                    + " SetPass0=" + setPass
                    + " SetPassError=" + setPassError
                    + " colliders=" + (colliders != null ? colliders.Length : 0)
                    + " active=" + activeColliders
                    + " solid=" + activeSolidColliders
                    + " PhysicsCapsule=" + physicsCapsule
                    + " collisionRay=" + collisionRay
                    + " collisionReady=" + collisionReady
                    + " voxelTop=" + terrainTop.ToString("0.000")
                    + " surfaceRay=" + surfaceRay.ToString("0.000")
                    + " surfaceHit=" + surfaceHit
                    + " voxelMinusSurface=" + (terrainTop - surfaceRay).ToString("0.000")
                    + " visualBottom=" + visualBottom.ToString("0.000")
                    + " groundClearance=" + groundClearance.ToString("0.000")
                    + " groundReady=" + groundReady
                    + " camera=" + camera);
            }
            return collisionReady && groundReady;
        }

        /// <summary>The root Y that puts a spawned entity's feet on the terrain at (x, z).
        /// <para>The engine grounds an entity by the CharacterController capsule
        /// the generated model carries on its `Physics` child node; the capsule's
        /// bottom is `center.y - height/2` below that node, and a generated
        /// creature authors it so the bottom sits at the mesh's feet (a hair
        /// below the root). A downward physics ray gives the actual traversable
        /// surface of slopes and partial blocks; `World.GetHeight(x,z) + 1` is
        /// only the full-voxel fallback. Subtracting the capsule bottom from
        /// that surface puts the capsule, and therefore the authored feet, on
        /// it.</summary>
        static float GroundYFor(World world, EntityAlive alive, float x, float z)
        {
            // GetHeightAt is the terrain generator's uncarved heightmap. It
            // measured world Y 60.05 in a live column whose top voxel face was
            // Y 61, so the old harness forced a healthy creature nearly one
            // full block into the road every tick. GetHeight returns the loaded
            // top block; +1 is its standing surface.
            float voxelTop = world.GetHeight((int)x, (int)z) + 1f;
            float surface;
            string surfaceHit;
            if (!TryGroundSurface(alive, x, alive.GetPosition().y, z, out surface, out surfaceHit))
                surface = voxelTop;
            float capsuleBottom = 0f;
            var colliders = alive != null
                ? alive.GetComponentsInChildren<CapsuleCollider>(true) : null;
            for (int i = 0; colliders != null && i < colliders.Length; i++)
            {
                var capsule = colliders[i];
                if (capsule != null && capsule.name == "Physics")
                {
                    capsuleBottom = capsule.center.y - capsule.height * 0.5f;
                    break;
                }
            }
            return surface - capsuleBottom + 0.01f;
        }

        static bool TryGroundSurface(
            EntityAlive alive,
            float worldX,
            float worldY,
            float worldZ,
            out float surfaceWorldY,
            out string hitName)
        {
            surfaceWorldY = float.NaN;
            hitName = "<none>";
            // Physics and render transforms use rebased coordinates. Cast from
            // well above the candidate column, then convert the hit back to
            // absolute world Y. RaycastAll lets the entity ignore its own
            // bone/capsule colliders when the next orbit sample overlaps it.
            var origin = new Vector3(
                worldX - Origin.position.x,
                worldY - Origin.position.y + 10f,
                worldZ - Origin.position.z);
            RaycastHit[] hits;
            try { hits = Physics.RaycastAll(origin, Vector3.down, 200f, 268500992); }
            catch { return false; }
            if (hits == null || hits.Length == 0) return false;
            Array.Sort(hits, (left, right) => left.distance.CompareTo(right.distance));
            for (int i = 0; i < hits.Length; i++)
            {
                var transform = hits[i].transform;
                if (transform != null && alive != null && transform.IsChildOf(alive.transform))
                    continue;
                surfaceWorldY = hits[i].point.y + Origin.position.y;
                hitName = transform != null ? transform.name : "<null>";
                return true;
            }
            return false;
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
        /// <summary>Min/max Y (×100) reached by a walking entity, to expose a grounding regression as a number.</summary>
        public long LongA;
        public long LongB;
        /// <summary>Optional entity id for combat fixtures (ranged target, etc.).</summary>
        public int TargetEntityId;
        public ulong WorldTime0;
        public float CaseStartUnscaled;
        public string Detail = "";
        public int BenchmarkLap;
    }
}
