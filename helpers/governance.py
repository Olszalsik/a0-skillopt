"""
SkillOpt - per-skill governance (opt-out + per-skill policy).

v1.5.0-Dev (Day-5 item 8): per-skill policies + opt-out marker.

Today every skill in `usr/skills/` is subject to the auto-loop. On a
multi-tenant install, that's wrong - some skills must be immutable
(compliance / legal), some must be rate-limited (production), and
some must be opt-in (`mode=opt_in` is the default for new skills).

This module is the per-skill lever. It evaluates the effective
policy for a skill at the start of each auto-loop cycle. If the
skill is ineligible, the cycle is skipped with the reason written
to `logs/runs/governance.log` and (best-effort) surfaced on the
dashboard's new `governance` block.

The optout marker + per-skill policy.json live under the USER's skill
dir at `<a0>/usr/skills/<name>/.skillopt.optout` and
`<a0>/usr/skills/<name>/.skillopt.policy.json`. Those are A0-owned
skill dirs, NOT plugin dirs. The governance decision log lives at
`<plugin>/logs/runs/governance.log`.

CRITICAL RULES (do not break):
1. Preserve the gate. `check_skill_eligible()` runs BEFORE the gate,
   never inside it. If governance says "skip", the gate is not called.
2. Loud-not-crash. Every public function returns structured {ok, error}
   on failure instead of raising.
3. Plugin-local only for decisions; user-skill-dir for the markers + policy.
4. Backwards-compat: if `governance.default_policy` is missing from
   config, fall back to `mode=opt_out` (the SAFEST default - no
   auto-loop unless explicitly opted in).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------------- #
# Defaults + test override hook
# ----------------------------------------------------------------------- #

DEFAULT_POLICY: dict[str, Any] = {
    "mode": "opt_out",                # safest default: no auto-loop unless opted in
    "min_interval_seconds": 3600,     # 1 hour floor between cycles
    "daily_budget_cents": 100,         # 100c/skill/day cap
    "require_human_approval": False,   # off by default
}

# Module-level override used by smoke.py + reset_for_tests(). When set,
# _a0_skills_dir() returns this path instead of the production one.
_TEST_SKILLS_DIR: Path | None = None


def set_skills_dir_for_tests(path: Path | None) -> None:
    """Inject a per-skill directory for tests. Pass None to clear."""
    global _TEST_SKILLS_DIR
    _TEST_SKILLS_DIR = path


# ----------------------------------------------------------------------- #
# Path resolution
# ----------------------------------------------------------------------- #

def _runs_dir() -> Path:
    """Locate <plugin>/logs/runs/. Lazy import keeps the helper importable
    in isolation (smoke runner, test harnesses). Mirrors the same
    fallback ladder used by helpers/cycle_history.py."""
    p: Path
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        p = sleep_runner.runs_dir()
    except Exception:
        here = Path(__file__).resolve()
        for ancestor in [here] + list(here.parents):
            candidate = ancestor / "logs" / "runs"
            if candidate.is_dir():
                p = candidate
                break
        else:
            p = here.parent / "logs" / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _a0_skills_dir() -> Path:
    """Locate <a0>/usr/skills/. Test override wins, then sleep_runner,
    then a best-effort path walk from the plugin dir."""
    if _TEST_SKILLS_DIR is not None:
        return _TEST_SKILLS_DIR
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        return sleep_runner.a0_skills_dir()
    except Exception:
        here = Path(__file__).resolve()
        # plugin lives at .../usr/plugins/skillopt/helpers/governance.py
        # so the skills dir is .../usr/skills
        return here.parent.parent.parent / "usr" / "skills"


def _skill_dir(skill_name: str) -> Path:
    """Return <a0>/usr/skills/<name>/. Auto-created only in test mode."""
    if not skill_name:
        return _a0_skills_dir() / "_invalid_"
    p = _a0_skills_dir() / skill_name
    if _TEST_SKILLS_DIR is not None:
        p.mkdir(parents=True, exist_ok=True)
    return p


# ----------------------------------------------------------------------- #
# Config
# ----------------------------------------------------------------------- #

def _default_policy() -> dict[str, Any]:
    """Resolve the effective default policy: built-in defaults merged
    with whatever config.json's `governance.default_policy` block says.
    Missing keys fall back to DEFAULT_POLICY."""
    out: dict[str, Any] = dict(DEFAULT_POLICY)
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        cfg_path = sleep_runner.plugin_root() / "config.json"
        if cfg_path.is_file():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            gov = cfg.get("governance") or {}
            dp = gov.get("default_policy") or {}
            if isinstance(dp, dict):
                for k in ("mode", "min_interval_seconds", "daily_budget_cents", "require_human_approval"):
                    if k in dp:
                        out[k] = dp[k]
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------- #
# Public API
# ----------------------------------------------------------------------- #

def load_skill_policy(skill_name: str) -> dict[str, Any]:
    """Read the effective policy for a skill. Returns a dict with at
    least `{mode, min_interval_seconds, daily_budget_cents, require_human_approval, _source}`.

    `_source` is `"skill_overlay"` when the per-skill `policy.json`
    was loaded, `"global_default"` otherwise. A malformed
    `policy.json` is treated as if missing - we silently fall back.
    """
    policy = _default_policy()
    if not skill_name:
        policy["_source"] = "global_default"
        return policy
    p = _skill_dir(skill_name) / ".skillopt.policy.json"
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                known = {"mode", "min_interval_seconds", "daily_budget_cents", "require_human_approval"}
                extras = {k: v for k, v in data.items() if k not in known}
                for k in known:
                    if k in data:
                        policy[k] = data[k]
                policy["_source"] = "skill_overlay"
                if extras:
                    policy["_raw_extra"] = extras
                return policy
        except Exception:
            pass
    policy["_source"] = "global_default"
    return policy


def check_skill_eligible(skill_name: str) -> tuple[bool, str]:
    """Return (eligible, reason).

    Decision order (the first match wins):
      1. `.skillopt.optout` file present            -> False, "opted_out_via_marker"
      2. effective policy `mode=immutable`          -> False, "mode_immutable"
      3. effective policy `mode=opt_in` + no opt-in -> False, "mode_opt_in_no_marker"
      4. effective policy `mode=opt_out` + no opt-in -> False, "mode_opt_in_no_marker"
      5. last eligible decision within `min_interval_seconds`
                                                     -> False, "rate_limited_min_interval"
      6. today's spend (from budget.py) >= `daily_budget_cents`
                                                     -> False, "daily_budget_exceeded"
      7. `require_human_approval=True` + no approval on file
                                                     -> False, "require_human_approval_pending"
      8. otherwise                                   -> True,  "eligible"

    An opt-in marker (`usr/skills/<name>/.skillopt.optin`) opts the
    skill INTO the auto-loop regardless of the effective mode.
    """
    if not skill_name:
        return False, "mode_opt_in_no_marker"
    sd = _skill_dir(skill_name)

    # 1. Optout wins everything.
    if (sd / ".skillopt.optout").is_file():
        return False, "opted_out_via_marker"

    # Opt-in marker is read once and used in steps 3 + 4.
    has_optin = (sd / ".skillopt.optin").is_file()

    # 2. Immutable?
    policy = load_skill_policy(skill_name)
    mode = policy.get("mode", "opt_out")
    if mode == "immutable":
        return False, "mode_immutable"

    # 3 + 4. Opt-in vs opt-out mode + marker check.
    if mode in ("opt_in", "opt_out"):
        if not has_optin:
            return False, "mode_opt_in_no_marker"
    # mode == "rate_limited" or "open": always allow; rate-limit / budget below.

    # 5. Rate-limit by min_interval_seconds.
    try:
        interval = max(0, int(policy.get("min_interval_seconds", 3600) or 3600))
    except (TypeError, ValueError):
        interval = 3600
    if interval > 0:
        last_ts = _last_eligible_decision_ts(skill_name)
        if last_ts is not None and (time.time() - last_ts) < interval:
            return False, "rate_limited_min_interval"

    # 6. Daily budget cap (from budget.py if available).
    try:
        cap = int(policy.get("daily_budget_cents", 100) or 100)
    except (TypeError, ValueError):
        cap = 100
    if 0 <= cap < 10_000_000:  # sentinel 10M = "budget disabled"
        spent = _today_spent_cents(skill_name)
        if spent is not None and spent >= cap:
            return False, "daily_budget_exceeded"

    # 7. Human approval gate.
    if bool(policy.get("require_human_approval", False)):
        if not _human_approved(skill_name):
            return False, "require_human_approval_pending"

    return True, "eligible"


def mark_decision(skill_name: str, eligible: bool, reason: str) -> dict[str, Any]:
    """Append one decision row to logs/runs/governance.log. Returns
    {ok, error}. Best-effort: callers wrap in their own try/except."""
    entry = {
        "ts_iso": datetime.now(timezone.utc).isoformat(),
        "skill": str(skill_name or ""),
        "eligible": bool(eligible),
        "reason": str(reason or ""),
        "event": "decision",
    }
    return _append_log(entry)


def mark_human_decision(skill_name: str, approved: bool, decided_by: str = "user") -> dict[str, Any]:
    """Record a human approval/rejection. Returns `{ok, ts, decided_by, error}`."""
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts_iso": ts,
        "skill": str(skill_name or ""),
        "event": "human_decision",
        "approved": bool(approved),
        "decided_by": str(decided_by or "user"),
    }
    res = _append_log(entry)
    return {
        "ok": bool(res.get("ok")),
        "ts": ts,
        "decided_by": entry["decided_by"],
        "error": res.get("error"),
    }


def auto_optin_new_skill(skill_name: str, *, source: str = "auto_loop") -> dict[str, Any]:
    """Auto-opt a NEW skill into the auto-loop, behind the human-approval
    guardrail. Creates `.skillopt.optin` (empty) + `.skillopt.policy.json`
    (`mode=opt_in`, `require_human_approval=True`) under the skill dir.

    NEVER touches:
      - skills with a `.skillopt.optout` marker (user explicitly opted out)
      - skills whose effective policy `mode == "immutable"` (compliance/legal)

    Idempotent: if `.skillopt.optin` already exists, returns
    `{ok:True, reason:"exists"}` without rewriting policy.json. Returns
    `{ok, reason, skill, markers}` on success, `{ok:False, error}` on
    failure. The `require_human_approval:true` guardrail means an
    auto-opted-in skill is NOT silently adoptable — `check_skill_eligible`
    still returns `require_human_approval_pending` until a human approves
    it via `mark_human_decision` (the /governance_approve endpoint).
    """
    if not skill_name:
        return {"ok": False, "error": "missing skill_name"}
    sd = _skill_dir(skill_name)
    # Guardrail 1: explicit opt-out — never touch.
    if (sd / ".skillopt.optout").is_file():
        return {"ok": True, "reason": "opted_out", "skill": skill_name,
                "markers": {"optout": True}}
    # Guardrail 2: immutable — never touch.
    try:
        policy = load_skill_policy(skill_name)
    except Exception:
        policy = {}
    if str(policy.get("mode", "opt_out")) == "immutable":
        return {"ok": True, "reason": "immutable", "skill": skill_name,
                "markers": {"immutable": True}}
    # Idempotent: already opted in.
    if (sd / ".skillopt.optin").is_file():
        return {"ok": True, "reason": "exists", "skill": skill_name,
                "markers": {"optin": True}}
    try:
        if _TEST_SKILLS_DIR is None:
            sd.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
        policy_doc = {
            "mode": "opt_in",
            "require_human_approval": True,
            "auto_optin_source": str(source or "auto_loop"),
            "auto_optin_at": ts,
        }
        (sd / ".skillopt.policy.json").write_text(
            json.dumps(policy_doc, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _append_log({
            "ts_iso": ts,
            "skill": skill_name,
            "event": "auto_optin",
            "source": policy_doc["auto_optin_source"],
        })
        return {"ok": True, "reason": "created", "skill": skill_name,
                "markers": {"optin": True, "policy": True}}
    except Exception as e:
        return {"ok": False, "error": str(e), "skill": skill_name}


def list_governed_skills() -> list[str]:
    """All skills in the user's skills dir that have an optout marker
    or a per-skill policy.json. Sorted alphabetically."""
    out: list[str] = []
    root = _a0_skills_dir()
    if not root.is_dir():
        return out
    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / ".skillopt.optout").is_file() or (entry / ".skillopt.policy.json").is_file():
                out.append(entry.name)
    except Exception:
        pass
    return out


def get_governance_status() -> dict[str, Any]:
    """Read-only snapshot for the dashboard's governance block. Mirrors
    the shape used by helpers/cycle_history.get_history_status() and
    helpers/failure_memory.get_status_block(): `enabled`, `available`,
    `default_policy`, `opted_out`, `governed`, `last_decisions`,
    `skills_dir`, `log_path`, `file_size_bytes`. On failure returns
    `{available: False, error: ...}` rather than raising."""
    try:
        default_policy = _default_policy()
        opted_out: list[str] = []
        opted_in: list[str] = []
        governed: list[str] = []
        skills_root = _a0_skills_dir()
        if skills_root.is_dir():
            try:
                for entry in sorted(skills_root.iterdir()):
                    if not entry.is_dir():
                        continue
                    if (entry / ".skillopt.optout").is_file():
                        opted_out.append(entry.name)
                    if (entry / ".skillopt.optin").is_file():
                        opted_in.append(entry.name)
                    if (entry / ".skillopt.policy.json").is_file():
                        governed.append(entry.name)
            except Exception:
                pass
        log = _runs_dir() / "governance.log"
        last_decisions: dict[str, dict[str, Any]] = {}
        if log.is_file():
            try:
                with open(log, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    sk = entry.get("skill")
                    if not sk or sk in last_decisions:
                        continue
                    if entry.get("event") != "decision":
                        continue
                    last_decisions[sk] = {
                        "ts": entry.get("ts_iso") or entry.get("ts"),
                        "eligible": entry.get("eligible"),
                        "reason": entry.get("reason"),
                    }
            except Exception:
                pass
        return {
            "available": True,
            "enabled": True,
            "default_policy": default_policy,
            "opted_out": opted_out,
            "opted_in": opted_in,
            "governed": governed,
            "last_decisions": last_decisions,
            "skills_dir": str(skills_root),
            "log_path": str(log),
            "file_size_bytes": (log.stat().st_size if log.is_file() else 0),
        }
    except Exception as e:
        return {"available": False, "enabled": True, "error": str(e)}


def reset_for_tests() -> None:
    """Wipe test state. Called by smoke tests in setUp/tearDown."""
    set_skills_dir_for_tests(None)
    try:
        log = _runs_dir() / "governance.log"
        if log.is_file():
            log.unlink()
    except Exception:
        pass


# ----------------------------------------------------------------------- #
# Internals
# ----------------------------------------------------------------------- #

def _append_log(entry: dict[str, Any]) -> dict[str, Any]:
    """Append one JSON line to logs/runs/governance.log. Best-effort."""
    try:
        runs = _runs_dir()
        out_path = runs / "governance.log"
        with open(out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _last_eligible_decision_ts(skill_name: str) -> float | None:
    """Find the most recent eligible=True decision ts for this skill
    in governance.log, or None if not found."""
    log = _runs_dir() / "governance.log"
    if not log.is_file():
        return None
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("skill") != skill_name:
                continue
            if entry.get("event") != "decision":
                continue
            if not entry.get("eligible"):
                continue
            ts_str = entry.get("ts_iso") or entry.get("ts")
            if not ts_str:
                continue
            try:
                dt = datetime.fromisoformat(str(ts_str))
                return dt.timestamp()
            except Exception:
                continue
    except Exception:
        pass
    return None


def _today_spent_cents(skill_name: str) -> int | None:
    """Best-effort lookup of today's spend via helpers/budget.py.
    Returns None when the budget helper is unavailable."""
    if not skill_name:
        return None
    try:
        from usr.plugins.skillopt.helpers import budget  # type: ignore  # noqa: E402
    except Exception:
        try:
            from helpers import budget  # type: ignore  # noqa: E402
        except Exception:
            return None
    try:
        tracker = budget.BudgetTracker(skill_name)
        s = tracker.get_status()
        return int(s.get("spent_today_cents", 0))
    except Exception:
        return None


def _human_approved(skill_name: str) -> bool:
    """Look in governance.log for a `human_decision` entry on this skill.
    The most recent entry wins (True = approved, False = denied)."""
    log = _runs_dir() / "governance.log"
    if not log.is_file():
        return False
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("skill") != skill_name:
                continue
            if entry.get("event") == "human_decision":
                return bool(entry.get("approved"))
    except Exception:
        pass
    return False


__all__ = [
    "DEFAULT_POLICY",
    "auto_optin_new_skill",
    "check_skill_eligible",
    "get_governance_status",
    "list_governed_skills",
    "load_skill_policy",
    "mark_decision",
    "mark_human_decision",
    "reset_for_tests",
    "set_skills_dir_for_tests",
]
