"""Token usage from the session logs Claude Code, Codex and Grok keep on disk.

Only usage metadata is read: timestamp, model, working directory and token
counts. Message content is never kept.

Both agents repeat records, so counting lines over-reports:

* Claude writes one line per content block of a response, and resumed sessions
  copy earlier lines into a new file. Records are merged on (message id,
  request id) across all files.
* Codex forks replay the parent's events. Records are merged on response id.
* Grok resumed and forked sessions inherit earlier turns. Records are merged on
  (turn end time, model).

Token fields are normalized to four non-overlapping buckets, because Codex and
Grok ``input_tokens`` include cached tokens while Claude's excludes them.
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from rex_agents.pricing import cost_usd

CODEX_LIMIT_ID = "codex"
HOURLY_SECONDS = 25 * 3600  # 24 full hours, plus the one in progress
CLAUDE_MODEL = re.compile(r"claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?")
GPT_MODEL = re.compile(r"gpt-(\d+(?:\.\d+)?)-(astra|sol|terra|luna)")
GROK_MODEL = re.compile(r"grok-(\d+(?:\.\d+)?)")


def model_name(model: str) -> str:
    """The name the vendor publishes for a model id. An id with no published
    name, such as ``grok-4.7-build``, stays as it is rather than getting a made-up one."""
    if match := CLAUDE_MODEL.fullmatch(model):
        line, major, minor = match.groups()
        return f"Claude {line.title()} {major}" + (f".{minor}" if minor else "")
    if match := GPT_MODEL.fullmatch(model):
        return f"GPT-{match[1]} {match[2].title()}"
    if match := GROK_MODEL.fullmatch(model):
        return f"Grok {match[1]}"
    return model


@dataclass
class Rec:
    key: str
    agent: str
    ts: float
    model: str
    project: str
    session: str
    fresh: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0
    requests: int = 1
    cache_write_long: int = 0  # the part of cache_write with Claude's 1 hour retention

    @property
    def cost(self) -> Optional[float]:
        return cost_usd(self.model, self.fresh, self.cache_read, self.cache_write, self.output, self.cache_write_long)

    def merge(self, other: "Rec") -> None:
        """Keep the largest value seen: streaming lines of one response only
        ever grow, and a replayed line is identical."""
        self.ts = min(self.ts, other.ts)
        self.fresh = max(self.fresh, other.fresh)
        self.cache_read = max(self.cache_read, other.cache_read)
        self.cache_write = max(self.cache_write, other.cache_write)
        self.output = max(self.output, other.output)
        self.requests = max(self.requests, other.requests)
        self.cache_write_long = max(self.cache_write_long, other.cache_write_long)


@dataclass
class ParsedFile:
    records: list[Rec] = field(default_factory=list)
    # Newest Codex (timestamp, rate_limits) in this file, if any.
    rate_limits: Optional[tuple[str, dict[str, Any]]] = None
    # The short title Claude Code generates for a session, if any.
    title: Optional[str] = None


@dataclass
class LatestSession:
    session: str
    written_at: int


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pretty_project(cwd: Any) -> str:
    if not isinstance(cwd, str) or not cwd:
        return "(未知)"
    home = str(Path.home())
    if cwd == home:
        return "~"
    return "~" + cwd[len(home):] if cwd.startswith(home + os.sep) else cwd


def _json_lines(path: Path, needles: tuple[str, ...]) -> Iterable[dict[str, Any]]:
    """Objects from lines containing a needle. Lines can be megabytes of
    message content, so most are skipped without being decoded."""
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                if not any(n in line for n in needles):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:  # e.g. a line still being written
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def parse_claude(path: Path) -> ParsedFile:
    parsed = ParsedFile()
    for obj in _json_lines(path, ('"usage"', '"ai-title"')):
        if obj.get("type") == "ai-title":
            title = obj.get("aiTitle")
            parsed.title = title if isinstance(title, str) and title else parsed.title
            continue
        msg = obj.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        ts = _epoch(obj.get("timestamp"))
        if not isinstance(usage, dict) or ts is None:
            continue
        model = msg.get("model")
        if not isinstance(model, str) or model == "<synthetic>":
            continue
        msg_id, request_id = msg.get("id"), obj.get("requestId")
        if not msg_id and not request_id:
            continue
        creation = usage.get("cache_creation")
        rec = Rec(
            key=f"{msg_id}:{request_id}",
            agent="claude",
            ts=ts,
            model=model,
            project=pretty_project(obj.get("cwd")),
            session=str(obj.get("sessionId") or path.stem),
            fresh=_int(usage.get("input_tokens")),
            cache_read=_int(usage.get("cache_read_input_tokens")),
            cache_write=_int(usage.get("cache_creation_input_tokens")),
            output=_int(usage.get("output_tokens")),
            cache_write_long=_int(creation.get("ephemeral_1h_input_tokens")) if isinstance(creation, dict) else 0,
        )
        if rec.fresh or rec.cache_read or rec.cache_write or rec.output:
            parsed.records.append(rec)
    return parsed


def _codex_buckets(usage: dict[str, Any]) -> dict[str, int]:
    cached = _int(usage.get("cached_input_tokens"))
    written = _int(usage.get("cache_write_input_tokens"))
    return {
        "fresh": max(_int(usage.get("input_tokens")) - cached - written, 0),
        "cache_read": cached,
        "cache_write": written,
        "output": _int(usage.get("output_tokens")),  # includes reasoning
    }


def parse_codex(path: Path) -> ParsedFile:
    parsed = ParsedFile()
    model = "(未知)"
    cwd: Any = None
    session = path.stem
    records: list[Rec] = []
    fallback: list[Rec] = []  # older logs have token_count but no token_usage_record
    previous_total: Optional[int] = None
    needles = ('"session_meta"', '"turn_context"', '"token_usage_record"', '"token_count"')
    for obj in _json_lines(path, needles):
        kind, payload = obj.get("type"), obj.get("payload")
        if not isinstance(payload, dict):
            continue
        ts = _epoch(obj.get("timestamp"))
        if kind == "session_meta":
            session = str(payload.get("id") or session)
            cwd = payload.get("cwd") or cwd
        elif kind == "turn_context":
            model = payload.get("model") if isinstance(payload.get("model"), str) else model
            cwd = payload.get("cwd") or cwd
        elif kind == "token_usage_record" and ts is not None:
            usage, response_id = payload.get("usage"), payload.get("response_id")
            if isinstance(usage, dict) and response_id:
                records.append(Rec(f"{response_id}", "codex", ts, model, pretty_project(cwd), session,
                                   **_codex_buckets(usage)))
        elif payload.get("type") == "token_count":
            limits = payload.get("rate_limits")
            stamp = obj.get("timestamp")
            if (isinstance(limits, dict) and isinstance(stamp, str)
                    and limits.get("limit_id") in (None, CODEX_LIMIT_ID)
                    and (parsed.rate_limits is None or stamp > parsed.rate_limits[0])):
                parsed.rate_limits = (stamp, limits)
            info = payload.get("info")
            if not isinstance(info, dict) or ts is None:
                continue
            total = (info.get("total_token_usage") or {}).get("total_tokens")
            last = info.get("last_token_usage")
            # The same totals are re-emitted between turns; only a change is a
            # new response. `last` rather than a delta, so a fork's inherited
            # total is not counted on its first event.
            if isinstance(last, dict) and total != previous_total:
                fallback.append(Rec(f"{session}:{len(fallback)}", "codex", ts, model, pretty_project(cwd),
                                    session, **_codex_buckets(last)))
            previous_total = total
    parsed.records = records or fallback
    return parsed


def parse_grok(path: Path) -> ParsedFile:
    """``usage.json`` of one session. Only ``info.cwd`` is taken from the
    summary next to it, which otherwise describes the conversation."""
    parsed = ParsedFile()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):  # e.g. a file still being written
        return parsed
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list):
        return parsed
    cwd: Any = None
    try:
        info = json.loads((path.parent / "summary.json").read_text()).get("info")
        cwd = info.get("cwd") if isinstance(info, dict) else None
    except (OSError, ValueError, AttributeError):
        pass
    session = str(data.get("sessionId") or path.parent.name)
    for turn in turns:
        ts = _epoch(turn.get("endedAt")) if isinstance(turn, dict) else None
        if ts is None:
            continue
        models = turn.get("modelUsage")
        if not isinstance(models, dict) or not models:
            models = {turn.get("primaryModelId"): turn}
        for model, usage in models.items():
            if not isinstance(model, str) or not isinstance(usage, dict):
                continue
            cached, written = _int(usage.get("cachedReadTokens")), _int(usage.get("cacheCreationTokens"))
            rec = Rec(
                key=f"grok:{turn['endedAt']}:{model}",
                agent="grok",
                ts=ts,
                model=model,
                project=pretty_project(cwd),
                session=session,
                fresh=max(_int(usage.get("inputTokens")) - cached - written, 0),
                cache_read=cached,
                cache_write=written,
                output=_int(usage.get("outputTokens")),  # includes reasoning
                requests=_int(usage.get("modelCalls")) or 1,
            )
            if rec.fresh or rec.cache_read or rec.cache_write or rec.output:
                parsed.records.append(rec)
    return parsed


def claude_files() -> list[Path]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return list((base / "projects").glob("**/*.jsonl"))


def codex_homes() -> list[Path]:
    homes = [Path.home() / ".codex"]
    homes.extend(sorted(p for p in Path.home().glob(".codex-*") if p.is_dir()))
    if os.environ.get("CODEX_HOME"):
        homes.insert(0, Path(os.environ["CODEX_HOME"]))
    return list(dict.fromkeys(homes))


def codex_files() -> list[Path]:
    files: list[Path] = []
    for home in codex_homes():
        for folder in ("sessions", "archived_sessions"):
            files.extend((home / folder).glob("**/*.jsonl"))
    return files


def grok_home() -> Path:
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")


def grok_files() -> list[Path]:
    return list((grok_home() / "sessions").glob("*/*/usage.json"))


class Scanner:
    """Parses log files, re-reading only those whose size or mtime changed.
    Safe to share between threads: a quota refresh reads it in the background."""

    def __init__(self) -> None:
        self._files: dict[Path, tuple[tuple[int, int], ParsedFile]] = {}
        self._lock = threading.RLock()

    def _parsed(self) -> list[ParsedFile]:
        with self._lock:
            return self._parse()

    def _parse(self) -> list[ParsedFile]:
        wanted = ([(p, parse_claude) for p in claude_files()] + [(p, parse_codex) for p in codex_files()]
                  + [(p, parse_grok) for p in grok_files()])
        seen = set()
        for path, parser in wanted:
            seen.add(path)
            try:
                stat = path.stat()
            except OSError:
                continue
            sig = (stat.st_mtime_ns, stat.st_size)
            cached = self._files.get(path)
            if cached is None or cached[0] != sig:
                self._files[path] = (sig, parser(path))
        for path in set(self._files) - seen:
            del self._files[path]
        return [entry[1] for entry in self._files.values()]

    def records(self) -> list[Rec]:
        merged: dict[str, Rec] = {}
        for parsed in self._parsed():
            for rec in parsed.records:
                existing = merged.get(rec.key)
                if existing is None:
                    # Copy: merge() must not mutate the per-file cache.
                    merged[rec.key] = Rec(**vars(rec))
                else:
                    existing.merge(rec)
        return list(merged.values())

    def claude_title(self, session: str) -> Optional[str]:
        """From the files already parsed, so call after ``records``."""
        with self._lock:
            for path, (_, parsed) in self._files.items():
                if path.stem == session and parsed.title:
                    return parsed.title
        return None

    def latest_sessions(self) -> dict[tuple[str, str], LatestSession]:
        """By (agent, project), the session whose file was written last. From
        the files already parsed, so call after ``records``."""
        latest: dict[tuple[str, str], LatestSession] = {}
        with self._lock:
            files = list(self._files.values())
        for (mtime_ns, _), parsed in files:
            if not parsed.records:
                continue
            rec = parsed.records[-1]
            written_at = mtime_ns // 1_000_000_000
            current = latest.get((rec.agent, rec.project))
            if current is None or written_at > current.written_at:
                latest[(rec.agent, rec.project)] = LatestSession(rec.session, written_at)
        return latest

    def latest_codex_rate_limits(self) -> Optional[tuple[str, dict[str, Any]]]:
        found = [p.rate_limits for p in self._parsed() if p.rate_limits]
        return max(found, key=lambda item: _epoch(item[0]) or 0) if found else None


def _hour_start(ts: float) -> int:
    return int(datetime.fromtimestamp(ts).replace(minute=0, second=0, microsecond=0).timestamp())


def rollup(records: list[Rec], now: Optional[float] = None) -> dict[str, Any]:
    """Daily rows by (date, agent, model, project) in the local timezone, and
    hourly rows by (hour, agent) for the last day."""
    since = (datetime.now().timestamp() if now is None else now) - HOURLY_SECONDS
    rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    hours: dict[tuple[int, str], dict[str, Any]] = {}
    session_ids: dict[tuple[str, str], int] = {}
    for rec in records:
        date = datetime.fromtimestamp(rec.ts).strftime("%Y-%m-%d")
        key = (date, rec.agent, rec.model, rec.project)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "date": date, "agent": rec.agent, "model": rec.model, "model_name": model_name(rec.model),
                "project": rec.project,
                "fresh": 0, "cache_read": 0, "cache_write": 0, "output": 0, "requests": 0, "sessions": set(), "cost": None,
            }
        cost = rec.cost
        if cost is not None:
            row["cost"] = (row["cost"] or 0) + cost
        # Sessions span days and models, so the client needs ids to count
        # distinct ones in a slice. An index keeps the payload small.
        session = session_ids.setdefault((rec.agent, rec.session), len(session_ids))
        slices = [row]
        if rec.ts >= since:
            hour = _hour_start(rec.ts)
            slices.append(hours.setdefault((hour, rec.agent), {
                "hour": hour, "agent": rec.agent, "fresh": 0, "cache_read": 0, "cache_write": 0, "output": 0,
                "requests": 0, "sessions": set(),
            }))
        for target in slices:
            target["fresh"] += rec.fresh
            target["cache_read"] += rec.cache_read
            target["cache_write"] += rec.cache_write
            target["output"] += rec.output
            target["requests"] += rec.requests
            target["sessions"].add(session)
    result = sorted(rows.values(), key=lambda r: (r["date"], r["agent"], r["model"], r["project"]))
    for row in result:
        row["sessions"] = sorted(row["sessions"])
    hourly = sorted(hours.values(), key=lambda r: (r["hour"], r["agent"]))
    for row in hourly:
        row["sessions"] = sorted(row["sessions"])
    return {
        "rows": result,
        "hours": hourly,
        "first_date": result[0]["date"] if result else None,
        "last_date": result[-1]["date"] if result else None,
    }
