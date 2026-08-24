using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Scenario runner on GameManager.gmUpdate. Act → Wait → Assert per case.
    /// Suites are named demos (smoke, core, demo, benchmark, …); see SCENARIOS.md.
    /// </summary>
    [HarmonyPatch(typeof(GameManager), "gmUpdate")]
    static class Patch_GameManager_PlayTest
    {
        static void Postfix()
        {
            Runner.Tick();
        }
    }

    enum Phase
    {
        WaitReady,
        RunCase,
        Waiting,
        BetweenCases,
        Finished,
    }

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
        /// Build a deferred case (recorded as SKIP with <paramref name="reason"/>).
        /// </summary>
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

    static class Runner
    {
        static bool _armed;
        static string[] _suites = Array.Empty<string>();
        static Phase _phase = Phase.Finished;
        static readonly List<CaseDef> _queue = new List<CaseDef>();
        static int _caseIndex = -1;
        static CaseCtx _ctx;
        static float _waitUntil;
        static float _readySince = -1f;
        static bool _readyLogged;
        /// <summary>One-shot guard so a throwing LocomotionDrive.Tick warns once, not per frame.</summary>
        static bool _locomotionFaultLogged;
        static int _benchmarkLaps = 1;
        /// <summary>When player is null/dead mid-suite, count unscaled time to avoid hang.</summary>
        static float _playerMissingSince = -1f;

        /// <summary>
        /// First non-empty environment variable among <paramref name="names"/>.
        /// Supports both canonical <c>PLAYTEST_*</c> and legacy <c>ZDTD_PLAYTEST_*</c>
        /// names used by older host runners.
        /// </summary>
        static string EnvFirst(params string[] names)
        {
            if (names == null) return null;
            foreach (var name in names)
            {
                if (string.IsNullOrEmpty(name)) continue;
                string v = Environment.GetEnvironmentVariable(name);
                if (!string.IsNullOrEmpty(v)) return v;
            }
            return null;
        }

        public static void ArmFromEnv()
        {
            // Canonical: PLAYTEST_SUITE. Legacy/Atomic host: ZDTD_PLAYTEST_SUITE.
            string suiteEnv = EnvFirst("PLAYTEST_SUITE", "ZDTD_PLAYTEST_SUITE");
            string legacy = EnvFirst("PLAYTEST", "ZDTD_PLAYTEST");
            string laps = EnvFirst("PLAYTEST_LAPS", "ZDTD_PLAYTEST_LAPS");
            if (!string.IsNullOrEmpty(laps) && int.TryParse(laps, out int n) && n > 0)
                _benchmarkLaps = Math.Min(n, 20);
            else
                _benchmarkLaps = 1;

            if (!string.IsNullOrEmpty(suiteEnv))
            {
                _suites = Catalog.ExpandSuites(suiteEnv);
                _armed = _suites.Length > 0;
            }
            else if (legacy == "1" || string.Equals(legacy, "true", StringComparison.OrdinalIgnoreCase))
            {
                _suites = Catalog.ExpandSuites("demo");
                _armed = true;
            }
            else
            {
                _armed = false;
                return;
            }

            if (!_armed) return;

            Report.Reset();
            Report.Info("armed suites=" + string.Join(",", _suites)
                + " laps=" + _benchmarkLaps + " v" + ModIdentity.Version);
            if (string.Equals(suiteEnv, "list", StringComparison.OrdinalIgnoreCase)
                || string.Equals(suiteEnv, "catalog", StringComparison.OrdinalIgnoreCase))
            {
                Catalog.LogCatalog();
                Report.Summary(new[] { "catalog" });
                Report.Done();
                _armed = false;
                _phase = Phase.Finished;
                return;
            }

            BuildQueue();
            if (_queue.Count == 0)
            {
                // Every requested suite failed to produce cases (unknown id or
                // uninstalled provider). Finish now with the recorded failures
                // instead of waiting out the join for an empty green pass.
                Report.Summary(_suites);
                Report.Done();
                _armed = false;
                _phase = Phase.Finished;
                return;
            }
            _phase = Phase.WaitReady;
            _caseIndex = -1;
            _readySince = -1f;
            _readyLogged = false;
        }

        static void BuildQueue()
        {
            _queue.Clear();
            int laps = Array.IndexOf(_suites, "benchmark") >= 0 ? _benchmarkLaps : 1;
            var produced = new HashSet<string>();

            for (int lap = 0; lap < laps; lap++)
            {
                foreach (var s in _suites)
                {
                    int before = _queue.Count;
                    Catalog.AppendSuite(_queue, s, lap);
                    if (_queue.Count > before) produced.Add(s);
                }
            }

            // A requested suite that appended nothing is a harness failure, not
            // a green run: typo'd ids and uninstalled providers must stay
            // visible in SUMMARY / DONE exit_hint so hosts can detect them
            // programmatically instead of reading exit 0 for zero work.
            foreach (var s in _suites)
            {
                if (produced.Contains(s)) continue;
                Report.Info("unknown or empty suite: " + s);
                Report.Result(s, "(unknown)", "fail", 0f, "unknown or empty suite");
            }
            Report.Info("queue cases=" + _queue.Count);
        }

        public static void Tick()
        {
            if (!_armed || _phase == Phase.Finished) return;

            var gm = GameManager.Instance;
            if (gm == null) return;
            var world = gm.World;
            if (world == null) return;

            // Keep locomotion applied on every gmUpdate while drive is active.
            // A throwing Tick must not take down the runner, but it also must
            // not vanish: every walk-driven case would then fail with an
            // opaque "position never reached" and no cause. Warn once.
            try { LocomotionDrive.Tick(); }
            catch (Exception ex)
            {
                if (!_locomotionFaultLogged)
                {
                    _locomotionFaultLogged = true;
                    Log.Warning("[7dtd-playtest] locomotion tick failed (further failures silent): " + ex.Message);
                }
            }

            if (_phase == Phase.WaitReady)
            {
                if (!IsPlayReady(gm, world, out var player, out var why))
                {
                    if (Time.frameCount % 300 == 0)
                        Report.Info("wait_ready " + why);
                    return;
                }
                if (_readySince < 0f) _readySince = Time.unscaledTime;
                // Let physics + PlayerMoveController settle after join (motors need this).
                if (Time.unscaledTime - _readySince < 3.0f) return;
                if (!_readyLogged)
                {
                    _readyLogged = true;
                    Report.Info("ready player=" + player.entityId + " pos=" + player.GetPosition());
                }
                AdvanceToNextCase(gm, world, player);
                return;
            }

            if (_ctx == null) return;
            _ctx.Gm = gm;
            _ctx.World = world;
            _ctx.Player = ResolveLocalPlayer(world) ?? _ctx.Player;

            // Death/respawn must not freeze the suite (early return forever).
            // Per-case tolerance is data on the CaseDef (Gate / NoAutoHeal),
            // not id lists here.
            var curDef = (_caseIndex >= 0 && _caseIndex < _queue.Count) ? _queue[_caseIndex] : null;
            bool deathCase = curDef != null && curDef.Gate == PlayerGate.AllowDead;
            // Survival claims: do not auto-heal mid-case (would soft-pass death under load).
            bool survivalCase = curDef != null && curDef.NoAutoHeal;
            // World-only cases only touch World.GetBlock / TE; allow dead or unspawned.
            bool worldOnlyPersist = curDef != null && curDef.Gate == PlayerGate.WorldOnly;
            // IsSpawned() lags after rejoin/heal; Health>0 + entity is enough to run cases.
            bool live = _ctx.Player != null
                && !_ctx.Player.IsDead()
                && _ctx.Player.Health > 0;
            bool playerOk = deathCase
                ? _ctx.Player != null
                : worldOnlyPersist
                    ? _ctx.World != null
                    : live;
            if (!playerOk)
            {
                if (_playerMissingSince < 0f)
                    _playerMissingSince = Time.unscaledTime;
                float missingFor = Time.unscaledTime - _playerMissingSince;

                // Best-effort force respawn after a short settle (not death/survival asserts).
                if (!deathCase && !survivalCase && missingFor > 1.0f)
                {
                    var p = _ctx.Player ?? ResolveLocalPlayer(world);
                    if (p != null)
                    {
                        try
                        {
                            EnsurePlayerHealthy(p);
                            _ctx.Player = p;
                            if (!p.IsDead() && p.Health > 0)
                            {
                                // Healed: clear missing and run case path this tick.
                                _playerMissingSince = -1f;
                                playerOk = true;
                            }
                        }
                        catch { /* API drift */ }
                    }
                    if (!playerOk && missingFor > 3f && missingFor < 3.5f)
                        Report.Barrier("settime_day");
                }

                if (!playerOk)
                {
                    // Mid-case: fail current on timeout so Wait cannot hang past TimeoutSec.
                    if (_phase == Phase.Waiting || _phase == Phase.RunCase)
                    {
                        var def = _queue[_caseIndex];
                        float elapsed = Time.unscaledTime - _ctx.CaseStartUnscaled;
                        float limit = def != null ? Math.Max(def.TimeoutSec, 6f) : 8f;
                        if (deathCase && _ctx.Player != null)
                        {
                            // Continue into Wait/Assert path with dead player.
                        }
                        else if (elapsed >= limit || missingFor >= 12f)
                        {
                            FinishCase(def, "fail", elapsed,
                                "player dead/missing mid-case t=" + missingFor.ToString("0.0"));
                            return;
                        }
                        else
                            return;
                    }
                    else if (_phase == Phase.BetweenCases)
                    {
                        // Wait up to 8s for respawn before aborting the rest.
                        if (missingFor >= 8f)
                        {
                            Report.Info("player dead too long; finishing suite early");
                            // Abort bypasses FinishCase, so stop the drive here too:
                            // the PMC patches key off Active alone and would keep
                            // injecting inputs after the run is over.
                            try { LocomotionDrive.Stop(_ctx?.Player); } catch { /* */ }
                            while (_caseIndex + 1 < _queue.Count)
                            {
                                _caseIndex++;
                                var d = _queue[_caseIndex];
                                Report.Result(d.Suite, d.Id, d.Deferred ? "skip" : "fail", 0f,
                                    d.Deferred ? (d.DeferReason ?? "deferred") : "aborted: player dead");
                            }
                            Report.Summary(_suites);
                            Report.Done();
                            _phase = Phase.Finished;
                            _armed = false;
                        }
                        return;
                    }
                    else
                        return;
                }
            }
            if (playerOk) _playerMissingSince = -1f;

            if (_phase == Phase.BetweenCases)
            {
                if (Time.unscaledTime < _waitUntil) return;
                AdvanceToNextCase(gm, world, _ctx.Player);
                return;
            }

            if (_phase == Phase.RunCase)
            {
                var def = _queue[_caseIndex];
                if (def.Deferred)
                {
                    FinishCase(def, "skip", 0f, def.DeferReason ?? "deferred");
                    return;
                }

                try
                {
                    def.Act?.Invoke(_ctx);
                }
                catch (Exception ex)
                {
                    FinishCase(def, "fail", 0f, DescribeException("act", ex));
                    return;
                }

                if (def.Wait != null)
                {
                    _phase = Phase.Waiting;
                    return;
                }

                bool ok = RunCaseAssert(def, out string detail);
                FinishCase(def, ok ? "pass" : "fail", 0f, detail);
                return;
            }

            if (_phase == Phase.Waiting)
            {
                var def = _queue[_caseIndex];
                float elapsed = Time.unscaledTime - _ctx.CaseStartUnscaled;
                bool done = false;
                try { done = def.Wait(_ctx); }
                catch (Exception ex)
                {
                    FinishCase(def, "fail", elapsed, DescribeException("wait", ex));
                    return;
                }

                if (done)
                {
                    bool ok = RunCaseAssert(def, out string detail);
                    FinishCase(def, ok ? "pass" : "fail", elapsed, detail);
                    return;
                }

                if (elapsed >= def.TimeoutSec)
                {
                    string d = def.FailDetail + " after " + elapsed.ToString("0.0") + "s";
                    if (_ctx != null && !string.IsNullOrEmpty(_ctx.Detail))
                        d = d + " | " + _ctx.Detail;
                    FinishCase(def, "fail", elapsed, d);
                }
            }
        }

        /// <summary>
        /// Invoke the case assert against the live ctx; exceptions fail the case.
        /// </summary>
        static bool RunCaseAssert(CaseDef def, out string detail)
        {
            bool ok = true;
            detail = _ctx.Detail ?? "";
            if (def.Assert == null) return ok;
            try
            {
                ok = def.Assert(_ctx);
                detail = _ctx.Detail ?? detail;
            }
            catch (Exception ex)
            {
                ok = false;
                detail = DescribeException("assert", ex);
            }
            return ok;
        }

        /// <summary>
        /// Case-failure detail for a thrown exception. The type name is the
        /// triage signal: an NRE's Message alone is just "Object reference not
        /// set…", which names neither member nor cause.
        /// </summary>
        static string DescribeException(string stage, Exception ex)
        {
            string msg = ex.Message ?? "";
            return stage + " exception " + ex.GetType().Name
                + (msg.Length > 0 ? ": " + msg : "");
        }

        static void EnsurePlayerHealthy(EntityPlayerLocal p)
        {
            if (p == null) return;
            try
            {
                if (p.IsDead() || p.Health <= 0)
                {
                    try { p.Respawn(RespawnType.Died); } catch { /* */ }
                    try { p.SetAlive(); } catch { /* */ }
                }
                try { p.Health = Math.Max(p.Health, p.GetMaxHealth()); } catch { /* */ }
                try { p.Stamina = Math.Max(p.Stamina, p.GetMaxStamina()); } catch { /* */ }
            }
            catch { /* never break runner */ }
        }

        /// <summary>
        /// Resolve local player. Prefer World.GetPrimaryPlayer, then GameManager
        /// myEntityPlayerLocal, LocalPlayerUI, then player/entity lists.
        /// </summary>
        /// <remarks>
        /// Tick calls this every game frame, and during dead/missing-player windows
        /// the primary-player path misses so the fallbacks run per frame (the same
        /// window where cold joins are CPU-starved). Member lookup is therefore
        /// resolved once and reused, mirroring LocomotionDrive's reflection caches.
        /// </remarks>
        static FieldInfo _gmPlayerField;
        static PropertyInfo _gmPlayerProp;
        static bool _gmMembersResolved;
        static FieldInfo _uiPlayerField;

        static EntityPlayerLocal ResolveLocalPlayer(World world)
        {
            try
            {
                if (world != null)
                {
                    var primary = world.GetPrimaryPlayer() as EntityPlayerLocal;
                    if (primary != null) return primary;
                }
            }
            catch { /* */ }
            try
            {
                // GameManager.myEntityPlayerLocal is the client avatar ref.
                var gm = GameManager.Instance;
                if (gm != null)
                {
                    if (!_gmMembersResolved)
                    {
                        _gmMembersResolved = true;
                        _gmPlayerField = AccessTools.Field(typeof(GameManager), "myEntityPlayerLocal")
                            ?? AccessTools.Field(typeof(GameManager), "entityPlayerLocal");
                        _gmPlayerProp = AccessTools.Property(typeof(GameManager), "myEntityPlayerLocal")
                            ?? AccessTools.Property(typeof(GameManager), "entityPlayerLocal");
                    }
                    var v = _gmPlayerField?.GetValue(gm) as EntityPlayerLocal;
                    if (v != null) return v;
                    v = _gmPlayerProp?.GetValue(gm, null) as EntityPlayerLocal;
                    if (v != null) return v;
                }
            }
            catch { /* */ }
            try
            {
                var ui = LocalPlayerUI.GetUIForPrimaryPlayer();
                if (ui != null)
                {
                    var ep = ui.entityPlayer as EntityPlayerLocal;
                    if (ep != null) return ep;
                    // GetUIForPrimaryPlayer always yields a LocalPlayerUI, so one
                    // lazy resolve covers every call.
                    if (_uiPlayerField == null)
                        _uiPlayerField = AccessTools.Field(ui.GetType(), "entityPlayerLocal")
                            ?? AccessTools.Field(ui.GetType(), "<entityPlayerLocal>k__BackingField");
                    var v = _uiPlayerField?.GetValue(ui) as EntityPlayerLocal;
                    if (v != null) return v;
                }
            }
            catch { /* */ }
            try
            {
                var list = world?.Players?.list;
                if (list != null)
                {
                    for (int i = 0; i < list.Count; i++)
                    {
                        if (list[i] is EntityPlayerLocal epl)
                            return epl;
                    }
                }
            }
            catch { /* */ }
            try
            {
                var ents = world?.Entities?.list;
                if (ents != null)
                {
                    for (int i = 0; i < ents.Count; i++)
                    {
                        if (ents[i] is EntityPlayerLocal epl)
                            return epl;
                    }
                }
            }
            catch { /* */ }
            return null;
        }

        static void AdvanceToNextCase(GameManager gm, World world, EntityPlayerLocal player)
        {
            _caseIndex++;
            if (_caseIndex >= _queue.Count)
            {
                Report.Summary(_suites);
                Report.Done();
                _phase = Phase.Finished;
                _armed = false;
                return;
            }

            player = ResolveLocalPlayer(world) ?? player;
            // Heal between cases, except AllowDead cases: leave HP as-is so the
            // death screen can observe the kill and respawn drives its own flow
            // (its act re-establishes life itself).
            var def = _queue[_caseIndex];
            if (def.Gate != PlayerGate.AllowDead)
                EnsurePlayerHealthy(player);

            _ctx = new CaseCtx
            {
                Gm = gm,
                World = world,
                Player = player,
                StartPos = player != null ? player.GetPosition() : Vector3.zero,
                CaseStartUnscaled = Time.unscaledTime,
                BenchmarkLap = 0,
            };
            // Parse lap from suite name suffix if present (demo@1)
            if (def.Suite != null && def.Suite.Contains("@"))
            {
                var parts = def.Suite.Split('@');
                if (parts.Length == 2 && int.TryParse(parts[1], out int lap))
                    _ctx.BenchmarkLap = lap;
            }
            _phase = Phase.RunCase;
        }

        static void FinishCase(CaseDef def, string status, float elapsedSec, string detail)
        {
            // Never leave motor drive stuck between cases.
            try { LocomotionDrive.Stop(_ctx?.Player); } catch { /* */ }

            float ms = elapsedSec * 1000f;
            if (ms <= 0f && _ctx != null)
                ms = (Time.unscaledTime - _ctx.CaseStartUnscaled) * 1000f;
            Report.Result(def.Suite, def.Id, status, ms, detail ?? "");
            _phase = Phase.BetweenCases;
            // Deferred skips: almost no pause so full catalog finishes quickly.
            float pause;
            if (status == "skip")
                pause = 0.02f;
            else if (def.PauseAfterSec > 0f)
                pause = def.PauseAfterSec;
            else
                pause = 0.4f;
            _waitUntil = Time.unscaledTime + pause;
        }

        static bool IsPlayReady(GameManager gm, World world, out EntityPlayerLocal player, out string why)
        {
            player = null;
            why = "no-player";
            bool started = gm.gameStateManager != null && gm.gameStateManager.IsGameStarted();
            if (!started) { why = "game-not-started"; return false; }

            player = ResolveLocalPlayer(world);
            if (player == null)
            {
                why = "player-null";
                return false;
            }
            // Dead players: try heal so rejoin cold start can proceed.
            if (player.Health <= 0 || player.IsDead())
            {
                try { EnsurePlayerHealthy(player); } catch { /* */ }
            }
            if (player.Health <= 0 || player.IsDead())
            {
                why = "player-dead-hp=" + player.Health;
                return false;
            }

            // IsSpawned() can lag for minutes while the avatar is live and movable.
            // Prefer true spawn; accept live + xui + chunks (connect-style readiness).
            bool spawned = false;
            try { spawned = player.IsSpawned(); } catch { spawned = true; }

            var ui = LocalPlayerUI.GetUIForPrimaryPlayer();
            bool xuiReady = ui != null && ui.xui != null && ui.xui.IsReady;
            int cgo = world.m_ChunkManager != null
                ? world.m_ChunkManager.GetDisplayedChunkGameObjectsCount() : -1;
            int viewDist = GameUtils.GetViewDistance();
            bool fixedSize = world.ChunkCache != null && world.ChunkCache.IsFixedSize;
            int needed = fixedSize ? 0 : Math.Max(0, viewDist * viewDist - 10);
            if (!xuiReady)
            {
                why = "xui-not-ready";
                return false;
            }

            bool chunksOk = cgo >= needed || (cgo >= 8 && player.Health > 0)
                || (fixedSize && player.Health > 0);
            if (!chunksOk)
            {
                why = "cgo=" + cgo + "/" + needed;
                return false;
            }

            // Prefer real IsSpawned (motors need it). Residual suites can start with
            // live-only after a longer grace; gate motors are flaky until spawn is true.
            if (!spawned)
            {
                if (_readySince < 0f) _readySince = Time.unscaledTime;
                float waitUnspawned = 25f;
                if (Time.unscaledTime - _readySince < waitUnspawned)
                {
                    why = "player-unspawned-live t="
                        + (Time.unscaledTime - _readySince).ToString("0");
                    return false;
                }
                why = "ok-unspawned-live";
                return true;
            }
            why = "ok";
            return true;
        }
    }
}
