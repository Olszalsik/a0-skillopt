"""
SkillOpt inner loop - per-rollout critique-and-suggest.

ROADMAP Day-4, item 4.

Today every Sleep cycle is 'rewrite the whole skill.' That is expensive
(one LLM call per cycle) and coarse. The inner loop is per-rollout:

  1. After every chat, the harvester writes the rollout and tags it
     with `awaiting_suggestion: true` (see
     extensions/python/monologue_end/_60_skillopt_harvest_rollout.py).
     The harvester does NOT call the LLM - that would slow every chat.
  2. A separate background worker (`inner_loop_thread`, started by
     `helpers/auto_loop.py`) calls `inner_loop_tick()` every
     `inner_loop_interval_seconds` (default 60s). The tick scans
     `logs/rollouts/` for rollouts without a corresponding
     suggestion file, calls the LLM with a tiny prompt
     (`INNER_LOOP_PROMPT`), and enqueues a one-sentence suggestion
     for each.
  3. The suggestion is appended to a queue
     (`logs/runs/suggestions/<skill>_<rollout>_<ts>.md`).
  4. When the outer loop fires, it reads all pending suggestions via
     `list_pending_suggestions()` and produces ONE targeted proposal -
     not a full rewrite. The targeted prompt is the whole point of
     the inner loop.

Critical rules (per ROADMAP engineering principle 6 - two-loop = clear
contracts):
  - Inner loop writes only to `logs/runs/suggestions/` and
    `logs/runs/inner_loop.log`.
  - Inner loop never writes to `staging/`, `logs/runs/critiques/`,
    `logs/runs/adoptions.log`, or `usr/skills/<name>/SKILL.md`.
  - The 41 v1.2.0 tests must remain green (the inner loop is
    additive, never a refactor of the outer loop).

Failure-mode policy (per ROADMAP engineering principle 2):
  - LLM unreachable -> `errors=1`, `last_error='llm_unreachable'`,
    never raises. Status snapshot surfaces `last_error`.
  - Cycle log is the source of truth: every `inner_loop_tick`
    appends one line to `logs/runs/inner_loop.log`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any


PLUGIN_NAME = "skillopt"
log = logging.getLogger("skillopt.inner_loop")


# ----------------------------------------------------------------------- #
# The tiny prompt (module-level so it's easy to iterate)
# ----------------------------------------------------------------------- #

INNER_LOOP_PROMPT = """You are a skill-improvement suggester. You are given:

  TASK         : the agent's original task
  TRAJECTORY   : the tool calls the agent made (truncated)
  LAST_RESPONSE: the agent's last response (truncated)
  OUTCOME      : success | partial | failure
  FAILURE_MODE : a one-line hint about what went wrong (or 'none')
  SKILL_HINT   : the skill the agent was using (or 'unknown')

Your job: read the task, the trajectory, and the failure mode. Reply
with one JSON object, no prose:

  {"suggestion": "<one-sentence improvement to the skill used>",
   "confidence": 0..1,
   "failure_mode": "<one-line restatement of what went wrong, or 'none'"}

The suggestion must be a single concrete edit to the skill (e.g.
'add a step that validates the input before parsing', 'mention the
need to set timeouts', 'warn against mutating the input dict').
If the rollout was a clean success, you may still suggest a small
tightening. If you have no useful suggestion, return
{"suggestion": "", "confidence": 0.0, "failure_mode": "none"}. Be
conservative with confidence."""


# ----------------------------------------------------------------------- #
# Configuration
# ----------------------------------------------------------------------- #

DEFAULT_ENABLED = True
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_MAX_AGE_SECONDS = 7 * 86400  # 7 days
DEFAULT_MIN_ROLLOUT_CONFIDENCE = 0.4
DEFAULT_LLM_MODEL = "minimax-m3"


def _config() -> dict[str, Any]:
    """Read inner_loop_* keys from the merged config + env overrides."""
    try:
        from helpers.sleep_runner import merged_config  # type: ignore
        cfg = merged_config()
    except Exception:
        cfg = {}
    out = {
        "enabled": bool(cfg.get("inner_loop_enabled", DEFAULT_ENABLED)),
        "interval_seconds": int(cfg.get("inner_loop_interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        "max_age_seconds": int(cfg.get("inner_loop_max_suggestion_age_seconds", DEFAULT_MAX_AGE_SECONDS)),
        "min_rollout_confidence": float(cfg.get("inner_loop_min_rollout_confidence", DEFAULT_MIN_ROLLOUT_CONFIDENCE)),
        "llm_model": str(cfg.get("inner_loop_llm_model", cfg.get("target_model", DEFAULT_LLM_MODEL))),
    }
    if os.environ.get("SKILLOPT_INNER_LOOP_ENABLED"):
        out["enabled"] = os.environ["SKILLOPT_INNER_LOOP_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
    return out


# ----------------------------------------------------------------------- #
# Paths
# ----------------------------------------------------------------------- #

def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _rollouts_dir() -> Path:
    p = _plugin_root() / "logs" / "rollouts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def suggestions_dir() -> Path:
    p = _plugin_root() / "logs" / "runs" / "suggestions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def inner_log_path() -> Path:
    p = _plugin_root() / "logs" / "runs" / "inner_loop.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ----------------------------------------------------------------------- #
# Module-level state (counters, last run)
# ----------------------------------------------------------------------- #

_last_tick_at: float = 0.0
_total_ticks: int = 0
_total_suggestions: int = 0
_last_error: str | None = None


# ----------------------------------------------------------------------- #
# LLM call (mirror ab_harness._llm_judge_via_http - POST JSON, never raise)
# ----------------------------------------------------------------------- #

def _llm_suggest_via_http(
    payload: dict[str, Any], *, endpoint: str, model: str, timeout: float = 15.0,
) -> dict[str, Any]:
    """POST a suggestion request to `endpoint`. Returns {text, error}.

    Never raises. On any failure returns {"text": "", "error": <reason>}
    so the caller can record `last_error` and move on (per ROADMAP
    principle 2).
    """
    import urllib.request  # local import to keep import-time fast
    import urllib.error
    body = {"model": model, "prompt": INNER_LOOP_PROMPT, **payload}
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return {"text": raw, "error": None}
        except Exception:
            return {"text": "", "error": f"endpoint returned non-text: {type(raw).__name__}"}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        return {"text": "", "error": f"llm_unreachable: {type(e).__name__}: {e}"}
    except Exception as e:
        # Belt-and-braces: a bug in the LLM path must NEVER crash the
        # background worker. Record and continue.
        return {"text": "", "error": f"{type(e).__name__}: {e}"}


def _extract_suggestion(llm_text: str) -> tuple[str, float, str]:
    """Pull {suggestion, confidence, failure_mode} out of the LLM's text.

    The model is supposed to return a single JSON object. We tolerate
    prose around it. Returns ("", 0.0, "none") on any failure.
    """
    if not llm_text:
        return "", 0.0, "none"
    # Find the first {...} block on a single line OR across lines
    m = re.search(r"\{[^{}]*\}", llm_text, re.DOTALL)
    if not m:
        return "", 0.0, "none"
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return "", 0.0, "none"
    if not isinstance(obj, dict):
        return "", 0.0, "none"
    suggestion = str(obj.get("suggestion") or "").strip()
    try:
        confidence = float(obj.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    failure_mode = str(obj.get("failure_mode") or "none").strip() or "none"
    return suggestion, confidence, failure_mode


# ----------------------------------------------------------------------- #
# Enqueue / list / drain
# ----------------------------------------------------------------------- #

def enqueue_suggestion(
    rollout: dict[str, Any],
    suggestion_text: str,
    skill_name: str | None = None,
) -> dict[str, Any]:
    """Write a one-sentence suggestion to logs/runs/suggestions/.

    Returns {"path": <abs>, "bytes": <n>, "ts": <epoch>}. The file
    has a YAML-ish frontmatter block followed by the suggestion text
    as the body. Never raises.
    """
    try:
        rid = str(rollout.get("id") or "unknown")
        skill = (skill_name or rollout.get("skill_used") or "unknown") or "unknown"
        # Sanitize for filenames: only [A-Za-z0-9_-] survive
        safe_skill = re.sub(r"[^A-Za-z0-9_\-]", "_", str(skill))[:64] or "unknown"
        safe_rid = re.sub(r"[^A-Za-z0-9_\-]", "_", rid)[:64] or "unknown"
        ts = time.time()
        ts_int = int(ts)
        out_path = suggestions_dir() / f"{safe_skill}_{safe_rid}_{ts_int}.md"
        task = str(rollout.get("task") or "")[:240]
        failure_mode = str(rollout.get("outcome") or "none")
        try:
            confidence = float((rollout.get("reward") or {}).get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        frontmatter = (
            "---\n"
            f"skill: {safe_skill}\n"
            f"rollout_id: {safe_rid}\n"
            f"ts: {ts_int}\n"
            f"task: {task!r}\n"
            f"failure_mode: {failure_mode}\n"
            f"confidence: {confidence}\n"
            "---\n\n"
        )
        body = (suggestion_text or "").rstrip() + "\n"
        out_path.write_text(frontmatter + body, encoding="utf-8")
        return {"path": str(out_path), "bytes": out_path.stat().st_size, "ts": ts}
    except Exception as e:
        # enqueue_suggestion is called by the harvester path. A bug
        # here can never crash the outer loop. Log and return empty.
        log.debug("[skillopt] enqueue_suggestion failed: %s", e)
        return {"path": "", "bytes": 0, "ts": time.time()}


def _parse_suggestion_file(path: Path) -> dict[str, Any] | None:
    """Read a suggestion file and return its parsed dict, or None on failure."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm_block = text[3:end].strip()
    body = text[end + 4:].lstrip("\n").rstrip()
    out: dict[str, Any] = {
        "path": str(path),
        "skill": "unknown",
        "rollout_id": "unknown",
        "ts": 0.0,
        "task": "",
        "failure_mode": "none",
        "confidence": 0.0,
        "text": body,
    }
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k == "skill":
            out["skill"] = v
        elif k == "rollout_id":
            out["rollout_id"] = v
        elif k == "ts":
            try:
                out["ts"] = float(v)
            except ValueError:
                pass
        elif k == "task":
            out["task"] = v
        elif k == "failure_mode":
            out["failure_mode"] = v
        elif k == "confidence":
            try:
                out["confidence"] = float(v)
            except ValueError:
                pass
    return out


def list_pending_suggestions(
    skill_name: str | None = None,
    since_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Return all unconsumed suggestions, sorted by ts ascending.

    Filters:
      skill_name  - exact match (case-insensitive). None = all skills.
      since_ts    - only suggestions with ts >= since_ts. None = all.

    Each entry has {path, skill, rollout_id, ts, task, failure_mode,
    confidence, text}. Sorted by ts ascending so the outer loop can
    batch them chronologically.
    """
    out: list[dict[str, Any]] = []
    sd = suggestions_dir()
    needle = (skill_name or "").strip().lower()
    for child in sorted(sd.glob("*.md")):
        rec = _parse_suggestion_file(child)
        if rec is None:
            continue
        if needle and rec["skill"].lower() != needle:
            continue
        if since_ts is not None and rec["ts"] < float(since_ts):
            continue
        out.append(rec)
    out.sort(key=lambda r: r.get("ts") or 0.0)
    return out


def drain_suggestions(
    skill_name: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> list[dict[str, Any]]:
    """Return AND delete suggestions older than `max_age_seconds` for `skill_name`.

    This is the consume half of the queue. The outer loop calls it
    once per cycle to mark suggestions as processed. Anything older
    than `max_age_seconds` is treated as stale and dropped
    (it was a hint the outer loop never acted on).

    Returns the list of dicts that were removed, in ts-ascending order.
    """
    out: list[dict[str, Any]] = []
    sd = suggestions_dir()
    needle = (skill_name or "").strip().lower()
    if not needle:
        return out
    now = time.time()
    for child in sorted(sd.glob("*.md")):
        rec = _parse_suggestion_file(child)
        if rec is None:
            continue
        if rec["skill"].lower() != needle:
            continue
        age = now - float(rec.get("ts") or 0.0)
        if age > float(max_age_seconds):
            try:
                child.unlink()
            except OSError:
                pass
            continue
        # Delete the file we just read (the contract is "return AND delete")
        try:
            child.unlink()
            out.append(rec)
        except OSError:
            # If we couldn't delete, still keep the entry so the caller
            # sees it - they may re-drain later.
            out.append(rec)
    out.sort(key=lambda r: r.get("ts") or 0.0)
    return out


# ----------------------------------------------------------------------- #
# Targeted prompt builder (the whole point of the inner loop)
# ----------------------------------------------------------------------- #

def build_targeted_prompt(
    skill_name: str,
    current_text: str,
    suggestions: list[dict[str, Any]],
    *,
    top_n: int = 3,
) -> str:
    """Return a prompt for the LLM that proposes a minimal edit to `current_text`.

    The prompt is the outer-loop side of the inner-loop contract: it
    takes the suggestions + the current SKILL.md and asks the LLM to
    produce one targeted edit (not a full rewrite) that addresses the
    top `top_n` suggestions.

    Returns an empty string if there are no usable suggestions (the
    caller should fall back to the generic 'rewrite the whole skill'
    prompt in that case).
    """
    if not suggestions:
        return ""
    # Sort by confidence desc, take the top N
    ranked = sorted(
        suggestions,
        key=lambda s: (-(float(s.get("confidence") or 0.0)), float(s.get("ts") or 0.0)),
    )[: max(1, int(top_n))]
    lines: list[str] = []
    lines.append(
        f"You are refining the skill {skill_name!r}. You are given the current\n"
        "SKILL.md below and a small list of per-rollout suggestions produced by\n"
        "the inner loop. Produce a MINIMAL edit to the SKILL.md that addresses the\n"
        "top suggestions. Do not rewrite the whole skill - return the new file\n"
        "with only the changed sections.\n"
    )
    lines.append("## Current SKILL.md")
    lines.append("```markdown")
    lines.append(current_text.rstrip())
    lines.append("```")
    lines.append("")
    lines.append(f"## {len(ranked)} suggestion(s) for fragments/topics")
    for i, s in enumerate(ranked, 1):
        ts = s.get("ts") or 0.0
        lines.append(
            f"{i}. (conf={float(s.get('confidence') or 0.0):.2f}, "
            f"failure_mode={s.get('failure_mode', 'none')!r}, ts={int(ts)})\n"
            f"   task: {str(s.get('task') or '')[:160]}\n"
            f"   suggestion: {str(s.get('text') or '').strip()[:240]}"
        )
    lines.append("")
    lines.append(
        "Reply with the full revised SKILL.md in a single ```markdown code block.\n"
        "Keep the structure; only change what the suggestions require."
    )
    return "\n".join(lines)


# ----------------------------------------------------------------------- #
# Background worker tick
# ----------------------------------------------------------------------- #

def _rollout_needs_suggestion(rollout: dict[str, Any]) -> bool:
    """Return True if `rollout` has no matching suggestion file yet."""
    rid = str(rollout.get("id") or "")
    if not rid:
        return False
    safe_rid = re.sub(r"[^A-Za-z0-9_\-]", "_", rid)[:64]
    skill = (rollout.get("skill_used") or "unknown")
    safe_skill = re.sub(r"[^A-Za-z0-9_\-]", "_", str(skill))[:64] or "unknown"
    sd = suggestions_dir()
    # Match either the explicit rollout_id or the older generic naming
    for p in sd.glob(f"{safe_skill}_{safe_rid}_*.md"):
        if p.is_file():
            return False
    # Also treat `awaiting_suggestion: true` as the source of truth
    # when the harvester sets it but the file isn't found yet (e.g. on
    # a fresh install the rollout was already there from a previous run).
    return bool(rollout.get("awaiting_suggestion", True))


def _build_rollout_payload(rollout: dict[str, Any]) -> dict[str, Any]:
    """Build the prompt payload the LLM sees for one rollout."""
    return {
        "task": str(rollout.get("task") or "")[:600],
        "trajectory": (rollout.get("trajectory") or [])[:5],
        "last_response": str(rollout.get("last_response") or "")[:600],
        "outcome": str(rollout.get("outcome") or "unknown"),
        "failure_mode": str(rollout.get("outcome") or "none"),
        "skill_hint": str(rollout.get("skill_used") or "unknown"),
    }


def inner_loop_tick(llm_endpoint: str | None = None) -> dict[str, Any]:
    """One tick of the background worker.

    Scans logs/rollouts/ for rollouts without a suggestion file, calls
    the LLM with the tiny prompt, and enqueues the result. Returns
    counters and never raises (per ROADMAP principle 2).

    Result shape (always present):
      scanned   - rollouts inspected
      suggested - suggestions successfully enqueued
      skipped   - rollouts skipped (low confidence, no skill, etc.)
      errors    - count of LLM/IO failures
      last_error - str | None, the most recent error (for the snapshot)
    """
    global _last_tick_at, _total_ticks, _total_suggestions, _last_error
    _total_ticks += 1
    counters = {"scanned": 0, "suggested": 0, "skipped": 0, "errors": 0, "last_error": None}
    cfg = _config()
    if not cfg["enabled"]:
        counters["last_error"] = "inner_loop_disabled"
        _last_tick_at = time.time()
        _append_tick_log(counters)
        return counters
    endpoint = (llm_endpoint or os.environ.get("SKILLOPT_SUGGEST_ENDPOINT") or "").strip()
    rd = _rollouts_dir()
    try:
        files = sorted(rd.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception as e:
        counters["errors"] += 1
        counters["last_error"] = f"scan_failed: {type(e).__name__}: {e}"
        _last_error = counters["last_error"]
        _last_tick_at = time.time()
        _append_tick_log(counters)
        return counters
    min_conf = float(cfg["min_rollout_confidence"])
    model = cfg["llm_model"]
    # Cap the per-tick work so a backlog of 1000 rollouts doesn't
    # block the worker for an hour. 50 is the v1.2.0 default; the
    # outer loop catches up on the next tick.
    PER_TICK_CAP = 50
    for f in files:
        if counters["scanned"] >= PER_TICK_CAP:
            break
        try:
            rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            counters["skipped"] += 1
            continue
        counters["scanned"] += 1
        if not _rollout_needs_suggestion(rec):
            counters["skipped"] += 1
            continue
        # Confidence gate: skip rollouts the reward model is unsure about
        try:
            conf = float((rec.get("reward") or {}).get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        # Heuristic-only rollouts (no `reward` field) get a pass so we
        # still learn before the reward model is trained.
        if rec.get("reward") and conf < min_conf:
            counters["skipped"] += 1
            continue
        # No endpoint -> deterministically enqueue a keyword-stub
        # suggestion so the queue is non-empty in tests and offline
        # installs. The real LLM path replaces this when an endpoint
        # is configured.
        if not endpoint:
            suggestion = (
                f"Inner-loop stub: rollout outcome={rec.get('outcome', 'unknown')!r} "
                f"for skill {rec.get('skill_used', 'unknown')!r}; "
                "configure SKILLOPT_SUGGEST_ENDPOINT for real LLM suggestions."
            )
            conf_out = 0.5
            fm_out = str(rec.get("outcome") or "none")
        else:
            payload = _build_rollout_payload(rec)
            resp = _llm_suggest_via_http(payload, endpoint=endpoint, model=model)
            if resp.get("error"):
                counters["errors"] += 1
                counters["last_error"] = resp["error"]
                _last_error = resp["error"]
                # Don't re-attempt this rollout on the next tick -
                # we just hit the same LLM-unreachable error. Mark it
                # done by enqueueing a stub. The outer loop will see
                # an empty suggestion and skip it.
                enqueue_suggestion(rec, "", skill_name=rec.get("skill_used"))
                continue
            suggestion, conf_out, fm_out = _extract_suggestion(resp.get("text") or "")
            if not suggestion:
                # The LLM said "no useful suggestion". Enqueue a
                # no-op so we don't re-suggest on every tick.
                enqueue_suggestion(rec, "", skill_name=rec.get("skill_used"))
                counters["skipped"] += 1
                continue
        # Override the rollout's confidence / failure_mode with what the
        # LLM (or stub) reported so the outer loop can rank by it.
        rec["failure_mode"] = fm_out
        try:
            (rec.setdefault("reward", {}))["confidence"] = float(conf_out)
        except Exception:
            pass
        result = enqueue_suggestion(rec, suggestion, skill_name=rec.get("skill_used"))
        if result.get("path"):
            counters["suggested"] += 1
            _total_suggestions += 1
        else:
            counters["errors"] += 1
            counters["last_error"] = "enqueue_failed"
    _last_tick_at = time.time()
    if counters["last_error"]:
        _last_error = counters["last_error"]
    _append_tick_log(counters)
    return counters


def _append_tick_log(counters: dict[str, Any]) -> None:
    """Append one line to logs/runs/inner_loop.log (cycle log = source of truth)."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(inner_log_path(), "a", encoding="utf-8") as fp:
            fp.write(
                f"{ts} inner_loop_tick "
                f"scanned={counters['scanned']} "
                f"suggested={counters['suggested']} "
                f"skipped={counters['skipped']} "
                f"errors={counters['errors']} "
                f"last_error={counters.get('last_error') or '-'}\n"
            )
    except Exception:
        # The cycle log is best-effort. A bug here must not crash the
        # background worker.
        pass


def get_inner_status() -> dict[str, Any]:
    """One-shot status snapshot for the dashboard / status endpoint."""
    cfg = _config()
    pending: dict[str, int] = {}
    try:
        for rec in list_pending_suggestions():
            skill = rec.get("skill") or "unknown"
            pending[skill] = pending.get(skill, 0) + 1
    except Exception:
        pass
    return {
        "enabled": cfg["enabled"],
        "last_tick_at": _last_tick_at,
        "total_ticks": _total_ticks,
        "total_suggestions": _total_suggestions,
        "pending_for_skills": pending,
        "last_error": _last_error,
        "interval_seconds": cfg["interval_seconds"],
        "max_age_seconds": cfg["max_age_seconds"],
        "min_rollout_confidence": cfg["min_rollout_confidence"],
        "llm_model": cfg["llm_model"],
    }


def reset_for_tests() -> None:
    """Drop all module-level state. Used by the smoke tests."""
    global _last_tick_at, _total_ticks, _total_suggestions, _last_error
    _last_tick_at = 0.0
    _total_ticks = 0
    _total_suggestions = 0
    _last_error = None
