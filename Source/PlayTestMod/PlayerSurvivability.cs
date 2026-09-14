using System;
using System.Collections.Generic;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Spawn-screen and corpse recovery for a scenario player.
    ///
    /// A client that joins onto a character who died in an earlier run opens
    /// <c>XUiC_SpawnSelectionWindow</c> and sits on "Respawn Now": the harness
    /// has no input driver, nobody clicks it, and every later
    /// <see cref="PlayerGate.LivePlayer"/> case burns its timeout against a
    /// player who was never in the world. Pressing
    /// <c>XUiC_SpawnSelectionWindow.SpawnButtonPressed</c> is the same public
    /// method the button's own OnPressed handler calls. Nothing is pressed
    /// unless that window is open.
    ///
    /// <see cref="EntityPlayer.Respawn"/> / <see cref="EntityAlive.SetAlive"/>
    /// alone do not close that window. The runner uses this helper for
    /// LivePlayer recovery; <see cref="PlayerGate.AllowDead"/>,
    /// <see cref="PlayerGate.WorldOnly"/>, and <see cref="CaseDef.NoAutoHeal"/>
    /// stay on the per-case gate.
    /// </summary>
    public static class PlayerSurvivability
    {
        /// <summary>
        /// How long a provider <see cref="AddSurvivabilityGuard"/> case will
        /// keep pressing spawn before it fails. Generous enough for a chunk
        /// load on a cold save, short enough that a wedged world costs one
        /// case rather than every later timeout.
        /// </summary>
        public const float GuardTimeoutSeconds = 60f;

        const float PressIntervalSeconds = 2f;

        static float lastPress;
        static int presses;
        static string pressDetail = "";

        /// <summary>
        /// Queue a setup case that waits until the player is spawned, alive,
        /// and in God Mode with fly/noclip off. Optional: providers that want
        /// an explicit first case. The runner still recovers LivePlayer
        /// without this prepend.
        /// </summary>
        public static void AddSurvivabilityGuard(List<CaseDef> queue, string label)
        {
            if (queue == null) throw new ArgumentNullException(nameof(queue));
            queue.Add(CaseDef.Live(label, "player_survivable", new[] { "setup", "survivability" },
                ctx => Begin(ctx),
                wait: ctx => Wait(ctx),
                assert: ctx => Assert(ctx),
                timeout: GuardTimeoutSeconds,
                fail: "the scenario player was not alive, spawned and in God Mode within "
                    + "the guard window, so every later case would have been measuring a corpse "
                    + "or a spawn screen instead of the thing it tests"));
        }

        public static void Begin(CaseCtx ctx)
        {
            lastPress = 0f;
            presses = 0;
            pressDetail = "";
            if (ctx != null)
            {
                ctx.IntA = 0;
                ctx.Detail = "waiting for a spawned, living player";
            }
            TryPressSpawn(ctx);
        }

        public static bool Wait(CaseCtx ctx)
        {
            var player = ResolvePlayer(ctx);
            if (player != null && player.Spawned && !player.IsDead())
            {
                bool god = Ensure(player, fly: false, out string godDetail);
                if (ctx != null)
                {
                    ctx.IntA = god ? 1 : 0;
                    ctx.Detail = godDetail + pressDetail;
                }
                return true;
            }

            if (Time.unscaledTime - lastPress >= PressIntervalSeconds)
                TryPressSpawn(ctx);
            if (ctx != null)
            {
                ctx.Detail = "player spawned=" + (player != null && player.Spawned)
                    + " dead=" + (player != null && player.IsDead()) + pressDetail;
            }
            return false;
        }

        public static bool Assert(CaseCtx ctx)
        {
            return ctx != null && ctx.IntA == 1;
        }

        /// <summary>
        /// Press the spawn-selection button if that window is open.
        /// Returns true only when <c>SpawnButtonPressed</c> ran. Returns
        /// false without pressing when the window is closed, missing, or
        /// there is no player UI.
        /// </summary>
        public static bool TryPressSpawn(CaseCtx ctx)
        {
            lastPress = Time.unscaledTime;
            try
            {
                var player = ResolvePlayer(ctx);
                var ui = player != null ? player.PlayerUI : null;
                if (ui == null || ui.windowManager == null || ui.xui == null)
                {
                    pressDetail = " spawnPress=no-ui";
                    return false;
                }
                string id = XUiC_SpawnSelectionWindow.ID;
                if (string.IsNullOrEmpty(id) || !ui.windowManager.IsWindowOpen(id))
                {
                    pressDetail = " spawnPress=window-closed presses=" + presses;
                    return false;
                }
                var window = ui.xui.GetChildByType<XUiC_SpawnSelectionWindow>();
                if (window == null)
                {
                    pressDetail = " spawnPress=no-controller";
                    return false;
                }
                SpawnMethod method = window.bEnteringGame
                    ? SpawnMethod.Invalid
                    : (GameStats.GetInt(EnumGameStats.DeathPenalty) == 3
                        ? SpawnMethod.NewRandomSpawn
                        : SpawnMethod.NearDeath);
                window.SpawnButtonPressed(method);
                presses++;
                pressDetail = " spawnPress=" + method + " presses=" + presses
                    + " entering=" + window.bEnteringGame;
                return true;
            }
            catch (Exception ex)
            {
                pressDetail = " spawnPress=error " + ex.Message;
                return false;
            }
        }

        /// <summary>
        /// True when the spawn-selection window is actually open. Used by the
        /// runner so a LivePlayer case does not start against that screen.
        /// </summary>
        public static bool SpawnWindowOpen(EntityPlayerLocal player)
        {
            try
            {
                var ui = player != null ? player.PlayerUI : null;
                if (ui == null || ui.windowManager == null) return false;
                string id = XUiC_SpawnSelectionWindow.ID;
                return !string.IsNullOrEmpty(id) && ui.windowManager.IsWindowOpen(id);
            }
            catch
            {
                return false;
            }
        }

        /// <summary>
        /// God Mode as an explicit set of three flags rather than a toggle.
        /// Fly and no-clip follow <paramref name="fly"/> (default off):
        /// <c>PlayerMoveController.toggleGodMode</c> turns both on as a side
        /// effect, and this writes them back so a caller that did not ask
        /// for fly does not keep them.
        /// </summary>
        public static bool Ensure(EntityPlayerLocal player, bool fly, out string detail)
        {
            if (player == null)
            {
                detail = "godmode=no-player";
                return false;
            }
            try
            {
                if (!GamePrefs.GetBool(EnumGamePrefs.DebugMenuEnabled))
                    new ConsoleCmdDebugMenu().Execute(new List<string>(), default(CommandSenderInfo));

                if (!player.IsGodMode.Value)
                {
                    PlayerMoveController controller = UnityEngine.Object.FindAnyObjectByType<PlayerMoveController>();
                    if (controller == null || controller.toggleGodMode == null)
                    {
                        detail = "godmode=no-player-move-controller";
                        return false;
                    }
                    controller.toggleGodMode();
                }
                player.IsFlyMode.Value = fly;
                player.IsNoCollisionMode.Value = fly;
                player.bEntityAliveFlagsChanged = true;

                bool god = player.IsGodMode.Value;
                detail = "godmode=" + god
                    + " debugmenu=" + GamePrefs.GetBool(EnumGamePrefs.DebugMenuEnabled)
                    + " fly=" + player.IsFlyMode.Value
                    + " noclip=" + player.IsNoCollisionMode.Value
                    + " health=" + player.Health;
                return god;
            }
            catch (Exception ex)
            {
                detail = "godmode=error " + ex.Message;
                return false;
            }
        }

        static EntityPlayerLocal ResolvePlayer(CaseCtx ctx)
        {
            if (ctx != null && ctx.Player != null)
                return ctx.Player;
            try
            {
                return GameManager.Instance?.World?.GetPrimaryPlayer() as EntityPlayerLocal;
            }
            catch
            {
                return null;
            }
        }
    }
}
