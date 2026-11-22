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
    tab = AdvisorFeed()
    span = [SPANS[0]]

    def on_key(key: str) -> None:
        # 1, 2 and 3 pick the trend's span: 24h, 7d, 30d.
        if key in ("1", "2", "3"):
            span[0] = SPANS[int(key) - 1]

    def compose(snap: Optional[dict[str, Any]], error: Optional[str], w: int, h: int) -> list[Line]:
        advisors = tab()  # refreshes tab.agents too
        return build_pane(snap, error, w, h, int(time.time()), advisors, tab.agents, span[0])

    watch(Snapshots(claude_live=True), compose, wants_color(), REFRESH_SECONDS, on_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
