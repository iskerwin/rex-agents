import base64
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from rex_agents import quota
from rex_agents.quota import (
    Account,
    ClaudeQuota,
    GrokQuota,
    Window,
    most_urgent,
    parse_claude_live,
    parse_codex_windows,
    parse_grok_billing,
)

FUTURE = 4_000_000_000


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    monkeypatch.setattr(ClaudeQuota, "_whoami", lambda self, exe: (None, None))
    monkeypatch.setattr(quota, "grok_auth_path", lambda: tmp_path / "grok" / "auth.json")
    monkeypatch.setattr(quota, "_renew_grok_login", lambda: None)


def grok_login(tmp_path, *tokens):
    (tmp_path / "grok").mkdir()
    entries = {f"entry{i}": {"key": token, "refresh_token": "never-used", "email": f"user{i}@example.test"}
               for i, token in enumerate(tokens)}
    (tmp_path / "grok" / "auth.json").write_text(json.dumps(entries))


def grok_billing(used=30):
    period = {"type": quota.GROK_WEEKLY_PERIOD, "end": "2096-10-01T00:00:00Z"}
    return {"config": {"creditUsagePercent": used, "currentPeriod": period}}


def jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def http_error(code):
    return urllib.error.HTTPError("https://example.test", code, "error", None, None)


def test_grok_is_absent_without_a_login():
    assert GrokQuota().query() == []


def test_grok_reports_the_weekly_window_and_plan(tmp_path, monkeypatch):
    grok_login(tmp_path, "token")
    answers = {quota.GROK_USAGE_URL: grok_billing(), quota.GROK_SETTINGS_URL: {"subscription_tier_display": " SuperGrok "}}
    monkeypatch.setattr(quota, "_fetch_grok", lambda url, token: answers[url])
    account = GrokQuota().query()[0]
    assert (account.name, account.plan, account.note, account.stale) == ("Grok", "SuperGrok", None, False)
    assert account.email == "user0@example.test"
    assert [(w.label, w.left_percent) for w in account.windows] == [("每周", 70.0)]
    assert account.windows[0].resets_at == datetime(2096, 10, 1, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("payload", [
    {},
    {"config": {"creditUsagePercent": 30}},
    {"config": {"creditUsagePercent": 30, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_MONTHLY", "end": "2096-10-01T00:00:00Z"}}},
    grok_billing(used=None),
    grok_billing(used=140),
])
def test_grok_missing_values_stay_missing(payload):
    assert parse_grok_billing(payload) == []


def test_grok_expired_login_is_reported_and_never_sent_or_written(tmp_path, monkeypatch):
    grok_login(tmp_path, jwt(1000))
    before = (tmp_path / "grok" / "auth.json").read_bytes()
    monkeypatch.setattr(quota, "_fetch_grok", lambda url, token: pytest.fail("expired token was sent"))
    account = GrokQuota().query()[0]
    assert account.windows == []
    assert account.note == quota.GROK_EXPIRED_NOTE
    assert (tmp_path / "grok" / "auth.json").read_bytes() == before


def test_grok_expired_login_is_renewed_by_the_cli_itself(tmp_path, monkeypatch):
    grok_login(tmp_path, jwt(1000))
    auth = tmp_path / "grok" / "auth.json"
    renewed = jwt(4_000_000_000)
    monkeypatch.setattr(quota, "_renew_grok_login", lambda: auth.write_text(json.dumps({"entry": {"key": renewed}})))
    sent = []

    def fetch(url, token):
        sent.append(token)
        return grok_billing() if url == quota.GROK_USAGE_URL else {}

    monkeypatch.setattr(quota, "_fetch_grok", fetch)
    account = GrokQuota().query()[0]
    assert account.windows and account.note is None
    assert set(sent) == {renewed}


def test_grok_renewal_runs_a_command_without_a_session(monkeypatch):
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(quota.shutil, "which", lambda name: "/bin/grok" if name == "grok" else None)
    monkeypatch.setattr(quota.subprocess, "run", lambda args, **kw: calls.append((args, kw["stdin"])))
    quota._renew_grok_login()
    assert calls == [(["/bin/grok", "models"], quota.subprocess.DEVNULL)]


def test_grok_expiry_keeps_the_last_values_until_they_go_stale(tmp_path, monkeypatch):
    grok_login(tmp_path, jwt(4_000_000_000))
    service = GrokQuota()
    monkeypatch.setattr(quota, "_fetch_grok", lambda url, token: grok_billing() if url == quota.GROK_USAGE_URL else {})
    assert service.query()[0].windows

    (tmp_path / "grok" / "auth.json").write_text(json.dumps({"entry": {"key": jwt(1000)}}))
    service._last_attempt = 0
    account = service.query()[0]
    assert account.windows and account.note == quota.GROK_EXPIRED_NOTE and not account.stale

    captured, windows, plan, email = service._last
    service._last = (captured - quota.STALE_AFTER_SECONDS - 1, windows, plan, email)
    service._last_attempt = 0
    assert service.query()[0].stale


def test_grok_empty_login_file_means_logged_out(tmp_path):
    (tmp_path / "grok").mkdir()
    (tmp_path / "grok" / "auth.json").write_text("{}")
    assert GrokQuota().query()[0].note == quota.GROK_LOGIN_NOTE


def test_grok_tries_the_next_login_after_a_rejected_one(tmp_path, monkeypatch):
    grok_login(tmp_path, "rejected", "good")
    tried = []

    def fetch(url, token):
        if url == quota.GROK_SETTINGS_URL:
            return {}
        tried.append(token)
        if token == "rejected":
            raise http_error(401)
        return grok_billing()

    monkeypatch.setattr(quota, "_fetch_grok", fetch)
    account = GrokQuota().query()[0]
    assert tried == ["rejected", "good"]
    assert account.note is None and account.windows


def test_grok_failure_keeps_the_last_values_and_says_why(tmp_path, monkeypatch):
    grok_login(tmp_path, "token")
    service = GrokQuota()
    monkeypatch.setattr(quota, "_fetch_grok", lambda url, token: grok_billing() if url == quota.GROK_USAGE_URL else {})
    assert service.query()[0].windows

    def unavailable(url, token):
        raise http_error(503)

    monkeypatch.setattr(quota, "_fetch_grok", unavailable)
    service._last_attempt = 0
    account = service.query()[0]
    assert account.windows and account.note == "用量接口返回 HTTP 503"


def test_grok_logout_drops_the_old_values(tmp_path, monkeypatch):
    grok_login(tmp_path, "token")
    service = GrokQuota()
    monkeypatch.setattr(quota, "_fetch_grok", lambda url, token: grok_billing() if url == quota.GROK_USAGE_URL else {})
    assert service.query()[0].windows

    def rejected(url, token):
        raise http_error(401)

    monkeypatch.setattr(quota, "_fetch_grok", rejected)
    service._last_attempt = 0
    account = service.query()[0]
    assert account.windows == [] and account.note == quota.GROK_LOGIN_NOTE


def test_grok_refresh_is_rate_limited_even_when_forced(tmp_path, monkeypatch):
    grok_login(tmp_path, "token")
    calls = []

    def fetch(url, token):
        calls.append(url)
        return grok_billing() if url == quota.GROK_USAGE_URL else {}

    monkeypatch.setattr(quota, "_fetch_grok", fetch)
    service = GrokQuota()
    service.query()
    service.query()
    assert calls.count(quota.GROK_USAGE_URL) == 1


def test_grok_unreadable_login_file_does_not_crash(tmp_path):
    (tmp_path / "grok").mkdir()
    (tmp_path / "grok" / "auth.json").write_text("not json")
    assert GrokQuota().query()[0].note == "无法读取 Grok 登录凭据"


def test_codex_windows_are_labelled_by_duration_not_slot():
    limits = {
        "primary": {"windowDurationMins": 10080, "usedPercent": 20, "resetsAt": FUTURE},
        "secondary": {"windowDurationMins": 300, "usedPercent": 90, "resetsAt": FUTURE},
    }
    windows = parse_codex_windows(limits, "windowDurationMins", "usedPercent", "resetsAt")
    assert [(w.label, w.used_percent) for w in windows] == [("5 小时", 90), ("每周", 20)]


def test_invalid_percent_is_missing_rather_than_zero():
    limits = {"primary": {"windowDurationMins": 300, "usedPercent": 140, "resetsAt": FUTURE},
              "secondary": {"windowDurationMins": 10080, "resetsAt": FUTURE}}
    assert parse_codex_windows(limits, "windowDurationMins", "usedPercent", "resetsAt") == []


def test_levels_use_percent_left():
    assert Window("x", 59).level == "ok"
    assert Window("x", 60).level == "low"
    assert Window("x", 79).level == "low"
    assert Window("x", 80).level == "critical"


def test_most_urgent_skips_stale_accounts():
    fresh = Account("codex", "Codex", windows=[Window("每周", 50, FUTURE)])
    stale = Account("claude", "Claude Code", windows=[Window("每周", 99, FUTURE)], stale=True)
    assert most_urgent([fresh, stale])["name"] == "Codex"
    assert most_urgent([stale]) is None


def test_claude_live_iso_reset_and_optional_windows():
    windows = parse_claude_live({
        "five_hour": {"utilization": 7, "resets_at": "2099-01-01T00:00:00+00:00"},
        "seven_day": {"utilization": 68, "resets_at": "2099-01-02T00:00:00Z"},
        "seven_day_opus": None,
    })
    assert [w.label for w in windows] == ["5 小时", "每周"]
    assert windows[0].resets_at == 4070908800


def test_live_endpoint_is_throttled_even_when_forced(monkeypatch):
    calls = []
    monkeypatch.setattr(quota, "_claude_token", lambda: "tok")
    def fetch(token):
        calls.append(token)
        return {"five_hour": {"utilization": 10, "resets_at": FUTURE}}
    monkeypatch.setattr(quota, "_fetch_claude_live", fetch)
    claude = ClaudeQuota()
    claude.query()
    account = claude.query()[0]
    assert len(calls) == 1
    assert account.source == "Claude OAuth 用量接口" and account.windows[0].used_percent == 10


def test_live_failure_reports_status_without_token(monkeypatch):
    def fail(token):
        raise RuntimeError(f"boom {token}")
    monkeypatch.setattr(quota, "_claude_token", lambda: "sekret-token")
    monkeypatch.setattr(quota, "_fetch_claude_live", fail)
    account = ClaudeQuota().query()[0]
    assert "sekret-token" not in (account.note or "")
    assert account.windows == []


FABLE_LIMITS = [
    {"kind": "session", "percent": 3, "resets_at": "2099-01-01T00:00:00+00:00", "scope": None},
    {"kind": "weekly_all", "percent": 88, "resets_at": "2099-01-02T00:00:00+00:00", "scope": None},
    {"kind": "weekly_scoped", "percent": 18, "resets_at": "2099-01-02T00:00:00+00:00",
     "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
]


def test_claude_live_limits_include_model_windows_only_when_present():
    legacy = {"five_hour": {"utilization": 3}, "seven_day": {"utilization": 88}}
    windows = parse_claude_live({**legacy, "limits": FABLE_LIMITS})
    assert [(w.label, w.used_percent) for w in windows] == [("5 小时", 3), ("每周", 88), ("每周 (Fable)", 18)]
    without = parse_claude_live({**legacy, "limits": FABLE_LIMITS[:2]})
    assert [w.label for w in without] == ["5 小时", "每周"]
    unnamed = {**FABLE_LIMITS[2], "scope": {"model": {"display_name": None}}}
    assert [w.label for w in parse_claude_live({"limits": [unnamed, *FABLE_LIMITS[:1]]})] == ["5 小时"]


def test_without_a_login_claude_says_so(monkeypatch):
    monkeypatch.setattr(quota, "_claude_token", lambda: None)
    account = ClaudeQuota().query()[0]
    assert account.windows == [] and "未找到" in account.note


def test_passed_windows_are_dropped_not_shown_as_usage(monkeypatch):
    monkeypatch.setattr(quota, "_claude_token", lambda: "tok")
    monkeypatch.setattr(quota, "_fetch_claude_live", lambda token: {"five_hour": {"utilization": 99, "resets_at": 1000}})
    account = ClaudeQuota().query()[0]
    assert account.windows == [] and "已重置" in account.note


def test_backoff_follows_retry_after_else_doubles_within_bounds():
    assert quota.backoff(60, None) == 120
    assert quota.backoff(60, "300") == 300
    assert quota.backoff(600, None) == quota.CLAUDE_MAX_BACKOFF_SECONDS
    assert quota.backoff(60, "soon") == 120


def test_a_rate_limited_endpoint_is_asked_less_often(monkeypatch):
    calls = []
    clock = [1_000_000.0]
    monkeypatch.setattr(quota.time, "time", lambda: clock[0])
    monkeypatch.setattr(quota, "_claude_token", lambda: "tok")
    def limited(token):
        calls.append(clock[0])
        raise http_error(429)
    monkeypatch.setattr(quota, "_fetch_claude_live", limited)
    claude = ClaudeQuota()
    for _ in range(10):
        claude.query()
        clock[0] += 61
    # Each wait twice the last after a 429: three calls in ten minutes, not ten.
    assert len(calls) == 3 and calls[1] - calls[0] >= 120 and calls[2] - calls[1] >= 240
