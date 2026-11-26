"""The advisor of the Claude Code sessions in this Rex tab, read from their logs.

Rex says which panes share the pane view's tab and which program each runs in
front; Claude Code's registry (`~/.claude/sessions/<pid>.json`) turns a process
into its session; the session's log has every advisor call: a
`server_tool_use` named `advisor` and its `advisor_tool_result`, timestamped,
each record naming the advisor model. Only those blocks, the tools called and
whether they failed are read, never what was said.

The moment a call counts under is inferred the way advisor-live does: the same
tool failing twice in a row with the same message is "same error twice", a
step that ends the turn is "before calling it done", a call before any file
was written is "before the plan locks", and the rest are check-ins.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

TAB_SECONDS = 5.0
LOG_SECONDS = 1.0
RECENT_CALLS = 32
QUIET_TURNS = 3
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def model_name(model: Optional[str]) -> str:
    """`claude-opus-5-5` and `opus[1m]` read `Opus 5.5` and `Opus`; a name
    already written for people (`Fable 5.2`) stays."""
    if not model:
        return "…"
    if re.search(r"[A-Z ]", model):
        return model
    parts = [p for p in re.sub(r"\[.*\]$", "", model).split("-") if p != "claude"]
    family = next((p for p in parts if re.fullmatch(r"[a-z]+", p)), None)
    if family is None:
        return model
    version = ".".join(p for p in parts if re.fullmatch(r"\d{1,2}", p))
    return family.capitalize() + (f" {version}" if version else "")


def _ms(timestamp: Any) -> Optional[int]:
    try:
        return int(datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class AdvisorLog:
    """A session log read as it grows: complete lines only, each once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.main: Optional[str] = None
        self.advisor: Optional[str] = None
        self.counts = {"plan": 0, "error": 0, "done": 0, "other": 0}
        self.calls: list[dict[str, Any]] = []
        self.quiet_turns = 0
        self._tools: dict[str, str] = {}
        self._pending: dict[str, tuple[str, int]] = {}
        self._new_turn()

    def _new_turn(self) -> None:
        self.has_edited = False
        self.repeats = 0
        self.last_error = ""
        self.turn_worked = False
        self.turn_called = False

    def read(self) -> None:
        try:
            with open(self.path, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1
        self.offset += end
        for line in data[:end].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                self.take(record)

    def take(self, record: dict[str, Any]) -> None:
        if record.get("isSidechain"):
            return
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if record.get("type") == "user":
            self._user(record, content, blocks)
        elif record.get("type") == "assistant" and isinstance(message, dict):
            self._assistant(record, message, blocks)

    def _user(self, record: dict[str, Any], content: Any, blocks: list[dict[str, Any]]) -> None:
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if not results:
            if not record.get("isMeta") and content:
                # A prompt: the turn before it worked and never called, or did.
                if self.turn_worked and not self.turn_called:
                    self.quiet_turns += 1
                self._new_turn()
            return
        for result in results:
            self.turn_worked = True
            if result.get("is_error"):
                text = result.get("content")
                text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
                tool = self._tools.get(str(result.get("tool_use_id")), "")
                message = f"{tool}:{text[:80]}"
                self.repeats = self.repeats + 1 if message == self.last_error else 1
                self.last_error = message
            else:
                self.repeats, self.last_error = 0, ""
                if self._tools.get(str(result.get("tool_use_id"))) in EDIT_TOOLS:
                    self.has_edited = True

    def _assistant(self, record: dict[str, Any], message: dict[str, Any], blocks: list[dict[str, Any]]) -> None:
        if isinstance(message.get("model"), str) and not message["model"].startswith("<"):
            self.main = message["model"]
        if "advisorModel" in record:
            self.advisor = record["advisorModel"] if isinstance(record["advisorModel"], str) else None
        at = _ms(record.get("timestamp"))
        for block in blocks:
            kind = block.get("type")
            if kind == "tool_use":
                self._tools[str(block.get("id"))] = str(block.get("name"))
            elif kind == "server_tool_use" and block.get("name") == "advisor" and at is not None:
                ends_turn = message.get("stop_reason") == "end_turn"
                trigger = ("error" if self.repeats >= 2 else "done" if ends_turn
                           else "other" if self.has_edited else "plan")
                self._pending[str(block.get("id"))] = (trigger, at)
            elif kind == "advisor_tool_result" and at is not None:
                started = self._pending.pop(str(block.get("tool_use_id")), None)
                if started is None:
                    continue
                trigger, began = started
                self.counts[trigger] += 1
                self.calls = [*self.calls, {"trigger": trigger, "ms": max(0, at - began), "at": at}][-RECENT_CALLS:]
                self.turn_called = True
                self.quiet_turns = 0

    def state(self, busy: bool, turn_started_at: int, configured: Optional[str] = None) -> Optional[dict[str, Any]]:
        """What the pane draws; None while the advisor is off. A log names the
        advisor only once it runs, so `configured`, the one the session was
        started with, stands in until then: one less capable than the main
        model never runs, and that is worth seeing."""
        advisor = self.advisor or configured
        if not advisor:
            return None
        return {
            "main": model_name(self.main),
            "advisor": model_name(advisor),
            "busy": busy,
            "turnStartedAt": turn_started_at,
            "quietTurns": self.quiet_turns if self.quiet_turns >= QUIET_TURNS else 0,
            "counts": dict(self.counts),
            "calls": list(self.calls),
        }


def tab_blocks(view: dict[str, Any], block_id: str) -> list[str]:
    """The blocks of the window that holds `block_id`, itself left out."""
    for window in view.get("windows", []):
        blocks = [b["block_id"] for layer in window.get("layers", []) for b in layer.get("blocks", [])]
        if block_id in blocks:
            return [b for b in blocks if b != block_id]
    return []


def _rex(*args: str) -> Optional[dict[str, Any]]:
    rex = shutil.which("rex")
    if rex is None:
        return None
    try:
        ran = subprocess.run([rex, "--timeout", "3s", *args], capture_output=True, text=True, timeout=5)
        return json.loads(ran.stdout) if ran.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


class Rex:
    """The two questions the feed asks of Rex."""

    # Rex takes a block ID wherever a session goes, as the session holding it.

    def view(self, scope: str) -> Optional[dict[str, Any]]:
        return _rex("api", "call", "-s", scope, "session.view", "{}")

    def foreground(self, scope: str, block: str) -> Optional[dict[str, Any]]:
        """The program in front in a pane: its pid, name, path and argv0."""
        info = _rex("block", "call", "-s", scope, "-b", block, "process", "{}")
        front = info.get("foreground") if isinstance(info, dict) else None
        return front if isinstance(front, dict) and isinstance(front.get("pid"), int) else None


AGENTS = ("claude", "codex", "grok")


def agent_of(front: dict[str, Any]) -> Optional[str]:
    """Which agent a pane's front program is, by its name, path or argv0."""
    names = {Path(str(front.get(k) or "")).name for k in ("name", "argv0", "path", "invoked_path")}
    return next((a for a in AGENTS if a in names), None)


def launch_advisor(argv: str) -> Optional[str]:
    """`--advisor fable` or `--advisor=fable` on a launch command line; `off`
    reads as the empty string, no flag as None."""
    m = re.search(r"(?:^|\s)--advisor(?:=|\s+)(\S+)", argv)
    if m is None:
        return None
    return "" if m[1] == "off" else m[1]


def command_line(pid: int) -> str:
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def configured_advisor(pid: int) -> Optional[str]:
    """The advisor a session was started with: its launch flag, else the
    `advisorModel` setting, as Claude Code itself decides."""
    flagged = launch_advisor(command_line(pid))
    if flagged is not None:
        return flagged or None
    try:
        setting = json.loads((claude_home() / "settings.json").read_text()).get("advisorModel")
    except (OSError, ValueError, AttributeError):
        return None
    return setting if isinstance(setting, str) and setting else None


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def registry(pid: int) -> Optional[dict[str, Any]]:
    """Claude Code's own record of a running process: its session and status."""
    try:
        entry = json.loads((claude_home() / "sessions" / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return None
    return entry if isinstance(entry, dict) and entry.get("sessionId") else None


def session_log(session_id: str) -> Optional[Path]:
    return next((claude_home() / "projects").glob(f"*/{session_id}.jsonl"), None)


class AdvisorFeed:
    """The advisor states of this tab's Claude Code sessions, as often as a
    frame asks but no more than the tab and the logs change, and in `agents`
    which agents the tab runs. Rex sets REX_BLOCK in every pane it runs,
    and the block alone finds its session (REX_SESSION may be unset);
    outside Rex there is no tab and nothing to show."""

    def __init__(self, env: Optional[dict[str, str]] = None, rex: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        env = os.environ if env is None else env
        self.block = env.get("REX_BLOCK")
        self.rex = rex or Rex()
        self.clock = clock
        self.pids: list[int] = []
        self.agents: list[str] = []
        self.configured: dict[int, Optional[str]] = {}
        self.logs: dict[str, AdvisorLog] = {}
        self.states: list[dict[str, Any]] = []
        self.tab_at = self.log_at = float("-inf")

    def __call__(self) -> list[dict[str, Any]]:
        if not self.block:
            return []
        now = self.clock()
        if now - self.tab_at >= TAB_SECONDS:
            self.tab_at = now
            view = self.rex.view(self.block)
            if view is not None:
                fronts = [f for f in (self.rex.foreground(self.block, b) for b in tab_blocks(view, self.block)) if f]
                self.pids = [f["pid"] for f in fronts]
                self.agents = sorted({a for a in map(agent_of, fronts) if a})
                self.configured = {p: configured_advisor(p) for p in self.pids if registry(p) is not None}
            self.log_at = float("-inf")
        if now - self.log_at >= LOG_SECONDS:
            self.log_at = now
            self.states = [s for s in map(self._state, self.pids) if s is not None]
        return self.states

    def _state(self, pid: int) -> Optional[dict[str, Any]]:
        entry = registry(pid)
        if entry is None:
            return None
        session_id = str(entry["sessionId"])
        log = self.logs.get(session_id)
        if log is None:
            path = session_log(session_id)
            if path is None:
                return None
            log = self.logs[session_id] = AdvisorLog(path)
        log.read()
        changed = entry.get("statusUpdatedAt")
        return log.state(entry.get("status") not in (None, "idle"), changed if isinstance(changed, int) else 0,
                         self.configured.get(pid))
