using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Vehicle mount and stock-input drive helpers.</summary>
    public static partial class Helpers
    {

        public static int CountNearbyVehicles(World world, Vector3 pos, float radius, out string sample)
        {
            return CountNearbyType<EntityVehicle>(world, pos, radius, out sample);
        }


        public static EntityVehicle FindNearestVehicle(World world, Vector3 pos, float radius)
        {
            return FindNearest<EntityVehicle>(world, pos, radius);
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
    }
}
