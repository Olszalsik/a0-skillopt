"""v1.8.12: chat-model resolution for SkillOpt LLM calls.

The optimizer_model / target_model / judge_model config keys accept the
sentinel value 'chat' (the new default). At call time the sentinel resolves
to the active Agent Zero chat model - the same provider/model the main chat
UI uses (via the framework's _model_config preset plugin):

- active preset: <a0>/usr/plugins/_model_config/config.json (model_preset)
- chat slot:     matching entry in <a0>/usr/plugins/_model_config/presets.yaml
- api_base:      preset chat.api_base, else the provider entry in
                 <a0>/conf/model_providers.yaml (kwargs.api_base, then
                 models_list kwargs.api_base, then api_base)
- api_key:       env/dotenv API_KEY_<PROVIDER> / <PROVIDER>_API_KEY /
                 <PROVIDER>_API_TOKEN

A concrete model name bypasses resolution and keeps the legacy connection
behavior (env-file AZURE_OPENAI_ENDPOINT, ollama.com default).

SKILLOPT_A0_ROOT overrides the framework root (default /a0); the smoke
suite uses this to run hermetically. Results are cached for 60 seconds;
clear_cache() forces re-resolution. All lookups fail soft: they return
{'ok': False, 'error': ...} and never raise.
"""

import json
import os
import time

SENTINEL = "chat"
_TTL_S = 60.0
_CACHE = {"ts": 0.0, "val": None}


def _a0_root() -> str:
    return os.environ.get("SKILLOPT_A0_ROOT") or "/a0"


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_yaml(path: str):
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _dotenv_get(key: str) -> str:
    """Best-effort key lookup in the framework .env files."""
    up = key.upper()
    for suffix in ("/.env", "/usr/.env"):
        path = _a0_root() + suffix
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip().upper() == up:
                        val = v.strip().strip('"').strip("'")
                        if val:
                            return val
        except Exception:
            continue
    return ""


def _provider_api_base(provider: str, preset_base: str) -> str:
    if preset_base:
        return preset_base
    if not provider:
        return ""
    data = _load_yaml(_a0_root() + "/conf/model_providers.yaml")
    if not isinstance(data, dict):
        return ""

    def _find(obj):
        # v1.8.12: providers are nested under model-type keys
        # (chat/embedding) and the layout has shifted across framework
        # versions - find the provider subtree structurally instead of
        # assuming a fixed path.
        if isinstance(obj, dict):
            hit = obj.get(provider)
            if isinstance(hit, dict):
                return hit
            for v in obj.values():
                found = _find(v)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = _find(v)
                if found is not None:
                    return found
        return None

    entry = None
    chat_scope = data.get("chat")
    if isinstance(chat_scope, dict):
        entry = _find(chat_scope)
    if entry is None:
        entry = _find(data)
    if isinstance(entry, dict):
        kw = entry.get("kwargs")
        if isinstance(kw, dict) and kw.get("api_base"):
            return str(kw["api_base"])
        ml = entry.get("models_list")
        if isinstance(ml, dict):
            mkw = ml.get("kwargs")
            if isinstance(mkw, dict) and mkw.get("api_base"):
                return str(mkw["api_base"])
        if entry.get("api_base"):
            return str(entry["api_base"])
    return ""


def _provider_api_key(provider: str) -> str:
    if not provider:
        return ""
    up = provider.upper()
    for key in ("API_KEY_" + up, up + "_API_KEY", up + "_API_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
        dv = _dotenv_get(key)
        if dv:
            return dv
    return ""


def resolve_chat_model() -> dict:
    """Resolve the active chat model. Best-effort, never raises."""
    root = _a0_root()
    cfg = _read_json(root + "/usr/plugins/_model_config/config.json") or {}
    preset_name = str(cfg.get("model_preset") or "").strip()
    presets = _load_yaml(root + "/usr/plugins/_model_config/presets.yaml")
    preset = None
    if isinstance(presets, list):
        for p in presets:
            if isinstance(p, dict) and preset_name and str(p.get("name") or "").strip() == preset_name:
                preset = p
                break
        if preset is None and presets and isinstance(presets[0], dict):
            preset = presets[0]
    chat = preset.get("chat") if isinstance(preset, dict) else None
    if not isinstance(chat, dict):
        return {"ok": False, "error": "no active chat slot in _model_config presets (root: " + root + ")"}
    name = str(chat.get("name") or "").strip()
    provider = str(chat.get("provider") or "").strip()
    if not name:
        return {"ok": False, "error": "active preset chat slot has no model name"}
    return {
        "ok": True,
        "provider": provider,
        "model": name,
        "api_base": _provider_api_base(provider, str(chat.get("api_base") or "").strip()),
        "api_key": _provider_api_key(provider),
        "preset": preset_name or str((preset or {}).get("name") or ""),
    }


def get_chat_connection(force: bool = False) -> dict:
    """Cached resolution of the active chat model connection."""
    now = time.time()
    if not force and _CACHE["val"] is not None and (now - _CACHE["ts"]) < _TTL_S:
        return _CACHE["val"]
    try:
        val = resolve_chat_model()
    except Exception as exc:
        val = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)}
    _CACHE["ts"] = now
    _CACHE["val"] = val
    return val


def clear_cache() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["val"] = None


def is_sentinel(value) -> bool:
    return str(value or "").strip() == SENTINEL


def effective_model(model):
    """Resolve a requested model identifier for an LLM call.

    Returns (model_name, conn_or_None). Concrete names pass through with
    conn=None; the sentinel or an empty value resolves via the active chat
    model (conn carries provider api_base/api_key). Never raises; on failure
    returns ('', None).
    """
    m = str(model or "").strip()
    if m and m != SENTINEL:
        return m, None
    conn = get_chat_connection()
    if conn.get("ok") and conn.get("model"):
        return str(conn["model"]), conn
    return "", None
