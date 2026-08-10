using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Text;

namespace ZdtdPlaytest
{
    /// <summary>Structured + human log lines for the host orchestrator.</summary>
    static class Report
    {
        static readonly List<string> Results = new List<string>();
        static int Pass;
        static int Fail;
        static int Skip;
        static readonly Stopwatch Wall = new Stopwatch();
        static readonly List<(string id, float ms)> Timings = new List<(string, float)>();

        public static void Reset()
        {
            Results.Clear();
            Timings.Clear();
            Pass = Fail = Skip = 0;
            Wall.Reset();
            Wall.Start();
        }

        public static void Info(string msg)
        {
            Log.Out("[zdtd-playtest] " + msg);
            EmitJson("log", "\"level\":\"info\",\"msg\":" + JsonString(msg));
        }

        public static void Barrier(string name)
        {
            // Host orchestrator greps this and runs telnet/admin setup.
            Log.Out("[zdtd-playtest] barrier " + name);
            EmitJson("barrier", "\"name\":" + JsonString(name));
        }

        public static void Result(string suite, string caseId, string status, float ms, string detail)
        {
            status = (status ?? "fail").ToLowerInvariant();
            if (status == "pass") Pass++;
            else if (status == "skip") Skip++;
            else Fail++;

            string key = suite + "/" + caseId;
            if (status == "pass" || status == "fail")
                Timings.Add((key, ms));

            string line = status.ToUpperInvariant() + " " + key
                + (string.IsNullOrEmpty(detail) ? "" : " " + detail);
            Results.Add(line);
            Log.Out("[zdtd-playtest] " + line);
            EmitJson("result",
                "\"suite\":" + JsonString(suite)
                + ",\"case\":" + JsonString(caseId)
                + ",\"status\":" + JsonString(status)
                + ",\"ms\":" + ms.ToString("0", CultureInfo.InvariantCulture)
                + ",\"detail\":" + JsonString(detail ?? ""));
        }

        public static void Summary(string[] suites)
        {
            long wallMs = Wall.ElapsedMilliseconds;
            Log.Out("[zdtd-playtest] SUMMARY pass=" + Pass + " fail=" + Fail
                + " skip=" + Skip + " total=" + Results.Count
                + " wall_ms=" + wallMs);
            foreach (var r in Results)
                Log.Out("[zdtd-playtest]   " + r);

            // Top 5 slowest live cases (bench signal).
            if (Timings.Count > 0)
            {
                Timings.Sort((a, b) => b.ms.CompareTo(a.ms));
                int n = Math.Min(5, Timings.Count);
                var sbSlow = new StringBuilder("slowest");
                for (int i = 0; i < n; i++)
                    sbSlow.Append(' ').Append(Timings[i].id).Append('=')
                        .Append(Timings[i].ms.ToString("0", CultureInfo.InvariantCulture)).Append("ms");
                Log.Out("[zdtd-playtest] " + sbSlow);
            }

            var sb = new StringBuilder();
            sb.Append("\"pass\":").Append(Pass)
                .Append(",\"fail\":").Append(Fail)
                .Append(",\"skip\":").Append(Skip)
                .Append(",\"total\":").Append(Results.Count)
                .Append(",\"wall_ms\":").Append(wallMs)
                .Append(",\"suites\":[");
            for (int i = 0; i < suites.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(JsonString(suites[i]));
            }
            sb.Append(']');
            EmitJson("summary", sb.ToString());
        }

        public static void Done()
        {
            int exitHint = Fail > 0 ? 1 : 0;
            Log.Out("[zdtd-playtest] DONE exit_hint=" + exitHint
                + " wall_ms=" + Wall.ElapsedMilliseconds);
            EmitJson("done", "\"exit_hint\":" + exitHint + ",\"pass\":" + Pass
                + ",\"fail\":" + Fail + ",\"skip\":" + Skip
                + ",\"wall_ms\":" + Wall.ElapsedMilliseconds);
        }

        static void EmitJson(string type, string bodyFields)
        {
            Log.Out("[zdtd-playtest] {\"v\":1,\"t\":\"" + type + "\"," + bodyFields + "}");
        }

        static string JsonString(string s)
        {
            if (s == null) return "\"\"";
            var sb = new StringBuilder(s.Length + 2);
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '\\': sb.Append("\\\\"); break;
                    case '"': sb.Append("\\\""); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 32) sb.AppendFormat("\\u{0:x4}", (int)c);
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }
    }
}
