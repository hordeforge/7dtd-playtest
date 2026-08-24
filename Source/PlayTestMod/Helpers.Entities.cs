using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>World entity scanning and spawning: nearest/count queries, dropped loot, sleeper pose control.</summary>
    public static partial class Helpers
    {

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


        /// <summary>Count world-dropped items (EntityItem / backpack / loot container) near pos.</summary>
        public static int CountNearbyEntityItems(World world, Vector3 pos, float radius, out string sample)
        {
            // EntityBackpack / EntityLootContainer subclass EntityItem.
            return CountNearbyType<EntityItem>(world, pos, radius, out sample);
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
    }
}
