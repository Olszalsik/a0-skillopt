"""
SkillOpt LLM judge - v1.8.0 outcome labelling pass (P4).

Labels each harvested rollout's outcome (success / partial / failure) by
asking an LLM to judge the agent's final response against the task. These
labels are the training data for the DistilBERT reward model
(`scripts/train_reward_model.py`, P5). The 3-class space is aligned with
`reward_model.score_rollout` so a trained model predicts the same labels the
judge produces.

Reuses `direct_optimizer._call_llm` (OpenAI-compatible, `.skillopt-env`
credentials) with a judge-specific system prompt, so the judge uses the same
backend/credentials the optimizer already uses. Never raises: a judge failure
leaves the rollout unlabelled, and the training loader simply skips
unlabelled rollouts.

Public surface:
- JUDGE_SYSTEM / _build_judge_prompt(rollout)   (testable directly)
- judge_outcome(rollout, *, model=None) -> dict
    {label, confidence, reason, model}  |  {label: None, error: ...}
- label_rollout_file(path, *, force=False, model=None) -> dict
    {labelled, label, skipped, error}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PLUGIN_NAME = "skillopt"

_VALID_LABELS = ("success", "partial", "failure")

JUDGE_SYSTEM = (
    "You are an expert evaluator for autonomous coding-agent turns. You are "
    "given a TASK the agent was asked to do, the agent's final RESPONSE, and "
    "the TRAJECTORY of steps it took. Classify the OUTCOME of the turn into "
    "exactly one of: 'success' (the task was completed correctly), 'partial' "
    "(meaningful progress but incomplete or flawed), 'failure' (the task was "
    "not completed, or the response is an error / refusal / traceback). Also "
    "give a confidence in [0, 1] and a one-line reason. Respond with ONLY a "
    'JSON object: {"label": "success|partial|failure", "confidence": 0.0-1.0, '
    '"reason": "..."}. No preamble, no markdown fences.'
)


def _plugin_root() -> Path:
    here = Path(__file__).resolve()
    return here.parent.parent


def _ensure_path() -> None:
    root = str(_plugin_root())
    import sys as _sys
    if root not in _sys.path:
        _sys.path.insert(0, root)


def _build_judge_prompt(rollout: dict[str, Any]) -> str:
    """Build the user-message text for the judge from a rollout record."""
    task = str(rollout.get("task") or "").strip()
    response = str(rollout.get("last_response") or "").strip()
    traj = rollout.get("trajectory") or []
    steps: list[str] = []
    if isinstance(traj, list):
        for step in traj[:5]:
            if isinstance(step, dict):
                role = step.get("role") or step.get("name") or "step"
                content = step.get("content") or step.get("args") or ""
                steps.append(f"{role}: {str(content)[:160]}")
            else:
                steps.append(str(step)[:160])
    parts = [
        f"TASK:\n{task[:800]}",
        f"RESPONSE:\n{response[:2000]}",
        f"TRAJECTORY:\n" + ("\n".join(steps) if steps else "(none)"),
    ]
    return "\n\n".join(parts)


def _strip_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # drop the opening fence (with optional language) and the closing fence
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3].rstrip()
    return s.strip()


def _parse_judge_response(raw: str) -> dict[str, Any]:
    """Parse the judge's JSON response. Never raises.

    Returns {label, confidence, reason} on success, or {label: None, error}
    on any parse/validation failure.
    """
    try:
        data = json.loads(_strip_fences(raw))
    except Exception as e:
        return {"label": None, "error": f"json parse: {type(e).__name__}: {e}"}
    if not isinstance(data, dict):
        return {"label": None, "error": f"non-dict response: {type(data).__name__}"}
    label = str(data.get("label") or "").strip().lower()
    if label not in _VALID_LABELS:
        return {"label": None, "error": f"bad label {label!r}; want one of {_VALID_LABELS}"}
    try:
        conf = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    reason = str(data.get("reason") or "").strip()
    return {"label": label, "confidence": conf, "reason": reason}


def _judge_model(model: str | None) -> str:
    if model:
        return model
    env_model = os.environ.get("SKILLOPT_JUDGE_MODEL")
    if env_model:
        return env_model
    _ensure_path()
    from helpers import direct_optimizer  # type: ignore
    return direct_optimizer._default_model()


def judge_outcome(rollout: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    """Judge one rollout's outcome via the LLM. Never raises.

    Returns {label, confidence, reason, model} on success, or
    {label: None, error: ...} on any failure (LLM unreachable, bad response).
    """
    try:
        _ensure_path()
        from helpers import direct_optimizer  # type: ignore
        prompt = _build_judge_prompt(rollout)
        raw = direct_optimizer._call_llm(
            prompt, _judge_model(model), max_tokens=300, system=JUDGE_SYSTEM,
        )
        parsed = _parse_judge_response(raw)
        if parsed.get("label") is None:
            return parsed  # already an {label: None, error} shape
        return {
            "label": parsed["label"],
            "confidence": parsed["confidence"],
            "reason": parsed["reason"],
            "model": _judge_model(model),
        }
    except Exception as e:  # noqa: BLE001
        return {"label": None, "error": f"{type(e).__name__}: {e}"}


def label_rollout_file(
    path: str | Path, *, force: bool = False, model: str | None = None
) -> dict[str, Any]:
    """Label one rollout JSON file in place (atomic). Idempotent.

    Returns {labelled: bool, label, skipped: bool, error?}. Skips (labelled=
    False, skipped=True) when the file already has a `judge_label` and
    `force` is False. Atomic rewrite via a temp file + os.replace so a crash
    mid-write never corrupts the rollout.
    """
    p = Path(path)
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"labelled": False, "skipped": False, "error": f"read: {type(e).__name__}: {e}"}
    if not isinstance(rec, dict):
        return {"labelled": False, "skipped": False, "error": "rollout is not a dict"}

    if rec.get("judge_label") in _VALID_LABELS and not force:
        return {"labelled": False, "label": rec["judge_label"], "skipped": True}

    result = judge_outcome(rec, model=model)
    if result.get("label") is None:
        return {"labelled": False, "skipped": False, "error": result.get("error", "unknown")}

    rec["judge_label"] = result["label"]
    rec["judge_confidence"] = result["confidence"]
    rec["judge_reason"] = result["reason"]
    rec["judge_model"] = result["model"]
    rec["judge_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())

    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as e:  # noqa: BLE001
        return {"labelled": False, "skipped": False, "error": f"write: {type(e).__name__}: {e}"}

    return {"labelled": True, "label": result["label"], "skipped": False}