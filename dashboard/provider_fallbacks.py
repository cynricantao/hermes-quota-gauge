"""Personal local bridge for Quota Gauge. NOT part of the published plugin (gitignored).

Fills the two providers Hermes doesn't yet expose to plugins through a public route:

* anthropic_quota(): for a `claude setup-token` (inference-only scope) the OAuth usage API answers
  429/403, so this sends one Haiku request with `max_tokens: 0` (nothing generated) and reads the
  `anthropic-ratelimit-unified-*` headers, which are the same numbers claude.ai shows. The backend
  caches the reading for 5 minutes. If the probe cannot run, the card falls back to the reading the
  trigger job recorded in quota-window.json, labelled with the time it was read.
* xai_quota(): reads the SuperGrok weekly window from grok.com with Hermes's xai-oauth sign-in.

Both resolve tokens through Hermes's own resolvers (which refresh under Hermes's lock) and send
them only to the vendor that issued them. Catalog rule 11 forbids this in a listed plugin; the
upstream fix is a core route (see docs/OPEN_SOURCE_PLAN.md).
"""

from __future__ import annotations

import json
import os
import struct
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_UA = "hermes-quota-gauge/0.1.0 (local-bridge)"

# The trigger job (~/.hermes/scripts/start-5h-windows.py) writes this file every time it opens a
# subscription window. It is only a fallback now: its window bookkeeping goes stale as soon as
# anything else (Claude Code, Hermes) sends a message, so the live read is the primary source.
_WINDOW_MARKER = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")) / "quota-window.json"


class BridgeHTTPError(Exception):
    """Duck-typed like plugin_api.ProviderHTTPError (``status`` + ``retry_after``)."""

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


def _window(wid: str, label: str, used_percent: Optional[float], resets_at: Optional[float],
            detail: Optional[str] = None) -> Optional[dict]:
    if used_percent is None:
        return None
    used = max(0.0, min(100.0, float(used_percent)))
    return {"id": wid, "label": label, "used_percent": round(used, 2), "remaining_percent": round(100 - used, 2),
            "resets_at": resets_at, "detail": detail, "scope": "account"}


def _ok(pid: str, name: str, windows: list, source: str, details: Optional[list] = None,
        plan: Optional[str] = None, warning: Optional[dict] = None) -> Optional[dict]:
    windows = [w for w in windows if w]
    if not windows:
        return None
    return {"id": pid, "name": name, "status": "ok", "plan": plan, "source": source, "windows": windows,
            "details": details or [], "warning": warning, "message": None}


def _f(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Anthropic -----------------------------------------------------------------------------------

def _window_state(pid: str) -> dict:
    """The trigger job's record for one provider, or {} when it has never opened a window."""
    try:
        data = json.loads(_WINDOW_MARKER.read_text())
    except Exception:
        return {}
    entry = data.get(pid) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else {}


_PREFIX = "anthropic-ratelimit-unified-"


def _clock(epoch: Optional[float]) -> str:
    if not epoch:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _header_windows(raw: Any, now: Optional[float] = None) -> tuple[list, list[str], Optional[dict]]:
    """``(windows, details, warning)`` from a block of ``anthropic-ratelimit-unified-*`` headers.

    A window whose reset has already passed is reported full, with no countdown: its allowance is
    restored the moment it rolls over, and the next message opens a new one. Keeping the row
    means the card always shows both limits; claiming the old percentage under a "resets now"
    label is what made a closed window look like a nearly spent one.
    """
    if not isinstance(raw, dict):
        return [], [], None
    headers = {str(k).lower(): v for k, v in raw.items()}
    if not any(k.startswith(_PREFIX) for k in headers):
        return [], [], None
    now = time.time() if now is None else now
    windows: list = []
    details: list[str] = []
    for field, wid, label in (("5h", "5h", "5h"), ("7d", "week", "Week")):
        used = _f(headers.get(f"{_PREFIX}{field}-utilization"))
        reset = _f(headers.get(f"{_PREFIX}{field}-reset"))
        if reset and reset <= now:
            rolled = _window(wid, label, 0.0, None)
            if rolled:
                rolled["detail"] = f"ended at {_clock(reset)}"
                windows.append(rolled)
            details.append(f"{label} window ended at {_clock(reset)}")
            continue
        windows.append(_window(wid, label, used * 100 if used is not None else None, reset))
    warning = None
    status = str(headers.get(_PREFIX + "status") or "").strip().lower()
    claim = {"five_hour": "5h", "seven_day": "weekly"}.get(
        str(headers.get(_PREFIX + "representative-claim") or "").strip().lower(), "")
    if status == "allowed_warning":
        warning = {"label": "Near limit", "level": "warn",
                   "detail": f"Anthropic flags the {claim or 'current'} window as close to its limit"}
    elif status == "rejected":
        warning = {"label": "Limited", "level": "limit",
                   "detail": f"The {claim or 'current'} window is used up until it resets"}
    return windows, details, warning


def _checked_at(state: dict) -> Optional[float]:
    """Epoch of the trigger job's last reading."""
    raw = str(state.get("checked_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cached_anthropic(state: dict) -> Optional[dict]:
    """The trigger job's last reading, with any window that has since ended reported full.

    Free (no request) and honest: the card says when it was read instead of showing a stale number
    as if it were live. This is the fallback for a probe that could not run.
    """
    if not state:
        return None
    windows, details, _warning = _header_windows(state.get("ratelimit"))
    windows = [w for w in windows if w]
    if not windows:
        return None
    stamp = _clock(_checked_at(state))
    for w in windows:
        w["detail"] = f"{w['detail']} · read {stamp}" if w.get("detail") and stamp else (
            w.get("detail") or (f"read {stamp}" if stamp else "last scheduled read"))
    if stamp:
        details.append(f"read {stamp}")
    return _ok("anthropic", "Anthropic", windows, source="marker", details=details)


def anthropic_quota() -> Optional[dict]:
    """Live Claude limits, read from Anthropic's own rate-limit headers.

    Those headers are the only source that agrees with claude.ai: the OAuth usage API answers a
    `claude setup-token` with 429/403 (no profile scope), and a cached reading goes stale within
    minutes of real use. The probe is a message with `max_tokens: 0`, so nothing is generated and
    only the prompt's few input tokens are billed. When it cannot run (offline, throttled) the
    trigger job's last reading is served instead, labelled with the time it was read.
    """
    import httpx
    from agent.anthropic_credentials import resolve_anthropic_token

    token = (resolve_anthropic_token() or "").strip()
    if not token.startswith("sk-ant-oat"):
        return None  # API keys have no subscription windows
    # Never probe in a gap: a probe is a paid message, and outside a live window it would open
    # one off the 05/10/15/20 grid. Only probe while ai-window.py has a window recorded as live.
    try:
        live = float(_window_state("anthropic").get("expires_at") or 0) > time.time() + 120
    except (TypeError, ValueError):
        live = False
    if not live:
        return _cached_anthropic(_window_state("anthropic"))
    try:
        resp = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01",
                     "anthropic-beta": "oauth-2025-04-20", "content-type": "application/json",
                     "User-Agent": _UA},
            json={"model": "claude-haiku-4-5", "max_tokens": 0,
                  "messages": [{"role": "user", "content": "."}]},
            timeout=15.0,
        )
    except Exception:
        cached = _cached_anthropic(_window_state("anthropic"))
        if cached:
            return cached
        raise
    h = {k.lower(): v for k, v in resp.headers.items()}
    # The headers are the truth even on a 429: that is a spent window, and it still reports the
    # utilization and the reset time the card needs.
    if not any(k.startswith(_PREFIX) for k in h):
        cached = _cached_anthropic(_window_state("anthropic"))
        if cached:
            return cached
        raise BridgeHTTPError(resp.status_code or 502, _retry_after(h.get("retry-after")))
    windows, details, warning = _header_windows(h)
    return _ok("anthropic", "Anthropic", windows, source="headers", details=details, warning=warning)


# xAI -----------------------------------------------------------------------------------------

_GROK_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"


def _pb_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    out, i = [], 0
    while i < len(buf):
        key, i = _varint(buf, i)
        fn, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(buf, i)
        elif wire == 1:
            v, i = int.from_bytes(buf[i:i + 8], "little"), i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wire == 5:
            v, i = struct.unpack("<f", buf[i:i + 4])[0], i + 4
        else:
            break
        out.append((fn, wire, v))
    return out


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return value, i


def parse_grok_billing(raw: bytes) -> Optional[tuple[float, Optional[float]]]:
    """(weekly used %, reset epoch) from the gRPC-web billing reply. Field map from a live capture:
    1 = weekly % used (absent means 0), 5 = window end {1: epoch seconds}."""
    msg = raw
    if msg[:1] == b"\x00" and len(msg) >= 5:
        n = int.from_bytes(msg[1:5], "big")
        msg = msg[5:5 + n]
    top = _pb_fields(msg)
    if not top or top[0][0] != 1 or top[0][1] != 2:
        return None
    used, reset = None, None
    for fn, wire, v in _pb_fields(top[0][2]):
        if fn == 1 and wire == 5:
            used = float(v)
        elif fn == 5 and wire == 2:
            for sfn, sw, sv in _pb_fields(v):
                if sfn == 1 and sw == 0:
                    reset = float(sv)
    if used is None and reset is None:
        return None
    return (used or 0.0), reset


def xai_quota() -> Optional[dict]:
    import httpx
    from hermes_cli.auth_xai import resolve_xai_oauth_runtime_credentials

    try:
        token = str(resolve_xai_oauth_runtime_credentials().get("api_key") or "").strip()
    except Exception:
        return None
    if not token:
        return None
    resp = httpx.post(
        _GROK_URL, content=b"\x00\x00\x00\x00\x00", timeout=15.0, follow_redirects=False,
        headers={"Authorization": f"Bearer {token}", "content-type": "application/grpc-web+proto",
                 "x-grpc-web": "1", "accept": "*/*", "User-Agent": _UA},
    )
    if resp.status_code != 200:
        return None
    parsed = parse_grok_billing(resp.content)
    if not parsed:
        return None
    used, reset = parsed
    return _ok("xai", "xAI", [_window("week", "Week", used, reset)], source="grok")


# Antigravity ---------------------------------------------------------------------------------
#
# agy keeps its Google OAuth token in the Secret Service (go-keyring: service=gemini,
# username=antigravity), or in ~/.gemini/antigravity-cli/antigravity-oauth-token on hosts without
# one. Read-only: the token is refreshed in memory with Antigravity's public installed-app client
# and never written back. Quota comes from cloudcode-pa's retrieveUserQuotaSummary, which reports
# a 5h and a weekly bucket per model group as a remaining fraction.

# Antigravity's public installed-app OAuth client is not committed here: GitHub's secret scanning
# blocks the literal, and the pair identifies Antigravity's app rather than the user. Provide them
# through ANTIGRAVITY_CLIENT_ID / ANTIGRAVITY_CLIENT_SECRET (a gitignored local_bridge.py or
# ~/.hermes/.env is the usual home). Without them the in-memory refresh is skipped and a stale
# token asks you to reopen Antigravity, which mints a fresh one anyway.
_AG_CLIENT_ID_ENV = "ANTIGRAVITY_CLIENT_ID"
_AG_CLIENT_SECRET_ENV = "ANTIGRAVITY_CLIENT_SECRET"
_AG_QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
_AG_UA = "antigravity/2.8.0 linux/amd64"  # cloudcode-pa 403s an unknown User-Agent
_AG_TOKEN_FILE = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
# bucketId -> (window id, label)
_AG_BUCKETS = (
    ("gemini-5h", "5h", "Gemini 5h"),
    ("gemini-weekly", "week", "Gemini Week"),
)


def _ag_credential() -> Optional[dict]:
    import base64
    import subprocess

    blobs: list[str] = []
    try:
        done = subprocess.run(["secret-tool", "lookup", "service", "gemini", "username", "antigravity"],
                              capture_output=True, text=True, timeout=10, check=False)
        if done.returncode == 0 and done.stdout.strip():
            blobs.append(done.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        blobs.append(_AG_TOKEN_FILE.read_text().strip())
    except OSError:
        pass
    for blob in blobs:
        if blob.startswith("go-keyring-base64:"):
            try:
                blob = base64.b64decode(blob.split(":", 1)[1]).decode("utf-8")
            except Exception:
                continue
        try:
            data = json.loads(blob)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("token"), dict):
            return data["token"]
    return None


def parse_antigravity(payload: Any) -> list:
    """Windows from a retrieveUserQuotaSummary reply, matched by bucketId."""
    if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
        payload = payload["response"]
    found: dict[str, dict] = {}
    for group in (payload or {}).get("groups") or () if isinstance(payload, dict) else ():
        for bucket in (group or {}).get("buckets") or ():
            bid = bucket.get("bucketId") if isinstance(bucket, dict) else None
            if bid and bid not in found:
                found[bid] = bucket
    windows = []
    for bid, wid, label in _AG_BUCKETS:
        bucket = found.get(bid)
        if not bucket:
            continue
        reset = bucket.get("resetTime")
        try:
            reset_epoch = datetime.fromisoformat(str(reset).replace("Z", "+00:00")).timestamp() if reset else None
        except ValueError:
            reset_epoch = None
        frac = _f(bucket.get("remainingFraction"))
        if frac is None:
            used = 100.0 if reset_epoch else None  # reset time with no fraction = exhausted
        elif 0.0 <= frac <= 1.0:
            used = (1.0 - frac) * 100.0
        else:
            used = None
        windows.append(_window(wid, label, used, reset_epoch))
    return [w for w in windows if w]


def _ag_client() -> Optional[tuple[str, str]]:
    """Antigravity's public client id/secret from the environment, or None when unset."""
    client_id = (os.environ.get(_AG_CLIENT_ID_ENV) or "").strip()
    client_secret = (os.environ.get(_AG_CLIENT_SECRET_ENV) or "").strip()
    return (client_id, client_secret) if client_id and client_secret else None


def antigravity_quota() -> Optional[dict]:
    import httpx

    token = _ag_credential()
    if not token:
        return None
    access = str(token.get("access_token") or "").strip() or None
    refresh = str(token.get("refresh_token") or "").strip()
    client = _ag_client() if refresh else None
    if client:
        try:
            resp = httpx.post("https://oauth2.googleapis.com/token", timeout=15.0, data={
                "client_id": client[0], "client_secret": client[1],
                "refresh_token": refresh, "grant_type": "refresh_token"})
            if resp.status_code == 200:
                access = resp.json().get("access_token") or access
        except Exception:
            pass
    if not access:
        return None
    resp = httpx.post(_AG_QUOTA_URL, content=b"{}", timeout=15.0, follow_redirects=False,
                      headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json",
                               "Accept": "application/json", "User-Agent": _AG_UA})
    if resp.status_code != 200:
        raise BridgeHTTPError(resp.status_code, _retry_after(resp.headers.get("retry-after")))
    return _ok("antigravity", "Antigravity", parse_antigravity(resp.json()), source="cloudcode")


if __name__ == "__main__":  # manual check: hermes --run-module code < this file
    print(anthropic_quota())
    print(xai_quota())
    print(antigravity_quota())
    print(datetime.now(timezone.utc), time.time())
