"""Unit tests for the Quota Gauge backend (no network, no real Hermes home).

Run inside Hermes's runtime so fastapi/httpx resolve:
    hermes --run-module pytest tests -q
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def api():
    spec = importlib.util.spec_from_file_location("qg_plugin_api_under_test", ROOT / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def ledger(api, tmp_path):
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.isolation_level = None
    conn.executescript(api._SCHEMA)
    yield conn
    conn.close()


def make_state(path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL, last_activity_at REAL);
        CREATE TABLE session_model_usage (
          session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT DEFAULT '',
          billing_mode TEXT DEFAULT '', task TEXT DEFAULT '',
          input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
          first_seen REAL, last_seen REAL,
          PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task));
    """)
    set_rows(conn, rows)
    conn.close()


def set_rows(conn_or_path, rows):
    conn = sqlite3.connect(conn_or_path) if isinstance(conn_or_path, Path) else conn_or_path
    conn.executemany(
        "INSERT INTO session_model_usage (session_id, model, billing_provider, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT DO UPDATE SET input_tokens = excluded.input_tokens, output_tokens = excluded.output_tokens, "
        "cache_read_tokens = excluded.cache_read_tokens, cache_write_tokens = excluded.cache_write_tokens, "
        "last_seen = excluded.last_seen", rows)
    conn.commit()
    if isinstance(conn_or_path, Path):
        conn.close()


def bucket_sum(conn, provider=None):
    q = "SELECT SUM(input), SUM(output), SUM(cache_read), SUM(cache_write) FROM buckets"
    args = ()
    if provider:
        q += " WHERE provider = ?"
        args = (provider,)
    return tuple(int(v or 0) for v in conn.execute(q, args).fetchone())


T0 = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc).timestamp()


def test_spread_is_exact_and_proportional(api):
    out = api.spread({"input": 1001, "output": 7, "cache_read": 0, "cache_write": 3}, T0, T0 + 3 * 900)
    assert sum(b["input"] for b in out.values()) == 1001
    assert sum(b["output"] for b in out.values()) == 7
    assert sum(b["cache_write"] for b in out.values()) == 3
    assert all(t % 900 == 0 for t in out)
    # three equal quarter-hours (+ the zero-width bucket at the end) share the input evenly
    assert sorted(b["input"] for b in out.values() if b["input"])[-1] - sorted(
        b["input"] for b in out.values() if b["input"])[0] <= 1


def test_spread_single_point_lands_in_one_bucket(api):
    out = api.spread({"input": 5, "output": 0, "cache_read": 0, "cache_write": 0}, T0 + 10, T0 + 10)
    assert list(out) == [int(T0 // 900) * 900]


def test_sync_backfill_then_deltas_are_monotonic(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    make_state(state, [
        ("s1", "claude", "anthropic", 100, 50, 1000, 10, T0, T0 + 600),
        ("s2", "grok", "xai-oauth", 20, 5, 0, 0, T0 + 100, T0 + 200),
    ])
    api.sync_ledger(ledger, state, now=T0 + 700)
    assert bucket_sum(ledger, "anthropic") == (100, 50, 1000, 10)
    assert bucket_sum(ledger, "xai") == (20, 5, 0, 0)  # xai-oauth folds into xai

    # growth on s1 is booked once, at the new activity time
    set_rows(state, [("s1", "claude", "anthropic", 130, 60, 1500, 10, T0, T0 + 5000)])
    assert api.sync_ledger(ledger, state, now=T0 + 5100) == 1
    assert bucket_sum(ledger, "anthropic") == (130, 60, 1500, 10)
    late = ledger.execute("SELECT SUM(input) FROM buckets WHERE t >= ?", (int(T0 + 4500),)).fetchone()[0]
    assert late == 30

    # a re-sync with no change books nothing
    assert api.sync_ledger(ledger, state, now=T0 + 5200) == 0

    # growth across a short gap (<= 30 min) is spread over that gap
    set_rows(state, [("s1", "claude", "anthropic", 130, 60, 1500 + 1200, 10, T0, T0 + 5200 + 1200)])
    api.sync_ledger(ledger, state, now=T0 + 5200 + 1300)
    spread_rows = ledger.execute("SELECT COUNT(*) FROM buckets WHERE cache_read > 0 AND t >= ?",
                                 (int(T0 + 5100) // 900 * 900,)).fetchone()[0]
    assert spread_rows >= 2
    assert bucket_sum(ledger, "anthropic") == (130, 60, 2700, 10)

    # deleting a session never lowers the totals
    conn = sqlite3.connect(state)
    conn.execute("DELETE FROM session_model_usage WHERE session_id = 's1'")
    conn.commit()
    conn.close()
    api.sync_ledger(ledger, state, now=T0 + 6000)
    totals = dict((r[0], r[1:]) for r in ledger.execute(
        "SELECT provider, input, output, cache_read, cache_write FROM totals"))
    assert totals["anthropic"] == (130, 60, 2700, 10)


def test_row_reset_counts_new_value_as_growth(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    make_state(state, [("s1", "m", "commandcode", 500, 0, 0, 0, T0, T0)])
    api.sync_ledger(ledger, state, now=T0 + 1)
    set_rows(state, [("s1", "m", "commandcode", 40, 0, 0, 0, T0, T0 + 60)])
    api.sync_ledger(ledger, state, now=T0 + 61)
    assert bucket_sum(ledger, "commandcode")[0] == 540


def test_build_usage_bins_by_local_day(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    # 21:30 UTC on Oct 20 is 01:30 on Oct 21 in Dubai (+04)
    late = datetime(2026, 10, 20, 21, 30, tzinfo=timezone.utc).timestamp()
    make_state(state, [("s1", "m", "anthropic", 10, 0, 0, 0, late, late)])
    api.sync_ledger(ledger, state, now=late + 1)
    now = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc).timestamp()
    dubai = api.build_usage(ledger, "Asia/Dubai", now=now)
    utc = api.build_usage(ledger, "UTC", now=now)
    assert dubai["series"]["today"]["bars"][1]["input"] == 10   # 01:00-02:00 local, today
    assert sum(b["input"] for b in utc["series"]["today"]["bars"]) == 0  # yesterday in UTC
    assert utc["series"]["7d"]["bars"][-2]["input"] == 10
    assert len(dubai["series"]["7d"]["bars"]) == 7
    assert dubai["providers"]["anthropic"]["total"] == 10


def test_all_time_switches_to_weeks_for_long_history(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    start = datetime(2026, 1, 5, 12, tzinfo=timezone.utc).timestamp()
    make_state(state, [("old", "m", "anthropic", 1, 0, 0, 0, start, start)])
    api.sync_ledger(ledger, state, now=start + 1)
    now = datetime(2026, 10, 21, 12, tzinfo=timezone.utc).timestamp()
    usage = api.build_usage(ledger, "UTC", now=now)
    assert usage["series"]["all"]["unit"] == "week"
    assert sum(b["input"] for b in usage["series"]["all"]["bars"]) == 1


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, routes):
        self.routes = routes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        for suffix, (status, payload) in self.routes.items():
            if url.endswith(suffix):
                return FakeResponse(status, payload)
        return FakeResponse(404, {})


def test_commandcode_windows(api, monkeypatch):
    monkeypatch.setattr(api, "_secret", lambda name: "cc-test-key")
    routes = {
        "/alpha/whoami": (200, {"org": {"id": "org_test"}}),
        "/alpha/billing/credits": (200, {
            "credits": {"monthlyCredits": 40.0, "purchasedCredits": 0},
            "windowLimits": {"limited": True,
                             "fiveHour": {"used": 7.5, "cap": 30, "resetAt": 1800000000000},
                             "weekly": {"used": 67.5, "cap": 75, "resetAt": 1800300000000}}}),
        "/alpha/billing/subscriptions": (200, {"data": {"planId": "individual-pro-v1",
                                                        "currentPeriodEnd": "2027-01-15T00:00:00Z"}}),
    }
    result = api.fetch_commandcode(client_factory=lambda: FakeClient(routes))
    assert result["status"] == "ok" and result["plan"] == "Pro"
    by_id = {w["id"]: w for w in result["windows"]}
    assert by_id["5h"]["remaining_percent"] == 75.0
    assert by_id["week"]["remaining_percent"] == 10.0
    assert by_id["cycle"]["remaining_percent"] == 50.0
    assert by_id["5h"]["resets_at"] == pytest.approx(1800000000.0)


def test_commandcode_auth_failure_raises_status(api, monkeypatch):
    monkeypatch.setattr(api, "_secret", lambda name: "bad")
    routes = {"/alpha/whoami": (401, {})}
    with pytest.raises(api.ProviderHTTPError) as err:
        api.fetch_commandcode(client_factory=lambda: FakeClient(routes))
    assert err.value.status == 401


def test_commandcode_without_key_is_unavailable(api, monkeypatch):
    monkeypatch.setattr(api, "_secret", lambda name: None)
    assert api.fetch_commandcode()["status"] == "no_credentials"


def test_quota_cache_keeps_last_good_reading_on_error(api, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_hermes_home", lambda: tmp_path)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return api._result("xai", windows=[api._window("week", "Week", 40.0)], source="test")
        raise api.ProviderHTTPError(429)

    monkeypatch.setitem(api.FETCHERS, "xai", flaky)
    monkeypatch.setitem(api.FETCHERS, "anthropic", lambda: api._unavailable("anthropic", "n/a"))
    monkeypatch.setitem(api.FETCHERS, "commandcode", lambda: api._unavailable("commandcode", "n/a"))
    first = api.get_quota()["providers"]["xai"]
    assert first["windows"][0]["remaining_percent"] == 60.0
    api._quota_cache[(str(tmp_path), "xai")]["fetched_at"] -= 10_000  # expire it
    second = api.get_quota()["providers"]["xai"]
    assert second["status"] == "ok" and second["stale"] is True
    assert "Rate limited" in second["error"]
