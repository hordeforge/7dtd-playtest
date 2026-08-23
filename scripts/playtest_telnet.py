"""Stock dedicated / zdtd telnet admin transport for the playtest orchestrator.

Self-contained connection + command layer; barrier orchestration (when to
spawn, kill, teleport) stays in ``playtest_run``. Log lines keep the
``[playtest-orch]`` prefix so host log scraping is unchanged.
"""

from __future__ import annotations

import re
import socket
import time


def log(msg: str) -> None:
    print(f"[playtest-orch] {msg}", flush=True)


class TelnetAdmin:
    """Minimal stock dedicated telnet (password prompt)."""

    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self._sock: socket.socket | None = None

    def connect(self, timeout: float = 5.0) -> bool:
        try:
            self.close()
            s = socket.create_connection((self.host, self.port), timeout=timeout)
            s.settimeout(2.0)
            self._sock = s
            banner = self._recv(0.8)
            if "password" in banner.lower():
                self._send(self.password)
                _ = self._recv(0.6)
            log(f"telnet connected {self.host}:{self.port} banner={banner[:60]!r}")
            return True
        except OSError as ex:
            log(f"telnet connect fail: {ex}")
            self.close()
            return False

    def exec(self, cmd: str) -> str:
        if not self._sock:
            return ""
        try:
            self._send(cmd)
            # zdtd admin is polled on the 20 Hz tick; allow a few frames.
            return self._recv(1.2)
        except OSError as ex:
            log(f"telnet exec fail: {ex}")
            return ""

    def clear_ai(self) -> None:
        """Remove non-player AI without killing the human player.

        Do **not** use stock `killall` here: it also kills the player entity and
        leaves the demo stuck on a death screen.
        """
        # listents lines vary; kill known zombie class names near world if present.
        out = self.exec("listents")
        # Avoid low / player-looking ids if we can: players often 171 etc. We only
        # kill when the line also looks like a zombie/animal.
        killed = 0
        for line in out.splitlines():
            low = line.lower()
            if not any(
                k in low
                for k in ("zombie", "animal", "vulture", "kind=zombie", "kind=animal")
            ):
                continue
            m = re.search(r"(?:id|ID)\s*=\s*(\d+)", line)
            if not m:
                continue
            eid = m.group(1)
            self.exec(f"kill {eid}")
            killed += 1
        if killed == 0:
            log("telnet clear_ai: no zombie/animal lines matched; nothing to clear")
        log(f"telnet clear_ai killed~={killed} (listents sample {out[:100]!r})")

    def kill_non_player_ai(self) -> int:
        """Kill zombie/animal entities from listents (not the player)."""
        out = self.exec("listents")
        killed = 0
        players = {str(i) for i in self.list_player_ids()}
        for line in out.splitlines():
            low = line.lower()
            if not any(
                k in low
                for k in (
                    "zombie",
                    "animal",
                    "vulture",
                    "bear",
                    "wolf",
                    "snake",
                    "kind=zombie",
                    "kind=animal",
                )
            ):
                continue
            m = re.search(r"(?:id|ID)\s*=\s*(\d+)", line)
            if not m:
                continue
            eid = m.group(1)
            if eid in players:
                continue
            r = self.exec(f"kill {eid}")
            log(f"telnet kill {eid} → {r[:80]!r}")
            killed += 1
        if killed == 0:
            # Broader: kill all entity ids in listents that are not players
            for m in re.finditer(r"(?:id|ID)\s*=\s*(\d+)", out):
                eid = m.group(1)
                if eid in players:
                    continue
                if int(eid) < 100:
                    continue
                r = self.exec(f"kill {eid}")
                log(f"telnet kill fallback {eid} → {r[:80]!r}")
                killed += 1
                if killed >= 16:
                    break
        if killed == 0:
            # No player-hostile fallback: stock `killall` also kills the local
            # player entity, which the survival asserts must not observe.
            log("telnet kill_non_player_ai: no non-player AI matched listents")
        log(f"telnet kill_non_player_ai killed~={killed}")
        return killed

    def list_player_ids(self) -> list[int]:
        """Parse stock `listplayers` / zdtd `list` for entity ids."""
        out = self.exec("listplayers")
        if not out or "unknown" in out.lower():
            out = self.exec("list") or out
        ids = [
            int(x) for x in re.findall(r"(?:id|entity)\s*=\s*(\d+)", out, flags=re.IGNORECASE)
        ]
        # zdtd console style: "(entity 107)"
        ids += [int(x) for x in re.findall(r"\(entity\s+(\d+)\)", out, flags=re.IGNORECASE)]
        ids = [i for i in ids if i > 0]
        return list(dict.fromkeys(ids))

    def teleport_players_to(self, x: float, y: float, z: float) -> int:
        """Teleport every listed player to world coords. Returns how many cmds ran."""
        ids = self.list_player_ids()
        if not ids:
            log("telnet teleport: no players from listplayers")
            return 0
        n = 0
        for pid in ids:
            r = self.exec(f"teleportplayer {pid} {x:g} {y:g} {z:g}")
            log(f"telnet teleportplayer {pid} {x:g} {y:g} {z:g} → {r[:120]!r}")
            n += 1
        return n

    def spawn_near_players(self, entity: str = "zombieBoe", per: int = 1) -> int:
        ids = self.list_player_ids()
        if not ids:
            log("telnet listplayers empty/unparsed for spawn")
            return 0
        spawned = 0
        # One passive-ish spawn near first player only (no scouts: they swarm and kill).
        for pid in ids[:1]:
            for _ in range(max(1, per)):
                r = self.exec(f"spawnentity {pid} {entity}")
                if "No spawn point" in r:
                    break
                spawned += 1
        if spawned == 0:
            # Offset from known pad so the zombie is visible but not on top of the player.
            for pos in ("520 62 950", "530 62 960", "515 62 955"):
                r = self.exec(f"spawnentityat {entity} {pos}")
                if r and "ERR" not in r.upper() and "Unknown" not in r:
                    spawned += 1
                    break
        log(f"telnet spawn near players={ids[:1]} units~={spawned} type={entity}")
        return spawned

    def _send(self, line: str) -> None:
        assert self._sock
        self._sock.sendall((line + "\n").encode("utf-8", errors="replace"))

    def _recv(self, settle: float) -> str:
        assert self._sock
        time.sleep(settle)
        chunks: list[bytes] = []
        self._sock.settimeout(0.25)
        try:
            while True:
                try:
                    data = self._sock.recv(4096)
                except TimeoutError:
                    break
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
                if len(chunks) > 32:
                    break
        finally:
            self._sock.settimeout(2.0)
        return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
