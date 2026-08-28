using System;
using System.Collections.Generic;
using System.Reflection;

namespace ZdtdPlaytest
{
    /// <summary>
    /// Adds named scenarios from another client mod to the playtest catalog.
    /// The provider must have a public parameterless constructor. Its assembly
    /// must be installed beside 7dtd-playtest before the client is launched.
    /// Append live/deferred cases via <see cref="CaseDef.Live"/> /
    /// <see cref="CaseDef.Defer"/> (shared with the built-in catalog).
    /// </summary>
    public interface IScenarioProvider
    {
        /// <summary>
        /// Suite ids this provider owns. Matching is case-insensitive; an id
        /// claimed by a built-in suite remains built-in.
        /// </summary>
        IEnumerable<string> SuiteIds { get; }

        /// <summary>
        /// Appends this provider's cases for one requested suite and benchmark
        /// lap. Use the supplied <paramref name="lap"/> to keep repeated case
        /// ids distinct in host reports.
        /// </summary>
        void AppendSuite(List<CaseDef> queue, string suite, int lap);
    }

    /// <summary>Discovers scenario providers from already-loaded mod assemblies.</summary>
    static class ScenarioProviders
    {
        static readonly List<IScenarioProvider> Providers = new List<IScenarioProvider>();
        static bool _discovered;

        static void Discover()
        {
            if (_discovered) return;
            _discovered = true;

            foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = assembly.GetTypes(); }
                catch (ReflectionTypeLoadException ex) { types = ex.Types; }
                catch { continue; }

                if (types == null) continue;
                foreach (var type in types)
                {
                    if (type == null || type.IsAbstract || type.IsInterface
                        || !typeof(IScenarioProvider).IsAssignableFrom(type))
                        continue;
                    try
                    {
                        var provider = Activator.CreateInstance(type) as IScenarioProvider;
                        if (provider != null) Providers.Add(provider);
                    }
                    catch (Exception ex)
                    {
                        Report.Info("scenario provider skipped " + type.FullName + ": " + ex.Message);
                    }
                }
            }
        }

        public static string[] SuiteIds()
        {
            Discover();
            var ids = new List<string>();
            foreach (var provider in Providers)
            {
                try
                {
                    foreach (var suite in provider.SuiteIds ?? Array.Empty<string>())
                    {
                        if (!string.IsNullOrWhiteSpace(suite) && !ids.Contains(suite))
                            ids.Add(suite);
                    }
                }
                catch (Exception ex)
                {
                    Report.Info("scenario provider suite list failed: " + ex.Message);
                }
            }
            return ids.ToArray();
        }

        public static bool AppendSuite(List<CaseDef> queue, string suite, int lap)
        {
            Discover();
            bool matched = false;
            foreach (var provider in Providers)
            {
                try
                {
                    bool ownsSuite = false;
                    foreach (var id in provider.SuiteIds ?? Array.Empty<string>())
                    {
                        if (string.Equals(id, suite, StringComparison.OrdinalIgnoreCase))
                        {
                            ownsSuite = true;
                            break;
                        }
                    }
                    if (!ownsSuite) continue;

                    matched = true;
                    provider.AppendSuite(queue, suite, lap);
                }
                catch (Exception ex)
                {
                    Report.Info("scenario provider " + provider.GetType().FullName
                        + " failed for " + suite + ": " + ex.Message);
                }
            }
            return matched;
        }
    }
}
