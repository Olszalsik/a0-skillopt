"""
SkillOpt - monologue_start warning banner.

v1.1.0 — surfaces auto-loop failures to the user. The v1.0 loop
swallowed exceptions into auto_loop.log, which the dashboard never
read. The user saw a green "running: true, cycles_run: 0" forever.

This hook runs at the start of every agent turn and reads the
.last_auto_loop_last_error.json file. If there's a recent (within
the last hour) error, it appends a warning card to the banners list
so the user sees the failure on the next chat turn instead of
waiting for the WebUI to refresh.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLUGIN_NAME = "skillopt"
CARD_ID = "skillopt_loop_error_v1"
MAX_AGE_SEC = 3600  # 1 hour


def _last_error_path() -> Path | None:
    """Locate the persisted error file written by helpers/auto_loop.py."""
    candidates = [
        # Live plugin path
        Path("/a0/usr/plugins/skillopt/logs/runs/.auto_loop_last_error.json"),
        # Workdir path (when running outside the installed plugin)
        Path(__file__).resolve().parent.parent.parent.parent / "logs" / "runs" / ".auto_loop_last_error.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _read_error() -> dict[str, Any] | None:
    p = _last_error_path()
    if p is None:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def execute(*args, banners=None, **kwargs):  # type: ignore[no-untyped-def]
    """monologue_start hook. Adds a warning card to `banners` if the loop is broken."""
    err = _read_error()
    if not err:
        return
    try:
        ts = float(err.get("at_ts") or 0)
        if not ts:
            # Fallback: parse the at field (ISO 8601 with TZ offset).
            from datetime import datetime
            ts = datetime.fromisoformat(err["at"]).timestamp()
    except Exception:
        return
    if (time.time() - ts) > MAX_AGE_SEC:
        # Stale error from a transient hiccup; don't nag the user.
        return
    if banners is None:
        return
    for existing in banners or []:
        if isinstance(existing, dict) and existing.get("id") == CARD_ID:
            return
    banners.append({
        "id": CARD_ID,
        "type": "warning",
        "priority": 80,  # higher than the discovery card so the user sees it
        "title": "SkillOpt auto-loop is failing",
        "description": (
            f"Last error in `{err.get('where', '?')}`: "
            f"{err.get('type', '?')}: {err.get('message', '?')}\n\n"
            f"Open the SkillOpt plugin page to inspect the log."
        ),
        "icon": "alert-triangle",
        "cta_text": "Open Settings",
        "cta_action": "open-plugin-config:skillopt",
        "dismissible": True,
        "source": "backend",
    })
