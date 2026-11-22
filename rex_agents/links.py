"""Which Rex pane each task runs in, and going there on a click.

A task is a process; Rex says which program is in front of each pane. A task
matches a pane when it is that program or runs under it, as a launcher's
child does.
"""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Iterable, Optional

from rex_agents.advisor import _rex

# A pane: its session, window and block.
Place = tuple[str, str, str]
ANCESTORS = 6


def parent(pid: int) -> Optional[int]:
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        ppid = int(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return ppid if ppid > 1 else None


def lineage(pid: int) -> list[int]:
    """`pid` and the processes above it, nearest first, short of launchd."""
    chain = [pid]
    while len(chain) <= ANCESTORS and (up := parent(chain[-1])) is not None:
        chain.append(up)
    return chain


class RexPanes:
    """What the links ask of Rex."""

    def fronts(self) -> dict[int, Place]:
        """The pid in front of every placed pane, in every session."""
        found: dict[int, Place] = {}
        for s in (_rex("api", "call", "session.list", "{}") or {}).get("sessions", []):
            session = s["session_id"]
            for b in (_rex("api", "call", "-s", session, "session.list_blocks", "{}") or {}).get("blocks", []):
                if b.get("placement") != "placed" or not b.get("window_id"):
                    continue
                info = _rex("block", "call", "-s", session, "-b", b["block_id"], "process", "{}")
                front = info.get("foreground") if isinstance(info, dict) else None
                if isinstance(front, dict) and isinstance(front.get("pid"), int):
                    found[front["pid"]] = (session, b["window_id"], b["block_id"])
        return found

    def go(self, place: Place) -> None:
        """Show the pane in every Rex app: its session, its tab, focused."""
        session, window, block = place
        for c in (_rex("api", "call", "client.list", "{}") or {}).get("clients", []):
            if (c.get("info") or {}).get("kind") == "app":
                args = {"client_id": c["client_id"], "action": "session.select",
                        "args": {"session_id": session, "window_id": window}}
                _rex("api", "call", "client.perform", json.dumps(args))
        _rex("api", "call", "-s", session, "session.focus_block", json.dumps({"session_id": session, "block_id": block}))


def locate(pids: Iterable[int], fronts: dict[int, Place], lineage_of=lineage) -> dict[int, Place]:
    """The pane of each task that runs in one."""
    found: dict[int, Place] = {}
    for pid in pids:
        place = next((fronts[p] for p in lineage_of(pid) if p in fronts), None)
        if place is not None:
            found[pid] = place
    return found


class Links:
    """The panes of the latest tasks, found off the frame loop, and a jump to
    one, also off it: each asks Rex several times."""

    def __init__(self, rex: Any = None) -> None:
        self.rex = rex or RexPanes()
        self.places: dict[int, Place] = {}

    def refresh(self, tasks: Iterable[dict[str, Any]]) -> None:
        pids = [t["pid"] for t in tasks if isinstance(t.get("pid"), int)]
        self.places = locate(pids, self.rex.fronts()) if pids else {}

    def go(self, pid: int) -> None:
        place = self.places.get(pid)
        if place is not None:
            threading.Thread(target=self.rex.go, args=(place,), daemon=True).start()
