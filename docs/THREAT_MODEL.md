# Threat model: 7dtd-playtest

Starter threat model, built from code on this tree. Scope: the host
orchestrator (`scripts/playtest_run.py` and helpers) and the client mod
(`Source/PlayTestMod/`). Individual vulnerabilities are not fixed here; each
risk below names where a fix belongs (sec-review or the owning repo).

- Last reviewed: 2026-08-23 (against commit 134673b)
- Owner: organizational; no named owner or review cadence is recorded yet.
- Re-verify every file reference below against the current tree before acting.

## Risk-ranked summary

| # | Risk | Boundary | Exploit / impact | Status |
|---|------|----------|------------------|--------|
| R1 | LAN-exposed game + admin planes with weak or absent credentials | B1, B2 | Hostile LAN peer joins the game (empty `ServerPassword`) or logs into telnet admin (static default `retest`) and gets full console authority | Gap, highest priority |
| R2 | Log-derived strings forwarded to telnet without sanitization | B3 → B2 | A crafted client log line (reachable from remote chat content) injects extra telnet commands | Gap |
| R3 | Verdicts are self-reported by the client under test | B3 | A buggy or compromised client grades itself PASS; host decisions inherit that | Accepted by design, note |
| R4 | Mod supply chain executes inside the game client | B5 | Any installed assembly implementing `IScenarioProvider` is auto-instantiated; committed `dist/` binaries are installed unverified | Gap (accepted for dev use) |
| R5 | Lock tampering / stale-takeover races redirect destructive cleanup | B6 | A wrong takeover lets one agent `pkill` another's client/server or delete save data | Mitigated, residual risk |
| R6 | Availability: broad process kills and recursive deletes | B4 | `--kill-wine` kills Steam/wine sessions; `--fresh-save` rmtree under userdata; misdirected paths widen blast radius | Gap, operator-scoped |
| R7 | No disclosure path, no audit trail beyond run artifacts | - | No SECURITY.md exists; investigation relies on ephemeral files under LOGDIR/workspace | Note only |

## Assets

- The workstation runtime: orchestrator holds kill authority over game,
  wine/Proton, and Steam-adjacent processes (`clean_processes`,
  scripts/playtest_run.py:96) and delete authority over saves
  (`fresh_save`, scripts/playtest_run.py:910).
- Telnet/admin credential: `PLAYTEST_TELNET_PASSWORD` / `--telnet-password`,
  static weak default `retest` (scripts/playtest_run.py:1126). Lives in env /
  argv, plaintext in the generated server config
  (`serverconfig_playtest.xml` under userdata, write_stock_config
  scripts/playtest_run.py:140), and is sent cleartext over the telnet socket
  (`TelnetAdmin._send`, scripts/playtest_run.py:841). No rotation point.
- Game world / save state integrity (reproducibility of runs):
  userdata Saves tree, zdtd world dir (scripts/playtest_run.py:1272).
- Run verdicts and reports (gate CI-style decisions downstream): JSON/JUnit
  reports and compare outputs written to LOGDIR and `workspace/`
  (scripts/playtest_run.py:617, 2124).
- Exclusive-runtime coordination state: the lock file
  (`playtest_lock.default_lock_path`, scripts/playtest_lock.py:183).

## Entry points

| EP | Surface | Where |
|----|---------|-------|
| EP1 | CLI arguments (ports, paths, suite, password, session) | argparse block, scripts/playtest_run.py:973-1151 |
| EP2 | Environment variables (orchestrator + mod) | scripts/playtest_run.py:993-1149; mod reads `PLAYTEST_SUITE*`, `ZDTD_APM_*` at Source/PlayTestMod/Runner.cs:148, Source/PlayTestMod/Catalog.cs:4509 |
| EP3 | Network listeners started as side effects: stock dedicated game port (+2 LiteNet for loadgen), telnet port = `--admin-port` (default 8081); zdtd `--port` + `--admin-port` | start_stock_dedicated scripts/playtest_run.py:219; start_loadgen :472; start_zdtd :306; comment on zdtd admin surface :1362 |
| EP4 | Client log parsed as data (game output incl. remote chat lines, mod detail strings, JSON event lines) | playtest_log.py regexes + `json.loads` (:17-23, :67-108); polling loops in scripts/playtest_run.py:1711-2056 |
| EP5 | Barrier lines in that log trigger privileged telnet actions (spawn/kill/settime/say/teleport) | handlers scripts/playtest_run.py:1759-1998 |
| EP6 | Lock file read/write across agents (parseable key=value, flock sidecar) | scripts/playtest_lock.py:272 (read_lock), :355 (is_stale), /proc liveness probe :381-409 |
| EP7 | Scenario providers: any loaded mod assembly implementing `IScenarioProvider` | Source/PlayTestMod/ScenarioProvider.cs:26-55 |
| EP8 | Remote player chat captured via Harmony patch into harness memory and matched against tokens | Source/PlayTestMod/ChatProbe.cs:54-61 |
| EP9 | Deployment artifacts: CI workflow (pinned third-party actions), Makefile targets that launch servers, committed dist DLL installed into Mods | .github/workflows/ci.yml:17-19, Makefile:139, dist/ |

Trusted-by-convention inputs that cross a boundary: EP4 log content carries
remote-player chat text (EP8 feeds it) and is treated as instructions by EP5;
the lock file (EP6) is written by other agents and gates destructive cleanup.

## Trust boundaries

- **B1 Remote/LAN players ↔ game server/client.** UDP game protocol and chat;
  entry via EP3 ports. The generated config enables LAN explicitly
  (`platform.cfg` rewrite with `serverplatforms=Steam,LAN,Local`,
  scripts/playtest_run.py:244) and sets an empty `ServerPassword`
  (:205). ServerVisibility=0 only hides the server from browsing.
- **B2 Orchestrator ↔ server admin plane.** TCP telnet (stock) or zdtd admin
  port, password auth via `TelnetAdmin` (scripts/playtest_run.py:672).
  Whether the listener is restricted to loopback depends on template config
  and game defaults outside this repo; see "Unverified claims".
- **B3 Client log ↔ host automation.** File bytes become verdicts (EP4) and
  commands (EP5). Validation exists for report XML and event parsing, none for
  command forwarding.
- **B4 Operator ↔ orchestrator.** CLI/env are trusted operator input; the
  orchestrator then acts with user-level authority (process kills, deletes,
  config generation). Secrets enter here (asset above) and leave into the
  generated config file and telnet session.
- **B5 Installed mods ↔ client mod.** Provider discovery instantiates foreign
  code in-process (EP7); committed dist binaries are trusted when installed.
- **B6 Peer agents ↔ lock file.** Coordination is cooperative: flock
  serialization plus heartbeat staleness (scripts/playtest_lock.py:355);
  takeover requires stale heartbeat AND no live runtime process
  (/proc probe, :381).

Privilege transitions: barrier handling is where log parsing (untrusted) starts
acting with admin authority on the server (B3→B2); provider instantiation is
where installed-mod trust becomes in-game code execution (B5).

## Threats per boundary (concrete)

- **B1 (spoofing, tampering, DoS):** any LAN host can join the passwordless
  game server (R1); remote players inject chat text that reaches ChatProbe and
  the client log (EP8→EP4), seeding R2; loadgen bots connect unauthenticated
  by design on port+2 (start_loadgen, scripts/playtest_run.py:472).
- **B2 (spoofing, elevation):** telnet password is a static weak default,
  shipped in argv/env/config; a LAN-visible telnet gives full console
  (R1). zdtd admin-port auth enforcement lives in the zdtd repo, not here.
- **B3→B2 (tampering, elevation):** `chat_echo:<token>` and
  `spawn_vehicle:<class>` substrings are lifted raw from the log and
  interpolated into telnet commands (`say {token}` scripts/playtest_run.py:1934;
  `spawnentity {pid} {cls}` via spawn_near_players :818-839, handler
  :1949-1967): a crafted line can append arbitrary console commands (R2).
  Countermeasures that do exist on this path: whole-name barrier matching with
  escaped regex (`_barrier_hits`, :1450), bounded recv windows
  (`TelnetAdmin._recv`, :845).
- **B3 (repudiation/integrity):** results are whatever the client printed
  (R3); ANSI/control characters from game/chat lines can forge orchestrator
  stdout and JSON report fields (XML reports are sanitized, JSON are not).
- **B4 (availability, misdirection):** fixed pkill patterns are constants, but
  `kill_wine` extends them to wineserver/Steam (:111-120); `fresh_save`
  deletes `<userdata>/Saves/<world>/<game_name>` for every world (:910).
- **B6 (denial of service against peers):** a live holder refreshing its
  heartbeat blocks all other agents indefinitely; takeover of a crashed holder
  is guarded by the /proc liveness check, but a forged lock file could still
  misdirect a takeover (R5).

DoS exposure of parsing is bounded: NRE sample cap 50 (playtest_log.py:25),
chat history cap 64 (Source/PlayTestMod/ChatProbe.cs:32), recv chunk cap 32
(scripts/playtest_run.py:861). Unbounded: the poll loop re-reads the entire
client log into memory every cycle on some paths (scripts/playtest_run.py:1716),
which is cost, not compromise; LogTail exists as the incremental alternative
(playtest_log.py:182).

## Mitigations map (existing controls)

| Control | Covers | Where |
|---------|--------|-------|
| XML attribute escaping + illegal-character stripping for JUnit and serverconfig generation | B3 log→report markup injection | xml_attr scripts/playtest_run.py:628-641; regression gate scripts/test_report_surface.py |
| Event parser hardening: dict type check, scalar coercion, int guards catching TypeError/ValueError/OverflowError | B3 malformed/crafted event lines crashing the host | playtest_log.py:67-108; scripts/test_report_surface.py |
| Exclusivity lock acquired before any clean/launch; release refused unless held | B6 concurrent destructive cleanup | scripts/playtest_run.py:1228-1254, finally block :2185-2215; scripts/test_playtest_lock.py, DST simulation |
| Stale takeover gated on absence of live runtime processes | B6 wrongful takeover while a client lives | scripts/playtest_lock.py:355-409 |
| Post-clean double-bind refusal on ServerPort/admin port | B1/B2 orphan listeners racing new runs | scripts/playtest_run.py:1257-1270 |
| Signal conversion so SIGTERM/SIGHUP unwind through cleanup (stop children, release lock) | availability / stale-lock wedge | install_signal_handlers scripts/playtest_run.py:876-907 |
| Generated-server hardening: WebDashboardEnabled=false, SteamNetworking disabled, crossplay off, EnemySpawnMode off | shrinks B1 surface (dashboard, relay) | write_stock_config repls scripts/playtest_run.py:181-207 |
| Single-source telnet password feeding both config and client | config/auth divergence | scripts/playtest_run.py:1126-1129, :1307-1311; documented README.md:422-429 |
| Third-party CI actions pinned by commit SHA | supply chain of build tooling | .github/workflows/ci.yml:17-19 |

## Claimed mitigations not enforced in this repo

Highest-value category; verify before relying on them.

- README.md:424-429 states "the server binds localhost in playtest runs". This
  repo's config generator never sets a bind address or
  `TelnetRemoteAllowedIPs`; loopback-only reachability depends entirely on the
  template config (loadgen's or the game install's) and game defaults outside
  this repository. Treat R1 reachability as unverified until probed on a real
  run (sec-review owns that check).
- zdtd admin port "speaks the same command surface" (comment
  scripts/playtest_run.py:1362): authentication of that port is implemented in
  the zdtd repo, not here; the model records the boundary, not its strength.

## Abuse cases (authenticated/hostile-but-authorized actors)

- **Console holder:** anyone who obtains telnet/admin access (R1) can kick
  players, teleport, set time, spawn entities, or corrupt the save via
  saveworld; every `TelnetAdmin.exec` call site is such a capability
  (scripts/playtest_run.py:698). By design these are the fixture primitives;
  the abuse is unauthorized holders, not the commands.
- **Lock squatter:** an agent holding the exclusivity lock and refreshing its
  heartbeat denies all other agents the shared runtime indefinitely
  (heartbeat interval/staleness are env-tunable,
  scripts/playtest_lock.py:190). Stale takeover is the only recourse and is
  deliberately conservative.
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
- Audit trail: run evidence lives in LOGDIR reports/logs and `workspace/`
  snapshots; lock takeovers and cleans are logged to orchestrator stdout only
  and are not persisted anywhere structured.

## Out of scope here

- Fixing R1/R2/R4 (sec-review; R1 server-side hardening partly belongs to
  zdtd/loadgen template owners).
- CVE inventory of dependencies (deps-review); PII/compliance mapping
  (privacy-review); log structure standards (o11y-review); general docs prose
  (doc-review).
