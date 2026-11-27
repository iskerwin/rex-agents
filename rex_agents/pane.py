"""The Agents panel, drawn as a TUI from a snapshot. Pure: no
I/O and no clock.

One box per agent with a meter per quota window, then the cost, then the
running tasks down to the pane's foot. Short of rows, one line per agent.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Collection, Optional, Sequence

from rex_agents.text import MUTED, Line, Span, fit, line_width, pad_text, plain, plan_text, text_width


# The terminal's own 16 colors, so the panel takes on its theme, whatever
# that is. TUI layout: each box an outline dimmed from its hue, titles
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

def short_label(window: dict[str, Any]) -> str:
    """A 5-hour window reads `5h`, a weekly one `7d`, Fable's weekly `Fable 7d`.
    Data saved before agents-meter 0.2 has no ``seconds`` and shows its label."""
    seconds = window.get("seconds")
    if not seconds:
        return window["label"]
    span = next(f"{seconds // n}{unit}" for n, unit in ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))
                if seconds % n == 0)
    return f"{window['model']} {span}" if window.get("model") else span


def even_pace_left(window: dict[str, Any], now: int) -> Optional[float]:
    """The percent left if use had been even since the window began."""
    span, resets_at = window.get("seconds"), window.get("resets_at")
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


def month_end(month: Optional[float], now: int) -> Optional[float]:
    """This month's cost if the rest of it goes at its pace so far; None in
    its first day, too early to say."""
    if not month:
        return None
    t = datetime.fromtimestamp(now)
    start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    elapsed = (t - start).total_seconds()
    return None if elapsed < 86400 else month * (end - start).total_seconds() / elapsed


def daily_costs(rows: list[dict[str, Any]], now: int, days: int = 30) -> list[float]:
    """The cost of each of the last `days` days, oldest first, 0 for a day
    with none."""
    today = datetime.fromtimestamp(now).date()
    bars = dict.fromkeys(((today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)), 0.0)
    for r in rows:
        if r["date"] in bars and r.get("cost"):
            bars[r["date"]] += r["cost"]
    return list(bars.values())


# The models list's spans: the hourly rows carry no model.
MODEL_SPANS = ("7d", "30d")
AGENT_ORDER = ("claude", "codex", "grok")


def model_usage(rows: list[dict[str, Any]], span: str, now: int) -> list[tuple[str, str, int, Optional[float]]]:
    """Each model's agent, name, tokens and cost over the last `span` days,
    by agent, then most tokens first. Unused models are left out; a cost is
    None when the model has no public price."""
    today = datetime.fromtimestamp(now).date()
    days = {(today - timedelta(days=i)).isoformat() for i in range(int(span[:-1]))}
    found: dict[tuple[str, str], list[Any]] = {}
    for r in rows:
        if r["date"] not in days:
            continue
        name = re.sub(r"^Claude ", "", r.get("model_name") or r["model"])
        entry = found.setdefault((r["agent"], name), [0, None])
        entry[0] += tokens(r)
        if r.get("cost") is not None:
            entry[1] = (entry[1] or 0) + r["cost"]
    order = {a: i for i, a in enumerate(AGENT_ORDER)}
    listed = [(a, n, t, c) for (a, n), (t, c) in found.items() if t]
    return sorted(listed, key=lambda m: (order.get(m[0], len(order)), m[0], -m[2]))


# The trend's spans: the hours of the last day, or the last 7 or 30 days.
SPANS = ("24h", "7d", "30d")
CHART_ROWS = 3
COST_ROWS = 2
COST_DAYS = 30
LINK = "36"
COST_BAR = "33"
BLOCKS = " ▁▂▃▄▅▆▇█"


def tokens(row: dict[str, Any]) -> int:
    """Tokens worked through. Cache reads are left out: every turn rereads
    the whole context, so they would swamp what the turns added."""
    return row["fresh"] + row["cache_write"] + row["output"]


def trend(usage: dict[str, Any], agent: str, span: str, now: int) -> tuple[list[int], int]:
    """One agent's tokens per bar, oldest first, and its distinct sessions."""
    if span == "24h":
        hour = int(datetime.fromtimestamp(now).replace(minute=0, second=0, microsecond=0).timestamp())
        keys, rows, field = [hour - 3600 * i for i in range(23, -1, -1)], usage.get("hours", []), "hour"
    else:
        today = datetime.fromtimestamp(now).date()
        days = int(span[:-1])
        keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
        rows, field = usage.get("rows", []), "date"
    bars = dict.fromkeys(keys, 0)
    sessions: set[int] = set()
    for row in rows:
        if row["agent"] == agent and row[field] in bars:
            bars[row[field]] += tokens(row)
            sessions.update(row.get("sessions", ()))
    return list(bars.values()), len(sessions)


def count(n: int) -> str:
    """`950`, `12.3K`, `3.89M`, `1.2B`: three significant figures."""
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.3g}{unit}"
    return str(n)


def chart(values: list[int], width: int, rows: int = CHART_ROWS) -> list[Line]:
    """Bars of `values` in `rows` lines of `width` cells, eighth blocks for
    the tops. A span with no use keeps its place as an unlit ▁ on the
    baseline, and one with any use rises at least a quarter cell, so the two
    differ by shape as well as color. Bars sit evenly, centered: a cell short
    of an equal slot when the slot has two cells or more, side by side a
    cell each when not, as uneven gaps would pair them up. The newest are
    kept when they outnumber the cells."""
    values = values[-width:]
    n = max(len(values), 1)
    slot = width // n
    bar_w = slot - 1 if slot >= 2 else 1
    pad = (width - (n * slot - (slot - bar_w))) // 2
    starts = [pad + i * slot for i in range(n)]
    top = max(values, default=0)
    levels = [0 if not v else max(2, round(v / top * rows * 8)) for v in values]
    lines: list[Line] = []
    for r in range(rows):
        floor = (rows - 1 - r) * 8
        cells = [Span(" ")] * width
        for at, h in zip(starts, levels):
            cell = Span(BLOCKS[1], UNLIT) if h == 0 and r == rows - 1 else Span(BLOCKS[min(max(h - floor, 0), 8)])
            cells[at:at + bar_w] = [cell] * bar_w
        lines.append(cells)
    return lines


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
        if out and out[-1][1:] == span[1:]:
            out[-1] = span._replace(text=out[-1].text + span.text)
        else:
            out.append(span)
    return out


class _Boxes:
    """TUI boxes: `╭─┐title┌───┐label┌─╮`, rows between `│`
    bars, an optional label set into the bottom edge."""

    def __init__(self, width: int) -> None:
        self.width = width
        self.inner = width - 4

    def edge(self, top: bool, color: str, title: str = "", label: str = "", label_color: str = TITLE,
             label_key: Optional[str] = None) -> Line:
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
            line += [Span(label, label_color, False, label_key), Span(closing + "─", color)]
        line.append(Span(right, color))
        return line

    def box(self, title: str, color: str, rows: list[Line], top: str = "", foot: str = "",
            foot_color: str = TITLE, top_key: Optional[str] = None) -> list[Line]:
        body = [[Span("│ ", color), *fit(row, self.inner), Span(" │", color)] for row in rows]
        return [self.edge(True, color, title, top, TITLE, top_key), *body, self.edge(False, color, "", foot, foot_color)]


def _spread(left: Line, right: Line, width: int) -> Line:
    """`left` and `right` at either end of `width` cells, the left cut first."""
    room = width - line_width(right) - 1
    if line_width(left) > room:
        left = fit(left, max(room, 0))
    return [*left, Span(" " * max(width - line_width(left) - line_width(right), 0)), *right]


def build_pane(snap: Optional[dict[str, Any]], error: Optional[str], width: int, height: int, now: int,
               advisors: Sequence[dict[str, Any]] = (), tab_agents: Sequence[str] = (),
               span: str = SPANS[0], linked: Collection[int] = (), models: Optional[str] = None) -> list[Line]:
    """Exactly `height` lines of `width` cells. `advisors` are the states
    advisor-live shares of the Claude Code sessions in this tab;
    `tab_agents` the agents running in it, whose boxes get a usage trend
    over `span` when there is room; `linked` the pids of tasks in a Rex
    pane, which a click on their row goes to; `models` the span of the
    per-model list the Cost box opens to, None while it is closed."""
    width, height = max(width, 1), max(height, 1)
    if snap is None:
        lines: list[Line] = [[Span(error, ERROR)] if error else [Span("Loading", MUTED)]]
    elif width < 24:
        lines = [[Span("Too narrow", MUTED)]]
    else:
        lines = _layout(snap, error, width, height, now, advisors, tab_agents, span, linked, models)
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
            advisors: Sequence[dict[str, Any]], tab_agents: Sequence[str], span: str,
            linked: Collection[int], models: Optional[str] = None) -> list[Line]:
    boxes = _Boxes(width)
    accounts = snap["accounts"]
    usage = snap.get("usage", {})

    def live(account: dict[str, Any]) -> list[dict[str, Any]]:
        return [w for w in account["windows"] if w.get("resets_at") is None or w["resets_at"] > now]

    def percent(window: dict[str, Any]) -> str:
        left = window.get("left_percent")
        return "—" if left is None else f"{round(left)}%"

    today, month = costs(usage.get("rows", []), now)
    month_tail: Line = [Span("Month ", MUTED), Span(money(month), None, True)]
    projected = month_end(month, now)
    cost_left: Line = [Span("Today ", MUTED), Span(money(today), None, True)]
    if projected is not None and line_width(cost_left) + 1 + line_width(month_tail) + 4 + len(money(projected)) <= boxes.inner:
        month_tail.append(Span(f" → ≈{money(projected)}", WARN))
    cost_row = _spread(cost_left, month_tail, boxes.inner)
    # A snapshot restored from disk is old until the first refresh lands.
    age = "failed" if error else "refreshing" if snap.get("refreshing") else ago(now - snap["generated_at"])

    account_rows = sum(max(1, len(live(a))) + 2 for a in accounts)
    account_rows += sum(len(_moments(st)) + 3 for st in advisors)
    # The models list, opened by a click, goes before anything else, cut to
    # fit; then trends while the boxes, cost and a task row still fit
    # beside them; the cost chart goes first, then the agents' trends.
    listed = model_usage(usage.get("rows", []), models, now) if models else []
    model_rows: list[Line] = []
    if models:
        tabs: Line = []
        for s in MODEL_SPANS:
            tabs += [Span(" " if tabs else ""), Span(s, TITLE if s == models else MUTED, s == models, f"models:{s}")]
        model_rows.append(_spread([Span("Models", None, True)], tabs, boxes.inner))
        for agent, name, n, cost in listed:
            model_rows.append(_spread([Span("● ", BOX.get(agent, OTHER_BOX).split(";")[-1]), Span(name)],
                                      [Span(pad_text(count(n), 7, True), None, True),
                                       Span(pad_text(money(cost), 8, True), MUTED)], boxes.inner))
        if not listed:
            model_rows.append([Span("No usage", MUTED)])
        model_rows = model_rows[:max(0, height - (account_rows + 3 + 3))]
    base = account_rows + 3 + 3 + len(model_rows)
    charted = [a["agent"] for a in accounts if a["agent"] in tab_agents]
    if height < base + len(charted) * (1 + CHART_ROWS):
        charted = []
    cost_chart = height >= base + len(charted) * (1 + CHART_ROWS) + COST_ROWS
    if height < base:
        compact: list[Line] = []
        for a in accounts:
            line: Line = [Span(pad_text(a["agent"].capitalize(), 7), MUTED if a.get("stale") else None, True)]
            for i, w in enumerate(live(a)):
                line += [Span(" · " if i else "", MUTED), Span(short_label(w) + " ", MUTED),
                         Span(percent(w), MUTED if a.get("stale") else LEVEL[w["level"]], True)]
            compact.append(line)
            if a["agent"] == "claude":
                for st in advisors:
                    calls = st.get("calls") or []
                    tail = [Span(" · ", MUTED), Span(calls[-1]["trigger"], MUTED)] if calls else []
                    compact.append([Span(pad_text("Advisor", 8), MUTED), Span(st["advisor"], VIOLET, True),
                                    Span(f" ×{sum(st['counts'].values())}"), *tail])
        return [*compact, _spread(cost_row, [Span(age, ERROR if error else MUTED)], width)]

    labels = [short_label(w) for a in accounts for w in live(a)]
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
                Span(pad_text(short_label(w), label_w + 1), MUTED if stale else None),
                *cells,
                Span(pad_text(percent(w), PERCENT_W, True), color if w.get("left_percent") is not None else MUTED,
                     not stale),
                Span(pad_text(MARK[w["level"]], 2), color, True),
                Span(pad_text(reset, TAIL_W - 2, True), MUTED),
            ])
        if not rows:
            rows = [[Span(a.get("note") or "No quota data", MUTED)]]
        if a["agent"] in charted:
            values, sessions = trend(usage, a["agent"], span, now)
            tabs: Line = []
            for i, s in enumerate(SPANS):
                tabs += [Span(" " if tabs else ""), Span(s, TITLE if s == span else MUTED, s == span, str(i + 1))]
            total: Line = [Span(f"{count(sum(values))} tokens", None, True),
                           Span(f" · {sessions} session{'s' * (sessions != 1)}", MUTED)]
            if line_width(total) + 1 + line_width(tabs) > boxes.inner:
                total[0] = Span(count(sum(values)), None, True)  # the word goes before the count is cut
            rows.append(_spread(total, tabs, boxes.inner))
            rows += chart(values, boxes.inner)
        plan = " · ".join(p for p in (a.get("plan") and plan_text(a["plan"]), a.get("workspace")) if p)
        stale_for = f"stale {ago(now - a['captured_at'])}" if stale and a.get("captured_at") else "stale"
        out += boxes.box(a["agent"].capitalize(), BOX.get(a["agent"], OTHER_BOX), rows, plan,
                         stale_for if stale else "", WARN)
        # The advisors of this tab's Claude sessions, under Claude's quota.
        if a["agent"] == "claude":
            for st in advisors:
                total = sum(st["counts"].values())
                out += boxes.box("Advisor", BOX["advisor"], _advisor_rows(st, boxes.inner, label_w, meter_w, now),
                                 f"{st['main']} → {st['advisor']}", f"×{total}" if total else "")

    cost_rows = [cost_row]
    if cost_chart:
        cost_rows += [[Span(c.text, c.color or COST_BAR) for c in row]
                      for row in chart(daily_costs(usage.get("rows", []), now, COST_DAYS), boxes.inner, COST_ROWS)]
    cost_rows += model_rows
    out += boxes.box("Cost", BOX["cost"], cost_rows, "▾ models" if models else "▸ models", age,
                     ERROR if error else MUTED if snap.get("refreshing") else TITLE, "models")

    # Running tasks first, then the newest; the box takes every row left over.
    room = max(1, height - len(out) - 2)
    tasks = sorted(snap.get("tasks", []), key=lambda t: (t["state"] == "running", t.get("since") or 0), reverse=True)
    task_rows: list[Line] = []
    for t in tasks[:room]:
        idle = t["state"] != "running"
        since = ago(now - t["since"]) if t.get("since") else ""
        tail = [Span(since, MUTED)]
        if t.get("pid") in linked:
            tail.append(Span(" ↗", LINK))
        row = _spread([Span("○ " if idle else "● ", MUTED if idle else LEVEL["ok"]),
                       Span(t.get("title") or t["project"], MUTED if idle else None)], tail, boxes.inner)
        # The whole row is the link, the gap in it too.
        task_rows.append([c._replace(key=f"task:{t['pid']}") for c in row] if t.get("pid") in linked else row)
    if not task_rows:
        # A restored snapshot keeps no tasks: their processes may be gone.
        task_rows = [[Span("Loading" if snap.get("refreshing") else "No agents running", MUTED)]]
    task_rows += [[] for _ in range(room - len(task_rows))]
    running = sum(t["state"] == "running" for t in tasks)
    out += boxes.box("Tasks", BOX["tasks"], task_rows, f"{running}/{len(tasks)}" if tasks else "")
    return out
