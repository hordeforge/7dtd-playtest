namespace ZdtdPlaytest
{
    /// <summary>
    /// Shared client-side helpers for scenarios (no invented S2C).
    /// Public so external <see cref="IScenarioProvider"/> mods can reuse
    /// give/equip/vehicle helpers without reimplementing stock API glue.
    ///
    /// One static class, split across partial-class files by domain:
    /// <c>Helpers.Ui</c>, <c>Helpers.World</c>, <c>Helpers.Player</c>,
    /// <c>Helpers.Inventory</c>, <c>Helpers.Entities</c>,
    /// <c>Helpers.Vehicles</c>, <c>Helpers.Traders</c>, <c>Helpers.Rig</c>.
    /// </summary>
    public static partial class Helpers
    {
    }
}
