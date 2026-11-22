"""One snapshot for the panel: quotas, usage and tasks. Every panel is a
process of its own, and Claude's live quota allows one call a minute, so the
quotas are shared through a saved snapshot: the first panel to find it older
than the TTL queries and saves, the others wait on the lock and read it.
Usage and tasks are each process's own and always current."""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator, Optional

from rex_agents import __version__
from rex_agents.logs import Scanner, rollup
from rex_agents.quota import QuotaService, account_from_dict, age_account, cache_dir, summarize, timezone_name, write_private
from rex_agents.tasks import list_tasks


def saved_snapshot_path() -> Path:
    return cache_dir() / "snapshot.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def kept(quota: dict[str, Any], saved: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The new quotas, but where a query failed (a 429, a timeout) the account
    last saved, aged under the freshness rules: old values marked as old say
    more than none, and they go once their windows reset."""
    if saved is None:
        return quota
    now = int(time.time())
    before = {(a.get("agent"), a.get("email")): a for a in saved.get("accounts", []) if a.get("windows")}
    accounts = []
    for data in quota["accounts"]:
        account = account_from_dict(data)
        old = before.get((account.agent, account.email))
        if not account.windows and old is not None:
            account = replace(age_account(account_from_dict(old), now), note=account.note)
        accounts.append(account)
    return summarize(accounts)


class Snapshots:
    def __init__(self) -> None:
        self.scanner = Scanner()
        self.quota = QuotaService(self.scanner)

    def _build(self, quota: dict[str, Any]) -> dict[str, Any]:
        return {
            "version": __version__,
            "generated_at": int(time.time()),
            "timezone": timezone_name(),
            **quota,
            "usage": rollup(self.scanner.records()),
            "tasks": list_tasks(self.scanner),
        }

    def _saved(self) -> Optional[dict[str, Any]]:
        """The saved snapshot, its quotas under the freshness rules again."""
        now = int(time.time())
        try:
            saved = json.loads(saved_snapshot_path().read_text())
            accounts = [age_account(account_from_dict(a), now) for a in saved["accounts"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return {**saved, **summarize(accounts), "generated_at": now, "captured_at": saved.get("generated_at", 0)}

    def __call__(self) -> dict[str, Any]:
        with _locked(cache_dir() / "snapshot.lock"):
            saved = self._saved()
            if saved is not None and saved["generated_at"] - saved["captured_at"] < self.quota.ttl:
                return self._build({k: saved[k] for k in ("accounts", "most_urgent", "unmeasured")})
            snapshot = self._build(kept(self.quota.get(), saved))
            write_private(saved_snapshot_path(), json.dumps(snapshot))
            return snapshot
