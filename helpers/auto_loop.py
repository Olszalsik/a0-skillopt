"""
SkillOpt auto-loop - background daemon that runs SkillOpt Sleep
periodically, then auto-adopts proposals that pass the validation gate.

Started by `hooks.py:install()` when the plugin is enabled and
auto_loop_enabled is true in the config. Stops cleanly when
`hooks.py:uninstall()` is called or when the config is toggled off.

The thread is a daemon so it never blocks A0 shutdown. It does NO
LLM work itself - it just launches `python -m skillopt_sleep` as a
detached subprocess and tails the result.

Behaviour (configurable via /api/plugins/skillopt/config):
- auto_loop_enabled (default True) master kill switch
- auto_loop_interval_sec (default 1800) how often to wake up
- auto_loop_min_rollouts (default 10) new rollouts before a cycle is worth running
- auto_loop_skill_target (default "all") which skill to focus on
- auto_adopt (default False) auto-promote staged proposals that pass the gate
- gate_min_improvement_pp (default 0.0) minimum held-out improvement to accept
- gate_min_chars (default 200) minimum proposal length to accept
- gate_max_shrink_ratio (default 0.5) reject proposals that shrink by more

v1.1.0 changes:
- Uses the shared `validate_proposal()` from sleep_runner (with
  whitespace-normalised equality check, mandatory example block, and
  shrink ceiling). The old hollow gate let a 1904->1904 'no-op'
  adoption through.
- Parses the Sleep engine's `held-out X -> Y` from the run log and
  feeds it into the gate (so `gate_min_improvement_pp` is real).
- Failures are written to BOTH auto_loop.log AND a small JSON file
  the dashboard / status endpoint reads. The dashboard no longer
  shows green when the loop is silently broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

try:
    from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
except Exception:
    from helpers import sleep_runner  # type: ignore  # noqa: F401
try:
    from usr.plugins.skillopt.helpers import cadence  # type: ignore
    from usr.plugins.skillopt.helpers import budget    # type: ignore
except ImportError:
    cadence = None
    budget = None


PLUGIN_NAME = "skillopt"
LOOP_STATE_FILENAME = ".auto_loop_state.json"
LAST_ERROR_FILENAME = ".auto_loop_last_error.json"


# ----------------------------------------------------------------------- #
# State persistence (survive A0 restarts)
# ----------------------------------------------------------------------- #

def _state_path() -> Path:
    return sleep_runner.runs_dir() / LOOP_STATE_FILENAME


def _load_state() -> dict[str, Any]:
    p = _state_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "last_cycle_at": 0.0,
        "last_cycle_verb": None,
        "last_rollout_count_at_cycle": 0,
        "cycles_run": 0,
        "proposals_adopted": 0,
        "proposals_rejected": 0,
        "running": False,
        "last_error": None,
        "last_engine": None,
        # v1.8.22: mirror of the live real-gate sidecar (dashboard only —
        # the sidecar file is the source of truth across restarts).
        "real_gate": None,
    }


def _save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_error(exc: BaseException, where: str) -> None:
    """Persist the most recent error so the dashboard can surface it."""
    payload = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "where": where,
        "type": type(exc).__name__,
        "message": str(exc),
    }
    try:
        p = sleep_runner.runs_dir() / LAST_ERROR_FILENAME
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# ----------------------------------------------------------------------- #
# The daemon thread
# ----------------------------------------------------------------------- #

class AutoLoopThread(threading.Thread):
    """Background thread that periodically runs Sleep + auto-adopt."""

    def __init__(self, get_config, stop_event: threading.Event | None = None):
        super().__init__(name="skillopt-auto-loop", daemon=True)
        self.get_config = get_config
        self._stop_event = stop_event or threading.Event()
        # v1.8.18 (P1, audit RC2): the in-memory `self._last_rollout_count`
        # per-tick delta is gone - the trigger baseline is now the PERSISTED
        # state key `last_rollout_count_at_cycle` (survives restarts, only
        # advances when a cycle actually runs). See _tick().

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Main loop. Returns when stop() is called."""
        state = _load_state()
        state["running"] = True
        _save_state(state)
        try:
            while not self._stop_event.is_set():
                # v1.8.18 (P0, audit RC8): timestamp every tick attempt so
                # the dashboard can tell "alive and ticking" apart from a
                # thread that silently died (last_tick_at goes stale).
                state["last_tick_at"] = time.time()
                cfg = self._safe_config()
                if not cfg:
                    # No config yet - wait and try again. v1.8.18: surface
                    # this on the state file (previously a SILENT spin with
                    # zero logs when config.json resolved empty - the outer
                    # loop never ticked again, audit 2026-09-22).
                    if not state.get("cfg_empty_since"):
                        state["cfg_empty_since"] = time.time()
                        self._log(
                            "auto-loop: config resolved empty - idling until "
                            "a non-empty config is saved (defaults merge "
                            "should prevent this; see audit RC1)"
                        )
                    _save_state(state)
                    self._sleep(30)
                    continue
                if state.get("cfg_empty_since"):
                    state["cfg_empty_since"] = None
                    _save_state(state)
                if not cfg.get("auto_loop_enabled", True):
                    # Master kill switch
                    self._sleep(60)
                    continue
                try:
                    self._tick(cfg, state)
                except Exception as e:
                    # Never let the thread die; record so the dashboard can see it
                    self._log(f"tick error: {e}")
                    _record_error(e, "tick")
                    state["last_error"] = f"{type(e).__name__}: {e}"
                interval = max(60, int(cfg.get("auto_loop_interval_sec", 1800)))
                self._sleep(interval)
        finally:
            state["running"] = False
            _save_state(state)

    # ----------------------------------------------------------------- #

    def _safe_config(self) -> dict[str, Any]:
        try:
            return self.get_config() or {}
        except Exception as e:
            self._log(f"config read failed: {e}")
            _record_error(e, "config")
            return {}

    def _tick(self, cfg: dict[str, Any], state: dict[str, Any]) -> None:
        """One iteration: maybe launch a Sleep cycle, maybe auto-adopt."""
        # 1. Apply opt-in rollout retention before counting. The trigger is
        # count-based, so keep its persisted baseline aligned when old files
        # are removed; otherwise a lower count could suppress future cycles.
        try:
            retention = sleep_runner.enforce_rollout_retention(cfg)
            removed = int(retention.get("deleted", 0))
            if removed:
                base_before_prune = int(state.get("last_rollout_count_at_cycle", 0) or 0)
                state["last_rollout_count_at_cycle"] = max(0, base_before_prune - removed)
                self._log(
                    f"auto-loop: rollout retention removed {removed} expired file(s); "
                    f"baseline {base_before_prune} -> {state['last_rollout_count_at_cycle']}"
                )
            if retention.get("errors"):
                self._log(f"auto-loop: rollout retention had {retention['errors']} file error(s)")
        except Exception as e:
            # A cleanup problem cannot block harvesting or optimization.
            self._log(f"auto-loop: rollout retention failed: {e}")

        # Count rollouts after any expiry cleanup.
        rollout_count = len(sleep_runner.list_rollouts())
        # v1.8.18 (P1, audit RC2): count rollouts accumulated SINCE THE LAST
        # CYCLE (persisted in state), not since the previous tick. The old
        # per-tick window (default >=10 new rollouts inside one 30-min tick)
        # almost never fired at real harvest rates (~25 rollouts/day); the
        # audit log showed `new_rollouts=3 < threshold=10` as its last line
        # for days. A persisted base also makes a fresh boot treat the
        # accumulated backlog as eligible (first cycle fires immediately).
        base = int(state.get("last_rollout_count_at_cycle", 0) or 0)
        new_rollouts = max(0, rollout_count - base)
        min_new = int(cfg.get("auto_loop_min_rollouts", 10))

        # v1.8.18 (P1, audit RC3): auto-opt-in MUST NOT live only inside the
        # (historically never-firing) cycle branch. A skill seen in rollouts
        # gets its optin marker on the very next tick, not "whenever a cycle
        # eventually fires". Scan only when new rollouts exist (cheap tick).
        if new_rollouts > 0:
            try:
                for skill in self._candidate_skills(
                    (cfg.get("auto_loop_skill_target") or "").strip() or None,
                    records=self._load_rollout_records(),
                ):
                    self._maybe_auto_optin(skill, cfg)
            except Exception as e:
                # Defensive: a marker bug can never stall the tick.
                self._log(f"auto-loop: early auto-optin failed: {e}")
                _record_error(e, "early_optin")

        # 2. Maybe launch a Sleep cycle
        if new_rollouts >= min_new:
            target = (cfg.get("auto_loop_skill_target") or "").strip() or None
            self._log(
                f"auto-loop: tick "
                f"(new_rollouts={new_rollouts}, threshold={min_new}, target={target})"
            )
            try:
                self._run_cycle_for_eligible_skills(
                    target, cfg, state, rollout_count,
                    records=self._load_rollout_records(),
                )
            except Exception as e:
                self._log(f"cycle failed: {e}")
                _record_error(e, "cycle")
                raise
        else:
            self._log(
                f"auto-loop: skipping cycle "
                f"(new_rollouts={new_rollouts} < threshold={min_new})"
            )

        # v1.5.0: Record a tick-level cycle_history entry so the dashboard
        # has visibility even when no skill is eligible or no cycle fires.
        # Best-effort: a cycle_history bug can never crash the auto-loop.
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore # noqa: F401
        except Exception:
            from helpers import cycle_history  # type: ignore # noqa: F401
        try:
            cycle_history.record_cycle_entry({
                "skill": "_tick",
                "outcome": "tick",
                "outcome_detail": f"rollouts={rollout_count} new={new_rollouts} threshold={min_new}",
                "gate_reasons": [],
                "gate_stages_passed": [],
                "llm_calls": 0,
                "runtime_seconds": 0.0,
                "links": {
                    "rollout_count": rollout_count,
                    "new_rollouts": new_rollouts,
                    "min_rollouts": min_new,
                },
            })
        except Exception as e:
            self._log(f"cycle_history tick entry failed: {e}")

        # 3. Maybe auto-adopt
        if cfg.get("auto_adopt", False):
            self._auto_adopt(state, cfg)

    # ----------------------------------------------------------------- #
    # v1.6.0: engine selection + per-skill gating
    # ----------------------------------------------------------------- #
    #
    # _run_cycle_for_eligible_skills() is the v1.6.0 replacement for the
    # old "launch direct_optimizer once for all skills" call. It:
    #   1. enumerates candidate skills (the configured target, or every
    #      skill that has rollouts),
    #   2. filters them through governance + cadence + budget (Phase 3),
    #   3. runs the official engine when `use_official_engine` is on and
    #      the package is importable, else falls back to direct_optimizer
    #      (Phase 1),
    #   4. records `state["last_engine"]` so _auto_adopt knows whether
    #      the staged proposal was already gated by the official engine
    #      (Phase 2 — the official_gated flag).
    #
    # All gating helpers are defensive: a governance/cadence/budget bug
    # falls through to "eligible" (matching the existing fall-through in
    # _auto_adopt), so a helper failure can never stall evolution. The
    # official-engine path is fail-soft: any error returns
    # fallback_to_direct and we retry that skill on the direct optimizer.

    def _load_rollout_records(self) -> list[dict[str, Any]]:
        """Parse every rollout ONCE per cycle.

        v1.8.1 (optimization): the tick previously re-scanned and re-parsed
        the whole rollouts dir separately in _candidate_skills AND
        _build_targeted_prompts (O(N·k) JSON parses per cycle on an
        unbounded directory). One scan is now threaded through both.
        """
        out: list[dict[str, Any]] = []
        try:
            for rp in sleep_runner.rollouts_dir().glob("*.json"):
                try:
                    r = json.loads(rp.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
                if isinstance(r, dict):
                    out.append(r)
        except Exception as e:
            self._log(f"rollout scan failed: {e}")
        return out

    def _run_cycle_for_eligible_skills(
        self, target: str | None, cfg: dict[str, Any],
        state: dict[str, Any], rollout_count: int,
        records: list[dict[str, Any]] | None = None,
    ) -> None:
        """Gate candidate skills, then run the official or direct engine."""
        use_official = bool(cfg.get("use_official_engine", True))
        if records is None:
            records = self._load_rollout_records()
        candidates = self._candidate_skills(target, records=records)
        if not candidates:
            self._log("auto-loop: no skills with rollouts to optimize")
            return

        eligible: list[str] = []
        for skill in candidates:
            # v1.7.0 (Phase C4): auto-opt-in a brand-new skill behind the
            # human-approval guardrail BEFORE the eligibility check, so the
            # first cycle that sees it can proceed (still gated on human
            # approval). Defensive: never stalls the tick.
            self._maybe_auto_optin(skill, cfg)
            ok, reason = self._skill_eligible_for_cycle(skill, cfg)
            if ok:
                eligible.append(skill)
            else:
                self._log(f"auto-loop: skip {skill!r} ({reason})")
        if not eligible:
            self._log("auto-loop: no eligible skills this tick")
            return

        # v1.3.0: build targeted prompts from inner-loop suggestions.
        # Only consumed by the direct path (the official engine does not
        # take free-form prompts). We still build them so the direct
        # fallback / direct-only path uses them.
        custom_prompts = self._build_targeted_prompts(target, records=records)

        ran_engine: str | None = None
        for skill in eligible:
            result = self._run_engine_for_skill(skill, use_official, cfg, custom_prompts)
            engine_used = result.get("engine") or "direct"
            ran_engine = engine_used
            self._log(
                f"auto-loop: {skill} cycle via {engine_used}: "
                f"ok={result.get('ok')} {result.get('reason', '')}"
            )
            # Drain inner-loop suggestions only when the direct path
            # actually consumed them (official path doesn't take prompts).
            # v1.8.1: only the CONSUMED ids are drained; unconsumed ones
            # stay queued for the next cycle.
            if engine_used == "direct" and result.get("ok"):
                self._drain_consumed_suggestions(
                    [skill], cfg,
                    consumed_ids=(custom_prompts.get(skill) or {}).get("consumed_ids"),
                )
            # Per-skill cadence + budget bookkeeping (best-effort).
            self._mark_skill_cycle(skill, cfg)
            state["last_cycle_at"] = time.time()
            state["last_cycle_verb"] = cfg.get("official_run_verb") or "run"
            state["last_rollout_count_at_cycle"] = rollout_count
            state["cycles_run"] = int(state.get("cycles_run", 0)) + 1
            state["last_engine"] = engine_used
            _save_state(state)

    def _run_engine_for_skill(
        self, skill: str, use_official: bool, cfg: dict[str, Any],
        custom_prompts: dict[str, str],
    ) -> dict[str, Any]:
        """Run the official engine for one skill, falling back to direct."""
        if use_official:
            try:
                from usr.plugins.skillopt.helpers import official_adapter  # type: ignore
                # v1.8.28: self-heal durability. Only /a0 is bind-mounted, so
                # /opt/venv-a0 loses the engine on every rebuild; the plugin
                # re-installs it rather than silently reverting to direct.
                # ensure_engine() is a no-op when the engine is already
                # importable and rate-limited after a failure, so this costs
                # nothing on a healthy install.
                oa_ensure = getattr(official_adapter, "ensure_engine", None)
                if callable(oa_ensure):
                    healed = oa_ensure()
                    if not healed.get("available"):
                        self._log(
                            f"auto-loop: official engine unavailable for {skill!r} "
                            f"({healed.get('reason')}); using direct_optimizer"
                        )
                probe = official_adapter.probe_official()
            except Exception as e:
                self._log(f"official_adapter import/probe failed: {e}; using direct")
                probe = {"available": False}
            if probe.get("available"):
                timeout = int(cfg.get("official_run_timeout_s", 600) or 600)
                result = official_adapter.run_official_sleep_cycle(
                    target=skill, custom_prompts=custom_prompts, cfg=cfg,
                    timeout_s=timeout,
                )
                if result.get("ok"):
                    return {**result, "engine": "official"}
                # v1.6.1: an official gate REJECT is authoritative — the
                # engine already evaluated the candidate on the held-out
                # set and found it no better. Do NOT fall back to direct
                # (direct has no held-out signal and would override the
                # official gate with an ungated edit). Return the reject
                # so the tick records a no-op reject cycle and moves on.
                if result.get("gate_rejected"):
                    self._log(
                        f"auto-loop: official gate REJECTED {skill!r}: "
                        f"{result.get('reason')}"
                    )
                    return {**result, "engine": "official"}
                # Fall through to direct on any infra failure.
                self._log(
                    f"auto-loop: official engine fallback for {skill!r}: "
                    f"{result.get('reason')}"
                )
        # Direct optimizer (the existing working path, also the fallback).
        from usr.plugins.skillopt.helpers import direct_optimizer  # type: ignore
        # v1.8.1: custom_prompts values are now {"prompt", "consumed_ids"};
        # accept a plain string too for backwards compatibility.
        cp_entry = custom_prompts.get(skill)
        cp = cp_entry.get("prompt") if isinstance(cp_entry, dict) else cp_entry
        res = direct_optimizer.optimize_skill(
            skill, min_rollouts=int(cfg.get("auto_loop_min_rollouts", 3)),
            custom_prompt=cp,
        )
        res["engine"] = "direct"
        return res

    def _candidate_skills(self, target: str | None, records: list[dict[str, Any]] | None = None) -> list[str]:
        """Skills with rollouts. A configured target is the only candidate.

        v1.8.1: accepts preloaded rollout records (single scan per cycle).
        """
        if target:
            return [target]
        out: set[str] = set()
        if records is None:
            records = self._load_rollout_records()
        for r in records:
            sk = (r.get("skill_used") or "").strip()
            if sk:
                out.add(sk)
        return sorted(out)

    def _maybe_auto_optin(self, skill: str, cfg: dict[str, Any]) -> None:
        """v1.7.0 (Phase C4): auto-opt-in a NEW skill behind the
        human-approval guardrail. `auto_optin_new_skill` is idempotent and
        self-guarding (skips optout/immutable/already-opted-in), so this is
        safe to call on every candidate every tick. Only logs on the
        `created` transition. Defensive: never stalls the tick."""
        try:
            gov_cfg = (cfg.get("governance") or {}).get("default_policy") or {}
            if not bool(gov_cfg.get("auto_optin_new_skills", True)):
                return
        except Exception:
            return
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
        except Exception:
            try:
                from helpers import governance  # type: ignore
            except Exception:
                return
        try:
            res = governance.auto_optin_new_skill(skill, source="auto_loop")
            if res.get("ok") and res.get("reason") == "created":
                self._log(
                    f"auto-loop: auto-opted-in new skill {skill!r} "
                    f"(pending human approval via /governance_approve)"
                )
        except Exception as e:
            self._log(f"auto-loop: auto_optin failed for {skill!r}: {e}")

    def _skill_eligible_for_cycle(
        self, skill: str, cfg: dict[str, Any],
    ) -> tuple[bool, str]:
        """Per-skill gate: governance + cadence + budget. Defensive.

        On any helper failure we fall through to eligible (a helper bug
        must never stall evolution), mirroring the fall-through in
        _auto_adopt's governance check.
        """
        # Governance (opt-out / immutable / rate-limited / approval).
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore  # noqa: F401
        except Exception:
            governance = None  # type: ignore[assignment]
        if governance is not None:
            try:
                # v1.8.18 (P2.1): the loop's own auto_adopt flag is the
                # operator's autonomy opt-in - it overrides the per-skill
                # pending-approval block (governance.py step 7). Optout /
                # immutable / pause markers still win.
                eligible, reason = governance.check_skill_eligible(
                    skill, auto_adopt=bool(cfg.get("auto_adopt", False)))
                try:
                    governance.mark_decision(skill, eligible, reason)
                except Exception:
                    pass
                if not eligible:
                    return False, reason
            except Exception as e:
                self._log(f"governance check failed for {skill!r}: {e}; fall through")

        # Cadence: is this skill due for a cycle yet?
        if cadence is not None:
            try:
                st = cadence.load_per_skill_state(skill)
                new_n = cadence.count_new_rollouts(skill, st.get("last_run_at", 0.0))
                # v1.8.25: pass the CONFIGURED floor/ceiling. These were
                # declared in default_config.yaml since v1.6.0 but never
                # reached compute_next_run, which fell back to its own
                # DEFAULT_FLOOR_S/DEFAULT_CEILING_S - so an operator tuning
                # `cadence:` saw no effect and had no way to know why.
                ccfg = cfg.get("cadence")
                floor_s = (
                    int(ccfg.get("floor_seconds")) if isinstance(ccfg, dict) and
                    ccfg.get("floor_seconds") is not None else None
                )
                ceiling_s = (
                    int(ccfg.get("ceiling_seconds")) if isinstance(ccfg, dict) and
                    ccfg.get("ceiling_seconds") is not None else None
                )
                kwargs = {}
                if floor_s is not None:
                    kwargs["floor_s"] = floor_s
                if ceiling_s is not None:
                    kwargs["ceiling_s"] = ceiling_s
                next_in_s = cadence.compute_next_run(new_n, **kwargs)
                if (time.time() - st.get("last_run_at", 0.0)) < next_in_s:
                    return False, f"cadence: not due for {next_in_s}s"
            except Exception as e:
                self._log(f"cadence check failed for {skill!r}: {e}; fall through")

        # Budget: can we spend one more LLM call on this skill today?
        if budget is not None:
            try:
                # v1.8.19 (live fix, 2026-09-22): `cfg.get("budget", {})`
                # returns a STRING when the nested `budget:` YAML section is
                # mangled by merged_config()'s flat light-parse (the parser
                # stores the bare `budget:` key as ""), so `.get(...)` raised
                # "'str' object has no attribute 'get'" on every cycle. In
                # _should_run_skill that silently DISABLED the daily budget
                # cap (except -> "fall through" = always eligible); in
                # _mark_skill_cycle it meant spend was never recorded. Accept
                # dict only; anything else falls back to module defaults.
                _b = cfg.get("budget")
                if not isinstance(_b, dict):
                    _b = {}
                soft_pct = int(_b.get("soft_warn_pct", 80) or 0)
                bt = budget.BudgetTracker(skill_name=skill, soft_warn_pct=soft_pct)
                cost = int(_b.get("cost_per_call_cents", 1) or 1)
                ok, reason = bt.can_spend(cost)
                if not ok:
                    return False, f"budget: {reason}"
            except Exception as e:
                self._log(f"budget check failed for {skill!r}: {e}; fall through")

        return True, "eligible"

    def _mark_skill_cycle(self, skill: str, cfg: dict[str, Any]) -> None:
        """Update per-skill cadence + budget state after a cycle. Best-effort."""
        if cadence is not None:
            try:
                st = cadence.load_per_skill_state(skill)
                st["last_run_at"] = time.time()
                st["total_cycles"] = int(st.get("total_cycles", 0)) + 1
                cadence.save_per_skill_state(skill, st)
            except Exception as e:
                self._log(f"cadence state save failed for {skill!r}: {e}")
        if budget is not None:
            try:
                # v1.8.19 (live fix, 2026-09-22): `cfg.get("budget", {})`
                # returns a STRING when the nested `budget:` YAML section is
                # mangled by merged_config()'s flat light-parse (the parser
                # stores the bare `budget:` key as ""), so `.get(...)` raised
                # "'str' object has no attribute 'get'" on every cycle. In
                # _should_run_skill that silently DISABLED the daily budget
                # cap (except -> "fall through" = always eligible); in
                # _mark_skill_cycle it meant spend was never recorded. Accept
                # dict only; anything else falls back to module defaults.
                _b = cfg.get("budget")
                if not isinstance(_b, dict):
                    _b = {}
                soft_pct = int(_b.get("soft_warn_pct", 80) or 0)
                bt = budget.BudgetTracker(skill_name=skill, soft_warn_pct=soft_pct)
                cost = int(_b.get("cost_per_call_cents", 1) or 1)
                _res = bt.record_spend(cost)
                if _res.get("soft_warning"):
                    self._log(
                        f"budget soft warning for {skill!r}: "
                        f"{_res.get('new_total')}c spent "
                        f"(cap {bt.daily_cap_cents}c, soft {bt.soft_warn_pct}%)"
                    )
            except Exception as e:
                self._log(f"budget record failed for {skill!r}: {e}")

    # ----------------------------------------------------------------- #
    # v1.3.0 (Day-4 item 4) - inner-loop integration
    # ----------------------------------------------------------------- #

    def _build_targeted_prompts(
        self, target: str | None,
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return {skill_name: {"prompt": str, "consumed_ids": [rollout_id...]}}.

        Per the inner-loop contract: inner_loop writes to
        logs/runs/suggestions/; the outer loop reads via
        list_pending_suggestions() and builds a targeted prompt via
        build_targeted_prompt(). The targeted prompt replaces the
        generic 'rewrite the whole skill' prompt for that skill only.
        Skills without suggestions keep the default behavior.

        v1.8.1: the value is now a dict carrying the CONSUMED rollout ids
        alongside the prompt, so _drain_consumed_suggestions can delete
        only what was actually used instead of the whole queue.

        A bug here can never crash the cycle - we swallow all errors
        and return an empty dict, which makes the direct optimizer
        fall back to the generic prompt (the documented v1.3.0
        fallback).
        """
        out: dict[str, dict[str, Any]] = {}
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore
        except Exception as e:
            self._log(f"inner_loop import failed: {e}")
            return out
        # Collect the set of skills we'll iterate. If a target is set
        # we only look at that skill; otherwise we look at every skill
        # that has a rollout (so we don't waste effort on empty skills).
        # v1.8.1: reuses the single per-cycle scan when provided.
        skills_to_consider: set[str] = set()
        try:
            if records is None:
                records = self._load_rollout_records()
            for r in records:
                sk = (r.get("skill_used") or "").strip()
                if not sk:
                    continue
                if target and sk != target:
                    continue
                skills_to_consider.add(sk)
        except Exception as e:
            self._log(f"rollout scan for targeted_prompts failed: {e}")
            return out
        # For each candidate skill, list its pending suggestions and
        # build a targeted prompt if there are any.
        for skill in sorted(skills_to_consider):
            try:
                pending = inner_loop.list_pending_suggestions(skill_name=skill)
            except Exception as e:
                self._log(f"list_pending_suggestions({skill}) failed: {e}")
                continue
            if not pending:
                continue
            # Read the current SKILL.md so the targeted prompt is
            # self-contained. Fall back to empty if missing.
            try:
                current_text = (sleep_runner.safe_skill_md(skill)).read_text(
                    encoding="utf-8", errors="replace",
                )
            except Exception:
                current_text = ""
            try:
                prompt = inner_loop.build_targeted_prompt(skill, current_text, pending)
            except Exception as e:
                self._log(f"build_targeted_prompt({skill}) failed: {e}")
                continue
            if not prompt:
                continue
            # v1.3.0 (Day-4 item 6): append the [FAILURE MEMORY] block so
            # the optimizer knows what we already tried that didn't
            # work. Wrapped in try/except so a failure_memory bug can
            # never crash the cycle - it would silently fall back to
            # the no-context behavior.
            try:
                from usr.plugins.skillopt.helpers import failure_memory  # type: ignore
            except Exception:
                from helpers import failure_memory  # type: ignore  # noqa: F401
            try:
                ctx = failure_memory.build_failure_context(skill)
                if ctx:
                    prompt = prompt + "\n\n" + ctx
            except Exception as e:
                self._log(f"failure_memory.build_failure_context({skill}) failed: {e}")
            out[skill] = {
                "prompt": prompt,
                # v1.8.1: the ids actually consumed (top-N by confidence).
                "consumed_ids": [
                    str(p.get("rollout_id") or "") for p in pending if p.get("rollout_id")
                ],
            }
        return out

    def _drain_consumed_suggestions(
        self, skills: list[str], cfg: dict[str, Any],
        consumed_ids: list[str] | None = None,
    ) -> None:
        """After a cycle consumes suggestions, delete them so the queue stays bounded.

        v1.8.1: passes the consumed rollout ids through to
        inner_loop.drain_suggestions(keep_ids=...) so only the CONSUMED
        suggestions are deleted; unconsumed ones stay queued for the next
        cycle (previously the whole queue was wiped, silently dropping the
        suggestions below build_targeted_prompt's top-N). We still don't
        drain suggestions of a DIFFERENT skill in a multi-skill cycle.
        Anything older than max_age_seconds is treated as stale and
        dropped - it was a hint the outer loop never acted on.
        """
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore
        except Exception:
            return
        max_age = int(cfg.get("inner_loop_max_suggestion_age_seconds", 7 * 86400))
        for skill in skills:
            try:
                drained = inner_loop.drain_suggestions(
                    skill, max_age_seconds=max_age, keep_ids=consumed_ids,
                )
                if drained:
                    self._log(
                        f"auto-loop: drained {len(drained)} suggestion(s) for skill {skill!r}"
                    )
            except Exception as e:
                self._log(f"drain_suggestions({skill}) failed: {e}")

    def _auto_adopt(self, state: dict[str, Any], cfg: dict[str, Any]) -> None:
        """If auto_adopt is on and proposals are staged, run the gate and adopt.

        v1.8.21 (P2 follow-up): DRAIN, not head-of-line. The previous
        version examined only the newest staged proposal and returned on
        the first governance-skip or gate-reject, so throughput was one
        adoption attempt per 30-min tick and a single malformed proposal
        blocked every staged proposal behind it (proven live 2026-09-23:
        a degenerate proposal re-rejected 14x over ~14h while well-formed
        proposals waited). Now every candidate gets an attempt per tick,
        bounded by `auto_adopt_max_per_tick` (default 5); a rejected
        proposal is QUARANTINED out of staging (staging/rejected/) —
        re-attempting identical bytes is deterministic waste — and an
        adopted one is consumed (staging/adopted/). Governance skips stay
        in staging: the skill may become eligible later.
        """
        staged = sleep_runner.find_staged_proposals()
        if not staged:
            # v1.8.22: even an empty queue needs the cleanup pass — a
            # real-gate worker may have finished while staging was
            # otherwise quiet, and the state mirror must not go stale.
            self._real_gate_cleanup(state, cfg)
            return
        self._real_gate_cleanup(state, cfg)
        staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        try:
            max_per_tick = int(cfg.get("auto_adopt_max_per_tick", 5) or 5)
        except (TypeError, ValueError):
            max_per_tick = 5
        if max_per_tick < 1:
            max_per_tick = 1
        attempted = adopted = quarantined = skipped = pending = 0
        for src in staged[:max_per_tick]:
            attempted += 1
            try:
                outcome = self._adopt_one(state, cfg, src)
            except Exception as e:
                # One candidate's bug can never stall the drain.
                self._log(f"auto-loop: staged proposal {src.name!r} attempt failed: {e}")
                outcome = "skipped"
            if outcome == "adopted":
                adopted += 1
            elif outcome == "quarantined":
                quarantined += 1
            elif outcome == "pending":
                pending += 1
            else:
                skipped += 1
        if attempted > 1 or pending:
            self._log(
                f"auto-loop: staged drain: attempted={attempted} adopted={adopted} "
                f"quarantined={quarantined} pending={pending} skipped={skipped}"
            )

    # ----------------------------------------------------------------- #

    def _real_gate_cleanup(self, state: dict[str, Any], cfg: dict[str, Any]) -> None:
        """v1.8.22: real-gate rehydrate/orphan-cleanup pass, run at the
        top of every _auto_adopt (including empty-queue ticks).

        The sidecar file is the source of truth; state["real_gate"] is a
        dashboard mirror. Orphaned sidecars (proposal left staging while
        the worker was out) are unlinked once the pid is dead — without
        this, a manually-consumed pending proposal would strand its
        sidecar and single-flight would block every future real gate."""
        try:
            sd = sleep_runner.staging_dir()
            if not sd.is_dir():
                return
            live: dict[str, Any] | None = None
            for child in sd.glob("*" + sleep_runner.REAL_GATE_SIDECAR_SUFFIX):
                try:
                    rg = json.loads(child.read_text(encoding="utf-8"))
                except Exception:
                    # Corrupt sidecar: unlink, never deadlock single-flight.
                    try:
                        child.unlink()
                    except OSError:
                        pass
                    continue
                if not isinstance(rg, dict):
                    continue
                prop = Path(str(rg.get("proposal_path") or ""))
                pid = int(rg.get("pid") or 0)
                if not prop.is_file() and not sleep_runner.is_running(pid):
                    # The verdict will never be consumable; the proposal
                    # was consumed/quarantined by another actor.
                    try:
                        child.unlink()
                    except OSError:
                        pass
                    continue
                if rg.get("status") == "pending" and sleep_runner.is_running(pid):
                    live = rg
            if live is not None and not state.get("real_gate"):
                # Restart mid-gate: rehydrate the dashboard mirror.
                state["real_gate"] = {
                    "skill": live.get("skill"),
                    "proposal": Path(str(live.get("proposal_path") or "")).name,
                    "pid": live.get("pid"),
                    "sidecar": str(child),
                    "started_ts": live.get("started_ts"),
                    "started_at": live.get("started_at"),
                    "jsonl_path": live.get("jsonl_path"),
                }
                _save_state(state)
            elif live is None and state.get("real_gate"):
                state["real_gate"] = None
                _save_state(state)
        except Exception as e:
            self._log(f"real gate cleanup failed: {e}")

    def _real_gate_enabled(
        self, cfg: dict[str, Any], official_gated: bool, src: "os.PathLike | str"
    ) -> bool:
        """v1.8.22: should this structurally-valid proposal wait for the
        ASYNC real replay confirmation instead of adopting immediately?

        - Both replay_real_executor_enabled AND replay_real_gate_enabled
          (the gate cannot work without its executor).
        - official_gated proposals bypass: the upstream engine already ran
          its monotonic gate (stage 0.7 skips them for the same reason).
        - Single-flight is NOT checked here: the caller parks the
          proposal as 'skipped' when another gate is in flight (see the
          spawn branch in _adopt_one) so it never adopts unconfirmed.
        - A proposal that already carries a sidecar is handled by the
          harvest branch earlier in _adopt_one, never here."""
        if not (
            bool(cfg.get("replay_real_executor_enabled", False))
            and bool(cfg.get("replay_real_gate_enabled", False))
        ):
            return False
        if official_gated:
            return False
        if sleep_runner.read_real_gate_sidecar(src) is not None:
            return False
        return True

    def _real_gate_spawn(
        self, state: dict[str, Any], cfg: dict[str, Any],
        src: "os.PathLike | str", skill_name: str,
    ) -> str | None:
        """Spawn the detached real-gate worker for one staged proposal.

        Returns 'pending' on a successful spawn (the caller parks the
        proposal), or None to fall through to the normal adopt path
        (fewer than replay_min_n held-out tasks — a 45-min worker would
        be guaranteed to return insufficient_n — or a spawn error).
        Sidecar is written BEFORE the spawn: a crash between spawn and
        sidecar write would orphan a running, never-harvested worker AND
        let a second spawn for the same proposal double-spend."""
        held = sleep_runner._load_held_out(skill_name)
        min_n = int(cfg.get("replay_min_n", 3) or 3)
        if len(held) < min_n:
            self._log(
                f"real gate: skipped for {skill_name} (held_out={len(held)} < "
                f"min_n={min_n}); adopting without real confirmation"
            )
            return None
        tasks_path = sleep_runner._real_gate_tasks_path(src)
        tasks_path.write_text(json.dumps(held, ensure_ascii=False), encoding="utf-8")
        sidecar_path = sleep_runner._real_gate_sidecar_path(src)
        now = time.time()
        payload: dict[str, Any] = {
            "real_gate": True,
            "schema": 1,
            "skill": skill_name,
            "proposal_path": str(src),
            "staged_mtime": src.stat().st_mtime,
            "status": "pending",
            "pid": 0,
            "started_ts": now,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "finished_ts": None,
            "jsonl_path": None,
            "tasks_file": str(tasks_path),
            "held_out_ids": [
                t.get("id") for t in held if isinstance(t, dict)
            ],
            # v1.8.23: honest sidecar labeling. At spawn NO gate has run yet —
            # `gate_passed: True` here was a placeholder that made completed
            # sidecars read as "gate passed" even when the verdict said
            # could-not-measure. Spawn records `pre_gate_recorded: true` (the
            # sentinel that a structural pre-gate ran and the real verdict
            # lives in the sidecar) and leaves `gate_passed` None until the
            # worker writes the verdict.
            "pre_gate_recorded": True,
            "gate_passed": None,
            "verdict": None,
            "error": None,
        }
        write_result = sleep_runner.write_real_gate_sidecar(src, payload)
        if write_result is None:
            raise RuntimeError("real-gate sidecar write failed")
        max_tasks = int(cfg.get("replay_real_max_tasks", 3) or 0)
        # v1.8.23: 450 was below the harness's own documented ceiling
        # (default_config.yaml / replay_harness default 600) — production ran
        # 6/6 monologue timeouts at 450s (real_gate_worker_20260924T112712).
        per_task = int(cfg.get("replay_real_per_task_timeout_s", 600) or 600)
        knobs = [
            "--per-task-timeout-s", str(per_task),
            "--max-tasks", str(max_tasks),
            "--gate-min-improvement-pp",
            str(float(cfg.get("gate_min_improvement_pp", 5.0) or 0.0)),
            "--replay-min-n", str(min_n),
        ]
        if str(cfg.get("replay_evalkit_enabled", False)).strip().lower() in {
                "1", "true", "yes", "on"}:
            knobs.append("--evalkit")
        try:
            launched = sleep_runner.launch_real_gate_worker(
                skill_name=skill_name,
                staged_path=src,
                sidecar_path=sidecar_path,
                tasks_file=tasks_path,
                extra_args=knobs,
            )
        except Exception:
            # Spawn failed: remove the pending sidecar + frozen tasks so a
            # half-spawn never blocks single-flight or parks the proposal
            # in a 4h stale window for nothing.
            for _p in (sidecar_path, tasks_path):
                try:
                    Path(str(_p)).unlink()
                except OSError:
                    pass
            raise
        payload["pid"] = launched.get("pid", 0)
        sleep_runner.write_real_gate_sidecar(src, payload)
        state["real_gate"] = {
            "skill": skill_name,
            "proposal": Path(str(src)).name,
            "pid": payload["pid"],
            "sidecar": str(sidecar_path),
            "started_ts": payload["started_ts"],
            "started_at": payload["started_at"],
            "jsonl_path": payload["jsonl_path"],
        }
        _save_state(state)
        # Budget bookkeeping: one gate = 2xN full monologues. Record-only
        # (no can_spend block): eligibility was already checked at cycle
        # time; a hard block here would park proposals invisibly.
        try:
            if budget is not None:
                cost = int(cfg.get("replay_real_gate_cost_cents", 6) or 0)
                if cost:
                    bt = budget.BudgetTracker(skill_name=skill_name)
                    bt.record_spend(cost)
        except Exception as e:
            self._log(f"real gate budget record failed for {skill_name}: {e}")
        self._log(
            f"real gate: spawned worker pid={payload['pid']} for {skill_name} "
            f"(n_held_out={len(held)}, worst_case_s={2 * max_tasks * per_task if max_tasks else 'uncapped'})"
        )
        return "pending"

    def _harvest_real_gate(
        self, state: dict[str, Any], cfg: dict[str, Any],
        src: "os.PathLike | str", skill_name: str,
        rg: dict[str, Any],
    ) -> str:
        """Resolve a real-gate sidecar on a later tick.

        Decision matrix (corrected 2026-09-26):

        - `status == "done"` + `verdict.ok` -> the gate measured. Adopt on
          `accepted`, otherwise quarantine. Fail CLOSED either way.
        - `status == "done"` + `verdict.ok is False` -> the gate RAN TO
          COMPLETION and returned a verdict that did not clear the bar
          (typically `insufficient_usable_pairs`). Quarantine. This is
          evidence, not an absence of evidence.
        - `status in ("failed", stale-pending, unknown)` -> no verdict was
          ever produced. Fail OPEN, so a transient executor outage cannot
          permanently block the drain.

        Why the completed-negative case changed (2026-09-26): the previous
        code treated every `ok=False` as "could not run" and adopted. The
        first production real-gate run returned
        `insufficient_usable_pairs:1 usable (2 task failures)` with
        `status: done`, and the harvest adopted it anyway. That proposal
        stripped the frontmatter from `usr/skills/scheduled-tasks/SKILL.md`,
        which made the skill unloadable by the framework - and because the
        auto-adopt path took no snapshot, it was unrecoverable. The risk is
        asymmetric: a quarantined proposal sits in `staging/rejected/` and an
        operator can re-approve it, whereas an adopted proposal overwrites a
        live skill. So a completed measurement must never fail open.
        """
        status = rg.get("status")
        verdict = rg.get("verdict") if isinstance(rg.get("verdict"), dict) else None
        stale_after = int(cfg.get("replay_real_gate_stale_after_s", 14400) or 14400)
        adopt: bool | None
        note: str
        if status == "done" and verdict is not None:
            if verdict.get("ok"):
                adopt = bool(verdict.get("accepted"))
                note = (
                    f"real_gate_accepted: {verdict.get('reason', '')}"
                    if adopt
                    else f"real_gate_rejected: {verdict.get('reason', '')}"
                )
            else:
                # status == "done", so the worker finished. ok=False means the
                # measurement did not clear the bar (e.g. too few usable
                # replay pairs), NOT that the gate was skipped. Quarantine.
                adopt = False
                note = (
                    f"real_gate_inconclusive: {verdict.get('reason', '')}"
                )
        elif status == "failed":
            adopt = True
            note = f"real_gate_failed: {rg.get('error', '')}"
        elif status == "pending":
            pid = int(rg.get("pid") or 0)
            if sleep_runner.is_running(pid):
                return "pending"
            age = time.time() - float(rg.get("started_ts") or 0.0)
            if age > stale_after:
                adopt = True
                note = f"real_gate_stale: worker pid={pid} dead after {int(age)}s"
            else:
                return "pending"  # within the grace window
        else:
            adopt = True
            note = f"real_gate_unknown_status: {status!r}"

        # Drift check — advisory, never veto: the measurement was paid
        # for and is internally consistent (frozen tasks file).
        try:
            drift: list[str] = []
            if abs(
                float(rg.get("staged_mtime") or 0.0) - Path(str(src)).stat().st_mtime
            ) > 1e-6:
                drift.append("proposal_changed_since_spawn")
            fresh_ids = [
                t.get("id") for t in sleep_runner._load_held_out(skill_name)
                if isinstance(t, dict)
            ]
            if list(rg.get("held_out_ids") or []) != fresh_ids:
                drift.append("held_out_set_changed")
            if drift:
                note += f" [drift: {', '.join(drift)}]"
        except Exception:
            pass

        state["real_gate"] = None
        _save_state(state)

        target = sleep_runner.safe_skill_md(skill_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        audit = sleep_runner.runs_dir() / "adoptions.log"
        audit.parent.mkdir(parents=True, exist_ok=True)

        if adopt:
            proposed = src.read_text(encoding="utf-8")
            # v1.8.24 (P0): snapshot + atomic replace. This path previously
            # called target.write_text() with no backup, so an unattended
            # auto-adopt could not be rolled back.
            _w = sleep_runner.adopt_write_skill(skill_name, proposed)
            if not _w.get("ok"):
                self._log(
                    f"adopt aborted for {skill_name}: {_w.get('error')}"
                )
                return _w.get("error", "adopt write failed")
            try:
                sleep_runner.clear_official_gate_marker(src)
            except Exception:
                pass
            state["proposals_adopted"] = int(state.get("proposals_adopted", 0)) + 1
            _save_state(state)
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "skill": skill_name,
                "source": str(src),
                "target": str(target),
                "passed": True,
                "reason": note,
                "real_gate": "harvested",
                "held_out": None,
            }
            with open(audit, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._log(f"auto-loop: ADOPTED {skill_name} ({note})")
            try:
                from usr.plugins.skillopt.helpers import cycle_history  # type: ignore  # noqa: F401
            except Exception:
                from helpers import cycle_history  # type: ignore  # noqa: F401
            try:
                cycle_history.record_cycle_entry({
                    "skill": skill_name,
                    "outcome": "adopted",
                    "outcome_detail": note,
                    "proposal_id": Path(str(src)).stem,
                    "proposed_size": len(proposed),
                    "gate_reasons": [],
                    "gate_stages_passed": ["real_gate"],
                    "runtime_seconds": 0.0,
                    "llm_calls": 0,
                    "links": {
                        "audit_log_entry": str(audit),
                        "staged_proposal": str(src),
                    },
                })
            except Exception as e:
                self._log(f"cycle_history.record_cycle_entry({skill_name}) failed: {e}")
            moved = sleep_runner.consume_staged_proposal(src)
            if moved is not None:
                self._log(f"auto-loop: consumed staged proposal {src.name!r} -> {moved.name!r}")
            return "adopted"

        # Quarantine: the real measurement rejected the proposal.
        state["proposals_rejected"] = int(state.get("proposals_rejected", 0)) + 1
        _save_state(state)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "skill": skill_name,
            "source": str(src),
            "target": str(target),
            "passed": False,
            "reason": note,
            "real_gate": "harvested",
            "held_out": None,
        }
        with open(audit, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._log(f"auto-loop: rejected {skill_name} ({note})")
        try:
            from usr.plugins.skillopt.helpers import failure_memory  # type: ignore  # noqa: F401
        except Exception:
            from helpers import failure_memory  # type: ignore  # noqa: F401
        try:
            first_line = ""
            for line in src.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s:
                    first_line = s[:120]
                    break
            failure_memory.record_failure(
                skill_name=skill_name,
                proposal_summary=first_line or src.stem or skill_name,
                failure_reason=note,
                rollouts=[],
                outcome="rejected",
            )
        except Exception as e:
            self._log(f"failure_memory.record_failure({skill_name}) failed: {e}")
        moved = sleep_runner.quarantine_staged_proposal(src, tag="real_gate_reject")
        if moved is not None:
            self._log(
                f"auto-loop: quarantined rejected proposal {src.name!r} -> {moved.name!r}"
            )
        return "quarantined"

    def _adopt_one(self, state: dict[str, Any], cfg: dict[str, Any],
                   src: "os.PathLike | str") -> str:
        """Gate one staged proposal; returns 'adopted' | 'quarantined' | 'skipped'."""
        skill_name = src.stem if src.suffix == ".md" else "unknown"

        # v1.8.1 guard: an official run WITHOUT a concrete target copies its
        # proposal to staging/best_skill.md; adopting that would create a
        # bogus usr/skills/best_skill/ skill. Skip such proposals loudly.
        if skill_name in ("", "unknown", "best_skill"):
            self._log(
                f"auto-loop: skipping staged proposal {src.name!r} "
                f"(no resolvable skill name in the filename)"
            )
            # v1.8.21: quarantine it too - a nameless artifact can never
            # become adoptable, so leaving it in staging is permanent churn.
            sleep_runner.quarantine_staged_proposal(src, tag="nameless")
            return "quarantined"

        # v1.8.1: provenance now comes from the per-proposal marker written
        # by official_adapter (state["last_engine"] was the engine of the
        # LAST skill run in the tick, which mismatches on multi-skill ticks).
        # Fall back to the old state heuristic for pre-marker staged files.
        marker = sleep_runner.read_official_gate_marker(src)
        if marker is not None:
            official_gated = bool(marker.get("official_gated"))
        else:
            official_gated = state.get("last_engine") == "official"

        # v1.5.0-Dev (Day-5 item 8): per-skill governance. Run BEFORE the
        # gate so an opt-out / immutable / rate-limited skill never even
        # enters the validation path. Wrapped in try/except so a
        # governance bug can never break the auto-loop; on failure we
        # fall through to the gate (the v1.4.0 behaviour).
        gov_reason: str = ""
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore  # noqa: F401
        except Exception:
            from helpers import governance  # type: ignore  # noqa: F401
        try:
            # v1.8.18 (P2.1): auto_adopt=true is the operator's autonomy
            # opt-in - it overrides the per-skill pending-approval block.
            eligible, gov_reason = governance.check_skill_eligible(
                # v1.8.18 (P1 fix, 2026-09-22): NO trailing comma here - the
                # first version of the P2.1 edit ended the call in a comma,
                # wrapping the 2-tuple result in a 1-tuple, and the unpack
                # raised "not enough values to unpack (expected 2, got 1)"
                # on EVERY _auto_adopt call - silently swallowed by the
                # except below, so governance logged no decisions and the
                # gate always fell through (the v1.5.0 smoke test caught it).
                skill_name, auto_adopt=bool(cfg.get("auto_adopt", False)))
            try:
                governance.mark_decision(skill_name, eligible, gov_reason)
            except Exception:
                pass
            if not eligible:
                self._log(
                    f"governance: skipped {skill_name} ({gov_reason})"
                )
                # v1.8.21: STAY in staging (no quarantine) - the skill may
                # become eligible later (approval, opt-in, pause lift).
                return "skipped"
        except Exception as e:
            # Governance failed: fall through to the gate. Don't crash.
            self._log(f"governance: {skill_name} check failed: {e}; falling through")

        # v1.8.22: real-gate HARVEST branch. A proposal with a verdict
        # sidecar skips the gate re-run entirely: the spawn-time sidecar
        # recorded pre_gate_recorded=true (v1.8.23; the old gate_passed=true
        # placeholder was misleading — see _real_gate_spawn), so harvesting
        # before the text reads avoids double governance, double audit rows
        # and a duplicate validate_proposal call. We act purely on the
        # replay verdict.
        rg = sleep_runner.read_real_gate_sidecar(src)
        if rg is not None:
            return self._harvest_real_gate(state, cfg, src, skill_name, rg)

        target = sleep_runner.safe_skill_md(skill_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        proposed = src.read_text(encoding="utf-8")
        current = ""
        if target.is_file():
            current = target.read_text(encoding="utf-8")

        # Find the most recent sleep log for held-out parsing
        last_log = _latest_sleep_log()
        held_out = sleep_runner.parse_held_out(last_log) if last_log else None

        # v1.6.0 (Phase 2): if the staged proposal was produced by the
        # official Sleep engine, that engine already ran its monotonic
        # held-out gate before staging — so the local gate only needs the
        # cheap structural pre-filter (official_gated=True skips the local
        # held-out stage and the advisory A/B harness). The direct
        # optimizer path keeps the full local gate.
        # (v1.8.1: official_gated is resolved above, from the per-proposal
        # provenance marker with the state heuristic as legacy fallback.)
        ab_enabled = bool(cfg.get("ab_harness_enabled", False)) and not official_gated
        ok, reason = sleep_runner.validate_proposal(
            proposed,
            current,
            min_chars=int(cfg.get("gate_min_chars", 200)),
            min_improvement_pp=float(cfg.get("gate_min_improvement_pp", 0.0)),
            max_shrink_ratio=float(cfg.get("gate_max_shrink_ratio", 0.5)),
            held_out=held_out,
            skill_name=skill_name,
            official_gated=official_gated,
            # v1.8.19 (P3): the ab_enabled flag computed above was dead -
            # the harness ran anyway with the stub judge. Pass the caller
            # resolution through so a disabled harness really is skipped.
            ab_harness_enabled=ab_enabled,
        )
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "skill": skill_name,
            "source": str(src),
            "target": str(target),
            "proposed_size": len(proposed),
            "current_size": len(current),
            "passed": ok,
            "reason": reason,
            "held_out": held_out,
            # v1.8.22: distinguish confirm-pending / bypassed adoptions
            # from adopt-immediately in adoptions.log.
            "real_gate": "bypassed" if official_gated else None,
        }
        audit = sleep_runner.runs_dir() / "adoptions.log"
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # v1.8.22: real-gate SPAWN branch. A structurally-valid, direct
        # (non-official-gated) proposal is parked in staging with a
        # pending sidecar while a detached worker runs the REAL replay
        # counterfactual (budget: 2 x replay_real_max_tasks x
        # replay_real_per_task_timeout_s). The verdict is harvested on a
        # later tick. Failures here fall through to the normal adopt
        # path (fail-open, loud) — never block the drain on spawn errors.
        if ok and self._real_gate_enabled(cfg, official_gated, src):
            if sleep_runner.find_pending_real_gate_sidecar() is not None:
                # Single-flight: one real gate at a time (filesystem-
                # derived). This proposal keeps its place in staging —
                # adopting it now would bypass the real verdict entirely.
                self._log(
                    f"real gate: single-flight busy; {skill_name} proposal "
                    f"stays queued for a later tick"
                )
                return "skipped"
            rg_outcome = None
            try:
                rg_outcome = self._real_gate_spawn(state, cfg, src, skill_name)
            except Exception as e:
                self._log(
                    f"real gate: spawn failed for {skill_name}: {e}; "
                    f"adopting without real confirmation"
                )
            if rg_outcome == "pending":
                return "pending"
            # rg_outcome None (insufficient held-out / spawn error): fall
            # through to the normal adopt path below.

        # v1.2.0 (Task A.2): one-line summary of the A/B harness result
        # so the cycle log captures whether the harness ran, was
        # skipped, or rejected. Best-effort: a missing harness helper
        # means it was never run.
        if ab_enabled:
            try:
                from usr.plugins.skillopt.helpers import ab_harness  # type: ignore
                ab_status = ab_harness.get_ab_status()
                last = ab_status.get("last_result") or {}
                if last:
                    if last.get("can_run") is False:
                        self._log(
                            f"ab_harness: skipped (can_run=False, reason={last.get('reason', 'unknown')!r})"
                        )
                    else:
                        self._log(
                            f"ab_harness: passed={last.get('passed')} "
                            f"lift={last.get('lift_pp', 0.0)}pp "
                            f"confidence={last.get('confidence', 0.0):.2f} "
                            f"samples={last.get('samples', 0)}"
                        )
                else:
                    self._log("ab_harness: never ran for this skill (no rollouts yet)")
            except Exception as e:
                self._log(f"ab_harness status read failed: {e}")

        # v1.8.24 (P1.6): refuse an UNGATED direct-path adoption.
        #
        # Reaching the write below on the direct (non-official-gated) path means
        # no authoritative measurement exists: either the real gate is off, or
        # the spawn failed, or there were too few held-out rollouts. The mock
        # counterfactual gate is advisory by default, so at this moment only the
        # structural pre-filter stands between the proposal and the live skill.
        # "The official package is absent, so fall back" was therefore silently
        # equivalent to "adopt ungated" - which is how an unloadable skill
        # reached disk on 2026-09-25.
        #
        # Fail closed. An operator who genuinely wants this sets
        # `allow_ungated_direct_adopt: true`: a deliberate opt-in to running
        # without a gate, not a side effect of a missing dependency.
        #
        # Official-gated proposals are exempt - the upstream monotonic held-out
        # gate is authoritative and already ran, so official_gated=True *is* a
        # measurement having happened. Auto-loop only: a human reviewing the
        # dashboard and calling /adopt is the intended escape hatch and must
        # keep working.
        if ok and not official_gated and not bool(
            cfg.get("allow_ungated_direct_adopt", False)
        ):
            self._log(
                f"ungated direct adopt REFUSED for {skill_name}: nothing gated "
                f"this proposal (official engine absent, and no real replay "
                f"verdict). Left in staging for review. Set "
                f"allow_ungated_direct_adopt: true to accept ungated rewrites, "
                f"or install skillopt for the authoritative gate."
            )
            return "ungated_blocked"

        if ok:
            # v1.8.24 (P0): snapshot + atomic replace, same reason as the
            # real-gate harvest path above.
            _w = sleep_runner.adopt_write_skill(skill_name, proposed)
            if not _w.get("ok"):
                self._log(f"adopt aborted for {skill_name}: {_w.get('error')}")
                return _w.get("error", "adopt write failed")
            # v1.8.1: the proposal is consumed — clear its provenance marker
            # so a later manual re-adopt re-runs the full local gate.
            try:
                sleep_runner.clear_official_gate_marker(src)
            except Exception:
                pass
            state["proposals_adopted"] = int(state.get("proposals_adopted", 0)) + 1
            _save_state(state)
            self._log(f"auto-loop: ADOPTED {skill_name} ({reason})")
            outcome = "adopted"
        else:
            state["proposals_rejected"] = int(state.get("proposals_rejected", 0)) + 1
            _save_state(state)
            self._log(f"auto-loop: rejected {skill_name} ({reason})")
            outcome = "rejected"
            # v1.3.0 (Day-4 item 6): record this rejection to the
            # failure memory so the next cycle's targeted prompt can
            # learn from it. Best-effort: a failure_memory bug here
            # can never crash the gate.
            try:
                from usr.plugins.skillopt.helpers import failure_memory  # type: ignore
            except Exception:
                from helpers import failure_memory  # type: ignore  # noqa: F401
            try:
                # Build a short proposal summary from the staged file
                # (first non-empty line of the proposal).
                first_line = ""
                for line in (proposed or "").splitlines():
                    s = line.strip()
                    if s:
                        first_line = s[:120]
                        break
                summary = first_line or src.stem or skill_name
                # Pull the rollout ids we used (best-effort - we may
                # not have them in this scope; leave empty if so).
                rollouts: list[str] = []
                failure_memory.record_failure(
                    skill_name=skill_name,
                    proposal_summary=summary,
                    failure_reason=reason or "rejected",
                    rollouts=rollouts,
                    outcome="rejected",
                )
            except Exception as e:
                self._log(f"failure_memory.record_failure({skill_name}) failed: {e}")

        # v1.4.0-Dev (Day-5 item 7): record the cycle boundary to
        # logs/runs/cycle_history.jsonl. Runs for BOTH adopted and
        # rejected outcomes (the failure_memory block above only runs
        # on rejection). Best-effort: a cycle_history bug can never
        # crash the auto-loop.
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore  # noqa: F401
        except Exception:
            from helpers import cycle_history  # type: ignore  # noqa: F401
        try:
            outcome_str = "adopted" if ok else "rejected"
            gate_reasons_out = [reason] if (reason and not ok) else []
            cycle_history.record_cycle_entry({
                "skill": skill_name,
                "outcome": outcome_str,
                "outcome_detail": reason or "",
                "proposal_id": src.stem,
                "proposed_size": len(proposed),
                "current_size": len(current),
                "gate_reasons": gate_reasons_out,
                "gate_stages_passed": [],
                "runtime_seconds": 0.0,
                "llm_calls": 0,
                "links": {
                    "audit_log_entry": str(audit),
                    "staged_proposal": str(src),
                },
            })
        except Exception as e:
            self._log(f"cycle_history.record_cycle_entry({skill_name}) failed: {e}")

        # v1.8.21: lifecycle move AFTER the audit rows (they reference the
        # staging path). Adopted -> staging/adopted/, rejected ->
        # staging/rejected/: both leave the pending queue, so the drain
        # always advances and a rejected proposal is never re-attempted
        # (identical bytes fail deterministically).
        if outcome == "adopted":
            try:
                moved = sleep_runner.consume_staged_proposal(src)
                if moved is not None:
                    self._log(f"auto-loop: consumed staged proposal {src.name!r} -> {moved.name!r}")
            except Exception as e:
                self._log(f"auto-loop: consume of adopted proposal {src.name!r} failed: {e}")
            return "adopted"
        moved = sleep_runner.quarantine_staged_proposal(src, tag="reject")
        if moved is not None:
            self._log(f"auto-loop: quarantined rejected proposal {src.name!r} -> {moved.name!r}")
        return "quarantined"

    # ----------------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        try:
            log = sleep_runner.runs_dir() / "auto_loop.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            # v1.8.1: rotate when large instead of growing without bound.
            try:
                sleep_runner.rotate_log_if_large(log)
            except Exception:
                pass
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}\n")
        except Exception:
            pass

    def _sleep(self, seconds: int) -> None:
        # Interruptible sleep
        self._stop_event.wait(seconds)


# ----------------------------------------------------------------------- #
# Public API for the WebUI / API
# ----------------------------------------------------------------------- #

def _latest_sleep_log() -> Path | None:
    runs_root = sleep_runner.runs_dir()
    if not runs_root.is_dir():
        return None
    logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def get_loop_state() -> dict[str, Any]:
    state = _load_state()
    snap = sleep_runner.get_status_snapshot()
    state["rollouts_now"] = snap["rollout_count"]
    state["staged_now"] = len(snap["staged_proposals"])
    # v1.8.22: real-gate mirror — add a live age so the dashboard can
    # show "pending for Xm" without parsing timestamps client-side.
    rg = state.get("real_gate")
    if isinstance(rg, dict) and rg.get("started_ts"):
        try:
            rg["age_s"] = round(time.time() - float(rg["started_ts"]), 1)
        except (TypeError, ValueError):
            pass
    # Surface the last error to the dashboard (was invisible in v1.0)
    err_path = sleep_runner.runs_dir() / LAST_ERROR_FILENAME
    if err_path.is_file():
        try:
            state["last_error_detail"] = json.loads(err_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    last_log = _latest_sleep_log()
    if last_log:
        state["last_sleep_log"] = str(last_log)
        state["last_sleep_held_out"] = sleep_runner.parse_held_out(last_log)
    return state


# ----------------------------------------------------------------------- #
# v1.3.0 (Day-4 item 4) - InnerLoopThread
# ----------------------------------------------------------------------- #
#
# The inner loop is a SEPARATE background thread from the auto-loop.
# It runs at a faster cadence (default 60s) than the auto-loop (default
# 30min) and produces per-rollout suggestions instead of full rewrites.
# The two threads share the same `get_config` and the same lifecycle
# pattern, but they are independent: the inner loop NEVER touches
# staging/, SKILL.md, or the validation gate. The auto-loop reads
# the inner loop's output via list_pending_suggestions() at the
# start of each cycle and feeds the targeted prompt to the LLM.
#
# Two-loop contract (per Day-3 item 6 of engineering principles):
#   - inner loop writes only to logs/runs/suggestions/ and
#     logs/runs/inner_loop.log
#   - outer loop reads from logs/runs/suggestions/ and never writes
#     to the inner loop's output paths

class InnerLoopThread(threading.Thread):
    """Background thread that periodically calls inner_loop_tick().

    Mirrors the AutoLoopThread lifecycle (daemon, stop_event,
    _log, never-raise tick) but is independent - stopping one
    does not stop the other. The inner loop runs on a faster
    cadence (default 60s vs 30min for the auto-loop) so
    suggestions are fresh by the time the next auto-loop cycle
    consumes them.
    """

    def __init__(self, get_config, stop_event: threading.Event | None = None):
        super().__init__(name="skillopt-inner-loop", daemon=True)
        self.get_config = get_config
        self._stop_event = stop_event or threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Main loop. Returns when stop() is called."""
        # Lazy import - the inner_loop module is plugin-local and may
        # not be importable in every environment; we never want a
        # missing import to kill the thread.
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore
        except Exception as e:
            # No inner_loop module available - the thread is a no-op
            # until it's restarted. We log once and exit cleanly.
            self._log(f"inner_loop import failed at thread start: {e}")
            return
        # Honour the master kill switch
        try:
            cfg = self.get_config() or {}
        except Exception:
            cfg = {}
        if not cfg.get("inner_loop_enabled", True):
            self._log("inner-loop: disabled by config, exiting")
            return
        self._log(
            f"inner-loop: starting "
            f"(interval={int(cfg.get('inner_loop_interval_seconds', 60))}s, "
            f"max_age={int(cfg.get('inner_loop_max_suggestion_age_seconds', 7*86400))}s, "
            f"min_confidence={float(cfg.get('inner_loop_min_rollout_confidence', 0.4))})"
        )
        while not self._stop_event.is_set():
            try:
                # Re-read config each tick so a WebUI toggle takes
                # effect without restarting the thread.
                cfg = self.get_config() or {}
                if not cfg.get("inner_loop_enabled", True):
                    self._log("inner-loop: disabled mid-run, exiting")
                    break
                tick_result = inner_loop.inner_loop_tick(
                    llm_endpoint=cfg.get("llm_endpoint"),
                )
                self._log(
                    f"inner-loop tick: scanned={tick_result.get('scanned', 0)} "
                    f"suggested={tick_result.get('suggested', 0)} "
                    f"skipped={tick_result.get('skipped', 0)} "
                    f"errors={tick_result.get('errors', 0)} "
                    f"last_error={tick_result.get('last_error')}"
                )
            except Exception as e:
                # The contract says inner_loop_tick must never raise,
                # but we belt-and-brace here in case the contract is
                # broken in a future refactor.
                self._log(f"inner-loop tick crashed: {e}")
            try:
                interval = max(5, int(cfg.get("inner_loop_interval_seconds", 60)))
            except Exception:
                interval = 60
            self._sleep(interval)
        self._log("inner-loop: stopped")

    # ----------------------------------------------------------------- #

    def _log(self, msg: str) -> None:
        try:
            log = sleep_runner.runs_dir() / "auto_loop.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {msg}\n")
        except Exception:
            pass

    def _sleep(self, seconds: int) -> None:
        self._stop_event.wait(seconds)


# Module-level singletons so each thread is started exactly once per process.
_inner_thread: InnerLoopThread | None = None
_inner_lock = threading.Lock()


def start_inner_loop(get_config) -> InnerLoopThread | None:
    """Start the inner-loop background thread. Idempotent.

    Called by the agent_init extension hook alongside
    AutoLoopThread. Returns the live thread (or None if the inner
    loop is disabled in config, or if the import failed).
    """
    global _inner_thread
    with _inner_lock:
        if _inner_thread is not None and _inner_thread.is_alive():
            return _inner_thread
        try:
            cfg = get_config() or {}
        except Exception:
            cfg = {}
        if not cfg.get("inner_loop_enabled", True):
            return None
        _inner_thread = InnerLoopThread(get_config=get_config)
        _inner_thread.start()
        return _inner_thread


def get_inner_loop_thread() -> InnerLoopThread | None:
    """Return the live InnerLoopThread (or None if not started)."""
    return _inner_thread


def stop_inner_loop(timeout: float = 5.0) -> None:
    """Signal the inner-loop thread to stop. Used by hooks.uninstall()."""
    global _inner_thread
    with _inner_lock:
        t = _inner_thread
        if t is None:
            return
        t.stop()
        t.join(timeout=timeout)
        _inner_thread = None



# ----------------------------------------------------------------------- #
# Day-4 item 5: per-skill cadence + per-skill budget integration
# ----------------------------------------------------------------------- #
# These functions are ADDITIVE. The existing AutoLoopThread keeps using the
# single-state-file flow. The new cadence/budget helpers can be invoked
# independently by get_status_snapshot() and the API layer.


def compute_cadence_for_skill(skill_name: str, cfg: dict | None = None) -> int:
    """Return seconds until the next cycle for `skill_name` (per-skill cadence).

    v1.8.25: honours the configured `cadence.floor_seconds` /
    `cadence.ceiling_seconds`, which were previously declared but never read
    (see the tick call site for the reasoning).
    """
    if cadence is None:
        return 60  # safe default
    state = cadence.load_per_skill_state(skill_name)
    new_rollouts = cadence.count_new_rollouts(skill_name, state["last_run_at"])
    kwargs = {}
    section = (cfg or {}).get("cadence")
    if isinstance(section, dict):
        if section.get("floor_seconds") is not None:
            try:
                kwargs["floor_s"] = int(section["floor_seconds"])
            except (TypeError, ValueError):
                pass
        if section.get("ceiling_seconds") is not None:
            try:
                kwargs["ceiling_s"] = int(section["ceiling_seconds"])
            except (TypeError, ValueError):
                pass
    return cadence.compute_next_run(new_rollouts, **kwargs)


def get_budget_status(skill_name: str | None = None) -> dict:
    """Return the BudgetTracker status for a skill (or global if None)."""
    if budget is None:
        return {"enabled": False, "reason": "budget module not loaded"}
    bt = budget.BudgetTracker(skill_name=skill_name)
    return {"enabled": True, **bt.get_status()}


def can_skill_spend(skill_name: str, cents: int) -> tuple:
    """Check if a skill can spend `cents` more today. Returns (ok, reason)."""
    if budget is None:
        return (True, "")
    bt = budget.BudgetTracker(skill_name=skill_name)
    return bt.can_spend(cents)


def record_skill_spend(skill_name: str, cents: int) -> dict:
    """Record a spend of `cents` for `skill_name`."""
    if budget is None:
        return {"recorded": 0, "new_total": 0, "day": ""}
    bt = budget.BudgetTracker(skill_name=skill_name)
    return bt.record_spend(cents)
