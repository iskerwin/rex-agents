from rex_agents.alerts import changes, notices, osc

NOW = 1_795_286_000


def snap(left, resets_at=NOW + 3600, **over):
    window = {"label": "5 小时", "left_percent": left, "resets_at": resets_at}
    return {"accounts": [{"agent": "claude", "name": "Claude", "stale": False, "windows": [window]}], **over}


def test_tells_once_when_low_and_once_when_a_spent_window_comes_back():
    seen = {}
    assert changes(seen, snap(40), NOW) == []  # the first sight is the baseline
    assert changes(seen, snap(9), NOW) == [("Claude 5h 剩 9%", "1h0m 后重置")]
    assert changes(seen, snap(5), NOW) == []
    assert changes(seen, snap(100, NOW + 5 * 3600), NOW) == [("Claude 5h 已重置", "剩 100%")]
    assert changes(seen, snap(90, NOW + 5 * 3600 + 30), NOW) == []  # drift is the same window


def test_restored_and_stale_data_tell_nothing():
    seen = {"claude/5 小时": {"left": 40, "resets_at": NOW + 3600}}
    assert changes(seen, snap(5, refreshing=True), NOW) == []
    stale = snap(5)
    stale["accounts"][0]["stale"] = True
    assert changes(seen, stale, NOW) == []


def test_panels_share_what_has_been_told(tmp_path):
    path = tmp_path / "alerts.json"
    assert notices(snap(40), NOW, path) == []
    assert len(notices(snap(9), NOW, path)) == 1
    assert notices(snap(9), NOW, path) == []  # another panel, the same news


def test_a_notice_is_osc_777_with_its_fields_kept_apart():
    assert osc("a;b", "c\x07d") == "\x1b]777;notify;a,b;cd\x07"
