"""SkillOpt official-engine adapter (v1.6.0).

Bridges the A0 auto-loop to the upstream Microsoft `skillopt` /
`skillopt_sleep` packages (installed into the A0 venv by hooks.install()).
When `use_official_engine` is true in config AND the package is
importable, the auto-loop drives the official Sleep engine here instead
of the hand-rolled helpers/direct_optimizer.py. When the official package
is absent or the run fails, the loop falls back to direct_optimizer —
the existing working path — so this adapter can never make things worse.

WHY DRIVE THE OFFICIAL ENGINE
  The official package implements the research-grade optimizer the
  SkillOpt paper describes: epochs, edit-budget "learning rate", bounded
  update modes (patch / rewrite / minibatch), slow-update, meta-skill,
  and a strict monotonic validation gate
  (skillopt.evaluation.gate.evaluate_gate). The local direct_optimizer is
  a single full-rewrite LLM call with a weaker structural gate. Bridging
  to the official engine is the highest-leverage improvement (plan
  Solution B).

DESIGN CONSTRAINTS
  1. The official package lives in the A0 venv, which may differ from
     the current interpreter (it is NOT installed in every env — e.g.
     not in the dev shell). Availability is therefore probed via a
     subprocess of sleep_runner._a0_python(), and the result is cached
     for PROBE_TTL seconds so we don't fork on every tick.
  2. The official Sleep engine runs its OWN internal monotonic gate
     before staging a proposal. A proposal that reaches staging/ has
     already passed the official held-out gate. The local
     sleep_runner.validate_proposal() then only needs the cheap
     structural pre-filter — see the official_gated=True flag wired in
     Phase 2. We do NOT call skillopt.evaluation.gate.evaluate_gate
     directly here: its exact signature has not been verified against
     an installed package in this environment, and the engine's own
     pre-staging gate already provides the authoritative held-out
     decision. Calling the Python gate API directly is a documented
     future refinement (see _try_evaluate_gate stub).
  3. All failures are fail-soft. run_official_sleep_cycle() returns
     {ok: False, fallback_to_direct: True, reason} on ANY error (bad
     verb, timeout, no staged output, import failure). The auto-loop
     falls back to direct_optimizer. Never crash, never stall
     evolution on an unverified integration.

UNVERIFIED API NOTE
  The `skillopt_sleep` CLI verb set (`run` / `cycle` / `harvest` /
  `adopt` / `status`) and the `evaluate_gate` signature are taken from
  the upstream README + paper and have NOT been verified against an
  installed package here (the package is not installed in this env; the
  live verification step in the plan covers this). The run verb is
  configurable via `official_run_verb` so the user can match whatever the
  installed version expects without a code change.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:
    from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
except Exception:
    from helpers import sleep_runner  # type: ignore  # noqa: F401


# ----------------------------------------------------------------------- #
# Availability probe (cached, subprocess-based)
# ----------------------------------------------------------------------- #

PROBE_TTL = 60.0  # re-probe at most once a minute
_probe_cache: dict[str, Any] = {"at": 0.0, "result": None}


def _a0_python() -> str:
    return sleep_runner._a0_python()


def probe_official(force: bool = False) -> dict[str, Any]:
    """Return {available, version, path, error, py}.

    Probes whether `skillopt_sleep` is importable in the A0 venv by
    running a short subprocess of the A0 python. Cached for PROBE_TTL
    seconds so a tick never forks more than once a minute. Best-effort:
    any probe error returns {available: False, error: ...} rather than
    raising.
    """
    now = time.time()
    if not force and _probe_cache["result"] is not None and (now - _probe_cache["at"]) < PROBE_TTL:
        return _probe_cache["result"]
    py = _a0_python()
    info: dict[str, Any] = {
        "available": False,
        "version": None,
        "path": None,
        "py": py,
        "error": None,
    }
    import subprocess
    try:
        out = subprocess.check_output(
            [py, "-c",
             "import skillopt_sleep as s; "
             "print(getattr(s,'__version__','unknown')); "
             "print(s.__file__)"],
            stderr=subprocess.STDOUT, text=True, timeout=20,
        )
        parts = out.strip().splitlines()
        info["available"] = True
        info["version"] = parts[0] if parts else "unknown"
        info["path"] = parts[1] if len(parts) > 1 else None
    except subprocess.CalledProcessError as e:
        info["error"] = (e.output or "").strip()[:200] if isinstance(e.output, str) else "not installed"
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    _probe_cache["at"] = now
    _probe_cache["result"] = info
    return info


# ----------------------------------------------------------------------- #
# Config -> official CLI arg mapping
# ----------------------------------------------------------------------- #

def _str_cfg(cfg: dict[str, Any], key: str) -> str | None:
    v = cfg.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _build_run_args(cfg: dict[str, Any], target: str | None) -> list[str]:
    """Map A0 config keys to `skillopt_sleep <verb>` CLI args.

    Conservative: only pass args we have values for. The verb itself is
    `official_run_verb` (default "run"); the user can override it to
    match the installed package's actual command (e.g. "cycle").
    """
    args: list[str] = []
    if target:
        args += ["--skill", str(target)]
    backend = _str_cfg(cfg, "official_backend")
    if backend:
        args += ["--backend", backend]
    opt_model = _str_cfg(cfg, "official_optimizer_model") or _str_cfg(cfg, "optimizer_model")
    if opt_model:
        args += ["--optimizer-model", opt_model]
    tgt_model = _str_cfg(cfg, "official_target_model") or _str_cfg(cfg, "target_model")
    if tgt_model:
        args += ["--target-model", tgt_model]
    # The official engine stages by default; we never pass --auto-adopt
    # here — our local _auto_adopt + validate_proposal decide promotion
    # so the safety wrapper stays in our control.
    lookback = cfg.get("official_lookback_hours")
    if lookback:
        try:
            args += ["--lookback-hours", str(int(lookback))]
        except (TypeError, ValueError):
            pass
    max_tasks = cfg.get("official_max_tasks")
    if max_tasks:
        try:
            args += ["--max-tasks", str(int(max_tasks))]
        except (TypeError, ValueError):
            pass
    return args


# ----------------------------------------------------------------------- #
# Staged-proposal discovery + rename
# ----------------------------------------------------------------------- #

def _find_staged(target: str | None) -> Path | None:
    """Locate the proposal the official engine wrote to staging/.

    The engine's `consolidate` step writes `best_skill.md` (generic). The
    auto-loop's _auto_adopt looks for `staging/<skill>.md`. When a target
    skill is known we rename best_skill.md -> <skill>.md so the existing
    adopt path finds it unchanged. When no target is set we leave
    best_skill.md in place (the adopt path's find_staged_proposals() also
    matches .md files).
    """
    sd = sleep_runner.staging_dir()
    if target:
        named = sd / f"{target}.md"
        if named.is_file():
            return named
        best = sd / "best_skill.md"
        if best.is_file():
            try:
                best.replace(named)
                return named
            except Exception:
                return best
    # No target: prefer best_skill.md, else first .md
    best = sd / "best_skill.md"
    if best.is_file():
        return best
    for child in sorted(sd.iterdir()):
        if child.is_file() and child.suffix == ".md":
            return child
    return None


# ----------------------------------------------------------------------- #
# Main entry: run one official Sleep cycle
# ----------------------------------------------------------------------- #

def run_official_sleep_cycle(
    target: str = "",
    custom_prompts: dict[str, str] | None = None,
    cfg: dict[str, Any] | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Run one official `skillopt_sleep` cycle for `target` (or all skills).

    Returns:
      {ok: True,  engine: "official", staged_path, held_out, log_path, pid}
      {ok: False, fallback_to_direct: True, reason, ...}  on any failure

    The auto-loop calls this when `use_official_engine` is true and
    probe_official()["available"] is true; on a fallback result it
    retries with direct_optimizer.run_direct_cycle().

    `custom_prompts` is accepted for signature parity with the direct
    optimizer but is NOT forwarded to the official engine (it does not
    take free-form prompts); the inner-loop suggestions are still
    consumed by the direct fallback path.
    """
    cfg = cfg or {}
    probe = probe_official()
    if not probe.get("available"):
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": f"official package not available: {probe.get('error') or 'not installed'}",
        }

    verb = _str_cfg(cfg, "official_run_verb") or "run"
    extra = _build_run_args(cfg, target or None)

    # Reuse sleep_runner's detached launcher: it bridges rollouts into the
    # Claude Code history format the engine's harvest expects, builds the
    # subprocess env from .skillopt-env, and writes a timestamped log file
    # we can parse for the held-out score.
    try:
        launch = sleep_runner.launch_sleep_subprocess(
            verb=verb,
            extra_args=extra,
            log_name=f"sleep-official-{time.strftime('%Y%m%dT%H%M%S')}.log",
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": f"launch failed: {type(e).__name__}: {e}",
        }

    pid = launch["pid"]
    log_path = launch["log_path"]

    # Poll the detached subprocess until it finishes or we time out.
    # We never kill it — if it outlives our timeout we leave it running
    # (detached) and fall back this tick; a later tick will pick up the
    # staged proposal it eventually produces.
    deadline = time.time() + max(30, int(timeout_s))
    while time.time() < deadline:
        try:
            if not sleep_runner.is_running(pid):
                break
        except Exception:
            break
        time.sleep(2)

    try:
        still_running = sleep_runner.is_running(pid)
    except Exception:
        still_running = False
    if still_running:
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": f"official run timed out after {timeout_s}s (left detached)",
            "pid": pid,
            "log_path": log_path,
        }

    held_out = sleep_runner.parse_held_out(log_path)
    staged = _find_staged(target or None)
    if not staged or not staged.is_file():
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": "official run produced no staged proposal",
            "pid": pid,
            "log_path": log_path,
            "held_out": held_out,
        }
    try:
        size = staged.stat().st_size
    except Exception:
        size = 0
    return {
        "ok": True,
        "engine": "official",
        "staged_path": str(staged),
        "held_out": held_out,
        "log_path": log_path,
        "pid": pid,
        "staged_size": size,
    }


# ----------------------------------------------------------------------- #
# Future hook: direct evaluate_gate delegation (not wired authoritatively)
# ----------------------------------------------------------------------- #

def _try_evaluate_gate(
    current_score: float,
    candidate_score: float,
    best_score: float,
    metric: str = "hard",
) -> dict[str, Any] | None:
    """Best-effort import + call of skillopt.evaluation.gate.evaluate_gate.

    Returns {accepted: bool, action: str, reason: str} if the official
    gate was importable and callable with the args it accepts, or None if
    it is unavailable or its signature could not be satisfied.

    NOT wired as authoritative today: the official Sleep engine already
    enforces the monotonic held-out gate before staging a proposal, so a
    staged proposal has passed it by construction. This stub is kept so a
    future refinement can make the Python gate authoritative without
    touching the auto-loop — and so tests can assert it degrades to None
    when the package is absent.
    """
    try:  # pragma: no cover - exercised only when the package is installed
        import inspect  # type: ignore
        from skillopt.evaluation import gate as _gate  # type: ignore
        fn = getattr(_gate, "evaluate_gate", None)
        if fn is None:
            return None
        sig = inspect.signature(fn)
        kwargs: dict[str, Any] = {}
        params = set(sig.parameters)
        if "current_score" in params:
            kwargs["current_score"] = current_score
        if "cand_score" in params or "candidate_score" in params:
            key = "cand_score" if "cand_score" in params else "candidate_score"
            kwargs[key] = candidate_score
        if "best_score" in params:
            kwargs["best_score"] = best_score
        if "metric" in params:
            kwargs["metric"] = metric
        result = fn(**kwargs)
        # GateResult is immutable; normalise the bits we care about.
        accepted = bool(getattr(result, "accepted", getattr(result, "accept", None)))
        action = str(getattr(result, "action", ""))
        return {"accepted": accepted, "action": action, "reason": str(result)}
    except Exception:
        return None


__all__ = [
    "probe_official",
    "run_official_sleep_cycle",
]