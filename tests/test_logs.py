import json

from rex_agents.logs import Rec, Scanner, parse_claude, parse_codex, rollup


def write_jsonl(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(i) + "\n" for i in items))


def claude_line(msg_id="m1", output=10, model="claude-opus-5-5", **extra):
    return {
        "timestamp": "2026-11-01T10:00:00.000Z",
        "requestId": "r1",
        "sessionId": "s1",
        "cwd": "/somewhere/proj",
        "message": {
            "id": msg_id,
            "model": model,
            "content": "SECRET CONTENT",
            "usage": {"input_tokens": 2, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 50, "output_tokens": output},
        },
        **extra,
    }


def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GROK_HOME", raising=False)


def test_claude_repeated_lines_count_once_across_files(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    folder = tmp_path / ".claude/projects/p"
    # One response streamed over three lines, then copied by a resumed session.
    write_jsonl(folder / "a.jsonl", [claude_line(output=1), claude_line(output=5), claude_line(output=10)])
    write_jsonl(folder / "b.jsonl", [claude_line(output=10)])
    records = Scanner().records()
    assert len(records) == 1
    rec = records[0]
    assert (rec.fresh, rec.cache_read, rec.cache_write, rec.output) == (2, 100, 50, 10)


def test_rescanning_does_not_inflate_totals(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    folder = tmp_path / ".claude/projects/p"
    write_jsonl(folder / "a.jsonl", [claude_line()])
    write_jsonl(folder / "b.jsonl", [claude_line()])
    scanner = Scanner()
    first = scanner.records()[0].output
    assert scanner.records()[0].output == first


def test_claude_skips_synthetic_and_keeps_no_content(tmp_path):
    path = tmp_path / "a.jsonl"
    write_jsonl(path, [claude_line(model="<synthetic>"), claude_line(msg_id="m2")])
    records = parse_claude(path).records
    assert [r.key for r in records] == ["m2:r1"]
    assert "SECRET" not in repr(records)


def codex_events(usage, response_id="resp1"):
    return [
        {"timestamp": "2026-11-01T10:00:00.000Z", "type": "session_meta", "payload": {"id": "sess", "cwd": "/w/x"}},
        {"timestamp": "2026-11-01T10:00:01.000Z", "type": "turn_context", "payload": {"model": "gpt-a", "cwd": "/w/x"}},
        {"timestamp": "2026-11-01T10:00:02.000Z", "type": "token_usage_record",
         "payload": {"response_id": response_id, "usage": usage}},
    ]


def test_codex_input_is_split_into_non_overlapping_buckets(tmp_path):
    path = tmp_path / "c.jsonl"
    usage = {"input_tokens": 100, "cached_input_tokens": 70, "cache_write_input_tokens": 5,
             "output_tokens": 9, "reasoning_output_tokens": 3, "total_tokens": 109}
    write_jsonl(path, codex_events(usage))
    rec = parse_codex(path).records[0]
    assert (rec.fresh, rec.cache_read, rec.cache_write, rec.output) == (25, 70, 5, 9)
    assert rec.fresh + rec.cache_read + rec.cache_write + rec.output == usage["total_tokens"]
    assert (rec.model, rec.session) == ("gpt-a", "sess")


def test_codex_fork_replay_is_deduplicated(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    usage = {"input_tokens": 10, "output_tokens": 1}
    write_jsonl(tmp_path / ".codex/sessions/a.jsonl", codex_events(usage))
    write_jsonl(tmp_path / ".codex/sessions/b.jsonl", codex_events(usage))
    assert len(Scanner().records()) == 1


def test_codex_old_logs_fall_back_to_token_count(tmp_path):
    def count(total, last):
        return {"timestamp": "2026-11-01T10:00:00.000Z", "type": "event_msg",
                "payload": {"type": "token_count", "info": {
                    "total_token_usage": {"total_tokens": total},
                    "last_token_usage": {"input_tokens": last, "output_tokens": 0}}}}
    path = tmp_path / "c.jsonl"
    # 500 is inherited by a fork; only `last` of each change counts. 510 repeats.
    write_jsonl(path, [count(500, 10), count(510, 10), count(510, 10), count(530, 20)])
    assert [r.fresh for r in parse_codex(path).records] == [10, 10, 20]


def test_codex_newest_rate_limits_ignore_other_limit_ids(tmp_path):
    def event(stamp, limit_id):
        return {"timestamp": stamp, "type": "event_msg",
                "payload": {"type": "token_count", "rate_limits": {"limit_id": limit_id}}}
    path = tmp_path / "c.jsonl"
    write_jsonl(path, [event("2026-11-01T10:00:00.000Z", "codex"), event("2026-11-01T11:00:00.000Z", "premium")])
    assert parse_codex(path).rate_limits[0] == "2026-11-01T10:00:00.000Z"


def test_rollup_counts_distinct_sessions_and_buckets_by_day():
    records = [
        Rec("a", "claude", 1793500000, "m", "p", "s1", fresh=1, output=2),
        Rec("b", "claude", 1793500100, "m", "p", "s1", fresh=1, output=2),
        Rec("c", "codex", 1793500100, "m", "p", "s1", fresh=1),
    ]
    rows = rollup(records)["rows"]
    claude = next(r for r in rows if r["agent"] == "claude")
    codex = next(r for r in rows if r["agent"] == "codex")
    assert claude["requests"] == 2 and claude["output"] == 4
    assert len(claude["sessions"]) == 1
    # The same id under another agent is another session.
    assert claude["sessions"] != codex["sessions"]


def grok_session(home, name, turns, cwd="/work/app"):
    folder = home / ".grok" / "sessions" / "group" / name
    folder.mkdir(parents=True)
    (folder / "usage.json").write_text(json.dumps({"sessionId": name, "session": {"inputTokens": 999999}, "turns": turns}))
    (folder / "summary.json").write_text(json.dumps({"info": {"id": name, "cwd": cwd}, "session_summary": "SECRET"}))
    return folder


def grok_turn(ended="2026-11-05T02:32:59+00:00", **models):
    usage = {model: {"inputTokens": 100, "cachedReadTokens": 60, "cacheCreationTokens": 10, "outputTokens": 7,
                     "reasoningTokens": 5, "modelCalls": 3} for model in models}
    return {"endedAt": ended, "primaryModelId": next(iter(models)), "modelUsage": usage}


def test_grok_buckets_turns_and_ignores_the_session_totals(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    grok_session(tmp_path, "s1", [grok_turn(**{"grok-a": 1, "grok-b": 1})])
    records = Scanner().records()
    assert sorted(r.model for r in records) == ["grok-a", "grok-b"]
    rec = next(r for r in records if r.model == "grok-a")
    assert (rec.agent, rec.fresh, rec.cache_read, rec.cache_write, rec.output, rec.requests) == ("grok", 30, 60, 10, 7, 3)
    assert rec.project == "/work/app" and rec.session == "s1"
    assert "SECRET" not in repr(records)


def test_grok_fork_inherits_turns_without_counting_them_twice(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    inherited = grok_turn(**{"grok-a": 1})
    grok_session(tmp_path, "parent", [inherited])
    grok_session(tmp_path, "fork", [inherited, grok_turn(ended="2026-11-05T03:00:00+00:00", **{"grok-a": 1})])
    assert len(Scanner().records()) == 2


def test_grok_requests_reach_the_rollup(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    grok_session(tmp_path, "s1", [grok_turn(**{"grok-a": 1})])
    assert rollup(Scanner().records())["rows"][0]["requests"] == 3


def test_grok_skips_a_broken_or_partial_file(monkeypatch, tmp_path):
    isolate(monkeypatch, tmp_path)
    folder = grok_session(tmp_path, "s1", [{"endedAt": None}, "junk"])
    assert Scanner().records() == []
    (folder / "usage.json").write_text('{"turns": [')
    assert Scanner().records() == []


def test_model_names_follow_what_the_vendors_publish():
    from rex_agents.logs import model_name
    assert model_name("claude-opus-5-5") == "Claude Opus 5.5"
    assert model_name("claude-haiku-5-5") == "Claude Haiku 5.5"
    assert model_name("claude-opus-5") == "Claude Opus 5"
    assert model_name("claude-haiku-4-5-20251001") == "Claude Haiku 4.5"
    assert model_name("gpt-6.1-sol") == "GPT-6.1 Sol"
    assert model_name("gpt-6-astra") == "GPT-6 Astra"
    assert model_name("grok-4.7") == "Grok 4.7"
    # No published name: not made up, not guessed.
    for unknown in ("grok-4.7-build", "codex-auto-review", "(未知)"):
        assert model_name(unknown) == unknown
