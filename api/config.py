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

v1.8.34 changes:
- Client values are COERCED to the type of their default. Previously the
  value was persisted verbatim (`clean[k] = v`) and _read_disk() applied no
  validation, so `{"governance": {"autonomous_require_post_adopt_check":
  "yes"}}` was stored as the string "yes" and reached every consumer
  unvalidated. A value that cannot be coerced is reported in `rejected` and
  SKIPPED - it never aborts the save, because the rest of the payload is
  still worth writing.
- Nested dicts are merged one level deep with per-leaf coercion, so posting
  a partial `governance` object updates the keys it names and leaves every
  sibling intact. Deep-merge is deliberately not attempted.
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


def _disk_only() -> dict:
    """The raw persisted config.json, WITHOUT the YAML defaults merged in.

    Used before every save: save_plugin_config REPLACES the file, so each
    write must be built on top of what is already persisted, never on the
    posted payload alone.

    v2026-10-13 (wipe post-mortem): `_clean_overrides()` returns only the
    keys the POST carried, and both `/config` and `/mode` saved exactly
    that. Caught live twice on 2026-10-13: a `/mode` toggle reduced
    config.json to `{"governance": {...}, "auto_adopt": false}` (destroying
    `auto_loop_*`, `backend`, `privacy_*`, `replay_real_*`), and minutes
    later the settings page's 12-key Save wiped `governance` + `auto_adopt`
    — silently flipping the install back to manual mode. Every write is
    now merged over the persisted file.
    """
    try:
        if CONFIG_PATH.is_file():
            persisted = json.loads(CONFIG_PATH.read_text(encoding="utf-8") or "{}")
            if isinstance(persisted, dict):
                return dict(persisted)
    except Exception:
        pass
    return {}


def _merge_for_save(base: dict, clean: dict) -> dict:
    """One-level merge of `clean` over `base` for a save.

    Same nesting rule as `_read_disk()`: a dict value merges one level so a
    partial section keeps its persisted siblings; a scalar replaces. The
    output is what the file will contain, so keys the payload did not
    mention can never disappear.
    """
    out = dict(base) if isinstance(base, dict) else {}
    for k, v in clean.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            inner = dict(out[k])
            inner.update(v)
            out[k] = inner
        else:
            out[k] = v
    return out


def _coerce_like_default(value, default):
    """Coerce a client value to the type of its default. Returns (ok, value).

    v1.8.34: api/config.py persisted the client's value VERBATIM
    (`clean[k] = v`), and _read_disk() applies no validation either. So
    `{"governance": {"autonomous_require_post_adopt_check": "yes"}}` landed
    in config.json as the string "yes" and reached every consumer
    unvalidated.

    A value that cannot be coerced is REJECTED, never aborting the save -
    the surrounding write is still worth doing, and the caller reports the
    rejected key. This mirrors initialize._args_override()'s contract: log
    and skip, do not abort.

    Booleans accept the string spellings a hand-edited config.json or an
    older build could contain; they do NOT accept arbitrary truthiness, so
    the string "false" cannot become True.
    """
    if isinstance(default, bool):
        if isinstance(value, bool):
            return True, value
        if isinstance(value, (int, float)) and value in (0, 1):
            return True, bool(value)
        if isinstance(value, str):
            s = value.strip().lower()
            if s in ("true", "yes", "on", "1"):
                return True, True
            if s in ("false", "no", "off", "0"):
                return True, False
        return False, default
    if isinstance(default, int) and not isinstance(default, bool):
        if isinstance(value, bool):
            return False, default
        try:
            return True, int(value)
        except (TypeError, ValueError):
            return False, default
    if isinstance(default, str):
        if isinstance(value, str):
            return True, value
        return False, default
    # list / dict / anything else: accept the exact type only.
    if type(value) is type(default):
        return True, value
    return False, default


def _clean_overrides(overrides: dict, merged: dict) -> tuple[dict, list]:
    """Whitelist + coerce overrides against the merged defaults.

    Returns `(clean, rejected)`. Top-level keys are whitelisted against
    `merged`; a nested dict value is merged ONE level deep with per-leaf
    coercion, so posting `{"governance": {"evolution_mode": "autonomous"}}`
    updates that key and leaves every sibling intact. Deep-merge is
    deliberately not attempted: a partial `governance` payload must not be
    able to drop the siblings it does not mention.
    """
    clean: dict = {}
    rejected: list[str] = []
    for k, v in overrides.items():
        if k not in merged:
            continue
        default = merged.get(k)
        if isinstance(default, dict) and isinstance(v, dict):
            inner = dict(default)
            for sub, subval in v.items():
                if sub not in default:
                    rejected.append(f"{k}.{sub}")
                    continue
                ok, coerced = _coerce_like_default(subval, default.get(sub))
                if ok:
                    inner[sub] = coerced
                else:
                    rejected.append(f"{k}.{sub}")
            clean[k] = inner
        else:
            ok, coerced = _coerce_like_default(v, default)
            if ok:
                clean[k] = coerced
            else:
                rejected.append(str(k))
    return clean, rejected


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
            clean, rejected = _clean_overrides(overrides, merged)
            if not clean:
                return {
                    "ok": True,
                    "config": merged,
                    "updated": [],
                    "ignored": unknown,
                    "rejected": rejected,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            # v2026-10-13 (wipe post-mortem): a save must CARRY FORWARD the
            # persisted keys the payload did not mention. `clean` holds only
            # the posted keys and save_plugin_config replaces the file, so
            # writing `clean` alone destroyed every other persisted setting —
            # including the autonomy switch, which is how an operator could
            # enable autonomous mode on one day and find it silently back on
            # manual after an unrelated save.
            for_save = _merge_for_save(_disk_only(), clean)
            try:
                plugins_helper.save_plugin_config(PLUGIN_NAME, "", "", for_save)
            except Exception as e:
                return {"ok": False, "error": f"save failed: {e}", "timestamp": datetime.now(timezone.utc).isoformat()}
            # v1.1.0: clear_plugin_cache in v2.5 takes a single positional
            # arg (plugin_names). The old `python_change=False` kwarg was
            # removed; passing it raises TypeError and 500s this endpoint.
            try:
                plugins_helper.clear_plugin_cache([PLUGIN_NAME])
            except Exception:
                pass
            # v1.8.43 (re-audit R3): ALSO drop this plugin's own 5-second
            # memo of the framework registry. `clear_plugin_cache` clears the
            # framework's cache, not ours, so without this a POST that changed
            # the operating mode (or any governance rail) would keep resolving
            # the OLD value for up to the TTL - including in the drain, which
            # reads the mode to decide whether it may write a live skill
            # unattended. Every config write in this plugin must invalidate
            # both caches.
            try:
                sleep_runner.invalidate_framework_config_cache()
            except Exception:
                pass
            fresh = _read_disk()
            return {"ok": True, "updated": list(clean.keys()), "ignored": unknown,
                    "rejected": rejected, "config": fresh,
                    "timestamp": datetime.now(timezone.utc).isoformat()}

        return {"ok": True, "config": _read_disk(), "timestamp": datetime.now(timezone.utc).isoformat()}