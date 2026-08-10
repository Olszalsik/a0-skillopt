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

2. **real** (STUB, guarded behind `replay_real_executor_enabled`,
   default false): `_real_score(task, skill_md)` would build an Agent Zero
   `AgentContext` + `Agent`, inject the skill, run `context.communicate()`,
   and score `agent.loop_data.last_response` via `reward_model.score_rollout`.
   That is the real counterfactual. It is NOT implemented here — the
   deliverable for C2 is the structure + guard wiring + recursion guard,
   not live verification. When enabled but unimplemented it raises, and
   `run_counterfactual` returns `real_executor_unavailable`.

Recursion guard: the real executor sets `SKILLOPT_REPLAY_MODE=1` BEFORE
creating the replay agent and unsets it in `finally`. The harvester
(C1) and the agent_init auto-loop starter (C2) both check this env var
and short-circuit, so the replay agent's own `monologue_end` /
`agent_init` do not pollute the training set with synthetic rollouts or
spawn a nested optimizer loop. The mock executor never touches a real
agent, so it does not need the guard.

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


def _real_score(task: dict[str, Any], skill_md: str, config: dict[str, Any]) -> float:
    """STUB: real A0-agent-loop counterfactual replay executor.

    The real counterfactual: build an Agent Zero `AgentContext` + `Agent`,
    inject this skill so `skill_instruction_name` matches (via
    `skills.add_loaded_skill_name` + `agent.hist_add_tool_result(...,
    additional={"skill_instructions": {"name": ..., "content_included":
    True, ...}})`), run `context.communicate(UserMessage(message=task[
    "task"]))`, then score `agent.loop_data.last_response` through
    `reward_model.score_rollout`. The score is `1.0` for a success
    outcome, `0.5` partial, `0.0` failure.

    DELIVERABLE for C2 = the structure + guard wiring + recursion guard,
    NOT live verification. This raises `NotImplementedError` so that
    `run_counterfactual` (which wraps the real path in try/except) returns
    `real_executor_unavailable` instead of silently faking a score. When
    the live follow-up lands, replace the body below.

    Recursion guard: `SKILLOPT_REPLAY_MODE` is set BEFORE the replay agent
    could be created and unset in `finally`, so the replay agent's own
    `monologue_end` (harvester) and `agent_init` (auto-loop starter) both
    short-circuit. The mock executor never creates a real agent, so it
    does not need this guard.
    """
    was = os.environ.get("SKILLOPT_REPLAY_MODE")
    os.environ["SKILLOPT_REPLAY_MODE"] = "1"
    try:
        # Live follow-up: implement the real A0-agent-loop replay here.
        # Until then, raising keeps the gate honest (no fake scores).
        raise NotImplementedError(
            "real replay executor is a stub (Solution C live follow-up)"
        )
    finally:
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
            for t in held_out_tasks:
                sc = _real_score(t, current_skill_md, cfg)
                sp = _real_score(t, proposed_skill_md, cfg)
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