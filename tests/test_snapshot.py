import json
import threading
import time

from rex_agents.snapshot import Snapshots, saved_snapshot_path


class CountingQuota:
    ttl = 60

    def __init__(self):
        self.calls = 0

    def get(self, force=False):
        self.calls += 1
        time.sleep(0.05)
        return {"accounts": [], "most_urgent": None, "unmeasured": []}


class NoLogs:
    def records(self):
        return []


def panel(quota):
    p = Snapshots.__new__(Snapshots)
    p.scanner, p.quota = NoLogs(), quota
    return p


def test_panels_share_one_quota_query(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr("rex_agents.snapshot.list_tasks", lambda scanner: [])
    quotas = [CountingQuota() for _ in range(4)]
    threads = [threading.Thread(target=panel(q)) for q in quotas]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(q.calls for q in quotas) == 1
    assert json.loads(saved_snapshot_path().read_text())["accounts"] == []


def test_an_old_shared_snapshot_is_queried_again(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr("rex_agents.snapshot.list_tasks", lambda scanner: [])
    first = CountingQuota()
    panel(first)()
    saved = json.loads(saved_snapshot_path().read_text())
    saved_snapshot_path().write_text(json.dumps({**saved, "generated_at": int(time.time()) - 61}))
    again = CountingQuota()
    panel(again)()
    assert again.calls == 1


def account(**over):
    return {"agent": "claude", "name": "Claude Code", "email": "a@example.test", "windows": [], **over}


def test_a_failed_query_keeps_the_last_saved_values_marked_old():
    from rex_agents.snapshot import kept
    window = {"label": "5 小时", "used_percent": 20, "resets_at": 4_000_000_000}
    saved = {"accounts": [account(windows=[window], captured_at=int(time.time()) - 900)]}
    failed = {"accounts": [account(note="用量接口返回 HTTP 429")]}
    [claude] = kept(failed, saved)["accounts"]
    assert claude["windows"][0]["used_percent"] == 20
    assert claude["stale"] and claude["note"] == "用量接口返回 HTTP 429"
    # A window that has reset since says nothing any more.
    passed = {"accounts": [account(windows=[{**window, "resets_at": 1000}], captured_at=0)]}
    assert kept(failed, passed)["accounts"][0]["windows"] == []
