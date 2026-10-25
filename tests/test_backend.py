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


def test_models_are_tracked_per_range(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    make_state(state, [
        ("s1", "claude-opus", "anthropic", 100, 50, 1000, 10, T0, T0 + 600),
        ("s2", "deepseek/v4", "commandcode", 20, 5, 0, 0, T0 + 100, T0 + 200),
        ("s3", "claude-opus", "anthropic", 1, 1, 0, 0, T0 + 100, T0 + 200),
    ])
    api.sync_ledger(ledger, state, now=T0 + 700)
    set_rows(state, [("s2", "deepseek/v4", "commandcode", 220, 5, 0, 0, T0 + 100, T0 + 900)])
    api.sync_ledger(ledger, state, now=T0 + 950)
    usage = api.build_usage(ledger, "UTC", now=T0 + 1000)
    models = usage["series"]["today"]["models"]
    assert [(m["provider"], m["model"]) for m in models] == [("anthropic", "claude-opus"), ("commandcode", "deepseek/v4")]
    assert (models[0]["input"], models[0]["output"], models[0]["cache_read"], models[0]["total"]) == (101, 51, 1000, 1162)
    assert models[1]["total"] == 225
    # per-model totals add up to the chart
    bars = usage["series"]["today"]["bars"]
    assert sum(m["total"] for m in models) == sum(sum(b[k] for k in api.KINDS) for b in bars)
    assert usage["series"]["all"]["models"] == models


def test_models_backfill_from_an_older_ledger(api, ledger, tmp_path):
    state = tmp_path / "state.db"
    make_state(state, [("s1", "grok-4", "xai-oauth", 40, 2, 0, 0, T0, T0 + 60)])
    api.sync_ledger(ledger, state, now=T0 + 100)
    # simulate a ledger written before per-model tracking existed
    ledger.execute("DELETE FROM model_buckets")
    ledger.execute("DELETE FROM meta WHERE k = 'models_v'")
    set_rows(state, [("s1", "grok-4", "xai-oauth", 50, 2, 0, 0, T0, T0 + 120)])
    api.sync_ledger(ledger, state, now=T0 + 200)
    models = api.build_usage(ledger, "UTC", now=T0 + 300)["series"]["today"]["models"]
    assert [(m["provider"], m["model"], m["input"], m["output"]) for m in models] == [("xai", "grok-4", 50, 2)]
    # backfill runs once
    api.sync_ledger(ledger, state, now=T0 + 400)
    assert ledger.execute("SELECT SUM(input) FROM model_buckets").fetchone()[0] == 50


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
    result = api.fetch_commandcode()
    assert result["status"] == "unavailable" and "COMMANDCODE_API_KEY" in result["message"]


def test_commandcode_pay_as_you_go_is_no_subscription(api, monkeypatch):
    monkeypatch.setattr(api, "_secret", lambda name: "cc-test-key")
    routes = {
        "/alpha/whoami": (200, {}),
        "/alpha/billing/credits": (200, {"credits": {"purchasedCredits": 5}, "windowLimits": {"limited": False}}),
        "/alpha/billing/subscriptions": (200, {"data": None}),
    }
    assert api.fetch_commandcode(client_factory=lambda: FakeClient(routes))["status"] == "no_subscription"


def only_signed_in(api, monkeypatch, *pids):
    api._detect_cache.clear()
    api._quota_cache.clear()
    monkeypatch.setattr(api, "detect", lambda pid: "hermes:test" if pid in pids else None)


def test_quota_cache_keeps_last_good_reading_on_error(api, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_hermes_home", lambda: tmp_path)
    only_signed_in(api, monkeypatch, "xai")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return api._result("xai", windows=[api._window("week", "Week", 40.0)], source="test")
        raise api.ProviderHTTPError(429)

    monkeypatch.setitem(api.FETCHERS, "xai", flaky)
    first = api.get_quota()["providers"]["xai"]
    assert first["windows"][0]["remaining_percent"] == 60.0 and first["hidden"] is False
    api._quota_cache[(str(tmp_path), "xai")]["fetched_at"] -= 10_000  # expire it
    second = api.get_quota()["providers"]["xai"]
    assert second["status"] == "ok" and second["stale"] is True
    assert "Rate limited" in second["error"]


def test_providers_without_credentials_are_hidden_and_never_fetched(api, monkeypatch, tmp_path):
    monkeypatch.setattr(api, "_hermes_home", lambda: tmp_path)
    only_signed_in(api, monkeypatch, "codex")
    called = []
    for pid in api.PROVIDERS:
        monkeypatch.setitem(api.FETCHERS, pid, lambda pid=pid: called.append(pid) or api._result(
            pid, windows=[api._window("5h", "5h", 10.0)], source="test"))
    out = api.get_quota()
    assert called == ["codex"]
    assert out["order"] == list(api.PROVIDERS)
    assert out["providers"]["codex"]["hidden"] is False
    for pid in set(api.PROVIDERS) - {"codex"}:
        p = out["providers"][pid]
        assert p["status"] == "inactive" and p["hidden"] is True and p["signed_in"] is None
        assert p["message"] == api.SPECS[pid].setup


def test_detect_prefers_declared_secret_then_hermes_pool(api, monkeypatch):
    monkeypatch.setattr(api, "_secret", lambda name: "k" if name == "GLM_API_KEY" else None)
    monkeypatch.setattr(api, "_pool_has", lambda slug: slug == "openai-codex")
    assert api.detect("zai") == "secret:GLM_API_KEY"
    assert api.detect("codex") == "hermes:openai-codex"
    assert api.detect("kimi") is None
    assert api.detect("antigravity") is None


def test_every_card_has_a_fetcher_and_setup_hint(api):
    assert set(api.FETCHERS) == set(api.PROVIDERS) == set(api.SPECS)
    assert all(spec.setup for spec in api.SPECS.values())


class Snap:
    def __init__(self, plan=None, windows=(), reason=None, details=()):
        self.plan, self.windows, self.unavailable_reason, self.details = plan, windows, reason, details


class Win:
    def __init__(self, label, used, reset=None):
        self.label, self.used_percent, self.reset_at, self.detail, self.scope = label, used, reset, None, "account"


def test_codex_maps_core_windows_and_hides_free_plans(api, monkeypatch):
    monkeypatch.setattr(api, "_core_snapshot", lambda slug: Snap("Plus", (Win("Session", 30.0), Win("Weekly", 55.0))))
    result = api.fetch_codex()
    assert result["plan"] == "Plus" and [w["id"] for w in result["windows"]] == ["5h", "week"]
    assert result["windows"][1]["remaining_percent"] == 45.0
    monkeypatch.setattr(api, "_core_snapshot", lambda slug: Snap("Free", (Win("Weekly", 5.0),)))
    assert api.fetch_codex()["status"] == "no_subscription"


def test_anthropic_api_key_only_is_no_subscription(api, monkeypatch):
    monkeypatch.setattr(api, "_core_snapshot", lambda slug: Snap(
        reason="Anthropic account limits are only available for OAuth-backed Claude accounts."))
    assert api.fetch_anthropic()["status"] == "no_subscription"


def test_opencode_go_windows_through_core(api, monkeypatch):
    monkeypatch.setattr(api, "_core_snapshot", lambda slug: Snap(None, (
        Win("Rolling window", 12.0), Win("Weekly", 40.0), Win("Monthly", 70.0))))
    result = api.fetch_opencode()
    assert result["plan"] == "Go" and [w["label"] for w in result["windows"]] == ["5h", "Week", "Month"]


def test_parse_zai_coding_plan(api):
    payload = {"code": 200, "success": True, "data": {"planName": "Pro", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 12, "nextResetTime": 1800000000000},
        {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 40, "nextResetTime": 1800300000000},
        {"type": "TIME_LIMIT", "unit": 5, "number": 1, "usage": 1000, "currentValue": 250, "remaining": 750,
         "percentage": 25, "nextResetTime": 1801000000000},
    ]}}
    result = api.parse_zai(payload)
    assert result["status"] == "ok" and result["plan"] == "Pro"
    by_id = {w["id"]: w for w in result["windows"]}
    assert by_id["5h"]["used_percent"] == 12 and by_id["week"]["used_percent"] == 40
    assert by_id["mcp"]["used_percent"] == 25.0 and by_id["5h"]["resets_at"] == pytest.approx(1800000000.0)
    assert api.parse_zai({"code": 200, "data": {"limits": []}})["status"] == "no_subscription"


def test_parse_kimi_counts(api):
    payload = {
        "usage": {"limit": "2048", "used": "214", "remaining": "1834", "resetTime": "2026-11-09T15:23:13Z"},
        "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "200", "used": "139", "remaining": "61", "resetTime": "2026-11-06T13:33:02Z"}}],
    }
    result = api.parse_kimi(payload)
    by_id = {w["id"]: w for w in result["windows"]}
    assert by_id["5h"]["used_percent"] == pytest.approx(69.5)
    assert by_id["week"]["used_percent"] == pytest.approx(10.45, abs=0.01)
    assert api.parse_kimi({})["status"] == "no_subscription"


def test_parse_minimax_counts_are_remaining(api):
    start, end = 1800000000000, 1800000000000 + 5 * 3600 * 1000
    payload = {"base_resp": {"status_code": 0}, "current_subscribe_title": "Plus", "model_remains": [{
        "model_name": "MiniMax-M2", "start_time": start, "end_time": end,
        "current_interval_total_count": 1500, "current_interval_usage_count": 1200,
        "current_weekly_total_count": 0}]}
    result = api.parse_minimax(payload)
    assert result["plan"] == "Plus" and len(result["windows"]) == 1
    w = result["windows"][0]
    assert w["id"] == "5h" and w["used_percent"] == 20.0 and w["resets_at"] == pytest.approx(end / 1000)
    assert api.parse_minimax({"base_resp": {"status_code": 0}, "model_remains": []})["status"] == "no_subscription"


def test_parse_copilot(api):
    payload = {"copilot_plan": "individual", "quota_reset_date": "2026-11-01", "quota_snapshots": {
        "premium_interactions": {"entitlement": 300, "remaining": 225, "percent_remaining": 75.0, "unlimited": False},
        "chat": {"unlimited": True}, "completions": {"unlimited": True}}}
    result = api.parse_copilot(payload)
    assert result["plan"] == "Pro" and result["windows"][0]["remaining_percent"] == 75.0
    assert result["details"] == ["Unlimited chat and completions"]
    assert api.parse_copilot({"copilot_plan": "free"})["status"] == "no_subscription"


def test_new_provider_slugs_fold_into_cards(api, ledger, tmp_path):
    assert api.normalize_provider("openai-codex") == "codex"
    assert api.normalize_provider("opencode-go") == "opencode"
    assert api.normalize_provider("kimi-coding") == "kimi"
    assert api.normalize_provider("some-new-provider") == "some-new-provider"
    # totals written under an old raw slug are merged onto the card at read time
    ledger.execute("INSERT INTO totals (provider, input, output, cache_read, cache_write, first_at) "
                   "VALUES ('commandcode-anthropic', 5, 0, 0, 0, 100), ('commandcode', 7, 1, 0, 0, 50)")
    usage = api.build_usage(ledger, "UTC", now=T0)
    assert usage["providers"]["commandcode"]["total"] == 13
    assert usage["providers"]["commandcode"]["first_at"] == 50
