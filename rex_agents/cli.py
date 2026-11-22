"""rex-agents: the Agents panel, run in a Rex pane by rex/init.lua."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Optional

from agents_meter.snapshot import Snapshots

from rex_agents import __version__
from rex_agents.advisor import AdvisorFeed
from rex_agents.alerts import notices, osc
from rex_agents.links import Links
from rex_agents.pane import SPANS, build_pane
from rex_agents.text import Line
from rex_agents.watch import watch

# Claude's and Grok's quotas are asked at most once a minute between every
# process using agents-meter, so asking more often would only reread.
REFRESH_SECONDS = 60


def wants_color() -> bool:
    return not ("NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb")


def main() -> int:
    parser = argparse.ArgumentParser(prog="rex-agents", description="The Agents panel for Rex: quota, advisor, cost and tasks")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    if not sys.stdout.isatty():
        print("rex-agents needs to run in a terminal", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    snapshots = Snapshots(claude_live=True)
    tab = AdvisorFeed()
    links = Links()
    span = [SPANS[0]]
    told: list[str] = []

    def fetch() -> dict[str, Any]:
        # On the worker thread: everything here may wait on a process.
        snap = snapshots()
        try:
            snapshots.save(snap)
        except OSError:
            pass  # only the next start's first frame would miss it
        links.refresh(snap.get("tasks") or [])
        told.extend(osc(*n) for n in notices(snap, int(time.time())))
        return snap

    def on_key(key: str) -> None:
        # 1, 2 and 3 pick the trend's span: 24h, 7d, 30d; a task row goes to its pane.
        if key in ("1", "2", "3"):
            span[0] = SPANS[int(key) - 1]
        elif key.startswith("task:"):
            links.go(int(key[5:]))

    def compose(snap: Optional[dict[str, Any]], error: Optional[str], w: int, h: int) -> list[Line]:
        while told:
            sys.stdout.write(told.pop(0))
        advisors = tab()  # refreshes tab.agents too
        return build_pane(snap, error, w, h, int(time.time()), advisors, tab.agents, span[0], links.places)

    # The last snapshot shows at once, marked refreshing, until a live one lands.
    watch(fetch, compose, wants_color(), REFRESH_SECONDS, on_key, snapshots.restored())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
