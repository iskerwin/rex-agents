"""rex-agents: the Agents panel, run in a Rex pane by rex/init.lua."""

from __future__ import annotations

import argparse
import os
import sys
import time

from agents_meter.snapshot import Snapshots

from rex_agents import __version__
from rex_agents.advisor import AdvisorFeed
from rex_agents.pane import build_pane
from rex_agents.watch import watch

# Claude's and Grok's quotas are asked at most once a minute between every
# process using agents-meter, so asking more often would only reread.
REFRESH_SECONDS = 60


def wants_color() -> bool:
    return not ("NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb")


def main() -> int:
    parser = argparse.ArgumentParser(prog="rex-agents", description="Rex 里的 Agents 面板：额度、advisor、费用和任务")
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args()
    if not sys.stdout.isatty():
        print("rex-agents 需要在终端里运行", file=sys.stderr)
        return 2
    sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]
    advisors = AdvisorFeed()
    watch(Snapshots(claude_live=True), lambda snap, error, w, h: build_pane(snap, error, w, h, int(time.time()), advisors()),
          wants_color(), REFRESH_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
