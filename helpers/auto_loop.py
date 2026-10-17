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
    """Write the single-writer auto-loop state file. Atomic + fsynced.

    v1.8.35 (audit F6): this was a bare `write_text`, the only non-atomic
    state write left in the plugin - `cadence.save_per_skill_state`,
    `budget._save` and `sleep_runner.write_real_gate_sidecar` all already used
    write-temp + `os.replace`. `Path.write_text` is open-truncate-then-write,
    so the file is 0 bytes between `open()` and close.

    That matters because there is a concurrent READER on another thread:
    `get_loop_state()` is called from `api/loop.py` on the HTTP request
    thread. A torn read there is swallowed by `_load_state`, which returns
    DEFAULTS - so the counters are not merely stale, they are reset, and
    `last_rollout_count_at_cycle` returns to 0, which makes the rollout delta
    exceed the threshold and fire an immediate unscheduled cycle.

    `fsync` before the rename makes the rename durable, not just atomic; the
    temp file is cleaned up on any failure so a repeated crash cannot litter
    `logs/runs/`. Never raises - the caller has no better recovery.
    """
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(state, ensure_ascii=False, indent=2))
            fp.flush()
            try:
                os.fsync(fp.fileno())
            except OSError:
                pass  # not all filesystems support it; atomicity still holds
        os.replace(tmp, p)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except Exception:
            pass


def _int_or_default(value: Any, default: int) -> int:
    """Coerce to int, defaulting only on absence/unusability - never on 0.

    v1.8.48 (audit A8). `int(cfg.get(k, 5) or 5)` turns a deliberate `0` into
    5, and the plugin's own contract bans the `or <default>` idiom for a
    numeric setting. Used for every numeric tick knob that must honour an
    explicit zero.
    """
    if value is None or isinstance(value, bool):
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _cost_per_call_cents(cfg: dict[str, Any] | None) -> int:
    """`budget.cost_per_call_cents`, defaulting only on absence.

    v1.8.48 (audit A11). `int(... or 1)` charged 1 per cycle even when the
    operator set 0, while the tracker built from the SAME key kept 0 and
    reported 0 in the status - so the cap the operator believes is free still
    accrued. The gate and the recorder must charge what the tracker reports.
    """
    raw = budget.budget_section(cfg or {}).get("cost_per_call_cents")
    return max(0, _int_or_default(raw, 1))


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
        # v1.8.48 (audit A10): write atomically, like every other file in
        # `logs/runs/`. This one has two readers OFF the writer thread - the
        # HTTP status route and the monologue_start warning - and both
        # degrade to "no error" on a torn read, so precisely when the loop is
        # failing the banner can read clean.
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            fh.flush()
            # v1.8.49 (audit X6): guard the fsync, exactly as the sibling
            # `_save_state` does. An unguarded `OSError` from a filesystem that
            # does not support it propagated to the cleanup branch, which
            # unlinked the temp - so the CURRENT error was discarded and the
            # banner kept rendering an older one. Losing the write is strictly
            # better than losing the newest record.
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, p)
    except Exception:
        try:
            tmp = sleep_runner.runs_dir() / (LAST_ERROR_FILENAME + ".tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


# ----------------------------------------------------------------------- #
# The daemon thread
# ----------------------------------------------------------------------- #

class _CadenceDisabled(Exception):
    """Raised internally when `cadence.enabled` is false.

    Control flow, not an error: it unwinds the cadence block so the
    budget gate still runs, without logging a scary line.
    """


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
        # v1.8.35 (audit F5): the rollout-count cache is initialised HERE as
        # an instance attribute, so `_invalidate_rollout_counts` (now an
        # instance method) actually clears it. Previously the cache was only
        # ever created lazily by the first drain, and the invalidation wrote
        # a class attribute the instance permanently shadowed.
        self._rollout_counts_cache: dict[str, int] | None = None

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
                # v1.8.34: the per-tick rollout-count cache is only valid for
                # one tick. Dropping it here keeps
                # `autonomous_min_evidence_rollouts` comparing against live
                # data rather than whatever the first drain ever saw.
                self._invalidate_rollout_counts()
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
                    # v1.8.50 (audit G3): the sleep interval belongs INSIDE
                    # this try, not after it. It was `int(cfg.get(k, 1800))`
                    # evaluated outside any guard, and the exact valueless-YAML
                    # case (`auto_loop_interval_sec:`) yields `None`, so
                    # `int(None)` raised TypeError, escaped `_run` entirely and
                    # killed the ONLY thread that ticks the plugin - nothing
                    # above it could run again for the life of the process.
                    # `_int_or_default` tolerates the value AND the structure
                    # keeps a future coercion mistake from being fatal.
                    interval = max(60, _int_or_default(
                        cfg.get("auto_loop_interval_sec"), 1800))
                    self._sleep(interval)
                except Exception as e:
                    # Never let the thread die; record so the dashboard can see it
                    self._log(f"tick error: {e}")
                    _record_error(e, "tick")
                    state["last_error"] = f"{type(e).__name__}: {e}"
                    # Fall back to the shipped cadence rather than spinning.
                    try:
                        self._sleep(1800)
                    except Exception:
                        pass
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
                # v1.8.48 (audit A2): PERSIST it. The nearest `_save_state` is
                # inside the per-skill cycle branch, which the pruning itself
                # makes less likely to run (fewer files -> below threshold ->
                # no cycle). So the log line reported an adjustment that was
                # never written, and after a restart the stale, too-high base
                # pinned `new_rollouts` at 0 until `removed + min_new` fresh
                # rollouts arrived - weeks at a real harvest rate. This is
                # the F6 class: an intent logged, a state not written.
                self._save_state(state)
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

# 3. Maybe auto-adopt (v1.8.34: gated on the resolved operating mode,
        # not on a bare truthiness test of cfg["auto_adopt"]).
        try:
            from usr.plugins.skillopt.helpers import governance as _gov_mode  # type: ignore
        except Exception:
            try:
                from helpers import governance as _gov_mode  # type: ignore
            except Exception:
                _gov_mode = None  # type: ignore[assignment]
        autonomous = False
        if _gov_mode is not None:
            try:
                autonomous = bool(_gov_mode.resolve_evolution_mode()
                                  .get("autonomous"))
            except Exception:
                autonomous = False
        if autonomous:
            self._auto_adopt(state, cfg)
        elif cfg.get("auto_adopt", False):
            # A bare truthy legacy value that the resolver REJECTED (e.g. the
            # string "false"). Auto-adopting on a value we just called invalid
            # would be the worst possible outcome, so say so and stay manual
            # rather than silently draining.
            self._log(
                "auto-loop: auto_adopt is set to a value the mode resolver "
                "rejected - staying in manual mode (see resolve_evolution_mode "
                "in logs/runs/auto_loop.log)"
            )

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
            if result.get("deferred"):
                # A timed-out official subprocess is still writing to its
                # staging area. Do not launch a competing direct cycle or
                # consume this skill's cadence/budget until it finishes.
                continue
            # Drain inner-loop suggestions only when the direct path
            # actually consumed them (official path doesn't take prompts).
            # v1.8.1: only the CONSUMED ids are drained; unconsumed ones
            # stay queued for the next cycle.
            if engine_used == "direct" and result.get("ok"):
                # v1.8.50 (audit G4): act on the pre-persist redaction flag.
                # The staged bytes a reviewer reads may not be the model's
                # output; say so in the loop log, so the discrepancy is
                # discoverable without diffing the provider transcript.
                if result.get("redacted_before_persist"):
                    self._log(
                        f"auto-loop: staged proposal for {skill!r} was "
                        f"redacted before it was written; the staged "
                        f"document is not byte-identical to the model output")
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
                if result.get("deferred"):
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

        v1.8.35 (audit F8): helper failure now DEFERS (returns not-eligible)
        instead of falling through to eligible. The old fall-through made a
        governance bug indistinguishable from a clean bill of health: optout,
        immutable, pause, cadence and budget were all skipped for that cycle,
        and under autonomous mode the drain then wrote a live SKILL.md for a
        skill the operator had opted out of or paused.

        A stall is recoverable and visible (the reason is returned and
        `_record_error` fires); a silent write to a live skill is neither.
        Cadence and budget below keep their own fall-through, because those two
        only throttle work and cannot authorise it.
        """
        # Governance (opt-out / immutable / rate-limited / approval).
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore  # noqa: F401
        except Exception:
            governance = None  # type: ignore[assignment]
        if governance is None:
            # Absent governance is a broken install, not a permissive one.
            self._log(
                "governance module unavailable; DEFER (not eligible) rather "
                "than run with every governance rail silently disabled"
            )
            return False, "governance_unavailable"
        if governance is not None:
            try:
                # v1.8.35 (audit F1): called with NO argument on purpose. Passing
    # `cfg` here fed the merged blob into the resolver, and a merged
    # blob always contains the shipped `governance.evolution_mode:
    # manual` - so the default outranked an install whose config.json
    # says `auto_adopt: true` and autonomy was silently inert. The
    # operator's autonomy opt-in comes from
                # governance.resolve_evolution_mode() - the single source of
                # truth for manual vs autonomous. Do NOT read
                # cfg["auto_adopt"] directly here: that bare truthiness test
                # is None for `auto_adopt:` with no value (drain silently
                # stops) and True for the string "false" (drain silently
                # starts). The resolver strictly coerces and logs a rejected
                # value instead.
                #
                # Autonomy still only overrides the per-skill
                # pending-approval block (governance.py step 7). Optout /
                # immutable / pause / interval / budget markers are all
                # evaluated ABOVE that step and always win.
                eligible, reason = governance.check_skill_eligible(
                    skill,
                    auto_adopt=bool(governance.resolve_evolution_mode()
                                    .get("autonomous")))
                try:
                    governance.mark_decision(skill, eligible, reason)
                except Exception:
                    pass
                if not eligible:
                    return False, reason
            except Exception as e:
                # v1.8.35 (audit F8): FAIL CLOSED, not open.
                #
                # This used to log "fall through" and continue into
                # cadence/budget, returning eligible. That made a governance
                # helper bug indistinguishable from a clean bill of health:
                # optout, immutable, pause, cadence and budget were ALL
                # skipped for that cycle, and under autonomous mode the drain
                # then wrote a live SKILL.md for a skill the operator had
                # opted out of or paused.
                #
                # The contract is explicit - "a rail that cannot be evaluated
                # must defer, never wave through" - and a machine-written
                # change to a live file is the case where deferring is
                # obviously correct. This stalls the cycle rather than
                # corrupting a skill, and both the log line and the reason
                # below are the operator's only signal, so they are asserted
                # by t_v1835_governance_failure_is_fail_closed.
                self._log(
                    f"governance check failed for {skill!r}: {e}; "
                    f"DEFER (not eligible)"
                )
                # Module-level function, NOT a method on this class.
                _record_error(e, "governance")
                return False, "governance_unavailable"

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
                if not isinstance(ccfg, dict):
                    ccfg = {}
                # v1.8.37 (audit F13): `cadence.enabled` was declared and never
                # read, so switching cadence off had no effect.
                if not bool(ccfg.get("enabled", True)):
                    raise _CadenceDisabled()
                target_n = (
                    int(ccfg.get("target_rollouts"))
                    if ccfg.get("target_rollouts") is not None else None
                )
                floor_s = (
                    int(ccfg.get("floor_seconds"))
                    if ccfg.get("floor_seconds") is not None else None
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
                # v1.8.37 (audit F13): `cadence.target_rollouts` was declared
                # and never passed, so compute_next_run always used its own
                # DEFAULT_TARGET. This is the "hot" threshold - how many new
                # rollouts earn the floor interval instead of the ceiling.
                if target_n is not None:
                    kwargs["cadence_target"] = target_n
                next_in_s = cadence.compute_next_run(new_n, **kwargs)
                if (time.time() - st.get("last_run_at", 0.0)) < next_in_s:
                    return False, f"cadence: not due for {next_in_s}s"
            except _CadenceDisabled:
                # Operator switched the cadence gate off: fall through to the
                # budget gate rather than treating it as an error.
                pass
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
                # v1.8.37 (audit F13): one factory, fully wired from the
                # `budget:` block. `daily_cap_cents` and `reset_hour_utc` were
                # declared but never supplied here, so the effective cap was
                # always the module default. Returns None when
                # `budget.enabled` is false, which nothing read before.
                bt = budget.tracker_from_config(skill, cfg)
                if bt is not None:
                    cost = _cost_per_call_cents(cfg)
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
                # v1.8.37 (audit F13): same factory as the eligibility gate,
                # so the tracker that RECORDS spend is configured identically
                # to the one that REFUSES it. They used to differ.
                # v1.8.45 (R10): through the factory even in the
                # disabled case, so "only the factory constructs a
                # tracker" is literally true rather than aspirational.
                bt = budget.tracker_from_config(
                    skill, cfg, default_when_disabled=True)
                cost = _cost_per_call_cents(cfg)
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
        # v1.8.48 (audit A8): `or 5` collapsed an explicit `0` - which an
        # operator writes to stop unattended writes - back to 5 per tick,
        # while the `< 1` clamp right below (dead under that idiom) proves
        # sub-1 values were expected. Default only when absent/unusable.
        max_per_tick = _int_or_default(cfg.get("auto_adopt_max_per_tick"), 5)
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
            if outcome.startswith("adopted"):
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
        # v1.8.39 (audit M9): sanitize before PERSISTING, not only before
        # egress. The frozen held-out task list is whole rollout records frozen
        # to disk, and the replay worker re-reads it - so an unsanitized
        # credential in a rollout that was captured before the privacy
        # controls existed (or that the best-effort redactor missed) was being
        # written verbatim into `logs/runs/` and then re-injected into a live
        # agent context. Sanitizing here means the durable artefact is safe
        # even if every future reader forgets.
        #
        # The gate's CONTENT is preserved: BOTH sides of every pair go through the
        # same transform, so the held-out measurement still compares like with
        # like. It is not a pure text rewrite, though - at the shipped
        # defaults `sanitize_rollout_records` POPS each trajectory step's
        # `args` and `result` (`privacy_include_tool_args` /
        # `privacy_include_tool_results` both default to false). That is
        # intended for privacy and it means the frozen records are
        # STRUCTURALLY lighter than the source rollouts, not merely redacted.
        # The gate only ever reads `task` / `last_response` / `trajectory`
        # step text, so the measured content is unchanged.
        #
        # v1.8.45 (audit R13): the helper being unimportable used to be a
        # SILENT skip - the tasks file was written with raw rollout records and
        # nothing said so. The sibling boundary (`bridge.py`) raises in the
        # same situation. Now: skip the real gate rather than persist
        # unsanitized records, and say why.
        _pv = None
        try:
            from usr.plugins.skillopt.helpers import privacy as _pv  # type: ignore
        except Exception:
            try:
                from helpers import privacy as _pv  # type: ignore
            except Exception:
                _pv = None  # type: ignore[assignment]
        if _pv is None:
            self._log(
                "real gate: privacy helper unavailable; SKIPPING the real gate "
                "rather than freezing unsanitized rollout records to disk")
            return None
        try:
            if not _pv.privacy_settings().get("redact_secrets", True):
                self._log(
                    "real gate: privacy_redact_secrets is false; freezing the "
                    "held-out records UNREDACTED by explicit operator request")
            else:
                held = _pv.sanitize_rollout_records(held)
        except Exception as e:
            self._log(
                f"real gate: task sanitization FAILED ({type(e).__name__}: {e}); "
                f"SKIPPING the real gate rather than freezing unsanitized "
                f"rollout records to disk")
            return None
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
            evalkit_tasks = str(cfg.get("replay_evalkit_tasks_file") or "").strip()
            if evalkit_tasks:
                try:
                    evalkit_max_tasks = max(1, min(
                        20, int(cfg.get("replay_evalkit_max_tasks", 4) or 4),
                    ))
                    evalkit_tasks = str(Path(evalkit_tasks).expanduser().resolve())
                    knobs += ["--evalkit-tasks-file", evalkit_tasks]
                    knobs += ["--evalkit-max-tasks", str(evalkit_max_tasks)]
                except (OSError, TypeError, ValueError) as exc:
                    self._log(f"real gate: independent evalkit config invalid: {exc}")
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
        # v1.8.48 (audit T7): this write was unchecked while the one three
        # lines earlier was not. A failure here leaves `pid: 0` in a sidecar
        # whose worker IS running - and `pid: 0` reads as "not live" to every
        # consumer, so single-flight would spawn a SECOND gate and the stale
        # window would park the proposal for hours. Log it loudly; the
        # proposal stays pending either way (that is the safe direction).
        if sleep_runner.write_real_gate_sidecar(src, payload) is None:
            self._log(
                f"real gate: could not record pid {payload['pid']} for "
                f"{skill_name}; single-flight and the stale window will "
                f"mis-report until the worker writes its own sidecar"
            )
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
                    # v1.8.45 (re-audit R10): through the factory, with `cfg`.
                    # This was a bare `BudgetTracker(skill_name=...)`, so the
                    # REAL GATE accumulated its spend against the MODULE
                    # DEFAULT cap rather than the operator's - and the drain
                    # checked against the operator's. Two different budgets
                    # for one skill, which is exactly what the v1.8.37 F13
                    # fix removed from nine other call sites and which
                    # reappeared here because nobody re-grepped.
                    bt = budget.tracker_from_config(skill_name, cfg)
                    if bt is not None:
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
        # v1.8.39 (audit M12): adoptions.log grew without bound.
        # Rotate before appending, like governance.log does.
        # Best-effort: a rotation failure must never stop an audit
        # row from being written.
        try:
            sleep_runner.rotate_log_if_large(audit)
        except Exception:
            pass

        if adopt:
            proposed = src.read_text(encoding="utf-8")
            # v1.8.34: the SAME autonomous rails as the normal drain path.
            # A real-gate-harvested adoption is still a write to a live
            # skill, and it is the path an unattended install takes most
            # often, so leaving it unrailed would bound autonomy only on the
            # rare path.
            _defer = self._autonomous_precheck(state, cfg, skill_name,
                                               Path(str(src)).stem)
            if _defer:
                self._log(f"autonomous: deferring harvest-adopt for "
                          f"{skill_name}: {_defer}")
                return "skipped"
            _before = ""
            try:
                if target.is_file():
                    _before = target.read_text(encoding="utf-8",
                                               errors="replace")
            except Exception:
                _before = ""
            # v1.8.24 (P0): snapshot + atomic replace. This path previously
            # called target.write_text() with no backup, so an unattended
            # auto-adopt could not be rolled back.
            _w = sleep_runner.adopt_write_skill(skill_name, proposed)
            if not _w.get("ok"):
                self._log(
                    f"adopt aborted for {skill_name}: {_w.get('error')}"
                )
                return _w.get("error", "adopt write failed")
            # v1.8.45 (audit R15): record that output redaction altered the live
            # document. The flag existed on the writer's result and was read
            # NOWHERE, so a redacted skill left no trace in any ledger - the
            # opposite of what `adopt_write_skill`'s own docstring claims.
            #
            # v1.8.48 (audit T3): this block was indented BEHIND the `return`
            # above and duplicated three times, so it never executed and the
            # harvest path - the one an unattended install takes most often -
            # recorded nothing. One copy, at the level where it runs.
            if _w.get("redacted"):
                self._log(
                    f"adopted {skill_name}: output redaction altered the "
                    f"document before it was written (credentials removed)")
                try:
                    from usr.plugins.skillopt.helpers import governance as _gov_r  # type: ignore
                except Exception:
                    try:
                        from helpers import governance as _gov_r  # type: ignore
                    except Exception:
                        _gov_r = None  # type: ignore[assignment]
                if _gov_r is not None:
                    try:
                        _gov_r.mark_human_decision(
                            skill_name, True, "redaction-notice",
                            proposal_id=Path(str(src)).stem,
                            note="output redaction altered this document before "
                                 "it was written into the live skill")
                    except Exception:
                        pass
            _rb = self._autonomous_postcheck(cfg, skill_name, _before)
            if _rb:
                self._log(f"autonomous: {_rb}")
            try:
                sleep_runner.clear_official_gate_marker(src)
            except Exception:
                pass
            state["proposals_adopted"] = int(state.get("proposals_adopted", 0)) + 1
            _save_state(state)
            # v1.8.34: counters + audit for a genuine autonomous harvest.
            try:
                from usr.plugins.skillopt.helpers import governance as _gh  # type: ignore
                _res = _gh.resolve_evolution_mode()
            except Exception:
                _res = None
            if _res and _res.get("autonomous"):
                _sum = None
                try:
                    from usr.plugins.skillopt.helpers import proposal_view  # type: ignore
                    _sum = proposal_view.get_summary(src, skill=skill_name)
                except Exception:
                    _sum = None
                _ev = (_sum or {}).get("evidence") or {}
                self._record_autonomous_adoption(state, skill_name, _ev)
                self._autonomous_audit(cfg, skill_name, Path(str(src)).stem,
                                        _sum, _ev)
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

    # ----------------------------------------------------------------- #
    # v1.8.34: autonomous-mode safety rails
    # ----------------------------------------------------------------- #
    #
    # `auto_adopt_max_per_tick` (5) times a 30-minute tick is a theoretical
    # ~240 writes/day to live SKILL.md files, with no daily bound at all.
    # That is the single most important thing missing from autonomous mode,
    # so the rails below bound HOW MUCH autonomy may do, independently of
    # WHETHER it is allowed to act.
    #
    # Every rail here returns a skip, NOT a quarantine: a daily cap that
    # resets tomorrow or a rollout threshold that will be crossed next week
    # is exactly the "may become eligible later" case, and the DOX rule is
    # that governance-style skips stay in staging while rejects and adopts
    # leave. The proposal must survive to be retried.

    def _autonomous_rails(self, state: dict[str, Any],
                          cfg: dict[str, Any]) -> tuple[dict, str]:
        """Resolve the rail config + the resolved mode. Never raises."""
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
        except Exception:
            try:
                from helpers import governance  # type: ignore
            except Exception:
                return {}, "governance unavailable"
        try:
            resolved = governance.resolve_evolution_mode() or {}
        except Exception:
            return {}, "mode resolution failed"
        if not resolved.get("autonomous"):
            # Manual mode: every rail is inert. Autonomy is what these
            # bound, and a human clicking Approve is not autonomous.
            return {}, ""
        try:
            rails = governance.autonomous_config(cfg) or {}
        except Exception:
            rails = {}
        return rails, ""

    def _autonomous_precheck(
        self, state: dict[str, Any], cfg: dict[str, Any], skill_name: str,
        proposal_id: str,
    ) -> str:
        """Reason string when this adoption must be DEFERRED, else "".

        Checked before the write, so a refused adoption leaves the proposal
        in staging untouched.
        """
        rails, err = self._autonomous_rails(state, cfg)
        if err:
            return err
        if not rails:
            return ""
        try:
            today = time.strftime("%Y-%m-%d")
            counters = state.get("autonomous_adoptions")
            if not isinstance(counters, dict):
                counters = {}
            day = counters.get(today)
            if not isinstance(day, dict):
                day = {"count": 0, "skills": {}}
            per_day = int(rails.get("max_adoptions_per_day", 0) or 0)
            count = int(day.get("count", 0) or 0)
            if per_day >= 0 and count >= per_day:
                return (f"autonomous daily cap reached ({count}/{per_day} "
                        f"on {today}); stays in staging until the cap resets")
            per_skill_cap = int(
                rails.get("max_adoptions_per_skill_per_day", 0) or 0)
            skills = day.get("skills") if isinstance(day.get("skills"), dict) else {}
            used = int(skills.get(skill_name, 0) or 0)
            if per_skill_cap >= 0 and used >= per_skill_cap:
                return (f"autonomous per-skill cap reached for {skill_name} "
                        f"({used}/{per_skill_cap} on {today})")
            min_rollouts = int(rails.get("min_evidence_rollouts", 0) or 0)
            if min_rollouts > 0:
                have = self._skill_rollout_count(skill_name)
                if have is not None and have < min_rollouts:
                    return (f"insufficient evidence for autonomous adoption: "
                            f"{have} rollout(s) for {skill_name}, "
                            f"need {min_rollouts}")
        except Exception as e:
            # A rail that cannot be evaluated must DEFER, not wave through:
            # the safe direction for a machine-written change to a live file.
            return f"autonomous rail check failed ({type(e).__name__}); deferring"
        return ""

    def _skill_rollout_count(self, skill_name: str) -> int | None:
        """Rollouts recorded for this skill. None when uncomputable.

        Cached per TICK, not for the thread's lifetime: the cache lives on
        the single long-lived AutoLoopThread instance, so without an explicit
        invalidate the counts would be frozen at whatever the first
        autonomous drain ever saw - a permanent, invisible staleness that
        would make `autonomous_min_evidence_rollouts` compare against a
        snapshot from hours or days earlier. `_invalidate_rollout_counts` is
        called at the top of `_tick`.
        """
        cached = getattr(self, "_rollout_counts_cache", None)
        if cached is None:
            cached = {}
            try:
                for rp in sleep_runner.rollouts_dir().glob("*.json"):
                    try:
                        rec = json.loads(
                            rp.read_text(encoding="utf-8", errors="replace"))
                    except Exception:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    name = str(rec.get("skill_used") or rec.get("skill")
                               or rec.get("skill_name") or "").strip()
                    if name:
                        cached[name] = cached.get(name, 0) + 1
            except Exception:
                return None
            self._rollout_counts_cache = cached
        return int(cached.get(skill_name, 0))

    def _invalidate_rollout_counts(self) -> None:
        """Drop the per-tick rollout-count cache.

        v1.8.35 (audit F5): this was a `@staticmethod` assigning
        `AutoLoopThread._rollout_counts_cache = None`, i.e. a CLASS attribute,
        while `_skill_rollout_count` reads `getattr(self, ...)` and writes
        `self._rollout_counts_cache`. Once the instance attribute existed it
        shadowed the class one permanently, so the per-tick invalidation was a
        no-op and the cache stayed frozen at whatever the first drain saw -
        exactly the staleness the comment claimed to prevent. It must be an
        INSTANCE method for the invalidation to take effect.
        """
        self._rollout_counts_cache = None

    def _record_autonomous_adoption(self, state: dict[str, Any],
                                     skill_name: str,
                                     evidence: dict | None = None) -> None:
        """Bump the daily counters in the SINGLE-WRITER state file.

        The auto-loop thread is the only writer of `.auto_loop_state.json`
        (child DOX, "State files are single-writer"); this goes through the
        same `_save_state` as every other counter and never opens the file
        directly.
        """
        try:
            today = time.strftime("%Y-%m-%d")
            counters = state.get("autonomous_adoptions")
            if not isinstance(counters, dict):
                counters = {}
            # Retain today plus the previous day only, so the block cannot
            # grow forever in a long-lived install.
            for stale in [k for k in list(counters) if k < today]:
                counters.pop(stale, None)
            day = counters.get(today)
            if not isinstance(day, dict):
                day = {"count": 0, "skills": {}}
            day["count"] = int(day.get("count", 0) or 0) + 1
            skills = day.get("skills")
            if not isinstance(skills, dict):
                skills = {}
            skills[skill_name] = int(skills.get(skill_name, 0) or 0) + 1
            day["skills"] = skills
            day["last_skill"] = skill_name
            day["last_at"] = time.time()
            counters[today] = day
            state["autonomous_adoptions"] = counters
            _save_state(state)
        except Exception as e:
            self._log(f"autonomous counter update failed: {e}")

    def _autonomous_audit(self, cfg: dict[str, Any], skill_name: str,
                           proposal_id: str, summary: dict | None,
                           evidence: dict | None) -> None:
        """Write the `decided_by="autonomous"` governance row.

        Reuses `mark_human_decision` rather than adding a second ledger: its
        `decided_by` parameter already discriminates human from machine, and
        one queryable history beats two. A machine decision that is not
        permanently distinguishable from a human one would make the
        off-by-default guarantee unverifiable after the fact.

        Best-effort: never raises, never blocks the adoption.
        """
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
        except Exception:
            try:
                from helpers import governance  # type: ignore
            except Exception:
                return
        try:
            rails = governance.autonomous_config(cfg) or {}
        except Exception:
            rails = {}
        if not rails.get("audit", True):
            return
        try:
            ev = dict(evidence or {})
            if summary:
                sdiff = summary.get("diff") or {}
                ev.setdefault("added_lines", sdiff.get("added_lines"))
                ev.setdefault("removed_lines", sdiff.get("removed_lines"))
                intent = summary.get("intent") or {}
                ev.setdefault("intent", intent.get("category"))
            resolved = governance.resolve_evolution_mode() or {}
            governance.mark_human_decision(
                skill_name, True, "autonomous",
                proposal_id=proposal_id,
                note=("mode=%s source=%s intent=%s lift_pp=%s n_held_out=%s"
                      % (resolved.get("mode"), resolved.get("source"),
                         ev.get("intent"), ev.get("lift_pp"),
                         ev.get("n_held_out")))[:500],
            )
            self._log(
                f"autonomous: recorded decided_by=autonomous for "
                f"{skill_name} proposal {proposal_id} ({ev.get('intent')})"
            )
        except Exception as e:
            self._log(f"autonomous audit write failed for {skill_name}: {e}")

    def _autonomous_postcheck(self, cfg: dict[str, Any], skill_name: str,
                              before: str) -> str:
        """Verify the write landed intact; roll back when it did not.

        Returns "" when fine, else a reason describing the rollback. This is
        a STRUCTURAL verification (file present, non-empty, frontmatter
        intact), not a re-measurement: it catches a truncated or corrupt
        write, which is the failure mode an automatic path can least afford
        to leave in place. Re-running the held-out measurement belongs to the
        real-gate executor, whose verdict is already required (or
        deliberately bypassed) upstream.
        """
        try:
            rails, err = self._autonomous_rails({}, cfg)
            if err or not rails:
                return ""
            if not rails.get("require_post_adopt_check", True):
                return ""
            try:
                target = sleep_runner.safe_skill_md(skill_name)
                text = target.read_text(encoding="utf-8") if target.is_file() else ""
            except Exception as e:
                return f"post-adopt verification could not read {skill_name}: {e}"
            if not text.strip():
                return self._rollback_autonomous(cfg, skill_name,
                                                 "post-adopt write is empty")
            if before and before.strip() and "---\n" in before \
                    and not text.lstrip().startswith("---"):
                return self._rollback_autonomous(
                    cfg, skill_name,
                    "post-adopt write lost its YAML frontmatter (unloadable)")
            return ""
        except Exception as e:
            self._log(f"post-adopt check failed for {skill_name}: {e}")
            return ""

    def _rollback_autonomous(self, cfg: dict[str, Any], skill_name: str,
                             why: str) -> str:
        """Restore the pre-adopt snapshot. Never raises."""
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
        except Exception:
            try:
                from helpers import fragment_store  # type: ignore
            except Exception:
                return f"{why}; rollback unavailable"
        try:
            res = fragment_store.restore_default_snapshot(skill_name)
            if res.get("ok"):
                self._log(f"autonomous ROLLBACK {skill_name}: {why}")
                return f"{why}; rolled back to the pre-adopt snapshot"
            return f"{why}; rollback FAILED ({res.get('error')})"
        except Exception as e:
            return f"{why}; rollback raised {type(e).__name__}: {e}"

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
            # v1.8.18 (P2.1): autonomy is the operator's opt-in - it
            # overrides the per-skill pending-approval block.
            # v1.8.34: the value comes from the single resolver, never from
            # a bare `bool(cfg.get("auto_adopt", False))`.
            #
            # NO TRAILING COMMA HERE - the first version of the P2.1 edit
            # ended this call in a comma, wrapping the 2-tuple result in a
            # 1-tuple. The unpack raised "not enough values to unpack
            # (expected 2, got 1)" on EVERY _auto_adopt call, was silently
            # swallowed by the except below, so governance logged no
            # decisions and the gate always fell through. The v1.5.0 smoke
            # test caught it; keep it that way.
            eligible, gov_reason = governance.check_skill_eligible(
                skill_name,
                auto_adopt=bool(governance.resolve_evolution_mode()
                                .get("autonomous")))
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
            # v1.8.42 (re-audit R2): FAIL CLOSED.
            #
            # This used to log "fall through" and continue into
            # `validate_proposal` -> `adopt_write_skill` -> a live write.
            # `_auto_adopt` calls `_adopt_one` DIRECTLY and never routes
            # through `_skill_eligible_for_cycle`, so this was the ONLY
            # governance evaluation on the drain's path to a live
            # `usr/skills/<n>/SKILL.md`. A raising check - an unreadable
            # ledger, a `mkdir` on a full disk, an ImportError - therefore
            # disabled optout, immutable, pause, cadence, budget AND the
            # human-rejection rail for that proposal, and the drain wrote a
            # live skill for a skill the operator had opted out of.
            #
            # v1.8.35's F8 fix corrected the SIBLING gate
            # (`_skill_eligible_for_cycle`) and the F8 test drove only that
            # sibling, so the fix looked complete and was not. That is the
            # same shape as re-audit R1: a partial fix, covered at the wrong
            # site.
            #
            # "skipped" is the correct outcome, not a new one: a governance
            # we cannot evaluate is a DEFERRAL, so the proposal STAYS in
            # staging and may become eligible next tick. It is not a
            # rejection, and it must never be a write.
            self._log(
                f"governance: {skill_name} check FAILED ({e}); DEFER "
                f"(staying in staging, not writing)"
            )
            try:
                _record_error(e, "governance_adopt_one")
            except Exception:
                pass
            return "skipped"

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

        # v1.8.50 (audit F5): no `target.parent.mkdir()` here. This is the drain's
        # own path to a live skill, and it created the directory BEFORE the
        # governance gate ran - so a proposal the gate then refused still left
        # an empty `usr/skills/<name>/` behind. `adopt_write_skill` creates the
        # parent itself. v1.8.48 removed exactly this line from the agent tool
        # (audit U10) and missed the drain; api/adopt.py has never had it.
        target = sleep_runner.safe_skill_md(skill_name)
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
            # v1.8.45 (re-audit R11): `passed` recorded the GATE's verdict
            # before anything was written, and every later refusal returned
            # without correcting it - so a snapshot failure or an ungated
            # refusal left an audit row asserting a PASSING ADOPTION for a
            # write that never landed. `api/adopt.py` already wrote a
            # correction row on refusal; the drain did not.
            #
            # The gate verdict moves to its own key, and `passed` /`outcome`
            # are written by `_audit` from the RESOLVED outcome on every exit.
            # This dict deliberately carries no `passed` key: `_audit` is the
            # only writer and always overwrites it, so a value here would be
            # dead - and a dead `passed` in an audit row is exactly the kind of
            # field a future reader trusts.
            "gate_passed": ok,
            "reason": reason,
            "held_out": held_out,
            # v1.8.22: distinguish confirm-pending / bypassed adoptions
            # from adopt-immediately in adoptions.log.
            "real_gate": "bypassed" if official_gated else None,
        }

        def _audit(outcome: str, *, note: str = "",
               redacted: bool = False) -> None:
            """Write the adoption row with the RESOLVED outcome.

            One writer for the row, called on every exit from here - success,
            refusal, deferral. That is the point: a trail that only records the
            happy path is not a trail.
            """
            row = dict(entry)
            row["passed"] = outcome.startswith("adopted")
            row["outcome"] = outcome
            if note:
                row["outcome_note"] = note[:300]
            # v1.8.45 (R15): the writer redacted this document, so the
            # live skill is NOT byte-identical to what was proposed.
            # Say so in the trail rather than leaving it implied.
            if redacted:
                row["output_redacted"] = True
            audit = sleep_runner.runs_dir() / "adoptions.log"
            try:
                audit.parent.mkdir(parents=True, exist_ok=True)
                # v1.8.39 (audit M12): adoptions.log grew without bound.
                try:
                    sleep_runner.rotate_log_if_large(audit)
                except Exception:
                    pass
                with open(audit, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception as e:
                self._log(f"adoption audit write failed for {skill_name}: {e}")

        _audit("proposed")

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
            # v1.8.45 (R11): record the refusal, so the trail does not keep
            # asserting a proposed-but-unwritten adoption.
            _audit("ungated_blocked")
            return "ungated_blocked"

        if ok:
            # v1.8.34: autonomous rails, checked BEFORE the write so a
            # deferred adoption leaves the proposal in staging untouched.
            # Inert in manual mode - these bound machine-written changes.
            defer = self._autonomous_precheck(state, cfg, skill_name, src.stem)
            if defer:
                self._log(f"autonomous: deferring {skill_name}: {defer}")
                _audit("deferred", note=defer)
                return "skipped"

            # v1.8.24 (P0): snapshot + atomic replace, same reason as the
            # real-gate harvest path above.
            _current_before = ""
            try:
                _t = sleep_runner.safe_skill_md(skill_name)
                if _t.is_file():
                    _current_before = _t.read_text(encoding="utf-8",
                                                   errors="replace")
            except Exception:
                _current_before = ""
            _w = sleep_runner.adopt_write_skill(skill_name, proposed)
            if not _w.get("ok"):
                self._log(f"adopt aborted for {skill_name}: {_w.get('error')}")
                # v1.8.45 (R11): the write was REFUSED - most commonly a
                # failed snapshot, which is the unrecoverable case. The
                # provisional row said `passed: True`; correct it.
                _audit("refused", note=str(_w.get("error") or "write refused"))
                return _w.get("error", "adopt write failed")

            # v1.8.34: post-adopt verification + automatic rollback.
            rollback_note = self._autonomous_postcheck(cfg, skill_name,
                                                      _current_before)
            if rollback_note:
                self._log(f"autonomous: {rollback_note}")
                # v1.8.50 (audit F10): a post-adopt ROLLBACK restored the
                # previous bytes, so the document is no longer what this
                # adoption wrote - yet the audit row said `adopted` and the
                # log line said "ADOPTED". Anyone reading the trail to answer
                # "is the live skill the one the gate approved?" was told
                # yes. The write DID happen (and the counters are right); the
                # outcome string has to carry what the file now contains.
                outcome = "adopted_rolled_back"

            # v1.8.34: the informed-approval evidence, read from the summary
            # sidecar the staging write already produced. Best-effort: an
            # absent sidecar must not change what happens next.
            _summary = None
            try:
                from usr.plugins.skillopt.helpers import proposal_view  # type: ignore
                _summary = proposal_view.get_summary(src, skill=skill_name)
            except Exception:
                _summary = None
            _evidence = (_summary or {}).get("evidence") or {}

            # v1.8.18 (P2.1): the gate marker is cleared AFTER the write, so
            # the proposal's provenance survives long enough for the summary
            # and evidence read above.
            try:
                sleep_runner.clear_official_gate_marker(src)
            except Exception:
                pass
            state["proposals_adopted"] = int(state.get("proposals_adopted", 0)) + 1
            _save_state(state)

            # v1.8.34: bump the daily counters and write the audit row, but
            # ONLY when this was genuinely an autonomous adoption. A manual
            # /adopt must not consume the machine budget.
            _resolved = None
            try:
                from usr.plugins.skillopt.helpers import governance as _g  # type: ignore
                _resolved = _g.resolve_evolution_mode()
            except Exception:
                _resolved = None
            if _resolved and _resolved.get("autonomous"):
                self._record_autonomous_adoption(state, skill_name, _evidence)
                self._autonomous_audit(cfg, skill_name, src.stem, _summary,
                                        _evidence)
            self._log(
                f"auto-loop: ADOPTED {skill_name} ({reason})"
                + (f" [autonomous; {rollback_note}]" if rollback_note else "")
            )
            if not rollback_note:
                outcome = "adopted"
            # v1.8.45 (R11): record the RESOLVED outcome. The provisional
            # row written before the gate said `passed: True` for a write
            # that had not happened yet; this is the row that means it did.
            # `outcome` is "adopted_rolled_back" when the post-adopt check
            # reverted the document (audit F10) - the row template carries no
            # `passed` key, so the row never claims more than it knows.
            _audit(outcome, note=rollback_note,
                   redacted=bool(_w.get("redacted")))
        else:
            state["proposals_rejected"] = int(state.get("proposals_rejected", 0)) + 1
            _save_state(state)
            self._log(f"auto-loop: rejected {skill_name} ({reason})")
            outcome = "rejected"
            # v1.8.45 (R11): a gate rejection is an outcome too.
            _audit("rejected", note=reason)
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
            # v1.8.50 (audit F10): the RESOLVED outcome, not `ok` - a rolled-back
            # adoption is an adoption that was reverted, and a reader asking
            # "what does the live skill contain?" must be able to tell.
            outcome_str = outcome if outcome else ("adopted" if ok else "rejected")
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
        # v1.8.50 (audit F10): prefix, not equality. A post-adopt ROLLBACK still
        # CONSUMES the proposal and still counts as an adoption - the write
        # happened and the counters above are right - but its outcome string
        # carries the revert. An exact comparison here quarantined a proposal
        # whose only fault was that the safety net caught its own write.
        if outcome.startswith("adopted"):
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
    # v1.8.34: today's autonomous adoption count + per-skill breakdown, so the
    # Evolution mode card can show consumption against the daily cap. READ
    # only - the auto-loop thread remains the single writer of this file.
    try:
        counters = state.get("autonomous_adoptions")
        if isinstance(counters, dict):
            today = time.strftime("%Y-%m-%d")
            day = counters.get(today) if isinstance(counters.get(today), dict) else {}
            state["autonomous_adoptions_today"] = int(day.get("count", 0) or 0)
            state["autonomous_adoptions_skills_today"] = (
                day.get("skills") if isinstance(day.get("skills"), dict) else {}
            )
        else:
            state["autonomous_adoptions_today"] = 0
            state["autonomous_adoptions_skills_today"] = {}
    except Exception:
        state["autonomous_adoptions_today"] = 0
        state["autonomous_adoptions_skills_today"] = {}
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
    # v1.8.37 (audit F13): wired from the `budget:` block, so the dashboard
    # reports the operator's real cap rather than the module default.
    # v1.8.45 (R10): see above - the factory, not a direct build.
    bt = budget.tracker_from_config(
        skill_name, sleep_runner.merged_config(),
        default_when_disabled=True)
    return {"enabled": True, **bt.get_status()}


def can_skill_spend(skill_name: str, cents: int) -> tuple:
    """Check if a skill can spend `cents` more today. Returns (ok, reason)."""
    if budget is None:
        return (True, "")
    # v1.8.37 (audit F13): same wiring as the gate, so this can never be more
    # permissive than `_skill_eligible_for_cycle`'s budget check.
    bt = budget.tracker_from_config(skill_name, sleep_runner.merged_config())
    if bt is None:
        return (True, "")   # budget gate disabled
    return bt.can_spend(cents)


def record_skill_spend(skill_name: str, cents: int) -> dict:
    """Record a spend of `cents` for `skill_name`."""
    if budget is None:
        return {"recorded": 0, "new_total": 0, "day": ""}
    # v1.8.37 (audit F13): must use the SAME cap as the checker, or spend is
    # accumulated against a different budget than the one that refused it.
    bt = budget.tracker_from_config(skill_name, sleep_runner.merged_config())
    if bt is None:
        return {"recorded": 0, "new_total": 0, "day": ""}
    return bt.record_spend(cents)
