"""The Agents panel, drawn in btop's style from a snapshot. Pure: no
I/O and no clock.

One box per agent with a meter per quota window, then the cost, then the
running tasks down to the pane's foot. Short of rows, one line per agent.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional, Sequence

from rex_agents.text import MUTED, Line, Span, fit, line_width, pad_text, plain, plan_text, text_width


# The terminal's own 16 colors, so the panel takes on its theme, whatever
# that is. btop's layout: each box an outline dimmed from its hue, titles
# bright, levels green to red, unlit meter cells the theme's bright black.
BOX = {"claude": "2;31", "codex": "2;32", "grok": "2;34", "cost": "2;33", "tasks": "2;36"}
OTHER_BOX = BOX["tasks"]
TITLE = "97"
UNLIT = "90"
LEVEL = {"ok": "32", "low": "33", "critical": "31"}
# Never by color alone.
# A mark of its own shape: a ■ would read as one more cell of the meter.
MARK = {"ok": "", "low": "!", "critical": "!!"}
# Every meter row, quota or advisor, shares these columns, so the meters line
# up: a label, the meter, the percent, then the mark and reset (or a count).
PERCENT_W = 5
TAIL_W = 2 + 6
WARN = LEVEL["low"]
ERROR = LEVEL["critical"]
# advisor-live's own: the advisor violet, each moment its color.
BOX["advisor"] = "2;35"
VIOLET = "35"
MOMENTS = [("plan", "plan", VIOLET), ("error", "error", ERROR), ("done", "done", LEVEL["ok"]), ("other", "check", MUTED)]

UNIT = {"分钟": "m", "小时": "h", "天": "d"}
SECONDS = {"m": 60, "h": 3600, "d": 86400}


def short_label(label: str) -> str:
    """`5 小时` reads `5h`; `每周` and `Fable 每周` read `7d` and `Fable 7d`."""
    label = re.sub(r"(\d+)\s*(分钟|小时|天)", lambda m: m[1] + UNIT[m[2]], label)
    return label.replace("每周", "7d").strip()


def window_seconds(label: str) -> Optional[int]:
    m = re.search(r"(\d+)([mhd])$", short_label(label))
    return int(m[1]) * SECONDS[m[2]] if m else None


def even_pace_left(window: dict[str, Any], now: int) -> Optional[float]:
    """The percent left if use had been even since the window began."""
    span, resets_at = window_seconds(window["label"]), window.get("resets_at")
    if not span or resets_at is None:
        return None
    return min(max((resets_at - now) / span * 100, 0), 100)


def until(seconds: float) -> str:
    """Two largest units, no spaces: `12m`, `2h41m`, `3d5h`."""
    s = max(0, round(seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d{h}h" if d else f"{h}h{m}m" if h else f"{m}m"


def ago(seconds: float) -> str:
    """The largest whole unit: `42s`, `11m`, `3h`, `2d`."""
    s = max(0, int(seconds))
    return f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"


def money(value: Optional[float]) -> str:
    return "—" if value is None else f"${round(value)}" if value >= 100 else f"${value:.2f}"


def costs(rows: list[dict[str, Any]], now: int) -> tuple[Optional[float], Optional[float]]:
    """Today's and this month's cost. None when tokens exist but none has a
    public price, which is not the same as costing nothing."""
    today = datetime.fromtimestamp(now).date().isoformat()

    def priced(selected: list[dict[str, Any]]) -> Optional[float]:
        known = [r["cost"] for r in selected if r.get("cost") is not None]
        return None if selected and not known else sum(known)

    return (priced([r for r in rows if r["date"] == today]),
            priced([r for r in rows if r["date"].startswith(today[:7])]))


def meter(left: Optional[float], pace: Optional[float], width: int) -> Line:
    """`width` columns: cells of ■, lit for what is left, and a bright │ between
    the cells where an even pace would have the level by now. The │ takes a
    column of its own, so it never hides whether a cell is lit."""
    cells = width if pace is None else width - 1
    lit = 0 if left is None else round(min(max(left, 0), 100) / 100 * cells)
    line: Line = [Span("■", None if i < lit else UNLIT) for i in range(cells)]
    if pace is not None:
        line.insert(round(min(max(pace, 0), 100) / 100 * cells), Span("│", TITLE))
    return line


def _merge(line: Line) -> Line:
    """Adjacent spans of one style as one, so a meter is a few escapes."""
    out: Line = []
    for span in line:
        if out and (out[-1].color, out[-1].bold) == (span.color, span.bold):
            out[-1] = Span(out[-1].text + span.text, span.color, span.bold)
        else:
            out.append(span)
    return out


class _Boxes:
    """Boxes as btop draws them: `╭─┐title┌───┐label┌─╮`, rows between `│`
    bars, an optional label set into the bottom edge."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.inner = width - 4

    def edge(self, top: bool, color: str, title: str = "", label: str = "", label_color: str = TITLE) -> Line:
        left, right = ("╭", "╮") if top else ("╰", "╯")
        opening, closing = ("┐", "┌") if top else ("┘", "└")
        room = self.width - 2 - (text_width(title) + 3 if title else 0)
        label = plain(fit([Span(label)], min(text_width(label), room - 4))) if label and room >= 6 else ""
        fill = room - (text_width(label) + 3 if label else 0)
        line: Line = [Span(left + ("─" + opening if title else ""), color)]
        if title:
            line += [Span(title, TITLE, True), Span(closing, color)]
        line.append(Span("─" * max(fill, 0) + (opening if label else ""), color))
        if label:
            line += [Span(label, label_color), Span(closing + "─", color)]
        line.append(Span(right, color))
        return line

    def box(self, title: str, color: str, rows: list[Line], top: str = "", foot: str = "",
            foot_color: str = TITLE) -> list[Line]:
        body = [[Span("│ ", color), *fit(row, self.inner), Span(" │", color)] for row in rows]
        return [self.edge(True, color, title, top), *body, self.edge(False, color, "", foot, foot_color)]


def _spread(left: Line, right: Line, width: int) -> Line:
    """`left` and `right` at either end of `width` cells, the left cut first."""
    room = width - line_width(right) - 1
    if line_width(left) > room:
        left = fit(left, max(room, 0))
    return [*left, Span(" " * max(width - line_width(left) - line_width(right), 0)), *right]


def build_pane(snap: Optional[dict[str, Any]], error: Optional[str], width: int, height: int, now: int,
               advisors: Sequence[dict[str, Any]] = ()) -> list[Line]:
    """Exactly `height` lines of `width` cells. `advisors` are the states
    advisor-live shares of the Claude Code sessions in this tab."""
    width, height = max(width, 1), max(height, 1)
    if snap is None:
        lines: list[Line] = [[Span(error, ERROR)] if error else [Span("Loading", MUTED)]]
    elif width < 24:
        lines = [[Span("Too narrow", MUTED)]]
    else:
        lines = _layout(snap, error, width, height, now, advisors)
    lines = [fit(_merge(line), width) for line in lines[:height]]
    return [*lines, *[[Span(" " * width)] for _ in range(height - len(lines))]]


def _moments(state: dict[str, Any]) -> list[tuple[str, str, str]]:
    """The moments a box lists: the check-ins only once there are some."""
    return [m for m in MOMENTS if m[0] != "other" or state["counts"].get("other", 0) > 0]


def _advisor_rows(state: dict[str, Any], inner: int, label_w: int, meter_w: int, now: int) -> list[Line]:
    """The main model's state and the last call, then a meter per moment."""
    now_ms = now * 1000
    calls = state.get("calls") or []
    last = calls[-1] if calls else None
    if state.get("busy"):
        secs = max(0, (now_ms - state.get("turnStartedAt", now_ms)) // 1000)
        left: Line = [Span(f"● build {secs // 60}:{secs % 60:02d}", VIOLET)]
    else:
        left = [Span("○ idle", MUTED)]
    if state.get("quietTurns"):
        right: Line = [Span(f"idle · {state['quietTurns']} turns", WARN)]
    elif last:
        right = [Span(f"{last['trigger'] if last['trigger'] != 'other' else 'check'} {last['ms'] / 1000:.1f}s ", MUTED),
                 Span(ago((now_ms - last["at"]) / 1000), None, True)]
    else:
        right = [Span("on call", MUTED)]
    rows = [_spread(left, right, inner)]
    total = sum(state["counts"].values())
    for key, name, color in _moments(state):
        k = state["counts"].get(key, 0)
        lit = 0 if not k else max(1, round(k / total * meter_w))
        lit_now = last is not None and last["trigger"] == key
        rows.append([Span(pad_text(name, label_w + 1), color if lit_now else None if k else MUTED, lit_now),
                     Span("■" * lit, color), Span("■" * (meter_w - lit), UNLIT),
                     Span(pad_text(f"{round(k / total * 100) if total else 0}%", PERCENT_W, True),
                          None if k else MUTED),
                     Span(pad_text(str(k), TAIL_W, True), color if k else MUTED, bool(k))])
    return rows


def _layout(snap: dict[str, Any], error: Optional[str], width: int, height: int, now: int,
            advisors: Sequence[dict[str, Any]]) -> list[Line]:
    boxes = _Boxes(width)
    accounts = snap["accounts"]

    def live(account: dict[str, Any]) -> list[dict[str, Any]]:
        return [w for w in account["windows"] if w.get("resets_at") is None or w["resets_at"] > now]

    def percent(window: dict[str, Any]) -> str:
        left = window.get("left_percent")
        return "—" if left is None else f"{round(left)}%"

    today, month = costs(snap.get("usage", {}).get("rows", []), now)
    cost_row = _spread([Span("Today ", MUTED), Span(money(today), None, True)],
                       [Span("Month ", MUTED), Span(money(month), None, True)], boxes.inner)
    age = "failed" if error else ago(now - snap["generated_at"])

    account_rows = sum(max(1, len(live(a))) + 2 for a in accounts)
    account_rows += sum(len(_moments(st)) + 3 for st in advisors)
    if height < account_rows + 3 + 3:
        compact: list[Line] = []
        for a in accounts:
            line: Line = [Span(pad_text(a["agent"].capitalize(), 7), MUTED if a.get("stale") else None, True)]
            for i, w in enumerate(live(a)):
                line += [Span(" · " if i else "", MUTED), Span(short_label(w["label"]) + " ", MUTED),
                         Span(percent(w), MUTED if a.get("stale") else LEVEL[w["level"]], True)]
            compact.append(line)
            if a["agent"] == "claude":
                for st in advisors:
                    calls = st.get("calls") or []
                    tail = [Span(" · ", MUTED), Span(calls[-1]["trigger"], MUTED)] if calls else []
                    compact.append([Span(pad_text("Advisor", 8), MUTED), Span(st["advisor"], VIOLET, True),
                                    Span(f" ×{sum(st['counts'].values())}"), *tail])
        return [*compact, _spread(cost_row, [Span(age, ERROR if error else MUTED)], width)]

    labels = [short_label(w["label"]) for a in accounts for w in live(a)]
    labels += [name for st in advisors for _, name, _ in _moments(st)]
    label_w = max([2, *map(text_width, labels)])
    meter_w = max(4, boxes.inner - label_w - 1 - PERCENT_W - TAIL_W)

    out: list[Line] = []
    for a in accounts:
        stale = bool(a.get("stale"))
        rows: list[Line] = []
        for w in live(a):
            color = MUTED if stale else LEVEL[w["level"]]
            cells = [Span(c.text, c.color or color) for c in meter(w.get("left_percent"), even_pace_left(w, now), meter_w)]
            reset = until(w["resets_at"] - now) if w.get("resets_at") else "—"
            rows.append([
                Span(pad_text(short_label(w["label"]), label_w + 1), MUTED if stale else None),
                *cells,
                Span(pad_text(percent(w), PERCENT_W, True), color if w.get("left_percent") is not None else MUTED,
                     not stale),
                Span(pad_text(MARK[w["level"]], 2), color, True),
                Span(pad_text(reset, TAIL_W - 2, True), MUTED),
            ])
        if not rows:
            rows = [[Span(a.get("note") or "No quota data", MUTED)]]
        plan = " · ".join(p for p in (a.get("plan") and plan_text(a["plan"]), a.get("workspace")) if p)
        out += boxes.box(a["agent"].capitalize(), BOX.get(a["agent"], OTHER_BOX), rows, plan,
                         "stale" if stale else "", WARN)
        # The advisors of this tab's Claude sessions, under Claude's quota.
        if a["agent"] == "claude":
            for st in advisors:
                total = sum(st["counts"].values())
                out += boxes.box("Advisor", BOX["advisor"], _advisor_rows(st, boxes.inner, label_w, meter_w, now),
                                 f"{st['main']} → {st['advisor']}", f"×{total}" if total else "")

    out += boxes.box("Cost", BOX["cost"], [cost_row], "", age, ERROR if error else TITLE)

    # Running tasks first, then the newest; the box takes every row left over.
    room = max(1, height - len(out) - 2)
    tasks = sorted(snap.get("tasks", []), key=lambda t: (t["state"] == "running", t.get("since") or 0), reverse=True)
    task_rows: list[Line] = []
    for t in tasks[:room]:
        idle = t["state"] != "running"
        since = ago(now - t["since"]) if t.get("since") else ""
        task_rows.append(_spread([Span("○ " if idle else "● ", MUTED if idle else LEVEL["ok"]),
                                  Span(t.get("title") or t["project"], MUTED if idle else None)],
                                 [Span(since, MUTED)], boxes.inner))
    if not task_rows:
        task_rows = [[Span("No agents running", MUTED)]]
    task_rows += [[] for _ in range(room - len(task_rows))]
    running = sum(t["state"] == "running" for t in tasks)
    out += boxes.box("Tasks", BOX["tasks"], task_rows, f"{running}/{len(tasks)}" if tasks else "")
    return out
