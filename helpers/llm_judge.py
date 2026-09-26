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
import random
import threading
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
    # v1.8.28: a slot sentinel ('chat' OR 'utility') must be RESOLVED, not
    # returned as a literal model name. The old `model != "chat"` test would
    # have handed "utility" straight to the provider as a model id.
    try:
        try:
            from usr.plugins.skillopt.helpers import chat_model as _cm0  # type: ignore
        except ImportError:
            from helpers import chat_model as _cm0  # type: ignore
        _is_slot = _cm0.is_slot
    except Exception:  # noqa: BLE001
        def _is_slot(v):
            return str(v or "").strip() in ("chat", "utility")

    if model and not _is_slot(model):
        return model
    # v1.8.28: a passed-in slot is a REQUEST, and must be honoured - the old
    # code dropped it and fell back to optimizer_model, so asking for
    # "utility" silently ran the chat model. Env still wins (a deliberate pin),
    # then the requested slot, then the configured default.
    _req_slot = model if (model and _is_slot(model)) else ""
    env_model = os.environ.get("SKILLOPT_JUDGE_MODEL")
    raw = env_model or _req_slot or ""
    if not raw:
        _ensure_path()
        # v1.8.1: two-path import - the bare `helpers` resolves to the
        # framework's helpers package in the framework runtime.
        try:
            from usr.plugins.skillopt.helpers import direct_optimizer  # type: ignore
        except ImportError:
            from helpers import direct_optimizer  # type: ignore
        raw = direct_optimizer._default_model()
    # v1.8.12/v1.8.28: resolve the slot sentinel (or empty) to the concrete
    # active model for that slot so the recorded judge_model names the model
    # actually used.
    if raw:
        try:
            try:
                from usr.plugins.skillopt.helpers import chat_model as _cm  # type: ignore
            except ImportError:
                from helpers import chat_model as _cm  # type: ignore
            resolved, _conn = _cm.effective_model(raw)
            if resolved:
                return resolved
        except Exception:  # noqa: BLE001
            pass
    return raw


# v1.8.16: judge burst protection - concurrency limiter + inter-request pacing
# + exponential backoff retries on HTTP 429 / transient transport errors.
#
# Batch labelling (scripts/label_rollouts.py) fires one judge LLM call per
# rollout back-to-back, which trips provider rate limits (429). Three layers,
# all standard-library (threading/time/random - the judge surface is fully
# synchronous, so a threading semaphore is the correct limiter here; an
# asyncio semaphore cannot gate cross-thread sync callers):
#   1. Pacing  - _throttle_wait() spaces consecutive HTTP attempts at least
#                SKILLOPT_JUDGE_THROTTLE_S apart (env, default 1.5; 0
#                disables). v1.8.28: the lock is held ACROSS the sleep and the
#                REAL start is stamped, so actual - not merely planned -
#                attempts stay spaced despite OS timer jitter. N concurrent
#                callers get N consecutive spaced slots.
#   2. Limiter - _get_limiter() caps in-flight judge calls at
#                SKILLOPT_JUDGE_MAX_CONCURRENCY (default 1 = strict
#                serialization; BoundedSemaphore released in finally).
#   3. Retries - _judge_llm_call() retries retryable failures (429 / rate
#                limit, 5xx, timeouts, connection errors) with exponential
#                backoff + jitter, honoring Retry-After when the SDK exposes
#                it. Knobs: SKILLOPT_JUDGE_RETRY_MAX (default 3),
#                SKILLOPT_JUDGE_RETRY_BASE_S (0.5), SKILLOPT_JUDGE_RETRY_MAX_S
#                (8.0).
# judge_outcome keeps its never-raises contract: non-retryable errors and
# retry exhaustion propagate to its wrapper and become {label: None, error}.
# Non-judge LLM callers are untouched - direct_optimizer is never modified.
_JUDGE_THROTTLE_DEFAULT_S = 1.5
_throttle_state = {'last': None}
_throttle_lock = threading.Lock()
_limiter_lock = threading.Lock()
_limiter = None
_limiter_n = None
_retry_stats = {'attempts': 0, 'retries': 0, 'exhausted': 0}


def _judge_throttle_seconds() -> float:
    raw = os.environ.get('SKILLOPT_JUDGE_THROTTLE_S')
    if not raw:
        return _JUDGE_THROTTLE_DEFAULT_S
    try:
        val = float(raw)
    except ValueError:
        return _JUDGE_THROTTLE_DEFAULT_S
    return max(0.0, val)


def _sleep(seconds: float) -> None:
    'Seam for tests: all judge sleeps (pacing + backoff) route here.'
    time.sleep(seconds)


def _monotonic() -> float:
    '''Seam for tests: the clock pacing stamps with.

    Exists so the reservation logic can be tested DETERMINISTICALLY. Real
    time.sleep cannot honour a small interval reliably - the Windows timer
    granularity is ~15.6ms, which is the same order as the sub-20ms
    intervals this pacing is verified at, so a wall-clock assertion of
    "gaps >= 15ms" races the scheduler and flakes. Injecting the clock
    tests the arithmetic instead of the OS timer.
    '''
    return time.monotonic()


def _throttle_wait() -> float:
    '''Reserve + sleep so consecutive judge HTTP attempts stay >= interval apart.

    v1.8.16 reserved an IDEAL slot under _throttle_lock, released the lock,
    then slept. That guarantees the *planned* starts are spaced, but the
    ACTUAL starts are what hit the provider, and those are subject to OS
    timer/scheduler jitter - on Windows the default timer granularity is
    ~15.6ms, comparable to a small interval. Waking early/late let real
    starts collapse toward each other, so under load the plugin could fire
    HTTP attempts closer together than SKILLOPT_JUDGE_THROTTLE_S. That is
    exactly the 429 burst pacing exists to prevent.

    v1.8.28: hold the lock ACROSS the sleep and stamp the ACTUAL start time.
    The wait is now serialized, which is the correct semantic for pacing
    (consecutive attempts are spaced by construction), while the judge calls
    themselves still run concurrently afterwards - the parallelism comes
    from the call duration, not from the wait. Stamping the real start also
    self-corrects for jitter: an oversleep pushes the next slot out rather
    than being silently absorbed.

    Returns the waited seconds (0 on the first call or when disabled).
    '''
    interval = _judge_throttle_seconds()
    with _throttle_lock:
        now = _monotonic()
        last = _throttle_state['last']
        if interval <= 0.0 or last is None:
            slot = now
        else:
            slot = max(now, last + interval)
        wait = max(0.0, slot - now)
        if wait > 0.0:
            _sleep(wait)
        # Stamp the REAL start, not the ideal slot, so jitter accumulates
        # conservatively instead of collapsing the next reservation.
        _throttle_state['last'] = _monotonic()
    return wait


def _judge_max_concurrency() -> int:
    raw = os.environ.get('SKILLOPT_JUDGE_MAX_CONCURRENCY')
    if not raw:
        return 1
    try:
        val = int(raw)
    except ValueError:
        return 1
    return max(1, val)


def _get_limiter() -> threading.BoundedSemaphore:
    'In-flight cap for judge LLM calls (lazily built from env, locked).'
    global _limiter, _limiter_n
    n = _judge_max_concurrency()
    with _limiter_lock:
        if _limiter is None or _limiter_n != n:
            _limiter = threading.BoundedSemaphore(n)
            _limiter_n = n
        return _limiter


def _reset_burst_state() -> None:
    'Reset pacer stamp + limiter + retry stats (test isolation helper).'
    global _limiter, _limiter_n
    with _limiter_lock:
        _limiter = None
        _limiter_n = None
    _throttle_state['last'] = None
    _retry_stats.update(attempts=0, retries=0, exhausted=0)


def _retry_max() -> int:
    raw = os.environ.get('SKILLOPT_JUDGE_RETRY_MAX')
    if not raw:
        return 3
    try:
        return max(0, int(raw))
    except ValueError:
        return 3


def _retry_base_s() -> float:
    raw = os.environ.get('SKILLOPT_JUDGE_RETRY_BASE_S')
    if not raw:
        return 0.5
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.5


def _retry_max_s() -> float:
    raw = os.environ.get('SKILLOPT_JUDGE_RETRY_MAX_S')
    if not raw:
        return 8.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 8.0


_RETRYABLE_HINTS = (
    '429', 'rate limit', 'ratelimit', 'too many requests',
    'timeout', 'timed out', 'connection', 'temporarily unavailable',
    'overloaded', '502', '503', '504', 'bad gateway', 'service unavailable',
)


def _is_retryable(exc: BaseException) -> bool:
    'True for HTTP 429 / 5xx / transient transport errors (attrs + text).'
    sc = getattr(exc, 'status_code', None)
    if isinstance(sc, int) and (sc == 429 or 500 <= sc <= 599):
        return True
    text = str(exc).lower()
    return any(h in text for h in _RETRYABLE_HINTS)


def _backoff_seconds(attempt: int, exc: BaseException | None = None) -> float:
    '''Exponential backoff with jitter; honors Retry-After when exposed.

    delay = min(SKILLOPT_JUDGE_RETRY_MAX_S, base_s * 2**attempt), then
    halved-to-full jitter (x uniform(0.5, 1.0)). A numeric Retry-After
    (exception attr or response header, seconds) is used verbatim, capped
    at MAX_S.
    '''
    cap = _retry_max_s()
    raw_ra = None
    if exc is not None:
        raw_ra = getattr(exc, 'retry_after', None)
        if raw_ra is None:
            try:
                hdrs = getattr(getattr(exc, 'response', None), 'headers', None)
                if hdrs:
                    raw_ra = hdrs.get('retry-after') or hdrs.get('Retry-After')
            except Exception:  # noqa: BLE001
                raw_ra = None
    if raw_ra is not None:
        try:
            return min(cap, max(0.0, float(raw_ra)))
        except (TypeError, ValueError):
            pass
    base = _retry_base_s()
    delay = min(cap, base * (2 ** attempt))
    return delay * random.uniform(0.5, 1.0)


def _judge_llm_call(direct_optimizer: Any, prompt: str, model: str) -> str:
    '''One judge LLM call with limiter + pacing + backoff retries.

    Per attempt: acquire the in-flight slot, pace (reserve a throttle slot),
    call. Retryable failures release the slot, back off, and retry up to
    SKILLOPT_JUDGE_RETRY_MAX times. Non-retryable failures re-raise
    immediately; exhaustion re-raises the last error - both land in
    judge_outcome never-raises wrapper as {label: None, error}.
    '''
    attempts_allowed = _retry_max() + 1
    last_exc: BaseException | None = None
    for attempt in range(attempts_allowed):
        _retry_stats['attempts'] += 1
        with _get_limiter():
            _throttle_wait()
            try:
                return direct_optimizer._call_llm(
                    prompt, model, max_tokens=300, system=JUDGE_SYSTEM,
                )
            except Exception as e:  # noqa: BLE001
                last_exc = e
                if not _is_retryable(e):
                    raise
        if attempt + 1 < attempts_allowed:
            _retry_stats['retries'] += 1
            _sleep(_backoff_seconds(attempt, last_exc))
    _retry_stats['exhausted'] += 1
    assert last_exc is not None
    raise last_exc


def judge_outcome(rollout: dict[str, Any], *, model: str | None = None) -> dict[str, Any]:
    """Judge one rollout's outcome via the LLM. Never raises.

    Returns {label, confidence, reason, model} on success, or
    {label: None, error: ...} on any failure (LLM unreachable, bad response).
    """
    try:
        # v1.8.1: resolve the model ONCE (the old code called _judge_model
        # twice — once for the call, once for the recorded field — which
        # re-read the env file and could record a different model than used).
        resolved_model = _judge_model(model)
        _ensure_path()
        try:
            from usr.plugins.skillopt.helpers import direct_optimizer  # type: ignore
        except ImportError:
            from helpers import direct_optimizer  # type: ignore
        prompt = _build_judge_prompt(rollout)
        raw = _judge_llm_call(direct_optimizer, prompt, resolved_model)
        parsed = _parse_judge_response(raw)
        if parsed.get("label") is None:
            return parsed  # already an {label: None, error} shape
        return {
            "label": parsed["label"],
            "confidence": parsed["confidence"],
            "reason": parsed["reason"],
            "model": resolved_model,
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