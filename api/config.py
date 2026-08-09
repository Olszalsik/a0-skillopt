"""
SkillOpt - config read/update endpoint.

Route: GET /api/plugins/skillopt/config - returns the merged config
Route: POST /api/plugins/skillopt/config - body { ...overrides... }

Uses the framework's save_plugin_config with the 4-argument signature
to persist settings, then calls clear_plugin_cache + direct disk read
to bypass the framework's sticky config cache so the UI reflects the
new values immediately.

v1.1.0 changes:
- Removed `python_change=False` kwarg from clear_plugin_cache — v2.5
  of the framework removed that kwarg (the new signature is just
  `clear_plugin_cache(plugin_names)`). Without this fix, every POST
  to this endpoint 500s with `TypeError: got an unexpected keyword
  argument 'python_change'`.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from helpers.api import ApiHandler # type: ignore
from helpers import plugins as plugins_helper # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner # type: ignore


PLUGIN_NAME = "skillopt"
PLUGIN_DIR = Path("/a0/usr/plugins/skillopt")
CONFIG_PATH = PLUGIN_DIR / "config.json"


def _read_disk() -> dict:
    """Read the merged config directly from disk, bypassing the
    framework's get_plugin_config cache."""
    merged = dict(sleep_runner.default_config())
    try:
        if CONFIG_PATH.is_file():
            persisted = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(persisted, dict):
                for k, v in persisted.items():
                    if isinstance(merged.get(k), dict) and isinstance(v, dict):
                        inner = dict(merged[k])
                        inner.update(v)
                        merged[k] = inner
                    else:
                        merged[k] = v
    except Exception:
        pass
    return merged


class Config(ApiHandler):
    async def process(self, input_data, request): # type: ignore[no-untyped-def]
        method = (getattr(request, "method", "GET") or "GET").upper()

        if method == "POST":
            overrides = input_data or {}
            if not isinstance(overrides, dict):
                return {"ok": False, "error": "body must be a JSON object"}
            try:
                plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", dict(overrides))
            except Exception as e:
                return {"ok": False, "error": f"save failed: {e}", "timestamp": datetime.now(timezone.utc).isoformat()}
            # v1.1.0: clear_plugin_cache in v2.5 takes a single positional
            # arg (plugin_names). The old `python_change=False` kwarg was
            # removed; passing it raises TypeError and 500s this endpoint.
            try:
                plugins_helper.clear_plugin_cache([PLUGIN_NAME])
            except Exception:
                pass
            fresh = _read_disk()
            return {"ok": True, "updated": list(overrides.keys()), "config": fresh, "timestamp": datetime.now(timezone.utc).isoformat()}

        return {"ok": True, "config": _read_disk(), "timestamp": datetime.now(timezone.utc).isoformat()}
