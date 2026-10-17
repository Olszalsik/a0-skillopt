"""
SkillOpt - operating mode endpoint (v1.8.34).

Routes:
  POST /api/plugins/skillopt/mode
    body: {mode?, pause?, adopted_today?}          # reads
    body: {mode: "manual"|"autonomous"}            # sets the mode
    body: {pause: true|false}                      # kill switch

WHY THIS IS A ROUTE AND NOT A CONFIG FIELD ALONE
------------------------------------------------
`governance.resolve_evolution_mode()` is the single source of truth for
manual vs autonomous, but an operator needs three things the merged config
cannot give them:

  1. the RESOLVED answer, including which input won (a legacy `auto_adopt`
     vs the canonical `governance.evolution_mode`) - otherwise the UI shows
     "autonomous" with no explanation of where that came from;
  2. the kill-switch state, which is a MARKER FILE and not a config key;
  3. the safety-rail caps, so the UI can state them BEFORE the operator
     turns autonomy on rather than after.

This is a plain ApiHandler POST and inherits the framework's auth + CSRF
defaults. There is no exemption, and none may be added.

`adopted_today` is passed IN by the caller (the auto-loop state mirror) and
is deliberately not read from disk here: this route must not become a second
writer or a second reader of the single-writer auto-loop state file. See the
child DOX rule "State files are single-writer".
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _governance():
    try:
        from usr.plugins.skillopt.helpers import governance  # type: ignore
        return governance
    except Exception:
        try:
            from helpers import governance  # type: ignore
            return governance
        except Exception:
            return None


def _snapshot(cfg=None) -> dict:
    gov = _governance()
    if gov is None:
        return {"ok": False, "error": "governance unavailable",
                "timestamp": _now()}
    resolved = gov.resolve_evolution_mode(cfg)
    rails = gov.autonomous_config(cfg)
    return {
        "ok": True,
        "mode": resolved.get("mode"),
        "autonomous": bool(resolved.get("autonomous")),
        "source": resolved.get("source"),
        "raw": resolved.get("raw"),
        "killed": bool(resolved.get("killed")),
        "error": resolved.get("error") or "",
        "valid_modes": list(gov.EVOLUTION_MODES),
        "rails": rails,
        "max_per_day": rails.get("max_adoptions_per_day"),
        "max_per_skill_per_day": rails.get("max_adoptions_per_skill_per_day"),
        "require_post_adopt_check": rails.get("require_post_adopt_check"),
        "min_evidence_rollouts": rails.get("min_evidence_rollouts"),
        "audit": rails.get("audit"),
        "timestamp": _now(),
    }


class Mode(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        if not isinstance(data, dict):
            data = {}

        gov = _governance()
        if gov is None:
            return {"ok": False, "error": "governance unavailable",
                    "timestamp": _now()}

        # --- set: kill switch -------------------------------------------
        if "pause" in data and data.get("pause") is not None:
            # v1.8.50 (audit G2): ONE coercion dialect for this plugin. This
            # was `bool(data.get("pause"))`, and `bool("false") is True` - so a
            # caller asking to RESUME froze the loop instead, which is the F10
            # bug class this plugin has fixed twice already (the route and the
            # dashboard both had their own copy). The vetted dialect lives in
            # `api/config.py`, which accepts real booleans, 0/1 and the
            # unambiguous string spellings and REJECTS anything else rather
            # than guessing; a kill switch must never be guessed. A rejected
            # value is reported and nothing is written.
            try:
                from usr.plugins.skillopt.api import config as _cfg_api  # type: ignore
            except Exception:
                try:
                    from api import config as _cfg_api  # type: ignore
                except Exception:
                    _cfg_api = None  # type: ignore[assignment]
            _raw_pause = data.get("pause")
            if _cfg_api is not None:
                _ok_pause, paused = _cfg_api._coerce_like_default(
                    _raw_pause, False)
                if not _ok_pause:
                    out = _snapshot()
                    out["ok"] = False
                    out["error"] = (
                        f"pause {data.get('pause')!r} is not a boolean "
                        f"(accept true/false, 0/1, or the unambiguous "
                        f"strings); the kill switch was not touched")
                    return out
            else:
                # The config module is unreachable; fall back to a STRICT
                # check, never to truthiness.
                paused = _raw_pause if isinstance(_raw_pause, bool) else bool(
                    str(_raw_pause).strip().lower() in ("true", "1", "yes", "on"))
            res = gov.set_autonomous_pause(bool(paused))
            out = _snapshot()
            out["pause"] = res
            if not res.get("ok"):
                out["ok"] = False
                out["error"] = f"kill switch: {res.get('error')}"
            return out

        # --- set: mode --------------------------------------------------
        if "mode" in data and data.get("mode") is not None:
            requested = str(data.get("mode") or "").strip()
            valid = list(gov.EVOLUTION_MODES)
            if requested.lower() not in valid:
                # Refuse the write. Falling back to manual silently would
                # store a value the operator did not ask for; storing the
                # invalid value would make the resolver log on every tick.
                out = _snapshot()
                out["ok"] = False
                out["error"] = (
                    f"mode {requested!r} is not valid; expected one of "
                    f"{', '.join(valid)}"
                )
                return out
            try:
                from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
                helpers = sleep_runner
            except Exception:
                try:
                    from helpers import sleep_runner as helpers  # type: ignore
                except Exception:
                    helpers = None  # type: ignore[assignment]
            if helpers is None:
                return {"ok": False, "error": "config writer unavailable",
                        "timestamp": _now()}

            # Delegate the WRITE to api/config.py's already-tested
            # _read_disk + _clean_overrides path rather than persisting
            # `merged_config()` directly.
            #
            # That is not a style preference. `merged_config()` returns an
            # EMPTY dict (0 keys) whenever the defaults YAML is unreadable -
            # measured, not hypothetical - and `save_plugin_config` REPLACES
            # the `governance` section rather than deep-merging it. Writing
            # the merged blob would therefore, in exactly that condition,
            # persist `governance: {evolution_mode: ...}` alone and delete
            # `default_policy` plus every `autonomous_*` rail. A single UI
            # toggle wiping the settings page is the v1.8.2 incident class,
            # so this route sends only the keys it changes, built from the
            # same vetted helpers every other config write uses.
            try:
                try:
                    from usr.plugins.skillopt.api import config as config_api  # type: ignore
                except Exception:
                    from api import config as config_api  # type: ignore
            except Exception as e:
                return {"ok": False,
                        "error": f"config module unavailable: {e}",
                        "timestamp": _now()}

            wanted = str(requested).strip().lower()
            try:
                current = config_api._read_disk()
            except Exception:
                current = {}
            if not isinstance(current, dict) or "governance" not in current:
                # Refuse rather than write. Saving a section we could not
                # read in full would replace the good section with a partial
                # one - the same wipe, one level deeper.
                return {"ok": False,
                        "error": ("refusing to write the mode: the effective "
                                  "config could not be read in full, so the "
                                  "governance section cannot be preserved"),
                        "timestamp": _now()}
            clean, rejected = config_api._clean_overrides(
                {"governance": {"evolution_mode": wanted},
                 "auto_adopt": wanted == "autonomous"},
                current,
            )
            if not clean or "governance" not in clean:
                return {"ok": False,
                        "error": f"refusing to write: nothing survived "
                                  f"validation (rejected: {rejected})",
                        "timestamp": _now()}
            # v2026-10-13 (wipe post-mortem): `clean` holds only the keys this
            # route changes, and save_plugin_config REPLACES the file — a
            # mode toggle used to destroy every loop/privacy/replay setting
            # in config.json (caught live 2026-10-13). The write now carries
            # the persisted config forward and merges the two posted keys
            # over it.
            for_save = config_api._merge_for_save(config_api._disk_only(), clean)
            try:
                # Match api/config.py's form exactly. `import helpers.plugins`
                # requires `helpers.plugins` to already be an attribute of the
                # `helpers` package, whereas `from helpers import plugins`
                # triggers submodule resolution - so the former can fail on an
                # install where the latter works. This is a proven import in
                # this plugin; do not "simplify" it into the other one.
                from helpers import plugins as plugins_helper  # type: ignore
                plugins_helper.save_plugin_config("skillopt", "", "", for_save)
                plugins_helper.clear_plugin_cache(["skillopt"])
                # v1.8.43 (re-audit R3): clear_plugin_cache() drops the
                # FRAMEWORK's cache, not this plugin's 5-second memo of the
                # framework registry. Without this, `resolve_evolution_mode`
                # kept answering from the memo for up to the TTL - so the
                # response below reported the PRE-write mode, and the drain
                # could keep treating itself as autonomous for that long
                # AFTER the operator switched autonomy OFF.
                try:
                    from usr.plugins.skillopt.helpers import sleep_runner as _sr  # type: ignore
                except Exception:
                    try:
                        from helpers import sleep_runner as _sr  # type: ignore
                    except Exception:
                        _sr = None  # type: ignore[assignment]
                if _sr is not None:
                    try:
                        _sr.invalidate_framework_config_cache()
                    except Exception:
                        pass
            except Exception as e:
                return {"ok": False, "error": f"save failed: {e}",
                        "timestamp": _now()}
            # Snapshot the config we JUST WROTE, not a re-read. Two reasons:
            #   - correctness: the response would otherwise depend on
            #     merged_config() observing the write immediately, i.e. on
            #     clear_plugin_cache() having taken effect on the very next
            #     read. When it has not, the UI shows the OLD mode right
            #     after the operator toggled it.
            #   - determinism: it is the answer to the action just taken.
            # `{**for_save, **{...defaults context}}`: `_merge_for_save` over
            # the merged view is a faithful post-write picture because this
            # route's payload carries the complete governance section, while
            # every key the payload did not mention keeps its persisted value.
            post_write = config_api._merge_for_save(dict(current), clean)
            out = _snapshot(post_write)
            out["saved_mode"] = wanted
            out["updated"] = sorted(clean.keys())
            if rejected:
                out["rejected"] = rejected
            return out

        # --- read -------------------------------------------------------
        out = _snapshot()
        # Descriptive only, supplied by the caller from the auto-loop state
        # mirror. Never used for a gate here.
        if "adopted_today" in data:
            try:
                out["adopted_today"] = int(data.get("adopted_today") or 0)
            except (TypeError, ValueError):
                out["adopted_today"] = 0
        return out