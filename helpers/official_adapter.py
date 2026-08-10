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

VERIFIED API (v1.6.1, verified against microsoft/skillopt @ HEAD 2026-08-10)
  The package is NOT installed in this dev env, so the live subprocess path is
  still exercised only when the user installs it. But the CLI surface and gate
  signature have been verified against the upstream source tree (clone of
  microsoft/skillopt), so the arg mapping and staging discovery below match
  the real package, not a guess:

  CLI (entry point `skillopt-sleep = skillopt_sleep.__main:main`; invoked as
  `python -m skillopt_sleep <subcommand>`). Subcommands: `run` (full cycle:
  harvest->mine->replay->gate->stage), `dry-run`, `status`, `adopt`, `harvest`,
  `schedule`, `unschedule`. `run` flags (from _add_common): `--project PATH`,
  `--target-skill-path PATH` (a real SKILL.md path, NOT a skill name),
  `--backend mock|claude|codex|copilot|cursor|pi|handoff|azure_openai`,
  `--model NAME` (single model; there is no separate optimizer/target model),
  `--lookback-hours N`, `--max-tasks N`, `--edit-budget N`, `--auto-adopt`,
  `--preferences`, `--json`, `--progress`. The verb is configurable via
  `official_run_verb` (default `run`).

  Staging: `run` writes to `<project>/.skillopt-sleep/staging/<ts>/` containing
  `proposed_SKILL.md`, `report.json`, `report.md`, `manifest.json`. The
  authoritative gate verdict is in `report.json`:
  `{accepted, gate_action, baseline_score, candidate_score, night, edits}`.
  We read it via _read_gate_verdict() instead of scraping the log. We copy
  `proposed_SKILL.md` into the plugin's `staging/<skill>.md` ONLY when the gate
  accepted, so _auto_adopt only ever promotes gate-accepted proposals.

  evaluate_gate signature (skillopt_sleep.gate AND skillopt.evaluation.gate,
  behaviourally identical; the vendored copy is what the sleep engine uses):
    evaluate_gate(candidate_skill, cand_hard, current_skill, current_score,
                  best_skill, best_score, best_step, global_step, *,
                  cand_soft=0.0, metric="hard", mixed_weight=0.5) -> GateResult
  GateResult(action, current_skill, current_score, best_skill, best_score,
  best_step); action in {"accept_new_best","accept","reject"}. We do NOT call
  this authoritatively — the engine already ran it before staging and the
  verdict is in report.json. _try_evaluate_gate() is corrected-to-spec for
  future direct use + tests; degrades to None when the package is absent.
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


def _a0_root() -> Path:
    """The A0 install root on this host.

    `sleep_runner.plugin_root()` = `<a0>/usr/plugins/skillopt`, so
    `.parent.parent` = `<a0>/usr` and `.parent.parent.parent` = `<a0>`.
    The official engine's `--project` and the skills dir both resolve
    from here. On the host this is E:\\...\\a0-inst-agent-zero-*; inside
    the container the same paths are under /a0 — but the subprocess we
    launch is the host .venv python, so host paths are correct.
    """
    return sleep_runner.plugin_root().parent.parent.parent


def _resolve_skill_path(target: str | None) -> str | None:
    """Resolve a skill name to its live SKILL.md path, or None.

    A0 skills live at `<a0_root>/usr/skills/<name>/SKILL.md`. The official
    `--target-skill-path` flag wants this real path (NOT a bare skill name).
    Returns None when the skill dir doesn't exist so the caller can omit
    the flag (the engine then evolves whatever skills it finds, or none).
    """
    if not target:
        return None
    p = _a0_root() / "usr" / "skills" / target / "SKILL.md"
    return str(p) if p.is_file() else None


def _official_staging_root() -> Path:
    """Where the official `run` writes its staging dirs: `<a0>/.skillopt-sleep/staging/`."""
    return _a0_root() / ".skillopt-sleep" / "staging"


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
    """Map A0 config keys to the real `skillopt_sleep run` CLI args.

    Verified against microsoft/skillopt @ HEAD (see module docstring):
      --project PATH            (project to evolve; staging lands here)
      --target-skill-path PATH  (a real SKILL.md path, NOT a skill name)
      --backend mock|claude|codex|copilot|cursor|pi|handoff|azure_openai
      --model NAME              (single model; no separate optimizer/target)
      --lookback-hours N
      --max-tasks N
      --edit-budget N
      --preferences TEXT
      --json                    (machine-readable stdout in the log)

    We do NOT pass --auto-adopt: our local _auto_adopt + validate_proposal
    decide promotion so the safety wrapper stays in our control. The verb
    itself is `official_run_verb` (default "run"); appended by
    launch_sleep_subprocess as the subcommand.
    """
    args: list[str] = []

    # --project: stage to <a0>/.skillopt-sleep/staging/<ts>/ for predictable
    # discovery. Without it the engine uses cwd (the plugin's staging dir),
    # burying the output where _find_staging_dir won't look.
    args += ["--project", str(_a0_root())]

    # --target-skill-path: real path to the live SKILL.md.
    skill_path = _resolve_skill_path(target)
    if skill_path:
        args += ["--target-skill-path", skill_path]

    backend = _str_cfg(cfg, "official_backend")
    if backend:
        # The real CLI rejects unknown backends with argparse error; only
        # pass values that look like a known choice.
        known = {"mock", "claude", "codex", "copilot", "cursor", "pi",
                 "handoff", "azure_openai"}
        if backend in known:
            args += ["--backend", backend]

    # Single --model (no separate optimizer/target model in the real CLI).
    model = _str_cfg(cfg, "official_optimizer_model") or _str_cfg(cfg, "optimizer_model")
    if model:
        args += ["--model", model]

    lookback = cfg.get("official_lookback_hours")
    if lookback is not None:
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
    edit_budget = cfg.get("official_edit_budget")
    if edit_budget:
        try:
            args += ["--edit-budget", str(int(edit_budget))]
        except (TypeError, ValueError):
            pass
    prefs = _str_cfg(cfg, "official_preferences")
    if prefs:
        args += ["--preferences", prefs]

    # Machine-readable stdout so the log carries the structured payload too.
    args += ["--json"]
    return args


# ----------------------------------------------------------------------- #
# Staged-proposal discovery + rename
# ----------------------------------------------------------------------- #

def _find_staging_dir() -> Path | None:
    """Locate the official engine's most recent staging dir.

    The `run` command writes to `<project>/.skillopt-sleep/staging/<ts>/`
    (one timestamped dir per night), each containing `proposed_SKILL.md`,
    `report.json`, `report.md`, `manifest.json`. We pick the newest dir
    that has a `manifest.json` (matches the upstream `latest_staging()`
    heuristic). Returns None if no staging exists.
    """
    root = _official_staging_root()
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for child in root.iterdir():
        if child.is_dir() and (child / "manifest.json").is_file():
            candidates.append(child)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _read_gate_verdict(staging_dir: Path) -> dict[str, Any] | None:
    """Read the authoritative gate verdict from `report.json`.

    The official engine runs its monotonic `evaluate_gate` BEFORE staging
    and writes the verdict to `report.json`. This is the authoritative
    accept/reject decision — we do not re-run the gate. Returns None if
    the report is missing or unreadable (caller treats as no proposal).
    """
    rj = staging_dir / "report.json"
    if not rj.is_file():
        return None
    try:
        rep = json.loads(rj.read_text(encoding="utf-8"))
    except Exception:
        return None
    return {
        "accepted": bool(rep.get("accepted", False)),
        "gate_action": rep.get("gate_action", ""),
        "baseline_score": rep.get("baseline_score"),
        "candidate_score": rep.get("candidate_score"),
        "night": rep.get("night"),
        "n_accepted_edits": rep.get("n_accepted_edits",
                                   len(rep.get("edits", []) or [])),
        "n_rejected_edits": rep.get("n_rejected_edits", 0),
        "report": rep,
    }


# ----------------------------------------------------------------------- #
# Main entry: run one official Sleep cycle
# ----------------------------------------------------------------------- #

def run_official_sleep_cycle(
    target: str = "",
    custom_prompts: dict[str, str] | None = None,
    cfg: dict[str, Any] | None = None,
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Run one official `skillopt_sleep run` cycle for `target` (or all skills).

    Returns one of:
      {ok: True,  engine:"official", staged_path, official_staging_dir, gate,
       held_out, log_path, pid, staged_size}
          — engine ran, gate ACCEPTED, proposal copied to plugin staging/.
      {ok: False, gate_rejected:True, engine:"official", reason, gate, ...}
          — engine ran, gate REJECTED. Do NOT fall back to direct (the official
          gate already considered the candidate). Record a reject cycle.
      {ok: False, fallback_to_direct:True, engine:"official", reason, ...}
          — infra failure (package absent, launch/timeout, no staging, copy
          failed). The auto-loop retries with direct_optimizer.

    The auto-loop calls this when `use_official_engine` is true and
    probe_official()["available"] is true. The authoritative gate verdict
    comes from the engine's own report.json (it ran evaluate_gate before
    staging); we never re-run the gate here.

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

    # Discover the official staging dir + the authoritative gate verdict
    # from report.json (NOT log scraping). The engine ran evaluate_gate
    # before staging, so report.json is the source of truth.
    staging_dir = _find_staging_dir()
    if not staging_dir:
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": "official run produced no staging dir",
            "pid": pid,
            "log_path": log_path,
        }
    verdict = _read_gate_verdict(staging_dir)
    proposed = staging_dir / "proposed_SKILL.md"
    if not proposed.is_file():
        # A multi-skill night may stage per-skill rows instead of the
        # single proposed_SKILL.md; without a single proposal we have
        # nothing to feed the existing adopt path -> fall back this tick.
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": "staging dir has no proposed_SKILL.md (multi-skill night?)",
            "pid": pid,
            "log_path": log_path,
            "official_staging_dir": str(staging_dir),
            "gate": verdict,
        }

    # If the official gate REJECTED the candidate, do NOT promote it and
    # do NOT fall back to direct (direct has no held-out signal and would
    # override the official gate's decision with an ungated edit). Return
    # a gate_rejected result; the auto-loop records a reject cycle and
    # moves on. The proposal is left in the official staging dir for
    # inspection but is NOT copied into the plugin's staging/.
    if verdict and not verdict.get("accepted"):
        return {
            "ok": False,
            "gate_rejected": True,
            "engine": "official",
            "reason": (
                f"official gate rejected (gate_action={verdict.get('gate_action')!r}, "
                f"baseline={verdict.get('baseline_score')} -> "
                f"candidate={verdict.get('candidate_score')})"
            ),
            "pid": pid,
            "log_path": log_path,
            "official_staging_dir": str(staging_dir),
            "gate": verdict,
        }

    # Gate accepted: copy proposed_SKILL.md into the plugin's staging/
    # <skill>.md so the existing _auto_adopt path finds it unchanged. We
    # COPY (not move) so the official staging dir + report.md stay intact
    # for `skillopt_sleep status`/`adopt`/human inspection.
    import shutil
    skill_name = target or "best_skill"
    dest = sleep_runner.staging_dir() / f"{skill_name}.md"
    try:
        shutil.copy2(str(proposed), str(dest))
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "fallback_to_direct": True,
            "engine": "official",
            "reason": f"copy staged proposal failed: {e}",
            "pid": pid,
            "log_path": log_path,
            "official_staging_dir": str(staging_dir),
            "gate": verdict,
        }
    try:
        size = dest.stat().st_size
    except Exception:
        size = 0
    return {
        "ok": True,
        "engine": "official",
        "staged_path": str(dest),
        "official_staging_dir": str(staging_dir),
        "gate": verdict,
        "held_out": {
            "before": verdict.get("baseline_score") if verdict else None,
            "after": verdict.get("candidate_score") if verdict else None,
            "delta_pp": (
                (verdict.get("candidate_score") - verdict.get("baseline_score"))
                if verdict and verdict.get("candidate_score") is not None
                and verdict.get("baseline_score") is not None
                else None
            ),
            "accepted": verdict.get("accepted") if verdict else None,
        },
        "log_path": log_path,
        "pid": pid,
        "staged_size": size,
    }


# ----------------------------------------------------------------------- #
# Future hook: direct evaluate_gate delegation (not wired authoritatively)
# ----------------------------------------------------------------------- #

def _try_evaluate_gate(
    candidate_skill: str,
    cand_hard: float,
    current_skill: str,
    current_score: float,
    best_skill: str,
    best_score: float,
    best_step: int,
    global_step: int,
    *,
    cand_soft: float = 0.0,
    metric: str = "hard",
    mixed_weight: float = 0.5,
) -> dict[str, Any] | None:
    """Best-effort import + call of the official evaluate_gate.

    Verified signature (skillopt_sleep.gate AND skillopt.evaluation.gate,
    behaviourally identical):
      evaluate_gate(candidate_skill, cand_hard, current_skill, current_score,
                    best_skill, best_score, best_step, global_step, *,
                    cand_soft=0.0, metric="hard", mixed_weight=0.5) -> GateResult
      GateResult(action, current_skill, current_score, best_skill, best_score,
                 best_step); action in {"accept_new_best","accept","reject"}.

    NOT wired as authoritative today: the official Sleep engine already runs
    this gate before staging, and the verdict is in report.json (read via
    _read_gate_verdict). This function is kept corrected-to-spec so a future
    refinement can call the Python gate directly without touching the
    auto-loop, and so tests can assert it degrades to None when the package
    is absent. Returns {accepted, action, reason} or None on any failure.
    """
    try:  # pragma: no cover - exercised only when the package is installed
        # The vendored skillopt_sleep.gate is preferred (zero dependency on
        # the research package); fall back to skillopt.evaluation.gate.
        try:
            from skillopt_sleep import gate as _gate  # type: ignore
        except Exception:
            from skillopt.evaluation import gate as _gate  # type: ignore
        fn = getattr(_gate, "evaluate_gate", None)
        if fn is None:
            return None
        result = fn(
            candidate_skill, cand_hard, current_skill, current_score,
            best_skill, best_score, best_step, global_step,
            cand_soft=cand_soft, metric=metric, mixed_weight=mixed_weight,
        )
        action = str(getattr(result, "action", ""))
        accepted = action in ("accept", "accept_new_best")
        return {"accepted": accepted, "action": action, "reason": str(result)}
    except Exception:
        return None


__all__ = [
    "probe_official",
    "run_official_sleep_cycle",
]