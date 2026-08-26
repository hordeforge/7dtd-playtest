namespace ZdtdPlaytest
{
    /// <summary>
    /// Assembly identity shared by bootstrap (<see cref="ModApi"/>) and the
    /// runner. Lives below both so the engine never references the entry
    /// point just to log its own version.
    /// </summary>
    public static class ModIdentity
    {
        public const string Name = "7dtd-playtest";
        public const string HarmonyId = "com.zdtd.playtest";
        public const string Version = "0.8.0";
    }
}
