"""Quota Gauge backend, mounted at ``/api/plugins/quota-gauge/``.

GET /quota            subscription windows for every provider card (cached per provider)
GET /usage?tz=<IANA>  token history read from every profile's ``state.db`` into one monotonic
                      account-wide ledger
GET /health           version and which providers this profile holds a credential for

A card is *live* only when this profile holds a credential for it and the provider reports a paid
plan; every other card is ``inactive`` (no credential found) or ``no_subscription`` and the pane
files it under *Hidden/Inactive*. Detection never reads a file: it asks Hermes's credential pool
(``agent.credential_pool``) whether a provider has a sign-in or key, and checks the plugin's declared
secrets through ``agent.secret_scope.get_secret``. Inactive cards make no network request.

Usage numbers come from Hermes's own account-usage route (``agent.account_usage``) where it has one
(Anthropic, Codex, OpenCode Go, Nous Portal) and otherwise from the provider's usage endpoint with
the user's declared key (Command Code, Z.ai, Kimi Code, MiniMax, GitHub Copilot).
"""

from __future__ import annotations

import bisect
import contextvars
import importlib.util
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

from fastapi import APIRouter

log = logging.getLogger(__name__)
router = APIRouter()

PLUGIN_ID = "quota-gauge"
VERSION = "0.2.1"
USER_AGENT = f"hermes-quota-gauge/{VERSION}"

KINDS = ("input", "output", "cache_read", "cache_write")

BUCKET_S = 900                 # ledger resolution: 15 minutes (aligns with every UTC offset)
MAX_SPREAD_BUCKETS = 96 * 120  # a single delta is never spread over more than ~120 days
LONG_GAP_S = 1800              # growth across a longer gap is booked at the latest activity
SYNC_MIN_INTERVAL_S = 15       # /usage re-reads state.db at most this often
BACKGROUND_SYNC_S = 60         # while the backend runs, the ledger keeps up without the pane open
FORCE_MIN_INTERVAL_S = 30      # a manual refresh can't hammer a provider
FETCH_DEADLINE_S = 20.0
DETECT_TTL_S = 60              # credential detection is re-checked at most this often
HIDDEN_STATUSES = frozenset({"inactive", "no_subscription"})


class ProviderSpec(NamedTuple):
    """One card. ``hermes`` lists Hermes provider slugs whose credential means "signed in";
    ``secrets`` lists env secrets (declared in plugin.yaml) that count too, first match wins.
    A NamedTuple rather than a dataclass: Hermes may load this file without registering it in
    ``sys.modules``, and dataclasses need that."""

    id: str
    name: str
    hermes: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    ttl: int = 180
    setup: str = ""  # what turns the card on, shown in Hidden/Inactive


SPECS: dict[str, ProviderSpec] = {s.id: s for s in (
    ProviderSpec("anthropic", "Anthropic", hermes=("anthropic",), ttl=300,
                 setup="No Claude sign-in in Hermes. Run `hermes auth add anthropic`."),
    ProviderSpec("codex", "ChatGPT / Codex", hermes=("openai-codex",), ttl=180,
                 setup="No ChatGPT sign-in in Hermes. Run `hermes auth add openai-codex`."),
    ProviderSpec("commandcode", "Command Code", hermes=("commandcode",), secrets=("COMMANDCODE_API_KEY",), ttl=120,
                 setup="No Command Code key. Add COMMANDCODE_API_KEY in Settings → Plugins → Quota Gauge."),
    ProviderSpec("xai", "xAI", hermes=("xai-oauth",), ttl=120,
                 setup="No Grok sign-in in Hermes. Run `hermes auth add xai-oauth` (an xAI API key has no subscription)."),
    ProviderSpec("antigravity", "Antigravity", hermes=("antigravity-oauth", "antigravity"), ttl=300,
                 setup="No Antigravity session. Run `hermes auth add antigravity-oauth`."),
    ProviderSpec("opencode", "OpenCode Go", hermes=("opencode-go",), secrets=("OPENCODE_GO_API_KEY",), ttl=180,
                 setup="No OpenCode Go key. Add OPENCODE_GO_API_KEY (Zen keys are pay-as-you-go)."),
    ProviderSpec("devin", "Devin", secrets=("DEVIN_API_KEY",), ttl=600,
                 setup="No Devin key. Add DEVIN_API_KEY in Settings → Plugins → Quota Gauge."),
    ProviderSpec("copilot", "GitHub Copilot", hermes=("copilot",), secrets=("COPILOT_GITHUB_TOKEN",), ttl=300,
                 setup="No Copilot sign-in in Hermes. Run `hermes model` and pick GitHub Copilot."),
    ProviderSpec("nous", "Nous Portal", hermes=("nous",), ttl=300,
                 setup="No Nous Portal sign-in in Hermes. Run `hermes auth add nous`."),
    ProviderSpec("zai", "Z.ai GLM", hermes=("zai",), secrets=("ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"), ttl=180,
                 setup="No Z.ai key. Add ZAI_API_KEY (the GLM Coding Plan key)."),
    ProviderSpec("kimi", "Kimi Code", hermes=("kimi-coding",), secrets=("KIMI_API_KEY", "KIMI_CODING_API_KEY"), ttl=180,
                 setup="No Kimi Code key. Add KIMI_API_KEY from the Kimi Code console."),
    ProviderSpec("minimax", "MiniMax", hermes=("minimax", "minimax-cn", "minimax-oauth"),
                 secrets=("MINIMAX_API_KEY", "MINIMAX_CN_API_KEY"), ttl=180,
                 setup="No MiniMax key. Add MINIMAX_API_KEY (the Coding Plan key)."),
)}
PROVIDERS = tuple(SPECS)
PROVIDER_NAMES = {pid: spec.name for pid, spec in SPECS.items()}
QUOTA_TTL_S = {pid: spec.ttl for pid, spec in SPECS.items()}



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

    def __init__(self, status: int, retry_after: Optional[float] = None, detail: str = ""):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after
        self.detail = detail


_TOKENISH = re.compile(r"[A-Za-z0-9_\-]{20,}")


def _error_detail(resp: Any) -> str:
    """The provider's own explanation for a failed call, or "".

    Its message beats our guess ("the credential was rejected" hides a Cloudflare block or a plan
    that lapsed), but an error body is not a safe place to quote: providers routinely echo the key
    back. A snippet that contains a token-shaped run is dropped rather than shown."""
    try:
        body = resp.json()
    except Exception:
        body = None
    text = ""
    if isinstance(body, dict):
        for key in ("error", "message", "detail"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("detail") or value.get("type")
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
    elif body is not None:
        text = str(body).strip()
    if not text:
        raw = (getattr(resp, "text", "") or "").strip()
        text = "" if raw[:1] == "<" else raw  # an HTML block page says nothing useful
    text = " ".join(text.split())[:160]
    if not text or text[:1] == "<" or _TOKENISH.search(text):
        return ""
    return text


def _retry_after(value: Any) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds form only)."""
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if 0 < seconds < 86400 else None


# ---------------------------------------------------------------------------------------------
# Detection: does this profile hold a credential for a card? (no file reads, no network)
# ---------------------------------------------------------------------------------------------

def _pool_has(slug: str) -> bool:
    """Hermes's credential pool for ``slug`` holds a sign-in or key (env keys and OAuth logins both
    land there); falls back to external-process auth verification."""
    try:
        from agent.credential_pool import load_pool

        if load_pool(slug).has_credentials():
            return True
    except Exception as exc:
        log.debug("quota-gauge: credential pool for %s unreadable: %s", slug, exc)
    try:
        from hermes_cli import auth

        return bool(auth.get_auth_status(slug).get("logged_in"))
    except Exception as exc:
        log.debug("quota-gauge: auth status for %s unreadable: %s", slug, exc)
        return False


def _pool_key(slug: str) -> tuple[Optional[str], Optional[str]]:
    """(api key, label) of the credential Hermes itself uses for ``slug``, or (None, None).

    Read-only: ``peek`` returns the entry already in the pool and ``runtime_api_key`` is a stored
    value, so this never rotates or refreshes a credential on the provider's behalf."""
    try:
        from agent.credential_pool import load_pool

        entry = load_pool(slug).peek()
    except Exception as exc:
        log.debug("quota-gauge: credential pool for %s unreadable: %s", slug, exc)
        return None, None
    if entry is None:
        return None, None
    key = str(getattr(entry, "runtime_api_key", "") or "").strip()
    if not key:
        return None, None
    return key, str(getattr(entry, "label", "") or "").strip() or None


_FALLBACK_FILES = ("provider_fallbacks.py", "local_bridge.py")
_fallback_cache: dict[str, Any] = {"key": None, "modules": ()}
_fallback_lock = threading.Lock()


def _fallback_modules():
    """The provider modules beside this file, loaded on change.

    ``provider_fallbacks.py`` ships with the plugin: it fills the providers Hermes has no public
    plugin route for yet (xAI's weekly window, Antigravity's quota, the spend-token Anthropic
    fallback). ``local_bridge.py`` is a developer's own override, gitignored, and wins per
    provider when present. Either may be missing: a call site treats that as "no extra provider",
    never as an error.
    """
    named = [(name, Path(__file__).with_name(name)) for name in _FALLBACK_FILES]
    stamps = []
    for name, path in named:
        try:
            stamps.append((name, path.stat().st_mtime))
        except OSError:
            stamps.append((name, None))
    key = tuple(stamps)
    with _fallback_lock:
        if _fallback_cache["key"] == key:
            return _fallback_cache["modules"]
        modules = []
        for (name, path), (_, mtime) in zip(named, stamps):
            if mtime is None:
                continue
            try:
                spec = importlib.util.spec_from_file_location(f"hermes_quota_gauge_{path.stem}", path)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    modules.append(module)
            except Exception as exc:  # a broken module must not take the routes down
                log.warning("quota-gauge: %s failed to load: %s", name, exc)
        _fallback_cache.update(key=key, modules=tuple(modules))
        return _fallback_cache["modules"]


def _bridge_quota(pid: str) -> Optional[dict]:
    """``<pid>_quota()`` from the fallback modules, a local override winning."""
    for module in reversed(_fallback_modules()):
        fn = getattr(module, f"{pid}_quota", None)
        if not callable(fn):
            continue
        try:
            result = fn()
        except Exception as exc:
            log.warning("quota-gauge: %s_quota failed: %s", pid, exc)
            continue
        if result:
            return result
    return None




def detect(pid: str) -> Optional[str]:
    """Where this profile keeps a credential for ``pid``: ``"secret:NAME"``, ``"hermes:slug"`` or None."""
    spec = SPECS[pid]
    for name in spec.secrets:
        if _secret(name):
            return f"secret:{name}"
    for slug in spec.hermes:
        if _pool_has(slug):
            return f"hermes:{slug}"
    # Sources Hermes's credential pool does not model, but a usable token for this provider
    # lives on this machine anyway. Anthropic: the same resolver core's account-usage route
    # uses (env key or the Claude CLI sign-in), so a card core can fill is never filed inactive.
    if pid == "anthropic" and _anthropic_core_token():
        return "token:anthropic"
    if pid == "antigravity" and _windows_cred_blob(_AG_CRED_TARGET) is not None:
        return "wincred:gemini:antigravity"
    return None


_detect_cache: dict[tuple[str, str], tuple[float, Optional[str]]] = {}


def _detect_cached(home_key: str, pid: str, force: bool = False) -> Optional[str]:
    now = time.monotonic()
    hit = _detect_cache.get((home_key, pid))
    if hit and not force and now - hit[0] < DETECT_TTL_S:
        return hit[1]
    via = detect(pid)
    _detect_cache[(home_key, pid)] = (now, via)
    return via


def _first_secret(pid: str) -> tuple[Optional[str], Optional[str]]:
    """(value, source name) of the first usable key for ``pid``.

    The plugin's own declared secret wins; when it is unset the provider's own credential in
    Hermes's pool is used, because that is the same key the provider itself is configured with —
    a card must never demand a second copy of a key Hermes already holds."""
    for name in SPECS[pid].secrets:
        value = _secret(name)
        if value:
            return value, name
    for slug in SPECS[pid].hermes:
        value, label = _pool_key(slug)
        if value:
            return value, label or slug
    return None, None


# ---------------------------------------------------------------------------------------------
# Normalized results
# ---------------------------------------------------------------------------------------------

def _inactive(pid: str, message: Optional[str] = None) -> dict:
    return _unavailable(pid, message or SPECS[pid].setup, code="inactive")


def _no_subscription(pid: str, message: str) -> dict:
    return _unavailable(pid, message, code="no_subscription")


_WINDOW_LABELS = {
    "current session": ("5h", "5h"),
    "session": ("5h", "5h"),
    "rolling window": ("5h", "5h"),
    "current week": ("week", "Week"),
    "weekly": ("week", "Week"),
    "monthly": ("month", "Month"),
    "subscription": ("plan", "Plan"),
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
        wid, short = _WINDOW_LABELS.get(label.lower(), (label.lower().replace(" ", "_") or "window", label))
        windows.append(_window(wid, short, getattr(w, "used_percent", None), getattr(w, "reset_at", None),
                               getattr(w, "detail", None), getattr(w, "scope", "account") or "account"))
    windows = [w for w in windows if w]
    if not windows:
        return None
    return _result(pid, windows=windows, plan=getattr(snapshot, "plan", None), source=source,
                   details=list(getattr(snapshot, "details", ()) or ()))


_core_skip_until: dict[str, float] = {}


def _core_snapshot(provider_slug: str) -> Any:
    """``fetch_account_usage`` from Hermes core (read-only: never refreshes or rotates a
    credential). A miss is remembered for 10 minutes so a provider that can't answer isn't asked on
    every poll."""
    now = time.time()
    if _core_skip_until.get(provider_slug, 0) > now:
        return None
    try:
        from agent.account_usage import fetch_account_usage

        snapshot = fetch_account_usage(provider_slug, read_only=True)
    except Exception as exc:
        log.debug("quota-gauge: core usage for %s failed: %s", provider_slug, exc)
        snapshot = None
    if snapshot is None:
        _core_skip_until[provider_slug] = now + 600
    return snapshot


def _plan_is_free(plan: Any) -> bool:
    return str(plan or "").strip().lower() in ("free", "guest", "none", "free_limited_copilot")


def _span_label(minutes: Optional[float]) -> tuple[str, str]:
    """Window id and short label for a window length in minutes."""
    if not minutes or minutes <= 0:
        return "window", "Window"
    if abs(minutes - 300) <= 1:
        return "5h", "5h"
    if abs(minutes - 1440) <= 1:
        return "day", "Day"
    if abs(minutes - 10080) <= 1:
        return "week", "Week"
    if 40000 <= minutes <= 46000:
        return "month", "Month"
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}d", f"{int(minutes // 1440)}d"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}h", f"{int(minutes // 60)}h"
    return f"{int(minutes)}m", f"{int(minutes)}m"


def _http_get(url: str, headers: dict, client_factory: Optional[Callable[[], Any]] = None) -> Any:
    """GET ``url`` and return JSON; a 4xx/5xx raises ProviderHTTPError (with Retry-After)."""
    import httpx

    factory = client_factory or (lambda: httpx.Client(timeout=10.0, follow_redirects=False))
    with factory() as client:
        resp = client.get(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers})
    if resp.status_code >= 400:
        raise ProviderHTTPError(resp.status_code, _retry_after(resp.headers.get("retry-after")))
    return resp.json()


# ---------------------------------------------------------------------------------------------
# Provider fetchers. Each runs only when ``detect`` found a credential, returns a normalized dict
# and may raise ProviderHTTPError.
# ---------------------------------------------------------------------------------------------

def fetch_anthropic() -> dict:
    snapshot = _core_snapshot("anthropic")
    reason = str(getattr(snapshot, "unavailable_reason", "") or "")
    if reason and "oauth" in reason.lower():
        return _no_subscription("anthropic", "Only an Anthropic API key is set. Plan limits need a Claude "
                                             "sign-in (`hermes auth`).")
    result = _from_core_snapshot("anthropic", snapshot) or _bridge_quota("anthropic")
    if result:
        return result
    return _unavailable(
        "anthropic",
        "Hermes couldn't read your Claude limits. The usage API needs a full Claude sign-in; "
        "tokens from `claude setup-token` only carry the inference scope.",
    )


def fetch_codex() -> dict:
    snapshot = _core_snapshot("openai-codex")
    if snapshot is not None and _plan_is_free(getattr(snapshot, "plan", None)):
        return _no_subscription("codex", "Signed in with a free ChatGPT account. No paid plan to track.")
    result = _from_core_snapshot("codex", snapshot)
    if result:
        return result
    return _unavailable("codex", "Hermes couldn't read your Codex limits right now. Check `hermes auth status`.")


def fetch_opencode() -> dict:
    result = _from_core_snapshot("opencode", _core_snapshot("opencode-go"))
    if result:
        result["plan"] = result.get("plan") or "Go"
        return result
    return _unavailable("opencode", "OpenCode didn't return Go usage for this key. Zen keys are pay-as-you-go "
                                    "and have no subscription windows.")


def fetch_nous() -> dict:
    try:
        from agent.account_usage import build_nous_credits_snapshot
        from hermes_cli.nous_account import get_nous_portal_account_info

        info = get_nous_portal_account_info(force_fresh=True)
    except Exception as exc:
        log.debug("quota-gauge: nous portal read failed: %s", exc)
        return _unavailable("nous", "Hermes couldn't reach the Nous Portal right now.")
    if not getattr(info, "logged_in", False):
        return _inactive("nous")
    snapshot = build_nous_credits_snapshot(info)
    sub = getattr(info, "subscription", None)
    windows = list(getattr(snapshot, "windows", ()) or ())
    if sub is None and not windows:
        return _no_subscription("nous", "Signed in to the Nous Portal without a subscription.")
    details = [d for d in (getattr(snapshot, "details", ()) or ())
               if not str(d).lower().startswith(("top up", "(or run"))]
    result = _from_core_snapshot("nous", snapshot)
    if result:
        result["details"] = details
        return result
    return _result("nous", windows=[], plan=getattr(sub, "plan", None), source="hermes", details=details) \
        if details else _unavailable("nous", "The Nous Portal returned no usage windows for this plan.")


def fetch_xai() -> dict:
    result = _from_core_snapshot("xai", _core_snapshot("xai-oauth"))
    if result:
        return result
    # Core has no Grok fetcher. The local bridge reads grok.com's weekly window with Hermes's
    # xAI sign-in; without it, fall back to the billing config, which reports a credits period
    # rather than a percentage window.
    br = _bridge_quota("xai")
    if br:
        return br
    native = _grok_credits()
    if native:
        return native
    return _unavailable(
        "xai",
        "Grok's billing config returned neither windows nor credits for this sign-in. Token "
        "totals below still track.",
    )


def fetch_antigravity() -> dict:
    result = _from_core_snapshot("antigravity", _core_snapshot("antigravity-oauth"))
    if result:
        return result
    br = _bridge_quota("antigravity")
    if br:
        return br
    native = _antigravity_quota()
    if native:
        return native
    return _unavailable(
        "antigravity",
        "Antigravity is signed in on this machine but its quota summary could not be read. "
        "Token totals track below.",
    )


def fetch_devin() -> dict:
    # Devin publishes ACU consumption only to Enterprise admins (v3 enterprise/consumption); a
    # personal or Teams key has no quota endpoint, so the card states that instead of guessing.
    return _unavailable("devin", "Devin key found. Devin publishes ACU consumption only to Enterprise "
                                 "admins, so there's no quota bar for this key.")


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
    key, _source = _first_secret("commandcode")
    if not key:
        return _unavailable("commandcode", "Hermes has a Command Code credential, but the plugin reads the key "
                                           "from COMMANDCODE_API_KEY. Set it in Settings → Plugins → Quota Gauge.")
    import httpx

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": USER_AGENT}
    factory = client_factory or (lambda: httpx.Client(timeout=10.0, follow_redirects=False, headers=headers))

    def get(client, path: str, **params) -> Any:
        resp = client.get(_CC_BASE + path, params={k: v for k, v in params.items() if v})
        if resp.status_code >= 400:
            raise ProviderHTTPError(resp.status_code, _retry_after(resp.headers.get("retry-after")),
                                    _error_detail(resp))
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

    if not plan_id and not [w for w in windows if w]:
        return _no_subscription("commandcode", "No Command Code subscription on this key (pay-as-you-go credits only).")
    if not windows and not details:
        return _unavailable("commandcode", "Command Code returned no usage windows for this key.")
    return _result("commandcode", windows=windows, plan=_cc_plan_name(plan_id), source="api", details=details,
                   warning=warning)


# Z.ai (GLM Coding Plan) -----------------------------------------------------------------------

_ZAI_HOSTS = ("https://api.z.ai", "https://open.bigmodel.cn")
_ZAI_UNIT_MINUTES = {1: 1440, 3: 60, 5: 1, 6: 10080}


def parse_zai(payload: Any) -> dict:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderHTTPError(502)
    plan = next((data.get(k) for k in ("planName", "plan", "plan_type", "packageName", "level")
                 if isinstance(data.get(k), str) and data.get(k)), None)
    windows, details = [], []
    for raw in data.get("limits") or ():
        if not isinstance(raw, dict) or raw.get("type") not in ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT"):
            continue
        cap, current, left = _num(raw.get("usage")), _num(raw.get("currentValue")), _num(raw.get("remaining"))
        percent = _num(raw.get("percentage"))
        if cap and cap > 0 and (left is not None or current is not None):
            used = cap - left if left is not None else current
            percent = max(0.0, min(cap, used or 0.0)) / cap * 100.0
        if percent is None:
            continue
        if raw.get("type") == "TIME_LIMIT":
            detail = f"{int(cap - (left or 0)):,} of {int(cap):,} tool calls" if cap and left is not None else None
            windows.append(_window("mcp", "MCP", percent, raw.get("nextResetTime"), detail))
            continue
        number, unit = raw.get("number"), raw.get("unit")
        minutes = number * _ZAI_UNIT_MINUTES[unit] if isinstance(number, int) and unit in _ZAI_UNIT_MINUTES else None
        wid, label = _span_label(minutes)
        windows.append(_window(wid, label, percent, raw.get("nextResetTime")))
    if not [w for w in windows if w]:
        return _no_subscription("zai", "This Z.ai key has no GLM Coding Plan.")
    return _result("zai", windows=windows, plan=plan, source="api", details=details)


def fetch_zai(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    key, _ = _first_secret("zai")
    if not key:
        return _unavailable("zai", "Hermes has a Z.ai credential, but the plugin reads the key from ZAI_API_KEY. "
                                   "Set it in Settings → Plugins → Quota Gauge.")
    last: Optional[ProviderHTTPError] = None
    for host in _ZAI_HOSTS:  # global first, then the China-mainland BigModel host
        try:
            return parse_zai(_http_get(host + "/api/monitor/usage/quota/limit",
                                       {"Authorization": f"Bearer {key}"}, client_factory))
        except ProviderHTTPError as exc:
            if exc.status not in (401, 403):
                raise
            last = exc
    raise last or ProviderHTTPError(401)


# Kimi Code ------------------------------------------------------------------------------------

_KIMI_UNIT_MINUTES = {"TIME_UNIT_MINUTE": 1, "TIME_UNIT_HOUR": 60, "TIME_UNIT_DAY": 1440, "TIME_UNIT_WEEK": 10080}


def _kimi_window(detail: Any, minutes: Optional[float], fallback: tuple[str, str]) -> Optional[dict]:
    if not isinstance(detail, dict):
        return None
    cap, used, left = _num(_f(detail.get("limit"))), _num(_f(detail.get("used"))), _num(_f(detail.get("remaining")))
    ratio = _num(_f(detail.get("ratio") if detail.get("ratio") is not None else detail.get("usedRatio")))
    if cap and cap > 0:
        used = used if used is not None else (cap - left if left is not None else None)
        if used is None:
            return None
        percent, text = used / cap * 100.0, f"{int(used):,} of {int(cap):,} requests"
    elif ratio is not None:
        percent, text = (ratio * 100.0 if ratio <= 1 else ratio), None
    else:
        return None
    wid, label = _span_label(minutes) if minutes else fallback
    return _window(wid, label, percent, detail.get("resetTime"), text)


def _f(value: Any) -> Any:
    """Kimi sends counts as strings."""
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return value


def parse_kimi(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ProviderHTTPError(502)
    blocks = [payload]
    if isinstance(payload.get("usages"), list):  # newer pooled shape
        blocks = [u for u in payload["usages"] if isinstance(u, dict)] or blocks
    windows = []
    for block in blocks:
        for item in block.get("limits") or ():
            if not isinstance(item, dict):
                continue
            win = item.get("window") or {}
            dur, unit = _num(_f(win.get("duration"))), _KIMI_UNIT_MINUTES.get(str(win.get("timeUnit") or ""))
            windows.append(_kimi_window(item.get("detail"), dur * unit if dur and unit else None, ("5h", "5h")))
        windows.append(_kimi_window(block.get("usage") or block.get("detail"), None, ("week", "Week")))
    windows = [w for w in windows if w]
    plan = None
    for key in ("membership", "level", "plan", "tier"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("level") or value.get("name") or value.get("title")
        if isinstance(value, str) and value:
            plan = value.replace("LEVEL_", "").replace("_", " ").title()
            break
    if not windows:
        return _no_subscription("kimi", "This key has no Kimi Code membership.")
    return _result("kimi", windows=windows, plan=plan, source="api")


def fetch_kimi(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    key, _ = _first_secret("kimi")
    if not key:
        return _unavailable("kimi", "Hermes has a Kimi credential, but the plugin reads the key from KIMI_API_KEY. "
                                    "Set it in Settings → Plugins → Quota Gauge.")
    return parse_kimi(_http_get("https://api.kimi.com/coding/v1/usages", {"Authorization": f"Bearer {key}"},
                                client_factory))


# MiniMax (Coding Plan) ------------------------------------------------------------------------

def _mm_window(total: Any, left: Any, left_pct: Any, start: Any, end: Any, remains_ms: Any,
               fallback: tuple[str, str]) -> Optional[dict]:
    total, left, left_pct = _num(total), _num(left), _num(left_pct)
    if left_pct is not None:
        used_pct = 100.0 - (left_pct * 100.0 if left_pct <= 1 else left_pct)
    elif total and total > 0 and left is not None:  # *_usage_count is the REMAINING count
        used_pct = (total - left) / total * 100.0
    else:
        return None
    start_s, end_s = _epoch(start), _epoch(end)
    reset = end_s or (time.time() + float(remains_ms) / 1000.0 if _num(remains_ms) else None)
    minutes = (end_s - start_s) / 60.0 if start_s and end_s and end_s > start_s else None
    wid, label = _span_label(round(minutes)) if minutes else fallback
    detail = f"{int(total - left):,} of {int(total):,} prompts" if total and left is not None else None
    return _window(wid, label, used_pct, reset, detail)


def parse_minimax(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ProviderHTTPError(502)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    base = payload.get("base_resp") or data.get("base_resp") or {}
    rows = [r for r in (data.get("model_remains") or []) if isinstance(r, dict)]
    if not rows:
        if base.get("status_code") in (1004, 2049):
            raise ProviderHTTPError(401)
        return _no_subscription("minimax", "This MiniMax key has no Coding Plan.")
    text_rows = [r for r in rows if "minimax-m" in str(r.get("model_name", "")).lower()] or rows[:1]
    row = text_rows[0]
    windows = [
        _mm_window(row.get("current_interval_total_count"), row.get("current_interval_usage_count"),
                   row.get("current_interval_remaining_percent"), row.get("start_time"), row.get("end_time"),
                   row.get("remains_time"), ("5h", "5h")),
    ]
    if _num(row.get("current_weekly_total_count")):
        windows.append(_mm_window(row.get("current_weekly_total_count"), row.get("current_weekly_usage_count"),
                                  row.get("current_weekly_remaining_percent"), row.get("weekly_start_time"),
                                  row.get("weekly_end_time"), row.get("weekly_remains_time"), ("week", "Week")))
    plan = next((data.get(k) for k in ("current_subscribe_title", "plan_name", "combo_title")
                 if isinstance(data.get(k), str) and data.get(k)), None)
    windows = [w for w in windows if w]
    if not windows:
        return _no_subscription("minimax", "This MiniMax key has no Coding Plan.")
    return _result("minimax", windows=windows, plan=plan, source="api")


def fetch_minimax(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    key, name = _first_secret("minimax")
    if not key:
        return _unavailable("minimax", "Hermes has a MiniMax sign-in, but the plugin can only read a Coding Plan "
                                       "key from MINIMAX_API_KEY. Set it in Settings → Plugins → Quota Gauge.")
    host = "https://api.minimaxi.com" if name == "MINIMAX_CN_API_KEY" else "https://api.minimax.io"
    return parse_minimax(_http_get(host + "/v1/api/openplatform/coding_plan/remains",
                                   {"Authorization": f"Bearer {key}"}, client_factory))


# GitHub Copilot -------------------------------------------------------------------------------

_COPILOT_PLANS = {"individual": "Pro", "individual_pro": "Pro+", "business": "Business", "enterprise": "Enterprise"}


def parse_copilot(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ProviderHTTPError(502)
    plan = str(payload.get("copilot_plan") or "")
    if _plan_is_free(plan) or payload.get("access_type_sku") == "free_limited_copilot":
        return _no_subscription("copilot", "Copilot Free on this account. No paid plan to track.")
    reset = payload.get("quota_reset_date_utc") or payload.get("quota_reset_date")
    snaps = payload.get("quota_snapshots") or {}
    windows, unlimited = [], []
    for key, label in (("premium_interactions", "Premium"), ("chat", "Chat"), ("completions", "Completions")):
        snap = snaps.get(key)
        if not isinstance(snap, dict):
            continue
        if snap.get("unlimited"):
            unlimited.append(label.lower())
            continue
        left_pct, cap, left = _num(snap.get("percent_remaining")), _num(snap.get("entitlement")), _num(snap.get("remaining"))
        if left_pct is None and cap and cap > 0 and left is not None:
            left_pct = left / cap * 100.0
        if left_pct is None:
            continue
        detail = f"{int(left):,} of {int(cap):,} left" if cap and left is not None else None
        windows.append(_window(key, label, 100.0 - left_pct, reset, detail))
    details = [f"Unlimited {' and '.join(unlimited)}"] if unlimited else []
    if not windows and not details:
        return _unavailable("copilot", "GitHub returned no Copilot quota for this token.")
    return _result("copilot", windows=windows, plan=_COPILOT_PLANS.get(plan, plan.replace("_", " ").title() or None),
                   source="api", details=details)


def fetch_copilot(client_factory: Optional[Callable[[], Any]] = None) -> dict:
    key, _ = _first_secret("copilot")
    if not key:
        return _unavailable("copilot", "Signed in to Copilot in Hermes, but Hermes doesn't expose that token to "
                                       "plugins. Set COPILOT_GITHUB_TOKEN to show quota bars.")
    return parse_copilot(_http_get("https://api.github.com/copilot_internal/user",
                                   {"Authorization": f"token {key}", "X-GitHub-Api-Version": "2025-04-01"},
                                   client_factory))


FETCHERS: dict[str, Callable[[], dict]] = {
    "anthropic": fetch_anthropic,
    "codex": fetch_codex,
    "commandcode": fetch_commandcode,
    "xai": fetch_xai,
    "antigravity": fetch_antigravity,
    "opencode": fetch_opencode,
    "devin": fetch_devin,
    "copilot": fetch_copilot,
    "nous": fetch_nous,
    "zai": fetch_zai,
    "kimi": fetch_kimi,
    "minimax": fetch_minimax,
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
            keep_prev = True
            if isinstance(status, int):
                wait_s = _retry_after(getattr(exc, "retry_after", None)) or 120.0
                if status == 429:
                    wait_s = max(wait_s, 600.0)
                message = ("The credential was rejected." if status in (401, 403)
                           else "Rate limited by the provider, backing off." if status == 429
                           else f"Provider error (HTTP {status}).")
                detail = " ".join(str(getattr(exc, "detail", "") or "").split())
                if detail and detail.lower() not in message.lower():
                    message = f"{message} {detail}"
                # A rejected credential is not a transient miss: the numbers behind it can't be
                # trusted until the key changes, so the card states the problem instead of
                # keeping the last good bars under a warning triangle.
                keep_prev = status not in (401, 403)
            else:  # network errors, malformed JSON, and any failure without an HTTP status
                log.debug("quota-gauge: %s fetch failed: %s", pid, exc, exc_info=True)
                wait_s = 120.0
                message = getattr(exc, "message", None) or f"Couldn't reach {PROVIDER_NAMES[pid]} ({type(exc).__name__})."
            entry = _error_entry(pid, prev if keep_prev else None, message, now, wait_s)
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
    signed_in = {pid: _detect_cached(home_key, pid, force=force) for pid in PROVIDERS}
    due = []
    for pid in PROVIDERS:
        if signed_in[pid] is None:
            continue  # no credential: no network request
        entry = _quota_cache.get((home_key, pid))
        if entry is None:
            due.append(pid)
            continue
        age = now - entry["fetched_at"]
        # ``force`` is the pane's refresh button: explicit intent outranks a provider backoff,
        # otherwise a credential the user just fixed stays invisible for the whole cooldown.
        if entry.get("backoff_until", 0) > now and not force:
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
        if signed_in[pid] is None:
            data = _inactive(pid)
        else:
            entry = _quota_cache.get((home_key, pid))
            if entry is None:
                providers[pid] = {**_unavailable(pid, "Still loading…", code="loading"), "signed_in": signed_in[pid]}
                continue
            data = dict(entry["data"])
            data["fetched_at"] = entry["fetched_at"]
            data["stale"] = bool(entry.get("error")) and data.get("status") == "ok"
            data["error"] = entry.get("error")
        data["signed_in"] = signed_in[pid]
        data["hidden"] = data.get("status") in HIDDEN_STATUSES
        providers[pid] = data
    return {"generated_at": now, "order": list(PROVIDERS), "providers": providers}


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
CREATE TABLE IF NOT EXISTS model_buckets (
  t INTEGER NOT NULL,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0, cache_write INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (t, provider, model)
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


# Hermes billing-provider slug -> card id. Anything else keeps its own slug (still charted).
_PROVIDER_ALIASES = {
    "xai-oauth": "xai", "grok": "xai",
    "antigravity-oauth": "antigravity", "agy": "antigravity",
    "command-code": "commandcode", "command_code": "commandcode", "commandcode-anthropic": "commandcode",
    "commandcode-chat": "commandcode", "commandcode-claude": "commandcode",
    "openai-codex": "codex",
    "opencode-go": "opencode", "opencode-zen": "opencode",
    "copilot-acp": "copilot",
    "kimi-coding": "kimi", "kimi-coding-cn": "kimi",
    "minimax-cn": "minimax", "minimax-oauth": "minimax",
}


def normalize_provider(raw: Any) -> str:
    p = str(raw or "").strip().lower()
    return _PROVIDER_ALIASES.get(p, p) or "unknown"


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


def _shared_root() -> Path:
    """The Hermes root that holds every profile, not the profile that happens to be serving."""
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        pass
    try:
        return _hermes_home()
    except Exception:  # no Hermes runtime around (unit tests): derive it from HERMES_HOME
        raw = os.environ.get("HERMES_HOME", "").strip()
        home = Path(os.path.expanduser(os.path.expandvars(raw))) if raw else Path.home() / ".hermes"
        return home.parent.parent if home.parent.name == "profiles" else home


def _profile_homes() -> list[Path]:
    """Every Hermes home on this install: the default home plus each profile under it."""
    root = _shared_root()
    homes: list[Path] = []

    def add(home: Path) -> None:
        try:
            key = home.resolve()
        except OSError:
            key = home
        if key not in seen and (home / "state.db").is_file():
            seen.add(key)
            homes.append(home)

    seen: set[Path] = set()
    add(root)
    for db in sorted((root / "profiles").glob("*/state.db")):
        add(db.parent)
    try:
        add(_hermes_home())
    except Exception:
        pass
    return homes


def _ledger_path() -> Path:
    """One ledger for the whole install: usage is counted account-wide, never per profile."""
    override = os.environ.get("HERMES_QUOTA_GAUGE_LEDGER", "").strip()
    if override:
        return Path(override).expanduser()
    return _shared_root() / "shared" / PLUGIN_ID / "ledger.db"


def _legacy_ledgers() -> list[Path]:
    root = _shared_root()
    found = [root / "plugin-data" / PLUGIN_ID / "ledger.db"]
    found += sorted((root / "profiles").glob(f"*/plugin-data/{PLUGIN_ID}/ledger.db"))
    return [p for p in found if p.is_file()]


def _seed_shared_ledger(target: Path) -> None:
    """First run on the shared path adopts the biggest existing per-profile ledger, so switching
    or adding a profile never resets the totals."""
    try:
        if target.is_symlink() or (target.exists() and target.stat().st_size > 0):
            return
    except OSError:
        return
    best: Optional[Path] = None
    best_total = -1
    for path in _legacy_ledgers():
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                total = src.execute(
                    "SELECT COALESCE(SUM(input + output + cache_read + cache_write), 0) FROM totals"
                ).fetchone()[0]
            finally:
                src.close()
        except sqlite3.Error:
            continue
        if total and total > best_total:
            best, best_total = path, int(total)
    if best is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f"{target.name}.seed-{os.getpid()}")
    try:
        src = sqlite3.connect(f"file:{best}?mode=ro", uri=True, timeout=5)
        try:
            src.execute("VACUUM INTO ?", (str(staging),))
        finally:
            src.close()
        os.replace(staging, target)
    except (sqlite3.Error, OSError) as exc:
        log.debug("quota-gauge: could not seed the shared ledger: %s", exc)
    finally:
        try:
            staging.unlink()
        except OSError:
            pass


def _open_ledger() -> sqlite3.Connection:
    target = _ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    _seed_shared_ledger(target)
    try:
        from hermes_cli.sqlite_util import open_db  # WAL + network-fs rules, same as plugin storage

        conn = open_db(target, db_label=f"shared/{PLUGIN_ID}/ledger.db",
                       foreign_keys=True, check_same_thread=False)
    except ImportError:  # older Hermes: plain sqlite
        conn = sqlite3.connect(target, check_same_thread=False, timeout=10)
    conn.isolation_level = None  # explicit transactions below
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.executescript(_SCHEMA)
    return conn


def _key_model(key: str) -> str:
    parts = key.split("\x1f")
    return (parts[1] if len(parts) > 1 else "").strip() or "unknown"


def _book(lo: float, last_seen: float) -> float:
    """Start of the span a delta is spread over: a short gap is spread evenly; across a long one
    (pane closed, backend down) the session was mostly idle, so the growth goes where its latest
    activity was."""
    lo = min(lo, last_seen)
    return last_seen if last_seen - lo > LONG_GAP_S else lo


def _backfill_models(conn: sqlite3.Connection, state_path: Path, seen: dict) -> dict:
    """One-time: rebuild the per-model buckets from what the ledger has already booked. Each known
    row's last reading is placed over its lifetime from state.db (or at its last sighting when the
    session is gone), the way a new row is booked."""
    timing = {}
    for row in _read_state_rows(state_path, 0):
        key = "\x1f".join(str(x or "") for x in row[:6])
        last_seen = float(row[11] or row[10] or 0)
        timing[key] = (float(row[10] or last_seen), last_seen)
    # Rough placement of each model's tokens in time, used only as weights below.
    guess: dict[tuple[int, str], dict[str, dict]] = defaultdict(dict)
    overall: dict[str, dict[str, dict]] = defaultdict(dict)
    for key, (provider, *vals, seen_last) in seen.items():
        model = _key_model(key)
        first, last = timing.get(key, (float(seen_last or 0), float(seen_last or 0)))
        for bt, amounts in spread(dict(zip(KINDS, vals)), _book(first, last), last).items():
            slot = guess[(bt, provider)].setdefault(model, dict.fromkeys(KINDS, 0))
            for k in KINDS:
                slot[k] += amounts[k]
        slot = overall[provider].setdefault(model, dict.fromkeys(KINDS, 0))
        for k, v in zip(KINDS, vals):
            slot[k] += int(v or 0)
    # The ledger's buckets stay the truth: each one is split across models, so per-model sums
    # always equal the chart.
    # Iterative proportional fitting nudges the weights so each model's lifetime total also holds.
    adds: dict[tuple[int, str, str], dict[str, int]] = defaultdict(lambda: {k: 0 for k in KINDS})
    cells: dict[tuple[str, str], list] = defaultdict(list)  # (provider, kind) -> [(t, amount, weights)]
    for t, provider, *vals in conn.execute(
            "SELECT t, provider, input, output, cache_read, cache_write FROM buckets").fetchall():
        for k, amount in zip(KINDS, vals):
            amount = int(amount or 0)
            if amount <= 0:
                continue
            target = {m: a[k] for m, a in overall.get(provider, {}).items() if a[k] > 0}
            weights = {m: float(a[k]) for m, a in guess.get((t, provider), {}).items() if a[k] > 0} or \
                {m: 1e-6 * v for m, v in target.items()} or {"unknown": 1.0}
            cells[(provider, k)].append((t, amount, weights))
    for (provider, k), rows in cells.items():
        target = {m: a[k] for m, a in overall.get(provider, {}).items() if a[k] > 0}
        for _ in range(30):
            for i, (t, amount, w) in enumerate(rows):  # rows: match each bucket
                s = sum(w.values()) or 1.0
                rows[i] = (t, amount, {m: v * amount / s for m, v in w.items()})
            col: dict[str, float] = defaultdict(float)
            for _, _, w in rows:
                for m, v in w.items():
                    col[m] += v
            if not target:
                break
            for _, _, w in rows:  # columns: match each model's total
                for m in w:
                    if m in target and col[m] > 0:
                        w[m] *= target[m] / col[m]
        for t, amount, w in rows:
            for model, share in _split(amount, w).items():
                adds[(t, provider, model)][k] += share
    return adds


def _split(amount: int, weights: dict[str, float]) -> dict[str, int]:
    """Integer split of ``amount`` by weight; largest remainder keeps the sum exact."""
    total = sum(weights.values())
    names = sorted(weights)
    shares = [amount * weights[n] / total for n in names]
    alloc = [int(s) for s in shares]
    for i in sorted(range(len(names)), key=lambda i: shares[i] - alloc[i], reverse=True)[:amount - sum(alloc)]:
        alloc[i] += 1
    return {n: a for n, a in zip(names, alloc) if a}


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


def sync_ledger(conn: sqlite3.Connection, state_path: Path, now: Optional[float] = None,
                hw_key: str = "hw") -> int:
    """Fold new token usage from state.db into the ledger. Returns the number of rows that changed.

    The ledger is shared by every profile, so each home carries its own high-water mark
    (``hw_key``) while the buckets and totals stay account-wide.

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
        hw = float(meta.get(hw_key, 0) or 0)
        last_sync = float(meta.get("last_sync", 0) or 0)
        rows = _read_state_rows(state_path, hw - 3600 if hw else 0)
        seen = {r[0]: r[1:] for r in conn.execute(
            "SELECT key, provider, input, output, cache_read, cache_write, last_seen FROM seen").fetchall()}
        model_adds = _backfill_models(conn, state_path, seen) if "models_v" not in meta else \
            defaultdict(lambda: dict.fromkeys(KINDS, 0))
        seen = {k: v[1:] for k, v in seen.items()}
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
            model = _key_model(key)
            for bt, amounts in spread(delta, _book(start, last_seen), last_seen).items():
                slot = bucket_adds[(bt, provider)]
                mslot = model_adds[(bt, provider, model)]
                for k in KINDS:
                    slot[k] += amounts[k]
                    mslot[k] += amounts[k]
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
            "INSERT INTO model_buckets (t, provider, model, input, output, cache_read, cache_write) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(t, provider, model) DO UPDATE SET "
            "input = input + excluded.input, output = output + excluded.output, "
            "cache_read = cache_read + excluded.cache_read, cache_write = cache_write + excluded.cache_write",
            [(bt, p, m, *(a[k] for k in KINDS)) for (bt, p, m), a in model_adds.items() if any(a.values())])
        conn.executemany(
            "INSERT INTO totals (provider, input, output, cache_read, cache_write, first_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET input = input + excluded.input, "
            "output = output + excluded.output, cache_read = cache_read + excluded.cache_read, "
            "cache_write = cache_write + excluded.cache_write, "
            "first_at = MIN(COALESCE(first_at, excluded.first_at), excluded.first_at)",
            [(p, *(a[k] for k in KINDS), first_at.get(p)) for p, a in total_adds.items()])
        updates = {"hw": repr(max(new_hw, float(meta.get("hw", 0) or 0))), "last_sync": repr(now),
                   "models_v": "1"}
        if hw_key != "hw":
            updates[hw_key] = repr(new_hw)
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
    """Fold every profile's state.db into the one shared ledger, so the totals are account-wide."""
    key = "shared"
    if not force and time.monotonic() - _last_sync.get(key, -1e9) < SYNC_MIN_INTERVAL_S:
        return
    with _sync_lock:
        if not force and time.monotonic() - _last_sync.get(key, -1e9) < SYNC_MIN_INTERVAL_S:
            return
        homes = _profile_homes() or [_hermes_home()]
        conn = _open_ledger()
        try:
            for home in homes:
                try:
                    sync_ledger(conn, home / "state.db", hw_key=f"hw:{home}")
                except Exception as exc:  # one unreadable home must not stop the others
                    log.debug("quota-gauge: sync failed for %s: %s", home, exc)
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


def _models(rows: list, lo: int, hi: int) -> list[dict]:
    """Per (provider, model) totals for buckets in ``[lo, hi)``, biggest first."""
    out: dict[tuple[str, str], dict] = {}
    for t, provider, model, *vals in rows:
        if not lo <= t < hi:
            continue
        p = normalize_provider(provider)
        slot = out.setdefault((p, model), {"model": model, "provider": p, **dict.fromkeys(KINDS, 0)})
        for k, v in zip(KINDS, vals):
            slot[k] += int(v or 0)
    models = []
    for slot in out.values():
        slot["total"] = sum(slot[k] for k in KINDS)
        if slot["total"] > 0:
            models.append(slot)
    return sorted(models, key=lambda m: (-m["total"], m["model"], m["provider"]))


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
    model_rows = conn.execute(
        "SELECT t, provider, model, SUM(input), SUM(output), SUM(cache_read), SUM(cache_write) "
        "FROM model_buckets WHERE t >= ? AND t < ? GROUP BY t, provider, model", (lo, hi)).fetchall()
    series = {name: {"unit": unit, "bars": _bin(rows, e), "models": _models(model_rows, e[0], e[-1])}
              for name, (unit, e) in edges.items()}
    providers: dict[str, dict] = {}
    for p, *vals in conn.execute(
            "SELECT provider, input, output, cache_read, cache_write, first_at FROM totals").fetchall():
        # Rows written before a slug got its alias still land on the right card.
        slot = providers.setdefault(normalize_provider(p), {**dict.fromkeys(KINDS, 0), "first_at": None})
        for k, v in zip(KINDS, vals[:4]):
            slot[k] += int(v or 0)
        if vals[4] is not None:
            slot["first_at"] = vals[4] if slot["first_at"] is None else min(slot["first_at"], vals[4])
    for slot in providers.values():
        slot["total"] = sum(slot[k] for k in KINDS)
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
    home_key = str(_hermes_home())
    return {"ok": True, "version": VERSION,
            "signed_in": {pid: _detect_cached(home_key, pid) for pid in PROVIDERS}}


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


# ---------------------------------------------------------------------------------------------
# Provider-native fetchers for the cards Hermes core cannot answer
# ---------------------------------------------------------------------------------------------

_AG_CRED_TARGET = "gemini:antigravity"
_AG_UA = "antigravity/2.8.0 windows/amd64"
_AG_QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
_GROK_GRPC_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
_GROK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# bucketId -> (window id, label). Google matches buckets by id, never by array position.
_AG_BUCKETS = {
    "gemini-5h": ("gemini_5h", "5h"),
    "gemini-weekly": ("gemini_week", "Week"),
    "3p-5h": ("3p_5h", "5h"),
    "3p-weekly": ("3p_week", "Week"),
}


def _anthropic_core_token() -> bool:
    """True when core can resolve a Claude token, i.e. its account-usage route can answer.

    Hermes's credential pool does not list Anthropic when the sign-in lives in the Claude CLI
    store, yet ``fetch_account_usage("anthropic")`` reads it fine. Gating on the pool alone
    filed a card core could fill under Hidden/Inactive.
    """
    try:
        from agent.anthropic_credentials import resolve_anthropic_token

        return bool((resolve_anthropic_token() or "").strip())
    except Exception:
        return False


def _windows_cred_blob(target: str) -> Optional[bytes]:
    """Generic-credential blob from Windows Credential Manager, or None. Never raises."""
    import sys

    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as wt

    class _Attr(ctypes.Structure):
        _fields_ = [("Keyword", wt.LPWSTR), ("Flags", wt.DWORD),
                    ("ValueSize", wt.DWORD), ("Value", ctypes.POINTER(ctypes.c_byte))]

    class _Cred(ctypes.Structure):
        # FILETIME is two DWORDs: a 64-bit field here shifts every later member and the
        # CredFree call then corrupts the heap.
        _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD),
                    ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                    ("LastWritten", wt.FILETIME), ("CredentialBlobSize", wt.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                    ("Attributes", ctypes.POINTER(_Attr)),
                    ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

    advapi = ctypes.windll.advapi32  # type: ignore[attr-defined]
    out = ctypes.POINTER(_Cred)()
    try:
        if not advapi.CredReadW(target, 1, 0, ctypes.byref(out)):  # CRED_TYPE_GENERIC
            return None
        try:
            cred = out.contents
            if cred.CredentialBlobSize <= 0 or not cred.CredentialBlob:
                return None
            return bytes(bytearray(cred.CredentialBlob[: cred.CredentialBlobSize]))
        finally:
            advapi.CredFree(out)
    except Exception:
        return None


def _antigravity_token() -> Optional[str]:
    """The Antigravity session token its own app keeps on this machine (read-only)."""
    blob = _windows_cred_blob(_AG_CRED_TARGET)
    if not blob:
        return None
    import base64
    import json

    if blob.startswith(b"go-keyring-base64:"):
        try:
            blob = base64.b64decode(blob[len(b"go-keyring-base64:"):])
        except Exception:
            pass
    try:
        data = json.loads(blob.decode("utf-8"))
    except Exception:
        return None
    # agy/antigravity-cli store the token as a nested object on Windows and macOS
    # ({"token": {"access_token": ...}}); a bare string elsewhere.
    token = data.get("token") if isinstance(data, dict) else None
    if isinstance(token, dict):
        token = token.get("access_token")
    return token.strip() if isinstance(token, str) and token.strip() else None


def _antigravity_quota() -> Optional[dict]:
    """Antigravity's own quota summary (Google cloudcode) with its session token.

    ``remainingFraction`` is 0..1 and *remaining*, so used = (1 - f) * 100 — never a
    fabricated denominator. A bucket carrying a reset time but no fraction is an exhausted
    counter, so it renders 100% used instead of being dropped.
    """
    token = _antigravity_token()
    if not token:
        return None
    import json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        _AG_QUOTA_URL,
        data=b"{}",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _AG_UA,
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _unavailable(
                "antigravity",
                "Antigravity's stored session has expired. Open Antigravity (or run `agy login`) "
                "so it mints a fresh token, then press \u21bb.",
            )
        return _unavailable("antigravity", f"Antigravity quota request failed (HTTP {exc.code}).")
    except Exception:
        return _unavailable("antigravity", "Antigravity quota request failed.")
    windows = []
    for group in payload.get("groups") or ():
        for bucket in (group or {}).get("buckets") or ():
            spec = _AG_BUCKETS.get(str((bucket or {}).get("bucketId") or ""))
            if not spec:
                continue
            fraction = bucket.get("remainingFraction")
            try:
                used = (1.0 - float(fraction)) * 100.0 if fraction is not None else 100.0
            except (TypeError, ValueError):
                used = 100.0
            windows.append(_window(spec[0], spec[1], used, bucket.get("resetTime")))
    if not windows:
        return None
    return _result("antigravity", windows=windows, source="antigravity_quota")


def _xai_oauth_token() -> Optional[str]:
    """Hermes's xAI OAuth access token, read from its own store (never refreshed here)."""
    import json

    try:
        path = Path(_hermes_home()) / "auth.json"
    except Exception:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = ((data.get("providers") or {}).get("xai-oauth") or {})
    token = (state.get("tokens") or {}).get("access_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    for entry in (data.get("credential_pool") or {}).get("xai-oauth") or ():
        token = (entry or {}).get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
    return None


def _grok_credits() -> Optional[dict]:
    """Grok's own billing config (gRPC-web) as an OAuth sign-in can read it.

    grok.com refuses OAuth tokens on ``/rest/rate-limits`` ("Action cannot be performed by
    OAuth2 token users"), so the credits config is the only read a Hermes sign-in can make.
    It reports a credit grant and its period, not percentage windows, so the card carries
    details instead of bars it cannot back.
    """
    token = _xai_oauth_token()
    if not token:
        return None
    import struct
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        _GROK_GRPC_URL,
        data=b"\x00\x00\x00\x00\x00",
        headers={"Authorization": f"Bearer {token}", "User-Agent": _GROK_UA,
                 "content-type": "application/grpc-web+proto", "x-grpc-web": "1",
                 "x-user-agent": "connect-es/2.1.1", "origin": "https://grok.com",
                 "referer": "https://grok.com/?_s=usage", "accept": "*/*"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except Exception:
        return None
    body = raw
    if body[:1] == b"\x00" and len(body) >= 5:
        size = int.from_bytes(body[1:5], "big")
        if size and len(body) >= 5 + size:
            body = body[5:5 + size]

    def _varint(buf: bytes, i: int) -> tuple[int, int]:
        value = shift = 0
        while i < len(buf):
            byte = buf[i]
            i += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                break
        return value, i

    def _fields(buf: bytes) -> dict:
        out: dict[int, list] = {}
        i = 0
        while i < len(buf):
            key, i = _varint(buf, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                value, i = _varint(buf, i)
                out.setdefault(field, []).append(value)
            elif wire == 1:
                i += 8
            elif wire == 5:
                out.setdefault(field, []).append(struct.unpack("<f", buf[i:i + 4])[0])
                i += 4
            elif wire == 2:
                size, i = _varint(buf, i)
                out.setdefault(field, []).append(buf[i:i + size])
                i += size
            else:
                break
        return out

    top = _fields(body)
    inner = (top.get(1) or [b""])[0]
    node = _fields(inner) if isinstance(inner, bytes) else {}
    period: dict[str, int] = {}
    for field, key in ((4, "start"), (5, "end")):
        sub = (node.get(field) or [b""])[0]
        if isinstance(sub, bytes) and sub:
            stamps = _fields(sub).get(1) or []
            if stamps and isinstance(stamps[0], int):
                period[key] = stamps[0]
    details = []
    if period.get("start"):
        details.append("Period: " + datetime.fromtimestamp(
            period["start"], tz=timezone.utc).strftime("%b %d %H:%M UTC"))
    if period.get("end"):
        details.append("Resets " + datetime.fromtimestamp(
            period["end"], tz=timezone.utc).strftime("%b %d %H:%M UTC"))
    if not details:
        return None
    result = _result("xai", windows=[], source="grok_credits", details=details)
    # A card with no windows falls back to ``message`` in the pane; without one it reads
    # "Loading…" forever, which is exactly how this looked broken.
    result["message"] = (
        "Grok publishes a credits period, not usage windows, to an OAuth sign-in. "
        "Bars appear here only if Grok starts reporting a window."
    )
    return result
