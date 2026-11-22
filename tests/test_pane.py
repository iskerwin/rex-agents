import time
from datetime import date, datetime, timedelta

import pytest

from rex_agents.pane import UNLIT, build_pane, chart, costs, count, even_pace_left, meter, short_label, trend, until, window_seconds
from rex_agents.text import line_width, plain, to_ansi

NOW = 1_795_286_000


def snapshot(**over):
    snap = {
        "generated_at": NOW - 30,
        "accounts": [
            {"agent": "claude", "name": "Claude Code", "plan": "pro", "workspace": None, "stale": False, "note": None,
             "windows": [
                 {"label": "5 小时", "left_percent": 80.0, "resets_at": NOW + 3 * 3600, "level": "ok"},
                 {"label": "Fable 每周", "left_percent": 15.0, "resets_at": NOW + 3 * 86400, "level": "critical"},
                 {"label": "每周", "left_percent": 50.0, "resets_at": NOW - 60, "level": "ok"},
             ]},
            {"agent": "grok", "name": "Grok", "plan": "Free", "workspace": None, "stale": True,
             "note": "用量接口返回 HTTP 401", "windows": []},
        ],
        "tasks": [
            {"agent": "codex", "title": "旧任务", "project": "~/a", "state": "idle", "since": NOW - 7200},
            {"agent": "claude", "title": "迁移到侧边栏", "project": "~/b", "state": "running", "since": NOW - 600},
        ],
        "usage": {"rows": []},
    }
    return {**snap, **over}


@pytest.mark.parametrize("width,height", [(38, 30), (60, 12), (30, 3), (10, 5)])
def test_every_row_spans_the_pane_exactly(width, height):
    lines = build_pane(snapshot(), None, width, height, NOW)
    assert len(lines) == height
    assert {line_width(line) for line in lines} == {width}


def test_boxes_per_agent_then_cost_then_tasks():
    text = [plain(line) for line in build_pane(snapshot(), None, 38, 30, NOW)]
    assert text[0].startswith("╭─┐Claude┌") and text[0].rstrip().endswith("┐Pro┌─╮")
    assert any(" Fable 7d " in t and "15%!!" in t for t in text)
    # A window past its reset is gone; a stale account says so and why.
    assert not any("│ 7d " in t for t in text)
    assert any("┘stale└" in t for t in text)
    assert any("HTTP 401" in t for t in text)
    assert any("┘30s└" in t for t in text)
    tasks = text.index(next(t for t in text if "┐Tasks┌" in t))
    assert "┐1/2┌" in text[tasks]
    assert "● 迁移到侧边栏" in text[tasks + 1] and "○ 旧任务" in text[tasks + 2]
    # The task box runs to the pane's foot.
    assert text[-1].startswith("╰")


def test_a_short_pane_folds_to_a_line_per_agent():
    text = [plain(line).rstrip() for line in build_pane(snapshot(), "boom", 38, 6, NOW)]
    assert text[0] == "Claude 5h 80% · Fable 7d 15%"
    assert text[2].endswith("failed")


def test_nothing_yet_and_failure_say_so():
    assert plain(build_pane(None, None, 30, 2, NOW)[0]).strip() == "Loading"
    assert plain(build_pane(None, "boom", 30, 2, NOW)[0]).strip() == "boom"


def test_meter_lights_what_is_left_and_marks_an_even_pace():
    cells = meter(50, 25, 9)
    assert [c.text for c in cells] == ["■", "■", "│", "■", "■", "■", "■", "■", "■"]
    assert [c.color == UNLIT for c in cells if c.text == "■"] == [False] * 4 + [True] * 4


def test_pace_notch_sits_between_cells_and_hides_none():
    # The notch falls exactly on the lit edge: every lit cell still shows.
    cells = meter(50, 50, 9)
    assert [c.text for c in cells] == ["■"] * 4 + ["│"] + ["■"] * 4
    assert [c.color == UNLIT for c in cells if c.text == "■"] == [False] * 4 + [True] * 4
    assert [c.text for c in meter(100, 0, 5)] == ["│", "■", "■", "■", "■"]
    assert [c.text for c in meter(100, 100, 5)] == ["■", "■", "■", "■", "│"]
    assert len(meter(30, None, 9)) == len(meter(30, 40, 9)) == 9
    assert all(c.text == "■" for c in meter(None, None, 4)) and all(c.color == UNLIT for c in meter(None, None, 4))


def test_labels_spans_and_pace():
    assert short_label("5 小时") == "5h" and short_label("Fable 每周") == "Fable 7d"
    assert window_seconds("5 小时") == 18000 and window_seconds("每周") == 604800 and window_seconds("月度") is None
    assert even_pace_left({"label": "5 小时", "resets_at": NOW + 9000}, NOW) == 50
    assert until(3 * 86400 + 5 * 3600) == "3d5h" and until(2 * 3600 + 41 * 60) == "2h41m"


def test_cost_counts_today_and_the_month_and_unpriced_is_not_zero():
    today = time.strftime("%Y-%m-%d", time.localtime(NOW))
    rows = [{"date": today, "cost": 1.5}, {"date": today, "cost": None}, {"date": "1999-01-01", "cost": 9}]
    assert costs(rows, NOW) == (1.5, 1.5)
    assert costs([{"date": today, "cost": None}], NOW) == (None, None)


def test_colors_come_from_the_terminal_palette():
    # No 24-bit or 256-color codes: the terminal's theme decides every color.
    text = "".join(to_ansi(line, True) for line in build_pane(snapshot(), None, 40, 30, NOW))
    assert "\x1b[" in text and "38;2;" not in text and "38;5;" not in text


def usage_row(agent, sessions, fresh=0, cache_read=0, cache_write=0, output=0, **key):
    return {"agent": agent, "sessions": sessions, "fresh": fresh, "cache_read": cache_read,
            "cache_write": cache_write, "output": output, **key}


def hour_of(ts):
    return int(datetime.fromtimestamp(ts).replace(minute=0, second=0, microsecond=0).timestamp())


USAGE = {
    "rows": [usage_row("claude", [0, 1], fresh=100, cache_read=10**9, output=50, date=date.fromtimestamp(NOW).isoformat()),
             usage_row("claude", [2], cache_write=40, date=(date.fromtimestamp(NOW) - timedelta(days=6)).isoformat()),
             usage_row("claude", [3], fresh=999, date=(date.fromtimestamp(NOW) - timedelta(days=7)).isoformat()),
             usage_row("codex", [4], fresh=7, date=date.fromtimestamp(NOW).isoformat())],
    "hours": [usage_row("claude", [0], output=30, hour=hour_of(NOW)),
              usage_row("claude", [1], fresh=10, hour=hour_of(NOW) - 23 * 3600),
              usage_row("claude", [5], fresh=10**6, hour=hour_of(NOW) - 24 * 3600)],
}


def test_trend_sums_one_agents_tokens_per_bar_without_cache_reads():
    assert trend(USAGE, "claude", "24h", NOW) == ([10] + [0] * 22 + [30], 2)
    assert trend(USAGE, "claude", "7d", NOW) == ([40, 0, 0, 0, 0, 0, 150], 3)
    assert trend(USAGE, "claude", "30d", NOW)[0][-8] == 999
    assert trend({}, "grok", "24h", NOW) == ([0] * 24, 0)


def test_chart_draws_bars_in_eighths_and_keeps_the_newest():
    assert [plain(r) for r in chart([0, 1, 8], 3, 1)] == ["▁▂█"]
    assert [plain(r) for r in chart([16, 4], 2, 2)] == ["█ ", "█▄"]
    assert [plain(r) for r in chart([5, 6, 7], 2, 1)] == ["▇█"]


def test_bars_sit_evenly_with_one_cell_gaps_when_there_is_room():
    assert [plain(r) for r in chart([2, 4], 7, 1)] == [" ▄▄ ██ "]
    assert plain(chart([8] * 7, 35, 1)[0]) == ("████ " * 7)[:-1] + " "


def test_a_bar_of_one_cell_stands_side_by_side_with_no_gaps():
    assert plain(chart([8] * 24, 35, 1)[0]) == " " * 5 + "█" * 24 + " " * 6
    assert plain(chart([8] * 30, 35, 1)[0]) == " " * 2 + "█" * 30 + " " * 3


def test_chart_keeps_unused_spans_on_an_unlit_baseline():
    rows = chart([0, 16, 0], 3, 2)
    assert [plain(r) for r in rows] == [" █ ", "▁█▁"]
    assert [s.color for s in rows[1] if s.text == "▁"] == [UNLIT, UNLIT]
    assert [plain(r) for r in chart([0, 0], 4, 1)] == ["▁ ▁ "]


def test_counts_read_short():
    assert [count(n) for n in (950, 12_345, 3_890_000, 1_200_000_000)] == ["950", "12.3K", "3.89M", "1.2B"]


def test_agents_in_the_tab_get_a_trend_when_there_is_room():
    snap = snapshot(usage=USAGE)
    text = [plain(r) for r in build_pane(snap, None, 40, 40, NOW, (), ["claude"], "7d")]
    head = next(i for i, t in enumerate(text) if "tokens" in t)
    assert "190 tokens · 3 sessions" in text[head] and text[head].rstrip(" │").endswith("24h 7d 30d")
    assert text[head - 1].startswith("│ ") and "█" in "".join(text[head + 1:head + 4])
    assert sum("tokens" in t for t in text) == 1  # codex is not in the tab
    assert not any("tokens" in t for t in map(plain, build_pane(snap, None, 40, 40, NOW)))
    assert not any("tokens" in t for t in map(plain, build_pane(snap, None, 40, 16, NOW, (), ["claude"])))


def test_the_span_tabs_are_clickable_through_the_box():
    from rex_agents.text import key_at
    lines = build_pane(snapshot(usage=USAGE), None, 40, 40, NOW, (), ["claude"], "24h")
    row = next(line for line in lines if "tokens" in plain(line) or "sessions" in plain(line))
    text = plain(row)
    assert [key_at(row, text.index(s)) for s in ("24h", "7d", "30d")] == ["1", "2", "3"]


def test_cost_shows_each_day_and_where_the_month_lands():
    from rex_agents.pane import daily_costs, month_end
    today = datetime.fromtimestamp(NOW).date()
    rows = [{"date": today.isoformat(), "cost": 2.0}, {"date": today.isoformat(), "cost": 1.0},
            {"date": (today - timedelta(days=2)).isoformat(), "cost": 4.0},
            {"date": (today - timedelta(days=40)).isoformat(), "cost": 9.0}]
    assert daily_costs(rows, NOW, 4) == [0.0, 4.0, 0.0, 3.0]
    t = datetime.fromtimestamp(NOW)
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed = (t - start).total_seconds()
    days = ((start + timedelta(days=32)).replace(day=1) - start).total_seconds()
    assert month_end(100.0, NOW) == pytest.approx(100 * days / elapsed)
    assert month_end(None, NOW) is None and month_end(0.0, NOW) is None
    lines = build_pane(snapshot(usage={"rows": rows}), None, 40, 40, NOW)
    text = [plain(l) for l in lines]
    at = next(i for i, t in enumerate(text) if "Cost" in t)
    assert "→ ≈$" in text[at + 1] and "█" in text[at + 3]


def test_the_cost_chart_folds_before_the_trends_and_the_tasks_keep_their_edge():
    snap = snapshot(usage={"rows": [{"date": datetime.fromtimestamp(NOW).date().isoformat(), "cost": 3.0,
                                     "agent": "claude", "fresh": 0, "cache_write": 0, "output": 5, "sessions": [0]}]})
    def chart_rows(height):
        text = [plain(l) for l in build_pane(snap, None, 40, height, NOW, (), ["claude"])]
        assert text[-1].startswith("╰") and text[-1].endswith("╯")
        at = next(i for i, t in enumerate(text) if "Cost" in t)
        return "tokens" in "".join(text), text[at + 2].startswith("╰")
    # Rows: Claude 2 windows + trend 4 + edges, Grok, cost, tasks.
    seen = [chart_rows(h) for h in range(14, 31)]
    assert seen[-1] == (True, False)
    # Growing the pane, the trend comes back before the cost chart does.
    assert seen.index((True, True)) < seen.index((True, False))


def test_stale_says_for_how_long_and_restored_data_says_refreshing():
    accounts = snapshot()["accounts"]
    accounts[1] = {**accounts[1], "captured_at": NOW - 3 * 3600}
    text = "\n".join(plain(l) for l in build_pane(snapshot(accounts=accounts), None, 40, 30, NOW))
    assert "stale 3h" in text
    text = "\n".join(plain(l) for l in build_pane(snapshot(refreshing=True, tasks=[]), None, 40, 30, NOW))
    assert "refreshing" in text and "Loading" in text and "No agents running" not in text


def test_a_task_in_a_rex_pane_is_a_link_across_its_whole_row():
    from rex_agents.text import key_at
    tasks = [{"agent": "claude", "pid": 7, "title": "迁移", "project": "~/b", "state": "running", "since": NOW - 600},
             {"agent": "codex", "pid": 8, "title": "别处", "project": "~/a", "state": "idle", "since": NOW - 60}]
    lines = build_pane(snapshot(tasks=tasks), None, 40, 30, NOW, linked={7})
    linked = next(l for l in lines if "迁移" in plain(l))
    other = next(l for l in lines if "别处" in plain(l))
    assert plain(linked).rstrip(" │").endswith("10m ↗") and "↗" not in plain(other)
    assert {key_at(linked, c) for c in range(2, 38)} == {"task:7"}
    assert key_at(linked, 0) is None and {key_at(other, c) for c in range(40)} == {None}


def test_the_cost_box_opens_to_each_models_tokens_and_cost():
    from rex_agents.pane import model_usage
    from rex_agents.text import key_at
    today = datetime.fromtimestamp(NOW).date()
    def row(agent, model, name, out, cost, days_ago=0):
        return {"date": (today - timedelta(days=days_ago)).isoformat(), "agent": agent, "model": model,
                "model_name": name, "fresh": 0, "cache_read": 99, "cache_write": 0, "output": out, "cost": cost}
    rows = [row("codex", "gpt", "GPT-6.1 Sol", 50, 1.0), row("claude", "opus", "Claude Opus 5.5", 10, 2.0),
            row("claude", "opus", "Claude Opus 5.5", 5, 1.0, 1), row("claude", "fable", "Claude Fable 5.2", 30, None),
            row("claude", "sonnet", "Claude Sonnet 5.5", 99, 9.0, 20), row("grok", "g", "g", 0, None)]
    assert model_usage(rows, "7d", NOW) == [("claude", "Fable 5.2", 30, None), ("claude", "Opus 5.5", 15, 3.0),
                                            ("codex", "GPT-6.1 Sol", 50, 1.0)]
    assert ("claude", "Sonnet 5.5", 99, 9.0) in model_usage(rows, "30d", NOW)
    closed = build_pane(snapshot(usage={"rows": rows}), None, 40, 40, NOW)
    edge = next(l for l in closed if "Cost" in plain(l))
    assert "▸ models" in plain(edge) and "models" in {key_at(edge, c) for c in range(40)}
    assert not any("Opus 5.5" in plain(l) for l in closed)
    opened = build_pane(snapshot(usage={"rows": rows}), None, 40, 40, NOW, models="7d")
    text = [plain(l) for l in opened]
    assert any("▾ models" in t for t in text)
    head = next(l for l in opened if plain(l).startswith("│ Models"))
    assert {key_at(head, c) for c in range(40)} == {None, "models:7d", "models:30d"}
    assert any("● Fable 5.2" in t and t.rstrip(" │").endswith("—") for t in text)
    assert any("● Opus 5.5" in t and "$3.00" in t for t in text)
    # Short of rows the list is cut to fit, and the tasks keep their edge.
    tight = build_pane(snapshot(usage={"rows": rows}), None, 40, 16, NOW, models="7d")
    assert plain(tight[-1]).startswith("╰") and any(plain(l).startswith("│ Models") for l in tight)
