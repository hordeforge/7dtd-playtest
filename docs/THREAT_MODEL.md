# Threat model: 7dtd-playtest

Living threat model, built from code on this tree. Scope: the host
orchestrator (`scripts/playtest_run.py` and helpers), the run-artifact
tooling (`scripts/capture_*.sh`, `scripts/review_video.py`,
`scripts/video_review.py`) and the client mod (`Source/PlayTestMod/`).
Individual vulnerabilities are not fixed here; each risk below names where a
fix belongs (sec-review or the owning repo).

- Last reviewed: 2026-09-28 (against commit 2a6c41c)
- Owner: organizational; no named owner or review cadence is recorded yet.
- Re-verify every file reference below against the current tree before acting.

## Risk-ranked summary

| # | Risk | Boundary | Exploit / impact | Status |
|---|------|----------|------------------|--------|
| R1 | Run artifacts, including the client log, are uploaded to a third-party vision provider | B7 | `review_video` sends a whole clip directory, and the capture scripts leave a copy of the client log beside the mp4 (`scripts/capture_frames.sh:243`, `scripts/capture_video.sh:279`). That log carries whatever remote LAN players typed (EP8). Consent-gated; the disclosure text names the logs, but a reviewer consenting to "review my clip" is also consenting to shipping a peer chat transcript | Gap: mitigation exists, operator-facing description is where the risk lives |
| R2 | Telnet admin credential passes through a process argument vector | B2 → B4 | The generated server config is rendered by handing `k=v` pairs to `sb render-config` (`scripts/playtest_targets.py:286`), and that dict carries `TelnetPassword` (`scripts/playtest_run.py:3176`). The password is therefore readable from the process table by any local user for the lifetime of that `sb` call. It is kept out of the run *report* (`:3164`), not out of argv | Gap, report to sec-review |
| R3 | Game server joins open to the LAN with an empty join password | B1 | A hostile host on the same LAN joins every playtest instance: `ServerPassword` is empty in each suite's server block (`suites/smoke.json:31`, `suites/core.json:31`, `suites/parachute.json:37`) and the templates enable LAN platforms. A joined peer is a peer the client-mod threat model below has to assume | Gap, accepted for a disposable test world; does not apply to `--readonly` hosts |
| R4 | Verdicts are self-reported by the client under test | B3 | A buggy or compromised client grades itself PASS; host decisions inherit that | Accepted by design, note |
| R5 | Mod supply chain executes inside the game client | B5 | Any installed assembly implementing `IScenarioProvider` is auto-instantiated; the built dist DLL is installed into Mods unverified | Gap (accepted for dev use) |
| R6 | Lock tampering / stale-takeover races redirect destructive cleanup | B6 | A wrong takeover lets one agent `pkill` another's client/server or move save data aside | Mitigated, residual risk |
| R7 | Availability: broad process kills and destructive moves | B4 | `--kill-wine` kills wineserver and Steam-adjacent processes (`clean_processes`, scripts/playtest_run.py:523); `--fresh-save` moves saves into quarantine, recoverable until pruned | Mitigated (quarantine), operator-scoped residual |
| R8 | No disclosure path; audit trail is bounded run artifacts | - | No SECURITY.md exists; evidence lives under LOGDIR and `.local/capture/`, both on one local disk, and is pruned | Note only |

Closed since the last review, kept here so the next pass does not re-raise it:

- **Admin plane is loopback-pinned.** The generated config sets
  `TelnetRemoteAllowedIPs=127.0.0.1` alongside `TelnetEnabled`, *after* the
  suite's own server block is copied in so a suite cannot widen it
  (scripts/playtest_run.py:3167-3176). The previous revision of this document
  claimed no such property existed anywhere under `scripts/`; that was wrong
  and is the reason R1 changed shape.
- **Telnet password is ephemeral per run.** An unset
  `PLAYTEST_TELNET_PASSWORD` becomes `secrets.token_urlsafe(15)`
  (`resolve_telnet_password`, scripts/playtest_run.py:2168, `:2183`). There is
  no shipped `retest` default on this path. Only `--no-server` runs require an
  operator-supplied value (`:2179-2182`).
- **Log-derived barrier parameters are allowlisted.** `chat_echo:` and
  `spawn_vehicle:` parameters must match `[A-Za-z0-9_]{1,64}` before they reach
  a telnet command (`BARRIER_PARAM_RE` / `safe_barrier_param`,
  scripts/playtest_run.py:2020, `:2023`; enforced `:3839` and `:3880`). A
  crafted log line can no longer append console commands. The amplification
  residue in R1's former sense is a fresh admin session per unique valid token,
  with no global rate cap.

## Assets

- The workstation runtime: the orchestrator holds kill authority over game,
  wine/Proton and Steam-adjacent processes (`GAME_PROC_PATTERNS`
  scripts/playtest_run.py:263, `clean_processes` :523, `kill_wine` branch :533,
  teardown `stop_proc` :1229) and move/delete authority over saves and logs
  (`prune_quarantine` rmtree of aged entries :1732, `fresh_zdtd_world` :1784,
  `snapshot_previous_log` :1840).
- Telnet/admin credential: `PLAYTEST_TELNET_PASSWORD` / `--telnet-password`
  (scripts/playtest_run.py:2521). Enters from env or argv, becomes an
  ephemeral per-run secret when unset, is sent cleartext over the telnet
  socket (`TelnetAdmin`, :1349), and reaches the generated server config both
  through the `sb render-config` argv (R2) and on disk under userdata. Startup
  logs redact it to set/unset (`config_summary`, :429). No rotation point: the
  value is valid for the life of the run and dies with the instance.
- Remote player chat: captured in client memory by a Harmony patch
  (`ChatProbe`, Source/PlayTestMod/ChatProbe.cs:10). It is third-party text
  this repo holds, which is why it appears in R1 and B7.
- Game world / save state integrity (reproducibility of runs): userdata Saves
  tree, zdtd world dir (scripts/playtest_run.py:1784).
- Run verdicts and reports (gate CI-style decisions downstream): JSON/JUnit
  reports written to LOGDIR (`write_report` :1258, `write_junit` :1317) and
  compared across runs by scripts/playtest_compare.py.
- Capture artifacts: frames, mp4, contact sheets and the copied client log
  under `.local/capture/`, uploaded outward on the B7 path.

## Entry points

| EP | Surface | Where |
|----|---------|-------|
| EP1 | CLI arguments (ports, paths, suite, password, session) | argparse block, scripts/playtest_run.py:2452-2696; telnet password option :2521 |
| EP2 | Environment variables (orchestrator + mod): ports, paths, timeout, lock knobs, `PLAYTEST_SUITE*` | orchestrator defaults scripts/playtest_run.py:2521ff and `seconds_from_env` :342; lock knobs scripts/playtest_lock.py:287, :308, :312; mod reads at Source/PlayTestMod/Runner.cs:163-189, Source/PlayTestMod/Catalog.cs:4510-4572 |
| EP3 | Network listeners started as side effects: stock dedicated game port (+2 LiteNet for loadgen), telnet port = `--admin-port`; zdtd `--port` + `--admin-port` | `start_zdtd` scripts/playtest_run.py:632; `start_loadgen` :864 (litenet = port+offset, room check `require_litenet_room` :371); managed stock listeners are opened by `sb up` (scripts/playtest_targets.py:288) |
| EP4 | Client log parsed as data (game output incl. remote chat lines, mod detail strings, JSON event lines) | playtest_log.py `feed_line` :181, `parse_client_log` :310; poll loops scripts/playtest_run.py `pump_log_tail`, `LogTail.poll` playtest_log.py:322, :361 |
| EP5 | Barrier lines in that log trigger privileged telnet actions (spawn/kill/settime/say/teleport/bot commands) | handlers scripts/playtest_run.py:3834 (`chat_echo`), :3876 (`spawn_vehicle`), `service_barrier` sites through :3908 |
| EP6 | Lock file read/write across agents (parseable key=value, atomic tmp+rename publish) | scripts/playtest_lock.py `read_lock` :390, `write_lock` :432, `is_stale` :474, /proc liveness probes `_runtime_pids` :508ff |
| EP7 | Scenario providers: any loaded mod assembly implementing `IScenarioProvider` | Source/PlayTestMod/ScenarioProvider.cs:56 (`Activator.CreateInstance`) |
| EP8 | Remote player chat captured via Harmony patch into harness memory and matched against tokens | Source/PlayTestMod/ChatProbe.cs:10ff |
| EP9 | Deployment artifacts: CI workflow (pinned third-party actions), Makefile targets that launch servers, built dist DLL installed into Mods | .github/workflows/ci.yml:14, :25, :28-29, :56-62; Makefile `install`/`playtest*` targets; dist/ (build output, not committed) |
| EP10 | Run-artifact capture and upload: the capture scripts write a clip directory, `review_video` submits it to an external gateway CLI | scripts/capture_frames.sh:243, scripts/capture_video.sh:279, scripts/review_video.py:74, scripts/video_review.py:358 |
| EP11 | Safehouse (`sb`) as a subprocess boundary: every managed-stock lifecycle step is an `sb` call, and suite server-block keys become `k=v` argv | scripts/playtest_targets.py `ensure_sandbox_server` :256, `render-config` :286 |

Trusted-by-convention inputs that cross a boundary: EP4 log content carries
remote-player chat text (EP8 feeds it) and is treated as instructions by EP5;
the lock file (EP6) is written by other agents and gates destructive cleanup;
the suite document's `server` block is operator input that this repo forwards
to the instance config verbatim apart from the telnet keys (EP11).

## Trust boundaries

- **B1 Remote/LAN players ↔ game server/client.** UDP game protocol and chat;
  entry via EP3 ports. Suites set `ServerVisibility=0` and an empty
  `ServerPassword` (`suites/smoke.json:23-31`) while the instance templates
  enable LAN platforms, so visibility is not reachability. `ServerVisibility=0`
  only hides the server from browsing.
- **B2 Orchestrator ↔ server admin plane.** TCP telnet (stock; the orchestrator
  always connects from `127.0.0.1`) or the zdtd admin port, password auth via
  `TelnetAdmin` (scripts/playtest_run.py:1349). On a managed sandbox run the
  listener is pinned to loopback (`:3175`); on an **attach** run this repo
  writes no config at all, so the exposure is whatever the host already runs.
- **B3 Client log ↔ host automation.** File bytes become verdicts (EP4) and
  commands (EP5). Validation exists for report XML and event parsing, and
  barrier parameters are allowlisted (EP5 note above).
- **B4 Operator ↔ orchestrator.** CLI/env are trusted operator input; the
  orchestrator then acts with user-level authority (process kills, deletes,
  config rendering through `sb`). Secrets enter here and leave into the
  generated config, the `sb` argv (R2) and the telnet session.
- **B5 Installed mods ↔ client mod.** Provider discovery instantiates foreign
  code in-process (EP7); the built dist binary is trusted when installed.
- **B6 Peer agents ↔ lock file.** Coordination is cooperative: flock
  serialization plus heartbeat staleness (scripts/playtest_lock.py:474);
  takeover requires a stale heartbeat AND no live runtime process
  (`_runtime_pids`, :508).
- **B7 This host ↔ a third-party vision provider.** `video_review` shells out
  to the `deadeye` gateway CLI (`GATEWAY`, scripts/video_review.py:32), which
  owns the credential and the network hop. The uploaded payload is a whole clip
  directory, not just frames: the capture scripts copy the client log beside the
  mp4 (EP10), and that log contains remote-player chat (EP8). Consent is
  explicit (`--allow-network`, refused by default, scripts/video_review.py:403)
  and the disclosure names the logs (`:428`), but the trust decision itself is
  delegated to an executable resolved from `PATH` (`deadeye_available` :353).

Privilege transitions: barrier handling is where log parsing (untrusted) starts
acting with admin authority on the server (B3→B2); provider instantiation is
where installed-mod trust becomes in-game code execution (B5); `review_video`
is where a local artifact becomes a disclosure to a third party (B7).

## Threats per boundary (concrete)

- **B1 (spoofing, tampering, DoS):** any LAN host joins the passwordless game
  server (R3); remote players inject chat text that reaches ChatProbe and the
  client log (EP8→EP4); loadgen bots connect unauthenticated by design
  (start_loadgen, scripts/playtest_run.py:864).
- **B2 (spoofing, elevation):** a managed run is loopback-pinned and holds an
  ephemeral password. The residual cases are (a) an attach run against a
  host this repo does not configure, where the password is the only control
  left, and (b) the zdtd admin port, whose authentication is implemented in the
  zdtd repo, not here.
- **B3→B2 (tampering, elevation):** closed to command injection by the
  `safe_barrier_param` allowlist, which admits only identifier-shaped values
  (`zombieBoe`, `ptchat12345`) and drops anything else with a warning
  (scripts/playtest_run.py:3839-3847, :3880-3884). What remains: a valid but
  attacker-chosen *identifier* still reaches the console (a remote player can
  choose the entity class asked for), and each unique valid token opens a fresh
  admin session with no global rate cap. Countermeasures that also hold on this
  path: fire-once-per-token state (`:3846`, `:3866`), whole-name/prefix barrier
  matching with escaped regex (`barrier_line_hits` playtest_log.py:130,
  `barrier_hits_prefix` :120), and bounded recv windows (`TelnetAdmin`).
- **B3 (repudiation/integrity):** results are whatever the client printed (R4).
  Control characters from game and chat lines are stripped before they reach
  interactive stdout (`scrub`, scripts/playtest_run.py:491), while report
  JSON/XML keep raw detail and escape it structurally.
- **B4 (availability, misdirection, disclosure):** pkill patterns are
  constants (`GAME_PROC_PATTERNS` :263) but `kill_wine` extends them to
  wineserver and Steam launch helpers (:533); save and log removal is
  quarantine-first (`fresh_zdtd_world` :1784, `snapshot_previous_log` :1840),
  with `prune_quarantine` dropping entries beyond the newest five
  (`QUARANTINE_KEEP` :1676, :1732). The secret-in-argv exposure is R2.
- **B5 (elevation):** any mod in the instance's Mods folder that implements
  `IScenarioProvider` runs in-process with full client authority (EP7), and
  can emit passing log lines for work it skipped (R4).
- **B6 (denial of service against peers):** a live holder refreshing its
  heartbeat blocks other agents indefinitely (`HeartbeatThread`,
  scripts/playtest_lock.py:934; interval/staleness env-tunable :308, :312).
  Takeover of a crashed holder is guarded by the /proc liveness check, but a
  forged lock file could still misdirect a takeover (R6).
- **B7 (information disclosure, spoofing):** the uploaded client log carries
  remote-player chat, so a consented review can disclose third-party text the
  operator did not write and may not have seen. The gateway CLI is resolved
  from `PATH` (`deadeye_available`, scripts/video_review.py:353), so a hostile
  or shadowed `deadeye` earlier on `PATH` receives the whole payload and
  whatever credentials its own environment holds. The returned envelope is
  structurally validated before use (`:160`, `:304`) and this repo's copy is
  described as mirroring the gateway's canonical validator, so the local
  backstop is a second line, not the control.

DoS exposure of parsing is bounded: NRE sample cap and chat history cap
(playtest_log.py; Source/PlayTestMod/ChatProbe.cs:12, `Recent(32)`) and a
bounded recv chunk in `TelnetAdmin`. Polling is incremental (O(new bytes)) on
both wait paths (`LogTail.poll` playtest_log.py:361; `wait_file_contains`
scripts/playtest_run.py:558), so a chatty or hostile log does not cost
quadratic host CPU. The upload path is bounded by
`DEFAULT_TIMEOUT_SECONDS` (scripts/video_review.py:30) and refused without
consent.

## Mitigations map (existing controls)

| Control | Covers | Where |
|---------|--------|-------|
| `TelnetRemoteAllowedIPs=127.0.0.1`, set after the suite's server block so a suite cannot widen it | B2 admin plane reachable only from the host the orchestrator runs on | scripts/playtest_run.py:3167-3176 |
| Ephemeral per-run telnet secret when the operator supplies none; attach runs must supply one explicitly | a published default password, config/client divergence | `resolve_telnet_password` scripts/playtest_run.py:2168, `:2183`; README.md:1115-1129 |
| Telnet password excluded from the recorded run config | credential in shareable run reports | `args._applied_server_config` filter, scripts/playtest_run.py:3164 |
| Identifier allowlist on every log-derived barrier parameter | B3→B2 command injection via remote chat | `BARRIER_PARAM_RE` / `safe_barrier_param` scripts/playtest_run.py:2020, `:2023`; enforcement :3839, :3880; gate scripts/test_capture_video_surface.py neighbourhood and scripts/test_playtest_run_units.py |
| Remote chat text has no public accessor; only `LastLength` is exposed | third-party text reaching reports, JUnit and the review upload | Source/PlayTestMod/ChatProbe.cs:14-28; gate scripts/test_chat_probe_surface.py |
| XML attribute escaping + illegal-character stripping for JUnit and serverconfig generation | B3 log→report/config markup injection | `xml_attr` scripts/playtest_run.py:1306; gate scripts/test_report_surface.py |
| Event parser hardening: dict type check, scalar coercion, int/number guards catching TypeError/ValueError/OverflowError | B3 malformed or crafted event lines crashing the host | playtest_log.py:181-267; scripts/test_report_surface.py |
| Control-character stripping on interactive echoes | ANSI escape injection into the operator's terminal from game/chat output | `scrub` scripts/playtest_run.py:491 |
| Upload refused without explicit consent, and the consent prompt names the logs, not just the media | B7 undisclosed disclosure of a client log that carries peer chat | `run_review` consent gate scripts/video_review.py:403, disclosure :428; CLI flag scripts/review_video.py:74 |
| Gateway response structurally validated, timeout-bounded, fail-closed; a verdict is advisory and cannot mark a clip accepted | B7 trusting an unvalidated or hung third-party response | scripts/video_review.py:160, :304, :358, module docstring :1-12; gate scripts/test_video_review.py |
| Exclusivity lock acquired before any clean/launch; release refused unless held | B6 concurrent destructive cleanup | scripts/playtest_run.py:2227, release :4248; scripts/test_playtest_lock.py, DST simulation |
| Stale takeover gated on absence of live runtime processes | B6 wrongful takeover while a client lives | scripts/playtest_lock.py:474, `_runtime_pids` :508 |
| Post-clean double-bind refusal on ServerPort/admin port | B1/B2 orphan listeners racing new runs | `require_litenet_room` scripts/playtest_run.py:371, `require_telnet_port` :392 |
| Signal conversion so SIGTERM/SIGHUP unwind through cleanup (stop children, release lock) | availability / stale-lock wedge | `install_signal_handlers` scripts/playtest_run.py:1635 |
| Deterministic run-end marker (`run-ended`, reason on one line) cleared once the run holds the lock | consumers keying on run completion; a killed-before-poll run cannot poison a rerun | `write_run_ended_marker` / `clear_run_ended_marker` scripts/playtest_run.py:1692, :1699, :2958, :2984 |
| Suite-driven server hardening (`ServerVisibility=0`, `WebDashboardEnabled=false`, empty `ServerPassword`) | shrinks or widens B1 depending on how a suite is written; the values live in the suite, not in code | suites/smoke.json:23-31, suites/core.json:23-31, suites/parachute.json:26-37 |
| Unknown keys and cross-field contradictions refused when a suite is loaded | a misspelled hardening key reading as unset | scripts/suite_loader.py; gate scripts/test_suite_loader.py |
| Quarantine-before-delete for fresh saves, zdtd world reset and prior client logs; report/junit pruning bounded to newest 50 | B4 irreversible destruction by mispointed flags | `prune_run_artifacts` scripts/playtest_run.py:1712, `prune_quarantine` :1732, `fresh_zdtd_world` :1784; gate scripts/test_playtest_run_units.py |
| Incremental log readers (offset tail, complete-line buffering, shrink detection) | DoS-by-log-volume cost on shared host CPU | `LogTail` playtest_log.py:349-388; `wait_file_contains` scripts/playtest_run.py:558 |
| Third-party CI actions pinned by commit SHA; read-only workflow token; job timeouts | supply chain of build tooling; hung-run cost bound | .github/workflows/ci.yml:14, :25, :28-29, :56-62 |

## Claimed mitigations not enforced in this repo

Highest-value category; verify before relying on them.

- **The loopback pin is a game-side property.** README.md:1126-1128 now
  matches this repo's code (EP11, `:3175`), but whether the dedicated honours
  `TelnetRemoteAllowedIPs` at all is a behaviour of the game build and the
  template config, neither of which lives here. Until someone observes a
  refused non-local telnet source on a real run, treat the managed admin plane
  as *probably* loopback-only rather than proven.
- **An attach run has no such pin.** `--no-server` writes no config
  (scripts/playtest_run.py:3150), so against a production or third-party
  dedicated the only control left is the operator-supplied password. A
  `--readonly` run inherits the host's exposure entirely.
- **zdtd admin port "speaks the same command surface"** (comment in the
  orchestrator's zdtd path): authentication of that port is implemented in the
  zdtd repo, not here; the model records the boundary, not its strength.
- **The deadeye validator is described as mirroring the gateway's canonical
  one** (scripts/video_review.py:164, :304). Nothing here pins the two together,
  so a gateway upgrade can change the schema this backstop accepts. It fails
  closed on a mismatch, which is the intended direction, but a schema change
  becomes a refused review rather than a detected drift.

## Abuse cases (authenticated or hostile-but-authorized actors)

- **Joined LAN peer (R3):** anyone on the LAN joins the playtest instance with
  no password. They can then type chat that reaches the client log (EP8), the
  host's barrier matching (EP5), the copied capture log (EP10) and, on consent,
  a third-party provider (B7). They are not a console holder, so this is data
  injection and noise, not server authority.
- **Console holder:** anyone who obtains telnet access can kick players,
  teleport, set time, spawn entities or corrupt the save via `saveworld`; every
  `TelnetAdmin.exec` call site is such a capability (class at
  scripts/playtest_run.py:1349, call sites from :3834 through the barrier
  handlers). By design these are the fixture primitives; the abuse is
  unauthorized holders, not the commands.
- **Lock squatter:** an agent holding the lock and refreshing its heartbeat
  denies all other agents the shared runtime indefinitely (R6).
- **Self-grading client:** the client under test both performs cases and
  reports their outcome through the same log the host trusts (EP4/R4); a
  hostile provider (B5) could print passing lines for work it skipped. No
  server-side corroboration exists for most claims.
- **PATH shadowing on the upload path:** a `deadeye` binary placed earlier on
  `PATH` than the real gateway receives the clip directory and whatever its own
  environment holds, and returns the envelope the run then treats as evidence.
  `deadeye_available` is presence-only by design and never verifies provenance
  (scripts/video_review.py:353).

No attack was demonstrated or executed while building this document; all
evidence is read from code.

## Response readiness (notes only)

- No SECURITY.md exists: there is no recorded disclosure contact or
  supported-version statement. Creating one requires organizational decisions
  this document does not make.
- Audit trail: run evidence lives in LOGDIR reports/logs and quarantine
  snapshots, but retention is bounded by design (newest 50 report/junit pairs
  `prune_run_artifacts`, scripts/playtest_run.py:1712; newest 5 quarantine
  entries `QUARANTINE_KEEP` :1676) and lock takeovers and cleans log to
  orchestrator stdout only. Enough to answer "what happened this run", thin for
  post-hoc incident review. Captured video, contact sheets and review envelopes
  are the exception: they live only under `.local/capture/<suite>-<stamp>/`,
  which nothing copies off the host, and a re-run does not reproduce the same
  frame.

## Out of scope here

- Fixing R1/R2/R3/R5 (sec-review; managed-instance hardening lives with the
  Safehouse and game-template owners).
- CVE inventory of dependencies (deps-review); PII/compliance mapping
  (privacy-review); log structure standards (o11y-review); general docs prose
  (doc-review).
