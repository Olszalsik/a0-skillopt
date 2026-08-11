"""
SkillOpt - local counterfactual replay harness (Solution C, Phase C2).

The authoritative counterfactual gate for the LOCAL / direct-optimizer path.
Until C2 the only "replay" the plugin had was the synthetic A/B harness
(helpers/ab_harness.py), which feeds [task+resp+skill[:512]] to the
(untrained) reward model and is NOT a real counterfactual — it cannot
re-run a task under a different skill and measure the difference. The
official `skillopt_sleep` engine runs its own monotonic held-out gate
before staging, so when `official_gated=True` the local replay gate is
SKIPPED (the upstream verdict is authoritative). This module fills the
gap for the direct/fallback path: it re-scores the SAME held-out tasks
under the CURRENT skill and under the PROPOSED skill, and accepts the
proposal only when the proposed skill strictly beats the current one
by at least `gate_min_improvement_pp` percentage points.

Two executors:

1. **mock** (default, deterministic, no LLM): `_mock_score(task, skill_md)`
   is a deterministic RELEVANCE heuristic, not a real counterfactual. It
   takes the task's stored outcome as a base score and modulates it by how
   many of the skill's directive keywords (markdown headings + bold lines)
   appear in the task text. The SAME task scores differently under current
   vs proposed skill because the keyword overlap differs — that IS a
   counterfactual signal (the skill content changes the score), but it is
   a cheap proxy, NOT a real agent replay. It rewards keyword relevance,
   which is a known limitation; document it clearly. Good enough to make
   the gate non-trivial and deterministic for tests; the real executor
   below is the live follow-up.

2. **real** (v1.8.0, guarded behind `replay_real_executor_enabled`,
   default false): `_real_score(task, skill_md, config, skill_name)` shells
   out to `scripts/replay_worker.py`, which runs ONE held-out task through a
   real Agent Zero monologue under the given skill in a CHILD PROCESS (temp
   working directory, own event loop, `SKILLOPT_REPLAY_MODE=1`) and writes a
   `{score, outcome, ...}` JSON envelope. The parent blocks on the
   subprocess, parses the envelope, and returns the float score. The worker
   injects the skill via `skills.add_loaded_skill_name(agent, name)` +
   `agent.hist_add_tool_result("skills_tool", skill_md,
   skill_instructions={...})` — note `skill_instructions` is a TOP-LEVEL
   kwarg of `hist_add_tool_result`, NOT nested under `additional` (that
   unwrap happens inside `Tool.after_execution`, which the worker bypasses).
   The subprocess design gives side-effect containment (temp cwd), a clean
   async boundary (no "event loop already running" when called from the
   auto-loop thread or an async WebUI handler), and recursion isolation.
   Cost is bounded by `replay_real_max_tasks` (default 4) since the real
   executor runs 2xN full monologues per gate call. When the worker fails or
   times out, `_real_score` raises and `run_counterfactual` returns
   `real_executor_unavailable:...` (loud-not-crash, falls through to the
   structural gate).

Recursion guard: the parent (`_real_score`) sets `SKILLOPT_REPLAY_MODE=1`
before spawning the worker and unsets it in `finally`; the worker also sets
it before creating the replay `AgentContext` (agent_init fires synchronously
inside `Agent.__init__`). The harvester (C1) and the agent_init auto-loop
starter (C2) both check this env var and short-circuit, so the replay
agent's own `monologue_end` / `agent_init` do not pollute the training set
with synthetic rollouts or spawn a nested optimizer loop. The mock executor
never touches a real agent, so it does not need the guard.

Public surface:
- run_counterfactual(skill_name, current_skill_md, proposed_skill_md,
                     held_out_tasks, *, executor="mock", config=None) -> dict
- _mock_score(task, skill_md) -> float            (testable directly)
- _directive_keywords(skill_md) -> list[str]       (testable directly)
- _decide(hard_current, hard_proposed, n, config) -> dict
"""

from __future__ import annotations

import os
import re
import logging
from typing import Any

log = logging.getLogger(__name__)

# Outcome -> base score. Matches the 3-class reward-model / heuristic
# taxonomy used everywhere else in the plugin.
_OUTCOME_BASE = {"success": 1.0, "partial": 0.5, "failure": 0.0}

# Common markdown / stopword tokens to drop from the directive-keyword set
# so the relevance overlap is not dominated by "the skill", "step", etc.
_STOPWORDS = {
    "skill", "the", "and", "for", "with", "you", "your", "this", "that",
    "use", "using", "step", "steps", "example", "examples", "note",
    "notes", "always", "never", "if", "then", "when", "a", "an", "to",
    "of", "in", "on", "or", "is", "are", "be", "do", "not", "no", "yes",
    "please", "must", "should", "can", "will", "it", "as", "by", "at",
    "from", "into", "out", "up", "down", "over", "all", "any", "each",
}

_BOLD_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _directive_keywords(skill_md: str) -> list[str]:
    """Extract the directive keywords from a SKILL.md: the text of markdown
    headings (``# ...``) and bold spans (``**...**``). These are the lines
    that tell the agent WHAT to do, so their overlap with a task's text is
    a cheap proxy for whether the skill is relevant to the task.

    Lowercased, deduped, stopwords dropped, length >= 3. Returns [] for an
    empty / malformed skill (the mock scorer then treats overlap as
    neutral — 0.5 — so an empty skill does not get a free zero).
    """
    if not skill_md:
        return []
    tokens: list[str] = []
    for line in skill_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # heading text minus the leading #'s
            tokens.append(stripped.lstrip("#").strip())
        # bold spans anywhere on the line
        for m in _BOLD_RE.finditer(line):
            tokens.append(m.group(1).strip())
    out: list[str] = []
    seen: set[str] = set()
    for tok in tokens:
        for w in _WORD_RE.findall(tok.lower()):
            if w in _STOPWORDS or w in seen:
                continue
            seen.add(w)
            out.append(w)
    return out


def _mock_score(task: dict[str, Any], skill_md: str) -> float:
    """Deterministic relevance heuristic in [0, 1]. No LLM, no network.

    base  = the task's stored outcome (success=1.0 / partial=0.5 /
            failure=0.0); defaults to 0.5 when the rollout has no outcome.
    overlap = fraction of the skill's directive keywords that appear in
            the task's task+last_response text (0..1). When the skill has
            no directive keywords, overlap is 0.5 (neutral) so the score
            is just the base — a no-op skill neither helps nor hurts.
    score = base * (0.5 + 0.5 * overlap)

    So a fully-relevant skill multiplies the base by up to 1.0; a
    fully-irrelevant skill multiplies it by 0.5; an empty skill leaves it
    at base. The SAME task under a more-relevant proposed skill scores
    higher than under the current skill — that difference is the
    counterfactual lift the gate decides on.

    Known limitation: this rewards keyword relevance, not actual agent
    behaviour. A skill that simply lists the task's keywords would score
    high. This is the documented trade-off of the mock executor; the real
    executor (stub) is the live follow-up.
    """
    outcome = task.get("outcome") if isinstance(task, dict) else None
    base = _OUTCOME_BASE.get(outcome, 0.5)  # unknown outcome -> neutral
    keywords = _directive_keywords(skill_md)
    if not keywords:
        overlap = 0.5
    else:
        hay = (
            str(task.get("task", "")) + "\n" + str(task.get("last_response", ""))
        ).lower()
        if not hay.strip():
            overlap = 0.0
        else:
            hits = sum(1 for k in keywords if k in hay)
            overlap = min(1.0, hits / len(keywords))
    return base * (0.5 + 0.5 * overlap)


def _real_score(
    task: dict[str, Any],
    skill_md: str,
    config: dict[str, Any],
    skill_name: str = "replay",
) -> float:
    """Real A0-agent-loop counterfactual replay executor (v1.8.0).

    Shells out to ``scripts/replay_worker.py``, which runs ONE held-out task
    through a real Agent Zero monologue under ``skill_md`` in a child process
    (temp working dir, own event loop, ``SKILLOPT_REPLAY_MODE=1``) and writes
    a ``{score, outcome, ...}`` JSON envelope. We block on the subprocess,
    parse the envelope, and return the float score (success=1.0 / partial=
    0.5 / failure=0.0).

    See the module docstring + ``scripts/replay_worker.py`` for the
    skill-injection recipe (note: ``hist_add_tool_result`` takes
    ``skill_instructions`` as a TOP-LEVEL kwarg, not under ``additional``).

    Never returns a fake score on failure: any error (timeout, missing
    envelope, worker error) raises, and ``run_counterfactual``'s try/except
    turns it into ``real_executor_unavailable:<ExcType>:<msg>`` so the gate
    falls through to the structural stages (loud-not-crash).

    ``skill_name`` is used only for the skill-instructions metadata so
    ``skill_instruction_name`` matches in the replay agent's history; it
    defaults to ``"replay"`` and is threaded from ``run_counterfactual``.
    """
    import json as _json
    import subprocess
    import tempfile
    from pathlib import Path

    cfg = config or {}
    was = os.environ.get("SKILLOPT_REPLAY_MODE")
    os.environ["SKILLOPT_REPLAY_MODE"] = "1"
    sf = tf = of = None
    try:
        from helpers import sleep_runner  # type: ignore  # noqa: E402

        py = sleep_runner._a0_python()
        worker = sleep_runner.plugin_root() / "scripts" / "replay_worker.py"
        if not worker.is_file():
            raise RuntimeError(f"replay worker not found: {worker}")
        timeout = float(cfg.get("replay_real_per_task_timeout_s", 180) or 180)

        fd, sf = tempfile.mkstemp(suffix=".md", prefix="skillopt_replay_skill_")
        os.write(fd, (skill_md or "").encode("utf-8"))
        os.close(fd)
        fd, tf = tempfile.mkstemp(suffix=".json", prefix="skillopt_replay_task_")
        os.write(fd, _json.dumps(task, ensure_ascii=False).encode("utf-8"))
        os.close(fd)
        fd, of = tempfile.mkstemp(suffix=".json", prefix="skillopt_replay_out_")
        os.close(fd)

        env = sleep_runner.build_subprocess_env()
        env["SKILLOPT_REPLAY_MODE"] = "1"
        cmd = [
            py, str(worker),
            "--skill-name", str(skill_name),
            "--skill-md", sf,
            "--task", tf,
            "--out", of,
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"replay worker timed out after {timeout}s") from e
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise RuntimeError(f"replay worker exit {proc.returncode}: {tail}")
        try:
            result = _json.loads(Path(of).read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"replay worker wrote no parseable output: {e}") from e
        if result.get("score") is None:
            raise RuntimeError(
                f"replay worker error: {result.get('error', 'unknown')}"
            )
        return float(result["score"])
    finally:
        for p in (sf, tf, of):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        if was is None:
            os.environ.pop("SKILLOPT_REPLAY_MODE", None)
        else:
            os.environ["SKILLOPT_REPLAY_MODE"] = was


def _decide(
    hard_current: float,
    hard_proposed: float,
    n: int,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Strict-monotonic gate verdict from the two hard scores.

    accepted = proposed STRICTLY beats current AND the lift in percentage
    points >= gate_min_improvement_pp (reusing the existing gate config
    key so the local replay gate and the held-out stage agree on the bar).

    Reasons:
      ok_lift_+Xpp                          -> accepted=True
      rejected_no_lift                      -> proposed == current
      rejected_regression                   -> proposed < current
      rejected_insufficient_lift:X<Y        -> lift positive but below bar
      rejected_insufficient_n:N<M           -> too few held-out tasks
    """
    cfg = config or {}
    min_pp = float(cfg.get("gate_min_improvement_pp", 5.0) or 0.0)
    min_n = int(cfg.get("replay_min_n", 3) or 0)
    if n < min_n:
        return {"accepted": False, "reason": f"rejected_insufficient_n:{n}<{min_n}"}
    if hard_proposed < hard_current:
        return {"accepted": False, "reason": "rejected_regression"}
    if hard_proposed == hard_current:
        return {"accepted": False, "reason": "rejected_no_lift"}
    lift_pp = (hard_proposed - hard_current) * 100.0
    if lift_pp < min_pp:
        return {
            "accepted": False,
            "reason": f"rejected_insufficient_lift:{lift_pp:.2f}<{min_pp}",
        }
    return {"accepted": True, "reason": f"ok_lift_+{lift_pp:.2f}pp"}


def run_counterfactual(
    skill_name: str,
    current_skill_md: str,
    proposed_skill_md: str,
    held_out_tasks: list[dict[str, Any]],
    *,
    executor: str = "mock",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a paired counterfactual over `held_out_tasks` and return a verdict.

    Returns a dict with:
      ok          : bool — True iff a verdict was produced (enough tasks +
                  executor available). False means "could not run"; the
                  caller falls through to the structural gate.
      executor    : "mock" | "real"
      n           : number of held-out tasks scored
      hard_current, hard_proposed : mean score in [0,1] under each skill
      lift_pp     : (hard_proposed - hard_current) * 100
      accepted    : bool — the gate verdict (only meaningful when ok=True)
      reason      : the gate's reason string (or the not-run reason)
      per_task    : list of {id, current, proposed} (only when ok=True)

    ok=False reasons:
      no_held_out_tasks            — held_out_tasks is empty
      insufficient_n:N<M           — fewer than replay_min_n tasks
      real_executor_not_enabled    — executor="real" but the flag is off
      real_executor_unavailable:.. — real executor raised (stub / framework)
      unknown_executor:NAME        — executor not mock|real
    """
    cfg = config or {}
    n = len(held_out_tasks or [])
    if n == 0:
        return {"ok": False, "reason": "no_held_out_tasks", "executor": executor, "n": 0}
    min_n = int(cfg.get("replay_min_n", 3) or 0)
    if n < min_n:
        return {
            "ok": False,
            "reason": f"insufficient_n:{n}<{min_n}",
            "executor": executor,
            "n": n,
        }

    # Score each task under both skills.
    per_task: list[dict[str, Any]] = []
    try:
        if executor == "mock":
            for t in held_out_tasks:
                sc = _mock_score(t, current_skill_md)
                sp = _mock_score(t, proposed_skill_md)
                per_task.append({
                    "id": (t.get("id") if isinstance(t, dict) else None),
                    "current": round(sc, 4),
                    "proposed": round(sp, 4),
                })
        elif executor == "real":
            if not bool(cfg.get("replay_real_executor_enabled", False)):
                return {
                    "ok": False,
                    "reason": "real_executor_not_enabled",
                    "executor": "real",
                    "n": n,
                }
            # Cost bound: the real executor spawns 2xN full A0 monologues
            # (current + proposed x N), so cap N at replay_real_max_tasks
            # (default 4). The min_n check above already passed against the
            # original n; we recompute n to the capped count so the means
            # and _decide below use the actual scored count.
            max_tasks = int(cfg.get("replay_real_max_tasks", 4) or 0)
            if max_tasks and len(held_out_tasks) > max_tasks:
                held_out_tasks = held_out_tasks[:max_tasks]
                n = len(held_out_tasks)
            for t in held_out_tasks:
                sc = _real_score(t, current_skill_md, cfg, skill_name)
                sp = _real_score(t, proposed_skill_md, cfg, skill_name)
                per_task.append({
                    "id": (t.get("id") if isinstance(t, dict) else None),
                    "current": round(sc, 4),
                    "proposed": round(sp, 4),
                })
        else:
            return {
                "ok": False,
                "reason": f"unknown_executor:{executor}",
                "executor": executor,
                "n": n,
            }
    except Exception as e:
        # Real-executor stub raises; framework imports may also fail.
        # Never crash the gate — return not-run so the structural gate runs.
        log.debug("[skillopt] replay executor '%s' failed: %s", executor, e)
        return {
            "ok": False,
            "reason": f"real_executor_unavailable:{type(e).__name__}:{e}",
            "executor": executor,
            "n": n,
        }

    hard_current = sum(p["current"] for p in per_task) / n
    hard_proposed = sum(p["proposed"] for p in per_task) / n
    lift_pp = round((hard_proposed - hard_current) * 100.0, 2)
    verdict = _decide(hard_current, hard_proposed, n, cfg)
    return {
        "ok": True,
        "executor": executor,
        "n": n,
        "hard_current": round(hard_current, 4),
        "hard_proposed": round(hard_proposed, 4),
        "lift_pp": lift_pp,
        "accepted": verdict["accepted"],
        "reason": verdict["reason"],
        "per_task": per_task,
    }