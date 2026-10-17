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

import hashlib
import threading
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
# v1.8.34: the operating mode (manual vs autonomous)
# ----------------------------------------------------------------------- #

EVOLUTION_MODES = ("manual", "autonomous")

# v1.8.35 (audit F9): `decided_by` values that mean "a machine decided this".
# `_human_approved` excludes exactly these, so an autonomous adoption can
# never satisfy `require_human_approval`. This is deliberately a small
# allow-list of MACHINE values rather than an allow-list of human ones:
# `decided_by` is free-form (the HTTP route passes through whatever the
# caller sends, and a named operator is legitimate), so requiring the literal
# "user" would silently invalidate every named-human approval.
MACHINE_DECIDED_BY = frozenset(
    {
        "autonomous",
        "auto",
        "machine",
        "bot",
        # v1.8.48 (audit G4): the redaction notice is written by the DRAIN
        # with approved=True (auto_loop, and now on the harvest path too).
        # Without it here that machine row read as a HUMAN decision and armed
        # `require_human_approval` with no person ever clicking Approve -
        # the exact F9 regression the allow-list exists to prevent. An
        # allow-list of machine values is only as good as its contents.
        "redaction-notice",
    }
)

# v1.8.43 (re-audit R4): returned by the ledger/staging readers when they
# could NOT EVALUATE, as distinct from `None` meaning "genuinely absent".
#
# Without this distinction the rejection rail's `except Exception: return
# True  # Undecidable => restrictive` branch was unreachable: both helpers
# swallowed everything and returned None, so nothing inside the rail's
# `try` could raise. An unreadable ledger therefore read as "no denial"
# and the rail was silently DISARMED - the drain adopting a document a
# human had explicitly refused, with no log line and no reason string.
_UNREADABLE = object()

# Set by `_iter_governance_rows` when a log generation exists but cannot
# be read. A generator cannot return a value, so the fault travels this
# way; `_last_human_decision` reads and resets it.
_LOG_READ_FAILED = False

# v1.8.36: tolerance for the rejection rail's mtime comparison. The ledger
# timestamp is microsecond-precision ISO; `st_mtime` is a full-resolution
# float. Absorbs the precision gap ONLY - see `_human_rejected_current_proposal`.
_REJECTION_MTIME_TOLERANCE_S = 0.01

# The kill-switch marker. Presence alone pauses autonomy, whatever the
# config says, so an operator can stop autonomous writes without editing
# config.json (which the WebUI also writes) and without a restart.
AUTONOMOUS_PAUSE_MARKER = ".autonomous_pause"

_TRUE_STRINGS = {"true", "yes", "on", "1"}
_FALSE_STRINGS = {"false", "no", "off", "0"}

# Last logged value per source, so a bad setting is reported ONCE per
# distinct value rather than on every tick (a 30-min loop would otherwise
# grow auto_loop.log by ~48 lines/day of the same complaint).
_mode_reported: dict[str, str] = {}


def _sleep_runner():
    """The plugin's sleep_runner, preferring an ALREADY-IMPORTED instance.

    The namespace-first two-path import used everywhere else in this plugin
    can materialise a SECOND module object in a bare-python context, where
    `usr.plugins.skillopt.helpers.sleep_runner` and `helpers.sleep_runner`
    are distinct. That matters here specifically because these functions
    log to and create markers inside sleep_runner's directory: if a test
    patches `helpers.sleep_runner.runs_dir`, a freshly-imported namespace
    copy would ignore the patch and write to the REAL runs dir.

    So: look in sys.modules under both names first (one of them is always
    already loaded by the time anything calls us), and only fall back to an
    import. Returns None when unavailable; every caller degrades softly.
    """
    import sys as _sys
    for name in ("usr.plugins.skillopt.helpers.sleep_runner",
                 "helpers.sleep_runner"):
        mod = _sys.modules.get(name)
        if mod is not None and hasattr(mod, "runs_dir"):
            return mod
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        return sleep_runner
    except Exception:
        pass
    try:
        from helpers import sleep_runner  # type: ignore
        return sleep_runner
    except Exception:
        return None


def _log_mode_rejection(msg: str) -> None:
    """Record a rejected mode value in logs/runs/auto_loop.log. Never raises.

    Mirrors official_adapter._log_backend_rejection: an ignored setting must
    be self-diagnosing on the next read, because a switch that silently does
    nothing is indistinguishable from one that is working."""
    try:
        sr = _sleep_runner()
        if sr is None:
            return
        log = sr.runs_dir() / "auto_loop.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            sr.rotate_log_if_large(log)
        except Exception:
            pass
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}\n")
    except Exception:
        pass


def _coerce_bool(value: Any) -> bool | None:
    """Strict boolean coercion. Returns None when the value is unusable.

    Deliberately NOT `bool(value)`. The old call sites used
    `cfg.get("auto_adopt", False)`, which is:
      - None for `auto_adopt:` with no value -> the drain silently stops;
      - True for the STRING "false" -> the drain silently STARTS.
    Both are wrong in opposite directions, and both are invisible. None here
    means "rejected", which the caller logs and treats as the safe default.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE_STRINGS:
            return True
        if s in _FALSE_STRINGS:
            return False
    return None


def _autonomous_pause_path():
    sr = _sleep_runner()
    if sr is None:
        return None
    try:
        return sr.runs_dir() / AUTONOMOUS_PAUSE_MARKER
    except Exception:
        return None


def is_autonomous_paused() -> bool:
    """True when the kill-switch marker exists. Never raises."""
    try:
        p = _autonomous_pause_path()
        return bool(p is not None and p.is_file())
    except Exception:
        return False


def set_autonomous_pause(paused: bool) -> dict[str, Any]:
    """Create/remove the kill-switch marker. Returns `{ok, paused, path}`."""
    path = None
    try:
        p = _autonomous_pause_path()
        if p is None:
            return {"ok": False, "error": "runs_dir unavailable"}
        path = str(p)
        if paused:
            p.write_text(
                "autonomy paused " + datetime.now(timezone.utc).isoformat(),
                encoding="utf-8",
            )
        elif p.is_file():
            p.unlink()
        return {"ok": True, "paused": bool(paused), "path": path}
    except Exception as e:
        return {"ok": False, "error": str(e), "path": str(path or "")}


def sleep_runner_for_mode():
    """The already-imported sleep_runner, or None. Prefers sys.modules.

    Same rationale as `_sleep_runner()`: a freshly-imported namespace copy can
    be a DIFFERENT module object from the one a test patched, which would send
    config reads to the real runs dir instead of the sandbox."""
    import sys as _sys
    for name in ("usr.plugins.skillopt.helpers.sleep_runner",
                 "helpers.sleep_runner"):
        mod = _sys.modules.get(name)
        if mod is not None and hasattr(mod, "merged_config"):
            return mod
    return _sleep_runner()


def _operator_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """The operator-level (PERSISTED) config, with no YAML defaults mixed in.

    This answers the one question the merged config cannot: was
    `evolution_mode` actually *chosen*, or is it merely the shipped default?

    `governance.evolution_mode` ships as `manual`, so `merged_config()` ALWAYS
    contains it. If precedence were decided from a merged blob, the shipped
    default would outrank an install whose config.json says
    `auto_adopt: true` and upgrading would silently stop unattended writes to
    live skills. Measured on a real install (audit F1): the drain reported
    manual while `/mode` reported autonomous, because only the no-argument
    call path consulted the operator's own settings.

    The registry is therefore authoritative whenever it yields anything. The
    `cfg` argument is a fallback for standalone python and unit tests, where
    there is no registry and the supplied dict is the only evidence there is.

    The production call sites pass NO argument for exactly this reason - see
    `Local Contracts` in AGENTS.md. Do not "optimise" them into passing the
    merged config.
    """
    sr = sleep_runner_for_mode()
    if sr is not None:
        try:
            reg = sr._framework_registry_config()
            if isinstance(reg, dict) and reg:
                return reg
        except Exception:
            pass
    if isinstance(cfg, dict):
        return cfg
    return {}
    try:
        reg = sr._framework_registry_config()
        return reg if isinstance(reg, dict) else {}
    except Exception:
        return {}


def resolve_evolution_mode(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """THE single source of truth for manual vs autonomous. Never raises.

    Returns `{mode, autonomous, source, raw, killed, error}` where `mode` is
    one of EVOLUTION_MODES and `autonomous` is `mode == "autonomous"`.

    WHY ONE RESOLVER
    ----------------
    The autonomous half of this feature already existed: `auto_adopt`
    (default false) gated the drain, and `check_skill_eligible(...,
    auto_adopt=True)` overrode `require_human_approval`. Adding a second
    `evolution_mode` flag beside it would create two overlapping switches
    with undefined interaction - the failure mode behind both the
    `auto_adopt: null` stall and the v1.8.18 semantics change that surprised
    operators. So `evolution_mode` becomes canonical and `auto_adopt` is
    demoted to a LEGACY ALIAS, read only when `evolution_mode` is absent.
    Existing installs keep working untouched, and `source` makes the
    provenance auditable.

    RESOLUTION ORDER (first match wins)
    ----------------------------------
    0. `logs/runs/.autonomous_pause` exists -> manual, `killed=True`.
       Checked FIRST so an operator can stop autonomy even if config says
       autonomous.
    1. The OPERATOR explicitly set `governance.evolution_mode`
       (see `_operator_config`) -> that wins, `source="evolution_mode"`.
    2. The OPERATOR explicitly set `auto_adopt` -> that wins,
       `source="auto_adopt_legacy"`. This is what preserves an existing
       autonomous install across the upgrade to v1.8.34.
    3. Otherwise the shipped default applies -> manual,
       `source="evolution_mode_default"`.

    Why precedence is decided against the OPERATOR's config rather than the
    merged one: `governance.evolution_mode` ships as `manual`, so the merged
    config ALWAYS contains it. A plain "canonical key wins" rule would
    therefore silently override an install whose config.json says
    `auto_adopt: true`, and upgrading would quietly stop unattended writes to
    live skills. That was measured on a real install, not hypothesised: a
    shipped default must never outrank an explicit operator setting.

    A rejected value falls back to `manual` (the SAFE direction) and is
    logged naming the value and the valid set.

    Callers must use this rather than reading `cfg["auto_adopt"]` directly.
    """
    out: dict[str, Any] = {
        "mode": "manual",
        "autonomous": False,
        "source": "evolution_mode_default",
        "raw": None,
        "killed": False,
        "error": "",
    }
    try:
        if is_autonomous_paused():
            out["killed"] = True
            out["source"] = "kill_switch"
            out["error"] = (
                f"{AUTONOMOUS_PAUSE_MARKER} is present; autonomy is paused"
            )
            return out
    except Exception:
        pass

    operator = _operator_config(cfg)
    op_gov = operator.get("governance")
    op_gov = op_gov if isinstance(op_gov, dict) else {}
    op_mode = op_gov.get("evolution_mode")
    op_legacy = operator.get("auto_adopt")

    # ---- 1. the operator chose the canonical key ----------------------
    if op_mode is not None and str(op_mode).strip() != "":
        cand = str(op_mode).strip().lower()
        if cand in EVOLUTION_MODES:
            out["mode"] = cand
            out["autonomous"] = cand == "autonomous"
            out["source"] = "evolution_mode"
            out["raw"] = op_mode
            return out
        out["error"] = (
            f"governance.evolution_mode {op_mode!r} is not a valid mode; "
            f"valid modes are {', '.join(EVOLUTION_MODES)}. Falling back to "
            f"manual. Set it to one of those values."
        )
        _report_once("evolution_mode", op_mode, out["error"])

    # ---- 2. the operator chose the legacy alias -----------------------
    if op_legacy is not None:
        coerced = _coerce_bool(op_legacy)
        if coerced is not None:
            out["mode"] = "autonomous" if coerced else "manual"
            out["autonomous"] = coerced
            out["source"] = "auto_adopt_legacy"
            out["raw"] = op_legacy
            return out
        out["error"] = (
            f"auto_adopt {op_legacy!r} is not a valid boolean (accepted: "
            f"true/false/yes/no/on/off/1/0). Falling back to manual."
        )
        _report_once("auto_adopt", op_legacy, out["error"])

    # ---- 3. nothing explicit: the shipped default -------------------
    try:
        merged = (sleep_runner_for_mode().merged_config()
                  if cfg is None else cfg)
        gov = merged.get("governance") if isinstance(merged, dict) else None
        gov = gov if isinstance(gov, dict) else {}
        raw_default = gov.get("evolution_mode")
        if raw_default is not None and str(raw_default).strip() != "":
            cand = str(raw_default).strip().lower()
            if cand in EVOLUTION_MODES:
                out["mode"] = cand
                out["autonomous"] = cand == "autonomous"
                out["raw"] = raw_default
                return out
    except Exception:
        pass
    return out


def _report_once(source: str, value: Any, msg: str) -> None:
    """Log a rejected mode value once per distinct value. Never raises."""
    key = f"{source}={value!r}"
    if _mode_reported.get(source) == key:
        return
    _mode_reported[source] = key
    _log_mode_rejection(msg)


def autonomous_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `governance.autonomous_*` rail settings, clamped and defaulted.

    Separate from resolve_evolution_mode so the mode question ("may it act
    without asking?") stays independent of the budget question ("how much
    may it do?"). Returns every key as a correctly typed value so no caller
    has to re-coerce."""
    out: dict[str, Any] = {
        "max_adoptions_per_day": 5,
        "max_adoptions_per_skill_per_day": 2,
        "require_post_adopt_check": True,
        # v1.8.34: DEFAULT 0 (inert), not 20 as first drafted. A raw
        # rollout COUNT is a weak proxy for justification — the held-out gate
        # measurement is the real evidence — and a non-zero default has a
        # concrete failure mode: `rollout_retention_days` deletes rollout
        # files, so a pruned/rotated rollout dir would block EVERY
        # autonomous adoption forever, with the proposal parked in staging
        # and no obvious cause. Operators who want the floor opt into it.
        "min_evidence_rollouts": 0,
        "audit": True,
    }
    if cfg is None:
        try:
            from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
            cfg = sleep_runner.merged_config()
        except Exception:
            try:
                from helpers import sleep_runner  # type: ignore
                cfg = sleep_runner.merged_config()
            except Exception:
                return out
    if not isinstance(cfg, dict):
        return out
    gov = cfg.get("governance")
    gov = gov if isinstance(gov, dict) else {}
    for key, default in out.items():
        raw = gov.get("autonomous_" + key)
        if raw is None:
            continue
        if isinstance(default, bool):
            coerced = _coerce_bool(raw)
            out[key] = default if coerced is None else coerced
        else:
            try:
                val = int(raw)
            except (TypeError, ValueError):
                continue
            # A cap of 0 means "no autonomous adoptions at all", which is a
            # legitimate way to keep the mode on but freeze writes. It is
            # NOT clamped up.
            out[key] = max(0, val)
    return out


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


def _fallback_skills_dir(here: Path) -> Path:
    """Best-effort <project>/usr/skills resolution from this file's own
    location (v1.8.2). Pure path math — no filesystem access — so it is
    trivially testable. `here` is normally
    .../usr/plugins/skillopt/helpers/governance.py.

    v1.8.1 regression: the old formula (here.parent.parent.parent /
    "usr" / "skills") was one parent short and resolved to the bogus
    <project>/usr/plugins/usr/skills tree, so governance markers created
    from bare-python contexts (skillopt_trainer subordinate, standalone
    scripts) landed outside the real skills dir.
    """
    for ancestor in here.parents:
        if ancestor.name == "plugins" and ancestor.parent.name == "usr":
            return ancestor.parent / "skills"
    for ancestor in here.parents:
        if ancestor.name == "skillopt" and ancestor.parent.name == "plugins":
            return ancestor.parent.parent / "skills"
    return Path("/a0/usr/skills")


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
        return _fallback_skills_dir(here)


def _opt_int(value: Any, default: int) -> int:
    """An int option where an explicit `0` is a REAL value, not "absent".

    v1.8.38 (audit M4). The idiom `int(cfg.get(key, D) or D)` silently turns a
    deliberate `0` back into the default, so `min_interval_seconds: 0` became
    3600 and `daily_budget_cents: 0` became 100. Both are legitimate operator
    settings meaning "no limit", and `budget.BudgetTracker` already documents
    `daily_cap_cents <= 0` as unlimited - so governance contradicted the very
    module it delegates to. Only `None` takes the default.

    A non-numeric value also takes the default rather than raising, because
    this runs inside the eligibility chain (audit F8: fail closed, never
    crash).
    """
    if value is None:
        return int(default)
    if isinstance(value, bool):
        # bool is an int subclass; treating True as 1 here would be a silent
        # surprise, so a bool takes the default.
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _parse_ts(value: Any) -> float:
    """Parse a ledger timestamp into an epoch float. 0.0 when unusable.

    v1.8.38 (audit M1). The two ledgers disagree about format AND timezone:

      - `governance.log` writes `datetime.now(timezone.utc).isoformat()`
        -> `2026-10-04T13:37:24.047338+00:00`  (microseconds, UTC)
      - `cycle_history.jsonl` writes `time.strftime("%Y-%m-%dT%H:%M:%S%z")`
        -> `2026-10-04T13:37:24+0200`          (no fraction, LOCAL time)

    `_decided_proposal_ids` merged them with a plain string compare, which is
    wrong twice over: `'.' > '+'`, so the microsecond form sorts above the
    fraction-less form regardless of the clock; and the offsets differ, so a
    local-time row could be attributed to the wrong decision. Comparing
    PARSED EPOCHS is the only correct merge.

    A naive timestamp is left naive: `strftime` produced it in local time, so
    `fromisoformat`'s local assumption is correct and "fixing" it to UTC would
    introduce a fresh offset bug.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    candidates = [text]
    # `fromisoformat` on Python < 3.11 rejects a `+0200` offset without the
    # colon that `strftime("%z")` emits, so insert it before trying.
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
        candidates.append(text[:-2] + ":" + text[-2:])
    for cand in candidates:
        try:
            return datetime.fromisoformat(cand).timestamp()
        except Exception:
            continue
    return 0.0


def _is_safe_skill_name(skill_name: Any) -> bool:
    """Shared name-safety predicate, resolved the same two-path way as the
    other cross-module helpers here. Only the PREDICATE is shared: it needs no
    path knowledge, so it cannot disagree with a caller about which base
    directory is in play."""
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        try:
            from helpers import sleep_runner  # type: ignore
        except Exception:
            # Last resort: reject rather than guess, since guessing is the
            # failure mode that lets a traversal through.
            return False
    try:
        return bool(sleep_runner.is_safe_skill_name(skill_name))
    except Exception:
        return False


def _skill_dir(skill_name: str) -> Path:
    """Return <a0>/usr/skills/<name>/. Auto-created only in test mode.

    v1.8.24 (P1.8): validates the NAME with the shared predicate, then joins it
    onto this module's OWN base rather than delegating the join to
    `sleep_runner.safe_skill_dir`.

    That split matters. `safe_skill_dir` joins onto `a0_skills_dir()`, which
    honours the `SKILLOPT_SKILLS_DIR` env override - but this module's test
    sandbox is `_TEST_SKILLS_DIR`, a different override. Delegating the join
    therefore pointed every governance test at the PRODUCTION skills tree:
    per-skill `policy.json` lookups missed (so the global default won and the
    reason came back `mode_opt_out_no_marker` instead of the real one) and
    marker writes targeted production. That is the P4 test-isolation class the
    plugin fixed in 1.8.19, reintroduced.

    A predicate needs no path knowledge, so it is shared; the join stays local.

    v1.8.38 (audit M3) - two hardening changes:

    1. **Post-resolve containment.** `safe_skill_dir` documents two layers -
       name validation, then a check that the RESOLVED path is still inside the
       skills dir - and this function implemented only the first. A symlinked
       skill directory therefore let governance's marker writes escape the
       tree while the adopt path refused the same name. Both layers now run
       here, so the two agree.

    2. **Per-name `_invalid_` isolation.** Every unsafe name collapsed into one
       shared `_invalid_/` directory, so two different bad names shared a
       policy scope and a marker written for one was read as the other's -
       and `auto_optin_new_skill` would `mkdir` and populate it. Unsafe names
       now get their own subdirectory keyed by a short digest of the raw
       name, so each is isolated while remaining deterministic (the same bad
       name always maps to the same place, which keeps the audit trail
       meaningful).
    """
    base = _a0_skills_dir()
    raw = "" if skill_name is None else str(skill_name)
    if not _is_safe_skill_name(skill_name):
        # Isolated per raw name: a shared bucket made distinct bad names share
        # one policy scope. The digest keeps the mapping deterministic without
        # putting the unsafe string itself into a path.
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
        p = base / "_invalid_" / digest
    else:
        p = base / raw.strip()

    # Layer 2: the RESOLVED path must still be inside the skills tree. Catches
    # a symlinked skill directory, which name validation alone cannot.
    try:
        base_real = Path(os.path.realpath(str(base)))
        p_real = Path(os.path.realpath(str(p)))
        inside = (p_real == base_real
                  or base_real in p_real.parents)
    except Exception:
        inside = False
    if not inside:
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]
        p = base / "_invalid_" / digest
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
        cfg_path = _default_policy_config_path()
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


# v1.8.52: test-isolation hook, same pattern as inner_loop.state_dir. When
# set (by the smoke suite), the default-policy rails read the operator's
# config.json from HERE instead of the plugin root - a live install's
# operator intent (mode / require_human_approval) is exactly what the
# suite must NOT inherit: 13 governance tests read as "skipped" on a
# live install while staying green in CI, purely through this file.
# None restores the production path; live code never sets it.
default_policy_config_path: Path | None = None


def _default_policy_config_path() -> Path:
    """Where the default-policy rails read the operator's config.json."""
    if default_policy_config_path is not None:
        return default_policy_config_path
    sr = _sleep_runner()
    if sr is not None:
        return sr.plugin_root() / "config.json"
    # v1.8.52: the old inline import raised out of _default_policy's try on a
    # bare import and silently used built-ins; keep that degrade, but through
    # a path that cannot exist rather than an exception.
    return Path.cwd() / ".nonexistent-governance-config.json"


# ----------------------------------------------------------------------- #
# Public API
# ----------------------------------------------------------------------- #

def _parse_pause_until(raw):
    # v1.8.6: parse a .skillopt.pause_until value (epoch or ISO-8601).
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _stuck_log():
    # v1.8.6: locate cycle_history.jsonl (module _runs_dir first, then
    # a module-relative fallback).
    cands = []
    try:
        cands.append(_runs_dir() / "cycle_history.jsonl")
    except Exception:
        pass
    cands.append(Path(__file__).resolve().parent.parent
                 / "logs" / "runs" / "cycle_history.jsonl")
    for c in cands:
        try:
            if c.is_file():
                return c
        except Exception:
            continue
    return cands[0]


def _entry_epoch(e):
    # v1.8.6: best-effort epoch seconds for a cycle_history entry.
    v = e.get("ts_epoch") or e.get("ts")
    if isinstance(v, (int, float)) and v > 0:
        return float(v)
    s = str(v or "")
    if s.replace(".", "", 1).isdigit():
        try:
            f = float(s)
            if f > 0:
                return f
        except Exception:
            pass
    iso = str(e.get("ts_iso") or "")
    if iso:
        p = _parse_pause_until(iso)
        if p > 0:
            return p
    return None


def get_stuck_skills(min_consecutive_rejects=3, days_window=7):
    # v1.8.6 (open question 5): a skill is stuck when its newest
    # cycle_history entries show >= min_consecutive_rejects consecutive
    # rejected/errored cycles AND no adoption within days_window days.
    # Read-only; never raises (loud-not-crash).
    try:
        log = _stuck_log()
        if not log.is_file():
            return []
        with open(log, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()[-500:]
        by_skill = {}
        for line in raw_lines:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not isinstance(e, dict):
                continue
            sk = str(e.get("skill") or "")
            if not sk or sk == "_tick":
                continue
            by_skill.setdefault(sk, []).append(e)
        now = time.time()
        out = []
        for sk, es in by_skill.items():
            consec = 0
            for e in reversed(es):
                if e.get("outcome") in ("rejected", "errored"):
                    consec += 1
                else:
                    break
            adopted_epoch = None
            adopted_raw = None
            for e in reversed(es):
                if e.get("outcome") == "adopted":
                    adopted_raw = e.get("ts_iso") or e.get("ts")
                    adopted_epoch = _entry_epoch(e)
                    break
            stale = adopted_epoch is None or (
                (now - adopted_epoch) > float(days_window) * 86400.0)
            if consec >= min_consecutive_rejects and stale:
                out.append({
                    "skill": sk,
                    "consecutive_rejects": consec,
                    "last_adopted_ts": adopted_raw,
                })
        return out
    except Exception:
        return []


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
                known = {"mode", "min_interval_seconds", "daily_budget_cents", "require_human_approval", "allowed_fragments", "max_verbosity_delta_ratio", "forbid_patterns"}
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


def check_skill_eligible(
    skill_name: str, *, auto_adopt: bool = False
) -> tuple[bool, str]:
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
    # 1.5. v1.8.6: one-click pause marker (.skillopt.pause_until).
    # Epoch seconds or ISO-8601; past/unparsable = not paused.
    _pu = sd / ".skillopt.pause_until"
    if _pu.is_file():
        # v1.8.35 (audit F8): the read is guarded. A marker written by an
        # interrupted multi-byte write can be non-UTF-8, and a directory or a
        # permissions change raises here - both reached auto_loop's fail-open
        # handler and disabled every rail, including optout and immutable.
        # An unreadable pause marker must NOT read as "not paused".
        try:
            _raw = _pu.read_text(encoding="utf-8").strip()
        except Exception:
            return False, "pause_marker_unreadable"
        if _raw.replace(".", "", 1).isdigit():
            _until = float(_raw)
        else:
            _until = _parse_pause_until(_raw)
        if _until and time.time() < _until:
            return False, "paused_until_marker"

    # Opt-in marker is read once and used in steps 3 + 4.
    has_optin = (sd / ".skillopt.optin").is_file()

    # 2. Immutable?
    policy = load_skill_policy(skill_name)
    mode = policy.get("mode", "opt_out")
    if mode == "immutable":
        return False, "mode_immutable"

    # 3 + 4. Opt-in vs opt-out mode + marker check.
    # v1.8.1: distinct reason strings so the dashboard/log can tell the two
    # modes apart (both previously reported "mode_opt_in_no_marker").
    if mode == "opt_in" and not has_optin:
        return False, "mode_opt_in_no_marker"
    if mode == "opt_out" and not has_optin:
        return False, "mode_opt_out_no_marker"
    # mode == "rate_limited" or "open": always allow; rate-limit / budget below.

    # 5. Rate-limit by min_interval_seconds.
    #    v1.8.38 (audit M4): an explicit 0 means "no rate limit", which the
    #    `or 3600` idiom silently converted back to a one-hour floor. Only an
    #    ABSENT value takes the default.
    interval = _opt_int(policy.get("min_interval_seconds"), 3600)
    if interval > 0:
        last_ts = _last_eligible_decision_ts(skill_name)
        if last_ts is not None and (time.time() - last_ts) < interval:
            return False, "rate_limited_min_interval"

    # 6. Daily budget cap (from budget.py if available).
    #    v1.8.38 (audit M4): same fix, and 0 now means "no cap" - matching
    #    `budget.BudgetTracker`, where `daily_cap_cents <= 0` is unlimited.
    #    It previously became 100c. Note the two zero readings differ by
    #    intent: for the interval 0 disables the WAIT, for the cap it disables
    #    the CEILING. Both mean "the operator asked for no limit here".
    cap = _opt_int(policy.get("daily_budget_cents"), 100)
    if cap != 0 and 0 <= cap < 10_000_000:  # sentinel 10M = "budget disabled"
        spent = _today_spent_cents(skill_name)
        if spent is not None and spent >= cap:
            return False, "daily_budget_exceeded"

    # 6.5 Human REJECTION rail (v1.8.36, audit F2).
    #
    #     Deliberately ABOVE step 7 and deliberately NOT part of it.
    #     Autonomy may bypass "has nobody approved this document?" - that is
    #     what autonomy means. It must never bypass "a human explicitly
    #     refused this document". Before this, a denial only flipped
    #     `_human_approved()` to False, which parks the skill at
    #     `require_human_approval_pending` in manual mode - but the autonomous
    #     override skips that gate, so the drain re-read the very same
    #     rejected file and wrote it into the live SKILL.md.
    #
    #     Staleness is handled inside the helper: a denial only binds the
    #     proposal it was made about, so a NEW proposal for the same skill is
    #     judged on its own merits rather than muted forever.
    if _human_rejected_current_proposal(skill_name):
        return False, "human_rejected"

    # 7. Human approval gate.
    #    v1.8.18 (P2.1, 2026-09-22 audit): `auto_adopt=True` is the
    #    operator's explicit opt-in to full autonomy and overrides the
    #    pending-approval block. Per-skill optout / immutable / pause
    #    markers still win unconditionally (steps 1-2.5 above), and a human
    #    rejection wins at step 6.5. The dashboard Approve flow keeps its
    #    role for `auto_adopt: false`.
    if bool(policy.get("require_human_approval", False)) and not auto_adopt:
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


def _approvals_index_path():
    """The durable approvals index. Never rotated, never pruned by retention."""
    try:
        return _runs_dir() / "approvals_index.jsonl"
    except Exception:
        return None


# v1.8.46 (audit R17): appends and compaction of this file are
# serialised by ONE lock. Compaction is read -> rewrite ->
# `os.replace`, and three writers append here (the /mode and
# /governance_approve routes plus the drain thread), so a row appended
# between the read and the replace used to be DROPPED with no error -
# the exact failure M2 was filed for. The lock closes that window;
# nothing else may `os.replace` this file.
_APPROVALS_INDEX_LOCK = threading.RLock()


def _append_approvals_index(entry: dict[str, Any]) -> None:
    """Mirror one decision into the durable index. Best-effort, never raises.

    v1.8.38 (audit M2). `governance.log` is rotated at 5 MB and the previous
    generation is DELETED, so every approval older than one rotation window
    silently vanished: `_human_approved` returned False, a
    `require_human_approval` skill reverted to *pending* with no visible
    cause, and `decided_by="autonomous"` audit rows were lost - contradicting
    the "auditable forever" contract.

    This index holds only what the readers actually need (the latest decision
    per skill, and the latest per proposal), so it is bounded by the number of
    skills and outstanding proposals rather than by history. It is written
    from the SAME function as the log, so it adds no second writer.

    Compaction keeps the newest row per key and only runs when the file grows
    past a threshold, so it cannot become an unbounded log.
    """
    try:
        p = _approvals_index_path()
        if p is None:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "skill": entry.get("skill"),
            "approved": bool(entry.get("approved")),
            "decided_by": entry.get("decided_by"),
            "proposal_id": entry.get("proposal_id"),
            "ts": entry.get("ts_iso") or entry.get("ts"),
        }
        if not row["skill"] and not row["proposal_id"]:
            return
        # v1.8.46 (audit R17): the append takes the SAME lock compaction
        # does, so an append can never land between compaction's read and its
        # `os.replace` and be silently dropped.
        with _APPROVALS_INDEX_LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        try:
            if p.stat().st_size > 512_000:
                _compact_approvals_index(p)
        except Exception:
            pass
    except Exception:
        pass


def _compact_approvals_index(p) -> None:
    """Keep the newest row per (skill, proposal_id) pair. Never raises.

    v1.8.46 (audit R17): the whole read -> rewrite -> `os.replace` sequence
    runs under `_APPROVALS_INDEX_LOCK`, the same lock `_append_approvals_index`
    takes. Compaction replaces the file wholesale, so a row appended between
    its read and its replace used to vanish with no error and no log line -
    and this file is the durable record that survives log rotation. The lock
    also makes the append atomic with respect to a concurrent compaction.

    The tmp file is cleaned up on failure so a repeated crash cannot litter
    `logs/runs/`.
    """
    tmp = None
    try:
        with _APPROVALS_INDEX_LOCK:
            newest: dict[str, dict[str, Any]] = {}
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    key = f"{row.get('skill') or ''}|{row.get('proposal_id') or ''}"
                    prior = newest.get(key)
                    if prior is None or _parse_ts(row.get("ts")) >= _parse_ts(
                            prior.get("ts")):
                        newest[key] = row
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for row in newest.values():
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
            tmp = None
    except Exception:
        if tmp is not None:
            try:
                if tmp.is_file():
                    tmp.unlink()
            except Exception:
                pass


def _read_approvals_index() -> list[dict[str, Any]] | object:
    """Rows from the durable index, oldest first.

    v1.8.43 (re-audit R4): returns `_UNREADABLE` when the index exists but
    could not be read, distinct from `[]` meaning "no rows". An absent index
    is genuinely empty; an unreadable one is a fault, and conflating them
    disarmed the rejection rail.
    """
    try:
        p = _approvals_index_path()
    except Exception:
        return _UNREADABLE
    if p is None:
        return _UNREADABLE
    try:
        if not p.is_file():
            return []          # genuinely no index -> no rows
    except Exception:
        return _UNREADABLE
    try:
        out: list[dict[str, Any]] = []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if isinstance(row, dict):
                    out.append(row)
        return out
    except Exception:
        return _UNREADABLE


def _log_generations():
    """governance.log plus its rotated generations, **oldest first**.

    Defence in depth for audit M2: even if the index is missing (an install
    upgraded from before v1.8.38), a decision that survived in `.1` is still
    found.

    v1.8.49 (audit X2): this helper's docstring has claimed "oldest first"
    since it was written while the code returned `[base, .1]` — base being the
    NEWEST generation, because rotation moves the old content into `.1` and
    replaces the live file with an empty one. v1.8.48 then added a
    `reversed(_log_generations())` "newest first" scan for the
    `min_interval_seconds` rail, which inherited the wrong order and so took
    the OLDEST eligible decision's timestamp: an older `last_ts` makes
    `(now - last_ts)` LARGER, so the rail waved through exactly the window it
    exists to close. The order now matches the documented contract
    (`[.1, base]`), which also fixes the dashboard's stale
    `last_decisions` for the same reason. Every consumer is either
    timestamp-max (order-independent) or explicitly reverses the result, so
    making the helper honest is safe.
    """
    try:
        base = _runs_dir() / "governance.log"
        rotated = None
        try:
            rotated = base.with_suffix(base.suffix + ".1")
        except Exception:
            pass
        paths = [rotated, base] if rotated is not None else [base]
        return [p for p in paths if p.is_file()]
    except Exception:
        return []


def _iter_governance_rows():
    """Yield every decision row across log generations, oldest first.

    v1.8.43 (re-audit R4): sets a module flag when a generation exists but
    could not be read, so `_last_human_decision` can tell "no rows" from
    "could not read". A generator cannot return a value, so the fault is
    reported through `_LOG_READ_FAILED`.
    """
    global _LOG_READ_FAILED
    for p in _log_generations():
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict):
                        yield row
        except Exception:
            # The file EXISTS (it came from _log_generations) but could not be
            # read. That is a fault, not an empty log.
            _LOG_READ_FAILED = True
            continue


def mark_human_decision(
    skill_name: str,
    approved: bool,
    decided_by: str = "user",
    *,
    proposal_id: str | None = None,
    summary_id: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a human approval/rejection. Returns `{ok, ts, decided_by, error}`.

    v1.8.34: `proposal_id` / `summary_id` / `note` are OPTIONAL keyword-only
    additions, defaulting to None so every existing caller and assertion is
    unchanged. They exist to close the proposal<->decision gap this file
    never had: the ledger was per-SKILL, so a reviewer approving "skill X"
    left no record of WHICH proposal they had actually read. With
    `proposal_id` set, `find_pending_proposal()` can tell a pending decision
    from a decided one, and the audit trail names the exact document.

    `decided_by` is already the discriminator between a human and a machine
    decision (autonomous adoptions record `decided_by="autonomous"`), so a
    new field is NOT added for that — one queryable history beats two.
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry = {
        "ts_iso": ts,
        "skill": str(skill_name or ""),
        "event": "human_decision",
        "approved": bool(approved),
        "decided_by": str(decided_by or "user"),
    }
    if proposal_id:
        entry["proposal_id"] = str(proposal_id)
    if summary_id:
        entry["summary_id"] = str(summary_id)
    if note:
        entry["note"] = str(note)[:500]
    # v1.8.45 (R20): bump the generation BEFORE the write, so a concurrent
    # reader either sees the pre-write cache or re-scans - never a cached
    # view that predates the decision it is about to render.
    _APPEND_GENERATION[0] += 1
    _invalidate_decided_cache()
    res = _append_log(entry)
    # v1.8.38 (audit M2): mirror into the durable index so log rotation
    # cannot silently revoke this approval. Same function, so still one
    # writer; best-effort, and a failure here does NOT fail the decision.
    _append_approvals_index(entry)
    return {
        "ok": bool(res.get("ok")),
        "ts": ts,
        "decided_by": entry["decided_by"],
        "proposal_id": entry.get("proposal_id"),
        "error": res.get("error"),
    }


def find_pending_proposal(skill_name: str) -> dict[str, Any] | None:
    """The staged proposal this skill's pending approval is actually about.

    v1.8.34. Returns `{proposal_id, staged_at, decision}` or None when the
    skill has no staged proposal, or every staged proposal for it has
    already been decided.

    This is the missing link between two disjoint stores: `staging/` keys on
    the proposal filename stem, while `logs/runs/governance.log` keys on the
    skill. Newest staged proposal first; a proposal counts as decided when a
    `human_decision` row names it explicitly, or when a cycle_history /
    adoptions row records it as adopted or rejected.

    Never raises: every failure degrades to None, which the callers render
    as "no pending proposal" rather than as an error state.
    """
    if not skill_name:
        return None
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        try:
            from helpers import sleep_runner  # type: ignore
        except Exception:
            return None
    try:
        staged = sleep_runner.find_staged_proposals()
    except Exception:
        return None
    try:
        mine = [
            p for p in staged
            if (p.stem if p.suffix in (".md", ".proposed") else p.name) == skill_name
        ]
    except Exception:
        return None
    if not mine:
        return None
    try:
        mine.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        mine = sorted(mine, key=lambda p: p.name, reverse=True)
    decided = _decided_proposal_ids()
    try:
        for p in mine:
            pid = p.stem if p.suffix in (".md", ".proposed") else p.name
            try:
                mtime = p.stat().st_mtime
            except Exception:
                mtime = 0.0
            decision = decided.get(pid)
            if decision is None:
                return {"proposal_id": pid, "staged_at": mtime,
                        "decision": "pending", "source": str(p)}
            # Already decided — report the most recent decision so the UI
            # can say "approved 3 minutes ago" instead of re-prompting.
            return {"proposal_id": pid, "staged_at": mtime,
                    "decision": decision.get("outcome") or "decided",
                    "decided_at": decision.get("ts"),
                    "decided_by": decision.get("decided_by"),
                    "source": str(p)}
    except Exception:
        return None


# v1.8.45 (re-audit R20): memo for `_decided_proposal_ids`.
#
# The ledger is append-only between rotations, and every READ of it is a full
# file scan, so caching it for a couple of seconds turns the dashboard's
# per-skill lookup from O(skills x ledger) into O(ledger) once per window.
# 21 governed skills x a full read each, on every page refresh, was the cost.
#
# Short TTL on purpose: a decision made a moment ago must be visible to the
# card that is about to render it. `_append_log` bumps the generation on
# every write, so a write invalidates the cache immediately rather than
# waiting out the TTL - which is the case that actually matters (approve,
# then refresh).
_DECIDED_CACHE: dict[str, Any] = {"generation": -1, "at": 0.0, "value": None}
_DECIDED_CACHE_TTL = 2.0
_APPEND_GENERATION = [0]


def _invalidate_decided_cache() -> None:
    """Drop the memo. Called on every ledger append and on rotation."""
    _DECIDED_CACHE["value"] = None
    _DECIDED_CACHE["at"] = 0.0


def _decided_proposal_ids() -> dict[str, dict[str, Any]]:
    """Memoised view of `_scan_decided_proposal_ids()`.

    v1.8.45 (R20): see `_DECIDED_CACHE`. Returns the SAME dict object to
    every caller within the window, so callers must not mutate it -
    `_pending_approval_map` and `find_pending_proposal` only read.
    """
    now = time.time()
    gen = _APPEND_GENERATION[0]
    if (_DECIDED_CACHE["generation"] == gen
            and _DECIDED_CACHE["value"] is not None
            and (now - _DECIDED_CACHE["at"]) < _DECIDED_CACHE_TTL):
        return _DECIDED_CACHE["value"]
    value = _scan_decided_proposal_ids()
    _DECIDED_CACHE["generation"] = gen
    _DECIDED_CACHE["at"] = now
    _DECIDED_CACHE["value"] = value
    return value


def _scan_decided_proposal_ids() -> dict[str, dict[str, Any]]:
    """Map proposal_id -> the most recent decision row naming it.

    Reads every source that can close a pending approval:
    `logs/runs/governance.log` **and its rotated generations**, the durable
    `approvals_index.jsonl`, and `cycle_history.jsonl` (adopted / rejected
    outcomes). Never raises; an unreadable source contributes nothing.

    v1.8.45 (re-audit R8): this used to read the LIVE log only. After the 5 MB
    rotation that is exactly the disagreement the child DOX warns about —
    `_human_approved` (which reads the index and the generations) knew an
    approval existed while `find_pending_proposal` reported the proposal as
    `"pending"`, so the Governance card re-prompted for a change already
    decided. It also called `_runs_dir()` OUTSIDE its try, and `_runs_dir()`
    ends in `mkdir` — the F8 pattern, third occurrence.

    So this now reuses `_iter_governance_rows()` (generations, and a read
    fault flag) and `_read_approvals_index()` (durable, tri-state), which is
    what keeps the card and the gate reading the same history.
    """
    out: dict[str, dict[str, Any]] = {}

    def offer(pid: Any, outcome: str, ts: Any, decided_by: Any) -> None:
        if not pid:
            return
        prior = out.get(str(pid))
        if prior is not None and _parse_ts(prior.get("ts")) > _parse_ts(ts):
            return                      # keep the newer decision
        out[str(pid)] = {"outcome": outcome, "ts": ts, "decided_by": decided_by}

    # --- governance.log, across its generations -----------------------
    try:
        for e in _iter_governance_rows():
            if e.get("event") != "human_decision":
                continue
            offer(e.get("proposal_id"),
                  "approved" if e.get("approved") else "denied",
                  e.get("ts_iso") or e.get("ts"),
                  e.get("decided_by"))
    except Exception:
        pass

    # --- the durable index (survives rotation) -------------------------
    try:
        rows = _read_approvals_index()
        if rows is not _UNREADABLE:
            for row in rows or []:
                offer(row.get("proposal_id"),
                      "approved" if row.get("approved") else "denied",
                      row.get("ts"),
                      row.get("decided_by"))
    except Exception:
        pass

    # --- cycle_history: adopted / rejected outcomes --------------------
    try:
        from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
    except Exception:
        try:
            from helpers import cycle_history  # type: ignore
        except Exception:
            cycle_history = None  # type: ignore[assignment]
    if cycle_history is not None:
        try:
            for entry in cycle_history.read_cycle_history(limit=300) or []:
                if not isinstance(entry, dict):
                    continue
                pid = entry.get("proposal_id")
                if not pid:
                    continue
                outcome = str(entry.get("outcome") or "")
                if outcome not in ("adopted", "rejected"):
                    continue
                # Only overwrite a governance row when this entry is NEWER.
                # v1.8.38 (audit M1): compared as PARSED EPOCHS. The two
                # ledgers write different formats (microsecond ISO+UTC here,
                # fraction-less strftime in LOCAL time there), so the old
                # string compare was biased by `'.' > '+'` and by the
                # differing offsets - it could attribute an automatic adoption
                # to a human approval, or hide a rejection.
                prior = out.get(str(pid))
                if prior is not None:
                    prior_ts = _parse_ts(prior.get("ts"))
                    entry_ts = _parse_ts(entry.get("ts"))
                    if prior_ts > entry_ts:
                        continue
                out[str(pid)] = {
                    "outcome": outcome,
                    "ts": entry.get("ts"),
                    "decided_by": entry.get("source") or "auto_loop",
                }
        except Exception:
            pass
    return out


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
        paused: list[str] = []
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
                        _pu2 = entry / ".skillopt.pause_until"
                        if _pu2.is_file():
                            try:
                                _praw = _pu2.read_text(encoding="utf-8").strip()
                                if _praw.replace(".", "", 1).isdigit():
                                    _puntil = float(_praw)
                                else:
                                    _puntil = _parse_pause_until(_praw)
                                if _puntil and time.time() < _puntil:
                                    paused.append(entry.name)
                            except Exception:
                                pass
                    if (entry / ".skillopt.policy.json").is_file():
                        governed.append(entry.name)
            except Exception:
                pass
        log = _runs_dir() / "governance.log"
        last_decisions: dict[str, dict[str, Any]] = {}
        # v1.8.48 (audit G5): this used to open the LIVE generation by hand.
        # `governance.log` rotates at 5 MB and the replacement is created
        # EMPTY, so between rotations this reported `last_decisions: {}` and
        # `file_size_bytes: 0` while `_last_human_decision` - used by the same
        # route - still saw the decision. Two readers of one ledger is exactly
        # how R8 happened. Go through the shared reader, which walks every
        # generation plus the durable index.
        try:
            for row in reversed(list(_iter_governance_rows())):
                try:
                    sk = row.get("skill")
                except Exception:
                    continue
                if not sk or sk in last_decisions:
                    continue
                if row.get("event") != "decision":
                    continue
                last_decisions[sk] = {
                    "ts": row.get("ts_iso") or row.get("ts"),
                    "eligible": row.get("eligible"),
                    "reason": row.get("reason"),
                }
        except Exception:
            pass
        try:
            _log_bytes = sum(
                p.stat().st_size for p in _log_generations()
                if p.is_file()
            )
        except Exception:
            _log_bytes = 0
        return {
            "available": True,
            "stuck": get_stuck_skills(),
            "enabled": True,
            "default_policy": default_policy,
            "opted_out": opted_out,
            "opted_in": opted_in,
            "governed": governed,
            "last_decisions": last_decisions,
            "paused": paused,
            "skills_dir": str(skills_root),
            "log_path": str(log),
            "file_size_bytes": _log_bytes,
        }
    except Exception as e:
        return {"available": False, "enabled": True, "error": str(e)}



# ----------------------------------------------------------------------- #
# One-click pause/resume (v1.8.8, roadmap open question 5)
# ----------------------------------------------------------------------- #

def pause_skill(skill_name: str, hours: float = 24.0) -> dict[str, Any]:
    # v1.8.8: write .skillopt.pause_until (epoch seconds). The marker is
    # the same gate check_skill_eligible step 1.5 already honors, so the
    # auto-loop skips the skill immediately. hours clamped 0.25..720.
    try:
        if not skill_name:
            return {'ok': False, 'error': 'missing skill'}
        try:
            _h = float(hours)
        except (TypeError, ValueError):
            _h = 24.0
        _h = min(max(_h, 0.25), 720.0)
        until = time.time() + _h * 3600.0
        sd = _skill_dir(skill_name)
        if _TEST_SKILLS_DIR is None and not sd.is_dir():
            return {'ok': False, 'error': 'unknown skill dir'}
        sd.mkdir(parents=True, exist_ok=True)
        (sd / '.skillopt.pause_until').write_text(str(until), encoding='utf-8')
        try:
            from datetime import datetime as _dt, timezone as _tz
            _iso = _dt.now(_tz.utc).isoformat()
        except Exception:
            _iso = ''
        _append_log({'event': 'pause', 'skill': skill_name,
                     'until_ts': until, 'until_iso': _iso,
                     'hours': _h, 'by': 'dashboard'})
        return {'ok': True, 'skill': skill_name, 'until': until, 'hours': _h}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def resume_skill(skill_name: str) -> dict[str, Any]:
    # v1.8.8: remove .skillopt.pause_until (idempotent). was_paused tells
    # the caller whether a marker was actually removed.
    try:
        if not skill_name:
            return {'ok': False, 'error': 'missing skill'}
        marker = _skill_dir(skill_name) / '.skillopt.pause_until'
        was = marker.is_file()
        if was:
            marker.unlink()
        _append_log({'event': 'resume', 'skill': skill_name,
                     'was_paused': was, 'by': 'dashboard'})
        return {'ok': True, 'skill': skill_name, 'was_paused': was}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def reset_for_tests() -> None:
    """Wipe test state. Called by smoke tests in setUp/tearDown.

    v1.8.38 (audit M2): must also remove `approvals_index.jsonl`. The
    durable index added in v1.8.38 is a NEW production-state file, and
    `reset_for_tests` wiping only `governance.log` left it accumulating test
    fixtures across runs - so `t_c4_governance_approve_endpoint`'s
    "before approval" assertion saw the PREVIOUS run's approval and the
    skill came back eligible. A test-state file that outlives its test is
    exactly the leak class the child DOX warns about; whenever a new file
    lands in `logs/runs/`, add it here AND to the pollution guard.
    """
    set_skills_dir_for_tests(None)
    for name in ("governance.log", "governance.log.1", "approvals_index.jsonl"):
        try:
            p = _runs_dir() / name
            if p.is_file():
                p.unlink()
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
        # v1.8.1: rotate when large (see sleep_runner.rotate_log_if_large).
        try:
            from usr.plugins.skillopt.helpers import sleep_runner as _sr  # type: ignore
        except Exception:
            try:
                from helpers import sleep_runner as _sr  # type: ignore
            except Exception:
                _sr = None  # type: ignore[assignment]
        if _sr is not None:
            try:
                _sr.rotate_log_if_large(out_path)
            except Exception:
                pass
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


def _read_log_tail(log: Path, max_bytes: int = 262_144) -> list[str]:
    """Read the last ~max_bytes of a JSONL decision log as lines.

    v1.8.1: the two lookups below used ``f.readlines()`` on the WHOLE
    governance.log on every eligibility check for every candidate skill —
    O(log size) per tick, forever growing. Reading a bounded tail is enough
    for "most recent entry wins" lookups (entries older than the tail are
    far past any min_interval window). Best-effort: returns [] on error.
    """
    try:
        import os as _os
        size = log.stat().st_size
        with open(log, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        # Drop the (possibly truncated) first line when we seeked.
        if size > max_bytes and lines:
            lines = lines[1:]
        return lines
    except Exception:
        return []


def _last_eligible_decision_ts(skill_name: str) -> float | None:
    """Find the most recent eligible=True decision ts for this skill
    in governance.log, or None if not found.

    v1.8.35 (audit F8): `_runs_dir()` is called INSIDE the try. It ends in
    `mkdir(parents=True, exist_ok=True)`, so on a full disk, a read-only
    mount, or a permissions change it raises OSError - and the caller in
    auto_loop treats any exception from this module as "fall through to
    eligible", which would silently disable every governance rail for that
    cycle. A rail that cannot be read must not be reported as absent."""
    try:
        # v1.8.48 (audit G7): the `min_interval_seconds` rail read the LIVE
        # generation only. `governance.log` rotates at 5 MB and the
        # replacement is empty, so for one tick after every rotation the rail
        # saw "no decision on record" and waved through - one of the six
        # autonomy-immune rails, silently inert. Search every generation.
        #
        # v1.8.49 (audit X2): `reversed()` is correct ONCE the helper honours its
        # contract. Before this release `_log_generations()` returned
        # `[base, .1]` - base being the NEWEST generation, since rotation
        # moves the old content into `.1` - so `reversed()` walked the OLDEST
        # first and returned the oldest eligible decision: an older `last_ts`
        # makes `(now - last_ts)` LARGER, so the rail waved through exactly
        # the window it exists to close. The helper now returns `[.1, base]`
        # as documented, so this scan is newest-generation-first.
        for gen in reversed(_log_generations()):
            for line in reversed(_read_log_tail(gen)):
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
                _ts = _parse_ts(ts_str)
                if _ts:
                    return _ts
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
        # v1.8.37 (audit F13): fully wired, so the governance-level budget
        # check honours the same cap as the drain.
        #
        # v1.8.45 (re-audit R9): `cfg` MUST be passed. Called with no
        # argument, the factory fell back to reading the registry itself -
        # which on an HTTP request thread means (a) `budget.enabled` was
        # inert on this path, and (b) `reset_hour_utc` fell back to the
        # module default of 00:00 while the drain used the operator's value.
        # Two different DAILY WINDOWS for one skill, so the governance check
        # and the drain could disagree about whether the budget was spent.
        #
        # It also made an HTTP thread a READER of the budget state, which
        # the single-writer rule wants avoided; passing the caller's config
        # removes the extra read entirely.
        try:
            from usr.plugins.skillopt.helpers import sleep_runner as _sr_cfg  # type: ignore
        except Exception:
            try:
                from helpers import sleep_runner as _sr_cfg  # type: ignore
            except Exception:
                _sr_cfg = None  # type: ignore[assignment]
        _cfg = None
        if _sr_cfg is not None:
            try:
                _cfg = _sr_cfg.merged_config()
            except Exception:
                _cfg = None
        tracker = budget.tracker_from_config(skill_name, _cfg)
        if tracker is None:
            return 0
        s = tracker.get_status()
        # v1.8.1 fix: get_status() returns `daily_total_cents` (the old key
        # `spent_today_cents` never existed, so the cap never triggered).
        return int(s.get("daily_total_cents", s.get("spent_today_cents", 0)) or 0)
    except Exception:
        return None


def _last_human_decision(skill_name: str) -> tuple[bool, float] | None:
    """`(approved, ts_epoch)` for the most recent HUMAN decision, else None.

    The single reader behind both `_human_approved` and the v1.8.36
    rejection rail, so the approval gate and the rail can never disagree
    about what the most recent human decision was.

    "Human" excludes the machine values in `MACHINE_DECIDED_BY` - an
    autonomous adoption must not read as a human decision (audit F9).
    Rows predating `decided_by` default to human.

    Never raises. Returns None when the log is missing, unreadable, or holds
    no human decision for this skill.

    v1.8.38 (audit M2): reads the durable approvals index as well as the live
    log and its rotated generation. `governance.log` is rotated at 5 MB with
    the old generation DELETED, so before this an approval older than one
    rotation window silently vanished and the skill reverted to *pending*.
    The index is written by `mark_human_decision` alongside the log and is
    never rotated, so it is the durable answer; the log is still consulted
    (newest wins) so a pre-v1.8.38 install with no index keeps working.
    """
    global _LOG_READ_FAILED
    _LOG_READ_FAILED = False
    try:
        best: tuple[bool, float] | None = None
        best_ts = -1.0

        def offer(approved: bool, ts: float) -> None:
            nonlocal best, best_ts
            if best is None or ts >= best_ts:
                best, best_ts = (approved, ts), ts

        for row in _iter_governance_rows():
            if row.get("skill") != skill_name:
                continue
            if row.get("event") != "human_decision":
                continue
            decided_by = str(row.get("decided_by") or "user").strip().lower()
            if decided_by in MACHINE_DECIDED_BY:
                continue
            offer(bool(row.get("approved")),
                  _parse_ts(row.get("ts_iso") or row.get("ts")))

        index_rows = _read_approvals_index()
        if index_rows is _UNREADABLE:
            return _UNREADABLE
        for row in index_rows or []:
            if row.get("skill") != skill_name:
                continue
            decided_by = str(row.get("decided_by") or "user").strip().lower()
            if decided_by in MACHINE_DECIDED_BY:
                continue
            offer(bool(row.get("approved")), _parse_ts(row.get("ts")))

        if _LOG_READ_FAILED:
            # A generation existed but could not be read: we may have missed
            # the very row that matters. Report the fault rather than a
            # confident "no decision".
            return _UNREADABLE
        return best
    except Exception:
        return _UNREADABLE


def _newest_staged_mtime(skill_name: str) -> float | None | object:
    """mtime of the newest staged proposal for this skill.

    v1.8.43 (re-audit R4): returns `_UNREADABLE` when the staging queue could
    not be enumerated, distinct from `None` meaning "nothing staged". The
    rejection rail needs the difference: "nothing staged" makes the rail moot,
    while "could not tell" must be restrictive.
    """
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except Exception:
        try:
            from helpers import sleep_runner  # type: ignore
        except Exception:
            return _UNREADABLE
    try:
        best = None
        for p in sleep_runner.find_staged_proposals():
            if (p.stem if p.suffix in (".md", ".proposed") else p.name) \
                    != skill_name:
                continue
            try:
                mt = float(p.stat().st_mtime)
            except Exception:
                # The file was listed but its mtime is unreadable; treat the
                # queue as unevaluable rather than as empty.
                return _UNREADABLE
            if best is None or mt > best:
                best = mt
        return best
    except Exception:
        return _UNREADABLE


def _human_rejected_current_proposal(skill_name: str) -> bool:
    """Has a human said NO to the proposal that is staged right now?

    v1.8.36 (audit F2). A denial used to be recorded as an approval-shaped
    flag only: `_human_approved()` returned False, which parks the skill at
    `require_human_approval_pending` in MANUAL mode - but the autonomous
    override skips that very gate, so the drain re-read the same rejected file
    and wrote it into the live SKILL.md. A human "no" was advisory.

    This is the rail that makes it binding, and it is deliberately NOT part
    of the approval gate: autonomy may bypass "has nobody approved this?"
    because that is what autonomy means, but it must never bypass "a human
    explicitly refused this document".

    Staleness: the denial must post-date the staged file. A NEW proposal for
    the same skill is a different document and is judged on its own merits -
    otherwise one rejection would permanently mute the skill.

    The comparison carries a small tolerance, and it is not cosmetic. The
    ledger stores `datetime.isoformat()`, which truncates to MICROSECONDS,
    while `st_mtime` is a float carrying the filesystem's full resolution
    (nanoseconds on NTFS/ext4). The two therefore disagree by up to a
    microsecond for a write that causally happened first, and a bare
    `ts >= staged_at` made the rail silently inert in exactly the case it
    exists for. `_REJECTION_MTIME_TOLERANCE_S` absorbs the precision gap and
    nothing else: it is orders of magnitude below any real re-stage interval.
    """
    try:
        decision = _last_human_decision(skill_name)
        # v1.8.43 (re-audit R4): the two failure readings are now DISTINCT.
        #   _UNREADABLE -> the ledger or index could not be read. A denial may
        #                  exist and be invisible, and this is the path to a
        #                  live file, so the restrictive reading wins.
        #   None        -> genuinely no human decision on record. Nothing to
        #                  enforce.
        if decision is _UNREADABLE:
            return True
        if not decision:
            return False
        approved, ts = decision
        if approved:
            return False
        staged_at = _newest_staged_mtime(skill_name)
        if staged_at is _UNREADABLE:
            # Cannot prove the denial is stale, so do not wave it through.
            return True
        if staged_at is None:
            # Nothing staged: there is nothing to adopt, so the rail is moot.
            return False
        # A denial with no usable timestamp cannot be proven newer, and the
        # restrictive reading is the safe one.
        if ts <= 0.0:
            return True
        return ts >= (staged_at - _REJECTION_MTIME_TOLERANCE_S)
    except Exception:
        # Undecidable => restrictive. This is the path to a live file.
        return True


def _human_approved(skill_name: str) -> bool:
    """Did a HUMAN approve this skill? True only for `decided_by == "user"`.

    v1.8.35 (audit F9): this used to accept ANY `human_decision` row, which
    meant an `autonomous` row - written by the drain's own audit hook through
    this same function - satisfied `require_human_approval`. Switching back to
    manual after one autonomous adoption therefore passed the human gate with
    no human ever having clicked Approve, and nothing in the UI distinguished
    the two. The gate must honour the discriminator it depends on.

    The test is "was this decided by a MACHINE?", not "is it literally the
    string 'user'?": `decided_by` is free-form and a caller may record a named
    operator (the existing suite uses "alice", and
    `api/governance_approve.py` accepts any `decided_by`). Blocking anything
    that is not exactly "user" would invalidate every named-human approval, so
    only the values the plugin itself writes for machine decisions are
    excluded. Rows predating the field default to human.

    v1.8.35 (audit F8): `_runs_dir()` moved inside the `try` for the same
    reason as `_last_eligible_decision_ts` - it ends in a `mkdir` that raises
    on a full disk or a read-only mount, and a raise here reached auto_loop's
    fail-open handler and disabled every governance rail for that cycle.

    v1.8.36: the scan itself now lives in `_last_human_decision`, shared with
    the rejection rail so the two can never disagree.
    """
    try:
        decision = _last_human_decision(skill_name)
    except Exception:
        return False
    # v1.8.43 (R4): `_UNREADABLE` is not a tuple, so it must be checked
    # BEFORE the subscript. It means "no approval could be established",
    # which is the same restrictive answer as "not approved" - and that is
    # the fail-CLOSED direction for this gate.
    if decision is _UNREADABLE:
        return False
    try:
        return bool(decision[0]) if decision else False
    except Exception:
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
