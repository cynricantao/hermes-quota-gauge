"""Quota Gauge backend, mounted at ``/api/plugins/quota-gauge/``.

GET /quota            subscription windows for Anthropic, Command Code and xAI (cached per provider)
GET /usage?tz=<IANA>  token history read from this profile's ``state.db`` into a monotonic local ledger
GET /health           version and which optional sources are present

Credentials never come from files here. Anthropic and xAI go through Hermes's own account-usage
route (``agent.account_usage.fetch_account_usage``); Command Code uses the ``COMMANDCODE_API_KEY``
secret through ``agent.secret_scope.get_secret``.
"""

from __future__ import annotations

import bisect
import contextvars
import logging
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter()

PLUGIN_ID = "quota-gauge"
VERSION = "0.1.0"
USER_AGENT = f"hermes-quota-gauge/{VERSION}"

KINDS = ("input", "output", "cache_read", "cache_write")
PROVIDERS = ("anthropic", "commandcode", "xai")
PROVIDER_NAMES = {"anthropic": "Anthropic", "commandcode": "Command Code", "xai": "xAI"}

BUCKET_S = 900                 # ledger resolution: 15 minutes (aligns with every UTC offset)
MAX_SPREAD_BUCKETS = 96 * 120  # a single delta is never spread over more than ~120 days
LONG_GAP_S = 1800              # growth across a longer gap is booked at the latest activity
SYNC_MIN_INTERVAL_S = 15       # /usage re-reads state.db at most this often
BACKGROUND_SYNC_S = 60         # while the backend runs, the ledger keeps up without the pane open
QUOTA_TTL_S = {"anthropic": 300, "commandcode": 120, "xai": 120}
FORCE_MIN_INTERVAL_S = 30      # a manual refresh can't hammer a provider
FETCH_DEADLINE_S = 20.0


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _secret(name: str) -> Optional[str]:
    try:
        from agent.secret_scope import get_secret

        value = get_secret(name)
    except Exception:  # an unscoped read under multiplexing raises; treat it as "not set"
        return None
    value = (value or "").strip()
    return value or None


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch(value: Any) -> Optional[float]:
    """Seconds since epoch from a datetime, ISO string, epoch seconds or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return v / 1000.0 if v > 1e11 else v
    if isinstance(value, str):
        text = value.strip()
        try:
            return _epoch(float(text))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return _epoch(dt)
    return None


def _window(wid: str, label: str, used_percent: Optional[float], resets_at: Any = None,
            detail: Optional[str] = None, scope: str = "account") -> Optional[dict]:
    if used_percent is None:
        return None
    used = max(0.0, min(100.0, float(used_percent)))
    return {
        "id": wid,
        "label": label,
        "used_percent": round(used, 2),
        "remaining_percent": round(100.0 - used, 2),
        "resets_at": _epoch(resets_at),
        "detail": detail,
        "scope": scope,
    }


def _result(pid: str, *, windows: list, plan: Optional[str] = None, source: str = "",
            details: Optional[list] = None, warning: Optional[dict] = None) -> dict:
    """``warning`` is an optional badge: {"label", "level": "warn"|"limit", "detail"}."""
    return {
        "id": pid,
        "name": PROVIDER_NAMES[pid],
        "status": "ok",
        "plan": plan,
        "source": source,
        "windows": [w for w in windows if w],
        "details": list(details or []),
        "warning": warning,
        "message": None,
    }


def _unavailable(pid: str, message: str, code: str = "unavailable") -> dict:
    return {
        "id": pid,
        "name": PROVIDER_NAMES[pid],
        "status": code,
        "plan": None,
        "source": "",
        "windows": [],
        "details": [],
        "warning": None,
        "message": message,
    }


class ProviderHTTPError(Exception):
    """An HTTP failure worth backing off on. Any exception with an int ``status`` attribute
    is treated the same way."""

    def __init__(self, status: int, retry_after: Optional[float] = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: Any) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds form only)."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds < 86400 else None


# ---------------------------------------------------------------------------------------------
# Provider fetchers. Each returns a normalized dict and may raise ProviderHTTPError.
# ---------------------------------------------------------------------------------------------

_ANTHROPIC_LABELS = {
    "current session": ("5h", "5h"),
    "current week": ("week", "Week"),
    "opus week": ("opus_week", "Opus wk"),
    "sonnet week": ("sonnet_week", "Sonnet wk"),
}


def _from_core_snapshot(pid: str, snapshot: Any, source: str = "hermes") -> Optional[dict]:
    """Normalize an ``agent.account_usage.AccountUsageSnapshot``; None when it carries no windows."""
    if snapshot is None or getattr(snapshot, "unavailable_reason", None):
        return None
    windows = []
    for w in getattr(snapshot, "windows", ()) or ():
        label = str(getattr(w, "label", "") or "")
        wid, short = _ANTHROPIC_LABELS.get(label.lower(), (label.lower().replace(" ", "_") or "window", label))
        windows.append(_window(wid, short, getattr(w, "used_percent", None), getattr(w, "reset_at", None),
                               getattr(w, "detail", None), getattr(w, "scope", "account") or "account"))
    windows = [w for w in windows if w]
    if not windows:
        return None
    return _result(pid, windows=windows, plan=getattr(snapshot, "plan", None), source=source,
                   details=list(getattr(snapshot, "details", ()) or ()))


_core_skip_until: dict[str, float] = {}


def _core_usage(provider_slug: str) -> Optional[dict]:
    """Hermes's own account-usage route. A miss is remembered for a while so a provider that
    can't answer (e.g. a token without the usage scope) isn't asked on every poll."""
    now = time.time()
    if _core_skip_until.get(provider_slug, 0) > now:
        return None
    try:
        from agent.account_usage import fetch_account_usage

        snapshot = fetch_account_usage(provider_slug)
    except Exception as exc:
        log.debug("quota-gauge: core usage for %s failed: %s", provider_slug, exc)
        snapshot = None
    pid = "xai" if provider_slug.startswith("xai") else provider_slug
    result = _from_core_snapshot(pid, snapshot)
    if result is None:
        _core_skip_until[provider_slug] = now + 1800
    return result


def fetch_anthropic() -> dict:
    result = _core_usage("anthropic")
    if result:
        return result
    return _unavailable(
        "anthropic",
        "Hermes couldn't read your Claude limits. The usage API needs a full Claude sign-in; "
        "tokens from `claude setup-token` only carry the inference scope.",
    )


# Command Code ---------------------------------------------------------------------------------

_CC_BASE = "https://api.commandcode.ai"
# planId -> (display name, monthly included credits in USD), as published for each plan.
_CC_PLANS = {
    "individual-go": ("Go", 10.0),
    "individual-pro": ("Pro", 30.0),
    "individual-pro-v1": ("Pro", 80.0),
    "individual-provider": ("Provider", 15.0),
    "individual-goat": ("GOAT", 70.0),
    "individual-max": ("Max", 150.0),
    "individual-ultra": ("Ultra", 300.0),
    "teams-pro": ("Teams Pro", 40.0),
}


def _cc_plan_name(plan_id: Optional[str]) -> Optional[str]:
    if not plan_id:
        return None
    known = _CC_PLANS.get(plan_id)
    if known:
        return known[0]
    return plan_id.replace("individual-", "").replace("-", " ").title() or None


def fetch_commandcode(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    key = _secret("COMMANDCODE_API_KEY")
    if not key:
        return _unavailable("commandcode", "Add your Command Code API key (COMMANDCODE_API_KEY) in "
                            "Settings → Plugins → Quota Gauge.", code="no_credentials")
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
    factory = client_factory or (lambda: httpx.Client(timeout=10.0, follow_redirects=False, headers=headers))

    def get(client, path: str, **params) -> Any:
        resp = client.get(_CC_BASE + path, params={k: v for k, v in params.items() if v})
        if resp.status_code >= 400:
            raise ProviderHTTPError(resp.status_code, _retry_after(resp.headers.get("retry-after")))
        return resp.json()

    with factory() as client:
        try:
            whoami = get(client, "/alpha/whoami", limits="1")
        except ProviderHTTPError as exc:
            if exc.status in (401, 403):
                raise
            whoami = None
        org = ((whoami or {}).get("org") or {}).get("id") if isinstance(whoami, dict) else None
        credits = get(client, "/alpha/billing/credits", orgId=org)
        try:
            subs = get(client, "/alpha/billing/subscriptions", orgId=org)
        except ProviderHTTPError:
            subs = None

    if not isinstance(credits, dict):
        raise ProviderHTTPError(502)
    raw_block = credits.get("credits")
    block: dict = raw_block if isinstance(raw_block, dict) else {}
    limits = {**(credits.get("windowLimits") or {}), **(block.get("windowLimits") or {})}
    sub = subs.get("data") if isinstance(subs, dict) and isinstance(subs.get("data"), dict) else None
    plan_id = (sub or {}).get("planId") or block.get("planId") or credits.get("planId")

    windows: list = []
    details: list[str] = []
    warning: Optional[dict] = None
    if limits.get("limited") is not False:
        for field, wid, label in (("fiveHour", "5h", "5h"), ("weekly", "week", "Week")):
            w = limits.get(field)
            if not isinstance(w, dict):
                continue
            if w.get("exceeded") is True and warning is None:
                warning = {"label": "Limit hit", "level": "limit",
                           "detail": f"{label} window used up; requests decline until it resets"}
            used, cap = _num(w.get("used")), _num(w.get("cap"))
            if used is None or not cap or cap <= 0:
                continue
            windows.append(_window(wid, label, used / cap * 100.0, w.get("resetAt"),
                                   f"${used:,.2f} of ${cap:,.2f} used"))
    else:
        details.append("Pay-as-you-go: no rolling windows on this plan")

    remaining = _num(block.get("monthlyCredits"))
    pool = (_CC_PLANS.get(plan_id or "") or (None, 0.0))[1]
    if remaining is not None and pool and 0 <= remaining <= pool:
        windows.append(_window("cycle", "Cycle", (pool - remaining) / pool * 100.0,
                               (sub or {}).get("currentPeriodEnd"),
                               f"${remaining:,.2f} of ${pool:,.2f} credits left"))
    elif remaining is not None:
        details.append(f"${remaining:,.2f} cycle credits left")
    purchased = _num(block.get("purchasedCredits"))
    if purchased:
        details.append(f"${purchased:,.2f} top-up credits")

    if not windows and not details:
        return _unavailable("commandcode", "Command Code returned no usage windows for this key.")
    return _result("commandcode", windows=windows, plan=_cc_plan_name(plan_id), source="api", details=details,
                   warning=warning)


# xAI ------------------------------------------------------------------------------------------

def fetch_xai() -> dict:
    result = _core_usage("xai-oauth")
    if result:
        return result
    return _unavailable(
        "xai",
        "Hermes doesn't expose your Grok subscription limits to plugins yet. Token totals below still track.",
    )


FETCHERS: dict[str, Callable[[], dict]] = {
    "anthropic": fetch_anthropic,
    "commandcode": fetch_commandcode,
    "xai": fetch_xai,
}


# ---------------------------------------------------------------------------------------------
# Quota cache
# ---------------------------------------------------------------------------------------------

_quota_cache: dict[tuple[str, str], dict] = {}
_quota_locks: dict[tuple[str, str], threading.Lock] = defaultdict(threading.Lock)
_quota_meta_lock = threading.Lock()


def _fetch_into_cache(home_key: str, pid: str) -> None:
    key = (home_key, pid)
    with _quota_meta_lock:
        lock = _quota_locks[key]
    if not lock.acquire(timeout=FETCH_DEADLINE_S):
        return
    try:
        prev = _quota_cache.get(key)
        now = time.time()
        try:
            data = FETCHERS[pid]()
            entry = {"data": data, "fetched_at": now, "error": None, "backoff_until": 0.0}
        except Exception as exc:
            status = getattr(exc, "status", None)
            if isinstance(status, int):
                wait_s = _retry_after(getattr(exc, "retry_after", None)) or 120.0
                if status == 429:
                    wait_s = max(wait_s, 600.0)
                message = ("The credential was rejected." if status in (401, 403)
                           else "Rate limited by the provider, backing off." if status == 429
                           else f"Provider error (HTTP {status}).")
            else:  # network errors, malformed JSON
                log.debug("quota-gauge: %s fetch failed: %s", pid, exc, exc_info=True)
                wait_s = 120.0
                message = f"Couldn't reach {PROVIDER_NAMES[pid]} ({type(exc).__name__})."
            entry = _error_entry(pid, prev, message, now, wait_s)
        _quota_cache[key] = entry
    finally:
        lock.release()


def _error_entry(pid: str, prev: Optional[dict], message: str, now: float, backoff_s: float) -> dict:
    """Keep the last good reading (marked stale) and remember the error."""
    if prev and prev.get("data") and prev["data"].get("status") == "ok":
        return {**prev, "error": message, "backoff_until": now + backoff_s}
    return {"data": _unavailable(pid, message, code="error"), "fetched_at": now, "error": message,
            "backoff_until": now + backoff_s}


def get_quota(force: bool = False) -> dict:
    home_key = str(_hermes_home())
    now = time.time()
    due = []
    for pid in PROVIDERS:
        entry = _quota_cache.get((home_key, pid))
        if entry is None:
            due.append(pid)
            continue
        age = now - entry["fetched_at"]
        if entry.get("backoff_until", 0) > now:
            continue
        if age >= QUOTA_TTL_S[pid] or (force and age >= FORCE_MIN_INTERVAL_S) or entry.get("error"):
            due.append(pid)
    if due:
        with ThreadPoolExecutor(max_workers=len(due), thread_name_prefix="quota-gauge") as pool:
            # One copied context per task: the route's profile/secret scope must reach each fetch,
            # and a single Context can't be entered by two threads at once.
            futures = [pool.submit(contextvars.copy_context().run, _fetch_into_cache, home_key, pid) for pid in due]
            wait(futures, timeout=FETCH_DEADLINE_S)
    providers = {}
    for pid in PROVIDERS:
        entry = _quota_cache.get((home_key, pid))
        if entry is None:
            providers[pid] = _unavailable(pid, "Still loading…", code="loading")
            continue
        data = dict(entry["data"])
        data["fetched_at"] = entry["fetched_at"]
        data["stale"] = bool(entry.get("error")) and data.get("status") == "ok"
        data["error"] = entry.get("error")
        providers[pid] = data
    return {"generated_at": now, "providers": providers}


# ---------------------------------------------------------------------------------------------
# Token ledger: state.db (read-only) -> plugin-data/quota-gauge/ledger.db
# ---------------------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seen (
  key TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  input INTEGER NOT NULL, output INTEGER NOT NULL,
  cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS buckets (
  t INTEGER NOT NULL,
  provider TEXT NOT NULL,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (t, provider)
);
CREATE TABLE IF NOT EXISTS totals (
  provider TEXT PRIMARY KEY,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  first_at REAL
);
"""

_USAGE_SQL = """
SELECT u.session_id, u.model, u.billing_provider, u.billing_base_url, u.billing_mode, u.task,
       u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
       COALESCE(u.first_seen, s.started_at),
       COALESCE(u.last_seen, s.last_activity_at, s.ended_at, s.started_at)
FROM session_model_usage u LEFT JOIN sessions s ON s.id = u.session_id
WHERE COALESCE(u.last_seen, s.last_activity_at, s.ended_at, s.started_at, 0) >= ?
"""

_USAGE_SQL_FALLBACK = """
SELECT session_id, model, billing_provider, billing_base_url, billing_mode, task,
       input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, first_seen, last_seen
FROM session_model_usage WHERE COALESCE(last_seen, first_seen, 0) >= ?
"""


def normalize_provider(raw: Any) -> str:
    p = str(raw or "").strip().lower()
    if p in ("xai", "xai-oauth", "grok"):
        return "xai"
    if p in ("commandcode", "command-code", "command_code"):
        return "commandcode"
    return p or "unknown"


def spread(delta: dict, start: float, end: float) -> dict[int, dict]:
    """Split integer token counts over the 15-minute buckets that ``[start, end]`` touches,
    proportionally to overlap; the largest-remainder rule keeps every sum exact."""
    if end < start:
        start = end
    start = max(start, end - MAX_SPREAD_BUCKETS * BUCKET_S)
    first = int(start // BUCKET_S) * BUCKET_S
    last = int(end // BUCKET_S) * BUCKET_S
    out: dict[int, dict] = {}
    if first == last or end - start < 1:
        amounts = {k: int(delta.get(k, 0)) for k in KINDS}
        if any(amounts.values()):
            out[last] = amounts
        return out
    edges = []
    t = first
    while t <= last:
        edges.append((t, max(0.0, min(end, t + BUCKET_S) - max(start, t))))
        t += BUCKET_S
    total_w = sum(w for _, w in edges) or 1.0
    for kind in KINDS:
        amount = int(delta.get(kind, 0))
        if amount <= 0:
            continue
        shares = [amount * w / total_w for _, w in edges]
        alloc = [int(s) for s in shares]
        remainder = amount - sum(alloc)
        for i in sorted(range(len(shares)), key=lambda i: shares[i] - alloc[i], reverse=True)[:remainder]:
            alloc[i] += 1
        for (bt, _), a in zip(edges, alloc):
            if a:
                out.setdefault(bt, dict.fromkeys(KINDS, 0))[kind] += a
    return out


def _open_ledger() -> sqlite3.Connection:
    try:
        from plugins.plugin_storage import plugin_db

        conn = plugin_db(PLUGIN_ID, "ledger.db")
    except ImportError:  # older Hermes: same location, plain sqlite
        directory = _hermes_home() / "plugin-data" / PLUGIN_ID
        directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(directory / "ledger.db", check_same_thread=False, timeout=10)
    conn.isolation_level = None  # explicit transactions below
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(_SCHEMA)
    return conn


def _read_state_rows(state_path: Path, since: float) -> list:
    src = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True, timeout=5)
    try:
        src.execute("PRAGMA query_only = 1")
        try:
            return src.execute(_USAGE_SQL, (since,)).fetchall()
        except sqlite3.OperationalError:
            return src.execute(_USAGE_SQL_FALLBACK, (since,)).fetchall()
    finally:
        src.close()


def sync_ledger(conn: sqlite3.Connection, state_path: Path, now: Optional[float] = None) -> int:
    """Fold new token usage from state.db into the ledger. Returns the number of rows that changed.

    Each ``session_model_usage`` row is a running total. The ledger remembers the last value it
    saw per row and books only the growth, so deleting or pruning sessions never lowers a total.
    A brand-new row spreads over its own lifetime. Growth on a known row is spread between the
    previous reading and the row's newest ``last_seen`` when that gap is short (<= LONG_GAP_S),
    otherwise it is booked at ``last_seen``.
    """
    if not state_path.exists():
        return 0
    now = time.time() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
        hw = float(meta.get("hw", 0) or 0)
        last_sync = float(meta.get("last_sync", 0) or 0)
        rows = _read_state_rows(state_path, hw - 3600 if hw else 0)
        seen = {r[0]: r[1:] for r in conn.execute(
            "SELECT key, input, output, cache_read, cache_write, last_seen FROM seen").fetchall()}
        bucket_adds: dict[tuple[int, str], dict] = defaultdict(lambda: dict.fromkeys(KINDS, 0))
        total_adds: dict[str, dict] = defaultdict(lambda: dict.fromkeys(KINDS, 0))
        first_at: dict[str, float] = {}
        upserts = []
        new_hw = hw
        earliest = None
        changed = 0
        for row in rows:
            key = "\x1f".join(str(x or "") for x in row[:6])
            provider = normalize_provider(row[2])
            cur = {k: max(0, int(row[6 + i] or 0)) for i, k in enumerate(KINDS)}
            last_seen = float(row[11] or row[10] or now)
            first_seen = float(row[10] or last_seen)
            new_hw = max(new_hw, last_seen)
            earliest = first_seen if earliest is None else min(earliest, first_seen)
            prev = seen.get(key)
            if prev is None:
                delta, start = cur, first_seen
            else:
                prev_vals = dict(zip(KINDS, prev[:4]))
                if all(cur[k] == prev_vals[k] for k in KINDS):
                    continue
                delta = {k: cur[k] - prev_vals[k] if cur[k] >= prev_vals[k] else cur[k] for k in KINDS}
                start = max(float(prev[4] or 0), last_sync)
            upserts.append((key, provider, cur["input"], cur["output"], cur["cache_read"], cur["cache_write"],
                            last_seen))
            if not any(delta.values()):
                continue
            changed += 1
            # A short gap is spread evenly; across a long one (pane closed, backend down) the
            # session was mostly idle, so the growth goes where its latest activity was.
            lo = min(start, last_seen)
            if last_seen - lo > LONG_GAP_S:
                lo = last_seen
            for bt, amounts in spread(delta, lo, last_seen).items():
                slot = bucket_adds[(bt, provider)]
                for k in KINDS:
                    slot[k] += amounts[k]
            for k in KINDS:
                total_adds[provider][k] += delta[k]
            first_at[provider] = min(first_at.get(provider, first_seen), first_seen)

        conn.executemany(
            "INSERT INTO seen (key, provider, input, output, cache_read, cache_write, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET provider = excluded.provider, "
            "input = excluded.input, output = excluded.output, cache_read = excluded.cache_read, "
            "cache_write = excluded.cache_write, last_seen = excluded.last_seen", upserts)
        conn.executemany(
            "INSERT INTO buckets (t, provider, input, output, cache_read, cache_write) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(t, provider) DO UPDATE SET input = input + excluded.input, "
            "output = output + excluded.output, cache_read = cache_read + excluded.cache_read, "
            "cache_write = cache_write + excluded.cache_write",
            [(bt, p, *(a[k] for k in KINDS)) for (bt, p), a in bucket_adds.items()])
        conn.executemany(
            "INSERT INTO totals (provider, input, output, cache_read, cache_write, first_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET input = input + excluded.input, "
            "output = output + excluded.output, cache_read = cache_read + excluded.cache_read, "
            "cache_write = cache_write + excluded.cache_write, "
            "first_at = MIN(COALESCE(first_at, excluded.first_at), excluded.first_at)",
            [(p, *(a[k] for k in KINDS), first_at.get(p)) for p, a in total_adds.items()])
        updates = {"hw": repr(new_hw), "last_sync": repr(now)}
        if "since" not in meta:
            updates["since"] = repr(min(earliest, now) if earliest is not None else now)
        conn.executemany("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                         list(updates.items()))
        conn.execute("COMMIT")
        return changed
    except BaseException:
        conn.execute("ROLLBACK")
        raise


_last_sync: dict[str, float] = {}
_sync_lock = threading.Lock()


def sync_now(force: bool = False) -> None:
    home = _hermes_home()
    key = str(home)
    if not force and time.monotonic() - _last_sync.get(key, -1e9) < SYNC_MIN_INTERVAL_S:
        return
    with _sync_lock:
        if not force and time.monotonic() - _last_sync.get(key, -1e9) < SYNC_MIN_INTERVAL_S:
            return
        conn = _open_ledger()
        try:
            sync_ledger(conn, home / "state.db")
        finally:
            conn.close()
        _last_sync[key] = time.monotonic()


_bg_started = threading.Event()


def _start_background_sync() -> None:
    """One daemon thread per backend process keeps the launch profile's ledger current while the
    backend runs, so usage lands in the right hour even when the pane is closed."""
    if _bg_started.is_set():
        return
    _bg_started.set()

    def loop() -> None:
        while True:
            time.sleep(BACKGROUND_SYNC_S)
            try:
                sync_now()
            except Exception as exc:
                log.debug("quota-gauge: background sync failed: %s", exc)

    threading.Thread(target=loop, name="quota-gauge-ledger", daemon=True).start()


# ---------------------------------------------------------------------------------------------
# Aggregation for the charts
# ---------------------------------------------------------------------------------------------

def _zone(name: Optional[str]):
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name) if name else timezone.utc
    except Exception:
        return timezone.utc


def _midnight(d: date, tz) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz)


def _edges_for(now: float, since: float, tz) -> dict[str, tuple[str, list[int]]]:
    today = datetime.fromtimestamp(now, tz).date()
    hours = [int((_midnight(today, tz) + timedelta(hours=h)).timestamp()) for h in range(25)]
    week = [int(_midnight(today - timedelta(days=6 - i), tz).timestamp()) for i in range(8)]
    start_day = min(datetime.fromtimestamp(since, tz).date(), today)
    span_days = (today - start_day).days + 1
    if span_days <= 62:
        unit = "day"
        days = [start_day + timedelta(days=i) for i in range(span_days + 1)]
    elif span_days <= 7 * 60:
        unit = "week"
        monday = start_day - timedelta(days=start_day.weekday())
        days = []
        d = monday
        while d <= today:
            days.append(d)
            d += timedelta(days=7)
        days.append(d)
    else:
        unit = "month"
        days = []
        y, m = start_day.year, start_day.month
        while date(y, m, 1) <= today:
            days.append(date(y, m, 1))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        days.append(date(y, m, 1))
    all_edges = [int(_midnight(d, tz).timestamp()) for d in days]
    return {"today": ("hour", hours), "7d": ("day", week), "all": (unit, all_edges)}


def _bin(rows: list, edges: list[int]) -> list[dict]:
    bars = [dict(start=edges[i], end=edges[i + 1], **dict.fromkeys(KINDS, 0)) for i in range(len(edges) - 1)]
    for t, *vals in rows:
        i = bisect.bisect_right(edges, t) - 1
        if 0 <= i < len(bars):
            for k, v in zip(KINDS, vals):
                bars[i][k] += int(v or 0)
    return bars


def build_usage(conn: sqlite3.Connection, tz_name: Optional[str], now: Optional[float] = None) -> dict:
    now = time.time() if now is None else now
    tz = _zone(tz_name)
    meta = dict(conn.execute("SELECT k, v FROM meta").fetchall())
    since = float(meta.get("since", now) or now)
    edges = _edges_for(now, since, tz)
    lo = min(e[0] for _, e in edges.values())
    hi = max(e[-1] for _, e in edges.values())
    rows = conn.execute(
        "SELECT t, SUM(input), SUM(output), SUM(cache_read), SUM(cache_write) FROM buckets "
        "WHERE t >= ? AND t < ? GROUP BY t ORDER BY t", (lo, hi)).fetchall()
    series = {name: {"unit": unit, "bars": _bin(rows, e)} for name, (unit, e) in edges.items()}
    providers = {}
    for p, *vals in conn.execute(
            "SELECT provider, input, output, cache_read, cache_write, first_at FROM totals").fetchall():
        counts = dict(zip(KINDS, (int(v or 0) for v in vals[:4])))
        providers[p] = {**counts, "total": sum(counts.values()), "first_at": vals[4]}
    return {
        "generated_at": now,
        "tz": getattr(tz, "key", "UTC"),
        "tracking_since": since,
        "series": series,
        "providers": providers,
    }


# ---------------------------------------------------------------------------------------------
# Routes (plain ``def``: FastAPI runs them in a worker thread, off the gateway's event loop)
# ---------------------------------------------------------------------------------------------

@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": VERSION}


@router.get("/quota")
def quota(force: int = 0) -> dict:
    _start_background_sync()
    return get_quota(force=bool(force))


@router.get("/usage")
def usage(tz: str = "UTC") -> dict:
    _start_background_sync()
    try:
        sync_now()
    except Exception as exc:
        log.warning("quota-gauge: ledger sync failed: %s", exc)
    conn = _open_ledger()
    try:
        return build_usage(conn, tz)
    finally:
        conn.close()
