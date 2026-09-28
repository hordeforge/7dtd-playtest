# Threat model: 7dtd-playtest

Living threat model, built from code on this tree. Scope: the host
orchestrator (`scripts/playtest_run.py` and helpers), the run-artifact
tooling (`scripts/capture_*.sh`, `scripts/playtest_repeat.sh`,
`scripts/review_video.py`, `scripts/video_review.py`, `scripts/playtest_compare.py`)
and the client mod (`Source/PlayTestMod/`).
Individual vulnerabilities are not fixed here; each risk below names where a
fix belongs (sec-review or the owning repo).

- Last reviewed: 2026-09-28 (against commit 38315fa)
- Owner: organizational; no named owner or review cadence is recorded yet.
- Re-verify every file reference below against the current tree before acting.

## Risk-ranked summary

| # | Risk | Boundary | Exploit / impact | Status |
|---|------|----------|------------------|--------|
| R1 | The per-run telnet secret is handed to a subprocess in argv | B4 → B11 | The generated server config is rendered by passing `k=v` pairs to `sb render-config` (`scripts/playtest_targets.py:309`), and that dict carries `TelnetPassword` (`scripts/playtest_run.py:3531`). `_run_sb` runs it through `subprocess.run(["bash", sb, *argv])` (`scripts/playtest_targets.py:427`, `:437`), so the secret is readable from `/proc/<pid>/cmdline` by any local user for the life of that call, on a machine running several agents. README.md:219 tells the operator to prefer the env var because `--telnet-password` is "visible in process listings"; this path reintroduces exactly that exposure for the generated password. It is kept out of the run *report* (`scripts/playtest_run.py:3538`), not out of argv | Gap, report to sec-review |
| R2 | Run artifacts, including the client log, are uploaded to a third-party vision provider | B7 | `review_video` sends a whole clip directory, and the capture scripts leave a copy of the client log beside the mp4 (`scripts/capture_frames.sh:221`, `scripts/capture_video.sh:262`). That log carries whatever remote LAN players typed (EP8). Consent-gated and the disclosure names the logs (`scripts/review_video.py:77`), but a reviewer consenting to "review my clip" is also consenting to shipping a peer chat transcript | Gap: mitigation exists, operator-facing description is where the risk lives |
| R3 | Game server joins open to the LAN with an empty join password | B1 | A hostile host on the same LAN joins every playtest instance: `ServerPassword` is empty in each suite's server block (`suites/smoke.json:30`, `suites/core.json:30`, `suites/parachute.json:36`) and the templates enable LAN platforms. A joined peer is a peer the client-mod threat model below has to assume | Gap, accepted for a disposable test world; does not apply to `--readonly` hosts |
| R4 | Verdicts are self-reported by the client under test | B3 | A buggy or compromised client grades itself PASS; host decisions inherit that | Accepted by design, note |
| R5 | Mod supply chain executes inside the game client | B5 | Any installed assembly implementing `IScenarioProvider` is auto-instantiated (`Source/PlayTestMod/ScenarioProvider.cs:56`); the built dist DLL is installed into Mods unverified | Gap (accepted for dev use) |
| R6 | Lock tampering / stale-takeover races redirect destructive cleanup | B6 | A wrong takeover lets one agent stop another's client or move save data aside. Managed runs no longer sweep by pattern: `game_sweep_patterns` returns an empty list for a sandbox plan (`scripts/playtest_run.py:634`) and the client is stopped through its own instance (`stop_sandbox_client`, `scripts/playtest_targets.py:417`). Residual risk is the lock file itself, which is peer-written | Mitigated, residual risk |
| R7 | Availability: broad process kills and destructive moves | B4 | A shared-client or zdtd run's clean kills by command-line pattern (`clean_processes`, `scripts/playtest_run.py:609`); `kill_wine` extends that to wineserver and the Steam launch helpers (`:621-625`); `--fresh-save` moves saves into quarantine, recorded before each move and recoverable only until pruned and until someone runs the restore tool | Mitigated (quarantine + `scripts/quarantine_restore.py`), operator-scoped residual |
| R8 | The lap gate reads a shared report directory, so another session's report can stand in for this run's | B4 | `scripts/playtest_compare.py` picks the newest `report-*.json` by mtime alone (`newest_report`, `scripts/playtest_compare.py:254-268`), with no owner or run marker. The default report dir is shared (`LOGDIR`, `~/.cache/7dtd-playtest`), so a concurrent run on this host publishes a report the comparison will happily diff against. `scripts/playtest_repeat.sh` closes the same hole for its own laps by snapshotting the pre-existing report set (`snapshot_reports`, `scripts/playtest_repeat.sh:109`) and keeping only reports its lap created | Gap for the compare path; repeat path mitigated |
| R9 | The desktop audio sink is recorded for the length of a run | B4 | `scripts/capture_audio.sh` records the default sink's monitor for a whole suite run (`scripts/capture_audio.sh:3-13`). That is operator audio, not game audio: anything the machine plays during the run lands in `.local/capture/` and, if a clip directory is reviewed, can travel outward on the B7 path. The script records what the run sounded like; it does not scope the recording to the game | Gap, operator-facing; no consent gate for the audio itself |
| R10 | No disclosure path; audit trail is bounded run artifacts | - | No SECURITY.md exists; evidence lives under LOGDIR and `.local/capture/`, both on one local disk, and is pruned | Note only |

Closed since the last review, kept here so the next pass does not re-raise it:

- **Admin plane is loopback-pinned.** The generated config sets
  `TelnetRemoteAllowedIPs=127.0.0.1` alongside `TelnetEnabled` (`scripts/playtest_run.py:3529-3530`), *after* the suite's own server block is copied in so a suite cannot widen it. A suite that tried to widen it is refused at load (`ORCHESTRATOR_TELNET_KEYS`, `scripts/suite_loader.py:83`; refusal `scripts/suite_loader.py:251`).
- **Telnet password is ephemeral per run.** An unset
  `PLAYTEST_TELNET_PASSWORD` becomes an ephemeral per-run secret
  (`resolve_telnet_password`, `scripts/playtest_run.py:2491`).
  There is no shipped default on this path. Only `--no-server` runs require an
  operator-supplied value (`scripts/playtest_run.py:3047`, README.md:1190-1193).
- **Log-derived barrier parameters are allowlisted.** `chat_echo:` and
  `spawn_vehicle:` parameters must match `[A-Za-z0-9_]{1,64}` before they reach
  a telnet command (`BARRIER_PARAM_RE` / `safe_barrier_param`,
  `scripts/playtest_run.py:2320`, `:2330`; dropped tokens warn at `:4216` and
  `:4254`). A crafted log line can no longer append console commands. The
  amplification residue is a fresh admin session per unique valid token, with
  no global rate cap.
- **Managed runs never kill a peer by pattern.** `game_sweep_patterns`
  (`scripts/playtest_run.py:634`) is the single decision point every kill site
  asks, and `stop_run_client` (`:651`) falls back to `sb stop <client>` for a
  managed instance.

## Assets

- The workstation runtime: the orchestrator holds kill authority over game,
  wine/Proton and Steam-adjacent processes (`GAME_PROC_PATTERNS`
  `scripts/playtest_run.py:293`, `clean_processes` `:609`, `kill_wine` branch
  `:621`, teardown `stop_proc` `:1369`) and move/delete authority over saves
  and logs (`prune_quarantine` `:1984`, `fresh_zdtd_world` `:2066`,
  `snapshot_previous_log` `:2122`).
- Telnet/admin credential: `PLAYTEST_TELNET_PASSWORD` / `--telnet-password`
  (`resolve_telnet_password`, `scripts/playtest_run.py:2491`). Enters from env
  or argv, becomes an ephemeral per-run secret when unset, is sent cleartext
  over the telnet socket (`TelnetAdmin`, `:1564`), and reaches the generated
  server config both through the `sb render-config` argv (R1) and on disk under
  userdata. Startup logs redact it to set/unset (`config_summary`, `:459`, value
  state at `:468`). No rotation point: the value is valid for the life of the
  run and dies with the instance.
- Remote player chat: captured in client memory by a Harmony patch
  (`Patch_ChatMessageClient_Probe`, `Source/PlayTestMod/ChatProbe.cs:89`;
  buffer at `:13`). It is third-party text this repo holds, which is why it
  appears in R2 and B7.
- Operator audio on the desktop sink: recorded verbatim for a run
  (`scripts/capture_audio.sh`), stored beside the other capture artifacts, and
  therefore eligible for the B7 upload path (R9).
- Game world / save state integrity (reproducibility of runs): userdata Saves
  tree, zdtd world dir (`fresh_zdtd_world`, `scripts/playtest_run.py:2066`).
- Run verdicts and reports (gate CI-style decisions downstream): JSON/JUnit
  reports written to LOGDIR (`write_report` `:1433`, `write_junit` `:1518`)
  and compared across runs by `scripts/playtest_compare.py`; baselines are
  committed under `workspace/comparison-playtest/`.
- Capture artifacts: frames, mp4, contact sheets, audio and the copied client
  log under `.local/capture/`, uploaded outward on the B7 path.

## Entry points

| EP | Surface | Where |
|----|---------|-------|
| EP1 | CLI arguments (ports, paths, suite, password, session) | argparse parsed at `scripts/playtest_run.py:2930`, consumed in `main` `:2594` |
| EP2 | Environment variables (orchestrator + mod): ports, paths, timeout, lock knobs, `PLAYTEST_SUITE*` | orchestrator defaults from `:2594`ff and `seconds_from_env`; lock knobs `scripts/playtest_lock.py:316`, `:321`; mod reads via `EnvFirst`, `Source/PlayTestMod/Runner.cs:60`, `:66`, callers `:89`-`:95` |
| EP3 | Network listeners started as side effects: stock dedicated game port (+2 LiteNet for loadgen), telnet port = `--admin-port`; zdtd `--port` + `--admin-port` | `start_zdtd` `scripts/playtest_run.py:761`; `start_loadgen` `:997` (litenet = port+offset, room check `require_litenet_room` `:401`); managed stock listeners are opened by `sb up` (`scripts/playtest_targets.py:312`) |
| EP4 | Client log parsed as data (game output incl. remote chat lines, mod detail strings, JSON event lines) | `ClientLogScan.feed_line` `scripts/playtest_log.py:181`; `parse_client_log` `:310`; `LogTail.poll` `:416`; host poll loops `scripts/playtest_run.py` `pump_log_tail` |
| EP5 | Barrier lines in that log trigger privileged telnet actions (spawn/kill/settime/say/teleport/bot commands) | `service_barrier` `scripts/playtest_run.py:3449`; `chat_echo` handler `:4205-4238`; `spawn_vehicle` handler `:4240-4258` |
| EP6 | Lock file read/write across agents (parseable key=value, atomic tmp+rename publish) | `scripts/playtest_lock.py` `read_lock` `:397`, `write_lock` `:439`, `is_stale` `:481`, /proc liveness probes `_runtime_pids` `:515`ff |
| EP7 | Scenario providers: any loaded mod assembly implementing `IScenarioProvider` | `Source/PlayTestMod/ScenarioProvider.cs:56` (`Activator.CreateInstance`) |
| EP8 | Remote player chat captured via Harmony patch into harness memory and matched against tokens | `Source/PlayTestMod/ChatProbe.cs:13`, `:89` |
| EP9 | Deployment artifacts: CI workflow (pinned third-party actions), Makefile targets that launch servers, built dist DLL installed into Mods | `.github/workflows/ci.yml:14`, `:32`, `:35-36`, `:67`, `:72-73`; Makefile `install`/`playtest*` targets; dist/ (build output, not committed) |
| EP10 | Run-artifact capture and upload: the capture scripts write a clip directory, `review_video` submits it to an external gateway CLI | `scripts/capture_frames.sh:221`, `scripts/capture_video.sh:247`, `:262`, `scripts/review_video.py:34`, `scripts/video_review.py:529` |
| EP11 | Safehouse (`sb`) as a subprocess boundary: every managed-stock lifecycle step is an `sb` call, and suite server-block keys become `k=v` argv | `scripts/playtest_targets.py` `ensure_sandbox_server` `:279`, `render-config` `:309`, `_run_sb` `:427` |
| EP12 | Review envelopes on disk: every `review-*.json` under `--attach-reviews` is read, size-checked and shape-checked, and its path is recorded in the run report | `collect_visual_reviews` `scripts/playtest_run.py:1450`, cap `MAX_VISUAL_REVIEW_BYTES` `:1447`, report field `:4442` |
| EP13 | Desktop audio capture: the default sink's monitor for the length of a run | `scripts/capture_audio.sh` |
| EP14 | Shared report directory read by the lap/compare tooling | `scripts/playtest_repeat.sh:109`, `:122`; `scripts/playtest_compare.py:134`, `:254`; `scripts/report_summary.py:26` |
| EP15 | Lap baselines: committed comparison baselines that a verdict is measured against | `workspace/comparison-playtest/` (per-suite subdirectories) |

Trusted-by-convention inputs that cross a boundary: EP4 log content carries
remote-player chat text (EP8 feeds it) and is treated as instructions by EP5;
the lock file (EP6) is written by other agents and gates destructive cleanup;
the suite document's `server` block is operator input that this repo forwards
to the instance config verbatim apart from the telnet keys (EP11); a review
envelope (EP12) is a model-influenced document whose `intent.suite`/`intent.case`
are echoed into the report as a key.

## Trust boundaries

- **B1 Remote/LAN players ↔ game server/client.** UDP game protocol and chat;
  entry via EP3 ports. Suites set `ServerVisibility=0` and an empty
  `ServerPassword` (`suites/smoke.json:23`, `:30`) while the instance templates
  enable LAN platforms, so visibility is not reachability. `ServerVisibility=0`
  only hides the server from browsing.
- **B2 Orchestrator ↔ server admin plane.** TCP telnet (stock; the orchestrator
  always connects from `127.0.0.1`) or the zdtd admin port, password auth via
  `TelnetAdmin.connect` (`scripts/playtest_run.py:1586`). On a managed sandbox
  run the listener is pinned to loopback (`:3530`); on an **attach** run this
  repo writes no config at all, so the exposure is whatever the host already
  runs.
- **B3 Client log ↔ host automation.** File bytes become verdicts (EP4),
  commands (EP5) and report keys (EP12). Validation exists for report XML and
  event parsing, barrier parameters are allowlisted (EP5 note above), and review
  envelopes are size-capped and shape-checked level by level.
- **B4 Operator ↔ orchestrator.** CLI/env are trusted operator input; the
  orchestrator then acts with user-level authority (process kills, deletes,
  config rendering through `sb`, audio capture, report-directory reads).
  Secrets enter here and leave into the generated config, the `sb` argv (R1)
  and the telnet session.
- **B5 Installed mods ↔ client mod.** Provider discovery instantiates foreign
  code in-process (EP7); the built dist binary is trusted when installed.
- **B6 Peer agents ↔ lock file.** Coordination is cooperative: flock
  serialization plus heartbeat staleness (`scripts/playtest_lock.py:481`);
  takeover requires a stale heartbeat AND no live runtime process
  (`_runtime_pids`, `:515`).
- **B7 This host ↔ a third-party vision provider.** `video_review` shells out
  to the `deadeye` gateway CLI (`GATEWAY`, `scripts/video_review.py:33`), which
  owns the credential and the network hop. The uploaded payload is a whole clip
  directory, not just frames: the capture scripts copy the client log beside the
  mp4 (EP10), and that log contains remote-player chat (EP8). Consent is
  explicit (`--allow-network`, refused by default, `scripts/review_video.py:74`)
  and the disclosure names the logs (`scripts/review_video.py:77`), but the
  trust decision itself is delegated to an executable resolved from `PATH`
  (`deadeye_available`, `scripts/video_review.py:440`).
- **B11 This repo ↔ the Safehouse CLI.** `sb` is a shell script on the
  operator's disk, invoked with the orchestrator's user authority and handed
  the full server config, including the telnet password (R1). Its own
  validation is not in this tree.

Privilege transitions: barrier handling is where log parsing (untrusted) starts
acting with admin authority on the server (B3→B2); provider instantiation is
where installed-mod trust becomes in-game code execution (B5); `review_video`
is where a local artifact becomes a disclosure to a third party (B7); the
`render-config` call is where an env-held secret becomes a world-readable
argument vector (B4→B11); a client-declared stage name becomes a directory this
repo reads back off disk (B5→B3, via `Helpers.AssetName`
`Source/PlayTestMod/Helpers.Ui.cs:361`).

## Threats per boundary (concrete)

- **B1 (spoofing, tampering, DoS):** any LAN host joins the passwordless game
  server (R3); remote players inject chat text that reaches ChatProbe and the
  client log (EP8→EP4); loadgen bots connect unauthenticated by design
  (`start_loadgen`, `scripts/playtest_run.py:997`).
- **B2 (spoofing, elevation):** a managed run is loopback-pinned and holds an
  ephemeral password. The residual cases are (a) an attach run against a
  host this repo does not configure, where the password is the only control
  left, and (b) the zdtd admin port, whose authentication is implemented in the
  zdtd repo, not here.
- **B3→B2 (tampering, elevation):** closed to command injection by the
  `safe_barrier_param` allowlist, which admits only identifier-shaped values
  (`zombieBoe`, `ptchat12345`) and drops anything else with a warning
  (`scripts/playtest_run.py:2330`, `:4216`, `:4254`). What remains: a valid
  but attacker-chosen *identifier* still reaches the console (a remote player
  can choose the entity class asked for), and each unique valid token opens a
  fresh admin session with no global rate cap. Countermeasures that also hold on
  this path: fire-once-per-token state, whole-name/prefix barrier matching with
  escaped regex (`barrier_line_hits` `playtest_log.py:130`, `barrier_hits_prefix`
  `:120`), and bounded recv windows (`TelnetAdmin._recv`,
  `scripts/playtest_run.py:1778`).
- **B3 (repudiation/integrity):** results are whatever the client printed (R4),
  and the report's visual-review map is whatever `review-*.json` files happen to
  sit under the attached directory (EP12); only paths are recorded, never a
  verdict, so a forged envelope can change what the report *points at* but not
  what it *concludes* (`collect_visual_reviews` docstring,
  `scripts/playtest_run.py:1450`). Control characters from game and chat lines
  are stripped before they reach interactive stdout (`scrub`,
  `scripts/playtest_run.py:524`), while report JSON/XML keep raw detail and
  escape it structurally (`xml_attr` `:1507`).
- **B4 (availability, misdirection, disclosure):** pkill patterns are
  constants (`GAME_PROC_PATTERNS` `:293`) but `clean_processes` extends them to
  the dedicated, zdtd and, with `kill_wine`, to wineserver and the Steam
  launch helpers (`:609`, `:621`); a managed run is the exception and kills
  nothing by pattern (`game_sweep_patterns` `:634`). Save and log removal is
  quarantine-first (`fresh_zdtd_world` `:2066`, `snapshot_previous_log`
  `:2122`), with `prune_quarantine` dropping entries beyond the newest five
  (`QUARANTINE_KEEP` `:1913`, `:1984`). Reading the shared report directory is
  R8; recording the audio sink is R9. The secret-in-argv exposure is R1.
- **B5 (elevation):** any mod in the instance's Mods folder that implements
  `IScenarioProvider` runs in-process with full client authority (EP7), and
  can emit passing log lines for work it skipped (R4).
- **B6 (denial of service against peers):** a live holder refreshing its
  heartbeat blocks other agents indefinitely (`HeartbeatThread`,
  `scripts/playtest_lock.py:951`; interval/staleness env-tunable `:316`,
  `:321`). Takeover of a crashed holder is guarded by the /proc liveness check,
  but a forged lock file could still misdirect a takeover (R6).
- **B7 (information disclosure, spoofing):** the uploaded client log carries
  remote-player chat, so a consented review can disclose third-party text the
  operator did not write and may not have seen; a clip directory taken from a
  run captured with `capture_audio.sh` carries operator audio too (R9). The
  gateway CLI is resolved from `PATH` (`deadeye_available`,
  `scripts/video_review.py:440`), so a hostile or shadowed `deadeye` earlier on
  `PATH` receives the whole payload and whatever credentials its own
  environment holds. The returned envelope is structurally validated before use
  (`validate_result`, `scripts/video_review.py:225`) and this repo's copy is
  described as mirroring the gateway's canonical validator, so the local
  backstop is a second line, not the control.
- **B11 (disclosure, tampering):** `sb render-config` receives the whole
  server config as arguments (R1). `sb` is invoked as `bash <path>` with no
  integrity check on the script itself, so a modified `sb` in the operator's
  sandbox checkout receives the password and controls the instance lifecycle.

DoS exposure of parsing is bounded: NRE sample cap (`NRE_SAMPLE_CAP` 50,
`playtest_log.py:63`, applied `:270`), a bounded chat history (`Recent`
capacity 32, `Source/PlayTestMod/ChatProbe.cs:13`), a per-envelope byte cap on
review files (`MAX_VISUAL_REVIEW_BYTES`, `scripts/playtest_run.py:1447`) and a
bounded recv chunk in `TelnetAdmin._recv` (`scripts/playtest_run.py:1778`).
Two bounds are partial and named as such: the review scan is `rglob` over a
directory tree with a per-file cap and no file-count or depth cap
(`scripts/playtest_run.py:1450`), and the barrier path has no global rate cap
across unique tokens. Polling is incremental (O(new bytes)) on both wait paths
(`LogTail.poll` `playtest_log.py:416`; `wait_file_contains`
`scripts/playtest_run.py:687`), so a chatty or hostile log does not cost
quadratic host CPU. The upload path is bounded by `DEFAULT_TIMEOUT_SECONDS`
(`scripts/video_review.py:31`) and refused without consent.

## Mitigations map (existing controls)

| Control | Covers | Where |
|---------|--------|-------|
| `TelnetRemoteAllowedIPs=127.0.0.1`, set after the suite's server block so a suite cannot widen it | B2 admin plane reachable only from the host the orchestrator runs on | `scripts/playtest_run.py:3529-3530`; loader refusal `scripts/suite_loader.py:83`, `:251` |
| Ephemeral per-run telnet secret when the operator supplies none; attach runs must supply one explicitly | a published default password, config/client divergence | `resolve_telnet_password` `scripts/playtest_run.py:2491`; attach requirement `:3047`; README.md:1185-1198 |
| Telnet password excluded from the recorded run config | credential in shareable run reports | `args._applied_server_config` filter, `scripts/playtest_run.py:3538`; report field `:4396` |
| Suite-declared admin keys refused at load | a suite declaring its own `TelnetEnabled` / `TelnetRemoteAllowedIPs` / `TelnetPassword` | `scripts/suite_loader.py:83`, `:251`; gate `scripts/test_suite_loader.py` |
| Identifier allowlist on every log-derived barrier parameter | B3→B2 command injection via remote chat | `BARRIER_PARAM_RE` / `safe_barrier_param` `scripts/playtest_run.py:2320`, `:2330`; drop-and-warn `:4216`, `:4254` |
| Remote chat text has no public accessor; only `LastLength` is exposed | third-party text reaching reports, JUnit and the review upload | `Source/PlayTestMod/ChatProbe.cs:29`; gate `scripts/test_chat_probe_surface.py` |
| XML attribute escaping + illegal-character stripping for JUnit and serverconfig generation | B3 log→report/config markup injection | `xml_attr` `scripts/playtest_run.py:1507`; gate `scripts/test_report_surface.py` |
| Event parser hardening: dict type check, scalar coercion, int/number guards catching TypeError/ValueError/OverflowError | B3 malformed or crafted event lines crashing the host | `playtest_log.py:181-270`; `scripts/test_report_surface.py` |
| Review envelopes capped, UTF-8 decoded, JSON-parsed and shape-checked at every level; only paths recorded | a hostile or truncated envelope rewriting the report's evidence map or exhausting host memory | `collect_visual_reviews` `scripts/playtest_run.py:1450`, cap `:1447`; warning on every skip `:1470`, `:1479`, `:1482` |
| Control-character stripping on interactive echoes | ANSI escape injection into the operator's terminal from game/chat output | `scrub` `scripts/playtest_run.py:524` |
| Upload refused without explicit consent, and the consent prompt names the logs, not just the media | B7 undisclosed disclosure of a client log that carries peer chat | `run_review` consent gate `scripts/video_review.py:468`, CLI flag `scripts/review_video.py:74`, text `:77` |
| Gateway response structurally validated, timeout-bounded, fail-closed; a verdict is advisory and cannot mark a clip accepted | B7 trusting an unvalidated or hung third-party response | `scripts/video_review.py:31`, `:225`, `:468`; gate `scripts/test_video_review.py` |
| Lap aggregation only counts reports its own lap created, decided by a pre-lap snapshot of the report dir | a concurrent session's report counting as this lap's result (R8, repeat path) | `snapshot_reports` / `latest_report` `scripts/playtest_repeat.sh:109`, `:122` |
| Exclusivity lock acquired before any clean/launch; release refused unless held | B6 concurrent destructive cleanup | `acquire_exclusive_lock` `scripts/playtest_run.py:2529`, `playtest_lock.acquire` `:2550`, release `:2555`, `:4639`; `scripts/test_playtest_lock.py`, DST simulation |
| Stale takeover gated on absence of live runtime processes | B6 wrongful takeover while a client lives | `scripts/playtest_lock.py:481`, `_runtime_pids` `:515` |
| Managed runs stop their own client by instance; no pattern sweep on the sandbox path | B4 killing a concurrent run's client or dedicated | `game_sweep_patterns` `scripts/playtest_run.py:634`, `stop_run_client` `:651`, `stop_sandbox_client` `scripts/playtest_targets.py:417` |
| Post-clean double-bind refusal on ServerPort/admin port | B1/B2 orphan listeners racing new runs | `require_litenet_room` `scripts/playtest_run.py:401`, `require_telnet_port` `:422` |
| Signal conversion so SIGTERM/SIGHUP unwind through cleanup (stop children, release lock) | availability / stale-lock wedge | `install_signal_handlers` `scripts/playtest_run.py:1872` |
| Deterministic run-end marker (`run-ended`, reason on one line) cleared once the run holds the lock | consumers keying on run completion; a killed-before-poll run cannot poison a rerun | `write_run_ended_marker` / `clear_run_ended_marker` `scripts/playtest_run.py:1941`, `:1951` |
| Suite-driven server hardening (`ServerVisibility=0`, `WebDashboardEnabled=false`, empty `ServerPassword`) | shrinks or widens B1 depending on how a suite is written; the values live in the suite, not in code | `suites/smoke.json:23`, `:30`; `suites/core.json:23`, `:30`; `suites/parachute.json:26`, `:36` |
| Unknown keys and cross-field contradictions refused when a suite is loaded | a misspelled hardening key reading as unset | `scripts/suite_loader.py`; gate `scripts/test_suite_loader.py` |
| Quarantine-before-delete for fresh saves, zdtd world reset and prior client logs; report/junit pruning bounded to newest 50; every move recorded (and fsynced, directory included) before the file leaves its path, so a quarantined world can be put back | B4 irreversible destruction by mispointed flags | `prune_run_artifacts` `scripts/playtest_run.py:1964`, `prune_quarantine` `:1984`, `fresh_zdtd_world` `:2066`; `scripts/quarantine_restore.py` (`restore.jsonl` manifest); gates `scripts/test_playtest_run_units.py`, `scripts/test_quarantine_restore.py` |
| Incremental log readers (offset tail, complete-line buffering, shrink detection) | DoS-by-log-volume cost on shared host CPU | `LogTail` `playtest_log.py:369`, `:416`; `wait_file_contains` `scripts/playtest_run.py:687` |
| Third-party CI actions pinned by commit SHA; read-only workflow token; job timeouts | supply chain of build tooling; hung-run cost bound | `.github/workflows/ci.yml:14`, `:32`, `:35-36`, `:67`, `:72-73` |
| Clip ids sanitized to one name (NFC, ASCII letters/digits/`-`/`_`), and a surviving Windows device name prefixed `_` | a suite-declared id writing outside the shots root or naming a device the Windows client cannot create | `Helpers.AssetName` `Source/PlayTestMod/Helpers.Ui.cs:361`, `ReservedDeviceNames` `:311`; gate `scripts/test_windows_path_surface.py` |

## Claimed mitigations not enforced in this repo

Highest-value category; verify before relying on them.

- **README.md:219 tells the operator that the env var is preferred because
  argv is "visible in process listings", and README.md:1185-1198 describes the
  ephemeral password as written into the generated config and used by the
  orchestrator. Neither mentions that the orchestrator re-exposes that password
  in argv to `sb render-config`** (R1, `scripts/playtest_targets.py:309`).
  The documented mitigation is real for the operator's input path and defeated
  by the output path; treat the secret as local-user-readable for the duration
  of a managed run.
- **The loopback pin is a game-side property.** README.md:1194-1198 matches
  this repo's code (EP11, `scripts/playtest_run.py:3530`), but whether the
  dedicated honours `TelnetRemoteAllowedIPs` at all is a behaviour of the game
  build and the template config, neither of which lives here. Until someone
  observes a refused non-local telnet source on a real run, treat the managed
  admin plane as *probably* loopback-only rather than proven.
- **An attach run has no such pin.** An attach run writes no config, so
  against a production or third-party dedicated the only control left is the
  operator-supplied password. A `--readonly` run inherits the host's exposure
  entirely.
- **zdtd admin port "speaks the same command surface"**: authentication of that
  port is implemented in the zdtd repo, not here; the model records the
  boundary, not its strength.
- **The deadeye validator is described as mirroring the gateway's canonical
  one** (`scripts/video_review.py` module docstring and `validate_result`,
  `:225`). Nothing here pins the two together, so a gateway upgrade can change
  the schema this backstop accepts. It fails closed on a mismatch, which is the
  intended direction, but a schema change becomes a refused review rather than
  a detected drift.
- **The generated server config is written chmod 0600** per README.md:1187-1188.
  That permission is applied by the Safehouse/template path, not by any code in
  this tree, so this repo cannot verify it.
- **The CI token is described as never entering a remote URL**
  (`.github/workflows/ci.yml:87`, `:95`). It is a header on an authenticated
  request, which this repo does not own, so treat it as a claim about the
  fetch step rather than a control enforced here.
- **Compare baselines are committed, so a verdict is measured against a fixed
  reference.** Nothing signs or revalidates them at load time
  (`scripts/playtest_compare.py:134`): an edit to
  `workspace/comparison-playtest/` is a change to what "no regression" means,
  and the gate that publishes them checks publication mechanics, not content.

## Abuse cases (authenticated or hostile-but-authorized actors)

- **Joined LAN peer (R3):** anyone on the LAN joins the playtest instance with
  no password. They can then type chat that reaches the client log (EP8), the
  host's barrier matching (EP5), the copied capture log (EP10) and, on consent,
  a third-party provider (B7). They are not a console holder, so this is data
  injection and noise, not server authority.
- **Console holder:** anyone who obtains telnet access can kick players,
  teleport, set time, spawn entities or corrupt the save via `saveworld`; every
  `TelnetAdmin.exec` call site is such a capability (class at
  `scripts/playtest_run.py:1564`, `exec` `:1603`, call sites from `:3468`
  through the barrier handlers). By design these are the fixture primitives;
  the abuse is unauthorized holders, not the commands.
- **Lock squatter:** an agent holding the lock and refreshing its heartbeat
  denies all other agents the shared runtime indefinitely (R6).
- **Self-grading client:** the client under test both performs cases and
  reports their outcome through the same log the host trusts (EP4/R4); a
  hostile provider (B5) could print passing lines for work it skipped. No
  server-side corroboration exists for most claims.
- **Concurrent report forger (R8):** anything that can write into the shared
  `LOGDIR` can drop a `report-*.json` newer than this run's, and
  `scripts/playtest_compare.py` will read it as this run's result. The
  threshold is a directory write, not a game capability.
- **PATH shadowing on the upload path:** a `deadeye` binary placed earlier on
  `PATH` than the real gateway receives the clip directory and whatever its own
  environment holds, and returns the envelope the run then treats as evidence.
  `deadeye_available` is presence-only by design and never verifies provenance
  (`scripts/video_review.py:440`).
- **Local user reading the process table:** on a machine running several
  agents, the per-run telnet secret is readable from argv for the duration of
  the `sb render-config` call (R1), and the lock file path plus session id
  announce which instance a run is driving.
- **Host audio (R9):** `capture_audio.sh` records whatever the desktop plays
  for the run's duration. Whoever can read `.local/capture/` gets that audio,
  and a consented review ships it outward with the clip.

No attack was demonstrated or executed while building this document; all
evidence is read from code.

## Response readiness (notes only)

- No SECURITY.md exists: there is no recorded disclosure contact or
  supported-version statement. Creating one requires organizational decisions
  this document does not make.
- Audit trail: run evidence lives in LOGDIR reports/logs and quarantine
  snapshots, but retention is bounded by design (newest 50 report/junit pairs
  `prune_run_artifacts`, `scripts/playtest_run.py:1964`; newest 5 quarantine
  entries `QUARANTINE_KEEP` `:1913`) and lock takeovers and cleans log to
  orchestrator stdout only. Enough to answer "what happened this run", thin for
  post-hoc incident review. Captured video, audio, contact sheets and review
  envelopes are the exception: they live only under `.local/capture/<suite>-<stamp>/`,
  which nothing copies off the host, and a re-run does not reproduce the same
  frame.

## Out of scope here

- Fixing R1/R2/R3/R5/R8/R9 (sec-review; managed-instance hardening lives with
  the Safehouse and game-template owners).
- CVE inventory of dependencies (deps-review); PII/compliance mapping
  (privacy-review); log structure standards (o11y-review); general docs prose
  (doc-review).
