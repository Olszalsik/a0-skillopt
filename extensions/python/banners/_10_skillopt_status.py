"""
SkillOpt - Welcome screen discovery banner.

Appends a discovery card to the Welcome screen so users learn about
the plugin from the first session. Card is shown only when the plugin
is active (toggle ON or default).

Follows the AGENTS.md banner/card contract:
- type: 'feature' (community plugin)
- priority: low (don't out-shout core system features)
- cta_action: 'open-plugin-config:skillopt'
- dismissible: True (so users can hide it once installed)
- source: 'backend' (v2.5 extension contract requirement)

v1.1.0 changes:
- Added `source: "backend"` field required by v2.5 banner contract.
- Switched to subclass-style Extension dispatcher for forward compat.
"""

from __future__ import annotations

import logging

try:
    from helpers.extension import Extension  # type: ignore
    _BASE = Extension
except Exception:  # pragma: no cover - legacy fallback for pre-v2.5
    _BASE = object

try:
    from helpers import plugins as plugins_helper  # type: ignore
except Exception:  # pragma: no cover
    plugins_helper = None  # type: ignore


log = logging.getLogger(__name__)

PLUGIN_NAME = "skillopt"
CARD_ID = "skillopt_discovery_v1"


def _is_active() -> bool:
    """Return True if the plugin is toggled ON or default (no toggle file)."""
    if plugins_helper is None:
        return True
    try:
        cfg = plugins_helper.get_plugin_config(PLUGIN_NAME) or {}
    except Exception:
        cfg = {}
    if isinstance(cfg, dict) and cfg.get("enabled") is False:
        return False
    return True


def execute(banners: list, **kwargs):  # type: ignore[no-untyped-def]
    """Append a discovery card to the banners list if the plugin is active.

    v2.5 keeps the legacy module-level `execute(banners, **kwargs)`
    dispatch path; the Extension subclass is the v2.6+ idiom. We
    support both so this file works on either runtime.
    """
    if not _is_active():
        return

    for existing in banners or []:
        if isinstance(existing, dict) and existing.get("id") == CARD_ID:
            return

    banners.append({
        "id": CARD_ID,
        "type": "feature",
        "priority": 50,
        "title": "SkillOpt Self-Evolution",
        "description": (
            "Your agent skills can train themselves overnight. "
            "SkillOpt harvests completed tasks, proposes improvements "
            "to your skill documents, and only adopts changes that "
            "strictly beat the current version on a held-out gate."
        ),
        "icon": "dna",
        "cta_text": "Open Settings",
        "cta_action": "open-plugin-config:skillopt",
        "dismissible": True,
        "source": "backend",
    })
