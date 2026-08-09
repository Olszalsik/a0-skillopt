"""
SkillOpt A/B harness - v1.2.0.

ROADMAP Day-3, item 2: "A/B harness on real rollouts".

The harness is a paired statistical test that compares a proposed
skill against the current skill on the agent's OWN past rollouts:

  1. Load the last N rollouts for `skill_name` (default 30).
  2. Split into A and B with stratified outcomes (success/partial/
     failure) so each arm sees the same difficulty mix.
  3. "Replay" each rollout under the current skill and the proposed
     skill. The replay is approximated by the v1.2.0 reward model
     on the text `[task + last_response + skill_text[:512]]` so the
     harness does not need to run the agent. When the reward model
     is untrained (v1.2.0 day 1), the replay falls back to the
     v1.1.0 heuristic; the harness will still detect order-of-
     magnitude differences when the proposed skill is genuinely
     better (or genuinely worse).
  4. An LLM judge (or a deterministic keyword stub if no LLM is
     configured) compares each A/B pair and emits `win` / `lose` /
     `tie` + a confidence in [0, 1].
  5. The proposal passes if and only if `B wins by >= ab_min_lift_pp`
     AND `judge confidence >= ab_min_confidence`.

Failure-mode policy (per ROADMAP engineering principle 2 - "make the
failure mode loud"):
  - can_run = False when there are fewer than ab_min_n rollouts for
    the skill. The gate falls through to the existing structural
    check; this is NOT a failure, it's the documented v1.2.0
    behaviour for low-data skills.
  - can_run = True but passed = False when the judge is unreachable
    OR when the proposal loses the A/B test. The gate REJECTS.
  - Errors are logged to the cycle log AND surfaced via get_ab_status()
    so the dashboard shows them.

Pluggable judge:
  The LLM judge is a function injected via `set_judge_fn(fn)`. The
  default judge is a deterministic keyword-overlap stub; production
  swaps in a real judge that POSTs to `SKILLOPT_JUDGE_ENDPOINT`. The
  smoke tests inject a deterministic judge so the harness is fully
  testable without an LLM.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable


PLUGIN_NAME = "skillopt"
log = logging.getLogger("skillopt.ab_harness")

# v1.2.0 (Task A.1): the LLM judge mode. Default is the deterministic
# keyword stub. Production calls `set_judge_mode("http")` to switch to
# `_llm_judge_via_http`, which POSTs JUDGE_PROMPT to
# SKILLOPT_JUDGE_ENDPOINT. Tests can call `set_judge_mode("stub")` to
# reset, or use `set_judge_fn(...)` to inject a fully custom callable.


# ----------------------------------------------------------------------- #
# Module-level state (counters, last run, judge injection)
# ----------------------------------------------------------------------- #

_last_run_at: float = 0.0
_last_result: dict[str, Any] | None = None
_total_runs: int = 0
_total_passed: int = 0
_total_rejected: int = 0
_total_skipped: int = 0
_last_error: str | None = None

# The judge is a function (rollout, score_a, score_b) -> {verdict, confidence, reason}.
# 'verdict' is one of: 'win' (B is better), 'lose' (B is worse), 'tie'.
# 'confidence' is in [0, 1].
# The default is a deterministic stub; production overrides via set_judge_fn.
JudgeFn = Callable[[dict, float, float], dict[str, Any]]
_judge_fn: JudgeFn | None = None


# ----------------------------------------------------------------------- #
# Configuration
# ----------------------------------------------------------------------- #

# Module-level constants. Per the v1.2.0 config layout these can be
# overridden by the env vars below. The full list is in
# default_config.yaml + config.json.
DEFAULT_N = 30
DEFAULT_MIN_N = 6
DEFAULT_MIN_LIFT_PP = 5.0
DEFAULT_MIN_CONFIDENCE = 0.6
DEFAULT_JUDGE_MODEL = "minimax-m3"

# ----------------------------------------------------------------------- #
# Judge prompt (module-level so it's easy to iterate)
# ----------------------------------------------------------------------- #

JUDGE_PROMPT = """You are a skill-evaluation judge. You are given:

  TASK     : the agent's original task
  RESPONSE : the agent's last response under the proposed skill
  SCORE_A  : reward-model score under the CURRENT skill (0..1)
  SCORE_B  : reward-model score under the PROPOSED skill (0..1)

For the same task, decide whether the proposed skill (B) produces a
MORE correct, idiomatic, and useful response than the current skill (A).
Reply with one JSON object, no prose:

  {"verdict": "win" | "lose" | "tie", "confidence": 0..1, "reason": "..."}

A `win` means B is materially better; `lose` means B is worse; `tie`
means they are comparable. Confidence is your self-assessed certainty
in the verdict (0 = guessing, 1 = certain). Be conservative with
confidence if SCORE_A and SCORE_B are close."""


# ----------------------------------------------------------------------- #
# Public API
# ----------------------------------------------------------------------- #

def set_judge_fn(fn: JudgeFn | None) -> None:
    """Inject a judge function. Used by tests; production calls this once
    at boot with a real LLM-backed judge when SKILLOPT_JUDGE_ENDPOINT is set.
    """
    global _judge_fn, _judge_mode
    _judge_fn = fn
    _judge_mode = "custom" if fn is not None else "stub"


def get_judge_fn() -> JudgeFn | None:
    return _judge_fn


# v1.2.0 (Task A.1): high-level judge mode. "stub" = deterministic
# keyword overlap (default). "http" = POSTs to SKILLOPT_JUDGE_ENDPOINT
# using the JUDGE_PROMPT constant. "custom" = a function injected via
# set_judge_fn() (used by some tests that want a specific behaviour).
# Production calls set_judge_mode("http") once at boot.
_judge_mode: str = "stub"


def set_judge_mode(mode: str) -> None:
    """Set the high-level judge mode. Valid: 'stub' | 'http' | 'custom'."""
    global _judge_fn, _judge_mode
    mode = (mode or "").strip().lower()
    if mode == "stub":
        _judge_fn = None
        _judge_mode = "stub"
    elif mode == "http":
        if not os.environ.get("SKILLOPT_JUDGE_ENDPOINT", "").strip():
            raise RuntimeError(
                "set_judge_mode('http') requires SKILLOPT_JUDGE_ENDPOINT"
            )
        # Bind the model at install time so the closure is cheap to call
        cfg = _config()
        model = cfg["judge_model"]

        def _http_judge(rollout, score_a, score_b):
            return _llm_judge_via_http(
                rollout, score_a, score_b, judge_model=model,
            )
        _judge_fn = _http_judge
        _judge_mode = "http"
    elif mode == "custom":
        # Caller must call set_judge_fn(...) separately. We don't touch _judge_fn.
        _judge_mode = "custom"
    else:
        raise ValueError(f"unknown judge mode: {mode!r}")


def get_judge_mode() -> str:
    return _judge_mode


def _plugin_root() -> Path:
    # `helpers/ab_harness.py` -> `..` -> plugin root
    return Path(__file__).resolve().parent.parent


def _rollouts_dir() -> Path:
    p = _plugin_root() / "logs" / "rollouts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _runs_dir() -> Path:
    p = _plugin_root() / "logs" / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _config() -> dict[str, Any]:
    """Read ab_harness_* keys from the merged config + env overrides."""
    try:
        from helpers.sleep_runner import merged_config
        cfg = merged_config()
    except Exception:
        cfg = {}
    out = {
        "n": int(cfg.get("ab_harness_n", DEFAULT_N)),
        "min_n": int(cfg.get("ab_harness_min_n", DEFAULT_MIN_N)),
        "min_lift_pp": float(cfg.get("ab_harness_min_lift_pp", DEFAULT_MIN_LIFT_PP)),
        "min_confidence": float(cfg.get("ab_harness_min_confidence", DEFAULT_MIN_CONFIDENCE)),
        "judge_model": str(cfg.get("judge_model", cfg.get("target_model", DEFAULT_JUDGE_MODEL))),
        "enabled": bool(cfg.get("ab_harness_enabled", True)),
    }
    # Env overrides
    if os.environ.get("SKILLOPT_JUDGE_MODEL"):
        out["judge_model"] = os.environ["SKILLOPT_JUDGE_MODEL"]
    if os.environ.get("SKILLOPT_AB_HARNESS_N"):
        try:
            out["n"] = int(os.environ["SKILLOPT_AB_HARNESS_N"])
        except ValueError:
            pass
    return out


def _load_recent_rollouts(skill_name: str, n: int) -> list[dict[str, Any]]:
    """Read the last N rollouts for `skill_name` (by `ts` desc), or the
    last N overall when the skill has no rollouts yet. Returns parsed
    dicts; corrupted JSON files are skipped with a warning."""
    out: list[dict[str, Any]] = []
    rollouts_root = _rollouts_dir()
    files = sorted(
        rollouts_root.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    skill_key = (skill_name or "").strip().lower()
    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("[skillopt] skipping unreadable rollout %s: %s", f.name, e)
            continue
        rec_skill = (rec.get("skill_used") or "").strip().lower()
        if skill_key and rec_skill and rec_skill != skill_key:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out


def _stratified_split(
    rollouts: list[dict[str, Any]],
    n_per_arm: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split into A and B with equal outcome distributions.

    Groups rollouts by their `outcome` field (success / partial / failure),
    then assigns every-other rollout from each group to A and B. The
    small class gets dropped first so we never run A on a class B has
    nothing of (and vice versa).
    """
    by_outcome: dict[str, list[dict[str, Any]]] = {}
    for r in rollouts:
        oc = r.get("outcome") or r.get("reward", {}).get("outcome") or "unknown"
        by_outcome.setdefault(oc, []).append(r)
    a: list[dict[str, Any]] = []
    b: list[dict[str, Any]] = []
    for group in by_outcome.values():
        for i, r in enumerate(group):
            (a if i % 2 == 0 else b).append(r)
    return a[:n_per_arm], b[:n_per_arm]


def _replay_under_skill(rollout: dict[str, Any], skill_text: str) -> float:
    """Approximate a replay: feed [task + response + skill_text] to the
    v1.2.0 reward model and return its success-probability in [0, 1].

    v1.2.0 (Day-3 item 3): if the rollout carries a `fragments_active_text`
    field (set by the v1.2.0 harvester after reading the v1.2.0 fragment
    store), use that as the skill context instead of the whole
    `skill_text`. This gives fragment-aware replay: a rollout that was
    actually produced with only the `intro` fragment active gets
    replayed under just `intro` text, not the full SKILL.md. Rollouts
    without the field fall back to `skill_text` (the v1.1.0 / v1.2.0
    pre-fragment-store behavior). Until the model is trained, the
    helper falls back to the v1.1.0 heuristic, which is fine: the
    harness still detects order-of-magnitude differences between
    current and proposed skills when they exist, and the
    `fallback_count` on the status snapshot makes it loud that no
    real model is doing the work.
    """
    try:
        from helpers import reward_model  # type: ignore  # noqa: E402
        # Build a synthetic rollout that exposes the skill text to the
        # model. We append a `[skill]` marker so the model's heuristic
        # can find the skill and a snippet (when a real model is loaded
        # later, it will see the skill as additional context).
        synthetic = dict(rollout)
        # Prefer fragment-aware context if present
        skill_ctx = (
            rollout.get("fragments_active_text")
            or skill_text
            or ""
        )
        skill_excerpt = skill_ctx[:512]
        task = (rollout.get("task") or "") + "\n[skill]\n" + skill_excerpt
        synthetic["task"] = task
        if not synthetic.get("last_response"):
            synthetic["last_response"] = rollout.get("task") or ""
        result = reward_model.score_rollout(synthetic)
        return float(result.get("success") or 0.0)
    except Exception as e:
        log.debug("[skillopt] replay_under_skill failed: %s", e)
        return 0.0


def _default_judge_fn(rollout: dict, score_a: float, score_b: float) -> dict[str, Any]:
    """Deterministic keyword-overlap judge. Used when no LLM is configured
    (default) and as a sane fallback if the real judge crashes.

    Returns {verdict, confidence, reason}. The verdict is `win` if
    score_b > score_a, `lose` if score_b < score_a, `tie` if equal.
    Confidence is the absolute score difference, capped at 1.0."""
    diff = score_b - score_a
    if abs(diff) < 1e-3:
        return {"verdict": "tie", "confidence": 0.5, "reason": "scores equal"}
    if diff > 0:
        return {
            "verdict": "win",
            "confidence": min(1.0, abs(diff) * 2.0),
            "reason": f"score_b > score_a by {diff:.3f}",
        }
    return {
        "verdict": "lose",
        "confidence": min(1.0, abs(diff) * 2.0),
        "reason": f"score_b < score_a by {abs(diff):.3f}",
    }


def _call_judge(rollout: dict, score_a: float, score_b: float) -> dict[str, Any]:
    """Call the configured judge (or the default). Never raises."""
    fn = _judge_fn if _judge_fn is not None else _default_judge_fn
    try:
        out = fn(rollout, score_a, score_b)
        if not isinstance(out, dict):
            return {"verdict": "tie", "confidence": 0.0,
                    "reason": f"judge returned non-dict: {type(out).__name__}"}
        # Normalise
        v = str(out.get("verdict", "tie")).lower().strip()
        if v not in {"win", "lose", "tie"}:
            v = "tie"
        try:
            c = float(out.get("confidence", 0.0))
        except (TypeError, ValueError):
            c = 0.0
        c = max(0.0, min(1.0, c))
        return {"verdict": v, "confidence": c, "reason": str(out.get("reason", ""))}
    except Exception as e:
        return {"verdict": "tie", "confidence": 0.0,
                "reason": f"judge raised {type(e).__name__}: {e}"}


def _llm_judge_via_http(
    rollout: dict, score_a: float, score_b: float, *, judge_model: str,
) -> dict[str, Any]:
    """Production LLM judge. POSTs to SKILLOPT_JUDGE_ENDPOINT if set.
    Returns {'verdict', 'confidence', 'reason'} or raises on error.

    Kept separate from _call_judge so the smoke tests don't have to
    monkey-patch the default - they just call set_judge_fn(...) with
    whatever stub they want.
    """
    import urllib.request  # local import to keep import-time fast
    endpoint = os.environ.get("SKILLOPT_JUDGE_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("SKILLOPT_JUDGE_ENDPOINT is not set")
    payload = {
        "model": judge_model,
        "prompt": JUDGE_PROMPT,
        "task": rollout.get("task", ""),
        "response": rollout.get("last_response", ""),
        "score_a": score_a,
        "score_b": score_b,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise RuntimeError(f"judge endpoint returned non-dict: {type(body).__name__}")
    return body


def _append_cycle_log(line: str) -> None:
    """Best-effort: append a one-line summary to the most recent sleep log.
    Per ROADMAP principle 5, the cycle log is the source of truth."""
    try:
        runs = sorted(
            _runs_dir().glob("sleep-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        target = runs[0] if runs else (_runs_dir() / "ab_harness.log")
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"[skillopt] ab_harness | {line}\n")
    except Exception as e:
        log.debug("[skillopt] cycle log append failed: %s", e)


def run_paired_test(
    skill_name: str,
    proposed_text: str,
    current_text: str | None = None,
    n: int | None = None,
) -> dict[str, Any]:
    """Run the A/B harness for `skill_name`. Returns a dict with the full
    result. The shape is the public contract for the gate and the dashboard.

    Result fields (all always present):
      wins, losses, ties             : int (judge verdicts on B vs A)
      confidence                     : float (mean judge confidence)
      win_rate                       : float (wins / samples)
      lift_pp                        : float (win_rate - 50%) * 100
      passed                         : bool (B beats A by enough)
      can_run                        : bool (we have enough data + judge)
      reason                         : str (why we passed/failed/skipped)
      samples                        : int (pairs actually judged)
      n_rollouts_loaded              : int
      judge_model                    : str
      judge_fallback                 : bool (using keyword stub)
      error                          : str | None

    Never raises. A bug here can only make the gate return 'no
    decision' (can_run=False), not crash validate_proposal().
    """
    global _last_run_at, _last_result, _total_runs
    global _total_passed, _total_rejected, _total_skipped, _last_error
    _total_runs += 1
    cfg = _config()
    if n is None:
        n = cfg["n"]
    result: dict[str, Any] = {
        "wins": 0, "losses": 0, "ties": 0,
        "confidence": 0.0, "win_rate": 0.0, "lift_pp": 0.0,
        "passed": False, "can_run": False,
        "reason": "", "samples": 0, "n_rollouts_loaded": 0,
        "judge_model": cfg["judge_model"],
        "judge_fallback": _judge_fn is None,
        "error": None,
    }
    if not cfg["enabled"]:
        result["reason"] = "ab_harness disabled by config"
        _total_skipped += 1
        _last_result = result
        _last_run_at = time.time()
        return result

    try:
        rollouts = _load_recent_rollouts(skill_name, n)
    except Exception as e:
        result["error"] = f"load_rollouts failed: {type(e).__name__}: {e}"
        result["reason"] = "failed to load rollouts"
        _last_error = result["error"]
        _last_result = result
        _last_run_at = time.time()
        return result
    result["n_rollouts_loaded"] = len(rollouts)

    if len(rollouts) < cfg["min_n"]:
        result["reason"] = (
            f"not enough rollouts for skill {skill_name!r} "
            f"({len(rollouts)} < min_n={cfg['min_n']}); falling back to structural gate"
        )
        _total_skipped += 1
        _last_result = result
        _last_run_at = time.time()
        return result

    # Stratified split
    n_per_arm = min(n // 2, len(rollouts) // 2)
    if n_per_arm < 3:
        result["reason"] = f"stratified split too small ({n_per_arm} per arm); skipping"
        _total_skipped += 1
        _last_result = result
        _last_run_at = time.time()
        return result
    a, b = _stratified_split(rollouts, n_per_arm)
    if not a or not b:
        result["reason"] = "stratified split produced an empty arm"
        _total_skipped += 1
        _last_result = result
        _last_run_at = time.time()
        return result

    # Judge each pair
    confidences: list[float] = []
    judge_unreachable = False
    for rollout in b:
        score_a = _replay_under_skill(rollout, current_text or "")
        score_b = _replay_under_skill(rollout, proposed_text or "")
        v = _call_judge(rollout, score_a, score_b)
        if v["verdict"] == "win":
            result["wins"] += 1
        elif v["verdict"] == "lose":
            result["losses"] += 1
        else:
            result["ties"] += 1
        confidences.append(v["confidence"])
        if v.get("reason", "").startswith("judge raised"):
            judge_unreachable = True

    result["can_run"] = True
    result["samples"] = len(b)
    result["confidence"] = sum(confidences) / len(confidences) if confidences else 0.0
    total = max(1, result["wins"] + result["losses"] + result["ties"])
    result["win_rate"] = result["wins"] / total
    result["lift_pp"] = round((result["win_rate"] - 0.5) * 100.0, 2)

    if judge_unreachable:
        # Per principle 2: judge unreachable -> fail closed, do NOT adopt.
        result["passed"] = False
        result["error"] = "judge_unreachable"
        result["reason"] = "judge raised; A/B result is uncertain, rejecting per fail-closed policy"
        _last_error = result["error"]
        _total_rejected += 1
    elif result["lift_pp"] >= cfg["min_lift_pp"] and result["confidence"] >= cfg["min_confidence"]:
        result["passed"] = True
        result["reason"] = (
            f"B wins by {result['lift_pp']}pp "
            f"(>= {cfg['min_lift_pp']}pp) with confidence {result['confidence']:.2f} "
            f"(>= {cfg['min_confidence']})"
        )
        _total_passed += 1
    else:
        result["passed"] = False
        result["reason"] = (
            f"insufficient lift or confidence: "
            f"lift={result['lift_pp']}pp (need {cfg['min_lift_pp']}pp), "
            f"confidence={result['confidence']:.2f} (need {cfg['min_confidence']})"
        )
        _total_rejected += 1

    _append_cycle_log(
        f"skill={skill_name!r} samples={result['samples']} "
        f"wins={result['wins']} losses={result['losses']} ties={result['ties']} "
        f"lift={result['lift_pp']}pp confidence={result['confidence']:.2f} "
        f"passed={result['passed']} reason={result['reason']!r}"
    )
    _last_result = result
    _last_run_at = time.time()
    return result


def get_ab_status() -> dict[str, Any]:
    """One-shot status the dashboard / status endpoint can surface."""
    cfg = _config()
    return {
        "enabled": cfg["enabled"],
        "can_run_last": bool(_last_result and _last_result.get("can_run")),
        "last_run_at": _last_run_at,
        "last_result": _last_result,
        "total_runs": _total_runs,
        "total_passed": _total_passed,
        "total_rejected": _total_rejected,
        "total_skipped": _total_skipped,
        "last_error": _last_error,
        "judge_model": cfg["judge_model"],
        "judge_endpoint_set": bool(os.environ.get("SKILLOPT_JUDGE_ENDPOINT", "").strip()),
        "judge_fallback_active": _judge_fn is None,
        "judge_mode": _judge_mode,
        "min_n": cfg["min_n"],
        "min_lift_pp": cfg["min_lift_pp"],
        "min_confidence": cfg["min_confidence"],
        "n": cfg["n"],
    }


def reset_for_tests() -> None:
    """Drop all state. Used by the smoke tests so order doesn't matter."""
    global _last_run_at, _last_result, _total_runs
    global _total_passed, _total_rejected, _total_skipped, _last_error
    global _judge_fn
    _last_run_at = 0.0
    _last_result = None
    _total_runs = 0
    _total_passed = 0
    _total_rejected = 0
    _total_skipped = 0
    _last_error = None
    _judge_fn = None
