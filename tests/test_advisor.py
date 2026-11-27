import json

from rex_agents.advisor import AdvisorFeed, AdvisorLog, agent_of, model_name, tab_blocks
from rex_agents.pane import build_pane
from rex_agents.text import plain

NOW = 1_795_286_000

VIEW = {"windows": [
    {"layers": [{"blocks": [{"block_id": "block:a"}, {"block_id": "block:pane"}]},
                {"blocks": [{"block_id": "block:float"}]}]},
    {"layers": [{"blocks": [{"block_id": "block:other-tab"}]}]},
]}


def state(**over):
    return {"main": "Opus 5.5", "advisor": "Fable", "busy": False, "turnStartedAt": 0, "quietTurns": 0,
            "counts": {"plan": 3, "error": 0, "done": 1, "other": 0},
            "calls": [{"trigger": "plan", "ms": 3200, "at": (NOW - 12) * 1000}], **over}


def ts(sec):
    return f"2026-11-22T00:00:{sec:02d}.000Z"


def prompt(text="go"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool(name, id_, sec, stop="tool_use"):
    return {"type": "assistant", "timestamp": ts(sec), "advisorModel": "fable",
            "message": {"model": "claude-opus-5-5", "stop_reason": stop,
                        "content": [{"type": "tool_use", "id": id_, "name": name, "input": {}}]}}


def result(id_, error=False, text="ok"):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": id_,
                                                     "is_error": error, "content": text}]}}


def advise(id_, sec, took=2, stop="tool_use"):
    head = {"type": "assistant", "timestamp": ts(sec), "advisorModel": "fable",
            "message": {"model": "claude-opus-5-5", "stop_reason": stop,
                        "content": [{"type": "server_tool_use", "id": id_, "name": "advisor", "input": {}}]}}
    tail = {**head, "timestamp": ts(sec + took),
            "message": {**head["message"], "content": [{"type": "advisor_tool_result", "tool_use_id": id_,
                                                        "content": {"type": "advisor_redacted_result"}}]}}
    return [head, tail]


def log_of(tmp_path, records):
    path = tmp_path / "s.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    log = AdvisorLog(path)
    log.read()
    return log


def test_calls_count_under_the_moment_the_turn_was_in(tmp_path):
    log = log_of(tmp_path, [
        prompt(), *advise("a1", 1),                                  # nothing written yet: plan
        tool("Edit", "t1", 4), result("t1"), *advise("a2", 5),         # after an edit: a check-in
        tool("Bash", "t2", 8), result("t2", True, "boom"),
        tool("Bash", "t3", 9), result("t3", True, "boom"), *advise("a3", 10),  # same failure twice
        prompt(), *advise("a4", 20, stop="end_turn"),                 # the step that ends the turn
    ])
    assert log.counts == {"plan": 1, "other": 1, "error": 1, "done": 1}
    assert [c["trigger"] for c in log.calls] == ["plan", "other", "error", "done"]
    assert log.calls[0]["ms"] == 2000
    st = log.state(True, 123)
    assert (st["main"], st["advisor"], st["busy"], st["turnStartedAt"]) == ("Opus 5.5", "Fable", True, 123)


def test_a_failed_edit_does_not_lock_the_plan_and_subagents_do_not_count(tmp_path):
    side = {**advise("s1", 3)[0], "isSidechain": True}
    log = log_of(tmp_path, [prompt(), tool("Edit", "t1", 1), result("t1", True), side, *advise("a1", 5)])
    assert log.counts["plan"] == 1 and len(log.calls) == 1


def test_turns_that_work_without_calling_go_quiet(tmp_path):
    turns = [r for _ in range(3) for r in (prompt(), tool("Bash", "t", 1), result("t"))]
    log = log_of(tmp_path, [*turns, prompt()])
    assert log.state(False, 0)["quietTurns"] == 3
    log = log_of(tmp_path, [*turns, *advise("a", 9), prompt()])
    assert log.state(False, 0)["quietTurns"] == 0


def test_no_advisor_no_state_and_a_log_is_read_as_it_grows(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({**prompt()}) + "\n")
    log = AdvisorLog(path)
    log.read()
    assert log.state(False, 0) is None
    first, second = advise("a", 1)
    # A line still being written is read once it is whole.
    path.write_text(path.read_text() + json.dumps(first) + "\n" + json.dumps(second)[:20])
    log.read()
    assert log.calls == [] and log.advisor == "fable"
    path.write_text(path.read_text() + json.dumps(second)[20:] + "\n")
    log.read()
    assert len(log.calls) == 1


def test_model_names():
    assert model_name("claude-opus-5-5") == "Opus 5.5"
    assert model_name("fable") == "Fable" and model_name("opus[1m]") == "Opus"
    assert model_name("Fable 5.2") == "Fable 5.2"


def test_the_tab_is_the_window_that_holds_the_pane():
    assert tab_blocks(VIEW, "block:pane") == ["block:a", "block:float"]
    assert tab_blocks(VIEW, "block:gone") == []


class FakeRex:
    def __init__(self):
        self.views = 0

    def view(self, scope):
        assert scope == "block:pane"  # the panel's own block stands for its session
        self.views += 1
        return VIEW

    def foreground(self, scope, block):
        assert scope == "block:pane"
        pid = {"block:a": 41, "block:float": 42}.get(block)
        return {"pid": pid, "name": "claude" if pid == 41 else "zsh"} if pid else None


def test_the_feed_follows_this_tabs_claude_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    (tmp_path / "projects" / "p").mkdir(parents=True)
    (tmp_path / "sessions" / "41.json").write_text(json.dumps({"sessionId": "s41", "status": "busy", "statusUpdatedAt": 7}))
    log = tmp_path / "projects" / "p" / "s41.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in [prompt(), *advise("a", 1)]))
    rex, clock = FakeRex(), [0.0]
    feed = AdvisorFeed({"REX_BLOCK": "block:pane"}, rex, lambda: clock[0])
    [st] = feed()
    assert st["busy"] and st["turnStartedAt"] == 7 and st["counts"]["plan"] == 1
    with open(log, "a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in advise("b", 5)))
    clock[0] = 0.5
    assert feed()[0]["counts"]["plan"] == 1  # the log is read once a second
    clock[0] = 1.0
    assert feed()[0]["counts"]["plan"] == 2
    clock[0] = 5.0
    feed()
    assert rex.views == 2


def test_outside_rex_there_is_no_tab():
    assert AdvisorFeed({}, FakeRex())() == []


def snapshot():
    return {"generated_at": NOW, "usage": {"rows": []}, "tasks": [], "accounts": [
        {"agent": "claude", "name": "Claude Code", "plan": "pro", "workspace": None, "stale": False, "note": None,
         "windows": [{"label": "5 小时", "seconds": 18000, "left_percent": 80.0, "resets_at": NOW + 3600, "level": "ok"}]},
        {"agent": "codex", "name": "Codex", "plan": "team", "workspace": None, "stale": False, "note": None,
         "windows": [{"label": "每周", "seconds": 604800, "left_percent": 90.0, "resets_at": NOW + 86400, "level": "ok"}]},
    ]}


def test_the_advisor_box_sits_under_claudes():
    text = [plain(line) for line in build_pane(snapshot(), None, 40, 30, NOW, [state()])]
    top = next(i for i, t in enumerate(text) if "┐Advisor┌" in t)
    assert "┐Claude┌" in text[top - 3] and "┐Opus 5.5 → Fable┌" in text[top]
    assert "○ idle" in text[top + 1] and text[top + 1].rstrip().endswith("plan 3.2s 12s │")
    assert [t.split()[1] for t in text[top + 2:top + 5]] == ["plan", "error", "done"]
    assert "75%" in text[top + 2] and "┘×4└" in text[top + 5]
    assert "┐Codex┌" in text[top + 6]


def test_a_busy_or_quiet_advisor_says_so_and_check_ins_get_a_row():
    busy = state(busy=True, turnStartedAt=(NOW - 87) * 1000, counts={"plan": 1, "error": 0, "done": 0, "other": 2})
    text = [plain(line) for line in build_pane(snapshot(), None, 40, 30, NOW, [busy])]
    assert any("● build 1:27" in t for t in text)
    assert any(t.split()[1:2] == ["check"] for t in text if t.startswith("│ check"))
    quiet = [plain(line) for line in build_pane(snapshot(), None, 40, 30, NOW, [state(quietTurns=4)])]
    assert any("idle · 4 turns" in t for t in quiet)


def test_a_short_pane_gives_the_advisor_a_line():
    text = [plain(line).rstrip() for line in build_pane(snapshot(), None, 40, 6, NOW, [state()])]
    assert text[1] == "Advisor Fable ×4 · plan"


def test_an_advisor_set_but_never_run_still_shows(tmp_path, monkeypatch):
    from rex_agents import advisor
    log = log_of(tmp_path, [{**prompt()}, {**tool("Bash", "t", 1), "advisorModel": None}, result("t")])
    log.advisor = None
    assert log.state(False, 0) is None
    assert log.state(False, 0, "fable")["advisor"] == "Fable"
    assert advisor.launch_advisor("claude --model opus --advisor fable") == "fable"
    assert advisor.launch_advisor("claude --advisor=off") == ""
    assert advisor.launch_advisor("claude --model opus") is None
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"advisorModel": "fable"}))
    monkeypatch.setattr(advisor, "command_line", lambda pid: "claude")
    assert advisor.configured_advisor(1) == "fable"
    monkeypatch.setattr(advisor, "command_line", lambda pid: "claude --advisor off")
    assert advisor.configured_advisor(1) is None


def test_every_meter_starts_and_ends_in_the_same_column():
    rows = [plain(line)[2:-2] for line in build_pane(snapshot(), None, 40, 30, NOW, [state()])]
    meters = [r for r in rows if "■" in r]
    spans = {(min(r.index(c) for c in "■│" if c in r), max(r.rindex(c) for c in "■│" if c in r)) for r in meters}
    assert len(meters) == 5  # claude 5h, codex 7d, the advisor's three moments
    assert len(spans) == 1


def test_the_feed_says_which_agents_the_tab_runs():
    # Rex sets only REX_BLOCK in a pane; that alone finds the tab.
    feed = AdvisorFeed({"REX_BLOCK": "block:pane"}, FakeRex())
    feed()
    assert feed.agents == ["claude"]
    assert AdvisorFeed({}, FakeRex())() == []
    assert agent_of({"pid": 1, "name": "node", "argv0": "codex"}) == "codex"
    assert agent_of({"pid": 1, "path": "/opt/homebrew/Caskroom/claude-code@latest/2.1.330/claude"}) == "claude"
    assert agent_of({"pid": 1, "name": "zsh", "argv0": "-zsh"}) is None
