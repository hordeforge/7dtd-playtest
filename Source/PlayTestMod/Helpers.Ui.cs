using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace ZdtdPlaytest
{
    /// <summary>Game UI surfaces: window groups, HUD toggle, framebuffer capture.</summary>
    public static partial class Helpers
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


        /// <summary>
        /// Opens a game UI window group and reports whether it really ended up
        /// open, not whether the call was accepted.
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
        /// was accepted, <b>not</b> whether the window is on screen.
        /// <c>Open</c> queues into <c>windowsToOpen</c> and the manager drains
        /// that on a later <c>Update</c>, so nothing here can answer "is it
        /// drawn" and any method that claims to is lying. A window trace on the
        /// installed build put the game's own <c>toolbelt</c> open 1.7 s after
        /// the call that asked for it. Verify with <see cref="OpenWindowNames"/>
        /// from a later tick: a wait callback, or the hold of a staged frame.
        /// </returns>
        public static bool OpenWindowGroup(EntityPlayerLocal player, string group, bool modal = false)
        {
            if (string.IsNullOrEmpty(group)) return false;
            try
            {
                var wm = UiFor(player)?.windowManager;
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

        static LocalPlayerUI UiFor(EntityPlayerLocal player)
        {
            return player == null ? null : LocalPlayerUI.GetUIForPlayer(player);
        }


        /// <summary>
        /// Every window the manager currently has open, by id, comma-joined and
        /// sorted. The answer to "it says it opened and the frame is empty".
        ///
        /// <para>Deterministic order on purpose: this ends up in a case's
        /// Detail, and a set that reorders between runs makes two identical
        /// runs look different.</para>
        /// </summary>
        public static string OpenWindowNames(EntityPlayerLocal player)
        {
            try
            {
                var wm = UiFor(player)?.windowManager;
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
        /// whatever is in front, which has repeatedly meant *another session's*
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
            string dir = ShotsRoot();
            if (dir == null) return null;
            string safe = AssetName(name);
            string path = WriteScreenshot(dir, safe + ".png", superSize,
                "capture of " + safe + " failed");
            if (path == null) return null;
            // The line a collector greps for; the file appears a frame later.
            Log.Out("[7dtd-playtest] shot " + safe + " x" + superSize + " -> " + path);
            return path;
        }

        /// <summary>
        /// Start an on-demand in-game clip recording.
        ///
        /// <para>The on-demand twin of <see cref="CaseDef.StagedClip"/>: start
        /// recording whatever is actually happening (a worn garment walked, a
        /// VFX firing, an item used) from any case, do the thing, then call
        /// <see cref="EndClip"/>. Frames are the client's own rendering at
        /// <paramref name="superSize"/> resolution into
        /// <c>playtest-shots/clips/&lt;id&gt;/</c>, exactly like a staged clip,
        /// and <c>scripts/capture_video.sh</c> muxes them on the same
        /// <c>clip complete</c> marker.</para>
        /// </summary>
        public static void BeginClip(string id, int superSize = 2, float fps = 4f)
        {
            ClipRecorder.Begin(id, superSize, fps);
        }

        /// <summary>Stop the clip started with <see cref="BeginClip"/> and emit its completion line.</summary>
        public static void EndClip(string id)
        {
            ClipRecorder.End(id);
        }

        /// <summary>
        /// Photograph one frame of a **clip**: the same in-game framebuffer
        /// guarantee as <see cref="CaptureFrame"/>, written into a per-clip
        /// subdirectory so a multi-frame sequence never collides with the flat
        /// single-shot folder.
        /// </summary>
        /// <remarks>
        /// <para>A clip is a sampled sequence at a chosen cadence, not a
        /// real-time-rate recording: it answers "does the motion look right",
        /// not "does it feel smooth". Frames land in
        /// <c>playtest-shots/clips/&lt;clipId&gt;/frame-XXXX.png</c>; the muxing
        /// to a video is the host script's job (<c>scripts/capture_video.sh</c>),
        /// which waits for the <c>clip complete</c> marker <see cref="CaseDef.StagedClip"/>
        /// emits once the hold ends.</para>
        /// </remarks>
        public static string CaptureClipFrame(string clipId, int frameIndex, int superSize = 2)
        {
            if (string.IsNullOrEmpty(clipId)) clipId = "clip";
            string shots = ShotsRoot();
            if (shots == null) return null;
            string safe = AssetName(clipId);
            string dir = System.IO.Path.Combine(shots, "clips", safe);
            string path = WriteScreenshot(dir, string.Format("frame-{0:D4}.png", frameIndex),
                superSize, "clip frame " + safe + " " + frameIndex + " failed");
            if (path == null) return null;
            // The line a collector greps for; the file appears a frame later.
            Log.Out("[7dtd-playtest] clip frame " + safe + " " + frameIndex + " x" + superSize + " -> " + path);
            return path;
        }


        /// <summary>
        /// Empty <c>playtest-shots/clips/&lt;clipId&gt;/</c> so a new take of
        /// the same clip id starts from zero.
        /// </summary>
        /// <remarks>
        /// <para>Frames are numbered from 0000 and the completion line names the
        /// same directory every time, so re-running a clip without this reset
        /// would leave the previous take's frames in place: a shorter new take
        /// leaves stale frame-XXXX.png behind that inflate the muxer's found
        /// count and land in the contact sheet as if this take had captured
        /// them.</para>
        /// <para>Best effort like every filesystem touch here: a failure is
        /// warned about and the recording proceeds, overwriting frames in
        /// place rather than aborting the case.</para>
        /// </remarks>
        public static void ResetClipDir(string clipId)
        {
            if (string.IsNullOrEmpty(clipId)) clipId = "clip";
            string shots = ShotsRoot();
            if (shots == null) return;
            string dir = System.IO.Path.Combine(shots, "clips", AssetName(clipId));
            try
            {
                if (System.IO.Directory.Exists(dir))
                    System.IO.Directory.Delete(dir, true);
                System.IO.Directory.CreateDirectory(dir);
            }
            catch (Exception e)
            {
                Log.Warning("[7dtd-playtest] cannot reset clip dir " + dir + ": " + e.Message);
            }
        }

        // Same profile derivation the connect mod uses: Proton user directory
        // under wine, and the native home directory otherwise.
        static string ShotsRoot()
        {
            string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
            if (string.IsNullOrEmpty(profile)) return null;
            return System.IO.Path.Combine(profile, "AppData", "Roaming", "7DaysToDie", "playtest-shots");
        }

        static string WriteScreenshot(string dir, string fileName, int superSize, string failLabel)
        {
            if (superSize < 1) superSize = 1;
            try { System.IO.Directory.CreateDirectory(dir); }
            catch (Exception e)
            {
                Log.Warning("[7dtd-playtest] cannot create " + dir + ": " + e.Message);
                return null;
            }
            string path = System.IO.Path.Combine(dir, fileName);
            try { ScreenCapture.CaptureScreenshot(path, superSize); }
            catch (Exception e)
            {
                Log.Warning("[7dtd-playtest] " + failLabel + ": " + e.Message);
                return null;
            }
            return path;
        }

        /// <summary>
        /// The Windows device names, lowercase. Windows reserves them in every
        /// directory, whatever the extension or casing, and COM0 and LPT0 are
        /// ordinary file names, so the list stops at 9. A path whose final
        /// segment is one of these is the device, not a file, so
        /// <c>CreateDirectory</c> on one fails and the case photographs
        /// nothing. Only the lowercase spelling can reach a path:
        /// <see cref="AssetName"/> maps every other casing and every extension
        /// to underscores first, and the comparison itself is
        /// case-insensitive.
        /// </summary>
        static readonly string[] ReservedDeviceNames = {
            "con", "prn", "aux", "nul",
            "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
            "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
        };

        /// <summary>
        /// The one name an id becomes on disk and in the lines collectors read.
        /// </summary>
        /// <remarks>
        /// <para>The clip contract is a round trip: a collector reads the
        /// trailing directory out of the <c>clip complete</c> line and looks
        /// the frames up under it. Deriving that name separately from the
        /// directory the frames are written into (marker carrying the raw id,
        /// frames written under a sanitized one) sent the collector to a
        /// directory that was never created, and the run reported no frames
        /// for a take that recorded them.</para>
        ///
        /// <para>Normalized to NFC first. A name that arrives decomposed
        /// (<c>e</c> followed by U+0301) is the same character to a reader and
        /// to a normalizing filesystem, but a different string to every
        /// comparison here, so the same scene would occupy two directories
        /// depending on which editor typed it.</para>
        ///
        /// <para>ASCII letters, digits, <c>-</c> and <c>_</c> survive and
        /// everything else becomes <c>_</c>: no separator, drive letter or
        /// path segment can be smuggled in, and the name is byte-identical on
        /// every host that stores it. The mapping is idempotent, so applying
        /// it to an already-safe name is a no-op. It also rewrites the
        /// extension separator, so a name that would reach a device still
        /// carrying one cannot. A name that maps to nothing is
        /// <c>unnamed</c>, never empty: an empty name collapses onto the
        /// parent directory, and the collector would look there for a file
        /// that was never written.</para>
        ///
        /// <para>The client is a Windows process, and a name Windows itself
        /// refuses is not a file, so the mapping is only the first half. The
        /// safe set is lowercase-only, which is what keeps the Windows device
        /// names out: <c>AUX</c> arrives as <c>___</c> and <c>aux.png</c> as
        /// <c>aux_png</c>, neither of which names a device. A clip id that is
        /// exactly one of them in lowercase still does, so the survivor takes
        /// a <c>_</c> prefix rather than being dropped: a collector reads the
        /// directory back out of the marker line, and the marker and the
        /// directory derive it here and nowhere else. <c>CreateDirectory</c>
        /// on a device name fails, which would leave the recorded clip with no
        /// directory to look in. The comparison is
        /// case-insensitive as well, because a device name matches in any
        /// casing. The mapping is idempotent, so prefixing the device name
        /// with <c>_</c> survives a second pass.</para>
        /// </remarks>
        public static string AssetName(string name)
        {
            if (string.IsNullOrEmpty(name)) return "unnamed";
            string normalized = name.Normalize(NormalizationForm.FormC);
            var sb = new System.Text.StringBuilder(normalized.Length);
            foreach (char c in normalized)
                sb.Append((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-' ? c : '_');
            string safe = sb.ToString();
            if (safe.Length == 0) return "unnamed";
            if (Array.IndexOf(ReservedDeviceNames, safe.ToLowerInvariant()) >= 0)
                return "_" + safe;
            return safe;
        }
    }
}
