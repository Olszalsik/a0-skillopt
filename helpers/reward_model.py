"""
SkillOpt - reward model for rollout outcome classification.

ROADMAP Day-3, item 3 (reward model).

v1.2.0 design (per ROADMAP.md):
- Fine-tuned DistilBERT (350M params, <50ms CPU per call).
- 3-class softmax over {success, partial, failure}.
- Trained from ~5K labeled rollouts (labels via the A/B harness's LLM
  judge - the harness is Day-3 item 2, not yet implemented).
- Until the model is trained, this module falls back to the same
  keyword-based heuristic the harvester already uses. The fallback
  path is loud (logged + status field) so the dashboard makes it
  obvious that the model isn't doing the real work yet.

The harvester calls score_rollout(rollout) AFTER the heuristic and
stores BOTH outcomes. The result 'outcome' field of the rollout is
the model output IF source == 'model' AND confidence >= 0.6.
Otherwise we trust the heuristic. This preserves v1.1.0 behaviour on
day 1 (before training) and gradually moves authority to the model
as it accumulates evidence.

Failure-mode policy (per ROADMAP engineering principle 3):
- Model file missing -> heuristic fallback, log INFO, source='heuristic_fallback'
- Model file corrupted -> heuristic fallback, log WARNING, source='heuristic_fallback_error'
- Inference exception -> heuristic fallback, log WARNING, source='heuristic_fallback_error'
- Garbage in rollout (no task) -> default to heuristic 'failure', source='heuristic_fallback'

We never raise. A bug here cannot break the harvester - the worst
case is we behave exactly like v1.1.0 did.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLUGIN_NAME = "skillopt"

# 3-class index. Order matters: the model's argmax maps to this.
_CLASSES = ("success", "partial", "failure")

_DEFAULT_MODEL_DIRNAME = "models"  # relative to the plugin root
_DEFAULT_MODEL_NAME = "reward_model"

# Module-level cache. The first call to score_rollout() may spend
# ~1-2s loading the tokenizer + DistilBERT from disk. After that,
# inference is <50ms per call on CPU. Caching is per-process, which
# is correct: the harvester runs in the A0 framework process and
# the API endpoints run in the same process.
_lock = threading.Lock()
_tokenizer = None
_model = None
_model_dir: Path | None = None
_model_version: str = "0.0.0"
_load_attempted: bool = False
_load_error: str | None = None

# Lightweight counters the dashboard can read via get_model_status()
_fallback_count: int = 0
_model_call_count: int = 0
_last_call_at: float = 0.0


# ----------------------------------------------------------------------- #
# Plugin-root + model-path resolution
# ----------------------------------------------------------------------- #

def _plugin_root() -> Path:
    """Locate the installed SkillOpt plugin directory."""
    here = Path(__file__).resolve()
    # this file is <plugin>/helpers/reward_model.py
    return here.parent.parent


def model_path() -> Path:
    """Where we look for the trained model on disk.

    Override via SKILLOPT_REWARD_MODEL_DIR env var. Default is
    <plugin>/models/reward_model/ - the directory the training
    script writes to.
    """
    override = os.environ.get("SKILLOPT_REWARD_MODEL_DIR")
    if override:
        return Path(override)
    return _plugin_root() / _DEFAULT_MODEL_DIRNAME / _DEFAULT_MODEL_NAME


# ----------------------------------------------------------------------- #
# Heuristic fallback (mirrors the v1.1.0 harvester)
# ----------------------------------------------------------------------- #

_HARD_FAIL = (
    "traceback (most recent call last)",
    "unhandled exception",
    "fatal error",
    "attributeerror:", "importerror:", "modulenotfounderror:",
    "syntaxerror:", "nameerror:", "typeerror:", "valueerror:",
)
_RECOVERY = ("but then", "after retry", "successfully recovered", "recovered and")
_SOFT_FAIL = (
    "i could not", "i couldn't", "i was unable",
    "i don't have", "failed to", "couldn't find",
    "error occurred", "went wrong",
)


def _heuristic_outcome(last_response: str) -> str:
    """Mirror the v1.1.0 harvester's keyword classifier.

    Kept identical to _60_skillopt_harvest_rollout.py:_heuristic_outcome
    so the fallback path is bit-for-bit the same as the v1.1.0 baseline.
    """
    if not last_response:
        return "failure"
    text = last_response.lower()
    if any(kw in text for kw in _HARD_FAIL):
        if any(kw in text for kw in _RECOVERY):
            return "partial"
        return "failure"
    if any(kw in text for kw in _SOFT_FAIL):
        return "partial"
    return "success"


# ----------------------------------------------------------------------- #
# Model loading (lazy, never raises)
# ----------------------------------------------------------------------- #

def _try_load_model() -> bool:
    """Load the tokenizer + DistilBERT classifier if the model files exist.

    Returns True on success. On any failure sets _load_error and
    returns False - the caller then falls back to the heuristic.
    Never raises.
    """
    global _tokenizer, _model, _model_dir, _model_version
    global _load_attempted, _load_error

    p = model_path()
    if not p.is_dir() or not (p / "config.json").is_file():
        _load_error = f"model directory not found: {p}"
        return False

    try:
        from transformers import AutoTokenizer, AutoModelForSequenceClassification  # type: ignore
        _tokenizer = AutoTokenizer.from_pretrained(str(p))
        _model = AutoModelForSequenceClassification.from_pretrained(str(p))
        _model.eval()  # inference mode
        _model_dir = p
        # Read the version stamp written by the training script, if any
        ver_file = p / "skillopt_reward_version.json"
        if ver_file.is_file():
            try:
                _model_version = json.loads(ver_file.read_text(encoding="utf-8")).get("version", "0.0.0")
            except Exception:
                _model_version = "0.0.0"
        else:
            _model_version = "trained-untagged"
        _load_error = None
        log.info("[skillopt] reward model loaded: %s (version=%s)", p, _model_version)
        return True
    except Exception as e:
        _tokenizer = None
        _model = None
        _load_error = f"{type(e).__name__}: {e}"
        log.warning("[skillopt] reward model load failed: %s", _load_error)
        return False
    finally:
        _load_attempted = True


def _ensure_model_loaded() -> bool:
    """Lazy load. Thread-safe. Returns True if model is ready."""
    global _model, _tokenizer
    if _model is not None and _tokenizer is not None:
        return True
    with _lock:
        if _model is not None and _tokenizer is not None:
            return True
        return _try_load_model()


# ----------------------------------------------------------------------- #
# Public API
# ----------------------------------------------------------------------- #

def _rollout_to_text(rollout: dict) -> str:
    """Concatenate the rollout fields the reward model uses as input.

    The model was trained on (task, trajectory, outcome-context) joined
    with [SEP] tokens. Keep this stable - changing it means retraining.
    """
    task = (rollout.get("task") or "").strip()
    parts: list[str] = [task[:600]]
    traj = rollout.get("trajectory") or []
    if isinstance(traj, list):
        for step in traj[:5]:
            if isinstance(step, dict):
                role = step.get("role") or step.get("name") or "step"
                content = step.get("content") or step.get("args") or ""
                parts.append(f"{role}: {str(content)[:120]}")
            else:
                parts.append(str(step)[:120])
    # Trailing heuristic hint as a soft feature. The model learns to
    # weight or ignore it; the heuristic is provided as a feature not
    # a label so the model can correct it when it disagrees.
    heur = (rollout.get("outcome") or "").strip()
    if heur:
        parts.append(f"heuristic: {heur}")
    return " [SEP] ".join(p for p in parts if p)


def _infer(text: str) -> tuple[str, float, list[float]] | None:
    """Run one inference call. Returns (label, confidence, probs) or None."""
    if not _ensure_model_loaded() or _model is None or _tokenizer is None:
        return None
    try:
        import torch  # type: ignore  # local import - we already paid the load cost
        with _lock:  # serialize torch.no_grad() inference for thread safety
            inputs = _tokenizer(
                text,
                return_tensors="pt",
                truncation=True,
                max_length=512,
                padding=False,
            )
            with torch.no_grad():
                logits = _model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)[0].tolist()
        idx = int(max(range(len(probs)), key=lambda i: probs[i]))
        return _CLASSES[idx], float(probs[idx]), [float(p) for p in probs]
    except Exception as e:
        global _load_error
        _load_error = f"inference error: {type(e).__name__}: {e}"
        log.warning("[skillopt] reward model inference failed: %s", _load_error)
        return None


def score_rollout(rollout: dict, *, prefer_model_above: float = 0.6) -> dict:
    """Classify a rollout's outcome.

    Returns a dict with these keys (always present):
      - success, partial, failure : probability in [0, 1] (sum to 1.0)
      - confidence                : max probability
      - outcome                   : 'success' | 'partial' | 'failure'
      - source                    : 'model' | 'heuristic' | 'heuristic_fallback'
                                     | 'heuristic_fallback_error' | 'heuristic_no_input'
      - model_version             : the loaded version, or 'none'
      - error                     : last error string if any, else None
      - prefer_model_above        : the threshold used

    The 'source' field tells the caller whether to trust the label
    over the v1.1.0 heuristic. The harvester stores BOTH the v1.1.0
    heuristic and the model output, and the consumers (the A/B
    harness, the direct optimiser) prefer the model when
    source == 'model' AND confidence >= prefer_model_above.

    Never raises. A rollout with no `task` field is a heuristic
    'failure' with source='heuristic_no_input' - the engine has
    nothing to learn from.
    """
    global _fallback_count, _model_call_count, _last_call_at
    _last_call_at = time.time()

    # v1.2.0: defensive type guard. The harvester can produce a `None`
    # or non-dict record if upstream is buggy; a corrupted record must
    # never crash the harvester hook. Treat it as a no-signal rollout
    # and return the canonical 'heuristic_no_input' result.
    if not isinstance(rollout, dict):
        _fallback_count += 1
        return {
            "success": 0.0, "partial": 0.0, "failure": 1.0,
            "confidence": 1.0, "outcome": "failure",
            "source": "heuristic_no_input",
            "model_version": "none", "error":
                f"score_rollout received non-dict input: {type(rollout).__name__}",
            "prefer_model_above": prefer_model_above,
        }

    task = (rollout.get("task") or "").strip()
    last_response = (rollout.get("last_response") or "").strip()
    if not task and not last_response:
        # Truly empty rollout - no signal at all. Treat as failure.
        _fallback_count += 1
        return {
            "success": 0.0, "partial": 0.0, "failure": 1.0,
            "confidence": 1.0, "outcome": "failure",
            "source": "heuristic_no_input",
            "model_version": "none", "error": None,
            "prefer_model_above": prefer_model_above,
        }

    text = _rollout_to_text(rollout)
    result = _infer(text)

    if result is None:
        # Model unavailable - heuristic fallback.
        _fallback_count += 1
        label = _heuristic_outcome(last_response)
        return {
            "success": 1.0 if label == "success" else 0.0,
            "partial": 1.0 if label == "partial" else 0.0,
            "failure": 1.0 if label == "failure" else 0.0,
            "confidence": 1.0,
            "outcome": label,
            "source": "heuristic_fallback_error" if _load_error else "heuristic_fallback",
            "model_version": "none",
            "error": _load_error,
            "prefer_model_above": prefer_model_above,
        }

    label, conf, probs = result
    _model_call_count += 1
    # Build the prob vector in the canonical class order
    prob_map = dict(zip(_CLASSES, probs))
    return {
        "success": prob_map.get("success", 0.0),
        "partial": prob_map.get("partial", 0.0),
        "failure": prob_map.get("failure", 0.0),
        "confidence": conf,
        "outcome": label,
        "source": "model",
        "model_version": _model_version,
        "error": None,
        "prefer_model_above": prefer_model_above,
    }


def get_model_status() -> dict[str, Any]:
    """One-shot status the dashboard / status endpoint can surface."""
    p = model_path()
    model_present = p.is_dir() and (p / "config.json").is_file()
    return {
        "path": str(p),
        "model_present_on_disk": model_present,
        "model_loaded": _model is not None and _tokenizer is not None,
        "model_version": _model_version if _model is not None else "none",
        "fallback_count": _fallback_count,
        "model_call_count": _model_call_count,
        "last_call_at": _last_call_at,
        "load_error": _load_error,
        "classes": list(_CLASSES),
        "enabled": True,  # v1.2.0: always on; future versions may add a toggle
    }


def reset_for_tests() -> None:
    """Drop the cached model. Used by tests to force a re-load."""
    global _tokenizer, _model, _load_attempted, _load_error, _model_version
    global _fallback_count, _model_call_count, _last_call_at
    _tokenizer = None
    _model = None
    _load_attempted = False
    _load_error = None
    _model_version = "0.0.0"
    _fallback_count = 0
    _model_call_count = 0
    _last_call_at = 0.0
