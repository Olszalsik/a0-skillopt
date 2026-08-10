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
        self._last_rollout_count: int = len(sleep_runner.list_rollouts())

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        """Main loop. Returns when stop() is called."""
        state = _load_state()
        state["running"] = True
        _save_state(state)
        try:
            while not self._stop_event.is_set():
                cfg = self._safe_config()
                if not cfg:
                    # No config yet - wait and try again
                    self._sleep(30)
                    continue
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
        # 1. Count rollouts
        rollout_count = len(sleep_runner.list_rollouts())
        new_rollouts = rollout_count - self._last_rollout_count
        self._last_rollout_count = rollout_count
        min_new = int(cfg.get("auto_loop_min_rollouts", 10))

        # 2. Maybe launch a Sleep cycle
        if new_rollouts >= min_new:
            target = (cfg.get("auto_loop_skill_target") or "").strip() or None
            self._log(
                f"auto-loop: tick "
                f"(new_rollouts={new_rollouts}, threshold={min_new}, target={target})"
            )
            try:
                self._run_cycle_for_eligible_skills(target, cfg, state, rollout_count)
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

    def _run_cycle_for_eligible_skills(
        self, target: str | None, cfg: dict[str, Any],
        state: dict[str, Any], rollout_count: int,
    ) -> None:
        """Gate candidate skills, then run the official or direct engine."""
        use_official = bool(cfg.get("use_official_engine", True))
        candidates = self._candidate_skills(target)
        if not candidates:
            self._log("auto-loop: no skills with rollouts to optimize")
            return

        eligible: list[str] = []
        for skill in candidates:
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
        custom_prompts = self._build_targeted_prompts(target)

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
            if engine_used == "direct" and result.get("ok"):
                self._drain_consumed_suggestions([skill], cfg)
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
                # Fall through to direct on any failure.
                self._log(
                    f"auto-loop: official engine fallback for {skill!r}: "
                    f"{result.get('reason')}"
                )
        # Direct optimizer (the existing working path, also the fallback).
        from usr.plugins.skillopt.helpers import direct_optimizer  # type: ignore
        cp = custom_prompts.get(skill)
        res = direct_optimizer.optimize_skill(
            skill, min_rollouts=int(cfg.get("auto_loop_min_rollouts", 3)),
            custom_prompt=cp,
        )
        res["engine"] = "direct"
        return res

    def _candidate_skills(self, target: str | None) -> list[str]:
        """Skills with rollouts. A configured target is the only candidate."""
        if target:
            return [target]
        out: set[str] = set()
        for rp in sleep_runner.rollouts_dir().glob("*.json"):
            try:
                r = json.loads(rp.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            sk = (r.get("skill_used") or "").strip()
            if sk:
                out.add(sk)
        return sorted(out)

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
                eligible, reason = governance.check_skill_eligible(skill)
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
                next_in_s = cadence.compute_next_run(new_n)
                if (time.time() - st.get("last_run_at", 0.0)) < next_in_s:
                    return False, f"cadence: not due for {next_in_s}s"
            except Exception as e:
                self._log(f"cadence check failed for {skill!r}: {e}; fall through")

        # Budget: can we spend one more LLM call on this skill today?
        if budget is not None:
            try:
                bt = budget.BudgetTracker(skill_name=skill)
                cost = int(cfg.get("budget", {}).get("cost_per_call_cents", 1) or 1)
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
                bt = budget.BudgetTracker(skill_name=skill)
                cost = int(cfg.get("budget", {}).get("cost_per_call_cents", 1) or 1)
                bt.record_spend(cost)
            except Exception as e:
                self._log(f"budget record failed for {skill!r}: {e}")

    # ----------------------------------------------------------------- #
    # v1.3.0 (Day-4 item 4) - inner-loop integration
    # ----------------------------------------------------------------- #

    def _build_targeted_prompts(self, target: str | None) -> dict[str, str]:
        """Return {skill_name: targeted_prompt} for skills with pending suggestions.

        Per the inner-loop contract: inner_loop writes to
        logs/runs/suggestions/; the outer loop reads via
        list_pending_suggestions() and builds a targeted prompt via
        build_targeted_prompt(). The targeted prompt replaces the
        generic 'rewrite the whole skill' prompt for that skill only.
        Skills without suggestions keep the default behavior.

        A bug here can never crash the cycle - we swallow all errors
        and return an empty dict, which makes the direct optimizer
        fall back to the generic prompt (the documented v1.3.0
        fallback).
        """
        out: dict[str, str] = {}
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore
        except Exception as e:
            self._log(f"inner_loop import failed: {e}")
            return out
        # Collect the set of skills we'll iterate. If a target is set
        # we only look at that skill; otherwise we look at every skill
        # that has a rollout (so we don't waste effort on empty skills).
        skills_to_consider: set[str] = set()
        try:
            for rp in sleep_runner.rollouts_dir().glob("*.json"):
                try:
                    r = json.loads(rp.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    continue
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
                current_text = (sleep_runner.a0_skills_dir() / skill / "SKILL.md").read_text(
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
            out[skill] = prompt
        return out

    def _drain_consumed_suggestions(self, skills: list[str], cfg: dict[str, Any]) -> None:
        """After a cycle consumes suggestions, delete them so the queue stays bounded.

        We don't drain suggestions we DIDN'T consume (different skill
        in a multi-skill cycle, or suggestions the optimizer rejected
        for low confidence). Those stay in the queue for the next
        cycle. Anything older than max_age_seconds is treated as
        stale and dropped - it was a hint the outer loop never acted
        on.
        """
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore
        except Exception:
            return
        max_age = int(cfg.get("inner_loop_max_suggestion_age_seconds", 7 * 86400))
        for skill in skills:
            try:
                drained = inner_loop.drain_suggestions(skill, max_age_seconds=max_age)
                if drained:
                    self._log(
                        f"auto-loop: drained {len(drained)} suggestion(s) for skill {skill!r}"
                    )
            except Exception as e:
                self._log(f"drain_suggestions({skill}) failed: {e}")

    def _auto_adopt(self, state: dict[str, Any], cfg: dict[str, Any]) -> None:
        """If auto_adopt is on and a proposal is staged, run the gate and adopt."""
        staged = sleep_runner.find_staged_proposals()
        if not staged:
            return
        staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        src = staged[0]
        skill_name = src.stem if src.suffix == ".md" else "unknown"

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
            eligible, gov_reason = governance.check_skill_eligible(skill_name)
            try:
                governance.mark_decision(skill_name, eligible, gov_reason)
            except Exception:
                pass
            if not eligible:
                self._log(
                    f"governance: skipped {skill_name} ({gov_reason})"
                )
                return
        except Exception as e:
            # Governance failed: fall through to the gate. Don't crash.
            self._log(f"governance: {skill_name} check failed: {e}; falling through")

        target = sleep_runner.a0_skills_dir() / skill_name / "SKILL.md"
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
        official_gated = state.get("last_engine") == "official"
        ab_enabled = bool(cfg.get("ab_harness_enabled", False)) and not official_gated
        ok, reason = sleep_runner.validate_proposal(
            proposed,
            current,
            min_chars=int(cfg.get("gate_min_chars", 200)),
            min_improvement_pp=float(cfg.get("gate_min_improvement_pp", 0.0)),
            max_shrink_ratio=float(cfg.get("gate_max_shrink_ratio", 0.5)),
            held_out=held_out,
            skill_name=skill_name if ab_enabled else None,
            official_gated=official_gated,
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
        }
        audit = sleep_runner.runs_dir() / "adoptions.log"
        audit.parent.mkdir(parents=True, exist_ok=True)
        with open(audit, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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

        if ok:
            target.write_text(proposed, encoding="utf-8")
            state["proposals_adopted"] = int(state.get("proposals_adopted", 0)) + 1
            _save_state(state)
            self._log(f"auto-loop: ADOPTED {skill_name} ({reason})")
        else:
            state["proposals_rejected"] = int(state.get("proposals_rejected", 0)) + 1
            _save_state(state)
            self._log(f"auto-loop: rejected {skill_name} ({reason})")
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


def compute_cadence_for_skill(skill_name: str) -> int:
    """Return seconds until the next cycle for `skill_name` (per-skill cadence)."""
    if cadence is None:
        return 60  # safe default
    state = cadence.load_per_skill_state(skill_name)
    new_rollouts = cadence.count_new_rollouts(skill_name, state["last_run_at"])
    return cadence.compute_next_run(new_rollouts)


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
