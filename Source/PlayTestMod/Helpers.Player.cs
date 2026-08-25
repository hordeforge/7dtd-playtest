using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Local player body state: pose and aim setup, primary attack pulse, food/water stats, held-item meta, land-claim count.</summary>
    public static partial class Helpers
    {

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
    }
}
