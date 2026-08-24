using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Game UI surfaces: window groups, HUD toggle, framebuffer capture.</summary>
    public static partial class Helpers
    {

    /// <summary>
    /// Shared client-side helpers for scenarios (no invented S2C).
    /// Public so external <see cref="IScenarioProvider"/> mods can reuse
    /// give/equip/vehicle helpers without reimplementing stock API glue.
    /// </summary>
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


        /// <summary>
        /// Shows or hides the whole in-game HUD.
        ///
        /// <para>For a fixture whose frames are the deliverable. Anything a
        /// person is meant to judge — a worn garment, a placed block, a
        /// detonation — is competing with the toolbelt, the compass, the stat
        /// bars and the tutorial callout, and none of those are the subject.
        /// The game already has one switch for all of it, and this is the same
        /// call <c>GameManager</c> makes during startup.</para>
        ///
        /// <para>Best effort and deliberately quiet. A fixture that cannot hide
        /// the HUD should still stage its scene: a photograph with a compass in
        /// the corner is worth having, and an exception thrown while tidying up
        /// the frame is not.</para>
        /// </summary>
        public static bool ShowHud(bool visible)
        {
            try
            {
                var manager = GameManager.Instance;
                if (manager == null || manager.nguiWindowManager == null) return false;
                manager.nguiWindowManager.Show(EnumNGUIWindow.InGameHUD, visible);
                return true;
            }
            catch
            {
                return false;
            }
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
        /// Photograph this client's own framebuffer, from inside the game.
        /// </summary>
        /// <remarks>
        /// <para>An external screen grab of a game window is unreliable and, on a
        /// host running more than one client, unsound: the window may be
        /// unfocused, occluded or not mapped, and a desktop capture photographs
        /// whatever is in front — which has repeatedly meant *another session's*
        /// client. A frame taken here is this process's own rendering, so it
        /// cannot be somebody else's run.</para>
        /// <para><paramref name="superSize"/> multiplies the resolution, which is
        /// how a staged frame becomes readable evidence rather than a thumbnail:
        /// 2 gives four times the pixels. Unity writes the file at the end of the
        /// frame, so the path is logged rather than returned open.</para>
        /// <para>Returns the path it asked Unity to write, or null when there is
        /// no home directory to write into.</para>
        /// </remarks>
        public static string CaptureFrame(string name, int superSize = 2)
        {
            if (string.IsNullOrEmpty(name)) name = "frame";
            if (superSize < 1) superSize = 1;
            // Same profile derivation the connect mod uses: resolves to the
            // Proton user directory under wine and stays valid natively.
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (string.IsNullOrEmpty(profile)) return null;
            string dir = System.IO.Path.Combine(profile, "AppData", "Roaming", "7DaysToDie", "playtest-shots");
            try { System.IO.Directory.CreateDirectory(dir); }
            catch (Exception e)
            {
                Log.Warning("[7dtd-playtest] cannot create " + dir + ": " + e.Message);
                return null;
            }
            string safe = SafeFileName(name);
            string path = System.IO.Path.Combine(dir, safe + ".png");
            try { ScreenCapture.CaptureScreenshot(path, superSize); }
            catch (Exception e)
            {
                Log.Warning("[7dtd-playtest] capture of " + safe + " failed: " + e.Message);
                return null;
            }
            // The line a collector greps for; the file appears a frame later.
            Log.Out("[7dtd-playtest] shot " + safe + " x" + superSize + " -> " + path);
            return path;
        }


        /// <summary>A file name that cannot escape its directory.</summary>
        static string SafeFileName(string name)
        {
            var sb = new System.Text.StringBuilder(name.Length);
            foreach (char c in name)
                sb.Append(char.IsLetterOrDigit(c) || c == '_' || c == '-' ? c : '_');
            return sb.ToString();
        }
    }
}
