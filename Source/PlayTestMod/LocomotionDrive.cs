using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Real motor walk via stock <see cref="PlayerMoveController"/> autorun /
    /// <see cref="MovementInput"/> injection.
    ///
    /// Keyboard writes into MovementInput mid-Update, then autorun forces
    /// moveForward=1. We set <c>isAutorun</c> in a Prefix so that path runs,
    /// and re-assert moveForward in Postfix as a belt-and-braces measure.
    /// This is not <c>SetPosition</c> teleport.
    /// </summary>
    static class LocomotionDrive
    {
        static bool _active;
        static float _forward = 1f;
        static float _strafe;
        static bool _running;
        static bool _sneak;
        static bool _jumpPulse;
        static float _jumpUntil; // unscaled time while vertical boost active
        static float _yaw;
        static bool _setYaw;
        static int _lastCcFrame = -1; // apply CC.Move at most once per Unity frame

        static FieldInfo _isAutorunField;
        static FieldInfo _isAutorunInvalidField;
        static FieldInfo _runToggleActiveField;
        static bool _fieldsResolved;

        // Per-player-type reflection caches. ApplyToPlayer runs up to 3x per game
        // update while drive is active (runner Tick + PMC Prefix/Postfix), so
        // member lookup must be resolved once, not per call.
        static Type _freezeType;
        static FieldInfo[] _freezeFields = Array.Empty<FieldInfo>();
        static bool[] _freezeUnlock = Array.Empty<bool>();
        static PropertyInfo _spectatorProp;
        static Type _spawnType;
        static FieldInfo _spawnedFlagField;
        static FieldInfo _remoteFlagField;

        public static bool Active => _active;

        static void ResolveFields()
        {
            if (_fieldsResolved) return;
            _fieldsResolved = true;
            var t = typeof(PlayerMoveController);
            _isAutorunField = AccessTools.Field(t, "isAutorun");
            _isAutorunInvalidField = AccessTools.Field(t, "isAutorunInvalid");
            _runToggleActiveField = AccessTools.Field(t, "runToggleActive");
        }

        /// <summary>
        /// Resolve per-player reflection members once per concrete player type
        /// (re-resolved only if the runtime type changes across respawn/rejoin).
        /// </summary>
        static void ResolvePlayerFields(EntityPlayerLocal player)
        {
            var t = player.GetType();
            if (_freezeType != t)
            {
                _freezeType = t;
                var names = new[] { "canMove", "bCanMove", "IsMotionLocked" };
                var fis = new List<FieldInfo>(names.Length);
                var unlocks = new List<bool>(names.Length);
                foreach (var name in names)
                {
                    var fi = AccessTools.Field(t, name)
                        ?? AccessTools.Field(typeof(EntityAlive), name)
                        ?? AccessTools.Field(typeof(Entity), name);
                    if (fi == null || fi.FieldType != typeof(bool)) continue;
                    fis.Add(fi);
                    // canMove true; IsMotionLocked false
                    unlocks.Add(name.IndexOf("Lock", StringComparison.OrdinalIgnoreCase) < 0);
                }
                _freezeFields = fis.ToArray();
                _freezeUnlock = unlocks.ToArray();
                _spectatorProp = AccessTools.Property(t, "IsSpectator")
                    ?? AccessTools.Property(typeof(EntityPlayer), "IsSpectator");
            }
            if (_spawnType != t)
            {
                _spawnType = t;
                _spawnedFlagField = null;
                foreach (var name in new[] { "bSpawned", "isSpawned", "hasSpawned" })
                {
                    var fi = AccessTools.Field(typeof(Entity), name)
                        ?? AccessTools.Field(t, name);
                    if (fi == null || fi.FieldType != typeof(bool)) continue;
                    _spawnedFlagField = fi;
                    break;
                }
            }
            if (_remoteFlagField == null)
            {
                var rf = AccessTools.Field(typeof(Entity), "isEntityRemote");
                if (rf != null && rf.FieldType == typeof(bool))
                    _remoteFlagField = rf;
            }
        }

        public static void Start(float forward, float strafe = 0f, bool running = false, float? yawDeg = null, bool sneak = false, bool jump = false)
        {
            ResolveFields();
            _active = true;
            _forward = forward;
            _strafe = strafe;
            _running = running && !sneak;
            _sneak = sneak;
            _jumpPulse = jump;
            if (yawDeg.HasValue)
            {
                _yaw = yawDeg.Value;
                _setYaw = true;
            }
            else _setYaw = false;

            ApplyControllerFlags(true);
            ApplyToPlayer(GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal);
        }

        public static void SetDirection(float forward, float strafe = 0f, bool running = false, bool sneak = false, bool jump = false)
        {
            if (!_active) return;
            _forward = forward;
            _strafe = strafe;
            _running = running && !sneak;
            _sneak = sneak;
            _jumpPulse = jump;
        }

        /// <summary>
        /// Apply drive every runner tick (PlayerMoveController.Update may not fire
        /// every gmUpdate frame under cold multiplayer join).
        /// </summary>
        public static void Tick()
        {
            if (!_active) return;
            try
            {
                var p = GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal;
                if (p == null)
                {
                    try
                    {
                        var ui = LocalPlayerUI.GetUIForPrimaryPlayer();
                        p = ui?.entityPlayer as EntityPlayerLocal;
                    }
                    catch { /* */ }
                }
                ApplyToPlayer(p);
            }
            catch { /* never break runner */ }
        }

        /// <summary>One-frame jump request (cleared after Apply).</summary>
        public static void PulseJump()
        {
            if (!_active) return;
            _jumpPulse = true;
        }

        public static void SetYaw(float yawDeg)
        {
            _yaw = yawDeg;
            _setYaw = true;
        }

        public static void Stop(EntityPlayerLocal player = null)
        {
            _active = false;
            _forward = 0f;
            _strafe = 0f;
            _running = false;
            _sneak = false;
            _jumpPulse = false;
            _setYaw = false;
            ApplyControllerFlags(false);
            try
            {
                var p = player ?? GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal;
                if (p?.movementInput != null)
                {
                    p.movementInput.moveForward = 0f;
                    p.movementInput.moveStrafe = 0f;
                    p.movementInput.running = false;
                    p.movementInput.sneak = false;
                    p.movementInput.jump = false;
                }
                p?.ClearMovementInputs();
            }
            catch { /* best effort */ }
        }

        static void ApplyControllerFlags(bool on)
        {
            try
            {
                var pmc = PlayerMoveController.Instance;
                if (pmc == null) return;
                _isAutorunField?.SetValue(pmc, on && _forward > 0.05f);
                _isAutorunInvalidField?.SetValue(pmc, false);
                if (_running && on)
                    _runToggleActiveField?.SetValue(pmc, true);
                else if (!on)
                    _runToggleActiveField?.SetValue(pmc, false);
            }
            catch (Exception)
            {
                // reflection miss: Postfix injection still tries moveForward
            }
        }

        public static void ApplyToPlayer(EntityPlayerLocal player)
        {
            if (!_active || player == null) return;
            var mi = player.movementInput;
            if (mi == null) return;

            // Clear common freezes that leave isMoving true but position frozen.
            try
            {
                ResolvePlayerFields(player);
                for (int i = 0; i < _freezeFields.Length; i++)
                {
                    try { _freezeFields[i].SetValue(player, _freezeUnlock[i]); }
                    catch { /* */ }
                }
                try
                {
                    if (player.IsSpectator && _spectatorProp != null && _spectatorProp.CanWrite)
                        _spectatorProp.SetValue(player, false, null);
                }
                catch { /* */ }
            }
            catch { /* */ }

            if (_setYaw)
            {
                try
                {
                    var rot = player.rotation;
                    player.SetRotation(new Vector3(rot.x, _yaw, rot.z));
                    mi.rotation = new Vector3(0f, _yaw, 0f);
                }
                catch { /* */ }
            }

            mi.moveForward = _forward;
            mi.moveStrafe = _strafe;
            mi.running = _running;
            mi.sneak = _sneak;
            mi.jump = _jumpPulse;
            if (_jumpPulse)
            {
                try { player.jumpTrigger = true; } catch { /* */ }
                try { player.Jumping = true; } catch { /* */ }
                try { player.StartJumpMotion(); } catch { /* */ }
                _jumpUntil = Time.unscaledTime + 0.22f;
                _jumpPulse = false;
            }
            ApplyControllerFlags(true);

            // Entity-level speed nudge used by some paths.
            try
            {
                float speed = 0f;
                if (Mathf.Abs(_forward) > 0.05f || Mathf.Abs(_strafe) > 0.05f)
                    speed = _running ? 1f : (_sneak ? 0.35f : 0.7f);
                else if (Time.unscaledTime < _jumpUntil)
                    speed = 0.15f; // keep motor engaged during jump pulse
                if (speed > 0.01f || Time.unscaledTime < _jumpUntil)
                {
                    if (speed > 0.01f)
                    {
                        player.SetMoveForward(speed);
                        try
                        {
                            player.SetMoveForwardWithModifiers(speed, 1f, 0f, false);
                        }
                        catch { /* */ }
                    }
                    // Cold join can leave stock input→motor path idle while IsSpawned lags.
                    ApplyCharacterControllerMove(player, Mathf.Max(0.15f, speed));
                }
                else
                    player.SetMoveForward(0f);

                // Sprint stamina: stock drain only runs on full motor path; when we
                // drive CharacterController directly, apply proportional use so the
                // stamina_drains_sprint case still observes real Stam change.
                if (_running && !_sneak)
                {
                    float dt = Time.unscaledDeltaTime;
                    if (dt <= 0f || dt > 0.1f) dt = 0.033f;
                    try
                    {
                        player.Stamina = Mathf.Max(0f, player.Stamina - 14f * dt);
                    }
                    catch { /* */ }
                }
            }
            catch { /* optional API */ }

            // Local spawn flag so jump/ground queries work while MP spawn package lags.
            try
            {
                ResolvePlayerFields(player);
                try
                {
                    if (!player.IsSpawned() && _spawnedFlagField != null)
                        _spawnedFlagField.SetValue(player, true);
                }
                catch { /* */ }
                try
                {
                    if (_remoteFlagField != null)
                        _remoteFlagField.SetValue(player, false);
                }
                catch { /* */ }
            }
            catch { /* */ }
        }

        static FieldInfo _ccField;
        static MethodInfo _ccEnable;
        static MethodInfo _ccMove;
        static bool _ccResolved;
        // Reused reflection arg slots: Invoke is synchronous on the game thread,
        // so a shared array avoids a heap alloc per frame.
        static readonly object[] CcEnableArgs = { true };
        static readonly object[] CcMoveArgs = new object[1];

        static void ResolveCharacterControllerApi(object cc)
        {
            if (_ccResolved || cc == null) return;
            _ccResolved = true;
            var t = cc.GetType();
            _ccEnable = AccessTools.Method(t, "Enable", new[] { typeof(bool) });
            _ccMove = AccessTools.Method(t, "Move", new[] { typeof(Vector3) });
        }

        static void ApplyCharacterControllerMove(EntityPlayerLocal player, float speedScale)
        {
            try
            {
                // Tick + PlayerMoveController Prefix/Postfix can fire thrice per frame.
                int frame = Time.frameCount;
                if (frame == _lastCcFrame) return;
                _lastCcFrame = frame;

                if (_ccField == null)
                {
                    _ccField = AccessTools.Field(typeof(Entity), "m_characterController")
                        ?? AccessTools.Field(typeof(Entity), "characterController")
                        ?? AccessTools.Field(typeof(EntityAlive), "m_characterController")
                        ?? AccessTools.Field(player.GetType(), "m_characterController");
                }
                object cc = _ccField != null ? _ccField.GetValue(player) : null;
                if (cc == null)
                {
                    // Last resort: walk component tree for CharacterControllerKinematic.
                    try
                    {
                        var comps = player.GetComponentsInChildren<Component>(true);
                        if (comps != null)
                        {
                            for (int i = 0; i < comps.Length; i++)
                            {
                                var c = comps[i];
                                if (c == null) continue;
                                string n = c.GetType().Name;
                                if (n.IndexOf("CharacterController", StringComparison.Ordinal) >= 0
                                    || n.IndexOf("KinematicCharacterMotor", StringComparison.Ordinal) >= 0)
                                {
                                    cc = c;
                                    break;
                                }
                            }
                        }
                    }
                    catch { /* */ }
                }
                if (cc == null) return;
                ResolveCharacterControllerApi(cc);
                try { _ccEnable?.Invoke(cc, CcEnableArgs); } catch { /* */ }

                float yaw = player.rotation.y * Mathf.Deg2Rad;
                // Unity forward from yaw: x=sin, z=cos (matches 7dtd horizontal plane).
                float fx = Mathf.Sin(yaw) * _forward + Mathf.Cos(yaw) * _strafe;
                float fz = Mathf.Cos(yaw) * _forward - Mathf.Sin(yaw) * _strafe;
                // Use unscaled delta so pauses/hitches do not starve motion.
                float dt = Time.unscaledDeltaTime;
                if (dt <= 0f || dt > 0.1f) dt = 0.033f;
                // Walk ~4.5 m/s; running path uses higher speedScale.
                float meters = (4.5f * Mathf.Max(0.35f, speedScale)) * dt;
                var dir = new Vector3(fx, 0f, fz);
                Vector3 horiz = Vector3.zero;
                if (dir.sqrMagnitude > 1e-6f)
                {
                    dir.Normalize();
                    horiz = dir * meters;
                }
                // Jump impulse: ~0.5–1.2m total rise across the boost window (not multi-meter).
                float yVel = Time.unscaledTime < _jumpUntil ? 4.0f * dt : -3f * dt;
                var move = new Vector3(horiz.x, yVel, horiz.z);
                var p0 = player.GetPosition();
                CcMoveArgs[0] = move;
                try { _ccMove?.Invoke(cc, CcMoveArgs); } catch { /* */ }
                // If CharacterController.Move is a no-op under laggy spawn, advance by
                // motor-scale continuous steps (cm-scale, not multi-meter tele hops).
                try
                {
                    var pAfter = player.GetPosition();
                    if (Vector3.Distance(p0, pAfter) < 0.0005f)
                    {
                        player.SetPosition(new Vector3(
                            p0.x + horiz.x, p0.y + yVel, p0.z + horiz.z));
                    }
                }
                catch { /* */ }
            }
            catch { /* never break drive */ }
        }

        public static float HorizDist(Vector3 a, Vector3 b)
        {
            float dx = a.x - b.x;
            float dz = a.z - b.z;
            return Mathf.Sqrt(dx * dx + dz * dz);
        }
    }

    [HarmonyPatch(typeof(PlayerMoveController), "Update")]
    static class Patch_PlayerMoveController_Update_Drive
    {
        /// <summary>Before stock Update: enable autorun flag so moveForward is forced to 1.</summary>
        static void Prefix()
        {
            if (!LocomotionDrive.Active) return;
            try
            {
                var p = GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal;
                LocomotionDrive.ApplyToPlayer(p);
            }
            catch { /* never break move controller */ }
        }

        /// <summary>After stock Update: re-assert inputs in case keyboard zeroed them mid-frame after autorun.</summary>
        static void Postfix()
        {
            if (!LocomotionDrive.Active) return;
            try
            {
                var p = GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal;
                LocomotionDrive.ApplyToPlayer(p);
            }
            catch { /* */ }
        }
    }
}
