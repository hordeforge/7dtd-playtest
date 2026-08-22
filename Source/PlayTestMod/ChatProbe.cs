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
        public static string Last = "";

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
