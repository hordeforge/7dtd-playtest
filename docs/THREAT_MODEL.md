# Threat model: 7dtd-playtest

Living threat model, built from code on this tree. Scope: the host
orchestrator (`scripts/playtest_run.py` and helpers) and the client mod
(`Source/PlayTestMod/`). Individual vulnerabilities are not fixed here; each
risk below names where a fix belongs (sec-review or the owning repo).

- Last reviewed: 2026-08-23 (against commit 88f19b5)
- Owner: organizational; no named owner or review cadence is recorded yet.
- Re-verify every file reference below against the current tree before acting.

## Risk-ranked summary

| # | Risk | Boundary | Exploit / impact | Status |
|---|------|----------|------------------|--------|
| R1 | LAN-exposed game + admin planes with weak or absent credentials | B1, B2 | Hostile LAN peer joins the game (empty `ServerPassword`) or logs into telnet admin (static default `retest`) and gets full console authority | Gap, highest priority |
| R2 | Log-derived strings forwarded to telnet without sanitization | B3 → B2 | A crafted client log line (reachable from remote chat content) injects extra telnet commands; each barrier line is also an amplification step (one line → one+ console commands) | Gap |
| R3 | Verdicts are self-reported by the client under test | B3 | A buggy or compromised client grades itself PASS; host decisions inherit that | Accepted by design, note |
| R4 | Mod supply chain executes inside the game client | B5 | Any installed assembly implementing `IScenarioProvider` is auto-instantiated; the built dist DLL is installed into Mods unverified | Gap (accepted for dev use) |
| R5 | Lock tampering / stale-takeover races redirect destructive cleanup | B6 | A wrong takeover lets one agent `pkill` another's client/server or move save data aside | Mitigated, residual risk |
| R6 | Availability: broad process kills and destructive moves | B4 | `--kill-wine` kills Steam/wine sessions; `--fresh-save` moves saves under `<logdir>/quarantine` (recoverable until pruned); misdirected paths widen blast radius | Mitigated (quarantine), operator-scoped residual |
| R7 | No disclosure path; audit trail is bounded run artifacts | - | No SECURITY.md exists; evidence lives under LOGDIR and is pruned (newest 50 reports, newest 5 quarantine entries) | Note only |

## Assets

- The workstation runtime: orchestrator holds kill authority over game,
  wine/Proton, and Steam-adjacent processes (`clean_processes`,
  scripts/playtest_run.py:178; teardown `stop_proc`, :703) and
  move/delete authority over saves and logs (`fresh_save`,
  scripts/playtest_run.py:1101; `fresh_zdtd_world`, :1140;
  `prune_quarantine` rmtree of aged entries, :1053).
- Telnet/admin credential: `PLAYTEST_TELNET_PASSWORD` / `--telnet-password`,
  static weak default `retest` (scripts/playtest_run.py:1476). Lives in env /
  argv, plaintext in the generated server config
  (`serverconfig_playtest.xml` under userdata, written by `write_stock_config`
  scripts/playtest_run.py:240, value at :303, file chmod 0600 at :316), and is
  sent cleartext over the telnet socket (`TelnetAdmin._send`,
  scripts/playtest_run.py:956). Startup logs redact it to set/unset
  (`config_summary`, :129). No rotation point.
- Game world / save state integrity (reproducibility of runs):
  userdata Saves tree, zdtd world dir (scripts/playtest_run.py:1140).
- Run verdicts and reports (gate CI-style decisions downstream): JSON/JUnit
  reports written to LOGDIR (`write_report` scripts/playtest_run.py:732,
  `write_junit` :765, paths :1540/:2461) and compared across runs by
  scripts/playtest_compare.py.
- Exclusive-runtime coordination state: the lock file
  (`playtest_lock.default_lock_path`, scripts/playtest_lock.py:191).

## Entry points

| EP | Surface | Where |
|----|---------|-------|
| EP1 | CLI arguments (ports, paths, suite, password, session) | argparse block, scripts/playtest_run.py:1318-1500 |
| EP2 | Environment variables (orchestrator + mod): ports, paths, timeout, lock knobs, `PLAYTEST_SUITE*`, `ZDTD_APM_*` | orchestrator defaults scripts/playtest_run.py:1334-1499 and `seconds_from_env` :102; mod reads at Source/PlayTestMod/Runner.cs:163-189, Source/PlayTestMod/Catalog.cs:4510-4572 |
| EP3 | Network listeners started as side effects: stock dedicated game port (+2 LiteNet for loadgen), telnet port = `--admin-port` (default 8081); zdtd `--port` + `--admin-port` | start_stock_dedicated scripts/playtest_run.py:355; start_loadgen :595 (litenet = port+2 :606); start_zdtd :434; zdtd admin-surface comment :1711 |
| EP4 | Client log parsed as data (game output incl. remote chat lines, mod detail strings, JSON event lines) | playtest_log.py regexes (:18-26) + `json.loads` inside `ClientLogScan.feed_line` (:71-113); poll loops scripts/playtest_run.py:1808, :2020 |
| EP5 | Barrier lines in that log trigger privileged telnet actions (spawn/kill/settime/say/teleport/bot commands) | handlers scripts/playtest_run.py:2040-2340 |
| EP6 | Lock file read/write across agents (parseable key=value, atomic tmp+rename publish) | scripts/playtest_lock.py:299 (read_lock), :379 (is_stale), /proc liveness probes :405-450 |
| EP7 | Scenario providers: any loaded mod assembly implementing `IScenarioProvider` | Source/PlayTestMod/ScenarioProvider.cs:26-55 (`Activator.CreateInstance` :46) |
| EP8 | Remote player chat captured via Harmony patch into harness memory and matched against tokens | Source/PlayTestMod/ChatProbe.cs:54-61 |
| EP9 | Deployment artifacts: CI workflow (pinned third-party actions), Makefile targets that launch servers, built dist DLL installed into Mods | .github/workflows/ci.yml:28-29, Makefile:71-73, dist/ (build output, not committed) |

Trusted-by-convention inputs that cross a boundary: EP4 log content carries
remote-player chat text (EP8 feeds it) and is treated as instructions by EP5;
the lock file (EP6) is written by other agents and gates destructive cleanup.

## Trust boundaries

- **B1 Remote/LAN players ↔ game server/client.** UDP game protocol and chat;
  entry via EP3 ports. The generated config enables LAN explicitly
  (`platform.cfg` rewrite with `serverplatforms=Steam,LAN,Local`,
  scripts/playtest_run.py:374) and sets an empty `ServerPassword`
  (:305). ServerVisibility=0 only hides the server from browsing.
- **B2 Orchestrator ↔ server admin plane.** TCP telnet (stock, host fixed to
  127.0.0.1 client-side, scripts/playtest_run.py:1658) or zdtd admin port,
  password auth via `TelnetAdmin` (scripts/playtest_run.py:797). Whether the
  listeners are reachable beyond loopback depends on template config and game
  defaults outside this repo; see "Claimed mitigations not enforced".
- **B3 Client log ↔ host automation.** File bytes become verdicts (EP4) and
  commands (EP5). Validation exists for report XML and event parsing, none for
  command forwarding.
- **B4 Operator ↔ orchestrator.** CLI/env are trusted operator input; the
  orchestrator then acts with user-level authority (process kills, deletes,
  config generation). Secrets enter here (asset above) and leave into the
  generated config file and telnet session.
- **B5 Installed mods ↔ client mod.** Provider discovery instantiates foreign
  code in-process (EP7); the built dist binary is trusted when installed.
- **B6 Peer agents ↔ lock file.** Coordination is cooperative: flock
  serialization plus heartbeat staleness (scripts/playtest_lock.py:379);
  takeover requires stale heartbeat AND no live runtime process
  (/proc probes, :405-450).

Privilege transitions: barrier handling is where log parsing (untrusted) starts
acting with admin authority on the server (B3→B2); provider instantiation is
where installed-mod trust becomes in-game code execution (B5).

## Threats per boundary (concrete)

- **B1 (spoofing, tampering, DoS):** any LAN host can join the passwordless
  game server (R1); remote players inject chat text that reaches ChatProbe and
  the client log (EP8→EP4), seeding R2; loadgen bots connect unauthenticated
  by design on port+2 (start_loadgen, scripts/playtest_run.py:595).
- **B2 (spoofing, elevation):** telnet password is a static weak default,
  shipped in argv/env/config; a LAN-visible telnet gives full console
  (R1). zdtd admin-port auth enforcement lives in the zdtd repo, not here.
- **B3→B2 (tampering, elevation):** `chat_echo:<token>` and
  `spawn_vehicle:<class>` substrings are lifted raw from the log and
  interpolated into telnet commands (`say {token}` scripts/playtest_run.py:2276;
  `spawnentity {pid} {cls}` via spawn_near_players :938-954, handler
  :2290-2308): a crafted line can append arbitrary console commands (R2).
  Countermeasures that do exist on this path: whole-name/prefix barrier
  matching with escaped regex (`barrier_line_hits` :45, `add_barrier_hits`
  :56, `barrier_hits_prefix` playtest_log.py:29), non-empty token check and
  fire-once-per-token state (:2266-2281), per-run counter tables reset only at
  generation boundaries (`new_barrier_tables`, :1285), bounded recv windows
  (`TelnetAdmin._recv`, :960). No character-level sanitization exists on
  token/class before interpolation.
- **B3 (repudiation/integrity):** results are whatever the client printed
  (R3); ANSI/control characters from game/chat lines reach orchestrator stdout
  verbatim via progress crumbs (scripts/playtest_run.py:2031-2037) and JSON
  report detail fields preserve raw text (XML reports are escaped and stripped,
  JSON are syntax-escaped only).
- **B4 (availability, misdirection):** fixed pkill patterns are constants, but
  `kill_wine` extends them to wineserver/Steam (:193-202); `fresh_save` moves
  `<userdata>/Saves/<world>/<game_name>` for every world into quarantine
  (:1101), so loss is deferred until `prune_quarantine` drops entries older
  than the newest five (:1053); rejoin teardown pkills game/server patterns
  (:1908, :1958).
- **B6 (denial of service against peers):** a live holder refreshing its
  heartbeat blocks all other agents indefinitely; takeover of a crashed holder
  is guarded by the /proc liveness check, but a forged lock file could still
  misdirect a takeover (R5).

DoS exposure of parsing is bounded: NRE sample cap 50 (playtest_log.py:26),
chat history cap 64 (Source/PlayTestMod/ChatProbe.cs:32), recv chunk cap 32
(scripts/playtest_run.py:976). Polling is incremental (O(new bytes)) on both
wait paths (`LogTail`, playtest_log.py:193; `pump_log_tail`,
scripts/playtest_run.py:1301; `wait_file_contains`, :216), so a chatty or
hostile log no longer costs quadratic host CPU. Remaining unbounded: each new
unique `chat_echo:`/`spawn_vehicle:` token fires fresh admin commands with no
global rate cap (only `settime_bloodmoon` has one, SETTIME_BLOODMOON_MAX_FIRES
= 2, scripts/playtest_run.py:1258) - an amplification path from log volume to
console traffic, not memory exhaustion.

## Mitigations map (existing controls)

| Control | Covers | Where |
|---------|--------|-------|
| XML attribute escaping + illegal-character stripping for JUnit and serverconfig generation | B3 log→report/config markup injection | xml_attr scripts/playtest_run.py:754-762; regression gate scripts/test_report_surface.py |
| Event parser hardening: dict type check, scalar coercion, int guards catching TypeError/ValueError/OverflowError | B3 malformed/crafted event lines crashing the host | playtest_log.py:71-113; scripts/test_report_surface.py |
| Exclusivity lock acquired before any clean/launch; release refused unless held | B6 concurrent destructive cleanup | scripts/playtest_run.py:1581-1610, finally block :2533-2563; scripts/test_playtest_lock.py, DST simulation |
| Stale takeover gated on absence of live runtime processes | B6 wrongful takeover while a client lives | scripts/playtest_lock.py:379-450 |
| Post-clean double-bind refusal on ServerPort/admin port | B1/B2 orphan listeners racing new runs | scripts/playtest_run.py:1616-1629; playtest_lock.tcp_port_in_use :464 |
| Signal conversion so SIGTERM/SIGHUP unwind through cleanup (stop children, release lock) | availability / stale-lock wedge | install_signal_handlers scripts/playtest_run.py:989-1019 |
| Generated-server hardening: WebDashboardEnabled=false, SteamNetworking disabled, crossplay off, EnemySpawnMode off, PlayerKillingMode 0 | shrinks B1 surface (dashboard, relay, PvP) | write_stock_config repls scripts/playtest_run.py:281-307 |
| Single-source telnet password feeding both config and client | config/auth divergence | scripts/playtest_run.py:1476, :303, :1658-1660; documented README.md:492-499 |
| Secret hygiene: startup config line prints password as set/unset only; generated serverconfig chmod 0600 | credential leakage into shareable run logs / world-readable files | config_summary scripts/playtest_run.py:129-151, :1538; chmod :316-321; gate scripts/test_playtest_run_units.py (config_summary_redaction, stock_config_permissions) |
| Quarantine-before-delete for --fresh-save, zdtd world reset, prior client logs; report/junit pruning bounded to newest 50 | B4 irreversible destruction by mispointed flags | QUARANTINE_* + prune helpers scripts/playtest_run.py:1022-1186; gate scripts/test_playtest_run_units.py |
| Incremental log readers (offset tail, complete-line buffering, shrink detection) | DoS-by-log-volume cost on shared host CPU | LogTail playtest_log.py:193-237; pump/wait loops scripts/playtest_run.py:216, :1301 |
| Third-party CI actions pinned by commit SHA; workflow token read-only; job timeout | supply chain of build tooling; hung-run cost bound | .github/workflows/ci.yml:14-15, :25, :28-29 |

## Claimed mitigations not enforced in this repo

Highest-value category; verify before relying on them.

- README.md:492-499 states "the server binds localhost in playtest runs"
  (justified by `ServerVisibility=0` and Steam+LAN platforms). This repo's
  config generator never sets a bind address or `TelnetRemoteAllowedIPs`
  (no such property appears anywhere under scripts/), and the same generator
  explicitly enables LAN platforms. Loopback-only reachability depends on the
  template config (loadgen's or the game install's) and game defaults outside
  this repository. Treat R1 reachability as unverified until probed on a real
  run (sec-review owns that check).
- zdtd admin port "speaks the same command surface" (comment
  scripts/playtest_run.py:1711): authentication of that port is implemented in
  the zdtd repo, not here; the model records the boundary, not its strength.

## Abuse cases (authenticated/hostile-but-authorized actors)

- **Console holder:** anyone who obtains telnet/admin access (R1) can kick
  players, teleport, set time, spawn entities, or corrupt the save via
  saveworld; every `TelnetAdmin.exec` call site is such a capability
  (exec scripts/playtest_run.py:836; call sites :1845-2331). By design these
  are the fixture primitives; the abuse is unauthorized holders, not the
  commands.
- **Lock squatter:** an agent holding the exclusivity lock and refreshing its
  heartbeat denies all other agents the shared runtime indefinitely
  (HeartbeatThread scripts/playtest_lock.py:736; interval/staleness env-tunable
  :228-233). Stale takeover is the only recourse and is deliberately
  conservative.
- **Self-grading client:** the client under test both performs cases and
  reports their outcome through the same log the host trusts (EP4/R3); a
  hostile provider (B5) could print passing lines for work it skipped. No
  server-side corroboration exists for most claims.

No attack was demonstrated or executed while building this document; all
evidence is read from code.

## Response readiness (notes only)

- No SECURITY.md exists: there is no recorded disclosure contact or supported-
  version statement. Creating one requires organizational decisions this
  document does not make.
- Audit trail: run evidence lives in LOGDIR reports/logs and quarantine
  snapshots, but retention is bounded by design (newest 50 report/junit pairs
  scripts/playtest_run.py:1035-1050; newest 5 quarantine entries :1028) and
  lock takeovers/cleans log to orchestrator stdout only. Enough to answer
  "what happened this run", thin for post-hoc incident review.

## Out of scope here

- Fixing R1/R2/R4 (sec-review; R1 server-side hardening partly belongs to
  zdtd/loadgen template owners).
- CVE inventory of dependencies (deps-review); PII/compliance mapping
  (privacy-review); log structure standards (o11y-review); general docs prose
  (doc-review).
