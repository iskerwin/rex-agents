"""Quota windows (5-hour, weekly, ...) as each agent exposes them.

The freshness rules follow aiusage (https://github.com/sergestack/aiusage):

* A missing value stays missing. It never becomes 0% or 100%, and one window is
  never copied into another.
* A window whose reset has passed no longer describes anything and is dropped.
* A stale snapshot is labelled, and is never picked as the most urgent window.
* No agent session is ever started to "refresh" usage, and no limit is touched.

Sources:

* Codex: the official ``codex app-server`` JSON-RPC, falling back to the
  newest ``rate_limits`` event in the session logs.
* Claude Code: the OAuth usage endpoint that ``/usage`` calls, using Claude
  Code's stored token (kept in memory, never logged).
* Grok: the billing endpoint the Grok CLI uses, with the token the CLI stored
  in ``~/.grok/auth.json``. That file is only read. The CLI renews its own
  login, so an expired one is handed to ``grok models``, which starts no session.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rex_agents import __version__
from rex_agents.logs import Scanner, _epoch, codex_homes, grok_home

LOW_MAX_LEFT = 40.0
CRITICAL_MAX_LEFT = 20.0
STALE_AFTER_SECONDS = 600
CLAUDE_MIN_REFRESH_SECONDS = 60
# After HTTP 429 the wait doubles up to this, or is what Retry-After says.
CLAUDE_MAX_BACKOFF_SECONDS = 900
CODEX_TIMEOUT_SECONDS = 12.0

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_WINDOWS = (
    ("five_hour", "5 小时"),
    ("seven_day", "每周"),
    ("seven_day_opus", "每周 (Opus)"),
    ("seven_day_sonnet", "每周 (Sonnet)"),
)
CLAUDE_SETUP_NOTE = "还没有读到配额"

GROK_USAGE_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"
GROK_WEEKLY_PERIOD = "USAGE_PERIOD_TYPE_WEEKLY"
GROK_MIN_REFRESH_SECONDS = 60
GROK_LOGIN_NOTE = "未登录：运行 grok login"
GROK_EXPIRED_NOTE = "登录凭据已过期且未能续期：运行一次 grok 后恢复"
GROK_RENEW_TIMEOUT_SECONDS = 30


@dataclass
class Window:
    label: str
    used_percent: float
    resets_at: Optional[int] = None

    @property
    def left_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)

    @property
    def level(self) -> str:
        if self.left_percent <= CRITICAL_MAX_LEFT:
            return "critical"
        return "low" if self.left_percent <= LOW_MAX_LEFT else "ok"

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "left_percent": self.left_percent, "level": self.level}


@dataclass
class Account:
    agent: str
    name: str
    email: Optional[str] = None
    plan: Optional[str] = None
    workspace: Optional[str] = None
    windows: list[Window] = field(default_factory=list)
    source: Optional[str] = None
    captured_at: Optional[int] = None
    stale: bool = False
    note: Optional[str] = None
    manual_resets: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["windows"] = [w.to_dict() for w in self.windows]
        return data


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 100 else None


def _reset_epoch(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    parsed = _epoch(value)
    return int(parsed) if parsed else None


def _live_windows(windows: list[Window], now: int) -> list[Window]:
    return [w for w in windows if w.resets_at is None or w.resets_at > now]


def _codex_label(minutes: int) -> str:
    if 4 * 60 <= minutes <= 6 * 60:
        return "5 小时"
    if 6 * 1440 <= minutes <= 8 * 1440:
        return "每周"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    return f"{minutes // 60} 小时" if minutes % 60 == 0 else f"{minutes} 分钟"


def parse_codex_windows(limits: dict[str, Any], minutes_key: str, used_key: str, reset_key: str) -> list[Window]:
    """Label windows by their reported duration, never by slot: Codex has moved
    the 5-hour and weekly windows between primary and secondary before."""
    windows = []
    for slot in ("primary", "secondary"):
        raw = limits.get(slot)
        if not isinstance(raw, dict):
            continue
        minutes, used = raw.get(minutes_key), _percent(raw.get(used_key))
        if not isinstance(minutes, int) or minutes <= 0 or used is None:
            continue
        windows.append(Window(_codex_label(minutes), used, _reset_epoch(raw.get(reset_key))))
    return sorted(windows, key=lambda w: (w.label != "5 小时", w.label != "每周"))


class CodexAppServer:
    """Line-delimited JSON-RPC to ``codex app-server``. A reader thread makes
    every wait bounded, so a wedged server cannot hang the panel."""

    def __init__(self, exe: str, home: Path) -> None:
        self.proc = subprocess.Popen(
            [exe, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env={**os.environ, "CODEX_HOME": str(home)},
        )
        self.messages: "queue.Queue[dict[str, Any]]" = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:  # type: ignore[union-attr]
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict):
                self.messages.put(msg)

    def send(self, payload: dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(payload) + "\n")  # type: ignore[union-attr]
        self.proc.stdin.flush()  # type: ignore[union-attr]

    def request(self, method: str, req_id: int, params: Any = None) -> dict[str, Any]:
        self.send({"method": method, "id": req_id, **({"params": params} if params is not None else {})})
        deadline = time.monotonic() + CODEX_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{method} 超时")
            try:
                msg = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"{method} 超时") from None
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败")
                result = msg.get("result")
                return result if isinstance(result, dict) else {}

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()


def _codex_name(home: Path) -> str:
    return "Codex" if home.name == ".codex" else f"Codex ({home.name.removeprefix('.codex-').lstrip('.')})"


def query_codex_home(exe: str, home: Path) -> Account:
    now = int(time.time())
    account = Account("codex", _codex_name(home), source="codex app-server", captured_at=now)
    server = CodexAppServer(exe, home)
    try:
        server.request("initialize", 1, {"clientInfo": {"name": "rex-agents", "title": "rex-agents",
                                                        "version": __version__}})
        server.send({"method": "initialized", "params": {}})
        info = server.request("account/read", 2, {"refreshToken": False}).get("account")
        if not isinstance(info, dict):
            raise RuntimeError("未登录")
        account.email, account.plan = info.get("email"), info.get("planType")
        account.workspace = info.get("workspaceName")
        result = server.request("account/rateLimits/read", 3)
        limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits")
        if not isinstance(limits, dict):
            raise RuntimeError("未返回配额")
        account.windows = _live_windows(parse_codex_windows(limits, "windowDurationMins", "usedPercent", "resetsAt"), now)
        credits = result.get("rateLimitResetCredits")
        count = credits.get("availableCount") if isinstance(credits, dict) else None
        account.manual_resets = count if isinstance(count, int) and count >= 0 else None
    finally:
        server.close()
    return account


def query_codex(scanner: Scanner) -> list[Account]:
    exe = shutil.which("codex")
    homes = [h for h in codex_homes() if (h / "auth.json").exists() or (h / "config.toml").exists()]
    accounts: list[Account] = []
    seen: set[str] = set()
    problem: Optional[str] = None
    for home in homes if exe else []:
        try:
            account = query_codex_home(exe, home)
        except Exception as exc:
            problem = str(exc) or type(exc).__name__
            continue
        key = (account.email or "").lower() or str(home)
        if key not in seen:  # several homes can share one login
            seen.add(key)
            accounts.append(account)
    if accounts:
        return accounts
    if not homes and not exe:
        return []

    now = int(time.time())
    fallback = Account("codex", "Codex", note=f"无法读取实时配额（{problem or '未找到 codex 命令'}）")
    latest = scanner.latest_codex_rate_limits()
    if latest:
        stamp, limits = latest
        captured = int(_epoch(stamp) or 0)
        fallback.source = "Codex 会话日志"
        fallback.captured_at = captured
        fallback.stale = now - captured > STALE_AFTER_SECONDS
        fallback.plan = limits.get("plan_type")
        fallback.windows = _live_windows(parse_codex_windows(limits, "window_minutes", "used_percent", "resets_at"), now)
    return [fallback]


# ------------------------------------------------------------------ Claude
def cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "rex-agents"


def _claude_limit_label(limit: dict[str, Any]) -> Optional[str]:
    kind = limit.get("kind")
    if kind == "session":
        return "5 小时"
    if kind == "weekly_all":
        return "每周"
    if kind == "weekly_scoped":
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        return f"每周 ({name.strip()})" if isinstance(name, str) and name.strip() else None
    return None


def parse_claude_limits(limits: Any) -> list[Window]:
    """The ``limits`` list, which also names per-model weekly allowances such as
    Fable's. One that the plan does not have is simply absent from it."""
    windows: list[Window] = []
    for raw in limits if isinstance(limits, list) else []:
        label = _claude_limit_label(raw) if isinstance(raw, dict) else None
        used = _percent(raw.get("percent")) if label else None
        if used is not None and all(w.label != label for w in windows):
            windows.append(Window(label, used, _reset_epoch(raw.get("resets_at"))))
    return windows


def parse_claude_live(payload: Any) -> list[Window]:
    if not isinstance(payload, dict):
        raise ValueError("响应格式不符")
    # The list or the older per-window keys, never both: a label names one window.
    windows = parse_claude_limits(payload.get("limits"))
    for key, label in CLAUDE_WINDOWS if not windows else ():
        raw = payload.get(key)
        used = _percent(raw.get("utilization")) if isinstance(raw, dict) else None
        if used is not None:
            windows.append(Window(label, used, _reset_epoch(raw.get("resets_at"))))
    if not windows:
        raise ValueError("响应中没有配额窗口")
    return windows


def write_private(path: Path, text: str) -> None:
    """Atomically, readable only by the user."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _claude_token() -> Optional[str]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    raw: Optional[str] = None
    try:
        raw = (base / ".credentials.json").read_text()
    except OSError:
        done = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=30,
        ) if shutil.which("security") else None
        raw = done.stdout if done and done.returncode == 0 else None
    if not raw:
        return None
    oauth = json.loads(raw).get("claudeAiOauth")
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    return token if isinstance(token, str) and token else None


def _fetch_claude_live(token: str) -> Any:
    request = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": CLAUDE_OAUTH_BETA,
        "Accept": "application/json",
        "User-Agent": f"rex-agents/{__version__}",
    })
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


def backoff(wait: float, retry_after: Optional[str]) -> float:
    """The wait after a 429: what Retry-After says in seconds, else twice the
    last, from one minute up to CLAUDE_MAX_BACKOFF_SECONDS."""
    try:
        asked = float(retry_after) if retry_after else 0.0
    except ValueError:
        asked = 0.0
    return min(max(asked, wait * 2, CLAUDE_MIN_REFRESH_SECONDS), CLAUDE_MAX_BACKOFF_SECONDS)


class ClaudeQuota:
    def __init__(self) -> None:
        self._last_live: Optional[tuple[int, list[Window]]] = None
        self._last_attempt = 0.0
        self._wait = CLAUDE_MIN_REFRESH_SECONDS
        self._last_problem: Optional[str] = None
        self._identity: tuple[float, Optional[str], Optional[str]] = (0.0, None, None)

    def _whoami(self, exe: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Email and plan via `claude auth status`, which exposes no token."""
        if exe and time.time() - self._identity[0] > 300:
            email = plan = None
            try:
                done = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                                      timeout=6, stdin=subprocess.DEVNULL)
                status = json.loads(done.stdout)
                email = status.get("email") or status.get("accountEmail")
                plan = status.get("subscriptionType") or status.get("plan")
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
                pass
            self._identity = (time.time(), email, plan)
        return self._identity[1], self._identity[2]

    def _refresh_live(self) -> None:
        # The endpoint rate-limits aggressively, so this interval also holds
        # for forced refreshes.
        if time.time() - self._last_attempt < self._wait:
            return
        self._last_attempt = time.time()
        try:
            token = _claude_token()
            if not token:
                self._last_problem = "未找到 Claude OAuth 登录凭据"
                return
            windows = parse_claude_live(_fetch_claude_live(token))
            self._last_live = (int(time.time()), windows)
            self._last_problem = None
            self._wait = CLAUDE_MIN_REFRESH_SECONDS
        except urllib.error.HTTPError as exc:
            self._last_problem = f"用量接口返回 HTTP {exc.code}"
            if exc.code == 429:
                self._wait = backoff(self._wait, exc.headers.get("Retry-After") if exc.headers else None)
        except Exception as exc:  # the message cannot contain the token: it is only ever in a header
            self._last_problem = f"读取实时配额失败（{type(exc).__name__}）"

    def query(self) -> list[Account]:
        exe = shutil.which("claude")
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        if not exe and not base.is_dir():
            return []
        now = int(time.time())
        email, plan = self._whoami(exe)
        account = Account("claude", "Claude Code", email=email, plan=plan)
        self._refresh_live()
        if self._last_live is None:
            account.note = self._last_problem or CLAUDE_SETUP_NOTE
            return [account]
        captured, windows = self._last_live
        account.source, account.captured_at = "Claude OAuth 用量接口", captured
        account.stale = now - captured > STALE_AFTER_SECONDS
        account.windows = _live_windows(windows, now)
        notes = [self._last_problem] if self._last_problem else []
        if not account.windows:
            notes.append("配额窗口都已重置，等下一次查询")
        account.note = "；".join(notes) or None
        return [account]


# ------------------------------------------------------------------ Grok
def grok_auth_path() -> Path:
    return grok_home() / "auth.json"


def _grok_logins() -> list[dict[str, Any]]:
    auth = json.loads(grok_auth_path().read_text())
    entries = auth.values() if isinstance(auth, dict) else []
    return [entry for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("key"), str) and entry["key"].strip()]


def _renew_grok_login() -> None:
    """Any authenticated CLI command renews the login, and writes it back itself."""
    exe = shutil.which("grok")
    if not exe:
        return
    try:
        subprocess.run([exe, "models"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=GROK_RENEW_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass


def _jwt_expired(token: str, now: float) -> bool:
    """A token that does not parse as a JWT is left for the server to judge."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) <= now
    except (IndexError, KeyError, TypeError, ValueError):
        return False


def parse_grok_billing(payload: Any) -> list[Window]:
    config = payload.get("config") if isinstance(payload, dict) else None
    period = config.get("currentPeriod") if isinstance(config, dict) else None
    if not isinstance(period, dict) or period.get("type") != GROK_WEEKLY_PERIOD:
        return []
    used = _percent(config.get("creditUsagePercent"))
    return [] if used is None else [Window("每周", used, _reset_epoch(period.get("end")))]


def _fetch_grok(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": "xai-grok-cli",
        "Accept": "application/json",
        "User-Agent": f"rex-agents/{__version__}",
    })
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.load(response)


class GrokQuota:
    def __init__(self) -> None:
        self._last: Optional[tuple[int, list[Window], Optional[str], Optional[str]]] = None
        self._last_attempt = 0.0
        self._problem: Optional[str] = None

    def _plan(self, token: str) -> Optional[str]:
        try:
            plan = _fetch_grok(GROK_SETTINGS_URL, token).get("subscription_tier_display")
        except Exception:  # only a label: the quota is still good without it
            return None
        return plan.strip() or None if isinstance(plan, str) else None

    def _refresh(self) -> None:
        # Holds for forced refreshes too, as with the Claude endpoint.
        if time.time() - self._last_attempt < GROK_MIN_REFRESH_SECONDS:
            return
        self._last_attempt = time.time()
        try:
            logins = _grok_logins()
        except (OSError, ValueError):
            self._problem = "无法读取 Grok 登录凭据"
            return
        live = [login for login in logins if not _jwt_expired(login["key"].strip(), time.time())]
        if logins and not live:
            _renew_grok_login()
            try:
                logins = _grok_logins()
            except (OSError, ValueError):
                logins = []
            live = [login for login in logins if not _jwt_expired(login["key"].strip(), time.time())]
            if not live:
                # The last values are still the newest known, and age into
                # stale like any other snapshot.
                self._problem = GROK_EXPIRED_NOTE
                return
        for login in live:
            token = login["key"].strip()
            try:
                windows = parse_grok_billing(_fetch_grok(GROK_USAGE_URL, token))
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    continue
                self._problem = f"用量接口返回 HTTP {exc.code}"
                return
            except Exception as exc:  # the message cannot contain the token: it is only ever in a header
                self._problem = f"读取配额失败（{type(exc).__name__}）"
                return
            if not windows:
                self._problem = "接口没有返回每周配额"
                return
            email = login.get("email") if isinstance(login.get("email"), str) else None
            self._last = (int(time.time()), windows, self._plan(token), email)
            self._problem = None
            return
        # Logged out: values from the old session no longer describe anything.
        self._last = None
        self._problem = GROK_LOGIN_NOTE

    def query(self) -> list[Account]:
        if not grok_auth_path().exists():
            return []
        self._refresh()
        account = Account("grok", "Grok", note=self._problem)
        if self._last:
            now = int(time.time())
            captured, windows, account.plan, account.email = self._last
            account.source, account.captured_at = "Grok CLI 用量接口", captured
            account.stale = now - captured > STALE_AFTER_SECONDS
            account.windows = _live_windows(windows, now)
        return [account]


# ------------------------------------------------------------------ summary
def most_urgent(accounts: list[Account]) -> Optional[dict[str, Any]]:
    """The fresh window with the least left; the sooner reset wins a tie."""
    best: Optional[tuple[float, int, Account, Window]] = None
    for account in accounts:
        if account.stale:
            continue
        for window in account.windows:
            rank = (window.left_percent, window.resets_at or 2**62)
            if best is None or rank < best[:2]:
                best = (*rank, account, window)
    if best is None:
        return None
    return {"agent": best[2].agent, "name": best[2].name, **best[3].to_dict()}


def summarize(accounts: list[Account]) -> dict[str, Any]:
    return {
        "accounts": [a.to_dict() for a in accounts],
        "most_urgent": most_urgent(accounts),
        "unmeasured": [a.name for a in accounts if not a.windows or a.stale],
    }


def age_account(account: Account, now: int) -> Account:
    """Apply the freshness rules again to an account read a while ago."""
    if account.captured_at:
        account.stale = now - account.captured_at > STALE_AFTER_SECONDS
    account.windows = _live_windows(account.windows, now)
    return account


def account_from_dict(data: dict[str, Any]) -> Account:
    windows = [Window(w["label"], w["used_percent"], w.get("resets_at")) for w in data["windows"]]
    return Account(**{**data, "windows": windows})


class QuotaService:
    def __init__(self, scanner: Scanner, ttl: int = 60) -> None:
        self.scanner = scanner
        self.claude = ClaudeQuota()
        self.grok = GrokQuota()
        self.ttl = ttl
        self._lock = threading.Lock()
        self._accounts: Optional[list[Account]] = None
        self._refreshed_at = 0.0

    def due(self) -> bool:
        return self._accounts is None or time.time() - self._refreshed_at >= self.ttl

    def _query(self) -> list[Account]:
        """Together rather than in turn, so the wait is the slowest source and not their sum."""
        with ThreadPoolExecutor(3) as pool:
            sources = [pool.submit(self.claude.query), pool.submit(query_codex, self.scanner),
                       pool.submit(self.grok.query)]
            return [account for source in sources for account in source.result()]

    def get(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if force or self.due():
                self._accounts = self._query()
                self._refreshed_at = time.time()
            return summarize(self._accounts)


def timezone_name() -> str:
    return datetime.now().astimezone().tzname() or ""
