"""Agent sessions open in a terminal right now.

A process is found with ``ps``. What it is doing comes from the agent's own
records: Claude Code keeps a registry with a status per process, and Codex and
Grok count as running while their session file is still being written. The
session title is the short one the agent generated; no message is read.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rex_agents.logs import Scanner, codex_homes, grok_home, pretty_project

AGENTS = ("claude", "codex", "grok")
RECENT_WRITE_SECONDS = 120
COMMAND_TIMEOUT_SECONDS = 5


@dataclass
class Proc:
    pid: int
    ppid: int
    agent: str
    started_at: int


def parse_etime(text: str) -> int:
    """``[[dd-]hh:]mm:ss`` from ps."""
    days, _, clock = text.rpartition("-")
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + int(days or 0) * 86400


def parse_ps(output: str, now: int) -> list[Proc]:
    """Agents attached to a terminal, leaving out the helpers they start."""
    found: list[Proc] = []
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5 or fields[3] == "??":
            continue
        agent = os.path.basename(fields[4].strip())
        if agent in AGENTS:
            found.append(Proc(int(fields[0]), int(fields[1]), agent, now - parse_etime(fields[2])))
    pids = {proc.pid for proc in found}
    return [proc for proc in found if proc.ppid not in pids]


def _processes(now: int) -> list[Proc]:
    output = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,tty=,comm="], capture_output=True, text=True,
                            timeout=COMMAND_TIMEOUT_SECONDS, check=True).stdout
    return parse_ps(output, now)


def _cwd(pid: int) -> Optional[str]:
    try:
        output = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                                timeout=COMMAND_TIMEOUT_SECONDS).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line[1:] for line in output.splitlines() if line.startswith("n")), None)


def _claude_entry(pid: int) -> Optional[dict[str, Any]]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        entry = json.loads((base / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) else None


def _codex_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    for home in codex_homes():
        try:
            lines = (home / "session_index.jsonl").read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("id") and entry.get("thread_name"):
                titles[str(entry["id"])] = str(entry["thread_name"])
    return titles


def _grok_title(session: str) -> Optional[str]:
    for path in (grok_home() / "sessions").glob(f"*/{session}/summary.json"):
        try:
            title = json.loads(path.read_text()).get("generated_title")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(title, str) and title:
            return title
    return None


def _task(proc: Proc, title: Optional[str], cwd: Optional[str], running: bool, since: int) -> dict[str, Any]:
    return {
        "agent": proc.agent,
        "pid": proc.pid,
        "title": title,
        "project": pretty_project(cwd),
        "cwd": cwd,
        "state": "running" if running else "idle",
        "since": since,
    }


def _claude_title(entry: dict[str, Any], scanner: Scanner) -> Optional[str]:
    """A name the user gave wins. A derived one only repeats the folder, so the
    generated title goes first."""
    name = entry.get("name") if isinstance(entry.get("name"), str) and entry.get("name") else None
    if name and entry.get("nameSource") != "derived":
        return name
    return scanner.claude_title(str(entry.get("sessionId"))) or name


def _claude_task(proc: Proc, entry: dict[str, Any], scanner: Scanner) -> dict[str, Any]:
    running = entry.get("status") != "idle"
    changed = entry.get("statusUpdatedAt")
    since = changed // 1000 if running and isinstance(changed, int) else proc.started_at
    cwd = entry.get("cwd")
    return _task(proc, _claude_title(entry, scanner), cwd if isinstance(cwd, str) else None, running, since)


def list_tasks(scanner: Scanner, now: Optional[int] = None) -> Optional[list[dict[str, Any]]]:
    """Running sessions first. None when processes could not be listed, which
    is not the same as nothing running. Call after ``scanner.records()``."""
    now = int(time.time()) if now is None else now
    try:
        processes = _processes(now)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    latest = scanner.latest_sessions()
    codex_titles: Optional[dict[str, str]] = None
    tasks = []
    for proc in processes:
        entry = _claude_entry(proc.pid) if proc.agent == "claude" else None
        if entry is not None:
            tasks.append(_claude_task(proc, entry, scanner))
            continue
        cwd = _cwd(proc.pid)
        session = latest.get((proc.agent, pretty_project(cwd)))
        # An older session in the same folder says nothing about this process.
        if session is not None and session.written_at < proc.started_at:
            session = None
        title = None
        if session is not None and proc.agent == "codex":
            codex_titles = _codex_titles() if codex_titles is None else codex_titles
            title = codex_titles.get(session.session)
        elif session is not None and proc.agent == "grok":
            title = _grok_title(session.session)
        running = session is not None and now - session.written_at <= RECENT_WRITE_SECONDS
        tasks.append(_task(proc, title, cwd, running, proc.started_at))
    tasks.sort(key=lambda task: (task["state"] != "running", task["project"], task["since"]))
    return tasks
