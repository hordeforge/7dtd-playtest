using System;
using System.Collections.Generic;
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
        Idle,
        WaitReady,
        RunCase,
        Waiting,
        BetweenCases,
        Finished,
    }

    /// <summary>One step in a scripted demo / suite.</summary>
    sealed class CaseDef
    {
        public string Suite;
        public string Id;
        public string[] Tags = Array.Empty<string>();
        /// <summary>If true, record skip immediately (deferred / needs admin / fixture).</summary>
        public bool Deferred;
        public string DeferReason = "";
        public Action<CaseCtx> Act;
        public Func<CaseCtx, bool> Wait;
        public Func<CaseCtx, bool> Assert;
        public float TimeoutSec = 8f;
        public string FailDetail = "timeout";
        /// <summary>Gap between this case and next (demo pacing).</summary>
        public float PauseAfterSec = 0.5f;
    }

    sealed class CaseCtx
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
        static Phase _phase = Phase.Idle;
        static readonly List<CaseDef> _queue = new List<CaseDef>();
        static int _caseIndex = -1;
        static CaseCtx _ctx;
        static float _waitUntil;
        static float _readySince = -1f;
        static bool _readyLogged;
        static int _benchmarkLaps = 1;
        /// <summary>When player is null/dead mid-suite, count unscaled time to avoid hang.</summary>
        static float _playerMissingSince = -1f;

        public static void ArmFromEnv()
        {
            string suiteEnv = Environment.GetEnvironmentVariable("PLAYTEST_SUITE");
            string legacy = Environment.GetEnvironmentVariable("PLAYTEST");
            string laps = Environment.GetEnvironmentVariable("PLAYTEST_LAPS");
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
                + " laps=" + _benchmarkLaps + " v" + ModApi.Version);
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
            _phase = Phase.WaitReady;
            _caseIndex = -1;
            _readySince = -1f;
            _readyLogged = false;
        }

        static void BuildQueue()
        {
            _queue.Clear();
            int laps = 1;
            foreach (var s in _suites)
            {
                if (s == "benchmark")
                    laps = _benchmarkLaps;
            }

            for (int lap = 0; lap < laps; lap++)
            {
                foreach (var s in _suites)
                {
                    int before = _queue.Count;
                    Catalog.AppendSuite(_queue, s, lap);
                    if (_queue.Count == before && s != "benchmark")
                        Report.Info("unknown or empty suite: " + s);
                }
            }
            Report.Info("queue cases=" + _queue.Count);
        }

        public static void Tick()
        {
            if (!_armed || _phase == Phase.Idle || _phase == Phase.Finished) return;

            var gm = GameManager.Instance;
            if (gm == null) return;
            var world = gm.World;
            if (world == null) return;

            // Keep locomotion applied on every gmUpdate while drive is active.
            try { LocomotionDrive.Tick(); } catch { /* */ }

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
            // Finale cases intentionally observe death / drive respawn.
            string curId = (_caseIndex >= 0 && _caseIndex < _queue.Count)
                ? (_queue[_caseIndex].Id ?? "") : "";
            bool deathCase = curId == "player_death_screen" || curId == "player_respawn";
            // Survival claims: do not auto-heal mid-case (would soft-pass death under load).
            bool survivalCase = curId == "bots_plus_playtest" || curId == "soak_15min_host"
                || curId == "soak_still_alive";
            // World-only cases only touch World.GetBlock / TE; allow dead or unspawned.
            bool worldOnlyPersist = curId == "dig_survives_rejoin"
                || curId == "te_survives_rejoin"
                || curId == "blockmeta_survives"
                || curId == "persist_setup_dig"
                || curId == "persist_setup_te"
                || curId == "persist_setup_blockmeta"
                || curId == "persist_setup_done";
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
                    FinishCase(def, "fail", 0f, "exception " + ex.Message);
                    return;
                }

                if (def.Wait != null)
                {
                    _phase = Phase.Waiting;
                    return;
                }

                bool ok = true;
                string detail = _ctx.Detail ?? "";
                if (def.Assert != null)
                {
                    try { ok = def.Assert(_ctx); detail = _ctx.Detail ?? detail; }
                    catch (Exception ex)
                    {
                        ok = false;
                        detail = "assert exception " + ex.Message;
                    }
                }
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
                    FinishCase(def, "fail", elapsed, "wait exception " + ex.Message);
                    return;
                }

                if (done)
                {
                    bool ok = true;
                    string detail = _ctx.Detail ?? "";
                    if (def.Assert != null)
                    {
                        try { ok = def.Assert(_ctx); detail = _ctx.Detail ?? detail; }
                        catch (Exception ex)
                        {
                            ok = false;
                            detail = "assert exception " + ex.Message;
                        }
                    }
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
                    var fi = AccessTools.Field(typeof(GameManager), "myEntityPlayerLocal")
                        ?? AccessTools.Field(typeof(GameManager), "entityPlayerLocal");
                    if (fi != null)
                    {
                        var v = fi.GetValue(gm) as EntityPlayerLocal;
                        if (v != null) return v;
                    }
                    var pi = AccessTools.Property(typeof(GameManager), "myEntityPlayerLocal")
                        ?? AccessTools.Property(typeof(GameManager), "entityPlayerLocal");
                    if (pi != null)
                    {
                        var v = pi.GetValue(gm, null) as EntityPlayerLocal;
                        if (v != null) return v;
                    }
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
                    var fi = AccessTools.Field(ui.GetType(), "entityPlayerLocal")
                        ?? AccessTools.Field(ui.GetType(), "<entityPlayerLocal>k__BackingField");
                    if (fi != null)
                    {
                        var v = fi.GetValue(ui) as EntityPlayerLocal;
                        if (v != null) return v;
                    }
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
            EnsurePlayerHealthy(player);
            // Skip death case's auto-heal until after respawn case? Heal always except death screen act.
            var def = _queue[_caseIndex];
            if (def.Id == "player_death_screen")
            {
                // Leave HP as-is; kill barrier will drop us.
            }
            else
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
