"""
SkillOpt plugin - config read/update endpoint.

Route: GET /api/plugins/skillopt/config - returns the merged config
Route: POST /api/plugins/skillopt/config - body { ...overrides... }

Uses the framework's save_plugin_config with the 4-argument signature
to persist settings, then calls clear_plugin_cache + direct disk read
to bypass the framework's sticky config cache so the UI reflects the
new values immediately.

v1.8.1 changes:
- Removed `python_change=False` kwarg from clear_plugin_cache — v2.5
  of the framework removed that kwarg (the new signature is just
  `clear_plugin_cache(plugin_names)`). Without this fix, every POST
  to this endpoint 500s with `TypeError: got an unexpected keyword
  argument 'python_change'`.

v1.8.18 (P0, 2026-09-22 autonomy audit RC1/RC8):
- GET is now an accepted method. The WebUI config page's refresh()
  previously had no way to READ without saving: api() defaults to
  POST, and this handler treated every POST as a save - so opening
  the page saved `{}` over config.json (the auto-adopt toggle bug).
- An empty POST body is now a READ, never a save.
- POST bodies are whitelisted against the merged defaults, so no
  client can persist arbitrary keys (and an empty/unknown payload
  can never wipe the file again). Unknown keys are reported in
  `ignored` instead of being silently dropped.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from helpers.api import ApiHandler # type: ignore
from helpers import plugins as plugins_helper # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner # type: ignore


PLUGIN_NAME = "skillopt"
# v1.8.1 fix: the hardcoded Linux path Path("/a0/usr/plugins/skillopt") never
# exists on Windows, so config.json was silently ignored there. Resolve from
# the module's own location instead.
PLUGIN_DIR = Path(__file__).resolve().parent.parent
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
    @classmethod
    def get_methods(cls) -> list[str]:
        # v1.8.18: GET for the WebUI config page's read; POST remains the save.
        return ["GET", "POST"]

    async def process(self, input_data, request): # type: ignore[no-untyped-def]
        method = (getattr(request, "method", "GET") or "GET").upper()

        if method == "POST":
            overrides = input_data or {}
            if not isinstance(overrides, dict):
                return {"ok": False, "error": "body must be a JSON object"}
            # v1.8.18 (P0, audit RC1): an empty POST is a READ, never a save.
            # The v1.8.17 code saved {} here, so any client POSTing without a
            # body (the config page's refresh() did exactly that on every
            # open) wiped config.json - which also silenced the auto-loop
            # thread, since the framework registry returns {} for an empty
            # config.json and the loop spun on the empty dict forever.
            if not overrides:
                return {
                    "ok": True,
                    "config": _read_disk(),
                    "updated": [],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            # v1.8.18: whitelist persisted keys against the merged defaults so
            # a malformed/foreign body can never replace the file wholesale.
            merged = _read_disk()
            unknown = [k for k in overrides if k not in merged]
            clean: dict = {}
            for k, v in overrides.items():
                if k not in merged:
                    continue
                if isinstance(merged.get(k), dict) and isinstance(v, dict):
                    inner = dict(merged[k])
                    inner.update(v)
                    clean[k] = inner
                else:
                    clean[k] = v
            if not clean:
                return {
                    "ok": True,
                    "config": merged,
                    "updated": [],
                    "ignored": unknown,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            try:
                plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", dict(clean))
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
            return {"ok": True, "updated": list(clean.keys()), "ignored": unknown, "config": fresh, "timestamp": datetime.now(timezone.utc).isoformat()}

        return {"ok": True, "config": _read_disk(), "timestamp": datetime.now(timezone.utc).isoformat()}