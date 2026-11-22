import json
import sys

import pytest

from rex_agents.cli import fetch


def script(code):
    return [sys.executable, "-c", code]


def test_a_snapshot_comes_from_agents_usage_json():
    snap = {"accounts": [], "tasks": []}
    assert fetch(script(f"print({json.dumps(json.dumps(snap))})")) == snap


@pytest.mark.parametrize("command, message", [
    (["/nonexistent/agents-usage"], "没有找到 agents-usage"),
    (script("import sys; sys.stderr.write('boom\\n'); sys.exit(1)"), "boom"),
    (script("import sys; sys.exit(3)"), "退出码 3"),
    (script("print('not json')"), "看不懂"),
    (script("print('[1]')"), "看不懂"),
])
def test_a_failed_run_says_what_went_wrong(command, message):
    with pytest.raises(RuntimeError, match=message):
        fetch(command)
