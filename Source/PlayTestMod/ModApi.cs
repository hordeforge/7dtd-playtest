using System;
using HarmonyLib;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Client-only automated play suite. Requires zdtd-connect (or manual join)
    /// to enter the game. Does not invent S2C world state.
    /// </summary>
    public class ModApi : IModApi
    {
        public const string HarmonyId = "com.zdtd.playtest";
        public const string Version = "0.7.1";

        public void InitMod(Mod _modInstance)
        {
            Log.Out("[zdtd-playtest] InitMod v" + Version);

            try
            {
                var harmony = new Harmony(HarmonyId);
                int ok = 0, fail = 0;
                foreach (var t in typeof(ModApi).Assembly.GetTypes())
                {
                    if (t.GetCustomAttributes(typeof(HarmonyPatch), true).Length == 0)
                        continue;
                    try
                    {
                        harmony.CreateClassProcessor(t).Patch();
                        ok++;
                    }
                    catch (Exception ex)
                    {
                        fail++;
                        Log.Warning("[zdtd-playtest] Harmony skip " + t.Name + ": " + ex.Message);
                    }
                }
                Log.Out("[zdtd-playtest] Harmony patches ok=" + ok + " fail=" + fail);
            }
            catch (Exception ex)
            {
                Log.Error("[zdtd-playtest] Harmony failed: " + ex.Message);
            }

            Runner.ArmFromEnv();
        }
    }
}
