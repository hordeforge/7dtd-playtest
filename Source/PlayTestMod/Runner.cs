using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using UnityEngine;

namespace ZdtdPlaytest
{
    enum Phase
    {
        WaitReady,
        RunCase,
        Waiting,
        BetweenCases,
        Finished,
    }

    /// <summary>
    /// The provider-facing case contract lives in <see cref="CaseDef"/> (CaseDef.cs);
    /// this file owns the queue/tick machinery that executes it.
    /// </summary>
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

        /// <summary>Whether the suite has run to completion (aborts any clip a case left recording).</summary>
        public static bool Finished => _phase == Phase.Finished;
        /// <summary>Opt-in per-second spawned-entity render/collision samples.</summary>
        public static bool TraceEntity { get; private set; }
        /// <summary>One-shot guard so a throwing LocomotionDrive.Tick warns once, not per frame.</summary>
        static bool _locomotionFaultLogged;
        static int _benchmarkLaps = 1;
        /// <summary>
        /// Case refs the host's declarative suite declared (PLAYTEST_CASE_REFS).
        /// Empty means no declarative suite: the catalog's own queue runs as-is.
        /// A case whose ref is not declared does not run, so adding a case to
        /// Catalog.cs without declaring it in suites/*.json is inert rather
        /// than a case that silently rides along with someone else's suite.
        /// </summary>
        static readonly HashSet<string> _declaredRefs =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        static readonly HashSet<string> _matchedRefs =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);
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
            string traceEntity = EnvFirst("PLAYTEST_TRACE_ENTITY", "ZDTD_PLAYTEST_TRACE_ENTITY");
            _declaredRefs.Clear();
            _matchedRefs.Clear();
            string declared = EnvFirst("PLAYTEST_CASE_REFS");
            if (!string.IsNullOrEmpty(declared))
            {
                foreach (var r in declared.Split(new[] { ',', ';', ' ' },
                             StringSplitOptions.RemoveEmptyEntries))
                {
                    var trimmed = r.Trim();
                    if (trimmed.Length > 0) _declaredRefs.Add(trimmed);
                }
            }
            TraceEntity = traceEntity == "1"
                || string.Equals(traceEntity, "true", StringComparison.OrdinalIgnoreCase);
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
                + " laps=" + _benchmarkLaps + " trace_entity=" + TraceEntity
                + " v" + ModIdentity.Version);
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
                    KeepDeclared(before);
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

            // A declared ref with no implementation is a harness failure for the
            // same reason a typo'd suite id is: the run must not read as green
            // for a case that never existed.
            foreach (var r in _declaredRefs)
            {
                if (_matchedRefs.Contains(r)) continue;
                Report.Info("declared case ref has no implementation: " + r);
                Report.Result("suite", r, "fail", 0f, "declared case ref has no implementation");
            }
            Report.Info("queue cases=" + _queue.Count
                + (_declaredRefs.Count > 0 ? " declared=" + _declaredRefs.Count : ""));
        }

        /// <summary>
        /// Case ref as the declarative suite names it: <c>catalog.SUITE.CASE</c>,
        /// with the benchmark lap suffix stripped so lap 2 of a case is the same
        /// implementation as lap 1.
        /// </summary>
        static string CaseRef(CaseDef c)
        {
            string suite = c == null ? "" : (c.Suite ?? "");
            int at = suite.IndexOf('@');
            if (at >= 0) suite = suite.Substring(0, at);
            return "catalog." + suite + "." + (c == null ? "" : c.Id);
        }

        /// <summary>
        /// Drop cases appended since <paramref name="from"/> that the host's
        /// suite did not declare. No declared refs means no filtering.
        /// </summary>
        static void KeepDeclared(int from)
        {
            if (_declaredRefs.Count == 0) return;
            for (int i = _queue.Count - 1; i >= from; i--)
            {
                string r = CaseRef(_queue[i]);
                if (_declaredRefs.Contains(r)) { _matchedRefs.Add(r); continue; }
                _queue.RemoveAt(i);
            }
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

    /// <summary>
    /// Scenario runner on GameManager.gmUpdate. Act → Wait → Assert per case.
    /// Suites are named demos (smoke, core, demo, benchmark, …); see SCENARIOS.md.
    /// The on-demand clip recorder ticks on the same hook, so a clip a case
    /// started keeps capturing between case callbacks.
    /// </summary>
    [HarmonyPatch(typeof(GameManager), "gmUpdate")]
    static class Patch_GameManager_PlayTest
    {
        static void Postfix()
        {
            Runner.Tick();
            ClipRecorder.Tick(Runner.Finished);
        }
    }
}
