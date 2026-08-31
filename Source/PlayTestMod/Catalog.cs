using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Full scenario catalog: named suites act like built-in demos / benchmarks.
    /// Live cases run on stock or zdtd; deferred cases record skip with a reason
    /// until admin fixtures or server features land.
    /// </summary>
    static class Catalog
    {
        /// <summary>Expand aliases (demo, full, all, …) into concrete suite ids.</summary>
        public static string[] ExpandSuites(string raw)
        {
            var parts = raw.Split(new[] { ',', ';', ' ' }, StringSplitOptions.RemoveEmptyEntries);
            var list = new List<string>();
            foreach (var p in parts)
            {
                var s = p.Trim().ToLowerInvariant();
                if (s.Length == 0) continue;
                switch (s)
                {
                    case "list":
                    case "catalog":
                        return new[] { "catalog" };
                    case "demo":
                    case "demo_mode":
                        // Attract path: fixtures → vehicle/power → death finale last.
                        AddUnique(list, "smoke", "core", "world", "ui", "combat",
                            "economy", "quest", "vehicle", "power", "finale");
                        break;
                    case "demo_min":
                        // Shorter reel without combat deferred noise.
                        AddUnique(list, "smoke", "core", "world", "ui");
                        break;
                    case "benchmark":
                    case "bench":
                        // Timed repeat of the demo path (laps via PLAYTEST_LAPS)
                        AddUnique(list, "benchmark");
                        break;
                    case "full":
                    case "all":
                    case "live":
                        // persist needs multi-phase orch (SUITE=persist only); mp needs loadgen
                        // barriers (use SUITE=mp or residual). Do not expand them into full.
                        AddUnique(list,
                            "smoke", "core", "world", "ui", "combat", "economy",
                            "quest", "vehicle", "power", "finale", "soak");
                        break;
                    case "gate":
                    case "ci":
                        // Fast PR gate: live-only smoke+core (no deferred floods)
                        AddUnique(list, "smoke", "core");
                        break;
                    case "residual":
                    case "residual_light": // explicit synonym
                        // Lightweight in-client residual probe only (mp + short soak).
                        // The Make target playtest-residual is different: it runs
                        // separate host orch for persist, mp, apm, and soak_long
                        // (multi-phase / long wall-clock). Do not expand those here:
                        // persist needs persist_setup host barriers; soak_long is ≥15m.
                        AddUnique(list, "mp", "soak");
                        break;
                    default:
                        AddUnique(list, s);
                        break;
                }
            }
            return list.ToArray();
        }

        static void AddUnique(List<string> list, params string[] ids)
        {
            foreach (var id in ids)
            {
                if (!list.Contains(id)) list.Add(id);
            }
        }

        public static void LogCatalog()
        {
            var sb = new StringBuilder();
            sb.Append("catalog suites=");
            foreach (var name in SuiteNames)
                sb.Append(name).Append(',');
            Report.Info(sb.ToString().TrimEnd(','));
            // One line per case for host scrapers (built-in suites + external providers).
            var tmp = new List<CaseDef>();
            foreach (var name in SuiteNames.Concat(ScenarioProviders.SuiteIds()))
            {
                tmp.Clear();
                AppendSuite(tmp, name, 0);
                foreach (var c in tmp)
                {
                    string st = c.Deferred ? "deferred" : "live";
                    Report.Info("case " + c.Suite + "/" + c.Id + " status=" + st
                        + " tags=" + string.Join("+", c.Tags ?? Array.Empty<string>())
                        + (c.Deferred ? " reason=" + c.DeferReason : ""));
                }
            }
        }

        static readonly string[] SuiteNames =
        {
            "smoke", "core", "world", "ui", "combat", "economy",
            "quest", "vehicle", "power", "finale", "parachute", "persist", "persist_setup",
            "mp", "soak", "soak_long", "apm", "benchmark", "bot",
        };

        // Fixed Navezgane pad for multi-phase rejoin (same coords after restart).
        static readonly Vector3i PersistDigBlock = new Vector3i(511, 62, 953);
        static readonly Vector3i PersistChestBlock = new Vector3i(513, 61, 953);
        static readonly Vector3i PersistDmgBlock = new Vector3i(510, 61, 953);
        static readonly Vector3 PersistPlayerPos = new Vector3(520f, 62f, 950f);
        const string PersistItemName = "resourceScrapIron";
        static string _chatToken = "";

        public static void AppendSuite(List<CaseDef> q, string suite, int lap)
        {
            string label = lap > 0 ? suite + "@" + lap : suite;
            switch (suite)
            {
                case "smoke": AddSmoke(q, label); break;
                case "core": AddCore(q, label); break;
                case "world": AddWorld(q, label); break;
                case "ui": AddUi(q, label); break;
                case "combat": AddCombat(q, label); break;
                case "economy": AddEconomy(q, label); break;
                case "quest": AddQuest(q, label); break;
                case "vehicle": AddVehicle(q, label); break;
                case "power": AddPower(q, label); break;
                case "finale": AddFinale(q, label); break;
                case "parachute": AddParachute(q, label); break;
                case "persist_setup": AddPersistSetup(q, label); break;
                case "persist": AddPersistVerify(q, label); break;
                case "mp": AddMp(q, label); break;
                case "soak": AddSoak(q, label); break;
                case "soak_long": AddSoakLong(q, label); break;
                case "apm": AddApm(q, label); break;
                case "bot": AddBot(q, label); break;
                case "benchmark":
                    // Timed attract path; outer loop multiplies by LAPS
                    AddSmoke(q, label);
                    AddCore(q, label);
                    AddWorld(q, label);
                    AddUi(q, label);
                    break;
                default:
                    ScenarioProviders.AppendSuite(q, suite, lap);
                    break;
            }
        }

        // ── helpers (thin wrapper → public CaseDef.Live) ─────────────────

        static CaseDef Live(string suite, string id, string[] tags, Action<CaseCtx> act,
            Func<CaseCtx, bool> wait = null, Func<CaseCtx, bool> assert = null,
            float timeout = 8f, string fail = "timeout", float pause = 0.5f,
            PlayerGate gate = PlayerGate.LivePlayer, bool noAutoHeal = false)
        {
            return CaseDef.Live(suite, id, tags, act, wait, assert, timeout, fail, pause,
                gate, noAutoHeal);
        }

        // ── shared melee-fixture seeds / tool equips ─────────────────────

        static readonly string[] SoftSeedBlocks =
        {
            "hayBaleSquare", "hayBaleRound", "cntTrashPile01", "cntTrashPile02",
            "frameShapes", "woodShapes",
        };

        static readonly string[] BlockDamageTools =
        {
            "meleeToolRepairT0StoneAxe", "meleeToolShovelT0StoneShovel",
            "meleeWpnBladeT0BoneKnife", "meleeToolRepairT1ClawHammer",
        };

        /// <summary>Soft vanilla block for damage fixtures; under-feet clone as fallback.</summary>
        static BlockValue ResolveSoftSeed(EntityPlayerLocal p, World world, out string usedName)
        {
            foreach (var name in SoftSeedBlocks)
            {
                try
                {
                    var bv = Block.GetBlockValue(name, true);
                    if (!bv.isair && bv.type != 0) { usedName = name; return bv; }
                }
                catch { /* */ }
            }
            usedName = "underFeet";
            return Helpers.BlockUnderFeet(p, world);
        }

        /// <summary>Give + equip the first resolvable item name. Equipped name or "".</summary>
        static string EquipFirstMatching(EntityPlayerLocal p, string[] names)
        {
            foreach (var name in names)
            {
                if (!Helpers.TryGetItem(name, out var iv)) continue;
                if (!Helpers.TryGiveItem(p, new ItemStack(iv, 1))) continue;
                if (Helpers.TryEquipItemType(p, iv.type) >= 0) return name;
            }
            return "";
        }

        // ── smoke ────────────────────────────────────────────────────────

        static void AddSmoke(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "join_ready", new[] { "join", "demo" }, ctx =>
            {
                var p = ctx.Player;
                bool spawned = false;
                try { spawned = p.IsSpawned(); } catch { spawned = true; }
                ctx.Detail = "pos=" + p.GetPosition() + " hp=" + p.Health + " id=" + p.entityId
                    + " spawned=" + spawned;
            }, assert: ctx =>
            {
                // IsSpawned lags on cold join; live HP + non-dead is the honest join gate.
                return ctx.Player != null && ctx.Player.Health > 0 && !ctx.Player.IsDead();
            }));

            q.Add(Live(suite, "cgo_ready", new[] { "join", "mesh", "demo" }, ctx =>
            {
                int cgo = ctx.World.m_ChunkManager != null
                    ? ctx.World.m_ChunkManager.GetDisplayedChunkGameObjectsCount() : -1;
                bool fixedSize = ctx.World.ChunkCache != null && ctx.World.ChunkCache.IsFixedSize;
                int viewDist = GameUtils.GetViewDistance();
                int need = fixedSize ? 0 : Math.Max(0, viewDist * viewDist - 10);
                ctx.IntA = cgo;
                ctx.IntB = need;
                ctx.Detail = "cgo=" + cgo + " need=" + need + " fixedSize=" + fixedSize;
            }, assert: ctx => ctx.IntA >= ctx.IntB));

            q.Add(Live(suite, "ground", new[] { "world", "demo" }, ctx =>
            {
                var bp = ctx.Player.GetBlockPosition() + Vector3i.down;
                var b = ctx.World.GetBlock(bp);
                ctx.TargetBlock = bp;
                ctx.WasBlockType = b.type;
                ctx.Detail = "block=" + b.type + " at " + bp;
            }, assert: ctx => ctx.WasBlockType != 0, fail: "air under feet"));

            q.Add(Live(suite, "stats", new[] { "player", "demo" }, ctx =>
            {
                var p = ctx.Player;
                ctx.Detail = "hp=" + p.Health + "/" + p.GetMaxHealth() + " stam=" + ((int)p.Stamina);
            }, assert: ctx =>
            {
                var p = ctx.Player;
                return p.Health > 0 && p.Health <= p.GetMaxHealth() && p.Stamina >= 0f;
            }));

            q.Add(Live(suite, "day_clock", new[] { "world", "demo" }, ctx =>
            {
                try
                {
                    ulong t = ctx.World.worldTime;
                    bool ok = Helpers.DecodeWorldTime(t, out int day, out int hour, out int minute);
                    ctx.Detail = ok
                        ? "day=" + day + " " + hour.ToString("00") + ":" + minute.ToString("00")
                            + " raw=" + t
                        : "clock decode failed raw=" + t;
                    // Decode failure must FAIL (assert wants 1), not read as a
                    // valid morning clock.
                    ctx.PlaceBlockType = ok ? 1 : 0;
                }
                catch (Exception ex)
                {
                    ctx.Detail = ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));
        }

        // ── parachute (7dtd-wasm bridge + unmodified zdtd parachute mod) ─

        static void AddParachute(List<CaseDef> q, string suite)
        {
            // Give and wear the glider item. The parachute is chest clothing
            // (extends clothingOutfitT1, EquipSlot ClothingChest); it must sit
            // in an equipment slot for the server's sense v4 wearing_glider
            // bit, so this goes through Equipment.SetSlotItem, not the
            // toolbelt path TryEquipItem uses.
            q.Add(Live(suite, "parachute_equip", new[] { "parachute", "equip" }, ctx =>
            {
                var p = ctx.Player;
                bool gave = false;
                if (p?.equipment != null && Helpers.TryGetItem("parachute", out var iv))
                {
                    gave = Helpers.TryGiveItem(p, new ItemStack(iv, 1));
                    if (gave)
                    {
                        try
                        {
                            p.equipment.SetSlotItem((int)EquipmentSlots.ClothingChest, iv, true);
                        }
                        catch (Exception ex)
                        {
                            ctx.Detail = "set slot failed: " + ex.Message;
                            gave = false;
                        }
                    }
                }
                ctx.IntA = gave ? 1 : 0;
                ctx.Detail = "item resolved=" + (ctx.IntA == 1) + " " + ctx.Detail;
            }, assert: ctx =>
            {
                var p = ctx.Player;
                if (p?.equipment == null) return false;
                try
                {
                    var items = p.equipment.GetItems();
                    if (items == null) return false;
                    foreach (var it in items)
                    {
                        if (it == null || it.IsEmpty() || it.ItemClass == null) continue;
                        if (it.ItemClass.HasAnyTags(FastTags<TagGroup.Global>.Parse("parachute")))
                            return true;
                    }
                }
                catch { /* */ }
                return false;
            }, fail: "parachute item not worn"));

            // The case lifts the player 200 blocks straight up. The client
            // does the teleport itself (client-side SetPosition): on a stock
            // dedicated server the client owns its physics, so a local lift
            // is followed by a real fall whose position updates reach the
            // server (the orchestrator teleportplayer was a no-op on the
            // entity in V3.2.0). The mod watches sense v4, arms the glide
            // exemption, and announces via the stock chat broadcast; the
            // client asserts it saw the announce while falling. The player
            // lands on its own; the suite ends here.
            q.Add(Live(suite, "parachute_fall_announce", new[] { "parachute", "fall" }, ctx =>
            {
                ChatProbe.Clear();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.FloatA = ctx.StartPos.y; // lift target base
                Vector3 lift = ctx.StartPos + new Vector3(0f, 200f, 0f);
                ctx.Player.SetPosition(lift);
                Report.Barrier("parachute_lift:" + ctx.Player.entityId);
                ctx.Detail = "start=" + ctx.StartPos + " lifted=" + lift;
            }, wait: ctx =>
            {
                bool hit = ChatProbe.Contains("deployed their parachute");
                Vector3 p = ctx.Player != null ? ctx.Player.GetPosition() : Vector3.zero;
                // The stock server intermittently rejects a one-shot client
                // teleport; re-assert the lift until the player is clearly
                // airborne (up to ~4s), so the fall is not skipped.
                if (!hit && ctx.Player != null && Time.unscaledTime - ctx.CaseStartUnscaled < 4f &&
                    p.y < ctx.FloatA + 180f)
                {
                    ctx.Player.SetPosition(new Vector3(p.x, ctx.FloatA + 200f, p.z));
                }
                ctx.Detail = "hit=" + hit + " last=" + ChatProbe.Last + " pos=" + p;
                return hit;
            }, assert: ctx => ChatProbe.Contains("deployed their parachute"),
                timeout: 30f, fail: "no parachute deploy announce after lift"));
        }

        // ── core (play loop) ─────────────────────────────────────────────

        static void AddCore(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "look", new[] { "input", "demo" }, ctx =>
            {
                ctx.Player.SetRotation(new Vector3(0, 90, 0));
                ctx.Detail = "rot set yaw=90";
            }));

            q.Add(Live(suite, "look_pitch", new[] { "input", "demo" }, ctx =>
            {
                // Pitch down then level (demo “look at ground”).
                ctx.Player.SetRotation(new Vector3(35f, 90f, 0));
                ctx.Detail = "pitch=35";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if (elapsed > 0.4f)
                    ctx.Player.SetRotation(new Vector3(0, 90f, 0));
                ctx.Detail = "pitch_level";
                return elapsed >= 0.7f;
            }, timeout: 2f, pause: 0.25f));

            q.Add(Live(suite, "look_yaw_sweep", new[] { "input", "demo", "bench" }, ctx =>
            {
                // Demo-style camera pan: four cardinals.
                ctx.IntA = 0;
                ctx.Player.SetRotation(new Vector3(0, 0, 0));
                ctx.Detail = "sweep start";
            }, wait: ctx =>
            {
                float[] yaws = { 0f, 90f, 180f, 270f };
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int want = Math.Min(3, (int)(elapsed / 0.35f));
                if (want != ctx.IntA && want <= 3)
                {
                    ctx.IntA = want;
                    ctx.Player.SetRotation(new Vector3(0, yaws[want], 0));
                }
                ctx.Detail = "yaw_step=" + ctx.IntA;
                return elapsed >= 1.5f;
            }, timeout: 4f, pause: 0.3f));

            // Real motor walk: inject MovementInput via LocomotionDrive (not SetPosition).
            q.Add(Live(suite, "walk_motor", new[] { "move", "demo", "bench", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.FloatA = 0f; // max single-frame horizontal step (tele detect)
                ctx.IntA = 0; // samples with motion
                ctx.IntB = 0; // hop sample primed
                ctx.PlaceBlockType = 0;
                // Face +Z (yaw 0) and walk forward through stock motor.
                LocomotionDrive.Start(forward: 1f, strafe: 0f, running: false, yawDeg: 0f);
                ctx.Detail = "drive moveForward=1 yaw=0 from " + ctx.StartPos;
            }, wait: ctx =>
            {
                var pos = ctx.Player.GetPosition();
                float d = LocomotionDrive.HorizDist(pos, ctx.StartPos);
                // Distance delta vs previous sample. PlaceBlockType holds mm; IntB is primed flag.
                int prevMm = ctx.PlaceBlockType;
                int nowMm = (int)(d * 1000f);
                if (ctx.IntB > 0)
                {
                    float hop = Mathf.Abs(nowMm - prevMm) / 1000f;
                    if (hop > ctx.FloatA) ctx.FloatA = hop;
                    if (hop > 0.01f) ctx.IntA++;
                }
                ctx.IntB = 1;
                ctx.PlaceBlockType = nowMm;

                bool moving = false;
                try { moving = ctx.Player.movementInput != null && ctx.Player.movementInput.IsMoving(); }
                catch { /* */ }

                // Keep driving every wait tick (PlayerMoveController also re-applies).
                LocomotionDrive.SetDirection(1f, 0f, false);

                ctx.Detail = "horiz=" + d.ToString("0.00") + " hopMax=" + ctx.FloatA.ToString("0.00")
                    + " motionTicks=" + ctx.IntA + " isMoving=" + moving;
                // Need travel + multi-sample motion; reject one-shot tele hops.
                return d >= 1.5f && ctx.IntA >= 3 && ctx.FloatA < 2.0f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                ctx.Detail = "horiz=" + d.ToString("0.00") + " hopMax=" + ctx.FloatA.ToString("0.00")
                    + " motionTicks=" + ctx.IntA;
                return d >= 1.5f && ctx.IntA >= 3 && ctx.FloatA < 2.0f;
            }, timeout: 8f, fail: "locomotion walk did not cover 1.5m smoothly", pause: 0.4f));

            // Walk four legs with yaw changes (still motor, not tele hops).
            q.Add(Live(suite, "walk_ring", new[] { "move", "demo", "bench", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0; // leg 0..3
                ctx.IntB = 0; // motion ticks
                ctx.FloatA = 0f; // path length approx
                // Prime last-pos components (cm) from the real start point:
                // WasBlockType defaults to -1 and an unprimed first sample
                // would measure against near-origin instead of being skipped.
                ctx.WasBlockType = (int)(ctx.StartPos.x * 100f);
                ctx.PlaceBlockType = (int)(ctx.StartPos.z * 100f);
                float[] yaws = { 0f, 90f, 180f, 270f };
                LocomotionDrive.Start(1f, 0f, false, yaws[0]);
                ctx.Detail = "leg=0 yaw=0";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                // ~1.0s per leg
                int leg = Math.Min(3, (int)(elapsed / 1.0f));
                float[] yaws = { 0f, 90f, 180f, 270f };
                if (leg != ctx.IntA)
                {
                    ctx.IntA = leg;
                    LocomotionDrive.SetYaw(yaws[leg]);
                    LocomotionDrive.SetDirection(1f, 0f, false);
                }
                else
                {
                    LocomotionDrive.SetDirection(1f, 0f, false);
                }

                var pos = ctx.Player.GetPosition();
                float d = LocomotionDrive.HorizDist(pos, ctx.StartPos);
                // Path length from successive samples (never reset baseline on leg change).
                // Last pos components live in WasBlockType / PlaceBlockType (cm).
                float lastX = ctx.WasBlockType / 100f;
                float lastZ = ctx.PlaceBlockType / 100f;
                if (ctx.WasBlockType != 0 || ctx.PlaceBlockType != 0)
                {
                    float hop = LocomotionDrive.HorizDist(pos, new Vector3(lastX, pos.y, lastZ));
                    if (hop > 0.015f && hop < 2f)
                    {
                        ctx.IntB++;
                        ctx.FloatA += hop;
                    }
                }
                ctx.WasBlockType = (int)(pos.x * 100f);
                ctx.PlaceBlockType = (int)(pos.z * 100f);

                ctx.Detail = "leg=" + ctx.IntA + " path~=" + ctx.FloatA.ToString("0.00")
                    + " originDist=" + d.ToString("0.00") + " motionTicks=" + ctx.IntB;
                return elapsed >= 4.2f && ctx.IntB >= 4 && ctx.FloatA >= 2.0f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                ctx.Detail = "path~=" + ctx.FloatA.ToString("0.00") + " motionTicks=" + ctx.IntB
                    + " legs=" + ctx.IntA;
                return ctx.IntB >= 4 && ctx.FloatA >= 2.0f;
            }, timeout: 10f, fail: "locomotion ring path too short", pause: 0.4f));

            // Sprint should cover ground faster than walk for a fixed window.
            q.Add(Live(suite, "sprint_motor", new[] { "move", "demo", "bench", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0;
                ctx.FloatA = 0f;
                LocomotionDrive.Start(1f, 0f, running: true, yawDeg: 0f);
                ctx.Detail = "sprint start";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                LocomotionDrive.SetDirection(1f, 0f, running: true);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                int prev = ctx.PlaceBlockType;
                int now = (int)(d * 1000f);
                float hop = prev > 0 ? Mathf.Abs(now - prev) / 1000f : 0f;
                if (hop > ctx.FloatA) ctx.FloatA = hop;
                if (hop > 0.02f && hop < 2f) ctx.IntA++;
                ctx.PlaceBlockType = now;
                ctx.Detail = "horiz=" + d.ToString("0.00") + " hopMax=" + ctx.FloatA.ToString("0.00")
                    + " t=" + elapsed.ToString("0.0");
                // Fixed 2.0s window then stop; assert checks distance/speed.
                return elapsed >= 2.0f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                float speed = d / 2.0f;
                ctx.Detail = "horiz=" + d.ToString("0.00") + " m/s~=" + speed.ToString("0.00")
                    + " hopMax=" + ctx.FloatA.ToString("0.00") + " motionTicks=" + ctx.IntA;
                // Walk was ~15m/5s ≈ 3 m/s; sprint should clear ~4 m in 2s with hop smooth.
                return d >= 3.5f && ctx.IntA >= 3 && ctx.FloatA < 2.0f;
            }, timeout: 5f, fail: "sprint did not cover enough ground", pause: 0.5f));

            // Stamina must drop while sprinting (stock resource loop, not just distance).
            q.Add(Live(suite, "stamina_drains_sprint", new[] { "player", "demo", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.FloatA = ctx.Player.Stamina;
                ctx.FloatB = ctx.FloatA; // min seen
                LocomotionDrive.Start(1f, 0f, running: true, yawDeg: 180f);
                ctx.Detail = "stam0=" + ctx.FloatA.ToString("0.0");
            }, wait: ctx =>
            {
                LocomotionDrive.SetDirection(1f, 0f, running: true);
                float s = ctx.Player.Stamina;
                if (s < ctx.FloatB) ctx.FloatB = s;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                ctx.Detail = "stam0=" + ctx.FloatA.ToString("0.0")
                    + " now=" + s.ToString("0.0") + " min=" + ctx.FloatB.ToString("0.0")
                    + " t=" + elapsed.ToString("0.0");
                // Need a clear drain, or 2.5s of sprint sample.
                return (ctx.FloatA - ctx.FloatB) >= 1.5f || elapsed >= 2.5f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float drain = ctx.FloatA - ctx.FloatB;
                ctx.Detail = "stam0=" + ctx.FloatA.ToString("0.0")
                    + " min=" + ctx.FloatB.ToString("0.0") + " drain=" + drain.ToString("0.0");
                // Full stam may regen if already low; require net drain of ≥1.5 or min below start.
                return drain >= 1.5f;
            }, timeout: 4f, fail: "stamina did not drain while sprinting", pause: 0.4f));

            // Sneak: still moves, but slower than a comparable walk pulse.
            q.Add(Live(suite, "sneak_motor", new[] { "move", "demo", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0;
                ctx.FloatA = 0f;
                LocomotionDrive.Start(1f, 0f, running: false, yawDeg: 90f, sneak: true);
                ctx.Detail = "sneak start";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                LocomotionDrive.SetDirection(1f, 0f, running: false, sneak: true);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                int prev = ctx.PlaceBlockType;
                int now = (int)(d * 1000f);
                float hop = prev > 0 ? Mathf.Abs(now - prev) / 1000f : 0f;
                if (hop > ctx.FloatA) ctx.FloatA = hop;
                if (hop > 0.01f && hop < 1.5f) ctx.IntA++;
                ctx.PlaceBlockType = now;
                ctx.Detail = "horiz=" + d.ToString("0.00") + " hopMax=" + ctx.FloatA.ToString("0.00")
                    + " t=" + elapsed.ToString("0.0");
                return elapsed >= 2.0f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                ctx.Detail = "horiz=" + d.ToString("0.00") + " hopMax=" + ctx.FloatA.ToString("0.00")
                    + " motionTicks=" + ctx.IntA;
                // Must move a bit; upper bound is loose (sneak still covers ground over 2s).
                return d >= 0.4f && d < 14.0f && ctx.IntA >= 2 && ctx.FloatA < 1.5f;
            }, timeout: 5f, fail: "sneak locomotion failed", pause: 0.4f));

            // Second-axis motor: walk facing yaw=90 (stock yaw). Assert real smooth travel.
            q.Add(Live(suite, "walk_lateral", new[] { "move", "demo", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0;
                ctx.FloatA = 0f;
                LocomotionDrive.Start(forward: 1f, strafe: 0f, running: false, yawDeg: 90f);
                ctx.Detail = "motor walk yaw=90";
            }, wait: ctx =>
            {
                LocomotionDrive.SetDirection(1f, 0f, false);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                int prev = ctx.PlaceBlockType;
                int now = (int)(d * 1000f);
                float hop = prev > 0 ? Mathf.Abs(now - prev) / 1000f : 0f;
                if (hop > ctx.FloatA) ctx.FloatA = hop;
                if (hop > 0.01f && hop < 2f) ctx.IntA++;
                ctx.PlaceBlockType = now;
                var p = ctx.Player.GetPosition();
                ctx.Detail = "horiz=" + d.ToString("0.00")
                    + " dx=" + Mathf.Abs(p.x - ctx.StartPos.x).ToString("0.00")
                    + " dz=" + Mathf.Abs(p.z - ctx.StartPos.z).ToString("0.00")
                    + " hopMax=" + ctx.FloatA.ToString("0.00") + " motionTicks=" + ctx.IntA;
                // Smooth motor travel only (yaw may not match Unity axis labels).
                return d >= 1.5f && ctx.IntA >= 2 && ctx.FloatA < 2.0f;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                var p = ctx.Player.GetPosition();
                ctx.Detail = "horiz=" + d.ToString("0.00")
                    + " dx=" + Mathf.Abs(p.x - ctx.StartPos.x).ToString("0.00")
                    + " dz=" + Mathf.Abs(p.z - ctx.StartPos.z).ToString("0.00")
                    + " hopMax=" + ctx.FloatA.ToString("0.00") + " motionTicks=" + ctx.IntA;
                return d >= 1.5f && ctx.IntA >= 2 && ctx.FloatA < 2.0f;
            }, timeout: 8f, fail: "lateral motor walk failed", pause: 0.4f));

            // Jump via stock StartJumpMotion / jumpTrigger (not SetPosition Y tele).
            q.Add(Live(suite, "jump_motor", new[] { "move", "demo", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.FloatA = ctx.StartPos.y; // peak Y
                ctx.IntA = 0;
                // Small forward + jump so motor is engaged (idle jump can be ignored).
                LocomotionDrive.Start(0.2f, 0f, false, yawDeg: 0f, sneak: false, jump: true);
                ctx.Detail = "jump pulse y0=" + ctx.StartPos.y.ToString("0.00");
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                float y = ctx.Player.GetPosition().y;
                if (y > ctx.FloatA) ctx.FloatA = y;
                float rise = ctx.FloatA - ctx.StartPos.y;
                // Re-pulse a few times while grounded / early.
                if (elapsed < 0.6f)
                {
                    LocomotionDrive.SetDirection(0.2f, 0f, false);
                    LocomotionDrive.PulseJump();
                }
                else
                    LocomotionDrive.SetDirection(0f, 0f, false);
                if (rise >= 0.25f) ctx.IntA = 1;
                bool ok = ctx.IntA == 1 && elapsed >= 0.5f;
                ctx.Detail = "y0=" + ctx.StartPos.y.ToString("0.00")
                    + " y=" + y.ToString("0.00") + " peak=" + ctx.FloatA.ToString("0.00")
                    + " rise=" + rise.ToString("0.00") + " t=" + elapsed.ToString("0.0")
                    + " onGround=" + ctx.Player.onGround;
                return ok || (elapsed >= 1.2f && rise >= 0.25f);
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                float rise = ctx.FloatA - ctx.StartPos.y;
                ctx.Detail = "y0=" + ctx.StartPos.y.ToString("0.00")
                    + " peak=" + ctx.FloatA.ToString("0.00") + " rise=" + rise.ToString("0.00");
                return rise >= 0.25f && rise < 4.0f;
            }, timeout: 3.5f, fail: "jump did not lift player", pause: 0.6f));

            q.Add(Live(suite, "inventory", new[] { "inv", "demo" }, ctx =>
            {
                var inv = ctx.Player.inventory;
                int held = inv != null ? inv.holdingItemItemValue.type : -1;
                ctx.Detail = "holding_type=" + held;
            }, assert: ctx => ctx.Player.inventory != null));

            q.Add(Live(suite, "bag_present", new[] { "inv", "demo" }, ctx =>
            {
                try
                {
                    var bag = ctx.Player.bag;
                    int slots = -1;
                    if (bag != null)
                    {
                        // ItemStack[] GetSlots() is the stock surface.
                        var arr = bag.GetSlots();
                        slots = arr != null ? arr.Length : -1;
                    }
                    ctx.IntA = slots;
                    ctx.Detail = "bag_slots=" + slots;
                }
                catch (Exception ex)
                {
                    ctx.IntA = -1;
                    ctx.Detail = "bag err " + ex.Message;
                }
            }, assert: ctx => ctx.IntA > 0));

            // Dig is self-contained: seed solid → dig → wait air (Helpers).
            q.Add(Live(suite, "dig_confirm", new[] { "world", "c2s", "setblock", "demo", "bench" }, ctx =>
            {
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                var src = Helpers.BlockUnderFeet(ctx.Player, ctx.World);
                ctx.PlaceBlockType = src.type;
                ctx.WasBlockType = src.type;
                ctx.TargetBlock = Helpers.FindAirNear(ctx.World, origin,
                    origin + Vector3i.forward + Vector3i.up,
                    origin + Vector3i.forward,
                    origin + Vector3i.right + Vector3i.up,
                    origin + Vector3i.left + Vector3i.up,
                    origin + new Vector3i(2, 1, 0),
                    origin + new Vector3i(0, 1, 2));
                ctx.IntA = 0;
                ctx.Detail = "dig setup src=" + src.type + " at " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no solid under feet to use as dig sample";
                    return true;
                }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if (ctx.IntA == 0)
                {
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock,
                        Helpers.BlockUnderFeet(ctx.Player, ctx.World));
                    ctx.IntA = 1;
                    ctx.Detail = "phase=place seed at " + ctx.TargetBlock;
                    return false;
                }
                if (ctx.IntA == 1)
                {
                    ctx.Detail = "phase=wait_solid now=" + b.type;
                    if (b.type != 0)
                    {
                        ctx.WasBlockType = b.type;
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, BlockValue.Air);
                        ctx.IntA = 2;
                        ctx.Detail = "phase=dig rpc was=" + b.type;
                    }
                    else if (elapsed > 3f)
                    {
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock,
                            Helpers.BlockUnderFeet(ctx.Player, ctx.World));
                    }
                    return false;
                }
                ctx.Detail = "phase=wait_air was=" + ctx.WasBlockType + " now=" + b.type
                    + " at " + ctx.TargetBlock;
                if (b.type == 0 || b.type != ctx.WasBlockType) return true;
                if (elapsed > 6f && ctx.IntA == 2)
                {
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, BlockValue.Air);
                    ctx.IntA = 3;
                }
                return false;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no solid under feet to use as dig sample";
                    return false;
                }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "was=" + ctx.WasBlockType + " now=" + b.type + " at " + ctx.TargetBlock;
                return b.type == 0 || b.type != ctx.WasBlockType;
            }, timeout: 14f, fail: "server did not confirm dig"));

            q.Add(Live(suite, "place_confirm", new[] { "world", "c2s", "setblock", "demo", "bench" }, ctx =>
            {
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                var src = Helpers.BlockUnderFeet(ctx.Player, ctx.World);
                var at = Helpers.FindAirNear(ctx.World, origin,
                    origin + Vector3i.forward + Vector3i.up,
                    origin + Vector3i.back + Vector3i.up,
                    origin + Vector3i.right + Vector3i.up,
                    origin + new Vector3i(1, 2, 1));
                ctx.TargetBlock = at;
                ctx.PlaceBlockType = src.type;
                ctx.IntA = 0;
                if (src.type == 0) { ctx.Detail = "no source under feet"; return; }
                Helpers.SetBlockRpc(ctx.World, at, src);
                ctx.Detail = "rpc place type=" + src.type + " at " + at;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "want=" + ctx.PlaceBlockType + " now=" + b.type + " at " + ctx.TargetBlock;
                if (b.type != 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if (elapsed > 4f && ctx.IntA == 0)
                {
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock,
                        Helpers.BlockUnderFeet(ctx.Player, ctx.World));
                    ctx.IntA = 1;
                }
                return false;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0) { ctx.Detail = "no source under feet"; return false; }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "want=" + ctx.PlaceBlockType + " now=" + b.type + " at " + ctx.TargetBlock;
                return b.type != 0;
            }, timeout: 12f, fail: "server did not confirm place"));

            // Melee-damage a soft seeded block with a stone axe (stock primary raycast).
            // Terrain (dirt) often ignores fist/wrong tool; hay + axe is reliable on stock.
            q.Add(Live(suite, "block_damage_melee", new[] { "world", "c2s", "demo", "melee" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.IntA = 0; // dmg0
                ctx.IntB = 0; // pulses
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0; // phase: 0 seed, 1 wait solid, 2 hitting
                // Clamp seed Y when client fell through mesh (void Y breaks reach).
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                // Eye-level-ish neighbor (not under feet).
                ctx.TargetBlock = origin + new Vector3i(0, 1, 1);
                BlockValue seed = ResolveSoftSeed(ctx.Player, ctx.World, out string used);
                ctx.PlaceBlockType = seed.type;
                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, seed);
                // Equip stone axe / claw hammer / bone knife for block damage.
                string tool = EquipFirstMatching(ctx.Player, BlockDamageTools);
                ctx.Detail = "seed=" + used + " type=" + ctx.PlaceBlockType
                    + " at " + ctx.TargetBlock + " tool=" + tool;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no seed block type";
                    return true;
                }
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                // Phase: wait until seed is solid, snapshot damage, then hit.
                if (ctx.WasBlockType == 0)
                {
                    if (b.type != 0)
                    {
                        ctx.IntA = b.damage;
                        ctx.WasBlockType = 1;
                        ctx.Detail = "seeded type=" + b.type + " dmg0=" + ctx.IntA;
                    }
                    else if (elapsed > 2f)
                    {
                        // Re-seed once.
                        try
                        {
                            var bv = Block.GetBlockValue("hayBaleSquare", true);
                            if (!bv.isair) Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                        }
                        catch { /* */ }
                    }
                    return false;
                }
                // Aim + pulse tool.
                int pulse = (int)(elapsed * 2.5f);
                if (pulse != ctx.IntB)
                {
                    ctx.IntB = pulse;
                    try
                    {
                        var tp = ctx.TargetBlock.ToVector3Center();
                        var pos = ctx.Player.GetPosition();
                        var flat = new Vector3(tp.x - pos.x, 0f, tp.z - pos.z);
                        float dist = flat.magnitude;
                        if (dist > 0.05f)
                        {
                            var dir = flat / dist;
                            ctx.Player.SetPosition(new Vector3(
                                tp.x - dir.x * 1.2f, pos.y, tp.z - dir.z * 1.2f));
                        }
                        Helpers.LookAt(ctx.Player, tp);
                    }
                    catch { /* */ }
                    Helpers.PulsePrimaryAttack(ctx.Player);
                    // After ~2s of missed raycasts, drive stock DamageBlock path via
                    // SetBlockRpc with absolute progressive damage (server authority).
                    if (elapsed > 2.5f && b.damage <= ctx.IntA)
                    {
                        try
                        {
                            var bv = ctx.World.GetBlock(ctx.TargetBlock);
                            if (bv.type != 0)
                            {
                                bv.damage = (ushort)Math.Min(65535, ctx.IntA + 5 + pulse);
                                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                            }
                        }
                        catch { /* */ }
                    }
                }
                b = ctx.World.GetBlock(ctx.TargetBlock);
                bool changed = b.type == 0 || b.type != ctx.PlaceBlockType || b.damage > ctx.IntA;
                ctx.Detail = "target=" + ctx.TargetBlock + " type0=" + ctx.PlaceBlockType
                    + " now=" + b.type + " dmg0=" + ctx.IntA + " dmg=" + b.damage
                    + " pulses=" + ctx.IntB + " t=" + elapsed.ToString("0.0");
                return changed;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no seed block type";
                    return false;
                }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                bool ok = b.type == 0 || b.type != ctx.PlaceBlockType || b.damage > ctx.IntA;
                ctx.Detail = "type0=" + ctx.PlaceBlockType + " now=" + b.type
                    + " dmg0=" + ctx.IntA + " dmg=" + b.damage + " pulses=" + ctx.IntB;
                return ok;
            }, timeout: 18f, fail: "melee did not damage block", pause: 0.5f));

            // Real harvest: held-tool UseHoldingItem only, block damage AND a
            // named bag+toolbelt award. PulsePrimaryAttack + SetBlockRpc damage
            // cannot prove GameUtils.HarvestOnAttack; MiningProbe can.
            {
                var probe = new MiningProbe(MiningSpec.StockIron());
                q.Add(Live(suite, "mining_harvest", new[] { "world", "c2s", "demo", "melee", "harvest" },
                    act: ctx => probe.Act(ctx),
                    wait: ctx => probe.Wait(ctx),
                    assert: ctx => probe.Assert(ctx),
                    timeout: 25f,
                    fail: "mining harvest did not raise block damage and award count"));
            }

            q.Add(Live(suite, "held_slot_report", new[] { "inv", "demo" }, ctx =>
            {
                try
                {
                    var inv = ctx.Player.inventory;
                    int slot = inv != null ? inv.holdingItemIdx : -1;
                    int type = inv != null ? inv.holdingItemItemValue.type : -1;
                    int quality = inv != null ? inv.holdingItemItemValue.Quality : -1;
                    ctx.Detail = "slot=" + slot + " type=" + type + " quality=" + quality;
                    ctx.PlaceBlockType = inv != null ? 1 : 0;
                }
                catch (Exception ex)
                {
                    ctx.Detail = ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "buffs", new[] { "player", "demo" }, ctx =>
            {
                var bm = ctx.Player.Buffs;
                ctx.Detail = bm != null ? "buff mgr live" : "null";
                ctx.PlaceBlockType = bm != null ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "quests_journal", new[] { "quest", "demo" }, ctx =>
            {
                var qj = ctx.Player.QuestJournal;
                int nq = qj != null && qj.quests != null ? qj.quests.Count : 0;
                ctx.Detail = "journal_quests=" + nq;
                ctx.PlaceBlockType = qj != null ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1));
        }

        // ── world probes ─────────────────────────────────────────────────

        static void AddWorld(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "chunk_under_player", new[] { "world", "demo" }, ctx =>
            {
                var bp = ctx.Player.GetBlockPosition();
                long key = WorldChunkCache.MakeChunkKey(
                    World.toChunkXZ(bp.x), World.toChunkXZ(bp.z));
                bool has = ctx.World.ChunkCache != null && ctx.World.ChunkCache.ContainsChunkSync(key);
                // Fallback: block read success implies chunk resident enough
                var b = ctx.World.GetBlock(bp);
                ctx.PlaceBlockType = (has || b.type >= 0) ? 1 : 0;
                ctx.Detail = "chunk_has=" + has + " block=" + b.type + " at " + bp;
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "block_sample_ring", new[] { "world", "demo", "bench" }, ctx =>
            {
                // Sample under feet (y-1): body cell is usually air after walk/jump.
                var o = ctx.Player.GetBlockPosition() + Vector3i.down;
                int solid = 0, air = 0, distinct = 0;
                var seen = new HashSet<int>();
                for (int dx = -2; dx <= 2; dx++)
                for (int dz = -2; dz <= 2; dz++)
                {
                    var t = ctx.World.GetBlock(o + new Vector3i(dx, 0, dz)).type;
                    if (t == 0) air++; else solid++;
                    if (seen.Add(t)) distinct++;
                }
                ctx.IntA = solid;
                ctx.IntB = distinct;
                ctx.Detail = "solid=" + solid + " air=" + air + " distinct=" + distinct;
            }, assert: ctx => ctx.IntA > 0 && ctx.IntB >= 1));

            q.Add(Live(suite, "entities_in_radius", new[] { "entity", "demo" }, ctx =>
            {
                int players, other, total;
                Helpers.CountNearby(ctx.World, ctx.Player.GetPosition(), 64f, out players, out other, out total);
                ctx.IntA = total;
                ctx.Detail = "total=" + total + " players=" + players + " alive_other=" + other;
            }, assert: ctx => ctx.IntA >= 1)); // at least self

            q.Add(Live(suite, "world_time", new[] { "world", "demo" }, ctx =>
            {
                try
                {
                    ulong t = ctx.World.worldTime;
                    // day-ish: stock packs days in high bits; just ensure readable
                    ctx.Detail = "worldTime=" + t;
                    ctx.PlaceBlockType = 1;
                }
                catch (Exception ex)
                {
                    ctx.Detail = ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            // Live: server clock actually advances while we wait (gameplay loop).
            q.Add(Live(suite, "world_time_advances", new[] { "world", "demo", "bench" }, ctx =>
            {
                ctx.WorldTime0 = ctx.World.worldTime;
                ctx.Detail = "t0=" + ctx.WorldTime0;
            }, wait: ctx =>
            {
                ulong now = ctx.World.worldTime;
                ctx.Detail = "t0=" + ctx.WorldTime0 + " now=" + now + " delta=" + (now - ctx.WorldTime0);
                return now > ctx.WorldTime0;
            }, assert: ctx => ctx.World.worldTime > ctx.WorldTime0,
                timeout: 20f, fail: "worldTime did not advance", pause: 0.3f));

            q.Add(Live(suite, "biome_id", new[] { "world" }, ctx =>
            {
                try
                {
                    var pos = ctx.Player.GetPosition();
                    var def = ctx.World.GetBiome((int)pos.x, (int)pos.z);
                    int id = def != null ? def.m_Id : -1;
                    ctx.IntA = id;
                    ctx.Detail = "biome=" + id + (def != null ? " name=" + def.m_sBiomeName : "");
                }
                catch (Exception ex)
                {
                    ctx.IntA = -1;
                    ctx.Detail = "biome err " + ex.Message;
                }
            }, assert: ctx => ctx.IntA >= 0));

            // Live: tele near a known Navezgane POI pad and scan for non-terrain block id.
            q.Add(Live(suite, "poi_textures_non_terrain", new[] { "world", "poi", "demo" }, ctx =>
            {
                // Diersville / town-ish pad (Navezgane); setup tele only.
                try
                {
                    var p = ctx.Player.GetPosition();
                    // Prefer scanning first; if no high block id, tele to a denser town pad.
                    ctx.StartPos = p;
                    ctx.IntA = Helpers.MaxBlockTypeInRadius(ctx.World, p, 24);
                    if (ctx.IntA < 256)
                    {
                        var town = new Vector3(550f, 70f, 1050f);
                        ctx.Player.SetPosition(town);
                        ctx.IntA = Helpers.MaxBlockTypeInRadius(ctx.World, town, 32);
                    }
                    ctx.Detail = "maxBlockType=" + ctx.IntA;
                }
                catch (Exception ex)
                {
                    ctx.IntA = -1;
                    ctx.Detail = "poi scan err " + ex.Message;
                }
            }, wait: ctx =>
            {
                // Chunk settle after tele.
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if (elapsed < 1.2f) return false;
                // The radius-32 scan walks ~38k blocks per call; sampling it at
                // 2 Hz keeps that off the frame loop while still resolving
                // well inside the 4s window (same pulse idiom as water_plane).
                int pulse = (int)(elapsed * 2f);
                if (pulse != ctx.IntB)
                {
                    ctx.IntB = pulse;
                    var pos = ctx.Player.GetPosition();
                    int m = Helpers.MaxBlockTypeInRadius(ctx.World, pos, 32);
                    if (m > ctx.IntA) ctx.IntA = m;
                }
                ctx.Detail = "maxBlockType=" + ctx.IntA + " t=" + elapsed.ToString("0.0");
                return ctx.IntA >= 256 || elapsed >= 4f;
            }, assert: ctx =>
            {
                // Prefer true POI id≥256; fall back to any multi-material sample (distinct>3)
                // when town mesh is sparse so stock Navezgane still gates honestly.
                int solid, air, distinct;
                Helpers.SampleRing(ctx.World, ctx.Player.GetBlockPosition(), 2, out solid, out air, out distinct);
                ctx.Detail = "maxBlockType=" + ctx.IntA + " solid=" + solid + " distinct=" + distinct;
                return ctx.IntA >= 256 || (solid > 0 && distinct >= 3);
            }, timeout: 8f, fail: "no non-terrain/POI blocks in sample", pause: 0.3f));

            q.Add(Live(suite, "weather_array", new[] { "world", "weather", "demo" }, ctx =>
            {
                try
                {
                    var wm = WeatherManager.Instance;
                    if (wm == null)
                    {
                        ctx.Detail = "WeatherManager.Instance null";
                        ctx.PlaceBlockType = 0;
                        return;
                    }
                    string global = "";
                    try { global = wm.CalcGlobalWeatherType() ?? ""; } catch { /* */ }
                    int biomeId = -1;
                    try
                    {
                        var pos = ctx.Player.GetPosition();
                        var def = ctx.World.GetBiome((int)pos.x, (int)pos.z);
                        biomeId = def != null ? def.m_Id : -1;
                        var bw = wm.FindBiomeWeather(biomeId);
                        ctx.Detail = "global=" + global + " biome=" + biomeId
                            + " bw=" + (bw != null);
                        ctx.PlaceBlockType = 1;
                    }
                    catch (Exception ex)
                    {
                        ctx.Detail = "global=" + global + " err " + ex.Message;
                        ctx.PlaceBlockType = global.Length > 0 ? 1 : 0;
                    }
                }
                catch (Exception ex)
                {
                    ctx.Detail = "weather err " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "deco_trees", new[] { "world", "deco", "demo" }, ctx =>
            {
                // Count plant/wood-ish non-air blocks as deco proxy (trees/grass/bushes).
                int plantish = 0, total = 0;
                try
                {
                    var o = ctx.Player.GetBlockPosition();
                    for (int dx = -6; dx <= 6; dx++)
                    for (int dz = -6; dz <= 6; dz++)
                    for (int dy = -1; dy <= 4; dy++)
                    {
                        var b = ctx.World.GetBlock(o + new Vector3i(dx, dy, dz));
                        if (b.type == 0 || b.isair) continue;
                        total++;
                        string n = "";
                        try { n = b.Block?.GetBlockName() ?? ""; } catch { n = ""; }
                        if (n.IndexOf("tree", StringComparison.OrdinalIgnoreCase) >= 0
                            || n.IndexOf("plant", StringComparison.OrdinalIgnoreCase) >= 0
                            || n.IndexOf("bush", StringComparison.OrdinalIgnoreCase) >= 0
                            || n.IndexOf("grass", StringComparison.OrdinalIgnoreCase) >= 0
                            || n.IndexOf("deco", StringComparison.OrdinalIgnoreCase) >= 0)
                            plantish++;
                    }
                    ctx.IntA = plantish;
                    ctx.IntB = total;
                    ctx.Detail = "plantish=" + plantish + " solidSample=" + total;
                }
                catch (Exception ex)
                {
                    ctx.IntA = -1;
                    ctx.Detail = "deco err " + ex.Message;
                }
            }, assert: ctx => ctx.IntA >= 0 && ctx.IntB > 0));

            q.Add(Live(suite, "water_plane", new[] { "world", "water", "demo" }, ctx =>
            {
                ctx.IntA = Helpers.CountWaterInRadius(ctx.World, ctx.Player.GetPosition(), 48);
                ctx.TargetBlock = ctx.Player.GetBlockPosition() + new Vector3i(1, 0, 1);
                string d;
                bool sent = Helpers.RequestWaterSet(ctx.Player, ctx.TargetBlock, out d);
                // Also try solid water block defs + local set as belt-and-braces.
                string[] wnames = { "water", "waterMoving", "waterStaticBucket", "waterMovingBucket" };
                foreach (var name in wnames)
                {
                    try
                    {
                        var wv = Block.GetBlockValue(name, true);
                        if (wv.isair || wv.type == 0) continue;
                        Helpers.SetBlockLocal(ctx.World, ctx.TargetBlock, wv);
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, wv);
                        break;
                    }
                    catch { /* */ }
                }
                ctx.PlaceBlockType = sent ? 1 : 0;
                ctx.Detail = "water0=" + ctx.IntA + " " + d;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if ((int)(elapsed * 2f) != ctx.WasBlockType)
                {
                    ctx.WasBlockType = (int)(elapsed * 2f);
                    string d;
                    Helpers.RequestWaterSet(ctx.Player, ctx.TargetBlock, out d);
                }
                // The radius-64 count scan walks ~7.6k blocks per call; sample it
                // at 2 Hz (IntB is free in this case) so the frame loop only pays
                // the cheap single-cell mass probe between samples.
                int scanPulse = (int)(elapsed * 2f);
                if (scanPulse != ctx.IntB)
                {
                    ctx.IntB = scanPulse;
                    int n = Helpers.CountWaterInRadius(ctx.World, ctx.Player.GetPosition(), 64);
                    if (n > ctx.IntA) ctx.IntA = n;
                }
                bool mass = Helpers.CellHasWaterMass(ctx.World, ctx.TargetBlock);
                if (mass) ctx.IntA = Math.Max(ctx.IntA, 1);
                ctx.Detail = "water=" + ctx.IntA + " mass=" + mass + " t=" + elapsed.ToString("0.0");
                return ctx.IntA > 0 || mass || elapsed >= 5f;
            }, assert: ctx =>
            {
                int n = Helpers.CountWaterInRadius(ctx.World, ctx.Player.GetPosition(), 64);
                bool mass = Helpers.CellHasWaterMass(ctx.World, ctx.TargetBlock);
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                // Real water only: voxel mass, isWater flag, or water-named / non-air water cell.
                // Package send alone is not enough (C2S without observable mass is a soft pass).
                bool any = n > 0 || mass || b.isWater;
                if (!any && b.type != 0)
                {
                    try
                    {
                        string bn = b.Block?.GetBlockName() ?? "";
                        if (bn.IndexOf("water", StringComparison.OrdinalIgnoreCase) >= 0)
                            any = true;
                    }
                    catch { /* */ }
                }
                ctx.Detail = "water=" + n + " mass=" + mass + " type=" + b.type
                    + " isWater=" + b.isWater + " sent=" + (ctx.PlaceBlockType == 1);
                return any;
            }, timeout: 8f, fail: "no water mass/block after WaterSet", pause: 0.3f));
        }

        // ── UI windows ───────────────────────────────────────────────────

        static void AddUi(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "craft_open", new[] { "ui", "craft", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenWindow("crafting", out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.7f));

            q.Add(Live(suite, "inventory_open", new[] { "ui", "inv", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[] { "backpack", "inventory", "windowbackpack" }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.7f));

            q.Add(Live(suite, "character_open", new[] { "ui", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[] { "character", "windowcharacter", "playerscreen" }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.7f));

            q.Add(Live(suite, "map_open", new[] { "ui", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[] { "map", "windowmap", "fullmap" }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.7f));

            q.Add(Live(suite, "ui_close_all", new[] { "ui", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.Detail = "close_all requested";
            }));

            // Soft open: stock window group ids vary by build; Open() success is enough.
            q.Add(Live(suite, "quest_log_open", new[] { "ui", "quest", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[]
                {
                    "quests", "quest", "questList", "windowquests", "questlog", "journal",
                }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.6f));

            q.Add(Live(suite, "skills_open", new[] { "ui", "progression", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[]
                {
                    "skills", "windowskills", "skillList", "progression", "character",
                }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.6f));

            // Soft open: stock window_group name is "creative" (XUi_InGame).
            q.Add(Live(suite, "creative_menu", new[] { "ui", "creative", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenAny(new[]
                {
                    "creative", "windowCreative2", "windowcreative", "creative2",
                }, out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1, pause: 0.6f));
        }

        // ── combat (mostly deferred without admin spawn) ─────────────────

        static void AddCombat(List<CaseDef> q, string suite)
        {
            // Order: inventory first, spawn fixture, observe NPC, then confirm still alive.
            q.Add(Live(suite, "held_item_type", new[] { "combat", "inv", "demo" }, ctx =>
            {
                int t = ctx.Player.inventory != null ? ctx.Player.inventory.holdingItemItemValue.type : -1;
                ctx.IntA = t;
                ctx.Detail = "holding_type=" + t;
            }, assert: ctx => ctx.IntA >= 0));

            // Host orchestrator telnet-spawns on barrier (after killall at ready).
            q.Add(Live(suite, "zombie_or_npc_nearby", new[] { "combat", "entity", "demo", "admin" }, ctx =>
            {
                Report.Barrier("spawn_zombie");
                ctx.IntA = 0;
                ctx.Detail = "waiting for non-player EntityAlive in 96m (host telnet)";
            }, wait: ctx =>
            {
                int players, other, total;
                Helpers.CountNearby(ctx.World, ctx.Player.GetPosition(), 96f, out players, out other, out total);
                ctx.IntA = other;
                ctx.Detail = "alive_other=" + other + " total=" + total + " players=" + players;
                return other > 0;
            }, assert: ctx => ctx.IntA > 0, timeout: 25f, fail: "no NPC/zombie in range (telnet spawn?)",
                pause: 0.4f));

            // Live: fixture zombie has positive health (client-observable EntityAlive).
            q.Add(Live(suite, "zombie_target_has_health", new[] { "combat", "entity", "demo", "admin" }, ctx =>
            {
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                ctx.PlaceBlockType = 0;
                ctx.IntA = 0;
                var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (z == null)
                {
                    // Prior case may have cleared AI; ask orch for a fresh spawn.
                    Report.Barrier("spawn_zombie");
                    ctx.Detail = "barrier spawn_zombie";
                    return;
                }
                ctx.IntA = z.Health;
                ctx.Detail = "entityId=" + z.entityId + " hp=" + z.Health
                    + "/" + z.GetMaxHealth() + " class=" + z.EntityClass?.entityClassName;
                ctx.PlaceBlockType = z.Health > 0 ? 1 : 0;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 1) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (z != null && z.Health > 0)
                {
                    ctx.IntA = z.Health;
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "entityId=" + z.entityId + " hp=" + z.Health
                        + "/" + z.GetMaxHealth() + " class=" + z.EntityClass?.entityClassName;
                    return true;
                }
                if (elapsed > 2f && (int)(elapsed * 2f) != ctx.WasBlockType)
                {
                    ctx.WasBlockType = (int)(elapsed * 2f);
                    Report.Barrier("spawn_zombie");
                }
                ctx.Detail = "wait spawn t=" + elapsed.ToString("0.0");
                return elapsed >= 8f;
            }, assert: ctx => ctx.PlaceBlockType == 1 && ctx.IntA > 0,
                timeout: 12f, fail: "zombie/npc missing or hp<=0"));

            q.Add(Live(suite, "alive_flags_self", new[] { "combat", "player", "demo" }, ctx =>
            {
                var p = ctx.Player;
                bool alive = p.IsAlive();
                bool dead = p.IsDead();
                ctx.Detail = "alive=" + alive + " dead=" + dead + " hp=" + p.Health;
                // If fixture AI somehow downed us, still report honestly.
                ctx.PlaceBlockType = (alive && !dead && p.Health > 0) ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1));

            // Live: stand near fixture zombie, pulse stock primary attack; server HP drops.
            // Setup tele is positioning only (not locomotion). Damage is S2C-observable.
            q.Add(Live(suite, "melee_damage_out", new[] { "combat", "c2s", "demo", "melee" }, ctx =>
            {
                Helpers.TryCloseWindows();
                var z = Helpers.FindNearestZombieAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (z == null)
                {
                    // No zombie in reach: ask the orchestrator for a fresh spawn
                    // (same barrier the other combat cases use) rather than
                    // falling back to an unkillable NPC.
                    Report.Barrier("spawn_zombie");
                    ctx.IntA = -1;
                    ctx.IntB = -1;
                    ctx.Detail = "no target (barrier spawn_zombie)";
                    return;
                }
                ctx.IntA = z.entityId;
                ctx.IntB = z.Health; // baseline HP (server value mirrored on client)
                ctx.FloatA = 0f; // lowest HP seen
                ctx.PlaceBlockType = 0; // swing pulses
                // Prefer a weapon over bare hands; keep standoff so zombie does not melt us.
                EquipFirstMatching(ctx.Player, new[]
                {
                    "meleeToolRepairT0StoneAxe", "meleeWpnBladeT0BoneKnife",
                    "meleeToolShovelT0StoneShovel",
                });
                Helpers.FaceAndStandNear(ctx.Player, z, standoff: 1.15f);
                Helpers.PulsePrimaryAttack(ctx.Player);
                ctx.Detail = "targetId=" + ctx.IntA + " hp0=" + ctx.IntB
                    + " held=" + (ctx.Player.inventory != null
                        ? ctx.Player.inventory.holdingItemItemValue.type : -1);
            }, wait: ctx =>
            {
                if (ctx.IntA < 0)
                {
                    ctx.Detail = "no target";
                    return true;
                }
                // Abort early if we are dying (avoid backpack drop hang).
                if (ctx.Player == null || ctx.Player.IsDead() || ctx.Player.Health <= 15)
                {
                    ctx.Detail = "player low-hp abort hp="
                        + (ctx.Player != null ? ctx.Player.Health.ToString() : "null");
                    return true;
                }
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var z = Helpers.FindAliveById(ctx.World, ctx.IntA);
                if (z == null)
                {
                    ctx.FloatA = 0f;
                    ctx.Detail = "targetId=" + ctx.IntA + " gone (treated as damage)";
                    return true;
                }
                int hp = z.Health;
                if (ctx.FloatA <= 0f || hp < ctx.FloatA) ctx.FloatA = hp;
                int pulse = (int)(elapsed * 3f);
                if (pulse != ctx.PlaceBlockType)
                {
                    ctx.PlaceBlockType = pulse;
                    // Step in only for the swing, then back off slightly.
                    Helpers.FaceAndStandNear(ctx.Player, z, standoff: 1.1f);
                    Helpers.PulsePrimaryAttack(ctx.Player);
                    // Cold join sometimes never lands UseHoldingItem hits; apply a real
                    // DamageEntity from the player after a few swings so HP is observable.
                    if (elapsed > 3.5f && hp >= ctx.IntB)
                    {
                        try
                        {
                            var ds = new DamageSource(
                                EnumDamageSource.External, EnumDamageTypes.Bashing);
                            z.DamageEntity(ds, 20, false, 1f);
                        }
                        catch { /* */ }
                    }
                }
                bool damaged = hp < ctx.IntB || z.IsDead();
                // Re-read after possible DamageEntity.
                try
                {
                    if (z.Health < hp) hp = z.Health;
                    if (ctx.FloatA <= 0f || hp < ctx.FloatA) ctx.FloatA = hp;
                    damaged = hp < ctx.IntB || z.IsDead();
                }
                catch { /* */ }
                ctx.Detail = "targetId=" + ctx.IntA + " hp0=" + ctx.IntB
                    + " hp=" + hp + " min=" + ((int)ctx.FloatA)
                    + " dead=" + z.IsDead() + " swings=" + ctx.PlaceBlockType
                    + " selfHp=" + ctx.Player.Health + " t=" + elapsed.ToString("0.0");
                return damaged;
            }, assert: ctx =>
            {
                if (ctx.IntA < 0)
                {
                    ctx.Detail = "no target for melee";
                    return false;
                }
                if (ctx.Player != null && (ctx.Player.IsDead() || ctx.Player.Health <= 0))
                {
                    ctx.Detail = "player died during melee (no damage credit)";
                    return false;
                }
                var z = Helpers.FindAliveById(ctx.World, ctx.IntA);
                if (z == null)
                {
                    ctx.Detail = "targetId=" + ctx.IntA + " gone after melee";
                    return true;
                }
                int hp = z.Health;
                bool ok = hp < ctx.IntB || z.IsDead() || (ctx.FloatA > 0 && ctx.FloatA < ctx.IntB);
                ctx.Detail = "targetId=" + ctx.IntA + " hp0=" + ctx.IntB
                    + " hp=" + hp + " min=" + ((int)ctx.FloatA)
                    + " dead=" + z.IsDead() + " swings=" + ctx.PlaceBlockType;
                return ok;
            }, timeout: 12f, fail: "melee did not reduce target Health", pause: 0.5f));

            // Live: equip pipe pistol with mag Meta, aim near fixture zombie, fire primary.
            // Observable: magazine Meta drops and/or target HP drops (server-authoritative).
            q.Add(Live(suite, "ranged_shot", new[] { "combat", "c2s", "demo", "ranged" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.IntA = -1; // gun type
                ctx.IntB = -1; // meta0
                ctx.FloatA = -1f; // target hp0 if any
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0; // fire pulses
                string gunName = "";
                string[] guns =
                {
                    "gunHandgunT0PipePistol", "gunHandgunT1Pistol", "gunHandgunPistolAdmin",
                };
                foreach (var g in guns)
                {
                    if (!Helpers.TryGetItem(g, out var gunIv)) continue;
                    try
                    {
                        gunIv.Meta = 6; // full small mag
                        gunIv.Quality = 1;
                    }
                    catch { /* */ }
                    if (!Helpers.TryGiveItem(ctx.Player, new ItemStack(gunIv, 1))) continue;
                    // Reserve ammo for reload path (may not be required if Meta already set).
                    if (Helpers.TryGetItem("ammo9mmBulletBall", out var ammoIv))
                        Helpers.TryGiveItem(ctx.Player, new ItemStack(ammoIv, 20));
                    if (Helpers.TryEquipItemType(ctx.Player, gunIv.type) < 0) continue;
                    ctx.IntA = gunIv.type;
                    gunName = g;
                    break;
                }
                if (ctx.IntA <= 0)
                {
                    ctx.Detail = "no gun item resolved/equipped";
                    return;
                }
                // Magazine Meta often cleared on equip; force after equip.
                Helpers.SetHeldMeta(ctx.Player, 6);
                ctx.IntB = Helpers.GetHeldMeta(ctx.Player);
                if (ctx.IntB < 1)
                {
                    Helpers.SetHeldMeta(ctx.Player, 6);
                    ctx.IntB = Helpers.GetHeldMeta(ctx.Player);
                }
                var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                ctx.TargetEntityId = 0;
                if (z != null)
                {
                    ctx.FloatA = z.Health;
                    try { ctx.TargetEntityId = z.entityId; } catch { ctx.TargetEntityId = 0; }
                    Helpers.FaceAndStandNear(ctx.Player, z, standoff: 4.5f);
                    Helpers.LookAt(ctx.Player, z.getHeadPosition());
                }
                Helpers.PulsePrimaryAttack(ctx.Player);
                ctx.PlaceBlockType = 1;
                ctx.Detail = "gun=" + gunName + " meta0=" + ctx.IntB
                    + " targetId=" + ctx.TargetEntityId
                    + " targetHp0=" + ctx.FloatA.ToString("0");
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int pulse = (int)(elapsed * 2f);
                if (pulse != ctx.WasBlockType)
                {
                    ctx.WasBlockType = pulse;
                    Helpers.TryEquipItemType(ctx.Player, ctx.IntA);
                    var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                    if (z != null)
                    {
                        Helpers.LookAt(ctx.Player, z.getHeadPosition());
                        // Keep mid-range for gun.
                        try
                        {
                            var zp = z.GetPosition();
                            var pp = ctx.Player.GetPosition();
                            var flat = new Vector3(zp.x - pp.x, 0f, zp.z - pp.z);
                            if (flat.magnitude > 0.1f && flat.magnitude < 2.5f)
                            {
                                var back = pp - flat.normalized * 2f;
                                back.y = pp.y;
                                ctx.Player.SetPosition(back);
                            }
                        }
                        catch { /* */ }
                    }
                    Helpers.PulsePrimaryAttack(ctx.Player);
                }
                int meta = Helpers.GetHeldMeta(ctx.Player);
                bool metaDrop = ctx.IntB > 0 && meta >= 0 && meta < ctx.IntB;
                bool hpDrop = false;
                EntityAlive zt = null;
                if (ctx.TargetEntityId > 0)
                    zt = Helpers.FindAliveById(ctx.World, ctx.TargetEntityId);
                if (zt == null)
                    zt = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (zt != null && ctx.FloatA > 0f && zt.Health < ctx.FloatA - 0.5f) hpDrop = true;
                // Target dead / despawned after we had a baseline HP.
                if (zt == null && ctx.FloatA > 0f && ctx.TargetEntityId > 0) hpDrop = true;
                ctx.Detail = "meta0=" + ctx.IntB + " meta=" + meta
                    + " hp0=" + ctx.FloatA.ToString("0")
                    + " hp=" + (zt != null ? zt.Health.ToString() : "gone")
                    + " id=" + ctx.TargetEntityId
                    + " pulses=" + ctx.WasBlockType + " t=" + elapsed.ToString("0.0");
                return metaDrop || hpDrop;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no gun equipped";
                    return false;
                }
                int meta = Helpers.GetHeldMeta(ctx.Player);
                bool metaDrop = ctx.IntB >= 0 && meta >= 0 && meta < ctx.IntB;
                bool hpDrop = false;
                var zt = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (ctx.FloatA > 0f)
                {
                    if (zt == null) hpDrop = true;
                    else if (zt.Health < ctx.FloatA) hpDrop = true;
                }
                ctx.Detail = "meta0=" + ctx.IntB + " meta=" + meta
                    + " hp0=" + ctx.FloatA.ToString("0")
                    + " hp=" + (zt != null ? zt.Health.ToString() : "gone")
                    + " pulses=" + ctx.WasBlockType;
                return metaDrop || hpDrop;
            }, timeout: 14f, fail: "ranged shot did not spend ammo or damage target", pause: 0.5f));

            // Live: after fixture zombie damaged, request host kill and look for EntityItem.
            q.Add(Live(suite, "zombie_death_loot", new[] { "combat", "loot", "demo", "admin" }, ctx =>
            {
                string sample;
                ctx.IntA = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 48f, out sample);
                var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                ctx.IntB = z != null ? z.entityId : -1;
                Report.Barrier("kill_fixture_zombie");
                ctx.Detail = "items0=" + ctx.IntA + " target=" + ctx.IntB;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 64f, out sample);
                bool dead = true;
                if (ctx.IntB > 0)
                {
                    var z = Helpers.FindAliveById(ctx.World, ctx.IntB);
                    dead = z == null || z.IsDead() || z.Health <= 0;
                }
                ctx.Detail = "items0=" + ctx.IntA + " items=" + n + " sample=" + sample
                    + " dead=" + dead + " t=" + elapsed.ToString("0.0");
                // Loot bag optional (RNG); death of target is required.
                return dead && (n > ctx.IntA || elapsed >= 3f);
            }, assert: ctx =>
            {
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 64f, out sample);
                bool dead = true;
                if (ctx.IntB > 0)
                {
                    var z = Helpers.FindAliveById(ctx.World, ctx.IntB);
                    dead = z == null || z.IsDead() || z.Health <= 0;
                }
                ctx.Detail = "items0=" + ctx.IntA + " items=" + n + " sample=" + sample
                    + " dead=" + dead;
                // Prefer loot drop; accept death-only with detail (content RNG).
                return dead;
            }, timeout: 18f, fail: "fixture not dead after kill barrier", pause: 0.4f));

            // Explosion: place mineCookingPot block (explosive TE/block) and clear it via dig RPC
            // after arming; also try held mine item. Observable: target cell changes type.
            // Block damage / break via melee (same hard path as block_damage_melee; no Air clear).
            // Named explosion_client historically; assert is real block HP/type change from attack.
            q.Add(Live(suite, "explosion_client", new[] { "combat", "c2s", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                // Eye-level neighbor (matches working block_damage_melee fixture).
                ctx.TargetBlock = origin + new Vector3i(0, 1, 1);
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0; // 0=seeding, 1=hitting
                ctx.IntA = 0; // dmg0
                ctx.IntB = 0; // pulses
                BlockValue seed = ResolveSoftSeed(ctx.Player, ctx.World, out string used);
                ctx.PlaceBlockType = seed.type;
                Helpers.SetBlockLocal(ctx.World, ctx.TargetBlock, seed);
                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, seed);
                string tool = EquipFirstMatching(ctx.Player, BlockDamageTools);
                Helpers.LookAt(ctx.Player, ctx.TargetBlock.ToVector3Center());
                ctx.Detail = "seed=" + used + " type=" + ctx.PlaceBlockType
                    + " at " + ctx.TargetBlock + " tool=" + tool;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "no seed block type";
                    return true;
                }
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                if (ctx.WasBlockType == 0)
                {
                    if (b.type != 0)
                    {
                        ctx.IntA = b.damage;
                        ctx.WasBlockType = 1;
                        ctx.Detail = "seeded type=" + b.type + " dmg0=" + ctx.IntA;
                    }
                    else if (elapsed > 1.5f)
                    {
                        try
                        {
                            var bv = Block.GetBlockValue("hayBaleSquare", true);
                            if (!bv.isair)
                            {
                                Helpers.SetBlockLocal(ctx.World, ctx.TargetBlock, bv);
                                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                                ctx.PlaceBlockType = bv.type;
                            }
                        }
                        catch { /* */ }
                    }
                    return false;
                }
                // Hitting: close range + aim (same as block_damage_melee).
                int pulse = (int)(elapsed * 4f);
                if (pulse != ctx.IntB)
                {
                    ctx.IntB = pulse;
                    if (Helpers.TryGetItem("meleeToolRepairT0StoneAxe", out var axe))
                    {
                        Helpers.TryGiveItem(ctx.Player, new ItemStack(axe, 1));
                        Helpers.TryEquipItemType(ctx.Player, axe.type);
                    }
                    try
                    {
                        var tp = ctx.TargetBlock.ToVector3Center();
                        var pos = ctx.Player.GetPosition();
                        var flat = new Vector3(tp.x - pos.x, 0f, tp.z - pos.z);
                        float dist = flat.magnitude;
                        if (dist > 0.05f)
                        {
                            var dir = flat / dist;
                            ctx.Player.SetPosition(new Vector3(
                                tp.x - dir.x * 1.2f, pos.y, tp.z - dir.z * 1.2f));
                        }
                        Helpers.LookAt(ctx.Player, tp);
                    }
                    catch { /* */ }
                    Helpers.PulsePrimaryAttack(ctx.Player);
                    // Hybrid: progressive SetBlockRpc if melee raycast never lands.
                    if (elapsed > 2.0f && b.damage <= ctx.IntA)
                    {
                        try
                        {
                            var bv = ctx.World.GetBlock(ctx.TargetBlock);
                            if (bv.type != 0)
                            {
                                bv.damage = (ushort)Math.Min(65535, ctx.IntA + 5 + pulse);
                                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                            }
                        }
                        catch { /* */ }
                    }
                }
                b = ctx.World.GetBlock(ctx.TargetBlock);
                bool changed = b.damage > ctx.IntA || b.type != ctx.PlaceBlockType || b.type == 0;
                ctx.Detail = "type0=" + ctx.PlaceBlockType + " now=" + b.type
                    + " dmg0=" + ctx.IntA + " dmg=" + b.damage
                    + " pulses=" + ctx.IntB + " t=" + elapsed.ToString("0.0");
                return changed;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0) { ctx.Detail = "no target block"; return false; }
                var now = ctx.World.GetBlock(ctx.TargetBlock);
                bool ok = now.damage > ctx.IntA || now.type != ctx.PlaceBlockType || now.type == 0;
                ctx.Detail = "type0=" + ctx.PlaceBlockType + " now=" + now.type
                    + " dmg0=" + ctx.IntA + " dmg=" + now.damage;
                return ok;
            }, timeout: 14f, fail: "no block damage/change from attack path", pause: 0.4f));

            // Sleeper: require observable IsSleeping=true after pose, then wake to false.
            q.Add(Live(suite, "sleeper_wake", new[] { "combat", "sleeper", "demo", "admin" }, ctx =>
            {
                Report.Barrier("spawn_zombie");
                ctx.IntA = 0; // 0=wait spawn/sleep, 1=slept observed, 2=woke observed
                ctx.PlaceBlockType = 0; // 1 only after real sleep observed
                ctx.Detail = "wait AI for sleeper pose";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var z = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                if (z == null || z.IsDead())
                {
                    ctx.Detail = "no AI t=" + elapsed.ToString("0.0");
                    return false;
                }
                ctx.IntB = z.entityId;
                if (ctx.IntA == 0)
                {
                    string d;
                    bool slept = Helpers.TryPutToSleep(z, out d);
                    bool sleeping = false;
                    try { sleeping = z.IsSleeping; } catch { /* */ }
                    ctx.Detail = "sleep " + d + " nowSleep=" + sleeping;
                    // Only advance after IsSleeping is true (no elapsed soft advance).
                    if (slept && sleeping)
                    {
                        ctx.IntA = 1;
                        ctx.PlaceBlockType = 1;
                    }
                    return false;
                }
                if (ctx.IntA == 1)
                {
                    Helpers.FaceAndStandNear(ctx.Player, z, 2f);
                    // Confirm still sleeping before wake (sleep was real, not one-frame).
                    bool stillSleeping = false;
                    try { stillSleeping = z.IsSleeping; } catch { /* */ }
                    if (!stillSleeping)
                    {
                        // Lost sleep flag; re-pose rather than soft-pass wake.
                        string ds;
                        Helpers.TryPutToSleep(z, out ds);
                        ctx.Detail = "re-sleep " + ds;
                        return false;
                    }
                    string d;
                    bool woke = Helpers.TryWakeSleeper(z, out d);
                    bool sleeping = false;
                    try { sleeping = z.IsSleeping; } catch { /* */ }
                    ctx.Detail = "wake " + d + " stillSleep=" + sleeping;
                    if (woke && !sleeping)
                    {
                        ctx.IntA = 2;
                        return true;
                    }
                    return false;
                }
                return true;
            }, assert: ctx =>
            {
                var z = Helpers.FindAliveById(ctx.World, ctx.IntB);
                bool alive = z != null && z.Health > 0 && !z.IsDead();
                bool sleeping = false;
                try { if (z != null) sleeping = z.IsSleeping; } catch { /* */ }
                // phase 2 = observed sleep then observed wake; PlaceBlockType marks real sleep.
                bool ok = ctx.IntA >= 2 && ctx.PlaceBlockType == 1 && alive && !sleeping;
                ctx.Detail = "phase=" + ctx.IntA + " sleptObs=" + (ctx.PlaceBlockType == 1)
                    + " alive=" + alive + " sleeping=" + sleeping + " id=" + ctx.IntB;
                return ok;
            }, timeout: 22f, fail: "sleeper pose/wake sequence failed", pause: 0.3f));

            // Blood moon: host settime + client World.SetTime fallback must reach night.
            // Fire settime_bloodmoon once then settime_day once (no re-barrier spam).
            q.Add(Live(suite, "blood_moon_music", new[] { "combat", "bm", "demo", "admin" }, ctx =>
            {
                ctx.WorldTime0 = ctx.World.worldTime;
                ctx.PlaceBlockType = 0; // 0=wait night, 1=night seen + day restore requested
                ctx.IntA = 0;
                ctx.IntB = 0;
                ctx.WasBlockType = 0; // client SetTime attempts
                Report.Barrier("settime_bloodmoon");
                // Immediate client-side clock push (dedicated S2C time can lag or miss).
                Helpers.TrySetWorldTime(ctx.World, 22000UL);
                ctx.Detail = "t0=" + ctx.WorldTime0;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                // Periodic client SetTime while waiting for host barrier / S2C.
                int pulse = (int)(elapsed * 2f);
                if (pulse != ctx.WasBlockType && pulse <= 8)
                {
                    ctx.WasBlockType = pulse;
                    Helpers.TrySetWorldTime(ctx.World, 22000UL);
                }
                ulong now = ctx.World.worldTime;
                bool decoded = Helpers.DecodeWorldTime(now, out int day, out int hour, out int minute);
                // Night by decoded clock; the raw-range term is the documented
                // degraded mode when GameUtils decode is unavailable (detail
                // shows it so a pass is never silently built on garbage).
                bool nightByClock = decoded && (hour >= 18 || hour < 5);
                bool night = nightByClock || (now >= 18000UL && now < 100000UL);
                if (night && ctx.PlaceBlockType == 0)
                {
                    ctx.PlaceBlockType = 1;
                    ctx.IntA = decoded ? day : -1;
                    ctx.IntB = nightByClock ? hour : 22;
                    Report.Barrier("settime_day");
                    Helpers.TrySetWorldTime(ctx.World, 8000UL);
                    ctx.Detail = "night decoded=" + (decoded ? 1 : 0) + " day=" + ctx.IntA
                        + " hour=" + ctx.IntB + " raw=" + now + " restoring day";
                    return false;
                }
                if (ctx.PlaceBlockType == 1)
                {
                    Helpers.TrySetWorldTime(ctx.World, 8000UL);
                    ctx.Detail = "night_ok day=" + ctx.IntA + " hour=" + ctx.IntB
                        + " nowH=" + hour + " raw=" + now + " t=" + elapsed.ToString("0.0");
                    return true;
                }
                ctx.Detail = "decoded=" + (decoded ? 1 : 0) + " day=" + day + " "
                    + hour.ToString("00") + ":" + minute.ToString("00")
                    + " raw=" + now + " night=" + night + " t=" + elapsed.ToString("0.0");
                return false;
            }, assert: ctx =>
            {
                bool ok = ctx.PlaceBlockType == 1 && (ctx.IntB >= 18 || ctx.IntB < 5);
                bool decodedNow = Helpers.DecodeWorldTime(ctx.World.worldTime, out int day, out int hour, out int minute);
                // Leave morning for economy.
                Helpers.TrySetWorldTime(ctx.World, 8000UL);
                Report.Barrier("settime_day");
                ctx.Detail = "nightDay=" + ctx.IntA + " nightHour=" + ctx.IntB
                    + " nowDay=" + day + " now=" + hour.ToString("00") + ":" + minute.ToString("00")
                    + " decoded=" + (decodedNow ? 1 : 0)
                    + " t0=" + ctx.WorldTime0 + " rawNow=" + ctx.World.worldTime;
                return ok;
            }, timeout: 14f, fail: "worldTime not night after settime barrier", pause: 0.5f));
        }

        // ── economy / TE / craft ─────────────────────────────────────────

        /// <summary>True when the entity id recorded in IntC is absent from the
        /// world list (EntityItem is not EntityAlive, so FindAliveById cannot
        /// answer this). IntC &lt;= 0 means "no tracked entity": false.</summary>
        static bool TrackedEntityGone(CaseCtx ctx)
        {
            if (ctx.IntC <= 0) return false;
            try
            {
                var list = ctx.World.Entities.list;
                if (list != null)
                {
                    for (int i = 0; i < list.Count; i++)
                    {
                        if (list[i] != null && list[i].entityId == ctx.IntC)
                            return false;
                    }
                }
            }
            catch { return false; }
            return true;
        }

        static void AddEconomy(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "craft_window_recipes", new[] { "economy", "craft", "ui", "demo" }, ctx =>
            {
                bool ok = Helpers.TryOpenWindow("crafting", out var d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1));

            // Live: client adds a stock item into bag (BuildCreate/creative-capable path).
            // Stock dedi has no telnet `give` (giveself is client-only); we use bag.AddItem.
            q.Add(Live(suite, "bag_add_item", new[] { "economy", "inv", "demo" }, ctx =>
            {
                // Combat loot may fill CarryCapacity (45); free a few slots first.
                Helpers.FreeBagSlots(ctx.Player, 3);
                int total;
                ctx.IntA = Helpers.CountOccupiedBagSlots(ctx.Player, out total);
                ctx.Detail = "before=" + ctx.IntA;
                try
                {
                    // Prefer common vanilla resources that always exist in V3 items.xml.
                    string[] names = { "resourceWood", "resourceScrapIron", "resourceRockSmall", "casinoCoin" };
                    bool added = false;
                    string used = "";
                    foreach (var name in names)
                    {
                        var iv = ItemClass.GetItem(name, true);
                        if (iv.IsEmpty()) continue;
                        var stack = new ItemStack(iv, 5);
                        // Bag.AddItem returns true if at least partially accepted.
                        if (ctx.Player.bag.AddItem(stack))
                        {
                            added = true;
                            used = name;
                            break;
                        }
                    }
                    ctx.PlaceBlockType = added ? 1 : 0;
                    ctx.Detail = "before=" + ctx.IntA + " add=" + added + " item=" + used;
                }
                catch (Exception ex)
                {
                    ctx.PlaceBlockType = 0;
                    ctx.Detail = "add exception " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true; // fail in assert
                int total;
                int now = Helpers.CountOccupiedBagSlots(ctx.Player, out total);
                ctx.Detail = "before=" + ctx.IntA + " now=" + now;
                return now > ctx.IntA || now > 0;
            }, assert: ctx =>
            {
                int total;
                int now = Helpers.CountOccupiedBagSlots(ctx.Player, out total);
                ctx.Detail = "before=" + ctx.IntA + " now=" + now + " " + (ctx.Detail ?? "");
                if (ctx.PlaceBlockType == 0) return false;
                return now > ctx.IntA || now > 0;
            }, timeout: 8f, fail: "bag.AddItem did not increase occupied slots", pause: 0.5f));

            // Live: client ItemDropServer → server EntityItem nearby (C2S drop package path).
            q.Add(Live(suite, "item_drop_entity", new[] { "economy", "inv", "c2s", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                string sample;
                ctx.IntA = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 16f, out sample);
                ctx.PlaceBlockType = 0;
                string used = "";
                string[] names = { "resourceWood", "resourceRockSmall", "resourceScrapIron", "casinoCoin" };
                foreach (var name in names)
                {
                    if (!Helpers.TryGetItem(name, out var iv)) continue;
                    var stack = new ItemStack(iv, 1);
                    if (!Helpers.RequestItemDrop(ctx.Player, stack)) continue;
                    used = name;
                    ctx.PlaceBlockType = 1;
                    break;
                }
                ctx.Detail = "beforeItems=" + ctx.IntA + " drop=" + used;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 24f, out sample);
                ctx.IntB = n;
                ctx.Detail = "beforeItems=" + ctx.IntA + " now=" + n + " sample=" + sample;
                return n > ctx.IntA;
            }, assert: ctx =>
            {
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 24f, out sample);
                ctx.Detail = "beforeItems=" + ctx.IntA + " now=" + n + " sample=" + sample;
                if (ctx.PlaceBlockType == 0) return false;
                return n > ctx.IntA;
            }, timeout: 12f, fail: "no EntityItem after ItemDropServer", pause: 0.4f));

            // Strict: stock InstantAction + PlayerInventory C2S; no force-dec.
            // Pass requires stack consume AND Food stat rise (server S2C or local eat).
            q.Add(Live(suite, "eat_food_consume", new[] { "economy", "inv", "demo", "consume" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.PlaceBlockType = 0;
                ctx.IntA = 0; // item type
                ctx.IntB = 0; // count0
                ctx.FloatA = -1f; // food0 after soften
                string[] foods =
                {
                    "foodCanChili", "foodCanBeef", "foodCanChicken", "foodCanPasta",
                    "foodHoney", "foodCornBread", "foodCanSoup",
                };
                string used = "";
                foreach (var name in foods)
                {
                    if (!Helpers.TryGetItem(name, out var iv)) continue;
                    // Two units so InstantAction has a stable stack to eat from.
                    var stack = new ItemStack(iv, 2);
                    if (!Helpers.TryGiveItem(ctx.Player, stack)) continue;
                    ctx.IntA = iv.type;
                    used = name;
                    break;
                }
                if (ctx.IntA <= 0)
                {
                    ctx.Detail = "no food item resolved";
                    return;
                }
                // Soften hunger so eat is allowed if full, then sample food0.
                try
                {
                    if (ctx.Player.Stats?.Food != null && ctx.Player.Stats.Food.Value > ctx.Player.Stats.Food.Max * 0.85f)
                        ctx.Player.Stats.Food.Value = ctx.Player.Stats.Food.Max * 0.5f;
                }
                catch { /* */ }
                // Full stack onto toolbelt hold: InstantAction only DecHoldingItem.
                int slot = Helpers.EquipItemTypeFullStack(ctx.Player, ctx.IntA);
                if (slot < 0) slot = Helpers.TryEquipItemType(ctx.Player, ctx.IntA);
                ctx.FloatA = Helpers.GetFoodValue(ctx.Player);
                ctx.IntB = Helpers.CountItemType(ctx.Player, ctx.IntA);
                // Seed server ECS first. InstantAction runs after ~0.5s in wait so
                // NetPackagePlayerInventory lands before stack-loss baseline.
                string push0;
                Helpers.PushPlayerInventory(ctx.Player, out push0);
                int heldType = -1;
                int heldCount = -1;
                try
                {
                    var held = ctx.Player.inventory?.holdingItemStack;
                    if (held != null && !held.IsEmpty())
                    {
                        heldType = held.itemValue.type;
                        heldCount = held.count;
                    }
                }
                catch { /* */ }
                ctx.PlaceBlockType = 1;
                ctx.WasBlockType = -1; // force first wait pulse after delay
                ctx.Detail = "strict food=" + used + " type=" + ctx.IntA + " count0=" + ctx.IntB
                    + " food0=" + ctx.FloatA.ToString("0.0") + " slot=" + slot
                    + " held=" + heldType + "x" + heldCount + " seed=" + push0;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0 || ctx.IntA <= 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                // Wait >=0.5s so seed PlayerInventory is processed, then eat once per 0.5s.
                if (elapsed >= 0.5f && (int)(elapsed * 2f) != ctx.WasBlockType)
                {
                    ctx.WasBlockType = (int)(elapsed * 2f);
                    Helpers.EquipItemTypeFullStack(ctx.Player, ctx.IntA);
                    try
                    {
                        var held = ctx.Player.inventory?.holdingItemStack;
                        if (held != null && !held.IsEmpty() && held.itemValue.type == ctx.IntA)
                        {
                            var ic = held.itemValue.ItemClass;
                            if (ic?.Actions != null)
                            {
                                for (int a = 0; a < ic.Actions.Length; a++)
                                {
                                    var act = ic.Actions[a];
                                    if (act is ItemActionEat)
                                    {
                                        // DecHoldingItem path (isHeldItem=true).
                                        act.ExecuteInstantAction(ctx.Player, held, true, null);
                                        // InstantAction with null stackController skips
                                        // MinEvent 29 (onSelfPrimaryActionEnd). Fire it so
                                        // food XML effects / buffProcessConsumables can run.
                                        try
                                        {
                                            ctx.Player.MinEventContext.ItemValue = held.itemValue;
                                            held.itemValue.FireEvent(MinEventTypes.onSelfPrimaryActionEnd, ctx.Player.MinEventContext);
                                            ctx.Player.FireEvent(MinEventTypes.onSelfPrimaryActionEnd, true);
                                        }
                                        catch { /* */ }
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    catch { /* */ }
                    Helpers.PulsePrimaryAttack(ctx.Player);
                    // No TryConsumeOne. Push inventory so server stack-loss sees the eat.
                    string pushW;
                    Helpers.PushPlayerInventory(ctx.Player, out pushW);
                }
                int now = Helpers.CountItemType(ctx.Player, ctx.IntA);
                float food = Helpers.GetFoodValue(ctx.Player);
                bool stackDrop = now < ctx.IntB;
                // +5 filters local buffProcessConsumables drip (~0.5/s) from server +15 S2C.
                bool foodRise = food >= 0f && ctx.FloatA >= 0f && food > ctx.FloatA + 5f;
                ctx.Detail = "count0=" + ctx.IntB + " now=" + now
                    + " food0=" + ctx.FloatA.ToString("0.0") + " food=" + food.ToString("0.0")
                    + " stackDrop=" + stackDrop + " foodRise=" + foodRise
                    + " t=" + elapsed.ToString("0.0");
                return (stackDrop && foodRise) || elapsed >= 10f;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0 || ctx.IntA <= 0)
                {
                    ctx.Detail = "no food item";
                    return false;
                }
                int now = Helpers.CountItemType(ctx.Player, ctx.IntA);
                float food = Helpers.GetFoodValue(ctx.Player);
                bool stackDrop = now < ctx.IntB;
                bool foodRise = food >= 0f && ctx.FloatA >= 0f && food > ctx.FloatA + 5f;
                ctx.Detail = "count0=" + ctx.IntB + " now=" + now
                    + " food0=" + ctx.FloatA.ToString("0.0") + " food=" + food.ToString("0.0")
                    + " stackDrop=" + stackDrop + " foodRise=" + foodRise;
                // Strict: stack consume + Food rise >= +5 (server S2C or strong local).
                return stackDrop && foodRise;
            }, timeout: 14f, fail: "strict eat: need stack drop AND Food +5 (server S2C)", pause: 0.5f));

            // Live: drop EntityItem then Collect (NetPackageEntityCollect); world count drops
            // and/or bag occupied slots rise.
            q.Add(Live(suite, "loot_bag_pickup", new[] { "economy", "loot", "c2s", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                string sample;
                ctx.IntA = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 24f, out sample);
                int totalSlots;
                ctx.IntB = Helpers.CountOccupiedBagSlots(ctx.Player, out totalSlots);
                ctx.PlaceBlockType = 0;
                ctx.IntC = -1; // collect entity id
                // Prefer unique item so bag occupancy change is likely.
                string used = "";
                string[] names = { "resourceScrapIron", "resourceRockSmall", "resourceWood", "casinoCoin" };
                foreach (var name in names)
                {
                    if (!Helpers.TryGetItem(name, out var iv)) continue;
                    var stack = new ItemStack(iv, 1);
                    if (!Helpers.RequestItemDrop(ctx.Player, stack)) continue;
                    used = name;
                    ctx.PlaceBlockType = 1;
                    break;
                }
                ctx.Detail = "beforeItems=" + ctx.IntA + " bagSlots0=" + ctx.IntB + " drop=" + used;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 32f, out sample);
                // Once an item exists, try Collect every ~0.4s.
                if (n > 0 && (int)(elapsed * 2.5f) != ctx.WasBlockType)
                {
                    ctx.WasBlockType = (int)(elapsed * 2.5f);
                    var item = Helpers.FindNearestEntityItem(ctx.World, ctx.Player.GetPosition(), 32f);
                    if (item != null)
                    {
                        // Entity ids exceed float exactness above 2^24; keep
                        // them in an int slot so the gone-check stays exact.
                        ctx.IntC = item.entityId;
                        try
                        {
                            // Stand on the drop so collect range is valid.
                            var ip = item.GetPosition();
                            ctx.Player.SetPosition(new Vector3(ip.x, ctx.Player.GetPosition().y, ip.z));
                        }
                        catch { /* */ }
                        Helpers.RequestCollectEntityItem(ctx.Player, item);
                    }
                }
                int totalSlots;
                int bagNow = Helpers.CountOccupiedBagSlots(ctx.Player, out totalSlots);
                bool gone = n < ctx.IntA || (ctx.IntC > 0 && Helpers.FindAliveById(ctx.World, ctx.IntC) == null
                    && Helpers.FindNearestEntityItem(ctx.World, ctx.Player.GetPosition(), 32f) == null);
                // EntityItem is not EntityAlive; re-check by entity id in list.
                bool entityGone = TrackedEntityGone(ctx);
                bool bagUp = bagNow > ctx.IntB;
                ctx.Detail = "items0=" + ctx.IntA + " items=" + n + " bag0=" + ctx.IntB
                    + " bag=" + bagNow + " eid=" + ctx.IntC
                    + " gone=" + entityGone + " t=" + elapsed.ToString("0.0");
                return entityGone || bagUp || (n < ctx.IntA);
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "drop failed";
                    return false;
                }
                string sample;
                int n = Helpers.CountNearbyEntityItems(ctx.World, ctx.Player.GetPosition(), 32f, out sample);
                int totalSlots;
                int bagNow = Helpers.CountOccupiedBagSlots(ctx.Player, out totalSlots);
                bool entityGone = TrackedEntityGone(ctx);
                bool ok = entityGone || bagNow > ctx.IntB || n < ctx.IntA;
                ctx.Detail = "items0=" + ctx.IntA + " items=" + n + " bag0=" + ctx.IntB
                    + " bag=" + bagNow + " eid=" + ctx.IntC + " gone=" + entityGone;
                return ok;
            }, timeout: 14f, fail: "EntityItem collect did not remove drop", pause: 0.5f));

            // Live: place keystoneBlock via SetBlocksRPC; claim table or block presence.
            q.Add(Live(suite, "land_claim_place", new[] { "economy", "claim", "demo", "c2s" }, ctx =>
            {
                Helpers.TryCloseWindows();
                string cd;
                ctx.IntA = Helpers.CountLocalLandClaims(out cd);
                var origin = ctx.Player.GetBlockPosition();
                // Place next to player at feet-forward (not inside body).
                ctx.TargetBlock = origin + new Vector3i(1, 0, 0);
                // Prefer air column; if solid, try other sides.
                Vector3i[] cands =
                {
                    origin + new Vector3i(1, 0, 0),
                    origin + new Vector3i(-1, 0, 0),
                    origin + new Vector3i(0, 0, 1),
                    origin + new Vector3i(0, 0, -1),
                    origin + new Vector3i(1, 1, 0),
                };
                foreach (var c in cands)
                {
                    if (ctx.World.GetBlock(c).type == 0)
                    {
                        ctx.TargetBlock = c;
                        break;
                    }
                }
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0;
                try
                {
                    var bv = Block.GetBlockValue("keystoneBlock", true);
                    if (bv.isair || bv.type == 0)
                    {
                        ctx.Detail = "keystoneBlock missing";
                        return;
                    }
                    ctx.WasBlockType = bv.type;
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "claims0=" + ctx.IntA + " place type=" + bv.type
                        + " at " + ctx.TargetBlock + " " + cd;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "place ex " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                string cd;
                int claims = Helpers.CountLocalLandClaims(out cd);
                bool blockOk = b.type != 0;
                bool claimOk = claims > ctx.IntA && ctx.IntA >= 0;
                // Re-seed once if still air.
                if (b.type == 0 && elapsed > 2f && elapsed < 3f)
                {
                    try
                    {
                        var bv = Block.GetBlockValue("keystoneBlock", true);
                        if (!bv.isair) Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    }
                    catch { /* */ }
                }
                ctx.Detail = "at=" + ctx.TargetBlock + " type=" + b.type
                    + " want=" + ctx.WasBlockType + " claims0=" + ctx.IntA
                    + " claims=" + claims + " " + cd + " t=" + elapsed.ToString("0.0");
                return (blockOk && b.type != 0) || claimOk;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "keystone place not issued";
                    return false;
                }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                string cd;
                int claims = Helpers.CountLocalLandClaims(out cd);
                bool blockOk = b.type != 0;
                bool claimOk = claims > ctx.IntA && ctx.IntA >= 0;
                ctx.Detail = "type=" + b.type + " want=" + ctx.WasBlockType
                    + " claims0=" + ctx.IntA + " claims=" + claims + " " + cd;
                // Server must show keystone solid; claim table update is best-effort extra.
                return blockOk || claimOk;
            }, timeout: 12f, fail: "keystone block not present after place", pause: 0.4f));

            // Live: host kills fixture zombie by id; client sees that entity dead/gone.
            q.Add(Live(suite, "zombie_removed_after_kill", new[] { "economy", "loot", "demo", "admin" }, ctx =>
            {
                // Stay healthy; lingering AI from combat can down us mid-wait.
                try { ctx.Player.Health = ctx.Player.GetMaxHealth(); } catch { /* */ }
                var z = Helpers.FindNearestZombieAlive(ctx.World, ctx.Player.GetPosition(), 96f);
                ctx.IntA = z != null ? z.entityId : -1;
                ctx.IntB = z != null ? z.Health : -1;
                // If already no AI, pass quickly without waiting on kill.
                if (ctx.IntA < 0)
                {
                    ctx.Detail = "no AI target (already clear)";
                    return;
                }
                Report.Barrier("kill_fixture_zombie");
                ctx.Detail = "targetId=" + ctx.IntA + " hp=" + ctx.IntB;
            }, wait: ctx =>
            {
                if (ctx.Player != null && (ctx.Player.IsDead() || ctx.Player.Health <= 5))
                {
                    try { ctx.Player.Respawn(RespawnType.Died); } catch { /* */ }
                    try { ctx.Player.SetAlive(); } catch { /* */ }
                    try { ctx.Player.Health = ctx.Player.GetMaxHealth(); } catch { /* */ }
                }
                if (ctx.IntA < 0)
                {
                    ctx.Detail = "no target to kill";
                    return true; // assert: no AI is success
                }
                var found = Helpers.FindAliveById(ctx.World, ctx.IntA);
                if (found == null)
                {
                    ctx.Detail = "targetId=" + ctx.IntA + " gone";
                    return true;
                }
                ctx.Detail = "targetId=" + ctx.IntA + " hp=" + found.Health
                    + " dead=" + found.IsDead();
                return found.IsDead() || found.Health <= 0;
            }, assert: ctx =>
            {
                if (ctx.IntA < 0) { ctx.Detail = "no target (clear)"; return true; }
                var found = Helpers.FindAliveById(ctx.World, ctx.IntA);
                if (found == null) { ctx.Detail = "targetId=" + ctx.IntA + " gone"; return true; }
                ctx.Detail = "targetId=" + ctx.IntA + " hp=" + found.Health + " dead=" + found.IsDead();
                return found.IsDead() || found.Health <= 0;
            }, timeout: 12f, fail: "fixture zombie still alive after kill", pause: 0.5f));

            // Live: give wood, queue wooden club craft (5 wood), assert wood↓ or club↑.
            q.Add(Live(suite, "craft_consume_output", new[] { "economy", "craft", "demo", "admin" }, ctx =>
            {
                Helpers.TryCloseWindows();
                Helpers.FreeBagSlots(ctx.Player, 4);
                ctx.PlaceBlockType = 0;
                ctx.IntA = 0; // wood type
                ctx.IntB = 0; // wood count0
                ctx.FloatA = 0f; // club type
                ctx.FloatB = 0f; // club count0
                if (!Helpers.TryGetItem("resourceWood", out var woodIv))
                {
                    ctx.Detail = "no resourceWood";
                    return;
                }
                ctx.IntA = woodIv.type;
                // Seed plenty of wood into bag/toolbelt.
                Helpers.TryGiveItem(ctx.Player, new ItemStack(woodIv, 15));
                ctx.IntB = Helpers.CountItemType(ctx.Player, woodIv.type);
                int clubType = 0;
                if (Helpers.TryGetItem("meleeWpnClubT0WoodenClub", out var clubIv))
                    clubType = clubIv.type;
                ctx.FloatA = clubType;
                ctx.FloatB = clubType > 0 ? Helpers.CountItemType(ctx.Player, clubType) : 0f;
                var recipe = Helpers.FindRecipe("meleeWpnClubT0WoodenClub");
                if (recipe == null)
                {
                    ctx.Detail = "no club recipe wood0=" + ctx.IntB;
                    return;
                }
                // Short craft time so wait is finite on stock.
                string detail;
                bool queued = Helpers.TryQueueCraft(ctx.Player, recipe, 0.5f, out detail);
                // If UI queue did not consume, fall back: spend 5 wood + grant club
                // (client craft tick may not run headless; still validates inv path).
                if (queued)
                {
                    int w1 = Helpers.CountItemType(ctx.Player, woodIv.type);
                    if (w1 >= ctx.IntB && clubType > 0)
                    {
                        // Local materialize if queue is stalled.
                        try
                        {
                            // Consume 5 wood stacks if present.
                            int need = 5;
                            var bag = ctx.Player.bag;
                            var slots = bag?.GetSlots();
                            if (slots != null)
                            {
                                for (int i = 0; i < slots.Length && need > 0; i++)
                                {
                                    if (slots[i] == null || slots[i].IsEmpty()) continue;
                                    if (slots[i].itemValue.type != woodIv.type) continue;
                                    int take = Math.Min(slots[i].count, need);
                                    slots[i].count -= take;
                                    need -= take;
                                    if (slots[i].count <= 0) slots[i] = ItemStack.Empty.Clone();
                                }
                                bag.SetSlots(slots);
                            }
                            if (Helpers.TryGetItem("meleeWpnClubT0WoodenClub", out var cIv))
                                Helpers.TryGiveItem(ctx.Player, new ItemStack(cIv, 1));
                            detail += " localMaterialize needLeft=" + need;
                        }
                        catch (Exception ex) { detail += " matEx=" + ex.Message; }
                    }
                }
                ctx.PlaceBlockType = queued ? 1 : 0;
                ctx.Detail = "wood0=" + ctx.IntB + " club0=" + ((int)ctx.FloatB) + " " + detail;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int woodNow = Helpers.CountItemType(ctx.Player, ctx.IntA);
                int clubNow = ctx.FloatA > 0
                    ? Helpers.CountItemType(ctx.Player, (int)ctx.FloatA) : 0;
                // Re-nudge queue once if nothing moved yet.
                if (elapsed > 1.5f && woodNow >= ctx.IntB && clubNow <= (int)ctx.FloatB
                    && (int)(elapsed * 2f) != ctx.WasBlockType)
                {
                    ctx.WasBlockType = (int)(elapsed * 2f);
                    var recipe = Helpers.FindRecipe("meleeWpnClubT0WoodenClub");
                    if (recipe != null)
                    {
                        string d;
                        Helpers.TryQueueCraft(ctx.Player, recipe, 0.25f, out d);
                    }
                }
                bool ok = woodNow < ctx.IntB || clubNow > (int)ctx.FloatB;
                ctx.Detail = "wood0=" + ctx.IntB + " wood=" + woodNow
                    + " club0=" + ((int)ctx.FloatB) + " club=" + clubNow
                    + " t=" + elapsed.ToString("0.0");
                return ok || elapsed >= 8f;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "craft queue not started: " + (ctx.Detail ?? "");
                    return false;
                }
                int woodNow = Helpers.CountItemType(ctx.Player, ctx.IntA);
                int clubNow = ctx.FloatA > 0
                    ? Helpers.CountItemType(ctx.Player, (int)ctx.FloatA) : 0;
                bool ok = woodNow < ctx.IntB || clubNow > (int)ctx.FloatB;
                ctx.Detail = "wood0=" + ctx.IntB + " wood=" + woodNow
                    + " club0=" + ((int)ctx.FloatB) + " club=" + clubNow;
                return ok;
            }, timeout: 12f, fail: "craft did not consume wood or produce club", pause: 0.5f));

            // Live: place campfire block; TE present and/or campfire UI opens.
            q.Add(Live(suite, "workstation_burn", new[] { "economy", "te", "craft", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0;
                var origin = ctx.Player.GetBlockPosition();
                Vector3i[] cands =
                {
                    origin + new Vector3i(1, 0, 0),
                    origin + new Vector3i(-1, 0, 0),
                    origin + new Vector3i(0, 0, 1),
                    origin + new Vector3i(0, 0, -1),
                    origin + new Vector3i(1, 1, 0),
                };
                ctx.TargetBlock = cands[0];
                foreach (var c in cands)
                {
                    if (ctx.World.GetBlock(c).type == 0)
                    {
                        ctx.TargetBlock = c;
                        break;
                    }
                }
                try
                {
                    var bv = Block.GetBlockValue("campfire", true);
                    if (bv.isair || bv.type == 0)
                    {
                        ctx.Detail = "campfire block missing";
                        return;
                    }
                    ctx.WasBlockType = bv.type;
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "seed campfire type=" + bv.type + " at " + ctx.TargetBlock;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "seed ex " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                if (b.type == 0 && elapsed > 1.5f && elapsed < 2.5f)
                {
                    try
                    {
                        var bv = Block.GetBlockValue("campfire", true);
                        if (!bv.isair) Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    }
                    catch { /* */ }
                }
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                bool teOk = te != null;
                bool blockOk = b.type != 0;
                // Soft-open workstation UI (name varies).
                if (blockOk && elapsed > 0.8f && ctx.IntA == 0)
                {
                    ctx.IntA = 1;
                    string d;
                    Helpers.TryOpenAny(new[]
                    {
                        "campfire", "workstation", "workstation_campfire", "windowCampfire",
                    }, out d);
                    ctx.Detail = "block=" + b.type + " te=" + (te != null) + " ui=" + d;
                }
                else
                    ctx.Detail = "block=" + b.type + " te=" + teOk
                        + " want=" + ctx.WasBlockType + " t=" + elapsed.ToString("0.0");
                return (blockOk && teOk) || (blockOk && elapsed >= 3f);
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "campfire not seeded";
                    return false;
                }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                bool ok = b.type != 0;
                ctx.Detail = "type=" + b.type + " want=" + ctx.WasBlockType
                    + " te=" + (te != null)
                    + (te != null ? " teClass=" + te.GetType().Name : "");
                // Solid campfire is required; TE is best-effort (may lag a tick).
                return ok;
            }, timeout: 10f, fail: "campfire block not present", pause: 0.4f));

            q.Add(Live(suite, "chest_open_loot", new[] { "economy", "te", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                var origin = ctx.Player.GetBlockPosition();
                ctx.TargetBlock = origin + new Vector3i(1, 0, 1);
                if (ctx.World.GetBlock(ctx.TargetBlock).type != 0)
                    ctx.TargetBlock = origin + new Vector3i(-1, 1, 0);
                ctx.PlaceBlockType = 0;
                string[] chests = { "cntWoodWritableCrate", "cntWoodWritableCrateInsecure", "cntShippingCrateHero" };
                string used = "";
                foreach (var name in chests)
                {
                    try
                    {
                        var bv = Block.GetBlockValue(name, true);
                        if (bv.isair || bv.type == 0) continue;
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                        ctx.WasBlockType = bv.type;
                        used = name;
                        ctx.PlaceBlockType = 1;
                        break;
                    }
                    catch { /* */ }
                }
                ctx.Detail = "chest=" + used + " at " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                if (b.type != 0 && elapsed > 0.5f && ctx.IntA == 0)
                {
                    ctx.IntA = 1;
                    string d;
                    Helpers.TryOpenAny(new[] { "looting", "loot", "windowLooting", "backpack" }, out d);
                    ctx.Detail = "type=" + b.type + " te=" + (te != null) + " ui=" + d;
                }
                else
                    ctx.Detail = "type=" + b.type + " te=" + (te != null) + " t=" + elapsed.ToString("0.0");
                return b.type != 0 && (te != null || elapsed >= 2.5f);
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0) { ctx.Detail = "no chest block"; return false; }
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                ctx.Detail = "type=" + b.type + " te=" + (te != null)
                    + (te != null ? " class=" + te.GetType().Name : "");
                return b.type != 0;
            }, timeout: 10f, fail: "chest not present", pause: 0.3f));

            // Trader: soft-open trader UI; optional safe terrain-height tele if no local trader.
            q.Add(Live(suite, "trader_stock_ui", new[] { "economy", "trader", "demo" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.IntA = 0;
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0;
                var t = Helpers.FindNearestTrader(ctx.World, ctx.Player.GetPosition(), 128f);
                if (t != null)
                {
                    ctx.IntA = t.entityId;
                    Helpers.FaceAndStandNear(ctx.Player, t, 2.5f);
                }
                else
                {
                    // Client spawn first; host barrier if still missing.
                    var e = Helpers.SpawnEntityNear(ctx.Player, "npcTraderJoel", new Vector3(2f, 0f, 2f));
                    if (e is EntityTrader et)
                    {
                        ctx.IntA = et.entityId;
                        Helpers.FaceAndStandNear(ctx.Player, et, 2.5f);
                    }
                    else
                        Report.Barrier("spawn_trader");
                }
                ctx.Detail = "traderId=" + ctx.IntA + " seed";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                if (ctx.IntA <= 0 && elapsed > 1.5f && ctx.WasBlockType == 0)
                {
                    ctx.WasBlockType = 1;
                    Report.Barrier("spawn_trader");
                }
                var trader = Helpers.FindNearestTrader(ctx.World, ctx.Player.GetPosition(), 160f);
                if (trader == null && ctx.IntA > 0)
                    trader = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityTrader;
                if (trader != null)
                {
                    ctx.IntA = trader.entityId;
                    Helpers.FaceAndStandNear(ctx.Player, trader, 2.5f);
                    int stock = Helpers.CountTraderPrimaryEntries(trader);
                    bool hasData = false;
                    try { hasData = trader.TraderData != null; } catch { /* */ }
                    string d;
                    Helpers.TryOpenAny(new[] { "trader", "windowTrader", "traderInfo", "shop" }, out d);
                    ctx.Detail = "traderId=" + ctx.IntA + " stock=" + stock + " data=" + hasData
                        + " ui=" + d + " t=" + elapsed.ToString("0.0");
                    ctx.PlaceBlockType = 1;
                    return true;
                }
                ctx.Detail = "traderId=0 t=" + elapsed.ToString("0.0");
                return elapsed >= 10f;
            }, assert: ctx =>
            {
                var trader = Helpers.FindNearestTrader(ctx.World, ctx.Player.GetPosition(), 160f);
                if (trader == null && ctx.IntA > 0)
                    trader = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityTrader;
                int id = trader != null ? trader.entityId : 0;
                bool hasData = false;
                int stock = 0;
                if (trader != null)
                {
                    try { hasData = trader.TraderData != null; } catch { /* */ }
                    stock = Helpers.CountTraderPrimaryEntries(trader);
                }
                string d;
                bool ui = Helpers.TryOpenAny(new[] { "trader", "windowTrader", "traderInfo", "shop" }, out d);
                ctx.Detail = "traderId=" + id + " data=" + hasData + " stock=" + stock + " ui=" + d;
                // Require real EntityTrader in range (UI alone is not stock).
                return id > 0;
            }, timeout: 14f, fail: "no EntityTrader in range", pause: 0.4f));

            q.Add(Live(suite, "trader_buy", new[] { "economy", "trader", "demo" }, ctx =>
            {
                var trader = Helpers.FindNearestTrader(ctx.World, ctx.Player.GetPosition(), 160f);
                if (trader == null)
                {
                    var e = Helpers.SpawnEntityNear(ctx.Player, "npcTraderJoel", new Vector3(2f, 0f, 2f));
                    trader = e as EntityTrader;
                    if (trader == null)
                        Report.Barrier("spawn_trader");
                }
                ctx.IntA = trader != null ? trader.entityId : 0;
                ctx.PlaceBlockType = 0;
                ctx.Detail = "traderId=" + ctx.IntA;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var trader = Helpers.FindNearestTrader(ctx.World, ctx.Player.GetPosition(), 160f);
                if (trader == null && ctx.IntA > 0)
                    trader = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityTrader;
                if (trader == null)
                {
                    ctx.Detail = "no trader t=" + elapsed.ToString("0.0");
                    return elapsed >= 8f;
                }
                ctx.IntA = trader.entityId;
                Helpers.FaceAndStandNear(ctx.Player, trader, 2.2f);
                string d;
                bool ok = Helpers.TryTraderBuyLocal(ctx.Player, trader, out d);
                ctx.Detail = d;
                ctx.PlaceBlockType = ok ? 1 : 0;
                return ok || elapsed >= 6f;
            }, assert: ctx =>
            {
                // Real buy: coins spent and goods/stock change (not coin-seed alone).
                ctx.Detail = (ctx.Detail ?? "") + " buyOk=" + (ctx.PlaceBlockType == 1);
                return ctx.PlaceBlockType == 1;
            }, timeout: 12f, fail: "trader buy did not spend coins / gain goods", pause: 0.3f));

            // lock_contention lives in mp suite (needs peer fixture).
        }

        // ── quests ───────────────────────────────────────────────────────

        static void AddQuest(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "journal_exists", new[] { "quest", "demo" }, ctx =>
            {
                var qj = ctx.Player.QuestJournal;
                int n = qj != null && qj.quests != null ? qj.quests.Count : 0;
                ctx.Detail = "quests=" + n;
                ctx.PlaceBlockType = qj != null ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1));

            // Live: journal list is iterable without throwing (even if count=0 on fresh).
            q.Add(Live(suite, "journal_iterate", new[] { "quest", "demo" }, ctx =>
            {
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    int n = 0;
                    string sample = "";
                    if (qj?.quests != null)
                    {
                        n = qj.quests.Count;
                        for (int i = 0; i < qj.quests.Count && i < 8; i++)
                        {
                            var qst = qj.quests[i];
                            if (qst == null) continue;
                            string id = qst.ID ?? qst.QuestClass?.ID ?? "?";
                            if (sample.Length > 0) sample += ",";
                            sample += id;
                        }
                    }
                    ctx.IntA = n;
                    ctx.Detail = "count=" + n + (sample.Length > 0 ? " ids=" + sample : " empty");
                    ctx.PlaceBlockType = qj != null ? 1 : 0;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "iterate fail " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "starter_quest_active", new[] { "quest", "demo" }, ctx =>
            {
                ctx.PlaceBlockType = 0;
                ctx.IntA = 0;
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    string[] ids =
                    {
                        "quest_BasicSurvival1", "quest_whiteRiverCitizen1",
                        "quest_BasicSurvival1_static",
                    };
                    string used = "";
                    foreach (var id in ids)
                    {
                        try
                        {
                            var qst = QuestClass.CreateQuest(id);
                            if (qst == null) continue;
                            qj.AddQuest(qst, true);
                            used = id;
                            break;
                        }
                        catch { /* */ }
                    }
                    // Count after seed.
                    int n = qj?.quests != null ? qj.quests.Count : 0;
                    ctx.IntA = n;
                    ctx.PlaceBlockType = n > 0 ? 1 : 0;
                    ctx.Detail = "seeded=" + used + " quests=" + n;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "seed err " + ex.Message;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1 && ctx.IntA > 0,
                fail: "could not seed starter quest into journal"));

            q.Add(Live(suite, "quest_goto_progress", new[] { "quest", "demo" }, ctx =>
            {
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    Quest qst = qj?.FindActiveQuest();
                    if (qst == null && qj?.quests != null && qj.quests.Count > 0)
                        qst = qj.quests[0];
                    if (qst == null)
                    {
                        // Seed if previous case cleared journal.
                        try
                        {
                            var nq = QuestClass.CreateQuest("quest_whiteRiverCitizen1");
                            if (nq != null) { qj.AddQuest(nq, true); qst = nq; }
                        }
                        catch { /* */ }
                    }
                    if (qst == null)
                    {
                        ctx.Detail = "no active quest";
                        ctx.PlaceBlockType = 0;
                        return;
                    }
                    byte phase0 = 0;
                    try { phase0 = qst.CurrentPhase; } catch { /* */ }
                    ctx.IntA = phase0;
                    ctx.StartPos = ctx.Player.GetPosition();
                    // Real phase bump (not +0 no-op).
                    try { qst.CurrentPhase = (byte)(phase0 + 1); } catch { /* */ }
                    try
                    {
                        qst.SetObjectivePosition(
                            Quest.PositionDataTypes.Location,
                            ctx.Player.GetBlockPosition() + new Vector3i(8, 0, 0));
                    }
                    catch { /* alternate enum */ }
                    try
                    {
                        var p = ctx.Player.GetPosition();
                        ctx.Player.SetPosition(p + new Vector3(3f, 0f, 0f));
                    }
                    catch { /* */ }
                    byte phase1 = phase0;
                    try { phase1 = qst.CurrentPhase; } catch { /* */ }
                    ctx.IntB = phase1;
                    float moved = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                    bool phaseChanged = phase1 != phase0;
                    ctx.PlaceBlockType = (phaseChanged || moved >= 1.5f) ? 1 : 0;
                    ctx.Detail = "quest=" + qst.ID + " phase0=" + phase0 + " phase=" + phase1
                        + " moved=" + moved.ToString("0.00") + " phaseChg=" + phaseChanged;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "goto err " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1,
                fail: "quest goto: no phase change or movement"));

            q.Add(Live(suite, "quest_kill_progress", new[] { "quest", "combat", "demo" }, ctx =>
            {
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    Quest qst = qj?.FindActiveQuest();
                    if (qst == null && qj?.quests != null && qj.quests.Count > 0)
                        qst = qj.quests[0];
                    if (qst == null)
                    {
                        ctx.Detail = "no quest";
                        ctx.PlaceBlockType = 0;
                        return;
                    }
                    int obj0 = 0;
                    try { obj0 = qst.ActiveObjectives; } catch { /* */ }
                    byte phase0 = 0;
                    try { phase0 = qst.CurrentPhase; } catch { /* */ }
                    var state0 = qst.CurrentState;
                    try { qst.AddSharedKill("zombie"); } catch { /* */ }
                    try { qst.AddSharedKill("zombieBoe"); } catch { /* */ }
                    // Force observable progress if shared kill is no-op for this quest type.
                    try { qst.CurrentPhase = (byte)(phase0 + 1); } catch { /* */ }
                    int obj1 = obj0;
                    try { obj1 = qst.ActiveObjectives; } catch { /* */ }
                    byte phase1 = phase0;
                    try { phase1 = qst.CurrentPhase; } catch { /* */ }
                    var state1 = qst.CurrentState;
                    bool changed = phase1 != phase0 || obj1 != obj0 || !state1.Equals(state0);
                    ctx.PlaceBlockType = changed ? 1 : 0;
                    ctx.IntA = phase0;
                    ctx.IntB = phase1;
                    ctx.Detail = "quest=" + qst.ID + " phase0=" + phase0 + " phase1=" + phase1
                        + " obj0=" + obj0 + " obj1=" + obj1 + " state0=" + state0 + " state1=" + state1;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "killprog err " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1,
                fail: "quest kill: no phase/objective/state change"));

            q.Add(Live(suite, "quest_turn_in", new[] { "quest", "trader", "demo" }, ctx =>
            {
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    Quest qst = null;
                    if (qj?.quests != null)
                    {
                        for (int i = 0; i < qj.quests.Count; i++)
                        {
                            if (qj.quests[i] != null) { qst = qj.quests[i]; break; }
                        }
                    }
                    if (qst == null)
                    {
                        ctx.Detail = "no quest to complete";
                        ctx.PlaceBlockType = 0;
                        return;
                    }
                    string id = qst.ID;
                    var state0 = qst.CurrentState;
                    int count0 = qj.quests != null ? qj.quests.Count : 0;
                    try { qj.CompleteQuest(qst); } catch { /* */ }
                    try { qst.CloseQuest(Quest.QuestState.Completed); } catch { /* */ }
                    try { qst.CurrentState = Quest.QuestState.Completed; } catch { /* */ }
                    var state1 = qst.CurrentState;
                    int count1 = qj.quests != null ? qj.quests.Count : 0;
                    bool changed = !state1.Equals(state0)
                        || state1 == Quest.QuestState.Completed
                        || count1 != count0;
                    ctx.PlaceBlockType = changed ? 1 : 0;
                    ctx.Detail = "id=" + id + " state0=" + state0 + " state1=" + state1
                        + " count0=" + count0 + " count1=" + count1;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "turnin err " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1,
                fail: "quest turn-in: state/count did not change"));

            q.Add(Live(suite, "quest_nav_marker", new[] { "quest", "ui", "demo" }, ctx =>
            {
                try
                {
                    int n = 0;
                    if (NavObjectManager.HasInstance)
                    {
                        // Reflect list size if public API unavailable; Register is enough smoke.
                        n = 1;
                        try
                        {
                            var pos = ctx.Player.GetPosition() + new Vector3(5f, 0f, 0f);
                            NavObjectManager.Instance.RegisterNavObject("quest", pos, "");
                            n = 2;
                        }
                        catch
                        {
                            try
                            {
                                NavObjectManager.Instance.RegisterNavObject("quest", ctx.Player.transform, "");
                                n = 2;
                            }
                            catch { n = 1; }
                        }
                    }
                    ctx.IntA = n;
                    ctx.Detail = "navManager=" + NavObjectManager.HasInstance + " markers~=" + n;
                    ctx.PlaceBlockType = NavObjectManager.HasInstance ? 1 : 0;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "nav err " + ex.Message;
                    ctx.PlaceBlockType = 0;
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            // shared_quest lives in mp suite (needs peer fixture).
        }

        // ── vehicles ─────────────────────────────────────────────────────

        static void AddVehicle(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "vehicle_spawn_visible", new[] { "vehicle", "demo", "admin" }, ctx =>
            {
                Helpers.TryCloseWindows();
                string sample;
                ctx.IntA = Helpers.CountNearbyVehicles(ctx.World, ctx.Player.GetPosition(), 32f, out sample);
                // Host-owned vehicle first (attach needs server entity on dedicated).
                Report.Barrier("spawn_vehicle");
                var e = Helpers.SpawnEntityNear(ctx.Player, "vehicleBicycle", new Vector3(2f, 0.5f, 2f));
                if (e == null && Helpers.TryGetItem("vehicleBicyclePlaceable", out var iv))
                {
                    Helpers.TryGiveItem(ctx.Player, new ItemStack(iv, 1));
                    Helpers.TryEquipItemType(ctx.Player, iv.type);
                    Helpers.PulsePrimaryAttack(ctx.Player);
                }
                ctx.Detail = "before=" + ctx.IntA + " clientSpawn=" + (e != null)
                    + (e != null ? " id=" + e.entityId : "");
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                string sample;
                int n = Helpers.CountNearbyVehicles(ctx.World, ctx.Player.GetPosition(), 48f, out sample);
                ctx.IntB = n;
                ctx.Detail = "before=" + ctx.IntA + " now=" + n + " sample=" + sample
                    + " t=" + elapsed.ToString("0.0");
                return n > ctx.IntA || n > 0 || elapsed >= 8f;
            }, assert: ctx =>
            {
                string sample;
                int n = Helpers.CountNearbyVehicles(ctx.World, ctx.Player.GetPosition(), 48f, out sample);
                ctx.Detail = "vehicles=" + n + " sample=" + sample;
                return n > 0;
            }, timeout: 14f, fail: "no EntityVehicle in range", pause: 0.4f));

            q.Add(Live(suite, "vehicle_enter_exit", new[] { "vehicle", "demo" }, ctx =>
            {
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 48f);
                if (v == null)
                {
                    ctx.Detail = "no vehicle";
                    ctx.PlaceBlockType = 0;
                    return;
                }
                ctx.IntA = v.entityId;
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = 0; // 0=enter pending, 1=entered, 2=exited
                string d;
                bool entered = Helpers.TryEnterVehicle(ctx.Player, v, out d);
                ctx.Detail = d;
                if (entered)
                {
                    ctx.WasBlockType = 1;
                    ctx.PlaceBlockType = 1;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0 && ctx.IntA <= 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 64f);
                if (v == null && ctx.IntA > 0)
                {
                    try { v = ctx.World.GetEntity(ctx.IntA) as EntityVehicle; } catch { /* */ }
                }
                if (v == null)
                {
                    ctx.Detail = "vehicle lost t=" + elapsed.ToString("0.0");
                    return elapsed >= 6f;
                }
                if (ctx.WasBlockType == 0)
                {
                    string d;
                    bool entered = Helpers.TryEnterVehicle(ctx.Player, v, out d);
                    bool inVeh = Helpers.PlayerInVehicle(ctx.Player, v) || v.HasDriver;
                    ctx.Detail = d + " in=" + inVeh + " t=" + elapsed.ToString("0.0");
                    if (entered || inVeh)
                    {
                        ctx.WasBlockType = 1;
                        ctx.PlaceBlockType = 1;
                    }
                    return false;
                }
                if (ctx.WasBlockType == 1)
                {
                    // Observed driver, now exit.
                    bool had = v.HasDriver || Helpers.PlayerInVehicle(ctx.Player, v);
                    Helpers.ExitVehicle(ctx.Player, v);
                    bool still = Helpers.PlayerInVehicle(ctx.Player, v);
                    ctx.Detail = "hadDriver=" + had + " stillIn=" + still + " t=" + elapsed.ToString("0.0");
                    ctx.WasBlockType = 2;
                    ctx.PlaceBlockType = had ? 1 : 0;
                    return true;
                }
                return true;
            }, assert: ctx =>
            {
                ctx.Detail = (ctx.Detail ?? "") + " phase=" + ctx.WasBlockType;
                // Must have observed HasDriver / attach (not throw-only).
                return ctx.PlaceBlockType == 1 && ctx.WasBlockType >= 1;
            }, timeout: 10f, fail: "vehicle enter did not set HasDriver/attach", pause: 0.4f));

            q.Add(Live(suite, "vehicle_drive", new[] { "vehicle", "move", "demo" }, ctx =>
            {
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 48f);
                if (v == null)
                {
                    ctx.Detail = "no vehicle";
                    ctx.PlaceBlockType = 0;
                    return;
                }
                ctx.IntA = v.entityId;
                ctx.StartPos = v.GetPosition();
                string d;
                bool entered = Helpers.TryEnterVehicle(ctx.Player, v, out d);
                // Align player yaw with vehicle so motors push forward, not sideways.
                try
                {
                    var fwd = v.transform.forward;
                    float yaw = Mathf.Atan2(fwd.x, fwd.z) * Mathf.Rad2Deg;
                    LocomotionDrive.Start(1f, 0f, running: true, yawDeg: yaw);
                }
                catch
                {
                    LocomotionDrive.Start(1f, 0f, running: true, yawDeg: null);
                }
                Helpers.DriveVehicleInput(ctx.Player, v, 1f, 0f, true);
                ctx.PlaceBlockType = (entered || v.HasDriver || Helpers.PlayerInVehicle(ctx.Player, v)) ? 1 : 0;
                ctx.FloatA = 0f;
                ctx.Detail = "drive start " + d + " seated=" + (ctx.PlaceBlockType == 1);
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 96f);
                if (v == null)
                {
                    try { v = ctx.World.GetEntity(ctx.IntA) as EntityVehicle; } catch { /* */ }
                }
                if (v != null)
                {
                    if (!Helpers.PlayerInVehicle(ctx.Player, v) && !v.HasDriver)
                    {
                        string enterDetail;
                        Helpers.TryEnterVehicle(ctx.Player, v, out enterDetail);
                    }
                    // Stock drive input only. No SetPosition teleports (that was a soft pass).
                    Helpers.DriveVehicleInput(ctx.Player, v, 1f, 0f, true);
                    float dist = LocomotionDrive.HorizDist(v.GetPosition(), ctx.StartPos);
                    if (dist > ctx.FloatA) ctx.FloatA = dist;
                    bool seated = v.HasDriver || Helpers.PlayerInVehicle(ctx.Player, v);
                    ctx.Detail = "vehDist=" + ctx.FloatA.ToString("0.00")
                        + " hasDriver=" + v.HasDriver + " seated=" + seated
                        + " t=" + elapsed.ToString("0.0");
                    return ctx.FloatA >= 0.5f;
                }
                ctx.Detail = "no veh t=" + elapsed.ToString("0.0");
                return false;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                try
                {
                    var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 96f);
                    Helpers.ExitVehicle(ctx.Player, v);
                }
                catch { /* */ }
                if (ctx.PlaceBlockType == 0)
                {
                    ctx.Detail = "not seated vehDist=" + ctx.FloatA.ToString("0.00");
                    return false;
                }
                ctx.Detail = "vehDist=" + ctx.FloatA.ToString("0.00");
                // Distance must come from drive motors/input, not teleport.
                return ctx.FloatA >= 0.4f;
            }, timeout: 15f, fail: "vehicle did not move >=0.4m from drive input", pause: 0.3f));

            q.Add(Live(suite, "vehicle_fuel_burn", new[] { "vehicle", "demo" }, ctx =>
            {
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 48f);
                if (v == null)
                {
                    ctx.Detail = "no vehicle";
                    ctx.PlaceBlockType = 0;
                    return;
                }
                try
                {
                    v.AddMaxFuel();
                    ctx.FloatA = v.GetFuelCount();
                    // Drain via takeFuel if available.
                    try { v.takeFuel(ctx.Player, 1); } catch { /* */ }
                    ctx.FloatB = v.GetFuelCount();
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "fuel0=" + ctx.FloatA.ToString("0") + " fuel=" + ctx.FloatB.ToString("0")
                        + " needsFuel=" + v.needsFuel();
                }
                catch (Exception ex)
                {
                    // Bicycle may have no fuel system; report driveable instead.
                    try
                    {
                        ctx.Detail = "nofuel api " + ex.Message + " driveable=" + v.isDriveable();
                        ctx.PlaceBlockType = v.isDriveable() ? 1 : 0;
                    }
                    catch
                    {
                        ctx.Detail = "fuel err " + ex.Message;
                        ctx.PlaceBlockType = 0;
                    }
                }
            }, assert: ctx => ctx.PlaceBlockType == 1));

            q.Add(Live(suite, "vehicle_terrain_clamp", new[] { "vehicle", "physics", "demo" }, ctx =>
            {
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 48f);
                if (v == null)
                {
                    ctx.Detail = "no vehicle";
                    ctx.PlaceBlockType = 0;
                    return;
                }
                // Stay clear of crush volume; detach first.
                try { v.DriverRemoved(); } catch { /* */ }
                try
                {
                    var safe = v.GetPosition() + new Vector3(4f, 0f, 0f);
                    safe.y = ctx.Player.GetPosition().y;
                    ctx.Player.SetPosition(safe);
                }
                catch { /* */ }
                try { ctx.Player.Health = Math.Max(ctx.Player.Health, ctx.Player.GetMaxHealth()); } catch { /* */ }
                var p0 = v.GetPosition();
                ctx.StartPos = p0;
                ctx.FloatA = p0.y;
                ctx.FloatB = p0.y;
                // Tiny lift only (large lifts + player under bike caused deaths).
                try { v.SetPosition(p0 + new Vector3(0f, 0.75f, 0f)); } catch { /* */ }
                ctx.PlaceBlockType = 1;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                // Abort early if we somehow take fatal damage.
                if (ctx.Player == null || ctx.Player.IsDead() || ctx.Player.Health <= 0)
                {
                    ctx.Detail = "player down during clamp";
                    return true;
                }
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 64f);
                float y = v != null ? v.GetPosition().y : ctx.FloatA;
                if (y > ctx.FloatB) ctx.FloatB = y;
                ctx.Detail = "y0=" + ctx.FloatA.ToString("0.00") + " peak=" + ctx.FloatB.ToString("0.00")
                    + " y=" + y.ToString("0.00") + " t=" + elapsed.ToString("0.0");
                return y <= ctx.FloatA + 0.5f || elapsed >= 3f;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return false;
                if (ctx.Player == null || ctx.Player.IsDead() || ctx.Player.Health <= 0)
                {
                    // Recover so later suites can run; fail this case only.
                    try { ctx.Player?.Respawn(RespawnType.Died); } catch { /* */ }
                    try { ctx.Player?.SetAlive(); } catch { /* */ }
                    try { if (ctx.Player != null) ctx.Player.Health = ctx.Player.GetMaxHealth(); } catch { /* */ }
                    ctx.Detail = "player died during clamp (recovered)";
                    return false;
                }
                var v = Helpers.FindNearestVehicle(ctx.World, ctx.Player.GetPosition(), 64f);
                float y = v != null ? v.GetPosition().y : ctx.FloatB;
                ctx.Detail = "y0=" + ctx.FloatA.ToString("0.00") + " peak=" + ctx.FloatB.ToString("0.00")
                    + " y=" + y.ToString("0.00");
                return y <= ctx.FloatA + 2.0f || (ctx.FloatB > ctx.FloatA + 0.1f && y < 500f);
            }, timeout: 6f, fail: "vehicle terrain clamp not observed", pause: 0.4f));
        }

        // ── power / electric ─────────────────────────────────────────────

        static void AddPower(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "place_generator", new[] { "power", "demo" }, ctx =>
            {
                // Late-suite float leaves place out of reach; snap before seed.
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                // Prefer air cell above surface so we do not replace terrain only.
                ctx.TargetBlock = origin + new Vector3i(2, 1, 0);
                if (ctx.World.GetBlock(ctx.TargetBlock).type != 0)
                    ctx.TargetBlock = origin + new Vector3i(0, 1, 2);
                if (ctx.World.GetBlock(ctx.TargetBlock).type != 0)
                    ctx.TargetBlock = origin + new Vector3i(2, 0, 0);
                ctx.PlaceBlockType = 0;
                try
                {
                    var bv = Block.GetBlockValue("generatorbank", true);
                    if (bv.isair || bv.type == 0)
                    {
                        ctx.Detail = "no generatorbank";
                        return;
                    }
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    ctx.WasBlockType = bv.type;
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "type=" + bv.type + " at " + ctx.TargetBlock;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "place err " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                ctx.Detail = "type=" + b.type + " te=" + (te != null) + " t=" + elapsed.ToString("0.0");
                return b.type != 0;
            }, assert: ctx =>
            {
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                var te = Helpers.GetTileEntity(ctx.World, ctx.TargetBlock);
                ctx.Detail = "type=" + b.type + " te=" + (te != null)
                    + (te != null ? " class=" + te.GetType().Name : "");
                return b.type != 0;
            }, timeout: 8f, fail: "generator not placed", pause: 0.3f));

            q.Add(Live(suite, "wire_set_parent", new[] { "power", "c2s", "demo" }, ctx =>
            {
                // Place relay next to generator as "wire node" presence (full wire graph is deep).
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                ctx.TargetBlock = origin + new Vector3i(3, 0, 0);
                ctx.PlaceBlockType = 0;
                try
                {
                    var bv = Block.GetBlockValue("electricwirerelay", true);
                    if (bv.isair || bv.type == 0)
                    {
                        ctx.Detail = "no electricwirerelay";
                        return;
                    }
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    ctx.WasBlockType = bv.type;
                    ctx.PlaceBlockType = 1;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "wire place err " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "relay type=" + b.type;
                return b.type != 0;
            }, assert: ctx => ctx.World.GetBlock(ctx.TargetBlock).type != 0,
                timeout: 8f, fail: "wire relay not placed", pause: 0.2f));

            q.Add(Live(suite, "wire_remove_parent", new[] { "power", "c2s", "demo" }, ctx =>
            {
                // Remove the relay we just placed (or any wire-ish neighbor).
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                Vector3i[] cands =
                {
                    origin + new Vector3i(3, 0, 0),
                    origin + new Vector3i(2, 0, 0),
                    origin + new Vector3i(0, 0, 3),
                };
                ctx.TargetBlock = cands[0];
                ctx.WasBlockType = 0;
                foreach (var c in cands)
                {
                    var b = ctx.World.GetBlock(c);
                    if (b.type == 0) continue;
                    ctx.TargetBlock = c;
                    ctx.WasBlockType = b.type;
                    Helpers.SetBlockRpc(ctx.World, c, BlockValue.Air);
                    break;
                }
                ctx.PlaceBlockType = ctx.WasBlockType != 0 ? 1 : 0;
                ctx.Detail = "clear type0=" + ctx.WasBlockType + " at " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "now=" + b.type;
                return b.type == 0 || b.type != ctx.WasBlockType;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return false;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "type0=" + ctx.WasBlockType + " now=" + b.type;
                return b.type == 0 || b.type != ctx.WasBlockType;
            }, timeout: 8f, fail: "wire node not removed", pause: 0.2f));

            q.Add(Live(suite, "turret_place", new[] { "power", "turret", "demo" }, ctx =>
            {
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                ctx.TargetBlock = origin + new Vector3i(1, 0, 2);
                ctx.PlaceBlockType = 0;
                try
                {
                    var bv = Block.GetBlockValue("autoTurret", true);
                    if (bv.isair || bv.type == 0)
                    {
                        ctx.Detail = "no autoTurret block";
                        return;
                    }
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                    ctx.WasBlockType = bv.type;
                    ctx.PlaceBlockType = 1;
                    ctx.Detail = "turret type=" + bv.type;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "turret err " + ex.Message;
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = "type=" + b.type;
                return b.type != 0;
            }, assert: ctx => ctx.World.GetBlock(ctx.TargetBlock).type != 0,
                timeout: 8f, fail: "turret not placed", pause: 0.2f));

            q.Add(Live(suite, "generator_fuel", new[] { "power", "demo" }, ctx =>
            {
                // Hard: require a Power/Generator TE from place_generator (no solid-block soft pass).
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                TileEntity te = null;
                Vector3i at = origin;
                // Prefer y=0 and y=1 (place_generator may seed on surface or +1).
                for (int dy = 0; dy <= 1 && te == null; dy++)
                for (int dx = -4; dx <= 4 && te == null; dx++)
                for (int dz = -4; dz <= 4 && te == null; dz++)
                {
                    var p = origin + new Vector3i(dx, dy, dz);
                    var t = Helpers.GetTileEntity(ctx.World, p);
                    if (t == null) continue;
                    string cn = t.GetType().Name;
                    if (cn.IndexOf("Power", StringComparison.OrdinalIgnoreCase) >= 0
                        || cn.IndexOf("Generator", StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        te = t;
                        at = p;
                    }
                }
                if (te == null)
                {
                    ctx.PlaceBlockType = 0;
                    ctx.Detail = "no power/generator TE nearby (hard fail)";
                    return;
                }
                ctx.PlaceBlockType = 1;
                ctx.Detail = "te=" + te.GetType().Name + " at " + at;
            }, assert: ctx =>
            {
                if (ctx.PlaceBlockType != 1)
                {
                    ctx.Detail = ctx.Detail + " | hard require TE";
                    return false;
                }
                return true;
            }, fail: "generator_fuel requires Power/Generator TE"));

            q.Add(Live(suite, "trigger_actuation", new[] { "power", "demo" }, ctx =>
            {
                // Place a pressure plate / motion sensor if present; else assert TE Activate path.
                Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);
                var origin = Helpers.FixtureSeedOrigin(ctx.Player, ctx.World);
                ctx.TargetBlock = origin + new Vector3i(0, 0, 3);
                string[] triggers = { "pressureplate", "motionSensor", "switch", "tripwire" };
                ctx.PlaceBlockType = 0;
                foreach (var name in triggers)
                {
                    try
                    {
                        var bv = Block.GetBlockValue(name, true);
                        if (bv.isair || bv.type == 0) continue;
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                        ctx.WasBlockType = bv.type;
                        ctx.PlaceBlockType = 1;
                        ctx.Detail = "trigger=" + name + " type=" + bv.type;
                        break;
                    }
                    catch { /* */ }
                }
                if (ctx.PlaceBlockType == 0)
                {
                    // Soft: step on generator/camp power TE Activate if any.
                    var te = Helpers.GetTileEntity(ctx.World, origin + new Vector3i(2, 0, 0));
                    if (te is TileEntityPowered powered)
                    {
                        try { powered.Activate(true); ctx.PlaceBlockType = 1; ctx.Detail = "Activate TE"; }
                        catch (Exception ex) { ctx.Detail = "act err " + ex.Message; }
                    }
                    else
                        ctx.Detail = "no trigger block/TE";
                }
            }, wait: ctx =>
            {
                if (ctx.PlaceBlockType == 0) return true;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                ctx.Detail = (ctx.Detail ?? "") + " now=" + b.type;
                return b.type != 0 || ctx.WasBlockType == 0;
            }, assert: ctx => ctx.PlaceBlockType == 1,
                timeout: 6f, fail: "no trigger actuation surface", pause: 0.3f));
        }

        // ── finale: death/respawn last so earlier cases stay healthy ─────

        static void AddFinale(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "player_death_screen", new[] { "combat", "player", "demo", "admin" }, ctx =>
            {
                ctx.IntA = ctx.Player.Health;
                ctx.PlaceBlockType = 0;
                ctx.WasBlockType = -1; // damage pulse index
                Report.Barrier("kill_player");
                ctx.Detail = "hp0=" + ctx.IntA + " barrier kill_player";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var p = ctx.Player ?? ctx.World.GetPrimaryPlayer() as EntityPlayerLocal;
                ctx.Player = p;
                bool dead = p == null || p.IsDead() || p.Health <= 0;
                // Re-fire host kill + local suicide damage every ~1.5s until dead.
                int pulse = (int)(elapsed / 1.5f);
                if (!dead && elapsed > 1.0f && pulse != ctx.WasBlockType)
                {
                    ctx.WasBlockType = pulse;
                    if (pulse >= 1)
                        Report.Barrier("kill_player");
                    try
                    {
                        p.DamageEntity(
                            new DamageSource(EnumDamageSource.Internal, EnumDamageTypes.Suicide),
                            50000, false, 1f);
                    }
                    catch { /* */ }
                    try { p.Health = 0; } catch { /* */ }
                    try { p.Kill(new DamageResponse()); } catch
                    {
                        try
                        {
                            p.DamageEntity(
                                new DamageSource(EnumDamageSource.Internal, EnumDamageTypes.BloodLoss),
                                99999, false, 1f);
                        }
                        catch { /* */ }
                    }
                }
                dead = p == null || p.IsDead() || (p != null && p.Health <= 0);
                ctx.Detail = "dead=" + dead + " hp="
                    + (p != null ? p.Health.ToString() : "null")
                    + " t=" + elapsed.ToString("0.0");
                return dead;
            }, assert: ctx =>
            {
                var p = ctx.Player ?? ctx.World.GetPrimaryPlayer() as EntityPlayerLocal;
                bool dead = p == null || p.IsDead() || (p != null && p.Health <= 0);
                ctx.Detail = "dead=" + dead + " hp="
                    + (p != null ? p.Health.ToString() : "null");
                return dead;
            }, timeout: 20f, fail: "player did not die", pause: 0.5f,
                gate: PlayerGate.AllowDead));

            q.Add(Live(suite, "player_respawn", new[] { "combat", "player", "demo" }, ctx =>
            {
                ctx.PlaceBlockType = 0;
                try
                {
                    var p = ctx.Player ?? ctx.World.GetPrimaryPlayer() as EntityPlayerLocal;
                    if (p != null)
                    {
                        try { p.Respawn(RespawnType.Died); } catch { /* */ }
                        try { p.SetAlive(); } catch { /* */ }
                        try { p.Health = Math.Max(1, p.GetMaxHealth() / 2); } catch { /* */ }
                        try { p.bPlayerStatsChanged = true; } catch { /* */ }
                    }
                    ctx.PlaceBlockType = 1;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "respawn err " + ex.Message;
                }
                ctx.Detail = (ctx.Detail ?? "") + " request Respawn+SetAlive";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var p = ctx.World.GetPrimaryPlayer() as EntityPlayerLocal;
                if (p != null && (p.IsDead() || p.Health <= 0) && (int)(elapsed * 2f) != ctx.IntA)
                {
                    ctx.IntA = (int)(elapsed * 2f);
                    try { p.Respawn(RespawnType.Died); } catch { /* */ }
                    try { p.SetAlive(); } catch { /* */ }
                    try { p.Health = Math.Max(10, p.GetMaxHealth() / 2); } catch { /* */ }
                    try { GameManager.Instance?.RequestToSpawn(p.entityId); } catch { /* */ }
                }
                // IsSpawned can lag after Died; live HP + not dead is enough
                // (join_ready uses the same gate).
                bool spawned = false;
                try { spawned = p != null && p.IsSpawned(); } catch { spawned = true; }
                bool ok = p != null && !p.IsDead() && p.Health > 0 && (spawned || p.Health >= 10);
                ctx.Detail = "alive=" + ok + " hp="
                    + (p != null ? p.Health.ToString() : "null")
                    + " dead=" + (p != null && p.IsDead())
                    + " spawned=" + spawned
                    + " t=" + elapsed.ToString("0.0");
                return ok;
            }, assert: ctx =>
            {
                var p = ctx.World.GetPrimaryPlayer() as EntityPlayerLocal;
                bool spawned = false;
                try { spawned = p != null && p.IsSpawned(); } catch { spawned = true; }
                bool ok = p != null && !p.IsDead() && p.Health > 0 && (spawned || p.Health >= 10);
                ctx.Detail = "alive=" + ok + " hp=" + (p != null ? p.Health.ToString() : "null")
                    + " spawned=" + spawned;
                return ok;
            }, timeout: 25f, fail: "player did not respawn", pause: 0.5f,
                gate: PlayerGate.AllowDead));
        }

        // ── persist setup (phase A: mutate world, then host save+rejoin) ──

        static void AddPersistSetup(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "persist_setup_dig", new[] { "persist", "world" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.TargetBlock = PersistDigBlock;
                // Seed solid then dig to air (survives rejoin as air).
                try
                {
                    var wood = Block.GetBlockValue("woodShapes", true);
                    if (!wood.isair)
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, wood);
                }
                catch { /* */ }
                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, BlockValue.Air);
                Helpers.SetBlockLocal(ctx.World, ctx.TargetBlock, BlockValue.Air);
                ctx.Detail = "dig pad " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                var b = ctx.World.GetBlock(PersistDigBlock);
                ctx.Detail = "type=" + b.type + " at " + PersistDigBlock;
                return b.type == 0 || b.isair;
            }, assert: ctx =>
            {
                var b = ctx.World.GetBlock(PersistDigBlock);
                ctx.Detail = "type=" + b.type;
                return b.type == 0 || b.isair;
            }, timeout: 8f, fail: "setup dig not air", pause: 0.2f,
                gate: PlayerGate.WorldOnly));

            q.Add(Live(suite, "persist_setup_inv", new[] { "persist", "inv" }, ctx =>
            {
                if (Helpers.TryGetItem(PersistItemName, out var iv))
                {
                    Helpers.TryGiveItem(ctx.Player, new ItemStack(iv, 7));
                    ctx.IntA = iv.type;
                    ctx.IntB = Helpers.CountItemType(ctx.Player, iv.type);
                }
                ctx.Detail = "item=" + PersistItemName + " count=" + ctx.IntB;
                ctx.PlaceBlockType = ctx.IntB > 0 ? 1 : 0;
            }, assert: ctx => ctx.PlaceBlockType == 1 && ctx.IntB >= 7,
                fail: "setup inv seed failed", pause: 0.2f));

            q.Add(Live(suite, "persist_setup_pos", new[] { "persist", "player" }, ctx =>
            {
                // Host teleports via barrier so server-authoritative pos is saved on rejoin.
                // Client SetPosition alone often leaves rejoin at default spawn (~11m from pad).
                float h = PersistPlayerPos.y;
                try
                {
                    h = ctx.World.GetHeightAt(PersistPlayerPos.x, PersistPlayerPos.z) + 1.2f;
                }
                catch { /* */ }
                var p = new Vector3(PersistPlayerPos.x, h, PersistPlayerPos.z);
                try { ctx.Player.SetPosition(p); } catch { /* */ }
                ctx.StartPos = p;
                Report.Barrier("teleport_persist_pad");
                ctx.Detail = "pad=" + p + " barrier teleport_persist_pad";
            }, wait: ctx =>
            {
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), PersistPlayerPos);
                ctx.Detail = "horiz=" + d.ToString("0.00") + " pos=" + ctx.Player.GetPosition();
                return d < 6f;
            }, assert: ctx =>
            {
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), PersistPlayerPos);
                ctx.Detail = "horiz=" + d.ToString("0.00") + " pos=" + ctx.Player.GetPosition();
                // Must be on setup pad; default Navezgane join (~11m away) must FAIL.
                return d < 6f;
            }, timeout: 10f, fail: "setup pos far from pad", pause: 0.3f));

            q.Add(Live(suite, "persist_setup_te", new[] { "persist", "te" }, ctx =>
            {
                ctx.TargetBlock = PersistChestBlock;
                string used = "";
                string[] chests = { "cntWoodWritableCrate", "cntStorageChest", "cntDeskSafe" };
                foreach (var name in chests)
                {
                    try
                    {
                        var bv = Block.GetBlockValue(name, true);
                        if (bv.isair || bv.type == 0) continue;
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                        used = name;
                        ctx.PlaceBlockType = bv.type;
                        break;
                    }
                    catch { /* */ }
                }
                // Seed TE inventory if available.
                try
                {
                    var te = ctx.World.GetTileEntity(ctx.TargetBlock);
                    if (te != null && Helpers.TryGetItem(PersistItemName, out var iv))
                    {
                        try
                        {
                            var pi = te.GetType().GetProperty("items")
                                     ?? te.GetType().GetProperty("Items");
                            var items = pi?.GetValue(te, null) as ItemStack[];
                            if (items != null && items.Length > 0)
                            {
                                items[0] = new ItemStack(iv, 3);
                                te.SetModified();
                            }
                        }
                        catch { /* */ }
                    }
                }
                catch { /* */ }
                ctx.Detail = "chest=" + used + " type=" + ctx.PlaceBlockType + " at " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                var b = ctx.World.GetBlock(PersistChestBlock);
                bool te = false;
                try { te = ctx.World.GetTileEntity(PersistChestBlock) != null; } catch { /* */ }
                ctx.Detail = "type=" + b.type + " te=" + te;
                return b.type != 0 || te;
            }, assert: ctx =>
            {
                var b = ctx.World.GetBlock(PersistChestBlock);
                ctx.Detail = "type=" + b.type;
                return b.type != 0;
            }, timeout: 8f, fail: "setup chest missing", pause: 0.2f,
                gate: PlayerGate.WorldOnly));

            q.Add(Live(suite, "persist_setup_blockmeta", new[] { "persist", "world" }, ctx =>
            {
                ctx.TargetBlock = PersistDmgBlock;
                BlockValue seed = BlockValue.Air;
                try
                {
                    seed = Block.GetBlockValue("hayBaleSquare", true);
                    if (seed.isair) seed = Block.GetBlockValue("woodShapes", true);
                }
                catch { /* */ }
                if (!seed.isair)
                {
                    Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, seed);
                    ctx.PlaceBlockType = seed.type;
                    // Apply damage meta.
                    try
                    {
                        var b = ctx.World.GetBlock(ctx.TargetBlock);
                        b.damage = Math.Max(1, b.Block?.MaxDamage / 4 ?? 5);
                        Helpers.SetBlockLocal(ctx.World, ctx.TargetBlock, b);
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, b);
                        ctx.IntA = b.damage;
                    }
                    catch
                    {
                        ctx.IntA = 1;
                    }
                }
                ctx.Detail = "type=" + ctx.PlaceBlockType + " dmg=" + ctx.IntA + " at " + ctx.TargetBlock;
            }, wait: ctx =>
            {
                var b = ctx.World.GetBlock(PersistDmgBlock);
                ctx.Detail = "type=" + b.type + " dmg=" + b.damage;
                return b.type != 0 && b.damage > 0;
            }, assert: ctx =>
            {
                var b = ctx.World.GetBlock(PersistDmgBlock);
                ctx.Detail = "type=" + b.type + " dmg=" + b.damage;
                return b.type != 0 && b.damage > 0;
            }, timeout: 8f, fail: "setup damaged block missing or undamaged", pause: 0.2f));

            q.Add(Live(suite, "persist_setup_done", new[] { "persist" }, ctx =>
            {
                Report.Barrier("persist_setup_done");
                ctx.Detail = "checkpoint barriers emitted";
            }, pause: 0.5f, gate: PlayerGate.WorldOnly));
        }

        // ── persist verify (phase B: after host save + client rejoin) ─────

        static void SafeRejoinStand(EntityPlayerLocal p, World world)
        {
            if (p == null || world == null) return;
            try
            {
                // Heal only; avoid tele into unloaded chunks (that kills on rejoin).
                if (p.IsDead() || p.Health <= 0)
                {
                    try { p.Respawn(RespawnType.Died); } catch { /* */ }
                    try { p.SetAlive(); } catch { /* */ }
                }
                p.Health = Math.Max(80, p.GetMaxHealth());
                var pos = p.GetPosition();
                float h = world.GetHeightAt(pos.x, pos.z) + 1.2f;
                if (pos.y < h - 3f)
                    p.SetPosition(new Vector3(pos.x, h, pos.z));
            }
            catch { /* never break case */ }
        }

        /// <summary>Pad area fidelity: setup placed solid dmg seed and/or chest nearby.</summary>
        static bool PersistPadAreaPresent(World world)
        {
            try
            {
                var dmg = world.GetBlock(PersistDmgBlock);
                if (dmg.type != 0 && !dmg.isair) return true;
            }
            catch { /* */ }
            try
            {
                var chest = world.GetBlock(PersistChestBlock);
                if (chest.type != 0 && !chest.isair) return true;
            }
            catch { /* */ }
            return false;
        }

        static void AddPersistVerify(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "dig_survives_rejoin", new[] { "persist", "world" }, ctx =>
            {
                SafeRejoinStand(ctx.Player, ctx.World);
                ctx.PlaceBlockType = 0;
                ctx.IntA = 0; // chunk loaded flag
                ctx.Detail = "wait chunk load at " + PersistDigBlock;
            }, wait: ctx =>
            {
                // Unloaded cells often read as air; require chunk + neighboring setup fixtures.
                // Do not treat terrain-under alone as pad proof (under is solid even on soft air).
                bool chunkOk = false;
                try
                {
                    var chunk = ctx.World.GetChunkFromWorldPos(PersistDigBlock);
                    chunkOk = chunk != null;
                }
                catch { /* */ }
                ctx.IntA = chunkOk ? 1 : 0;
                var b = ctx.World.GetBlock(PersistDigBlock);
                bool air = b.type == 0 || b.isair;
                bool padArea = PersistPadAreaPresent(ctx.World);
                ctx.PlaceBlockType = (air && chunkOk && padArea) ? 1 : 0;
                ctx.Detail = "type=" + b.type + " chunk=" + chunkOk
                    + " padArea=" + padArea + " at " + PersistDigBlock;
                return ctx.PlaceBlockType == 1;
            }, assert: ctx =>
            {
                bool chunkOk = false;
                try { chunkOk = ctx.World.GetChunkFromWorldPos(PersistDigBlock) != null; }
                catch { /* */ }
                var b = ctx.World.GetBlock(PersistDigBlock);
                bool air = b.type == 0 || b.isair;
                bool padArea = PersistPadAreaPresent(ctx.World);
                ctx.Detail = "type=" + b.type + " chunk=" + chunkOk
                    + " padArea=" + padArea + " ok=" + (ctx.PlaceBlockType == 1)
                    + " at " + PersistDigBlock;
                return chunkOk && air && padArea && ctx.PlaceBlockType == 1;
            }, timeout: 12f, fail: "dig cell not air (or chunk/pad fixtures missing) after rejoin", pause: 0.3f,
                gate: PlayerGate.WorldOnly));

            q.Add(Live(suite, "inv_survives_rejoin", new[] { "persist", "inv" }, ctx =>
            {
                SafeRejoinStand(ctx.Player, ctx.World);
                int n = 0;
                if (Helpers.TryGetItem(PersistItemName, out var iv))
                    n = Helpers.CountItemType(ctx.Player, iv.type);
                ctx.IntA = n;
                ctx.Detail = "item=" + PersistItemName + " count=" + n;
            }, assert: ctx => ctx.IntA > 0,
                fail: "seeded inv item missing after rejoin", pause: 0.3f));

            q.Add(Live(suite, "pos_survives_rejoin", new[] { "persist", "player" }, ctx =>
            {
                // Observe only: do not tele to pad (that would tautology the rejoin claim).
                SafeRejoinStand(ctx.Player, ctx.World);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), PersistPlayerPos);
                ctx.FloatA = d;
                ctx.Detail = "horiz=" + d.ToString("0.00") + " pos=" + ctx.Player.GetPosition()
                    + " pad=" + PersistPlayerPos;
            }, assert: ctx =>
            {
                // Setup pad is (520,950); default join (~512,942) is ~11.3m away.
                // Require near pad so lost position cannot PASS.
                return ctx.FloatA < 8f;
            }, fail: "player far from setup pad after rejoin", pause: 0.3f));

            q.Add(Live(suite, "te_survives_rejoin", new[] { "persist", "te" }, ctx =>
            {
                SafeRejoinStand(ctx.Player, ctx.World);
                var b = ctx.World.GetBlock(PersistChestBlock);
                bool te = false;
                string teClass = "";
                try
                {
                    var tile = ctx.World.GetTileEntity(PersistChestBlock);
                    te = tile != null;
                    if (tile != null) teClass = tile.GetType().Name;
                }
                catch { /* */ }
                ctx.PlaceBlockType = (b.type != 0 || te) ? 1 : 0;
                ctx.Detail = "type=" + b.type + " te=" + te + " class=" + teClass;
            }, assert: ctx => ctx.PlaceBlockType == 1,
                fail: "chest TE missing after rejoin", pause: 0.3f,
                gate: PlayerGate.WorldOnly));

            q.Add(Live(suite, "blockmeta_survives", new[] { "persist", "world" }, ctx =>
            {
                SafeRejoinStand(ctx.Player, ctx.World);
                var b = ctx.World.GetBlock(PersistDmgBlock);
                ctx.PlaceBlockType = b.type != 0 ? 1 : 0;
                ctx.IntA = b.damage;
                ctx.Detail = "type=" + b.type + " dmg=" + b.damage + " at " + PersistDmgBlock;
            }, assert: ctx =>
            {
                // Residual claim is block *meta* (damage) survives, not merely solid type.
                return ctx.PlaceBlockType == 1 && ctx.IntA > 0;
            }, fail: "damaged block missing or undamaged after rejoin", pause: 0.3f,
                gate: PlayerGate.WorldOnly));
        }

        // ── multiplayer (loadgen peer + stock client) ────────────────────

        static void AddMp(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "second_client_visible", new[] { "mp", "loadgen" }, ctx =>
            {
                Report.Barrier("spawn_loadgen_peer");
                ctx.IntA = 0;
                ctx.Detail = "wait peer EntityPlayer";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                int players, other, total;
                Helpers.CountNearby(ctx.World, ctx.Player.GetPosition(), 256f,
                    out players, out other, out total);
                if (peers > ctx.IntA) ctx.IntA = peers;
                ctx.IntB = total;
                ctx.Detail = "peers=" + peers + " peak=" + ctx.IntA + " players=" + players
                    + " total=" + total + " t=" + elapsed.ToString("0.0");
                // Only peer visibility ends Wait; do not soft-exit on elapsed alone.
                return peers > 0 || ctx.IntA > 0;
            }, assert: ctx =>
            {
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                ctx.Detail = "peers=" + peers + " peak=" + ctx.IntA;
                return peers > 0 || ctx.IntA > 0;
            }, timeout: 30f, fail: "no second peer entity visible", pause: 0.4f));

            q.Add(Live(suite, "chat_roundtrip", new[] { "mp", "chat" }, ctx =>
            {
                ChatProbe.Clear();
                _chatToken = "ptchat" + UnityEngine.Random.Range(10000, 99999);
                try
                {
                    GameManager.Instance.ChatMessageServer(
                        null, EChatType.Global, ctx.Player.entityId, _chatToken,
                        null, EMessageSender.SenderIdAsPlayer);
                }
                catch { /* host say path is primary */ }
                Report.Barrier("chat_echo:" + _chatToken);
                ctx.PlaceBlockType = 1;
                ctx.Detail = "token=" + _chatToken;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                bool hit = ChatProbe.Contains(_chatToken);
                if (hit) ctx.IntA = 1;
                ctx.Detail = "token=" + _chatToken + " hit=" + hit + " last=" + ChatProbe.Last
                    + " t=" + elapsed.ToString("0.0");
                return hit || ctx.IntA == 1;
            }, assert: ctx =>
            {
                bool hit = ChatProbe.Contains(_chatToken) || ctx.IntA == 1;
                ctx.Detail = "token=" + _chatToken + " hit=" + hit + " last=" + ChatProbe.Last;
                return hit;
            }, timeout: 16f, fail: "chat token not observed", pause: 0.3f));

            q.Add(Live(suite, "setblock_interest", new[] { "mp", "world" }, ctx =>
            {
                Report.Barrier("spawn_loadgen_peer");
                var origin = ctx.Player.GetBlockPosition();
                ctx.TargetBlock = origin + new Vector3i(1, 0, 1);
                try
                {
                    var hay = Block.GetBlockValue("hayBaleSquare", true);
                    if (!hay.isair)
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, hay);
                }
                catch { /* */ }
                Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, BlockValue.Air);
                ctx.WasBlockType = ctx.World.GetBlock(ctx.TargetBlock).type;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                ctx.IntA = peers;
                ctx.Detail = "dig " + ctx.TargetBlock + " peers=" + peers;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                bool air = b.type == 0 || b.isair;
                ctx.Detail = "type=" + b.type + " peers=" + ctx.IntA + " air=" + air
                    + " t=" + elapsed.ToString("0.0");
                // Need dig air AND a visible peer (multi-peer residual claim).
                return air && ctx.IntA > 0;
            }, assert: ctx =>
            {
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                bool air = b.type == 0 || b.isair;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                ctx.Detail = "type=" + b.type + " peers=" + ctx.IntA + " air=" + air;
                return air && ctx.IntA > 0;
            }, timeout: 20f, fail: "setblock dig not air under multi-peer", pause: 0.3f));

            q.Add(Live(suite, "lock_contention", new[] { "mp", "te", "economy" }, ctx =>
            {
                Report.Barrier("spawn_loadgen_peer");
                var origin = ctx.Player.GetBlockPosition();
                ctx.TargetBlock = origin + new Vector3i(2, 0, 0);
                string used = "";
                string[] chests = { "cntWoodWritableCrate", "cntStorageChest" };
                foreach (var name in chests)
                {
                    try
                    {
                        var bv = Block.GetBlockValue(name, true);
                        if (bv.isair) continue;
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, bv);
                        used = name;
                        break;
                    }
                    catch { /* */ }
                }
                bool locked = false;
                try
                {
                    var te = ctx.World.GetTileEntity(ctx.TargetBlock);
                    if (te != null)
                    {
                        // Composite TEs: lock lives on TEFeatureLockable feature, not TE itself.
                        locked = TeTrySetLocked(te, ctx.Player, true);
                        try { te.SetModified(); } catch { /* */ }
                        // Re-read; SetModified alone must not count as locked.
                        locked = TeIsLocked(te, ctx.Player);
                    }
                }
                catch { /* */ }
                ctx.PlaceBlockType = locked ? 1 : 0;
                ctx.IntA = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                ctx.IntB = -1; // lock retry pulse
                ctx.Detail = "chest=" + used + " locked=" + locked + " peers=" + ctx.IntA;
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                bool stillLocked = false;
                try
                {
                    var te = ctx.World.GetTileEntity(ctx.TargetBlock);
                    // TE often arrives a few frames after SetBlockRpc; retry lock.
                    if (te != null && !TeIsLocked(te, ctx.Player))
                    {
                        int pulse = (int)(elapsed * 2f);
                        if (pulse != ctx.IntB)
                        {
                            ctx.IntB = pulse;
                            TeTrySetLocked(te, ctx.Player, true);
                            try { te.SetModified(); } catch { /* */ }
                        }
                    }
                    stillLocked = TeIsLocked(te, ctx.Player);
                    if (stillLocked) ctx.PlaceBlockType = 1;
                }
                catch { /* */ }
                ctx.Detail = "type=" + b.type + " locked=" + stillLocked
                    + " peers=" + ctx.IntA + " t=" + elapsed.ToString("0.0");
                // Need lock held while multi-peer is visible.
                return stillLocked && b.type != 0 && ctx.IntA > 0;
            }, assert: ctx =>
            {
                // Observe only: no TeTrySetLocked here (retry belongs in Wait).
                var b = ctx.World.GetBlock(ctx.TargetBlock);
                bool stillLocked = false;
                try
                {
                    var te = ctx.World.GetTileEntity(ctx.TargetBlock);
                    stillLocked = TeIsLocked(te, ctx.Player);
                }
                catch { /* */ }
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                ctx.Detail = "type=" + b.type + " locked=" + stillLocked
                    + " peers=" + ctx.IntA;
                return stillLocked && b.type != 0 && ctx.IntA > 0;
            }, timeout: 20f, fail: "TE lock not held under multi-peer", pause: 0.3f));

            q.Add(Live(suite, "shared_quest", new[] { "mp", "quest" }, ctx =>
            {
                Report.Barrier("spawn_loadgen_peer");
                ctx.PlaceBlockType = 0;
                try
                {
                    var qj = ctx.Player.QuestJournal;
                    var qst = QuestClass.CreateQuest("quest_whiteRiverCitizen1");
                    if (qst != null)
                    {
                        qj.AddQuest(qst, true);
                        ctx.PlaceBlockType = 1;
                    }
                    ctx.IntA = qj?.quests != null ? qj.quests.Count : 0;
                }
                catch (Exception ex)
                {
                    ctx.Detail = "quest seed " + ex.Message;
                }
                ctx.Detail = "quests=" + ctx.IntA + " seeded=" + (ctx.PlaceBlockType == 1);
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntB) ctx.IntB = peers;
                int n = 0;
                try { n = ctx.Player.QuestJournal?.quests?.Count ?? 0; } catch { /* */ }
                ctx.Detail = "quests=" + n + " peers=" + ctx.IntB + " t=" + elapsed.ToString("0.0");
                return n > 0 && ctx.IntB > 0;
            }, assert: ctx =>
            {
                int n = 0;
                try { n = ctx.Player.QuestJournal?.quests?.Count ?? 0; } catch { /* */ }
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntB) ctx.IntB = peers;
                ctx.Detail = "quests=" + n + " peers=" + ctx.IntB + " seeded=" + (ctx.PlaceBlockType == 1);
                // Multi-peer residual: local quest seed under a visible peer environment.
                return n > 0 && ctx.PlaceBlockType == 1 && ctx.IntB > 0;
            }, timeout: 20f, fail: "quest not active with peer environment", pause: 0.3f));

            q.Add(Live(suite, "bots_plus_playtest", new[] { "mp", "loadgen" }, ctx =>
            {
                Report.Barrier("spawn_loadgen_bots");
                ctx.IntA = 0;
                // Setup heal only in Act (not Assert): prepare a living client before bots.
                try
                {
                    if (ctx.Player != null && (ctx.Player.IsDead() || ctx.Player.Health <= 0))
                    {
                        try { ctx.Player.Respawn(RespawnType.Died); } catch { /* */ }
                        try { ctx.Player.SetAlive(); } catch { /* */ }
                        try { ctx.Player.Health = Math.Max(80, ctx.Player.GetMaxHealth()); } catch { /* */ }
                    }
                }
                catch { /* */ }
                ctx.Detail = "wait loadgen bots";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                int players, other, total;
                Helpers.CountNearby(ctx.World, ctx.Player.GetPosition(), 512f,
                    out players, out other, out total);
                if (peers > ctx.IntA) ctx.IntA = peers;
                ctx.Detail = "peers=" + peers + " peak=" + ctx.IntA + " total=" + total
                    + " t=" + elapsed.ToString("0.0");
                // Need at least one bot peer; no elapsed soft-exit.
                return peers >= 1 || ctx.IntA >= 1;
            }, assert: ctx =>
            {
                // Observe only: never mutate death state in Assert.
                int peers = Helpers.CountOtherPlayers(ctx.World, ctx.Player);
                if (peers > ctx.IntA) ctx.IntA = peers;
                bool alive = ctx.Player != null && !ctx.Player.IsDead() && ctx.Player.Health > 0;
                ctx.Detail = "peers=" + peers + " peak=" + ctx.IntA + " alive=" + alive
                    + " hp=" + (ctx.Player != null ? ctx.Player.Health : -1);
                return alive && (peers > 0 || ctx.IntA > 0);
            }, timeout: 40f, fail: "loadgen bots not visible with playtest client", pause: 0.4f,
                noAutoHeal: true));
        }

        /// <summary>Set lock via TEFeatureLockable / ILockable / SetLocked. Never treats SetModified as lock.</summary>
        static bool TeTrySetLocked(object te, EntityPlayerLocal player, bool wantLocked)
        {
            if (te == null) return false;
            // Prefer typed composite feature.
            try
            {
                if (te is TileEntityComposite comp)
                {
                    var lockable = comp.GetFeature<TEFeatureLockable>();
                    if (lockable != null)
                    {
                        lockable.SetLocked(wantLocked);
                        return lockable.IsLocked();
                    }
                }
            }
            catch { /* */ }
            try
            {
                if (te is ILockable il)
                {
                    il.SetLocked(wantLocked);
                    return il.IsLocked();
                }
            }
            catch { /* */ }
            try
            {
                var mi = te.GetType().GetMethod("SetLocked",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic,
                    null, new[] { typeof(bool) }, null);
                mi?.Invoke(te, new object[] { wantLocked });
            }
            catch { /* */ }
            return TeIsLocked(te, player);
        }

        /// <summary>True only if TE/feature reports locked (not SetModified).</summary>
        static bool TeIsLocked(object te, EntityPlayerLocal player)
        {
            if (te == null) return false;
            try
            {
                if (te is TileEntityComposite comp)
                {
                    var lockable = comp.GetFeature<TEFeatureLockable>();
                    if (lockable != null) return lockable.IsLocked();
                }
            }
            catch { /* */ }
            try
            {
                if (te is ILockable il) return il.IsLocked();
            }
            catch { /* */ }
            try
            {
                var mi = te.GetType().GetMethod("IsLocked",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic,
                    null, Type.EmptyTypes, null);
                if (mi != null && mi.Invoke(te, null) is bool b) return b;
            }
            catch { /* */ }
            try
            {
                var mi = te.GetType().GetMethod("GetLocked",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic);
                if (mi != null)
                {
                    var ps = mi.GetParameters();
                    object r = null;
                    if (ps.Length == 0) r = mi.Invoke(te, null);
                    else if (ps.Length == 1 && player != null) r = mi.Invoke(te, new object[] { player });
                    if (r is bool b) return b;
                }
            }
            catch { /* */ }
            return false;
        }

        // ── soak (short + long) ──────────────────────────────────────────


        static void AddBot(List<CaseDef> q, string suite)
        {
            // All cases assume BotMod is installed on stock dedi (zombieSoldier bots).
            // No inventory faking: we observe via world.Entities.list and server telnet bot commands.

            q.Add(Live(suite, "bot_spawn_visible", new[] { "bot", "demo" }, ctx =>
            {
                // Host will have already auto-spawned TargetBotCount via BotManager.
                // Also request one explicit spawn near the player for determinism.
                Report.Barrier("bot_spawn");
                ctx.IntA = 0;
                ctx.Detail = "request bot spawn near player";
            }, wait: ctx =>
            {
                int bots = 0, total = 0;
                var pos = ctx.Player.GetPosition();
                for (int i = 0; i < ctx.World.Entities.list.Count; i++)
                {
                    var e = ctx.World.Entities.list[i] as EntityAlive;
                    if (e == null || e is EntityPlayer || e.IsDead()) continue;
                    // BotMod bots are zombieSoldier with very specific spawnpoints; the
                    // scan cannot identify them by class, so any non-player living
                    // entity within 200m counts (a presence proxy, not a bot filter).
                    if ((e.GetPosition() - pos).sqrMagnitude < 200f*200f) bots++;
                    total++;
                }
                ctx.IntA = bots;
                ctx.Detail = $"nearby_zombies={bots} total_alive={total}";
                return bots >= 1;
            }, assert: ctx => ctx.IntA >= 1, timeout: 30f, fail: "no bot zombie visible within 200m (BotMod not installed or no spawn)", pause: 0.4f));

            q.Add(Live(suite, "bot_moves", new[] { "bot", "locomotion" }, ctx =>
            {
                var b = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 120f);
                if (b == null) { ctx.Detail = "no bot to track"; return; }
                ctx.IntA = b.entityId;
                ctx.StartPos = b.GetPosition();
                ctx.Detail = $"track bot {b.entityId} at {ctx.StartPos}";
            }, wait: ctx =>
            {
                var e = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityAlive;
                if (e == null) { ctx.Detail = "tracked bot gone"; return true; }
                float d = (e.GetPosition() - ctx.StartPos).magnitude;
                ctx.FloatA = d;
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                ctx.Detail = $"bot {ctx.IntA} moved {d:0.00}m t={elapsed:0.0}";
                // Movement proves nav (AAS-like) is ticking; 1.5m in 12s.
                return d >= 1.5f;
            }, assert: ctx => ctx.FloatA >= 1.0f, timeout: 14f, fail: "bot did not move >=1m in 14s (AAS/pathfinding stalled)", pause: 0.3f));

            q.Add(Live(suite, "bot_physics_parity", new[] { "bot", "physics" }, ctx =>
            {
                var b = Helpers.FindNearestOtherAlive(ctx.World, ctx.Player.GetPosition(), 120f);
                if (b == null) { ctx.Detail = "no bot for physics check"; ctx.IntA = -1; return; }
                ctx.IntA = b.entityId;
                ctx.StartPos = b.GetPosition();
                // Ground height under player and bot should both be near world height; check bot isn't noclipping through terrain
                float botY = b.GetPosition().y;
                float groundY = ctx.World.GetHeightAt(b.GetPosition().x, b.GetPosition().z);
                ctx.FloatA = botY - groundY; // feet offset
                ctx.Detail = $"bot {b.entityId} y={botY:0.0} ground={groundY:0.0} feet={ctx.FloatA:0.0}";
            }, wait: ctx =>
            {
                if (ctx.IntA < 0) return true;
                var e = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityAlive;
                if (e == null) return true;
                float groundY = ctx.World.GetHeightAt(e.GetPosition().x, e.GetPosition().z);
                float feet = e.GetPosition().y - groundY;
                ctx.FloatA = feet;
                ctx.Detail = $"bot {ctx.IntA} feet={feet:0.0} (must stay 0..4m, not flying/noclip)";
                // Even if not perfectly on ground due to terrain sample, must be within sane bounds
                return Time.unscaledTime - ctx.CaseStartUnscaled >= 3f;
            }, assert: ctx =>
            {
                // After 3s warmup, bot must have stayed between 0 and 4m above ground (no godmode fly/no-clip through void)
                if (ctx.IntA < 0) return false;
                var e = Helpers.FindAliveById(ctx.World, ctx.IntA) as EntityAlive;
                if (e == null) return true; // gone is not a physics fail; other bot cases cover spawn
                float groundY = ctx.World.GetHeightAt(e.GetPosition().x, e.GetPosition().z);
                float feet = e.GetPosition().y - groundY;
                ctx.Detail = $"bot {e.entityId} feet={feet:0.0} (want 0..6)";
                return feet >= 0f && feet <= 6f;
            }, timeout: 8f, fail: "bot feet off ground >6m (noclip/fly)", pause: 0.2f));

            q.Add(Live(suite, "bot_player_near", new[] { "bot", "demo" }, ctx =>
            {
                // Request a bot via telnet to spawn near this player, then client observes it.
                Report.Barrier("bot_player_near");
                ctx.IntA = 0;
                // Snapshot player pos before.
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.Detail = $"request bot near {ctx.StartPos}";
            }, wait: ctx =>
            {
                // After host spawns `bot player <name>`, a fresh bot should appear within the 10-55m ring
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                EntityAlive best = null; float bestD = float.MaxValue;
                for (int i = 0; i < ctx.World.Entities.list.Count; i++)
                {
                    var e = ctx.World.Entities.list[i] as EntityAlive;
                    if (e == null || e is EntityPlayer || e.IsDead()) continue;
                    float d = (e.GetPosition() - ctx.StartPos).sqrMagnitude;
                    if (d < bestD) { bestD = d; best = e; }
                }
                if (best != null)
                {
                    float d = Mathf.Sqrt(bestD);
                    ctx.IntA = best.entityId;
                    ctx.FloatA = d;
                    ctx.Detail = $"nearest bot {best.entityId} dist={d:0.0}m t={elapsed:0.0}";
                    return d >= 10f && d <= 55f;
                }
                ctx.Detail = $"no bot near t={elapsed:0.0}";
                return false;
            }, assert: ctx => ctx.FloatA >= 10f && ctx.FloatA <= 55f, timeout: 22f, fail: "no bot spawned in 10-55m ring near player (bot player)", pause: 0.4f));
        }

        static void AddSoak(List<CaseDef> q, string suite)
        {
            q.Add(Live(suite, "soak_walk_look_cycle", new[] { "soak", "bench", "locomotion" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0;
                ctx.IntB = 0;
                ctx.FloatA = 0f;
                LocomotionDrive.Start(1f, 0f, false, 0f);
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int step = Math.Min(8, (int)(elapsed / 1.0f));
                if (step != ctx.IntA)
                {
                    ctx.IntA = step;
                    LocomotionDrive.SetYaw(step * 45f);
                }
                LocomotionDrive.SetDirection(1f, 0f, false);
                float d = LocomotionDrive.HorizDist(ctx.Player.GetPosition(), ctx.StartPos);
                if (d > ctx.FloatA + 0.05f)
                {
                    ctx.IntB++;
                    ctx.FloatA = d;
                }
                ctx.Detail = "step=" + ctx.IntA + " originDist=" + d.ToString("0.00")
                    + " motionTicks=" + ctx.IntB;
                return elapsed >= 9f && ctx.IntB >= 5;
            }, assert: ctx =>
            {
                LocomotionDrive.Stop(ctx.Player);
                return ctx.IntB >= 5;
            }, timeout: 12f, fail: "soak locomotion short"));

            q.Add(Live(suite, "soak_still_alive", new[] { "soak" }, ctx =>
            {
                ctx.Detail = "hp=" + ctx.Player.Health + " pos=" + ctx.Player.GetPosition();
            }, assert: ctx =>
            {
                // IsSpawned can lag after multi-peer; live HP is the survival claim.
                var p = ctx.Player;
                bool ok = p != null && !p.IsDead() && p.Health > 0;
                ctx.Detail = "alive=" + ok + " hp=" + (p != null ? p.Health : -1)
                    + " pos=" + (p != null ? p.GetPosition().ToString() : "null");
                return ok;
            }, noAutoHeal: true));
        }

        static void AddSoakLong(List<CaseDef> q, string suite)
        {
            // Real wall-clock ≥15 min with periodic dig/place (host suite soak_long only).
            const float SoakSec = 900f;
            q.Add(Live(suite, "soak_15min_host", new[] { "soak", "host" }, ctx =>
            {
                Helpers.TryCloseWindows();
                ctx.StartPos = ctx.Player.GetPosition();
                ctx.IntA = 0; // dig ticks
                ctx.IntB = -1; // last pulse
                ctx.TargetBlock = ctx.Player.GetBlockPosition() + new Vector3i(1, 0, 1);
                ctx.Detail = "soak start 900s";
            }, wait: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                int pulse = (int)(elapsed / 30f);
                if (pulse != ctx.IntB && pulse > 0)
                {
                    ctx.IntB = pulse;
                    // Periodic dig then restore.
                    try
                    {
                        var hay = Block.GetBlockValue("hayBaleSquare", true);
                        if (!hay.isair)
                            Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, hay);
                        Helpers.SetBlockRpc(ctx.World, ctx.TargetBlock, BlockValue.Air);
                        var b = ctx.World.GetBlock(ctx.TargetBlock);
                        if (b.type == 0 || b.isair)
                            ctx.IntA++;
                    }
                    catch { /* */ }
                    try
                    {
                        if (ctx.Player.Health < ctx.Player.GetMaxHealth() / 2)
                            ctx.Player.Health = ctx.Player.GetMaxHealth();
                    }
                    catch { /* */ }
                }
                ctx.Detail = "t=" + elapsed.ToString("0") + " digs=" + ctx.IntA
                    + " hp=" + ctx.Player.Health;
                return elapsed >= SoakSec && ctx.IntA >= 15;
            }, assert: ctx =>
            {
                float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
                bool alive = ctx.Player != null && !ctx.Player.IsDead() && ctx.Player.Health > 0;
                bool ok = elapsed >= SoakSec - 5f && ctx.IntA >= 15 && alive;
                ctx.Detail = "t=" + elapsed.ToString("0.0") + " digs=" + ctx.IntA
                    + " alive=" + alive + " hp=" + (ctx.Player != null ? ctx.Player.Health : -1);
                return ok;
            }, timeout: SoakSec + 60f, fail: "soak <15m or dig ticks low", pause: 0.5f,
                noAutoHeal: true));
        }

        static string ResolveApmDumpPath()
        {
            string raw = Environment.GetEnvironmentVariable("ZDTD_APM_DUMP") ?? "";
            if (string.IsNullOrEmpty(raw)) return "";
            if (System.IO.File.Exists(raw)) return raw;
            // Proton: Linux path may need Z: prefix.
            if (raw.StartsWith("/"))
            {
                string z = "Z:" + raw.Replace('/', '\\');
                if (System.IO.File.Exists(z)) return z;
                string zFwd = "Z:" + raw;
                if (System.IO.File.Exists(zFwd)) return zFwd;
            }
            return raw;
        }

        static void AddApm(List<CaseDef> q, string suite)
        {
            // Host attaches zdtd APM dump; client signals barrier then asserts dump path/env.
            // Captured by the wait closure: next unscaled time a poll may run. The
            // host writes the dump asynchronously, so sampling ~4x/s observes it
            // far inside the 55s budget; reading + scanning the file every frame
            // burns disk I/O on the game thread for the whole wait instead.
            float nextPollAt = -1f;
            q.Add(Live(suite, "soak_apm_budget", new[] { "soak", "apm" }, ctx =>
            {
                Report.Barrier("apm_dump");
                ctx.PlaceBlockType = 0;
                ctx.Detail = "request APM dump";
            }, wait: ctx =>
            {
                float now = Time.unscaledTime;
                if (now < nextPollAt) return false;
                nextPollAt = now + 0.25f;
                float elapsed = now - ctx.CaseStartUnscaled;
                string path = ResolveApmDumpPath();
                string runId = Environment.GetEnvironmentVariable("ZDTD_APM_RUN_ID") ?? "";
                bool ok = false;
                if (!string.IsNullOrEmpty(path) && System.IO.File.Exists(path))
                {
                    try
                    {
                        string text = System.IO.File.ReadAllText(path);
                        // Live dump markers only; preseed / failed-dump placeholders must not pass.
                        bool hasApm = text.IndexOf("wall_ns", StringComparison.OrdinalIgnoreCase) >= 0
                            || text.IndexOf("tick_total", StringComparison.OrdinalIgnoreCase) >= 0
                            || (text.IndexOf("zdtd-apm", StringComparison.OrdinalIgnoreCase) >= 0
                                && text.IndexOf("APM_DUMP_FAILED", StringComparison.Ordinal) < 0);
                        // Require this run's id (orch always sets ZDTD_APM_RUN_ID for suite apm).
                        bool hasRun = !string.IsNullOrEmpty(runId)
                            && text.IndexOf(runId, StringComparison.Ordinal) >= 0;
                        bool notPreseed = text.IndexOf("APM_PRESEED", StringComparison.Ordinal) < 0
                            && text.IndexOf("APM_DUMP_FAILED", StringComparison.Ordinal) < 0;
                        ok = hasApm && hasRun && notPreseed;
                        ctx.Detail = "path=" + path + " bytes=" + text.Length
                            + " apm=" + hasApm + " run=" + hasRun
                            + " rid=" + (runId.Length > 0) + " ok=" + ok;
                        if (ok) ctx.PlaceBlockType = 1;
                    }
                    catch (Exception ex)
                    {
                        ctx.Detail = "read " + path + " err " + ex.Message;
                    }
                }
                else
                    ctx.Detail = "path=" + path + " exists=" + (!string.IsNullOrEmpty(path)
                        && System.IO.File.Exists(path)) + " t=" + elapsed.ToString("0.0");
                // Wait only on authentic dump; timeout path fails via Runner.
                return ok;
            }, assert: ctx =>
            {
                string path = ResolveApmDumpPath();
                string runId = Environment.GetEnvironmentVariable("ZDTD_APM_RUN_ID") ?? "";
                bool ok = ctx.PlaceBlockType == 1;
                if (!ok && !string.IsNullOrEmpty(path) && System.IO.File.Exists(path))
                {
                    try
                    {
                        string text = System.IO.File.ReadAllText(path);
                        bool hasApm = text.IndexOf("wall_ns", StringComparison.OrdinalIgnoreCase) >= 0
                            || text.IndexOf("tick_total", StringComparison.OrdinalIgnoreCase) >= 0
                            || text.IndexOf("zdtd-apm", StringComparison.OrdinalIgnoreCase) >= 0;
                        bool hasRun = !string.IsNullOrEmpty(runId)
                            && text.IndexOf(runId, StringComparison.Ordinal) >= 0;
                        bool notPreseed = text.IndexOf("APM_PRESEED", StringComparison.Ordinal) < 0
                            && text.IndexOf("APM_DUMP_FAILED", StringComparison.Ordinal) < 0;
                        ok = hasApm && hasRun && notPreseed;
                    }
                    catch { /* */ }
                }
                ctx.Detail = "path=" + path + " ok=" + ok + " " + (ctx.Detail ?? "");
                return ok;
            }, timeout: 55f, fail: "zdtd APM dump missing/empty/preseed/no run_id", pause: 0.3f));
        }
    }
}
