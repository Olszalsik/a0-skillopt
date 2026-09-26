"""
SkillOpt - hub merge / indexing status endpoint.

Route: GET|POST /api/plugins/skillopt/hub_status

Exposes the hub-merge / catalog-indexing status that
scripts/check_hub_status.py (roadmap item 10) persists to
logs/hub_status.json as {latest: {...}, history: [...]}, with
latest.status in OPEN_PENDING / MERGED_INDEXED / MERGED_UNINDEXED
(or ERROR when the script itself failed a query - served as honest
data with HTTP 200).

Behavior:
- Fresh payload (file mtime age <= 3600s): returned as-is (HTTP 200),
  never triggering a refresh.
- Missing / unreadable / stale payload: exactly one refresh attempt -
  sys.executable scripts/check_hub_status.py under a hard 3s timeout,
  spawned as an asyncio subprocess so the server event loop is never
  parked on a network timeout. If another request is already
  refreshing (process-wide guard held), the caller polls file
  freshness instead of spawning a duplicate.
- The refresh counts as successful when it leaves a readable, fresh
  file - including exit-code-2 runs whose payload carries
  latest.status ERROR from the script itself.
- Refresh failed (spawn error, 3s timeout kill) or the file is still
  missing / stale afterwards: HTTP 500 with a structured
  {status: ERROR, message: ...} JSON body (no tracebacks to clients).

Auth/CSRF are relaxed for this read-only, non-sensitive public
PR / plugin-catalog status, mirroring the framework's own read-only
GET endpoints (api/api_log_get.py). Note on the requested handler
shape: Agent Zero API handlers implement
async def process(input, request) with the verb gated by
get_methods(); process IS the GET handler here - this framework has
no per-verb get() dispatch.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from helpers.api import ApiHandler  # type: ignore

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_LOG_FILE = _PLUGIN_DIR / "logs" / "hub_status.json"
_SCRIPT = _PLUGIN_DIR / "scripts" / "check_hub_status.py"

_STALENESS_S = 3600.0      # older than 3600 seconds per the unit spec
_REFRESH_TIMEOUT_S = 3.0   # hard cap on one watchdog run
_WAITER_TIMEOUT_S = 4.5    # max wait on a concurrent refresher (> 3s + reap)
_POLL_INTERVAL_S = 0.1

# Process-wide spawn guard: serializes watchdog spawns across Flask
# worker threads / event loops without ever blocking a loop - losers
# poll for freshness instead of holding the lock across an await.
_REFRESH_GUARD = threading.Lock()


def _read_payload() -> dict[str, Any] | None:
    """Parsed logs/hub_status.json, or None when missing / unreadable /
    not a dict carrying a latest entry."""
    try:
        data = json.loads(_LOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "latest" in data else None


def _is_stale() -> bool:
    """True when the file mtime is older than 3600s (or unreadable).
    The watchdog rewrites the whole file atomically on every run, so
    mtime equals the payload own timestamp."""
    try:
        return (time.time() - _LOG_FILE.stat().st_mtime) > _STALENESS_S
    except OSError:
        return True


async def _spawn() -> Any:
    """Seam for tests: launch the watchdog (stdout discarded; the script
    persists its payload to logs/hub_status.json itself)."""
    return await asyncio.create_subprocess_exec(
        sys.executable, str(_SCRIPT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _refresh() -> str | None:
    """Best-effort refresh; None on success (fresh readable file after),
    else a short client-safe reason string."""
    if not _SCRIPT.is_file():
        return "scripts/check_hub_status.py missing"
    if _REFRESH_GUARD.acquire(blocking=False):
        try:
            return await _run_refresh()
        finally:
            _REFRESH_GUARD.release()
    # Another request holds the guard: poll for freshness, never block.
    deadline = time.monotonic() + _WAITER_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(_POLL_INTERVAL_S)
        if _read_payload() is not None and not _is_stale():
            return None
    return "refresh in progress by another request; file still stale"


async def _run_refresh() -> str | None:
    """Guard held: skip when a concurrent refresh already won, else run
    the watchdog once under the hard timeout (kill on overrun)."""
    if _read_payload() is not None and not _is_stale():
        return None
    try:
        proc = await _spawn()
    except Exception as e:  # noqa: BLE001 - surfaced as a short reason
        return f"refresh spawn failed: {type(e).__name__}"
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REFRESH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:  # noqa: BLE001 - best-effort reaping
            pass
    if _read_payload() is not None and not _is_stale():
        return None
    return (
        f"refresh failed (rc={getattr(proc, 'returncode', None)}, "
        f"timeout={_REFRESH_TIMEOUT_S:.0f}s)"
    )


class HubStatus(ApiHandler):
    """GET the SkillOpt hub merge / indexing status payload."""

    @classmethod
    def get_methods(cls) -> list[str]:
        # v1.8.24: GET only. POST was advertised but never used, and it was the
        # reason auth and CSRF had to be switched off for the whole route.
        return ["GET"]

    # v1.8.24: auth and CSRF are restored to the framework defaults.
    #
    # This handler is not side-effect free. A stale or missing payload makes it
    # spawn `scripts/check_hub_status.py` as a subprocess, so an unauthenticated
    # caller could trigger process spawns on a network-reachable A0 instance,
    # rate-limited only by the 3600 s payload cache. The "read-only public
    # status payload" justification did not hold: nothing consumed this
    # anonymously (the WebUI dashboard never called it, and the background
    # watchdog reads the same file directly rather than the endpoint), so
    # relaxing the guards protected nothing and exposed the spawn path.
    #
    # Dropping POST removes the state-changing verb, so requiring CSRF is now
    # free: the framework derives requires_csrf() from requires_auth().

    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        try:
            reason: str | None = None
            payload = _read_payload()
            if payload is None or _is_stale():
                reason = await _refresh()
                payload = _read_payload()
            if payload is None:
                return self._error_response(
                    500, f"hub status unavailable: {reason or 'file unreadable'}"
                )
            if _is_stale():
                return self._error_response(
                    500, f"hub status stale (>3600s): {reason or 'refresh failed'}"
                )
            return payload
        except Exception as e:  # noqa: BLE001 - structured 500, no traceback to clients
            return self._error_response(
                500, f"hub_status handler error: {type(e).__name__}"
            )

    @staticmethod
    def _error_response(status: int, message: str) -> Any:
        """Structured JSON error; flask is imported lazily because the
        smoke harness / CI import this module without flask installed.
        Fallback: dict payload with an explicit http_status field so the
        error shape stays stable where flask is unavailable."""
        try:
            from flask import Response
        except ImportError:
            return {
                "status": "ERROR",
                "message": message,
                "http_status": status,
            }
        return Response(
            json.dumps({"status": "ERROR", "message": message}),
            status=status,
            mimetype="application/json",
        )
