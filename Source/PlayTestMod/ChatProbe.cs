using System;
using System.Collections.Generic;
using HarmonyLib;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Captures inbound chat for chat_roundtrip (stock ChatMessageClient path).
    /// </summary>
    static class ChatProbe
    {
        static readonly object Gate = new object();
        static readonly List<string> Recent = new List<string>(32);
        // The captured text stays private: it is whatever a remote LAN player
        // typed, and a case detail reaches the run log, the JUnit report and
        // the report JSON, all of which leave the machine for CI and for the
        // vision-review upload. Only LastLength is public, so a diagnostic can
        // say how much arrived without carrying its content anywhere.
        static string Last = "";

        /// <summary>Length of the last captured message; its text is never exposed.</summary>
        /// <remarks>
        /// Counted in code points, not <see cref="string.Length"/>'s UTF-16 code
        /// units: a C# string counts one emoji as two, so the number a reader
        /// took from <c>chat_len=4</c> would not be the number of characters
        /// that arrived. No verdict reads this; it is the size of a message
        /// nobody is allowed to see.
        /// </remarks>
        public static int LastLength
        {
            get
            {
                lock (Gate)
                {
                    if (string.IsNullOrEmpty(Last)) return 0;
                    int count = 0;
                    for (int i = 0; i < Last.Length; i++)
                    {
                        count++;
                        // A well-formed surrogate pair is one character held in
                        // two units; do not count the trailing half again.
                        if (char.IsHighSurrogate(Last[i]) && i + 1 < Last.Length
                            && char.IsLowSurrogate(Last[i + 1]))
                            i++;
                    }
                    return count;
                }
            }
        }

        public static void Clear()
        {
            lock (Gate)
            {
                Recent.Clear();
                Last = "";
            }
        }

        public static void Note(string msg)
        {
            if (string.IsNullOrEmpty(msg)) return;
            lock (Gate)
            {
                Last = msg;
                Recent.Add(msg);
                if (Recent.Count > 64)
                    Recent.RemoveRange(0, Recent.Count - 64);
            }
        }

        public static bool Contains(string token)
        {
            if (string.IsNullOrEmpty(token)) return false;
            lock (Gate)
            {
                // Note() mirrors Last into Recent, so one scan covers both.
                for (int i = 0; i < Recent.Count; i++)
                {
                    if (Recent[i] != null
                        && Recent[i].IndexOf(token, StringComparison.OrdinalIgnoreCase) >= 0)
                        return true;
                }
            }
            return false;
        }
    }

    [HarmonyPatch(typeof(GameManager), "ChatMessageClient")]
    static class Patch_ChatMessageClient_Probe
    {
        static void Prefix(string _msg)
        {
            try { ChatProbe.Note(_msg); } catch { /* never break chat */ }
        }
    }
}
