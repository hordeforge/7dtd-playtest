using System.Collections.Generic;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Back-compat entry points. Prefer <see cref="Catalog"/>.
    /// </summary>
    static class Suites
    {
        public static void AddSmoke(List<CaseDef> q) => Catalog.AppendSuite(q, "smoke", 0);
        public static void AddCore(List<CaseDef> q) => Catalog.AppendSuite(q, "core", 0);
    }
}
