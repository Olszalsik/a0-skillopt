"""Per-skill budget tracking (Day-4 item 5).

The auto-loop used to spend unbounded on LLM calls. Per-skill budget means:
a hard cap on LLM spend per skill per day (default $1 = 100 cents).

Public API:
- class BudgetTracker(skill_name, daily_cap_cents, reset_hour_utc, cost_per_call_cents, state_dir)
    - record_spend(cents, ts=None) -> dict
    - can_spend(cents) -> tuple[bool, str]
    - get_status() -> dict
- estimate_call_cost() -> int   (module-level helper, reads cost_per_call_cents)

Pure stdlib (json, os, time, pathlib). No external deps.

ROADMAP engineering principles honored:
1. Preserves the gate (operational, not a gate change).
2. Make failure modes loud (corrupt state -> defaults + warning, never crash).
3. Plugin-local only (state files under <plugin>/logs/runs/).
4. Cycle log is the source of truth (every decision lands in cadence.log
   via auto_loop.py, not here).
"""

from __future__ import annotations

import calendar
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_DAILY_CAP_CENTS = 100
DEFAULT_COST_PER_CALL_CENTS = 1
DEFAULT_RESET_HOUR_UTC = 0
DEFAULT_SOFT_WARN_PCT = 80  # v1.8.7 soft tier (percent of cap; 0 = off)

_NEW_PREFIX = "budget_"
_NEW_SUFFIX = ".json"


def _plugin_root() -> Path:
    """Locate the plugin root (where plugin.yaml lives)."""
    here = Path(__file__).resolve().parent
    for p in [here, *here.parents]:
        if (p / "plugin.yaml").is_file():
            return p
    return here.parent


def _default_state_dir() -> Path:
    d = _plugin_root() / "logs" / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def estimate_call_cost(cost_per_call_cents: int | None = None) -> int:
    """Return the cost per LLM call in cents.

    The default is 1 cent. Callers can override via the kwarg or by setting
    the module constant `DEFAULT_COST_PER_CALL_CENTS`.
    """
    if cost_per_call_cents is not None:
        try:
            return max(0, int(cost_per_call_cents))
        except (TypeError, ValueError):
            return DEFAULT_COST_PER_CALL_CENTS
    return DEFAULT_COST_PER_CALL_CENTS


def budget_section(cfg: dict | None) -> dict:
    """The `budget:` block from a merged config, as a dict. Never raises.

    v1.8.37 (audit F13). The `budget:` YAML section can arrive as a STRING when
    the nested section is mangled by `merged_config()`'s flat light-parse (the
    bare `budget:` key stored as ""), so `.get(...)` raised
    "'str' object has no attribute 'get'" on every cycle. The v1.8.19 fix
    guarded that at ONE call site; there are nine, so the guard belongs here,
    once.
    """
    try:
        sec = (cfg or {}).get("budget")
        return sec if isinstance(sec, dict) else {}
    except Exception:
        return {}


def budget_enabled(cfg: dict | None) -> bool:
    """Is the daily budget gate switched on? Default ON.

    `budget.enabled: false` was declared in default_config.yaml since v1.6.0
    and read by NOTHING (audit F13), so an operator who switched the budget
    off still had it enforced.
    """
    sec = budget_section(cfg)
    try:
        return bool(sec.get("enabled", True))
    except Exception:
        return True


def tracker_from_config(skill_name: str | None,
                        cfg: dict[str, Any] | None = None,
                        *,
                        default_when_disabled: bool = False
                        ) -> "BudgetTracker | None":
    """Build a BudgetTracker wired from the `budget:` block.

    v1.8.37 (audit F13). Nine call sites constructed `BudgetTracker(...)` with
    different ad-hoc subsets of the config, which is exactly why
    `daily_cap_cents` and `reset_hour_utc` were declared but never supplied:
    the effective cap was always the module default. One factory means the
    settings cannot drift per call site again.

    `daily_cap_cents: 0` keeps its documented meaning - "no cap" - so an
    operator can keep the tracker for its soft warnings with the hard cap off.

    v1.8.45 (re-audit R10): `default_when_disabled=True` returns a
    default-configured tracker instead of `None` when `budget.enabled` is
    false. Three callers need a tracker purely to READ status (or to record
    spend while the gate is off) and were constructing one directly, which
    left "only the factory constructs a tracker" true in spirit and false in
    fact. With this flag, those call sites go through the factory too and the
    disabled case is explicit at the one place that decides it.
    """
    if not budget_enabled(cfg) and not default_when_disabled:
        return None
    sec = budget_section(cfg)
    kwargs: dict[str, Any] = {}
    for key in ("daily_cap_cents", "reset_hour_utc", "cost_per_call_cents",
                "soft_warn_pct"):
        if key not in sec or sec.get(key) is None:
            continue
        try:
            kwargs[key] = int(sec[key])
        except (TypeError, ValueError):
            continue  # a malformed value falls back to the module default
    try:
        return BudgetTracker(skill_name=skill_name, **kwargs)
    except Exception:
        return BudgetTracker(skill_name=skill_name)


class BudgetTracker:
    """Per-skill (or global) LLM spend tracker.

    State file: <state_dir>/budget_<skill_name>.json (or budget_global.json
    if skill_name is None).
    """

    def __init__(
        self,
        skill_name: str | None = None,
        daily_cap_cents: int = DEFAULT_DAILY_CAP_CENTS,
        reset_hour_utc: int = DEFAULT_RESET_HOUR_UTC,
        cost_per_call_cents: int = DEFAULT_COST_PER_CALL_CENTS,
        soft_warn_pct: int = DEFAULT_SOFT_WARN_PCT,
        state_dir: str | Path | None = None,
    ) -> None:
        self.skill_name = (skill_name.strip().lower() if skill_name else None) or None
        self.daily_cap_cents = max(0, int(daily_cap_cents))
        self.reset_hour_utc = int(reset_hour_utc) % 24
        self.cost_per_call_cents = max(0, int(cost_per_call_cents))
        self.soft_warn_pct = max(0, int(soft_warn_pct))
        self._state_dir = Path(state_dir) if state_dir else _default_state_dir()
        self._state_dir.mkdir(parents=True, exist_ok=True)
        # In-process cache; loaded on first access
        self._cache: dict[str, Any] | None = None
        self._blocked_calls = 0

    # ------------------------------------------------------------------ #
    # State I/O
    # ------------------------------------------------------------------ #

    def _state_path(self) -> Path:
        name = self.skill_name if self.skill_name else "global"
        safe = name.replace("/", "_")
        return self._state_dir / f"{_NEW_PREFIX}{safe}{_NEW_SUFFIX}"

    def _default_state(self) -> dict[str, Any]:
        return {
            "daily_total_cents": 0,
            "reset_at": self._next_reset_after(time.time()),
            "blocked_calls": 0,
            "soft_warned": False,
        }

    def _load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        state = self._default_state()
        p = self._state_path()
        if p.is_file():
            try:
                with p.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    for k, v in state.items():
                        state[k] = loaded.get(k, v)
            except (OSError, json.JSONDecodeError):
                try:
                    import sys
                    print(
                        f"[skillopt.budget] WARN: corrupt state for "
                        f"{self.skill_name or 'global'!r} at {p}, "
                        f"falling back to defaults",
                        file=sys.stderr,
                    )
                except Exception:
                    pass
        self._cache = state
        return state

    def _save(self) -> None:
        state = self._load()
        p = self._state_path()
        tmp = p.with_suffix(p.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fp:
                json.dump(state, fp, indent=2, sort_keys=True)
            os.replace(tmp, p)
        except OSError:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # Day rollover logic
    # ------------------------------------------------------------------ #

    def _next_reset_after(self, ts: float) -> float:
        """Return the next daily reset boundary (UTC seconds) after `ts`.

        v1.8.1 fix: the UTC struct from ``time.gmtime`` was fed into
        ``time.mktime`` (which interprets it as LOCAL time), so the daily
        reset drifted by the machine's UTC offset. ``calendar.timegm``
        interprets the struct as UTC, matching the ``*_utc`` config key.
        """
        try:
            t = time.gmtime(ts)
        except Exception:
            t = time.gmtime()
        # Today's reset at reset_hour_utc
        today_reset = calendar.timegm(
            (t.tm_year, t.tm_mon, t.tm_mday, self.reset_hour_utc, 0, 0, 0, 0, 0)
        )
        if today_reset > ts:
            return float(today_reset)
        # Tomorrow's reset
        tomorrow = time.gmtime(today_reset + 86400)
        return float(calendar.timegm(
            (tomorrow.tm_year, tomorrow.tm_mon, tomorrow.tm_mday,
             self.reset_hour_utc, 0, 0, 0, 0, 0)
        ))

    def _maybe_rollover(self, ts: float) -> None:
        state = self._load()
        reset_at = float(state.get("reset_at") or 0.0)
        # If `ts` is past the stored reset boundary, rollover to a new day.
        if ts >= reset_at and reset_at > 0:
            state["daily_total_cents"] = 0
            state["blocked_calls"] = 0
            state["soft_warned"] = False
            state["reset_at"] = self._next_reset_after(ts)
            self._save()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def can_spend(self, cents: int) -> tuple[bool, str]:
        """Return (True, "") if `daily_total + cents <= cap`, else (False, reason).

        v1.8.35 (audit F7): this now rolls the day over first. It previously
        did not, and `_maybe_rollover` was reachable only from
        `record_spend` - which itself only runs AFTER a successful cycle,
        which requires `can_spend` to pass. So once the persisted total hit
        the cap the chain deadlocked permanently: the skill reported
        "daily cap exceeded" on every tick, forever, with a misleading reason
        and no way for the counter to reset.

        `_maybe_rollover` already writes the reset through `_save`, so calling
        it here is the same operation `record_spend` performs - just at the
        read side too.
        """
        try:
            c = max(0, int(cents))
        except (TypeError, ValueError):
            c = 0
        if self.daily_cap_cents <= 0:
            # No cap configured - allow anything
            return (True, "")
        try:
            self._maybe_rollover(time.time())
        except Exception:
            # A rollover we cannot perform must not silently unblock spend
            # beyond the cap: fall through to the (possibly stale) total,
            # which is the restrictive direction.
            pass
        state = self._load()
        total = int(state.get("daily_total_cents") or 0)
        if (total + c) <= self.daily_cap_cents:
            return (True, "")
        return (
            False,
            f"daily cap {self.daily_cap_cents}c would be exceeded "
            f"(current: {total}c, requested: {c}c)",
        )

    def record_spend(self, cents: int, ts: float | None = None) -> dict[str, Any]:
        """Record a spend of `cents`. Rolls over the daily counter if needed."""
        try:
            c = max(0, int(cents))
        except (TypeError, ValueError):
            c = 0
        now = float(ts) if ts is not None else time.time()
        self._maybe_rollover(now)
        state = self._load()
        state["daily_total_cents"] = int(state.get("daily_total_cents") or 0) + c
        # v1.8.7 soft tier: one warning per day when the threshold is crossed.
        soft_warning = False
        if (self.daily_cap_cents > 0 and self.soft_warn_pct > 0
                and not state.get("soft_warned")):
            _th = self.daily_cap_cents * self.soft_warn_pct // 100
            if _th > 0 and int(state["daily_total_cents"]) >= _th:
                state["soft_warned"] = True
                soft_warning = True
        self._save()
        return {
            "recorded": c,
            "new_total": int(state["daily_total_cents"]),
            "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
            "soft_warning": soft_warning,
        }

    def get_status(self) -> dict[str, Any]:
        # v1.8.35 (audit F7): same rollover as can_spend. A status read is
        # what the dashboard shows as "daily cap", so reporting a stale total
        # here is what made the permanent latch look like a live reading.
        try:
            self._maybe_rollover(time.time())
        except Exception:
            pass
        state = self._load()
        total = int(state.get("daily_total_cents") or 0)
        cap = self.daily_cap_cents
        cap_pct = (total / cap * 100.0) if cap > 0 else 0.0
        soft_threshold = (cap * self.soft_warn_pct // 100) if (cap > 0 and self.soft_warn_pct > 0) else 0
        return {
            "daily_total_cents": total,
            "daily_cap_cents": cap,
            "cap_pct": round(cap_pct, 2),
            "reset_at": float(state.get("reset_at") or 0.0),
            "blocked_calls": int(state.get("blocked_calls") or 0),
            "skill_name": self.skill_name or "global",
            "soft_warn_pct": self.soft_warn_pct,
            "soft_threshold_cents": soft_threshold,
            "soft_triggered": bool(soft_threshold > 0 and total >= soft_threshold),
        }

    def reset_for_tests(self) -> None:
        """Clear the in-process cache and delete the state file."""
        self._cache = None
        p = self._state_path()
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            pass

    @classmethod
    def reset_all_for_tests(cls, state_dir: str | Path | None = None) -> None:
        """Clear ALL budget state files (test helper)."""
        d = Path(state_dir) if state_dir else _default_state_dir()
        for p in d.glob(f"{_NEW_PREFIX}*{_NEW_SUFFIX}"):
            try:
                p.unlink()
            except OSError:
                pass
