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
    }
}
