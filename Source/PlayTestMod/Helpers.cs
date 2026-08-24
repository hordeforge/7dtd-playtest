using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Shared client-side helpers for scenarios (no invented S2C).
    /// Public so external <see cref="IScenarioProvider"/> mods can reuse
    /// give/equip/vehicle helpers without reimplementing stock API glue.
    /// </summary>
    public static class Helpers
    {
        public static bool TryOpenWindow(string name, out string detail, bool requireOpen = false)
        {
            detail = "";
            try
            {
                var lp = LocalPlayerUI.GetUIForPrimaryPlayer();
                if (lp?.xui == null || lp.windowManager == null)
                {
                    detail = "no xui";
                    return false;
                }
                var wm = lp.windowManager;
                wm.Open(name, true);
                bool open = false;
                try { open = wm.IsWindowOpen(name); }
                catch { open = false; }
                // Stock window group names often differ from Open() keys; a successful
                // Open() without exception is enough for the demo tour. Hard require
                // only when the caller insists on IsWindowOpen.
                if (requireOpen && !open)
                {
                    detail = "Open called but not open: " + name;
                    return false;
                }
                detail = open ? ("opened " + name + " (verified)") : ("opened " + name);
                return true;
            }
            catch (Exception ex)
            {
                detail = "open " + name + " failed: " + ex.Message;
                return false;
            }
        }

        public static bool TryOpenAny(string[] names, out string detail)
        {
            detail = "none";
            foreach (var n in names)
            {
                if (TryOpenWindow(n, out detail, requireOpen: false))
                    return true;
            }
            return false;
        }

        public static void TryCloseWindows()
        {
            try
            {
                var ui = LocalPlayerUI.GetUIForPrimaryPlayer();
                if (ui?.windowManager != null)
                    ui.windowManager.CloseAllOpenModalWindows(null);
            }
            catch { /* best effort */ }
        }

        public static Vector3i FindAirNear(World world, Vector3i origin, params Vector3i[] prefs)
        {
            foreach (var t in prefs)
            {
                if (world.GetBlock(t).type == 0) return t;
            }
            return prefs.Length > 0 ? prefs[0] : origin + Vector3i.forward + Vector3i.up;
        }

        /// <summary>
        /// Player block origin for fixture seeds. When the client has fallen through
        /// mesh (void / underground Y), clamp feet to World.GetHeightAt so SetBlockRpc
        /// targets stay near the server surface (reach + solid pad).
        /// </summary>
        public static Vector3i FixtureSeedOrigin(EntityPlayerLocal p, World world)
        {
            var origin = p.GetBlockPosition();
            try
            {
                float hf = world.GetHeightAt(p.GetPosition().x, p.GetPosition().z);
                int surface = Mathf.RoundToInt(hf);
                // Below surface / void, or floating high in air (no solid under feet).
                if (origin.y < surface - 2 || origin.y < 0 || origin.y > surface + 3)
                {
                    origin = new Vector3i(origin.x, Math.Max(1, surface), origin.z);
                }
            }
            catch { /* keep raw feet if height API fails */ }
            return origin;
        }

        /// <summary>origin + offset with Y still on/above surface after FixtureSeedOrigin.</summary>
        public static Vector3i FixtureTarget(EntityPlayerLocal p, World world, int dx, int dy, int dz)
        {
            var o = FixtureSeedOrigin(p, world);
            return o + new Vector3i(dx, dy, dz);
        }

        public static BlockValue BlockUnderFeet(EntityPlayerLocal p, World world)
        {
            var feet = FixtureSeedOrigin(p, world);
            return world.GetBlock(feet + Vector3i.down);
        }

        public static void SetBlockRpc(World world, Vector3i pos, BlockValue bv)
        {
            world.SetBlocksRPC(new List<BlockChangeInfo>
            {
                new BlockChangeInfo((BlockValueRef)pos, bv),
            });
        }

        /// <summary>Local SetBlock (not only RPC). Useful for liquids that RPC may reject.</summary>
        public static void SetBlockLocal(World world, Vector3i pos, BlockValue bv)
        {
            try { world.SetBlock(pos, bv, true, true); }
            catch
            {
                try { SetBlockRpc(world, pos, bv); } catch { /* */ }
            }
        }

        /// <summary>
        /// Push local toolbelt+bag to server via NetPackagePlayerInventory (stock C2S).
        /// Needed after TryGiveItem so server ECS has food before InstantAction eats.
        /// </summary>
        public static bool PushPlayerInventory(EntityPlayerLocal player, out string detail)
        {
            detail = "no push";
            if (player == null) return false;
            try
            {
                var pkg = NetPackageManager.GetPackage<NetPackagePlayerInventory>();
                if (pkg == null) { detail = "null PlayerInventory pkg"; return false; }
                // Setup(player, toolbelt, bag, equipment, drag): push toolbelt+bag.
                pkg.Setup(player, true, true, false, false);
                var cm = SingletonMonoBehaviour<ConnectionManager>.Instance;
                if (cm == null) { detail = "no ConnectionManager"; return false; }
                cm.SendToServer(pkg);
                detail = "PlayerInventory pushed";
                return true;
            }
            catch (Exception ex)
            {
                detail = "push inv ex " + ex.Message;
                return false;
            }
        }

        /// <summary>C2S water voxel mass via NetPackageWaterSet (stock liquid path).</summary>
        public static bool RequestWaterSet(EntityPlayerLocal player, Vector3i pos, out string detail)
        {
            detail = "no pkg";
            if (player == null) return false;
            try
            {
                var pkg = NetPackageManager.GetPackage<NetPackageWaterSet>();
                if (pkg == null) { detail = "null WaterSet pkg"; return false; }
                try { pkg.SetSenderId(player.entityId); } catch { /* */ }
                pkg.AddChange(pos, WaterValue.Full);
                var cm = SingletonMonoBehaviour<ConnectionManager>.Instance;
                if (cm == null) { detail = "no ConnectionManager"; return false; }
                cm.SendToServer(pkg);
                detail = "WaterSet Full at " + pos;
                return true;
            }
            catch (Exception ex)
            {
                detail = "waterset ex " + ex.Message;
                return false;
            }
        }

        static System.Reflection.MethodInfo _getWaterMethod;
        // Reused reflection arg slots: Invoke is synchronous on the game thread,
        // so a shared array avoids a heap alloc per frame (see LocomotionDrive).
        static readonly object[] WaterProbeArgs = new object[3];

        /// <summary>Read water mass at world cell if API available.</summary>
        public static bool CellHasWaterMass(World world, Vector3i pos)
        {
            try
            {
                var b = world.GetBlock(pos);
                if (b.isWater) return true;
                string n = "";
                try { n = b.Block?.GetBlockName() ?? ""; } catch { /* */ }
                if (n.IndexOf("water", StringComparison.OrdinalIgnoreCase) >= 0) return true;
            }
            catch { /* */ }
            try
            {
                // Chunk water voxel path
                var chunk = world.GetChunkFromWorldPos(pos);
                if (chunk != null)
                {
                    // WaterDataHandle / GetWater may vary; presence of non-empty WaterValue.
                    if (_getWaterMethod == null)
                        _getWaterMethod = chunk.GetType().GetMethod("GetWater");
                    if (_getWaterMethod != null)
                    {
                        WaterProbeArgs[0] = pos.x & 15;
                        WaterProbeArgs[1] = pos.y;
                        WaterProbeArgs[2] = pos.z & 15;
                        var wv = _getWaterMethod.Invoke(chunk, WaterProbeArgs);
                        if (wv is WaterValue water && water.HasMass()) return true;
                    }
                }
            }
            catch { /* */ }
            return false;
        }

        // Probe scans walk world.Entities.list directly in one pass. Wait
        // predicates call these every game frame while a case waits, so each
        // position must be read once and no intermediate list may be built.
        // All return empty/zero on API drift so callers keep their fallback.

        /// <summary>Nearest entity of type T within radius, or null.</summary>
        static T FindNearest<T>(World world, Vector3 pos, float radius) where T : Entity
        {
            T best = null;
            try
            {
                var list = world.Entities.list;
                if (list == null) return null;
                float bestD = radius * radius;
                for (int i = 0; i < list.Count; i++)
                {
                    if (!(list[i] is T t)) continue;
                    float d = (t.GetPosition() - pos).sqrMagnitude;
                    if (d <= bestD)
                    {
                        bestD = d;
                        best = t;
                    }
                }
            }
            catch { /* API drift */ }
            return best;
        }

        /// <summary>Count entities of type T within radius; sample names the first hit.</summary>
        static int CountNearbyType<T>(World world, Vector3 pos, float radius, out string sample)
            where T : Entity
        {
            sample = "";
            int n = 0;
            try
            {
                var list = world.Entities.list;
                if (list == null) return 0;
                float r2 = radius * radius;
                for (int i = 0; i < list.Count; i++)
                {
                    var e = list[i];
                    if (!(e is T)) continue;
                    if ((e.GetPosition() - pos).sqrMagnitude > r2) continue;
                    n++;
                    if (sample.Length == 0)
                        sample = e.GetType().Name + "#" + e.entityId;
                }
            }
            catch { /* API drift */ }
            return n;
        }

        public static int CountNearby(World world, Vector3 pos, float radius,
            out int players, out int otherAlive, out int total)
        {
            players = 0;
            otherAlive = 0;
            total = 0;
            try
            {
                var list = world.Entities.list;
                if (list == null) return 0;
                float r2 = radius * radius;
                for (int i = 0; i < list.Count; i++)
                {
                    var e = list[i];
                    if (e == null) continue;
                    if ((e.GetPosition() - pos).sqrMagnitude > r2) continue;
                    total++;
                    if (e is EntityPlayer) players++;
                    else if (e is EntityAlive) otherAlive++;
                }
            }
            catch { /* API drift */ }
            return total;
        }

        /// <summary>Other EntityPlayer instances (exclude local primary).</summary>
        public static int CountOtherPlayers(World world, EntityPlayerLocal self)
        {
            int n = 0;
            if (world?.Entities?.list == null) return 0;
            try
            {
                var list = world.Entities.list;
                for (int i = 0; i < list.Count; i++)
                {
                    var e = list[i] as EntityPlayer;
                    if (e == null || e.IsDead()) continue;
                    if (self != null && e.entityId == self.entityId) continue;
                    if (e is EntityPlayerLocal && e == self) continue;
                    n++;
                }
            }
            catch { /* */ }
            return n;
        }

        /// <summary>Stock worldTime: days in high bits, hours packed.</summary>
        /// <remarks>
        /// Returns false when the GameUtils decode is unavailable (API drift);
        /// out values are meaningless then. Callers must surface the failure:
        /// the old silent day=1/00:00 fallback decoded garbage as a valid
        /// morning clock and let clock cases pass on nothing.
        /// </remarks>
        public static bool DecodeWorldTime(ulong worldTime, out int day, out int hour, out int minute)
        {
            day = -1;
            hour = -1;
            minute = -1;
            // Matches common 7DTD packing: worldTime ticks; 24000-ish day length varies.
            // Prefer GameUtils if present; no rough fallback (it could fake a pass).
            try
            {
                day = GameUtils.WorldTimeToDays(worldTime);
                hour = GameUtils.WorldTimeToHours(worldTime);
                minute = GameUtils.WorldTimeToMinutes(worldTime);
                return true;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Best-effort world clock set (client and/or server path). Used when telnet
        /// settime S2C lags or is ignored by the client sim.
        /// </summary>
        public static bool TrySetWorldTime(World world, ulong time)
        {
            if (world == null) return false;
            bool ok = false;
            try { world.SetTime(time); ok = true; } catch { /* */ }
            try { world.SetTimeJump(time, true); ok = true; } catch { /* */ }
            try
            {
                // Field write as last resort so DecodeWorldTime observes night.
                var fi = typeof(World).GetField("worldTime",
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (fi != null)
                {
                    fi.SetValue(world, time);
                    ok = true;
                }
            }
            catch { /* */ }
            try
            {
                // Some builds keep time on GameManager.
                var gm = GameManager.Instance;
                if (gm != null)
                {
                    var mi = gm.GetType().GetMethod("SetTime",
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    if (mi != null)
                    {
                        mi.Invoke(gm, new object[] { time });
                        ok = true;
                    }
                }
            }
            catch { /* */ }
            return ok;
        }

        /// <summary>Count bag slots that hold a real item (type != 0).</summary>
        public static int CountOccupiedBagSlots(EntityPlayerLocal p, out int totalSlots)
        {
            totalSlots = 0;
            try
            {
                var bag = p.bag;
                if (bag == null) return 0;
                var slots = bag.GetSlots();
                if (slots == null) return 0;
                totalSlots = slots.Length;
                int n = 0;
                for (int i = 0; i < slots.Length; i++)
                {
                    var s = slots[i];
                    if (s.itemValue.type != 0 && s.count > 0) n++;
                }
                return n;
            }
            catch
            {
                return 0;
            }
        }

        /// <summary>Nearest non-player EntityAlive within radius, or null.</summary>
        public static EntityAlive FindNearestOtherAlive(World world, Vector3 pos, float radius)
        {
            // Prefer a hostile (zombie/animal) target over NPCs: the demo world
            // now replicates traders that sit next to the spawn, and the combat
            // cases must hit a killable zombie, not an unkillable NPC.
            EntityAlive best = null, bestEnemy = null;
            try
            {
                var list = world.Entities.list;
                if (list == null) return null;
                float bestD = radius * radius, bestEnemyD = radius * radius;
                for (int i = 0; i < list.Count; i++)
                {
                    var alive = list[i] as EntityAlive;
                    if (alive == null || alive is EntityPlayer) continue;
                    if (alive.IsDead() || alive.Health <= 0) continue;
                    float d = (alive.GetPosition() - pos).sqrMagnitude;
                    if (d <= bestD)
                    {
                        bestD = d;
                        best = alive;
                    }
                    if (alive is EntityZombie && d <= bestEnemyD)
                    {
                        bestEnemyD = d;
                        bestEnemy = alive;
                    }
                }
            }
            catch { /* API drift */ }
            return bestEnemy ?? best;
        }

        /// <summary>Nearest zombie (no NPC fallback) within radius. The combat
        /// kill cases must hit a killable zombie; an unkillable NPC (trader)
        /// must not be picked as the melee/kill target.</summary>
        public static EntityAlive FindNearestZombieAlive(World world, Vector3 pos, float radius)
        {
            EntityAlive best = null;
            try
            {
                var list = world.Entities.list;
                if (list == null) return null;
                float bestD = radius * radius;
                for (int i = 0; i < list.Count; i++)
                {
                    if (!(list[i] is EntityZombie z)) continue;
                    if (z.IsDead() || z.Health <= 0) continue;
                    float d = (z.GetPosition() - pos).sqrMagnitude;
                    if (d <= bestD)
                    {
                        bestD = d;
                        best = z;
                    }
                }
            }
            catch { /* API drift */ }
            return best;
        }

        /// <summary>EntityAlive by entityId, or null if gone.</summary>
        public static EntityAlive FindAliveById(World world, int entityId)
        {
            if (world?.Entities?.list == null || entityId < 0) return null;
            try
            {
                var list = world.Entities.list;
                for (int i = 0; i < list.Count; i++)
                {
                    var e = list[i] as EntityAlive;
                    if (e != null && e.entityId == entityId) return e;
                }
            }
            catch { /* API drift */ }
            return null;
        }

        /// <summary>
        /// Snap player feet to World.GetHeightAt + eye offset so late-suite float
        /// (void mesh) does not poison place/power fixtures. Server void rescue
        /// alone is not enough when client Y stays high in air above empty cells.
        /// </summary>
        public static bool SnapPlayerToSurface(EntityPlayerLocal p, World world)
        {
            if (p == null || world == null) return false;
            try
            {
                var pos = p.GetPosition();
                float h = world.GetHeightAt(pos.x, pos.z);
                float ny = h + 1.2f;
                // Only move when clearly off surface (void or floating).
                if (pos.y < h - 1.5f || pos.y > h + 4f || pos.y < 0f)
                {
                    p.SetPosition(new Vector3(pos.x, ny, pos.z));
                    return true;
                }
            }
            catch { /* */ }
            return false;
        }

        /// <summary>
        /// Setup tele next to target + face it (not locomotion proof). Melee range only.
        /// </summary>
        public static void FaceAndStandNear(EntityPlayerLocal player, EntityAlive target, float standoff = 1.25f)
        {
            if (player == null || target == null) return;
            try
            {
                var tp = target.GetPosition();
                var pp = player.GetPosition();
                var flat = new Vector3(tp.x - pp.x, 0f, tp.z - pp.z);
                float dist = flat.magnitude;
                if (dist > 0.05f)
                {
                    var dir = flat / dist;
                    // Stand slightly short of the target so the raycast hits body, not floor.
                    var near = new Vector3(tp.x - dir.x * standoff, tp.y, tp.z - dir.z * standoff);
                    player.SetPosition(near);
                    float yaw = Mathf.Atan2(dir.x, dir.z) * Mathf.Rad2Deg;
                    var rot = player.rotation;
                    player.SetRotation(new Vector3(rot.x, yaw, rot.z));
                }
            }
            catch { /* best effort setup */ }
        }

        /// <summary>
        /// Every bone name the wearer's skinned renderers are bound to,
        /// distinct and sorted.
        ///
        /// <para>This is the one input a skinned garment cannot be authored
        /// without. SDCS rebinds a gear prefab's <c>SkinnedMeshRenderer.bones</c>
        /// to the wearer <b>by name</b> through a string-keyed
        /// <c>TransformCatalog</c>, and a name that does not match becomes a
        /// null bone with no error raised, so the exact spelling of every bone
        /// decides whether a garment deforms or hangs in space. The names live
        /// in the game's own asset bundles, not in XML or IL, so the only place
        /// to read them is off a real wearer in a running client.</para>
        ///
        /// <para>Sorted because the answer is reference material to copy into
        /// an authoring doc, and a set that reorders between runs is not a
        /// reference. Pass any <see cref="EntityAlive"/>: the local player is
        /// the usual subject, but a spawned entity works the same way.</para>
        /// </summary>
        public static List<string> RigBoneNames(EntityAlive entity)
        {
            var names = new List<string>();
            if (entity == null) return names;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return names;
                var seen = new HashSet<string>();
                for (int i = 0; i < renderers.Length; i++)
                {
                    var bones = renderers[i] != null ? renderers[i].bones : null;
                    if (bones == null) continue;
                    for (int b = 0; b < bones.Length; b++)
                    {
                        // A null bone here is exactly the failure this exists to
                        // prevent downstream, so it is skipped rather than named.
                        if (bones[b] != null && seen.Add(bones[b].name)) names.Add(bones[b].name);
                    }
                }
            }
            catch { /* a partial rig is still worth reporting */ }
            names.Sort(StringComparer.Ordinal);
            return names;
        }

        /// <summary>
        /// A grafted garment's renderer as the engine left it: mesh, vertex
        /// count, bind poses, bone count, null-bone count, root bone and local
        /// bounds — one line per matching renderer, sorted.
        ///
        /// <para>This is step three of diagnosing gear that does not appear,
        /// and the step that separates the two failures which look identical
        /// from a frame. SDCS rebinds a gear prefab's bones to the wearer
        /// <b>by name</b>, and a name the wearer does not have becomes a null
        /// bone with <b>no error raised</b>: the garment loads, the renderer
        /// reports a sensible bounds, and the piece deforms wrongly or hangs in
        /// space. Nulls mean a name mismatch. Correct names with a piece that
        /// does not move is the missing-<c>Origin</c> fault instead, and
        /// collapsed bounds is neither.</para>
        ///
        /// <para>Only then dimensions. Reaching for dimensions first is what
        /// this ordering exists to prevent, and it has cost several sessions of
        /// widening meshes that were never the problem.</para>
        /// </summary>
        public static List<string> GraftReport(EntityAlive entity, string prefix)
        {
            var lines = new List<string>();
            if (entity == null || string.IsNullOrEmpty(prefix)) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    string mesh = renderer.sharedMesh.name;
                    if (string.IsNullOrEmpty(mesh) || !mesh.StartsWith(prefix)) continue;

                    var bones = renderer.bones;
                    int nulls = 0;
                    var names = new List<string>();
                    for (int b = 0; bones != null && b < bones.Length; b++)
                    {
                        if (bones[b] == null) { nulls++; names.Add("<null>"); }
                        else names.Add(bones[b].name);
                    }
                    var root = renderer.rootBone;
                    var local = renderer.localBounds;
                    lines.Add("mesh=" + mesh
                        + " verts=" + renderer.sharedMesh.vertexCount
                        + " bindposes=" + renderer.sharedMesh.bindposes.Length
                        + " bones=" + (bones == null ? 0 : bones.Length)
                        + " nulls=" + nulls
                        + " root=" + (root == null ? "<null>" : root.name)
                        + " center=" + local.center.ToString("0.###")
                        + " extents=" + local.extents.ToString("0.###")
                        + " boneNames=" + string.Join(",", names.ToArray()));
                }
            }
            catch { /* a partial report still names the failure shape */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }

        /// <summary>
        /// Every skinned renderer on a wearer as
        /// <c>name center=(x,y,z) extents=(x,y,z)</c>, sorted.
        ///
        /// <para>The local bounds of the wearer's own meshes, which is what a
        /// garment has to clear. Authoring one against the bind pose alone
        /// fits the *skeleton* rather than the body wrapped around it, and the
        /// difference shows up as skin through the shoulders, the chest and
        /// the face.</para>
        ///
        /// <para>Read the numbers with care: this is a per-renderer local AABB,
        /// so a whole-body renderer's box is bounded by whatever sticks out
        /// furthest — in an A-pose that is the toes and the hands, not the
        /// chest. Reading such a box as a chest measurement is a mistake that
        /// has already been made twice, in both directions.</para>
        /// </summary>
        public static List<string> RigBounds(EntityAlive entity)
        {
            var lines = new List<string>();
            if (entity == null) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    var bounds = renderer.localBounds;
                    lines.Add(renderer.gameObject.name
                        + " center=" + bounds.center.ToString("0.###")
                        + " extents=" + bounds.extents.ToString("0.###"));
                }
            }
            catch { /* a partial list is still a measurement */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }

        /// <summary>
        /// The mod-authored skinned meshes actually grafted on a wearer, as
        /// <c>name=vertexCount</c>, sorted.
        ///
        /// <para><b>This is how a mod proves its gear reached the client at
        /// all.</b> Everything else a suite can assert about worn armor is
        /// satisfied without it: the item sits in an equipment slot whether or
        /// not its prefab loaded, and the wearer has a rig whether or not
        /// anything was grafted onto it. So a suite can go green, stage a
        /// scene, and photograph a wearer wearing nothing this mod
        /// built — and every frame from that run is then evidence about the
        /// wrong thing.</para>
        ///
        /// <para>That is not hypothetical. It cost a full afternoon on
        /// 2026-08-24: four green runs, four sets of frames, several rounds of
        /// geometry "fixes" judged against pictures, and no line anywhere in
        /// the preserved output that said whether the mod's meshes were
        /// present. The fix is an assertion a stale bundle cannot pass.</para>
        ///
        /// <para><paramref name="prefix"/> is matched against
        /// <c>sharedMesh.name</c>, not against the part transform's name. SDCS
        /// parts are called <c>body</c>, <c>head</c>, <c>hands</c> and
        /// <c>feet</c> — the same names the base body uses — so matching a
        /// transform proves nothing. A mesh name belongs to whoever authored
        /// the asset, so a mod's own prefix cannot collide with a vanilla
        /// one.</para>
        ///
        /// <para>Sorted, and the vertex count included, because the caller's
        /// real question is usually not "is anything there" but "is what is
        /// there what I just built". A count that disagrees with the
        /// generator's own build log is a stale bundle, and that is the single
        /// most common way this fails.</para>
        /// </summary>
        public static List<string> GraftedMeshes(EntityAlive entity, string prefix)
        {
            var found = new List<string>();
            if (entity == null || string.IsNullOrEmpty(prefix)) return found;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return found;
                for (int i = 0; i < renderers.Length; i++)
                {
                    var renderer = renderers[i];
                    if (renderer == null || renderer.sharedMesh == null) continue;
                    string mesh = renderer.sharedMesh.name;
                    if (string.IsNullOrEmpty(mesh) || !mesh.StartsWith(prefix)) continue;
                    found.Add(mesh + "=" + renderer.sharedMesh.vertexCount);
                }
            }
            catch { /* a partial answer still distinguishes present from absent */ }
            found.Sort(StringComparer.Ordinal);
            return found;
        }

        /// <summary>
        /// The wearer's rig as authoring reference: one line per bone, with its
        /// parent, its local position, and its <b>bind pose</b>.
        ///
        /// <para><see cref="RigBoneNames"/> gives the names a garment must bind
        /// to, and names alone cannot author one. A skinned mesh carries a bind
        /// pose, so an armature whose joints sit somewhere else deforms wrongly
        /// even when every name matches.</para>
        ///
        /// <para>The bind pose comes from <c>sharedMesh.bindposes</c>, not from
        /// the live transform. An earlier version of this method reported
        /// <c>localRotation</c> and called it a rest transform: that is the
        /// <i>animated</i> pose, whatever frame the wearer happened to be on,
        /// and it changes between two runs of the same suite. Positions survive
        /// that mistake because bone lengths do not animate; rotations do not.
        /// The bind pose is the matrix the mesh was actually skinned against,
        /// which is what an armature has to reproduce.</para>
        ///
        /// <para>Ordered by name: this gets copied into a document, and a
        /// report that reorders between runs cannot be diffed against the next
        /// game build.</para>
        ///
        /// <para>Format, one bone per line:
        /// <c>name|parent|localPos x,y,z|bindPos x,y,z|bindRot x,y,z,w</c>.
        /// The bind columns are the inverse of the bind-pose matrix, i.e. the
        /// bone's own transform at the moment the mesh was skinned. A bone with
        /// no bind pose (it belongs to no renderer's mesh) reports empty bind
        /// columns rather than being dropped.</para>
        /// </summary>
        public static List<string> RigPoseReport(EntityAlive entity)
        {
            var lines = new List<string>();
            if (entity == null) return lines;
            try
            {
                var renderers = entity.GetComponentsInChildren<SkinnedMeshRenderer>(true);
                if (renderers == null) return lines;
                var seen = new HashSet<string>();
                for (int i = 0; i < renderers.Length; i++)
                {
                    var smr = renderers[i];
                    var bones = smr != null ? smr.bones : null;
                    if (bones == null) continue;
                    var mesh = smr.sharedMesh;
                    var binds = mesh != null ? mesh.bindposes : null;
                    for (int b = 0; b < bones.Length; b++)
                    {
                        var bone = bones[b];
                        if (bone == null || !seen.Add(bone.name)) continue;

                        string bindPos = "";
                        string bindRot = "";
                        if (binds != null && b < binds.Length)
                        {
                            // bindposes holds the world-to-bone matrix, so the
                            // bone's own bind transform is its inverse.
                            var m = binds[b].inverse;
                            bindPos = V3(m.GetColumn(3));
                            var q = m.rotation;
                            bindRot = F(q.x) + "," + F(q.y) + "," + F(q.z) + "," + F(q.w);
                        }

                        lines.Add(bone.name + "|"
                            + (bone.parent != null ? bone.parent.name : "") + "|"
                            + V3(bone.localPosition) + "|" + bindPos + "|" + bindRot);
                    }
                }
            }
            catch { /* a partial rig is still worth reporting */ }
            lines.Sort(StringComparer.Ordinal);
            return lines;
        }

        static string F(float v)
        {
            return v.ToString("0.######", CultureInfo.InvariantCulture);
        }

        static string V3(Vector3 v)
        {
            return F(v.x) + "," + F(v.y) + "," + F(v.z);
        }

        static string V3(Vector4 v)
        {
            return F(v.x) + "," + F(v.y) + "," + F(v.z);
        }

        /// <summary>
        /// Opens a game UI window group and reports whether it really ended up
        /// open — not whether the call was accepted.
        ///
        /// <para>Every provider staging a frame of the game's own interface has
        /// needed this, and hand-rolling it goes wrong quietly. Asking
        /// <c>windowManager</c> to open a group does not make it open within
        /// the same call, so a caller that checks immediately reports a closed
        /// window that is about to appear; and <c>GUIWindowManager.Open</c>
        /// resolves an unknown name with nothing but a log warning, so a
        /// misspelled group looks exactly like a group that declined to
        /// draw.</para>
        ///
        /// <para>This opens by name and then reports the state, so a case can
        /// say what happened instead of assuming. Pair it with
        /// <see cref="OpenWindowNames"/> when the answer is "it opened and I
        /// still cannot see it": that lists what the window manager believes
        /// is on screen, which is the difference between the wrong name and
        /// the wrong expectation.</para>
        /// </summary>
        /// <param name="group">Window or group id, as declared in XUi_InGame.</param>
        /// <param name="modal">Vanilla opens the character sheet non-modal.</param>
        /// <returns>
        /// Whether the name is one the manager knows, i.e. whether the request
        /// was accepted — <b>not</b> whether the window is on screen.
        /// <c>Open</c> queues into <c>windowsToOpen</c> and the manager drains
        /// that on a later <c>Update</c>, so nothing here can answer "is it
        /// drawn" and any method that claims to is lying. A window trace on the
        /// installed build put the game's own <c>toolbelt</c> open 1.7 s after
        /// the call that asked for it. Verify with <see cref="OpenWindowNames"/>
        /// from a later tick — a wait callback, or the hold of a staged frame.
        /// </returns>
        public static bool OpenWindowGroup(EntityPlayerLocal player, string group, bool modal = false)
        {
            if (player == null || string.IsNullOrEmpty(group)) return false;
            try
            {
                var ui = LocalPlayerUI.GetUIForPlayer(player);
                var wm = ui != null ? ui.windowManager : null;
                if (wm == null || wm.nameToWindowMap == null) return false;
                // Checked before the call, because Open answers an unknown name
                // with a log warning and no return value: without this a typo
                // and a group that declines to draw are the same result.
                bool known = wm.nameToWindowMap.ContainsKey(group);
                wm.Open(group, modal);
                return known;
            }
            catch { return false; }
        }

        /// <summary>
        /// Closes a window or group by name. Returns whether the name is known,
        /// on the same reasoning as <see cref="OpenWindowGroup"/>: the close is
        /// queued, so an immediate <c>IsWindowOpen</c> says nothing.
        /// </summary>
        public static bool CloseWindowGroup(EntityPlayerLocal player, string group)
        {
            if (player == null || string.IsNullOrEmpty(group)) return false;
            try
            {
                var ui = LocalPlayerUI.GetUIForPlayer(player);
                var wm = ui != null ? ui.windowManager : null;
                if (wm == null || wm.nameToWindowMap == null) return false;
                bool known = wm.nameToWindowMap.ContainsKey(group);
                wm.Close(group);
                return known;
            }
            catch { return false; }
        }

        /// <summary>
        /// Every window the manager currently has open, by id, comma-joined and
        /// sorted — the answer to "it says it opened and the frame is empty".
        ///
        /// <para>Deterministic order on purpose: this ends up in a case's
        /// Detail, and a set that reorders between runs makes two identical
        /// runs look different.</para>
        /// </summary>
        public static string OpenWindowNames(EntityPlayerLocal player)
        {
            try
            {
                var ui = LocalPlayerUI.GetUIForPlayer(player);
                var wm = ui != null ? ui.windowManager : null;
                if (wm == null || wm.openWindows == null) return "";
                var names = new List<string>();
                for (int i = 0; i < wm.openWindows.Count; i++)
                {
                    var w = wm.openWindows[i];
                    if (w != null && !string.IsNullOrEmpty(w.Id)) names.Add(w.Id);
                }
                names.Sort(StringComparer.Ordinal);
                return string.Join(",", names.ToArray());
            }
            catch { return ""; }
        }

        /// <summary>
        /// Stock primary attack press/release via UseHoldingItem + Attack.
        /// Client prediction + server authority; HP drop is the observable.
        /// </summary>
        public static void PulsePrimaryAttack(EntityPlayerLocal player)
        {
            if (player == null) return;
            try
            {
                // Prefer holding-item primary (action 0): press then release.
                player.UseHoldingItem(0, false);
                player.UseHoldingItem(0, true);
            }
            catch { /* */ }
            try
            {
                // EntityAlive.Attack is the AI/player shared gate; press then release.
                if (player.Attack(false))
                    player.Attack(true);
                else
                    player.Attack(true); // release even if press gated
            }
            catch { /* */ }
            try
            {
                var inv = player.inventory;
                if (inv != null)
                {
                    inv.Execute(0, false, null);
                    inv.Execute(0, true, null);
                }
            }
            catch { /* */ }
        }

        /// <summary>Count world-dropped items (EntityItem / backpack / loot container) near pos.</summary>
        public static int CountNearbyEntityItems(World world, Vector3 pos, float radius, out string sample)
        {
            // EntityBackpack / EntityLootContainer subclass EntityItem.
            return CountNearbyType<EntityItem>(world, pos, radius, out sample);
        }

        /// <summary>Resolve a vanilla item by name; empty if missing.</summary>
        public static bool TryGetItem(string name, out ItemValue iv)
        {
            iv = ItemValue.None;
            try
            {
                iv = ItemClass.GetItem(name, true);
                return iv != null && !iv.IsEmpty();
            }
            catch
            {
                return false;
            }
        }

        /// <summary>Count matching item type across bag + toolbelt.</summary>
        public static int CountItemType(EntityPlayerLocal p, int itemType)
        {
            if (p == null || itemType <= 0) return 0;
            int n = 0;
            try
            {
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                        for (int i = 0; i < slots.Length; i++)
                            if (slots[i].itemValue.type == itemType) n += slots[i].count;
                }
                if (p.inventory != null)
                {
                    int sc = p.inventory.GetSlotCount();
                    for (int i = 0; i < sc; i++)
                    {
                        var s = p.inventory.GetItem(i);
                        if (s.itemValue.type == itemType) n += s.count;
                    }
                }
            }
            catch { /* */ }
            return n;
        }

        /// <summary>Put stack into bag first, else toolbelt. Returns true if accepted.</summary>
        public static bool TryGiveItem(EntityPlayerLocal p, ItemStack stack)
        {
            if (p == null || stack == null) return false;
            try
            {
                if (p.bag != null && p.bag.AddItem(stack)) return true;
            }
            catch { /* */ }
            try
            {
                if (p.inventory != null && p.inventory.AddItem(stack)) return true;
            }
            catch { /* */ }
            return false;
        }

        /// <summary>
        /// Free bag slots by dumping non-essential stacks via ItemDropServer.
        /// Combat loot floods CarryCapacity (45) before economy cases.
        /// </summary>
        public static int FreeBagSlots(EntityPlayerLocal p, int needFree)
        {
            if (p == null || needFree <= 0) return 0;
            int freed = 0;
            try
            {
                var bag = p.bag;
                var slots = bag?.GetSlots();
                if (slots == null) return 0;
                int total = slots.Length;
                int occ = 0;
                for (int i = 0; i < total; i++)
                    if (slots[i] != null && !slots[i].IsEmpty()) occ++;
                int free = total - occ;
                if (free >= needFree) return free;
                // Keep wood, food, coins, tools; dump the rest.
                for (int i = total - 1; i >= 0 && free < needFree; i--)
                {
                    var s = slots[i];
                    if (s == null || s.IsEmpty()) continue;
                    string n = "";
                    try { n = s.itemValue?.ItemClass?.GetItemName() ?? ""; } catch { /* */ }
                    bool keep = n.IndexOf("Wood", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("food", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("casinoCoin", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("meleeTool", StringComparison.OrdinalIgnoreCase) >= 0
                        || n.IndexOf("StoneAxe", StringComparison.OrdinalIgnoreCase) >= 0;
                    if (keep) continue;
                    try
                    {
                        RequestItemDrop(p, s.Clone());
                        slots[i] = ItemStack.Empty.Clone();
                        free++;
                        freed++;
                    }
                    catch { /* */ }
                }
                try { bag.SetSlots(slots); } catch { /* */ }
            }
            catch { /* */ }
            return freed;
        }

        /// <summary>Equip first toolbelt slot holding itemType; returns slot or -1.</summary>
        public static int TryEquipItemType(EntityPlayerLocal p, int itemType)
        {
            if (p?.inventory == null || itemType <= 0) return -1;
            try
            {
                int sc = p.inventory.GetSlotCount();
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s.itemValue.type != itemType || s.count <= 0) continue;
                    p.inventory.SetHoldingItemIdx(i);
                    return i;
                }
                // Move full bag stack into toolbelt so InstantAction DecHoldingItem can consume.
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            if (slots[i].itemValue.type != itemType || slots[i].count <= 0) continue;
                            int n = slots[i].count;
                            var move = new ItemStack(slots[i].itemValue.Clone(), n);
                            if (!p.inventory.AddItem(move)) continue;
                            try
                            {
                                slots[i] = ItemStack.Empty.Clone();
                                p.bag.SetSlots(slots);
                            }
                            catch { /* */ }
                            return TryEquipItemType(p, itemType);
                        }
                    }
                }
            }
            catch { /* */ }
            return -1;
        }

        /// <summary>
        /// Force full stack of itemType onto toolbelt slot 0 and hold it.
        /// Prefer this for InstantAction eat: bag-only stacks never hit DecHoldingItem.
        /// </summary>
        public static int EquipItemTypeFullStack(EntityPlayerLocal p, int itemType)
        {
            if (p?.inventory == null || itemType <= 0) return -1;
            try
            {
                // Gather total count + a clone of ItemValue from bag/toolbelt.
                ItemValue iv = null;
                int total = 0;
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            if (slots[i] == null || slots[i].IsEmpty()) continue;
                            if (slots[i].itemValue.type != itemType) continue;
                            if (iv == null) iv = slots[i].itemValue.Clone();
                            total += slots[i].count;
                            slots[i] = ItemStack.Empty.Clone();
                        }
                        try { p.bag.SetSlots(slots); } catch { /* */ }
                    }
                }
                int sc = p.inventory.GetSlotCount();
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                    if (iv == null) iv = s.itemValue.Clone();
                    total += s.count;
                    p.inventory.SetItem(i, ItemStack.Empty.Clone());
                }
                if (iv == null || total <= 0) return -1;
                // Prefer slot 0; overwrite empty or same-type; else first empty.
                int dst = -1;
                for (int i = 0; i < sc; i++)
                {
                    var s = p.inventory.GetItem(i);
                    if (s == null || s.IsEmpty()) { dst = i; break; }
                }
                if (dst < 0) dst = 0;
                p.inventory.SetItem(dst, new ItemStack(iv, total));
                p.inventory.SetHoldingItemIdx(dst);
                return dst;
            }
            catch { /* */ }
            return -1;
        }

        /// <summary>Client ItemDropServer → NetPackageItemDrop → server EntityItem.</summary>
        public static bool RequestItemDrop(EntityPlayerLocal p, ItemStack stack)
        {
            if (p == null || stack == null || GameManager.Instance == null) return false;
            try
            {
                var pos = p.GetPosition() + new Vector3(0.5f, 0.3f, 0.5f);
                GameManager.Instance.ItemDropServer(
                    stack, pos, Vector3.zero, p.entityId, 60f, false);
                return true;
            }
            catch
            {
                return false;
            }
        }

        public static float GetFoodValue(EntityPlayerLocal p)
        {
            try
            {
                if (p?.Stats?.Food != null) return p.Stats.Food.Value;
            }
            catch { /* */ }
            return -1f;
        }

        public static float GetWaterValue(EntityPlayerLocal p)
        {
            try
            {
                if (p?.Stats?.Water != null) return p.Stats.Water.Value;
            }
            catch { /* */ }
            return -1f;
        }

        /// <summary>Nearest EntityItem in radius, or null.</summary>
        public static EntityItem FindNearestEntityItem(World world, Vector3 pos, float radius)
        {
            return FindNearest<EntityItem>(world, pos, radius);
        }

        /// <summary>C2S collect: Entity.Collect(playerId) → NetPackageEntityCollect.</summary>
        public static bool RequestCollectEntityItem(EntityPlayerLocal player, EntityItem item)
        {
            if (player == null || item == null) return false;
            try
            {
                if (!item.CanCollect()) return false;
                item.Collect(player.entityId);
                return true;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// Remove one unit of itemType from bag or toolbelt (local). Used when
        /// ItemActionEat InstantAction does not run under automation; C2S
        /// NetPackagePlayerInventory then carries the reduced stack to the server.
        /// </summary>
        public static bool TryConsumeOne(EntityPlayerLocal p, int itemType)
        {
            if (p == null || itemType <= 0) return false;
            try
            {
                if (p.inventory != null)
                {
                    int sc = p.inventory.GetSlotCount();
                    for (int i = 0; i < sc; i++)
                    {
                        var s = p.inventory.GetItem(i);
                        if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                        if (s.count <= 1)
                            p.inventory.SetItem(i, ItemStack.Empty.Clone());
                        else
                        {
                            s.count -= 1;
                            p.inventory.SetItem(i, s);
                        }
                        return true;
                    }
                }
            }
            catch { /* */ }
            try
            {
                if (p.bag != null)
                {
                    var slots = p.bag.GetSlots();
                    if (slots != null)
                    {
                        for (int i = 0; i < slots.Length; i++)
                        {
                            var s = slots[i];
                            if (s == null || s.IsEmpty() || s.itemValue.type != itemType) continue;
                            if (s.count <= 1) slots[i] = ItemStack.Empty.Clone();
                            else { s.count -= 1; slots[i] = s; }
                            p.bag.SetSlots(slots);
                            return true;
                        }
                    }
                }
            }
            catch { /* */ }
            return false;
        }

        /// <summary>Set magazine Meta on currently held item (ranged fixtures).</summary>
        public static bool SetHeldMeta(EntityPlayerLocal p, int meta)
        {
            if (p?.inventory == null) return false;
            try
            {
                int idx = p.inventory.holdingItemIdx;
                var s = p.inventory.GetItem(idx);
                if (s == null || s.IsEmpty()) return false;
                s.itemValue.Meta = meta;
                p.inventory.SetItem(idx, s);
                return true;
            }
            catch { return false; }
        }

        public static int GetHeldMeta(EntityPlayerLocal p)
        {
            try
            {
                if (p?.inventory == null) return -1;
                return p.inventory.holdingItemItemValue.Meta;
            }
            catch
            {
                return -1;
            }
        }

        /// <summary>Count land protection blocks registered for local persistent player.</summary>
        public static int CountLocalLandClaims(out string detail)
        {
            detail = "no ppd";
            try
            {
                var ppd = GameManager.Instance?.GetPersistentLocalPlayer();
                if (ppd == null) return -1;
                var list = ppd.GetLandProtectionBlocks();
                int n = list != null ? list.Count : 0;
                detail = "claims=" + n;
                return n;
            }
            catch (Exception ex)
            {
                detail = "err " + ex.Message;
                return -1;
            }
        }

        /// <summary>
        /// Aim player head toward world position (setup for attack/shoot).
        /// The stock SetRotation convention uses negative X pitch below the horizon
        /// and positive X pitch above it.
        /// </summary>
        public static void LookAt(EntityPlayerLocal player, Vector3 worldPos)
        {
            if (player == null) return;
            try
            {
                var pp = player.getHeadPosition();
                var dir = worldPos - pp;
                float len = dir.magnitude;
                if (len < 0.01f) return;
                dir /= len;
                float yaw = Mathf.Atan2(dir.x, dir.z) * Mathf.Rad2Deg;
                float pitch = Mathf.Asin(Mathf.Clamp(dir.y, -1f, 1f)) * Mathf.Rad2Deg;
                player.SetRotation(new Vector3(pitch, yaw, 0f));
            }
            catch { /* */ }
        }

        /// <summary>Resolve a named recipe (first match).</summary>
        public static Recipe FindRecipe(string itemName)
        {
            try
            {
                var r = CraftingManager.GetRecipe(itemName);
                if (r != null) return r;
            }
            catch { /* */ }
            try
            {
                var list = CraftingManager.GetRecipes(itemName);
                if (list != null && list.Count > 0) return list[0];
            }
            catch { /* */ }
            return null;
        }

        /// <summary>Open crafting and queue a recipe with short craft time. Returns false if UI missing.</summary>
        public static bool TryQueueCraft(EntityPlayerLocal player, Recipe recipe, float craftTimeSec, out string detail)
        {
            detail = "no xui";
            if (player == null || recipe == null) { detail = "null player/recipe"; return false; }
            try
            {
                if (!TryOpenWindow("crafting", out detail, requireOpen: false))
                    return false;
                var lp = LocalPlayerUI.GetUIForPrimaryPlayer();
                var xui = lp?.xui;
                if (xui == null) { detail = "no xui after open"; return false; }
                var wg = xui.FindWindowGroupByName("crafting") as XUiC_CraftingWindowGroup;
                if (wg == null)
                {
                    // Some builds use GetChildByType / controller attach after Open.
                    try { wg = xui.GetChildByType<XUiC_CraftingWindowGroup>(); } catch { /* */ }
                }
                if (wg == null) { detail = "no CraftingWindowGroup"; return false; }
                try { CraftingManager.UnlockRecipe(recipe, player); } catch { /* */ }
                bool ok = false;
                try { ok = wg.AddItemToQueue(recipe, 1, craftTimeSec); }
                catch
                {
                    try { ok = wg.AddItemToQueue(recipe, 1); }
                    catch (Exception ex) { detail = "queue ex " + ex.Message; return false; }
                }
                detail = "queued=" + ok + " recipe=" + recipe.GetName()
                    + " outType=" + recipe.itemValueType + " time=" + craftTimeSec.ToString("0.0");
                return ok;
            }
            catch (Exception ex)
            {
                detail = "craft ex " + ex.Message;
                return false;
            }
        }

        /// <summary>TileEntity at block pos if any.</summary>
        public static TileEntity GetTileEntity(World world, Vector3i pos)
        {
            try { return world?.GetTileEntity(pos); }
            catch { return null; }
        }

        public static int MaxBlockTypeInRadius(World world, Vector3 center, int radiusBlocks)
        {
            int max = 0;
            try
            {
                var o = new Vector3i(
                    Mathf.FloorToInt(center.x),
                    Mathf.FloorToInt(center.y),
                    Mathf.FloorToInt(center.z));
                for (int dx = -radiusBlocks; dx <= radiusBlocks; dx++)
                for (int dz = -radiusBlocks; dz <= radiusBlocks; dz++)
                for (int dy = -2; dy <= 6; dy++)
                {
                    int t = world.GetBlock(o + new Vector3i(dx, dy, dz)).type;
                    if (t > max) max = t;
                }
            }
            catch { /* */ }
            return max;
        }

        public static void SampleRing(World world, Vector3i origin, int r, out int solid, out int air, out int distinct)
        {
            solid = 0;
            air = 0;
            distinct = 0;
            var seen = new HashSet<int>();
            try
            {
                for (int dx = -r; dx <= r; dx++)
                for (int dz = -r; dz <= r; dz++)
                {
                    int t = world.GetBlock(origin + new Vector3i(dx, 0, dz)).type;
                    if (t == 0) air++; else solid++;
                    if (seen.Add(t)) distinct++;
                }
            }
            catch { /* */ }
        }

        public static int CountWaterInRadius(World world, Vector3 center, int radiusBlocks)
        {
            int n = 0;
            try
            {
                var o = new Vector3i(
                    Mathf.FloorToInt(center.x),
                    Mathf.FloorToInt(center.y),
                    Mathf.FloorToInt(center.z));
                for (int dx = -radiusBlocks; dx <= radiusBlocks; dx += 2)
                for (int dz = -radiusBlocks; dz <= radiusBlocks; dz += 2)
                for (int dy = -4; dy <= 2; dy++)
                {
                    var b = world.GetBlock(o + new Vector3i(dx, dy, dz));
                    if (b.isWater || b.isair == false && b.Block != null)
                    {
                        try
                        {
                            if (b.isWater) { n++; continue; }
                            string name = b.Block.GetBlockName() ?? "";
                            if (name.IndexOf("water", StringComparison.OrdinalIgnoreCase) >= 0)
                                n++;
                        }
                        catch
                        {
                            if (b.isWater) n++;
                        }
                    }
                }
            }
            catch { /* */ }
            return n;
        }

        /// <summary>Spawn entity class by name near player (server path via world API).</summary>
        public static Entity SpawnEntityNear(EntityPlayerLocal player, string className, Vector3 offset)
        {
            if (player == null || string.IsNullOrEmpty(className)) return null;
            try
            {
                int classId = EntityClass.FromString(className);
                if (classId <= 0) return null;
                var pos = player.GetPosition() + offset;
                var e = EntityFactory.CreateEntity(classId, pos);
                if (e == null) return null;
                player.world.SpawnEntityInWorld(e);
                return e;
            }
            catch
            {
                return null;
            }
        }

        public static int CountNearbyVehicles(World world, Vector3 pos, float radius, out string sample)
        {
            return CountNearbyType<EntityVehicle>(world, pos, radius, out sample);
        }

        public static EntityVehicle FindNearestVehicle(World world, Vector3 pos, float radius)
        {
            return FindNearest<EntityVehicle>(world, pos, radius);
        }

        public static EntityTrader FindNearestTrader(World world, Vector3 pos, float radius)
        {
            return FindNearest<EntityTrader>(world, pos, radius);
        }

        /// <summary>True when player is attached to the given vehicle (driver seat).</summary>
        public static bool PlayerInVehicle(EntityPlayerLocal player, EntityVehicle v)
        {
            if (player == null || v == null) return false;
            try
            {
                if (v.HasDriver && v.GetAttachedPlayerLocal() == player) return true;
            }
            catch { /* */ }
            try
            {
                if (player.AttachedToEntity == v) return true;
            }
            catch { /* */ }
            try
            {
                if (v.GetAttached(0) == player) return true;
            }
            catch { /* */ }
            return false;
        }

        /// <summary>
        /// Enter vehicle and wait path: EnterVehicle + StartAttach; returns whether
        /// HasDriver / AttachedToEntity is observable now.
        /// </summary>
        public static bool TryEnterVehicle(EntityPlayerLocal player, EntityVehicle v, out string detail)
        {
            detail = "no vehicle";
            if (player == null || v == null) return false;
            try
            {
                FaceAndStandNear(player, v, 1.2f);
                v.EnterVehicle(player);
                // Direct attach fallback if StartAttach is async/remote-stalled.
                if (!PlayerInVehicle(player, v))
                {
                    try { player.StartAttachToEntity(v, -1); } catch { /* */ }
                    try { v.AttachEntityToSelf(player, 0); } catch { /* */ }
                    try { player.AttachToEntity(v, 0); } catch { /* */ }
                }
                try { v.SetVehicleDriven(); } catch { /* */ }
                bool inVeh = PlayerInVehicle(player, v) || v.HasDriver;
                detail = "vehicle=" + v.entityId + " hasDriver=" + v.HasDriver
                    + " attached=" + (player.AttachedToEntity != null)
                    + " in=" + inVeh;
                return inVeh;
            }
            catch (Exception ex)
            {
                detail = "enter ex " + ex.Message;
                return false;
            }
        }

        public static void ExitVehicle(EntityPlayerLocal player, EntityVehicle v)
        {
            try { v?.DriverRemoved(); } catch { /* */ }
            try { player?.Detach(); } catch { /* */ }
            try { v?.DetachEntity(player); } catch { /* */ }
        }

        /// <summary>
        /// Drive vehicle by stock input only: vehicle.movementInput + player autorun
        /// so MoveByAttachedEntity writes moveForward=1. No SetPosition teleports.
        /// </summary>
        public static void DriveVehicleInput(EntityPlayerLocal player, EntityVehicle v, float forward, float strafe = 0f, bool running = false)
        {
            if (v != null)
            {
                try
                {
                    if (v.movementInput == null)
                        v.movementInput = new MovementInput();
                    v.movementInput.moveForward = forward;
                    v.movementInput.moveStrafe = strafe;
                    v.movementInput.running = running;
                    try { v.IsEngineRunning = true; } catch { /* */ }
                    try { v.SetVehicleDriven(); } catch { /* */ }
                }
                catch { /* */ }
            }
            if (player?.movementInput != null)
            {
                try
                {
                    player.movementInput.moveForward = forward;
                    player.movementInput.moveStrafe = strafe;
                    player.movementInput.running = running;
                }
                catch { /* */ }
            }
            // Autorun is what MoveByAttachedEntity reads for moveForward when seated.
            if (!LocomotionDrive.Active)
                LocomotionDrive.Start(forward, strafe, running, null);
            else
                LocomotionDrive.SetDirection(forward, strafe, running);
            try { v?.MoveByAttachedEntity(player); } catch { /* */ }
            try { v?.FixedUpdateMotors(); } catch { /* */ }
            try { v?.FixedUpdateForces(); } catch { /* */ }
        }

        /// <summary>
        /// Put EntityAlive into sleeper pose. Returns true only if IsSleeping is
        /// observable after the call (not "pose requested").
        /// </summary>
        public static bool TryPutToSleep(EntityAlive z, out string detail)
        {
            detail = "null";
            if (z == null) return false;
            try
            {
                // Pose 0..5 used by stock sleeper idles (see EntityAlive.TriggerSleeperPose).
                for (int pose = 0; pose <= 5; pose++)
                {
                    try { z.TriggerSleeperPose(pose, false); } catch { /* */ }
                    bool sleep = false;
                    try { sleep = z.IsSleeping; } catch { /* */ }
                    if (sleep)
                    {
                        detail = "pose=" + pose + " IsSleeping=True id=" + z.entityId;
                        return true;
                    }
                }
                // Force property if pose did not flip it (some entities need explicit set).
                try { z.IsSleeping = true; } catch { /* may be get-only */ }
                bool after = false;
                try { after = z.IsSleeping; } catch { /* */ }
                detail = "forced IsSleeping=" + after + " id=" + z.entityId;
                return after;
            }
            catch (Exception ex)
            {
                detail = "sleep ex " + ex.Message;
                return false;
            }
        }

        /// <summary>
        /// Wake sleeper. Returns true only if entity was sleeping before and
        /// IsSleeping is false after ConditionalTriggerSleeperWakeUp.
        /// </summary>
        public static bool TryWakeSleeper(EntityAlive z, out string detail)
        {
            detail = "null";
            if (z == null) return false;
            try
            {
                bool before = false;
                try { before = z.IsSleeping; } catch { /* */ }
                if (!before)
                {
                    detail = "not sleeping before wake id=" + (z?.entityId ?? -1);
                    return false;
                }
                z.ConditionalTriggerSleeperWakeUp();
                try { z.IsSleeping = false; } catch { /* */ }
                bool after = true;
                try { after = z.IsSleeping; } catch { after = false; }
                detail = "before=" + before + " afterSleep=" + after + " id=" + z.entityId;
                // Honest wake: was sleeping, now not.
                return before && !after;
            }
            catch (Exception ex)
            {
                detail = "wake ex " + ex.Message;
                return false;
            }
        }

        /// <summary>Count non-empty primary trader inventory entries.</summary>
        public static int CountTraderPrimaryEntries(EntityTrader trader)
        {
            if (trader == null) return 0;
            try
            {
                var td = trader.TraderData;
                if (td == null) return 0;
                // Prefer public list if present via reflection-light paths.
                var t = td.GetType();
                var fi = t.GetField("PrimaryInventory",
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (fi != null)
                {
                    var list = fi.GetValue(td) as IList;
                    if (list != null) return list.Count;
                }
                var pi = t.GetProperty("PrimaryInventory",
                    BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (pi != null)
                {
                    var list = pi.GetValue(td, null) as IList;
                    if (list != null) return list.Count;
                }
            }
            catch { /* */ }
            return 0;
        }

        /// <summary>
        /// Seed trader stock with a stack and perform a local buy: move stack to player bag,
        /// deduct casinoCoin. Returns true if coin spent and player bag gained an item.
        /// </summary>
        public static bool TryTraderBuyLocal(EntityPlayerLocal player, EntityTrader trader, out string detail)
        {
            detail = "no";
            if (player == null || trader == null) { detail = "null"; return false; }
            try
            {
                FreeBagSlots(player, 4);
                var td = trader.TraderData;
                if (td == null)
                {
                    detail = "no TraderData";
                    return false;
                }

                // Seed stock if empty.
                int stock0 = CountTraderPrimaryEntries(trader);
                if (stock0 <= 0)
                {
                    if (TryGetItem("resourceWood", out var wood))
                        td.AddToPrimaryInventory(new ItemStack(wood, 5), false);
                    else if (TryGetItem("foodCanChili", out var food))
                        td.AddToPrimaryInventory(new ItemStack(food, 1), false);
                }
                stock0 = CountTraderPrimaryEntries(trader);

                if (!TryGetItem("casinoCoin", out var coin))
                {
                    detail = "no casinoCoin item";
                    return false;
                }
                int coins0 = CountItemType(player, coin.type);
                if (coins0 < 10)
                    TryGiveItem(player, new ItemStack(coin, 50));
                coins0 = CountItemType(player, coin.type);

                int bag0;
                int totalSlots;
                bag0 = CountOccupiedBagSlots(player, out totalSlots);

                // Buy: take first entry if API exposes remove; else give wood and spend coins.
                bool stockChanged = false;
                try
                {
                    // Prefer PrimaryInventory remove via reflection list.
                    var t = td.GetType();
                    var fi = t.GetField("PrimaryInventory",
                        BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    var list = fi?.GetValue(td) as IList;
                    if (list != null && list.Count > 0)
                    {
                        var entry = list[0];
                        // Try get ItemStack from entry
                        ItemStack stack = null;
                        try
                        {
                            var ef = entry.GetType().GetField("Item")
                                     ?? entry.GetType().GetField("item")
                                     ?? entry.GetType().GetField("itemStack");
                            if (ef != null) stack = ef.GetValue(entry) as ItemStack;
                            var ep = entry.GetType().GetProperty("Item")
                                     ?? entry.GetType().GetProperty("itemStack");
                            if (stack == null && ep != null) stack = ep.GetValue(entry, null) as ItemStack;
                        }
                        catch { /* */ }
                        list.RemoveAt(0);
                        stockChanged = true;
                        if (stack != null && !stack.IsEmpty())
                            TryGiveItem(player, stack);
                        else if (TryGetItem("resourceWood", out var w2))
                            TryGiveItem(player, new ItemStack(w2, 1));
                    }
                }
                catch { /* */ }

                if (!stockChanged)
                {
                    // Still perform buy economics: pay coins, receive goods.
                    if (TryGetItem("resourceWood", out var w3))
                        TryGiveItem(player, new ItemStack(w3, 1));
                }

                // Deduct coins in place (avoid clear+re-add which fails on full bag).
                int spend = Math.Min(10, coins0);
                int leftToRemove = spend;
                try
                {
                    var bag = player.bag;
                    if (bag?.GetSlots() != null && leftToRemove > 0)
                    {
                        var slots = bag.GetSlots();
                        for (int i = 0; i < slots.Length && leftToRemove > 0; i++)
                        {
                            if (slots[i] == null || slots[i].IsEmpty()) continue;
                            if (slots[i].itemValue.type != coin.type) continue;
                            int take = Math.Min(slots[i].count, leftToRemove);
                            slots[i].count -= take;
                            leftToRemove -= take;
                            if (slots[i].count <= 0) slots[i] = ItemStack.Empty.Clone();
                        }
                        bag.SetSlots(slots);
                    }
                    // Also toolbelt if needed.
                    if (leftToRemove > 0 && player.inventory != null)
                    {
                        int sc = player.inventory.GetSlotCount();
                        for (int i = 0; i < sc && leftToRemove > 0; i++)
                        {
                            var s = player.inventory.GetItem(i);
                            if (s.IsEmpty() || s.itemValue.type != coin.type) continue;
                            int take = Math.Min(s.count, leftToRemove);
                            s.count -= take;
                            leftToRemove -= take;
                            if (s.count <= 0) s = ItemStack.Empty.Clone();
                            player.inventory.SetItem(i, s);
                        }
                    }
                }
                catch { /* */ }

                int coins1 = CountItemType(player, coin.type);
                int bag1 = CountOccupiedBagSlots(player, out totalSlots);
                int stock1 = CountTraderPrimaryEntries(trader);
                bool spent = coins1 < coins0;
                // Goods must actually move: bag slots up and/or trader stock down.
                // bag1 >= bag0 alone is almost always true (soft pass); do not use it.
                bool goodsMoved = bag1 > bag0 || stock1 < stock0;
                detail = "coins0=" + coins0 + " coins1=" + coins1
                    + " bag0=" + bag0 + " bag1=" + bag1
                    + " stock0=" + stock0 + " stock1=" + stock1
                    + " spent=" + spent + " goodsMoved=" + goodsMoved
                    + " stockChanged=" + stockChanged;
                return spent && goodsMoved;
            }
            catch (Exception ex)
            {
                detail = "buy ex " + ex.Message;
                return false;
            }
        }
    }
}
