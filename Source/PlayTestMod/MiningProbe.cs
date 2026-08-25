using System;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Provider-neutral request for one real mining attempt: seed a named
    /// block, equip a named tool, swing only through the held-item primary
    /// path, and observe both block damage and a named inventory award.
    /// </summary>
    public sealed class MiningSpec
    {
        public string BlockName;
        public string ToolName;
        public string AwardItemName;
        public Vector3i TargetOffset;
        public float TimeoutSeconds;
        public int MaxAttempts;
        public float Standoff;
        public float CooldownSeconds;

        public MiningSpec()
        {
            BlockName = "terrOreIron";
            ToolName = "meleeToolPickT1IronPickaxe";
            AwardItemName = "resourceScrapIron";
            TargetOffset = new Vector3i(0, 1, 1);
            TimeoutSeconds = 20f;
            MaxAttempts = 8;
            Standoff = 1.2f;
            CooldownSeconds = 0.45f;
        }

        /// <summary>Stock iron ore + iron pickaxe + scrap iron award.</summary>
        public static MiningSpec StockIron()
        {
            return new MiningSpec();
        }
    }

    public enum MiningPhase
    {
        Setup,
        WaitSeed,
        Ready,
        Pressing,
        Cooldown,
        Passed,
        Failed,
    }

    /// <summary>
    /// Observable mining outcome. Providers read this; they do not stash
    /// mining scratch on <see cref="CaseCtx"/>.
    /// </summary>
    public sealed class MiningResult
    {
        public Vector3i Target;
        public string BlockName;
        public string ToolName;
        public string AwardItemName;
        public int BlockType;
        public int ToolType;
        public int AwardType;
        public int InitialBlockType;
        public int CurrentBlockType;
        public int InitialDamage;
        public int CurrentDamage;
        public int InitialAwardCount;
        public int CurrentAwardCount;
        public int AcceptedPresses;
        public int CompletedAttempts;
        public MiningPhase Phase;
        public string Detail;

        /// <summary>
        /// Block evidence changed since the seeded baseline: type flipped,
        /// block removed, or damage rose. Providers branch on this instead of
        /// re-deriving the comparison from raw ints.
        /// </summary>
        public bool Damaged
        {
            get
            {
                return CurrentBlockType != InitialBlockType
                    || CurrentBlockType == 0
                    || CurrentDamage > InitialDamage;
            }
        }

        /// <summary>The named award count rose above the seeded baseline.</summary>
        public bool Awarded
        {
            get { return CurrentAwardCount > InitialAwardCount; }
        }

        /// <summary>
        /// Both observations at once: the canonical harvest verdict the probe's
        /// assert uses. False until the seed baseline is observed.
        /// </summary>
        public bool Harvested
        {
            get { return Damaged && Awarded; }
        }
    }

    /// <summary>
    /// Stateful real-mining driver for <see cref="CaseDef.Live"/>. Setup may
    /// mutate the fixture; the attack phase must not. Pass
    /// <see cref="Act"/> / <see cref="Wait"/> / <see cref="Assert"/> straight
    /// to the case factory.
    /// </summary>
    public sealed class MiningProbe
    {
        readonly MiningSpec _spec;
        readonly MiningResult _result = new MiningResult();
        BlockValue _savedBlock;
        bool _haveSavedBlock;
        float _phaseStart;
        bool _seeded;

        public MiningProbe(MiningSpec spec)
        {
            _spec = spec ?? new MiningSpec();
            _result.BlockName = _spec.BlockName;
            _result.ToolName = _spec.ToolName;
            _result.AwardItemName = _spec.AwardItemName;
            _result.Phase = MiningPhase.Setup;
            _result.Detail = "idle";
        }

        public MiningResult Result
        {
            get { return _result; }
        }

        public void Act(CaseCtx ctx)
        {
            Setup(ctx);
        }

        public bool Wait(CaseCtx ctx)
        {
            if (_result.Phase == MiningPhase.Failed || _result.Phase == MiningPhase.Passed)
                return true;
            if (_result.Phase == MiningPhase.Setup || _result.Phase == MiningPhase.WaitSeed)
                return TickSeed(ctx);
            return TickAttack(ctx);
        }

        public bool Assert(CaseCtx ctx)
        {
            Restore(ctx);
            if (ctx != null)
                ctx.Detail = _result.Detail;
            if (_result.Phase == MiningPhase.Passed)
                return true;
            if (_result.Harvested)
            {
                _result.Phase = MiningPhase.Passed;
                _result.Detail = Describe("pass");
                if (ctx != null) ctx.Detail = _result.Detail;
                return true;
            }
            if (string.IsNullOrEmpty(_result.Detail) || _result.Detail == "idle")
                _result.Detail = Describe("missing damage or award");
            if (ctx != null) ctx.Detail = _result.Detail;
            return false;
        }

        void Setup(CaseCtx ctx)
        {
            _result.Phase = MiningPhase.Setup;
            if (ctx == null || ctx.Player == null || ctx.World == null)
            {
                Fail("no player or world");
                return;
            }

            Helpers.TryCloseWindows();
            Helpers.SnapPlayerToSurface(ctx.Player, ctx.World);

            if (!TryResolveBlock(out var seed, out string blockErr))
            {
                Fail(blockErr);
                return;
            }
            if (!Helpers.TryGetItem(_spec.ToolName, out var toolIv) || toolIv == null || toolIv.IsEmpty())
            {
                Fail("unresolved tool: " + _spec.ToolName);
                return;
            }
            if (!Helpers.TryGetItem(_spec.AwardItemName, out var awardIv) || awardIv == null || awardIv.IsEmpty())
            {
                Fail("unresolved award: " + _spec.AwardItemName);
                return;
            }

            _result.BlockType = seed.type;
            _result.ToolType = toolIv.type;
            _result.AwardType = awardIv.type;
            _result.Target = Helpers.FixtureTarget(
                ctx.Player, ctx.World,
                _spec.TargetOffset.x, _spec.TargetOffset.y, _spec.TargetOffset.z);

            Helpers.FreeBagSlots(ctx.Player, 2);
            var given = toolIv.Clone();
            try { given.UseTimes = 0f; } catch { /* ItemValue shape */ }
            if (!Helpers.TryGiveItem(ctx.Player, new ItemStack(given, 1)))
            {
                Fail("could not give tool: " + _spec.ToolName);
                return;
            }
            if (Helpers.TryEquipItemType(ctx.Player, toolIv.type) < 0)
            {
                Fail("could not equip tool: " + _spec.ToolName);
                return;
            }
            Helpers.PushPlayerInventory(ctx.Player, out _);

            try
            {
                _savedBlock = ctx.World.GetBlock(_result.Target);
                _haveSavedBlock = true;
            }
            catch { _haveSavedBlock = false; }
            Helpers.SetBlockRpc(ctx.World, _result.Target, seed);
            _seeded = true;
            _result.Phase = MiningPhase.WaitSeed;
            _phaseStart = Time.unscaledTime;
            _result.Detail = "seeded " + _spec.BlockName + " at " + _result.Target;
        }

        bool TickSeed(CaseCtx ctx)
        {
            if (ctx == null || ctx.Player == null || ctx.World == null)
            {
                Fail("no player or world");
                return true;
            }
            var b = ctx.World.GetBlock(_result.Target);
            bool held = HeldToolReady(ctx.Player);
            if (b.type == _result.BlockType && b.type != 0 && held)
            {
                _result.InitialBlockType = b.type;
                _result.CurrentBlockType = b.type;
                _result.InitialDamage = b.damage;
                _result.CurrentDamage = b.damage;
                _result.InitialAwardCount = Helpers.CountItemType(ctx.Player, _result.AwardType);
                _result.CurrentAwardCount = _result.InitialAwardCount;
                _result.Phase = MiningPhase.Ready;
                _phaseStart = Time.unscaledTime;
                _result.Detail = "ready dmg0=" + _result.InitialDamage
                    + " award0=" + _result.InitialAwardCount;
                return false;
            }
            if (Time.unscaledTime - _phaseStart > 4f)
            {
                if (_seeded && b.type != _result.BlockType)
                {
                    Fail("seed replication: want=" + _result.BlockType + " now=" + b.type);
                    return true;
                }
                if (!held)
                {
                    Fail("held-item readiness: tool type " + _result.ToolType);
                    return true;
                }
            }
            _result.Detail = "wait seed type=" + b.type + " held=" + held;
            return false;
        }

        /// <summary>
        /// Attack-phase tick. Must not write blocks, simulate hits, or give
        /// inventory: the swing is <see cref="PressPrimary"/> then a later
        /// <see cref="ReleasePrimary"/>.
        /// </summary>
        public bool TickAttack(CaseCtx ctx)
        {
            if (ctx == null || ctx.Player == null || ctx.World == null)
            {
                Fail("no player or world");
                return true;
            }

            Observe(ctx);
            if (_result.Harvested)
            {
                _result.Phase = MiningPhase.Passed;
                _result.Detail = Describe("pass");
                return true;
            }

            float elapsed = Time.unscaledTime - ctx.CaseStartUnscaled;
            if (elapsed > _spec.TimeoutSeconds)
            {
                Fail(TimeoutGate());
                return true;
            }
            if (_result.CompletedAttempts >= _spec.MaxAttempts)
            {
                Fail(TimeoutGate());
                return true;
            }

            AimAtTarget(ctx.Player);

            if (_result.Phase == MiningPhase.Ready)
            {
                if (!PressPrimary(ctx.Player))
                {
                    _result.Detail = "rejected press";
                    _result.CompletedAttempts++;
                    _result.Phase = MiningPhase.Cooldown;
                    _phaseStart = Time.unscaledTime;
                    return false;
                }
                _result.AcceptedPresses++;
                _result.Phase = MiningPhase.Pressing;
                _phaseStart = Time.unscaledTime;
                _result.Detail = "pressing attempt=" + _result.AcceptedPresses;
                return false;
            }

            if (_result.Phase == MiningPhase.Pressing)
            {
                if (BlockChanged() || SwingFinished(ctx.Player) || Time.unscaledTime - _phaseStart > 2.5f)
                {
                    ReleasePrimary(ctx.Player);
                    _result.CompletedAttempts++;
                    _result.Phase = MiningPhase.Cooldown;
                    _phaseStart = Time.unscaledTime;
                    _result.Detail = "released attempt=" + _result.CompletedAttempts;
                }
                return false;
            }

            if (_result.Phase == MiningPhase.Cooldown)
            {
                if (Time.unscaledTime - _phaseStart >= _spec.CooldownSeconds
                    && SwingFinished(ctx.Player))
                {
                    _result.Phase = MiningPhase.Ready;
                    _phaseStart = Time.unscaledTime;
                }
            }
            return false;
        }

        /// <summary>
        /// One canonical primary press: <c>UseHoldingItem(0, false)</c> only.
        /// </summary>
        public bool PressPrimary(EntityPlayerLocal player)
        {
            if (player == null) return false;
            try
            {
                player.UseHoldingItem(0, false);
                return true;
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// One canonical primary release, after the swing has landed or finished.
        /// </summary>
        public void ReleasePrimary(EntityPlayerLocal player)
        {
            if (player == null) return;
            try { player.UseHoldingItem(0, true); }
            catch { /* */ }
        }

        void AimAtTarget(EntityPlayerLocal player)
        {
            try
            {
                var center = _result.Target.ToVector3Center();
                Helpers.LookAt(player, center);
                var pos = player.GetPosition();
                var flat = new Vector3(center.x - pos.x, 0f, center.z - pos.z);
                float dist = flat.magnitude;
                if (dist > 0.05f)
                {
                    var dir = flat / dist;
                    player.SetPosition(new Vector3(
                        center.x - dir.x * _spec.Standoff,
                        pos.y,
                        center.z - dir.z * _spec.Standoff));
                    Helpers.LookAt(player, center);
                }
            }
            catch { /* best-effort aim */ }
        }

        void Observe(CaseCtx ctx)
        {
            try
            {
                var b = ctx.World.GetBlock(_result.Target);
                _result.CurrentBlockType = b.type;
                _result.CurrentDamage = b.damage;
            }
            catch { /* */ }
            _result.CurrentAwardCount = Helpers.CountItemType(ctx.Player, _result.AwardType);
        }

        bool BlockChanged()
        {
            return _result.CurrentBlockType == 0
                || _result.CurrentBlockType != _result.InitialBlockType;
        }

        static bool SwingFinished(EntityPlayerLocal player)
        {
            try
            {
                return !player.IsHoldingItemInUse(0);
            }
            catch
            {
                return true;
            }
        }

        static bool HeldToolReady(EntityPlayerLocal player)
        {
            try
            {
                var inv = player.inventory;
                if (inv == null) return false;
                return inv.holdingItemItemValue.type > 0;
            }
            catch
            {
                return false;
            }
        }

        bool TryResolveBlock(out BlockValue seed, out string error)
        {
            seed = BlockValue.Air;
            error = "";
            try
            {
                seed = Block.GetBlockValue(_spec.BlockName, true);
            }
            catch (Exception ex)
            {
                error = "unresolved block: " + _spec.BlockName + " (" + ex.Message + ")";
                return false;
            }
            if (seed.isair || seed.type == 0)
            {
                error = "unresolved block: " + _spec.BlockName;
                return false;
            }
            return true;
        }

        string TimeoutGate()
        {
            if (_result.AcceptedPresses == 0)
                return "rejected press";
            if (!BlockChanged() && _result.CurrentDamage <= _result.InitialDamage)
                return "raycast/block-damage miss dmg0=" + _result.InitialDamage
                    + " dmg=" + _result.CurrentDamage;
            if (_result.CurrentAwardCount <= _result.InitialAwardCount)
                return "missing inventory award want=" + _spec.AwardItemName
                    + " have=" + _result.CurrentAwardCount
                    + " base=" + _result.InitialAwardCount;
            return Describe("timeout");
        }

        string Describe(string gate)
        {
            return gate
                + " block=" + _spec.BlockName
                + " tool=" + _spec.ToolName
                + " award=" + _spec.AwardItemName
                + " type0=" + _result.InitialBlockType
                + " now=" + _result.CurrentBlockType
                + " dmg0=" + _result.InitialDamage
                + " dmg=" + _result.CurrentDamage
                + " award0=" + _result.InitialAwardCount
                + " award=" + _result.CurrentAwardCount
                + " presses=" + _result.AcceptedPresses
                + " attempts=" + _result.CompletedAttempts
                + " phase=" + _result.Phase;
        }

        void Fail(string detail)
        {
            _result.Phase = MiningPhase.Failed;
            _result.Detail = detail;
        }

        void Restore(CaseCtx ctx)
        {
            if (!_haveSavedBlock || ctx == null || ctx.World == null) return;
            try { Helpers.SetBlockRpc(ctx.World, _result.Target, _savedBlock); }
            catch { /* best-effort cleanup */ }
        }
    }
}
