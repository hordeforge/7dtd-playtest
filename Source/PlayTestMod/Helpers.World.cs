using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>World mutation and sensing: block edits, fixture placement, water, tile entities, and the world clock.</summary>
    public static partial class Helpers
    {

        /// <summary>The block column a world position occupies.</summary>
        /// <para>Floor, not a truncating cast: a 7 Days to Die world is centred
        /// on the origin and spans roughly -4096..4095, so a position west or
        /// north of it has a negative coordinate, and <c>(int)(-3.7)</c> is
        /// -3, the next column over. Every terrain and biome read keyed on x,z
        /// has to name the column the position is in.</para>
        public static Vector3i BlockColumn(Vector3 pos)
        {
            return new Vector3i(
                Mathf.FloorToInt(pos.x),
                Mathf.FloorToInt(pos.y),
                Mathf.FloorToInt(pos.z));
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


        /// <summary>
        /// <see cref="FixtureSeedOrigin"/> plus the caller's offset. The seed
        /// has already clamped a void or floating player to the surface, so a
        /// non-negative <paramref name="dy"/> keeps the target on or above it.
        /// </summary>
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


        /// <summary>Client-to-server block change. The server accepts it, so a fixture
        /// seeded this way is world state the dedicated really has.</summary>
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

        // Block type -> its name, resolved once per type.
        // A radius scan reads tens of thousands of cells that hold a few
        // hundred distinct block types, so asking each cell's block for its
        // name (a managed-to-native call returning a fresh string) and then
        // searching that string repeats the same answer thousands of times
        // per scan. A block's name does not change while the world is loaded,
        // so the first cell of a type decides for the rest. Type 0 is air and
        // has no name; it is seeded so the commonest cell never calls out.
        static readonly Dictionary<int, string> BlockNameByType = new Dictionary<int, string>
        {
            { 0, "" },
        };

        /// <summary>A block type's name, resolved at most once per type.</summary>
        /// <remarks>
        /// The lookup is guarded exactly as each call site guarded it: a null
        /// block or a throwing accessor yields "" rather than propagating. A
        /// type that answered "" is not cached, so a block whose name was not
        /// resolvable yet is re-asked later rather than pinned to empty.
        /// </remarks>
        static string BlockName(BlockValue b)
        {
            string known;
            if (BlockNameByType.TryGetValue(b.type, out known)) return known;
            string name = "";
            try { name = b.Block?.GetBlockName() ?? ""; } catch { /* */ }
            if (name.Length == 0) return "";
            BlockNameByType[b.type] = name;
            return name;
        }

        /// <summary>Whether a block type's name contains <paramref name="needle"/>
        /// (case-insensitively), memoized per type.</summary>
        static bool BlockNameContains(BlockValue b, string needle, Dictionary<int, bool> memo)
        {
            bool known;
            if (memo.TryGetValue(b.type, out known)) return known;
            bool hit = BlockName(b).IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0;
            memo[b.type] = hit;
            return hit;
        }

        static readonly Dictionary<int, bool> WaterNameByType = new Dictionary<int, bool>
        {
            { 0, false },
        };
        static readonly Dictionary<int, bool> DecoNameByType = new Dictionary<int, bool>
        {
            { 0, false },
        };

        /// <summary>Whether a block's name reads as water.</summary>
        public static bool IsWaterName(BlockValue b)
        {
            return BlockNameContains(b, "water", WaterNameByType);
        }

        /// <summary>Whether a block's name reads as plant/wood-ish decoration.</summary>
        public static bool IsDecoName(BlockValue b)
        {
            bool known;
            if (DecoNameByType.TryGetValue(b.type, out known)) return known;
            // The five needles stay in one place: the memo exists so this list
            // is evaluated once per block type rather than once per cell.
            string name = BlockName(b);
            bool deco = name.IndexOf("tree", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("plant", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("bush", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("grass", StringComparison.OrdinalIgnoreCase) >= 0
                || name.IndexOf("deco", StringComparison.OrdinalIgnoreCase) >= 0;
            DecoNameByType[b.type] = deco;
            return deco;
        }


        /// <summary>Read water mass at world cell if API available.</summary>
        public static bool CellHasWaterMass(World world, Vector3i pos)
        {
            try
            {
                var b = world.GetBlock(pos);
                if (b.isWater) return true;
                if (IsWaterName(b)) return true;
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


        /// <summary>Decode worldTime to day/hour/minute through GameUtils.</summary>
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


        /// <summary>TileEntity at block pos if any.</summary>
        public static TileEntity GetTileEntity(World world, Vector3i pos)
        {
            try { return world?.GetTileEntity(pos); }
            catch { return null; }
        }


        /// <summary>Highest block type in a column footprint. Samples dy -2..6 only,
        /// so it is a near-surface band, not the whole radius.</summary>
        public static int MaxBlockTypeInRadius(World world, Vector3 center, int radiusBlocks)
        {
            int max = 0;
            try
            {
                var o = BlockColumn(center);
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


        /// <summary>Sample one Y level (dy 0) over the square of radius r: solid
        /// count, air count, and distinct block types seen.</summary>
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


        /// <summary>Count water-ish cells over a column footprint, stepping x/z by 2
        /// and sampling dy -4..2. A coarse grid, not a dense radius count.</summary>
        public static int CountWaterInRadius(World world, Vector3 center, int radiusBlocks)
        {
            int n = 0;
            try
            {
                var o = BlockColumn(center);
                for (int dx = -radiusBlocks; dx <= radiusBlocks; dx += 2)
                for (int dz = -radiusBlocks; dz <= radiusBlocks; dz += 2)
                for (int dy = -4; dy <= 2; dy++)
                {
                    var b = world.GetBlock(o + new Vector3i(dx, dy, dz));
                    if (b.isWater) { n++; continue; }
                    if (b.isair || b.Block == null) continue;
                    if (IsWaterName(b)) n++;
                }
            }
            catch { /* */ }
            return n;
        }
    }
}
