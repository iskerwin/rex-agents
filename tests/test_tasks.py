import json
from pathlib import Path

from rex_agents.logs import Rec, ParsedFile, Scanner, parse_claude, rollup
from rex_agents.pricing import cost_usd
from rex_agents.tasks import _claude_title, parse_etime, parse_ps


def test_etime_forms():
    assert parse_etime("00:03") == 3
    assert parse_etime("09:29:49") == 9 * 3600 + 29 * 60 + 49
    assert parse_etime("01-22:29:08") == 86400 + 22 * 3600 + 29 * 60 + 8


def test_ps_keeps_terminal_agents_without_their_helpers():
    output = "\n".join([
        "  100     1   01:00 ttys000 claude",
        "  101   100   00:30 ttys000 /opt/homebrew/bin/codex",  # started by the Claude session above
        "  200     1   01:00 ??      /Users/me/.codex/bin/codex",  # the desktop daemon
        "  300     1   02:00 ttys001 /usr/local/bin/grok",
        "  400     1   02:00 ttys002 zsh",
    ])
    procs = parse_ps(output, now=1000)
    assert [(p.pid, p.agent, p.started_at) for p in procs] == [(100, "claude", 940), (300, "grok", 880)]


def test_cost_uses_each_bucket_and_leaves_unknown_models_unpriced():
    assert cost_usd("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000) == 4 + 0.2 + 5 + 20
    assert cost_usd("grok-4.7-build", 1, 1, 1, 1) is None


def test_rollup_prices_rows_and_keeps_only_the_last_day_hourly():
    now = 1_800_000_000.0
    old = Rec("a", "claude", now - 2 * 86400, "claude-opus-5-5", "~/p", "s1", fresh=1_000_000)
    new = Rec("b", "codex", now - 60, "gpt-6.1-sol", "~/p", "s2", output=1_000_000)
    usage = rollup([old, new], now=now)
    assert sorted(r["cost"] for r in usage["rows"]) == [4, 10]
    assert [(h["agent"], h["output"], len(h["sessions"])) for h in usage["hours"]] == [("codex", 1_000_000, 1)]


def test_latest_session_is_the_last_written_file_per_project():
    scanner = Scanner()
    older = ParsedFile(records=[Rec("a", "codex", 1.0, "m", "~/p", "old")])
    newer = ParsedFile(records=[Rec("b", "codex", 2.0, "m", "~/p", "new")])
    scanner._files = {"x": ((5_000_000_000, 1), older), "y": ((9_000_000_000, 1), newer)}
    latest = scanner.latest_sessions()[("codex", "~/p")]
    assert (latest.session, latest.written_at) == ("new", 9)


def test_cost_prices_long_cache_writes_at_their_own_rate():
    assert cost_usd("claude-opus-5-5", 0, 0, 3_000_000, 0, cache_write_long=1_000_000) == 2 * 5 + 8


def test_claude_title_comes_from_its_own_record_and_not_from_a_message(tmp_path: Path):
    path = tmp_path / "s1.jsonl"
    lines = [
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "第一版标题"},
        {"type": "user", "message": {"content": '{"type": "ai-title", "aiTitle": "别读这个"}'}},
        {"type": "ai-title", "sessionId": "s1", "aiTitle": "重构登录流程"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines))
    assert parse_claude(path).title == "重构登录流程"


def test_given_name_beats_generated_title_which_beats_derived_name():
    scanner = Scanner()
    scanner._files = {Path("s1.jsonl"): ((1, 1), ParsedFile(title="生成的标题"))}
    assert _claude_title({"name": "我的名字", "nameSource": "user", "sessionId": "s1"}, scanner) == "我的名字"
    assert _claude_title({"name": "project-e2", "nameSource": "derived", "sessionId": "s1"}, scanner) == "生成的标题"
    assert _claude_title({"name": "project-e2", "nameSource": "derived", "sessionId": "other"}, scanner) == "project-e2"
