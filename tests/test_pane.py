import time

import pytest

from rex_agents.pane import UNLIT, build_pane, costs, even_pace_left, meter, short_label, until, window_seconds
from rex_agents.text import line_width, plain

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
