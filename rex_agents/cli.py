"""rex-agents: the Agents panel, run in a Rex pane by rex/init.lua."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

from rex_agents import __version__
from rex_agents.advisor import AdvisorFeed
from rex_agents.pane import build_pane
from rex_agents.watch import watch

# agents-usage shares one quota query a minute between every process that
# asks, Claude's live quota allowing no more; asking more often only rereads.
REFRESH_SECONDS = 60
# `--claude-live`: Claude's per-model weekly quotas, Fable's among them, come
# only from its usage endpoint.
ARGS = ["--json", "--claude-live"]


def agents_usage() -> str:
    """Where agents-usage is: on PATH, else where `uv tool install` puts it, as
    a pane Rex starts may not have the login shell's PATH."""
    found = shutil.which("agents-usage")
    if found:
        return found
    return os.path.expanduser("~/.local/bin/agents-usage")


def fetch(command: Optional[list[str]] = None) -> dict[str, Any]:
    """A snapshot from agents-usage, or an error that says what went wrong."""
    try:
        ran = subprocess.run(command or [agents_usage(), *ARGS], capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        raise RuntimeError("没有找到 agents-usage") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("agents-usage 超时") from None
    if ran.returncode != 0:
        raise RuntimeError(ran.stderr.strip().splitlines()[-1] if ran.stderr.strip() else f"agents-usage 退出码 {ran.returncode}")
    try:
        snapshot = json.loads(ran.stdout)
    except ValueError:
        snapshot = None
    if not isinstance(snapshot, dict) or "accounts" not in snapshot:
        raise RuntimeError("agents-usage 的输出看不懂")
    return snapshot


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
    watch(fetch, lambda snap, error, w, h: build_pane(snap, error, w, h, int(time.time()), advisors()),
          wants_color(), REFRESH_SECONDS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
