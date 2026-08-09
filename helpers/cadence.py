"""Per-skill cadence (Day-4 item 5).

The auto-loop used to run every 30 min for every skill. Per-skill cadence means:
skills that are hot (lots of new rollouts) get more cycles, skills that are
cold get fewer. This is a small, stdlib-only helper.

Public API (all pure functions, no classes):
- compute_next_run(new_rollouts, cadence_target, floor_s, ceiling_s) -> int
- load_per_skill_state(skill_name) -> dict
- save_per_skill_state(skill_name, state) -> None
- list_skills_with_state() -> list[str]
- count_new_rollouts(skill_name, since_ts) -> int
- _state_dir() -> Path

Backwards-compat: if the v1.1.0 single `logs/runs/.auto_loop_state.json`
exists AND skill_name == "_default", migrate it (read + write to new path +
delete old). One-shot migration.

ROADMAP engineering principles honored:
1. Preserves the gate (this is operational, not a gate change).
2. Make failure modes loud (corrupt state -> defaults + warning, never crash).
3. Plugin-local only (state files under <plugin>/logs/runs/).
4. Cycle log is the source of truth (every skip/run lands in cadence.log
   via auto_loop.py, not here).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Defaults match default_config.yaml
DEFAULT_TARGET = 20
DEFAULT_FLOOR_S = 60
DEFAULT_CEILING_S = 3600

# The v1.1.0 single-state-file path (backwards-compat)
_LEGACY_STATE_PATH = ".auto_loop_state.json"
_NEW_STATE_PREFIX = "auto_loop_state_"
_NEW_STATE_SUFFIX = ".json"


def _plugin_root() -> Path:
    """Locate the plugin root (where plugin.yaml lives)."""
    here = Path(__file__).resolve().parent
    for p in [here, *here.parents]:
        if (p / "plugin.yaml").is_file():
            return p
    return here.parent


def _state_dir() -> Path:
    """Where per-skill state files live."""
    d = _plugin_root() / "logs" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(skill_name: str) -> Path:
    safe = (skill_name or "_default").strip().lower().replace("/", "_")
    return _state_dir() / f"{_NEW_STATE_PREFIX}{safe}{_NEW_STATE_SUFFIX}"


def _default_state() -> dict[str, Any]:
    return {
        "last_run_at": 0.0,
        "new_rollouts_since_last_run": 0,
        "total_cycles": 0,
        "total_llm_calls": 0,
        "daily_spend_cents": 0,
        "daily_spend_reset_at": 0.0,
    }


# ----------------------------------------------------------------------- #
# Cadence computation
# ----------------------------------------------------------------------- #

def compute_next_run(
    new_rollouts: int,
    cadence_target: int = DEFAULT_TARGET,
    floor_s: int = DEFAULT_FLOOR_S,
    ceiling_s: int = DEFAULT_CEILING_S,
) -> int:
    """Seconds until the next cycle for this skill.

    - Hot (new_rollouts >= cadence_target) -> floor_s (min).
    - Cold (new_rollouts == 0)             -> ceiling_s (max).
    - Linear interpolation in between.
    - Clamped to [floor_s, ceiling_s] for safety.
    """
    try:
        target = max(1, int(cadence_target))
    except (TypeError, ValueError):
        target = DEFAULT_TARGET
    try:
        n = max(0, int(new_rollouts))
    except (TypeError, ValueError):
        n = 0
    if n >= target:
        return int(floor_s)
    if n <= 0:
        return int(ceiling_s)
    ratio = 1.0 - (n / target)  # 0.0 at hot, 1.0 at cold
    delta = (ceiling_s - floor_s) * ratio
    out = int(round(floor_s + delta))
    return max(int(floor_s), min(int(ceiling_s), out))


# ----------------------------------------------------------------------- #
# Per-skill state I/O (atomic, backwards-compat)
# ----------------------------------------------------------------------- #

def load_per_skill_state(skill_name: str) -> dict[str, Any]:
    """Read state for a skill, or return defaults.

    If the v1.1.0 legacy single-state file exists AND skill_name == "_default",
    migrate it on first read.
    """
    state = _default_state()
    # Backwards-compat migration (one-shot)
    if (skill_name or "").strip().lower() == "_default":
        legacy = _state_dir() / _LEGACY_STATE_PATH
        if legacy.is_file():
            try:
                with legacy.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    state.update({k: loaded.get(k, v) for k, v in state.items()})
                # Write to new path, then remove legacy
                save_per_skill_state("_default", state)
                try:
                    legacy.unlink()
                except OSError:
                    pass
            except (OSError, json.JSONDecodeError):
                # Corrupt legacy file: ignore, fall through to defaults
                pass
    # New path
    p = _state_path(skill_name)
    if p.is_file():
        try:
            with p.open("r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                for k, v in state.items():
                    state[k] = loaded.get(k, v)
        except (OSError, json.JSONDecodeError):
            # Corrupt state file: log warning via stderr, return defaults.
            # We deliberately do NOT raise — auto_loop must not crash on bad state.
            try:
                import sys
                print(
                    f"[skillopt.cadence] WARN: corrupt state for {skill_name!r} "
                    f"at {p}, falling back to defaults",
                    file=sys.stderr,
                )
            except Exception:
                pass
    return state


def save_per_skill_state(skill_name: str, state: dict[str, Any]) -> None:
    """Atomic write: write to <tmp>.json then os.replace() to final path."""
    p = _state_path(skill_name)
    # Coerce to the documented shape (fill missing keys with defaults)
    out = _default_state()
    if isinstance(state, dict):
        for k, v in out.items():
            out[k] = state.get(k, v)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(out, fp, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except OSError:
        # Best-effort: try to clean up the temp file
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass


def list_skills_with_state() -> list[str]:
    """All skills that have a state file on disk."""
    out: list[str] = []
    for p in _state_dir().glob(f"{_NEW_STATE_PREFIX}*{_NEW_STATE_SUFFIX}"):
        name = p.name[len(_NEW_STATE_PREFIX):-len(_NEW_STATE_SUFFIX)]
        if name:
            out.append(name)
    return sorted(out)


# ----------------------------------------------------------------------- #
# Rollout counting (cheap heuristic for cadence)
# ----------------------------------------------------------------------- #

def _rollouts_dir() -> Path:
    d = _plugin_root() / "logs" / "rollouts"
    if not d.is_dir():
        return d
    return d


def count_new_rollouts(skill_name: str, since_ts: float) -> int:
    """Count rollout JSONs whose mtime > since_ts AND whose skill_hint == skill_name.

    Exact attribution isn't needed for cadence — this is a cheap heuristic.
    """
    rd = _rollouts_dir()
    if not rd.is_dir():
        return 0
    needle = (skill_name or "").strip().lower()
    if not needle:
        return 0
    n = 0
    try:
        for f in rd.glob("*.json"):
            try:
                if f.stat().st_mtime <= since_ts:
                    continue
            except OSError:
                continue
            try:
                with f.open("r", encoding="utf-8") as fp:
                    rec = json.load(fp)
            except (OSError, json.JSONDecodeError):
                continue
            hint = str((rec or {}).get("skill_hint") or "").strip().lower()
            if hint == needle:
                n += 1
    except OSError:
        return n
    return n


def reset_for_tests() -> None:
    """Clear all state files (test helper)."""
    sd = _state_dir()
    for p in sd.glob(f"{_NEW_STATE_PREFIX}*{_NEW_STATE_SUFFIX}"):
        try:
            p.unlink()
        except OSError:
            pass
