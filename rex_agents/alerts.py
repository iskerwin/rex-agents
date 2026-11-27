"""Notices when a quota runs low and when a spent one comes back.

Every tab with an Agents panel runs its own process, so what has been told
is kept in one file under a lock: whichever panel sees a change first tells
it, and the others find it already told. A window seen for the first time
only sets the baseline, so opening or reinstalling a panel says nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Optional

from rex_agents.pane import short_label, until

LOW = 10  # percent left
# resets_at can drift a little between reads of one window.
SAME_WINDOW = 600


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "rex-agents" / "alerts.json"


def changes(seen: dict[str, Any], snap: dict[str, Any], now: int) -> list[tuple[str, str]]:
    """The notices `snap` calls for against `seen`, which it updates. A
    restored snapshot and a stale account are old news, and tell nothing."""
    if snap.get("refreshing"):
        return []
    notices = []
    for a in snap.get("accounts", []):
        if a.get("stale"):
            continue
        for w in a.get("windows", []):
            left, resets_at = w.get("left_percent"), w.get("resets_at")
            if left is None or resets_at is None:
                continue
            key = f"{a['agent']}/{w['label']}"
            name = f"{a.get('name') or a['agent'].capitalize()} {short_label(w)}"
            before = seen.get(key)
            if before is not None:
                same = abs(resets_at - before["resets_at"]) < SAME_WINDOW
                if same and before["left"] > LOW >= left:
                    notices.append((f"{name} 剩 {round(left)}%", f"{until(resets_at - now)} 后重置"))
                elif not same and resets_at > before["resets_at"] and before["left"] <= LOW:
                    notices.append((f"{name} 已重置", f"剩 {round(left)}%"))
            seen[key] = {"left": left, "resets_at": resets_at}
    return notices


def notices(snap: dict[str, Any], now: int, path: Optional[Path] = None) -> list[tuple[str, str]]:
    """`changes` against the shared file, told once between all panels."""
    path = path or state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                seen = json.loads(path.read_text())
            except (OSError, ValueError):
                seen = {}
            told = changes(seen, snap, now)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(seen))
            tmp.replace(path)
            return told
    except OSError:
        return []


def osc(title: str, body: str) -> str:
    """A desktop notification asked of the terminal: OSC 777, which Rex
    shows as a system notification."""
    clean = lambda s: "".join(c for c in s if c.isprintable()).replace(";", ",")  # noqa: E731
    return f"\x1b]777;notify;{clean(title)};{clean(body)}\x07"
