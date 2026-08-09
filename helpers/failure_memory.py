"""
SkillOpt failure memory - per-skill failure attribution for the next outer-loop prompt.

ROADMAP Day-4, item 6: "Failure memory (uses A0's existing vector store)".

Today when a Sleep cycle produces a bad proposal, we log it. But the next
cycle has no idea. Failure memory stores "proposal X failed because Y" and
injects it into the next prompt. The vector store already exists in A0
(`python.helpers.memory`); we just need a wrapper that scopes it to the
skill and adds the rollouts->outcome attribution.

Pluggable memory backend:
  The memory backend is three callables injected via `set_memory_fn(...)`.
  The default backend tries to import `python.helpers.memory` lazily; if
  the import fails (e.g. when running standalone tests outside A0, or
  when the A0 memory module signature differs), the module falls back
  to a local JSON file store at
  `<plugin>/logs/runs/failure_memory/<skill>/<memory_id>.json`. This
  keeps the feature working in production AND in this test runtime.
  The local store mimics the A0 contract: `save(text, area, metadata)`
  returns a memory_id; `load(query, threshold, limit, filter)` returns
  a list of `{id, text, area, metadata, similarity}`; `delete(ids)`
  returns the deletion count.

Failure mode loud (per ROADMAP principle 2):
  - If both backends are unavailable, `record_failure` returns
    `{ok: False, error: 'memory_api_unavailable'}` and the loop continues.
  - The cycle log captures every call so the dashboard can show the
    most recent error.

Backwards compat:
  - If `failure_memory_enabled=False` in config, the module is a no-op:
    `record_failure` returns `{ok: True, memory_id: None, skipped: True}`,
    `build_failure_context` returns "", and the auto-loop skips the
    injection entirely.
  - The two-loop contract is preserved: failure_memory only WRITES via
    `record_failure()`. The outer loop only READS via
    `build_failure_context()`. The module never writes to staging/ or
    SKILL.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable


PLUGIN_NAME = "skillopt"
log = logging.getLogger("skillopt.failure_memory")

DEFAULT_AREA = "skillopt_failures"
DEFAULT_PLUGIN_TAG = "skillopt"


# ----------------------------------------------------------------------- #
# Configuration
# ----------------------------------------------------------------------- #

DEFAULT_ENABLED = True
DEFAULT_MAX_ITEMS_IN_PROMPT = 5
DEFAULT_MIN_SIMILARITY = 0.0
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_MAX_PER_SKILL = 100


def _config() -> dict[str, Any]:
    """Read failure_memory_* keys from the merged config + env overrides."""
    try:
        from helpers.sleep_runner import merged_config  # type: ignore
        cfg = merged_config()
    except Exception:
        cfg = {}
    out = {
        "enabled": bool(cfg.get("failure_memory_enabled", DEFAULT_ENABLED)),
        "max_items_in_prompt": int(
            cfg.get("failure_memory_max_items_in_prompt", DEFAULT_MAX_ITEMS_IN_PROMPT)
        ),
        "min_similarity": float(
            cfg.get("failure_memory_min_similarity", DEFAULT_MIN_SIMILARITY)
        ),
        "max_age_seconds": int(
            float(cfg.get("failure_memory_max_age_days", DEFAULT_MAX_AGE_DAYS)) * 86400
        ),
        "max_per_skill": int(
            cfg.get("failure_memory_max_per_skill", DEFAULT_MAX_PER_SKILL)
        ),
    }
    if os.environ.get("SKILLOPT_FAILURE_MEMORY_ENABLED"):
        out["enabled"] = os.environ["SKILLOPT_FAILURE_MEMORY_ENABLED"].strip().lower() in (
            "1", "true", "yes", "on",
        )
    return out


# ----------------------------------------------------------------------- #
# Paths
# ----------------------------------------------------------------------- #

def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _runs_dir() -> Path:
    p = _plugin_root() / "logs" / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cycle_log_path() -> Path:
    p = _runs_dir() / "failure_memory.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _local_store_dir(skill_name: str | None = None) -> Path:
    """Directory for the local JSON file store fallback.

    Per-skill subdirectory: `logs/runs/failure_memory/<safe_skill>/`.
    The safe_skill keeps only [A-Za-z0-9_-] so the path is portable
    on every OS (Windows, macOS, Linux).
    """
    base = _runs_dir() / "failure_memory"
    if skill_name:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(skill_name))[:64] or "unknown"
        base = base / safe
    base.mkdir(parents=True, exist_ok=True)
    return base


# ----------------------------------------------------------------------- #
# Module-level state (counters, last run, backend injection)
# ----------------------------------------------------------------------- #

_last_record_at: float = 0.0
_last_load_at: float = 0.0
_last_forget_at: float = 0.0
_total_recorded: int = 0
_total_loaded: int = 0
_total_forgotten: int = 0
_total_skipped: int = 0
_last_error: str | None = None
_backend_kind: str = "unset"  # 'a0' | 'local' | 'injected' | 'unset'

# Memory backend callables. The wrapper of the A0 vector store API.
# Each takes the same args as `python.helpers.memory`:
#   save(text: str, area: str, metadata: dict) -> str  (returns memory_id)
#   load(query: str, threshold: float, limit: int, filter: str) -> list[dict]
#   delete(ids: list[str]) -> int  (returns deletion count)
SaveFn = Callable[[str, str, dict], str]
LoadFn = Callable[[str, float, int, str], list[dict]]
DeleteFn = Callable[[list[str]], int]
_save_fn: SaveFn | None = None
_load_fn: LoadFn | None = None
_delete_fn: DeleteFn | None = None


# ----------------------------------------------------------------------- #
# Backend resolution (lazy import of A0's memory module, local fallback)
# ----------------------------------------------------------------------- #

def _resolve_backend() -> tuple[SaveFn | None, LoadFn | None, DeleteFn | None, str]:
    """Return (save_fn, load_fn, delete_fn, kind) for the active backend.

    Order:
      1. Injected backend (set via set_memory_fn). Kind = 'injected'.
      2. A0's `python.helpers.memory` (lazy import). Kind = 'a0'.
      3. Local JSON file store fallback. Kind = 'local'.

    Returns the callables and a kind label so the cycle log can show
    which backend served the call. All three are None only when the
    module is disabled via config - in which case we don't need them.
    """
    if _save_fn is not None and _load_fn is not None and _delete_fn is not None:
        return _save_fn, _load_fn, _delete_fn, "injected"
    try:
        from python.helpers import memory as _a0_mem  # type: ignore  # noqa: E402
        # Probe the API surface so we fail fast if the install has a
        # different shape than the spec.
        for name in ("memory_save", "memory_load", "memory_delete"):
            if not hasattr(_a0_mem, name):
                raise AttributeError(f"python.helpers.memory missing {name}")
        def _a0_save(text: str, area: str, metadata: dict) -> str:
            return str(_a0_mem.memory_save(text=text, area=area, metadata=metadata))
        def _a0_load(query: str, threshold: float, limit: int, filter_: str) -> list[dict]:
            return list(_a0_mem.memory_load(
                query=query, threshold=threshold, limit=limit, filter=filter_,
            ))
        def _a0_delete(ids: list[str]) -> int:
            try:
                return int(_a0_mem.memory_delete(ids=ids))
            except TypeError:
                # Some A0 versions take positional args
                return int(_a0_mem.memory_delete(ids))
        return _a0_save, _a0_load, _a0_delete, "a0"
    except Exception as e:
        log.debug("[skillopt] A0 memory backend unavailable: %s", e)
    return _local_save, _local_load, _local_delete, "local"


# ----------------------------------------------------------------------- #
# Local JSON file store (the fallback when A0 memory isn't available)
# ----------------------------------------------------------------------- #

def _local_save(text: str, area: str, metadata: dict) -> str:
    """Write `text` + `metadata` to a per-skill JSON file. Return the id.

    The local store does NOT do real vector search. It indexes by skill
    (folder), then by ts desc. `memory_load` returns entries with
    `similarity=1.0` and `text`/`area`/`metadata` filled in. This is
    enough for `build_failure_context()` and the tests; the production
    semantic-search experience needs the real A0 backend.
    """
    skill = (metadata or {}).get("skill") or "unknown"
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(skill))[:64] or "unknown"
    mid = uuid.uuid4().hex
    payload = {
        "id": mid,
        "text": text or "",
        "area": area or DEFAULT_AREA,
        "metadata": dict(metadata or {}),
        "ts": time.time(),
    }
    target = _local_store_dir(skill) / f"{mid}.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return mid


def _local_load(query: str, threshold: float, limit: int, filter_: str) -> list[dict]:
    """Read the local store. Filters by `filter_` substring match.

    The local store is not a vector store: it does a substring match on
    `text + skill + failure_reason`. `similarity` is set to 1.0 for any
    match so the caller's threshold filter still passes them through
    when `min_similarity=0.0` (the v1.3.0 default).
    """
    needle_skill = ""
    area = DEFAULT_AREA
    m = re.search(r"area==\s*['\"]([^'\"]+)['\"]", filter_ or "")
    if m:
        area = m.group(1)
    m = re.search(r"skill==\s*['\"]([^'\"]+)['\"]", filter_ or "")
    if m:
        needle_skill = m.group(1).strip().lower()
    q = (query or "").lower()
    out: list[dict] = []
    if needle_skill:
        # Path-only: do NOT create the dir (mkdir side effect would
        # pollute the store every time we read a skill that has no
        # failures yet). The dir only gets created when record_failure
        # actually writes a file.
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", str(needle_skill))[:64] or "unknown"
        d = _runs_dir() / "failure_memory" / safe
        if not d.is_dir():
            return out
        dirs = [d]
    else:
        base = _runs_dir() / "failure_memory"
        if not base.is_dir():
            return out
        dirs = [d for d in sorted(base.iterdir()) if d.is_dir()]
    for d in dirs:
        for f in sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                rec = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            if area and rec.get("area") != area:
                continue
            if needle_skill:
                rec_skill = str((rec.get("metadata") or {}).get("skill") or "").lower()
                if rec_skill != needle_skill:
                    continue
            # Local store: when filter_ scopes by skill/area, trust the
            # structural match and skip the query-substring check (the
            # A0 backend would do real semantic search; the local store
            # is exact-match by folder). When filter_ is empty we still
            # do a loose substring match on the query as a usability win.
            if q and not needle_skill:
                hay = " ".join([
                    str(rec.get("text") or ""),
                    str((rec.get("metadata") or {}).get("skill") or ""),
                    str((rec.get("metadata") or {}).get("failure_reason") or ""),
                ]).lower()
                if q not in hay:
                    continue
            out.append({
                "id": rec.get("id") or f.stem,
                "text": rec.get("text") or "",
                "area": rec.get("area") or area,
                "metadata": rec.get("metadata") or {},
                "similarity": 1.0,
                "ts": rec.get("ts") or 0.0,
            })
            if len(out) >= max(1, int(limit)):
                return out
    return out


def _local_delete(ids: list[str]) -> int:
    """Delete the given ids from the local store. Return the count."""
    n = 0
    base = _runs_dir() / "failure_memory"
    for mid in ids or []:
        if not base.is_dir():
            continue
        for d in base.iterdir():
            if not d.is_dir():
                continue
            cand = d / f"{mid}.json"
            if cand.is_file():
                try:
                    cand.unlink()
                    n += 1
                except OSError:
                    pass
    return n


# ----------------------------------------------------------------------- #
# Public API: backend injection
# ----------------------------------------------------------------------- #

def set_memory_fn(
    save_fn: SaveFn | None,
    load_fn: LoadFn | None,
    delete_fn: DeleteFn | None,
) -> None:
    """Inject a custom memory backend. Used by tests; production calls
    this once at boot when SKILLOPT_MEMORY_BACKEND=custom is set.

    Pass None for any of the three to fall back to the default
    resolver (A0 first, then local JSON file store).
    """
    global _save_fn, _load_fn, _delete_fn, _backend_kind
    _save_fn = save_fn
    _load_fn = load_fn
    _delete_fn = delete_fn
    if save_fn is not None and load_fn is not None and delete_fn is not None:
        _backend_kind = "injected"
    else:
        _backend_kind = "unset"


def get_backend_kind() -> str:
    """Return the active backend kind ('a0' | 'local' | 'injected' | 'unset')."""
    if _save_fn is not None:
        return "injected"
    return _backend_kind if _backend_kind != "unset" else "unset"


def get_last_error() -> str | None:
    return _last_error


# ----------------------------------------------------------------------- #
# Public API: record / load / forget / status / context
# ----------------------------------------------------------------------- #

def record_failure(
    skill_name: str,
    proposal_summary: str,
    failure_reason: str,
    rollouts: list[str] | None = None,
    outcome: str | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """Persist a single failure attribution to the memory backend.

    Returns `{ok, memory_id, skill, ts}` on success, or
    `{ok: False, error: <reason>, detail: <str>}` on failure. Never raises.

    Failure modes (loud, not crashing):
      - failure_memory_enabled=False  -> {ok: True, memory_id: None, skipped: True}
      - backend unavailable           -> {ok: False, error: 'memory_api_unavailable'}
      - memory_save raised            -> {ok: False, error: 'memory_save_failed', detail: str(e)}
    """
    global _last_record_at, _total_recorded, _total_skipped, _last_error, _backend_kind
    cfg = _config()
    if not cfg["enabled"]:
        _total_skipped += 1
        _last_record_at = time.time()
        _append_cycle_log(
            action="record", skill=skill_name, count=0, last_error=None, skipped=True,
        )
        return {"ok": True, "memory_id": None, "skill": skill_name, "ts": _last_record_at, "skipped": True}
    save_fn, _load_fn_unused, _delete_fn_unused, kind = _resolve_backend()
    if save_fn is None:
        _last_error = "memory_api_unavailable"
        _append_cycle_log(
            action="record", skill=skill_name, count=0, last_error=_last_error,
        )
        return {"ok": False, "error": _last_error, "detail": "no backend"}
    _backend_kind = kind
    text = (
        f"skill={skill_name} reason={failure_reason} "
        f"summary={(proposal_summary or '')[:240]} outcome={outcome or 'rejected'}"
    )
    metadata = {
        "plugin": DEFAULT_PLUGIN_TAG,
        "skill": skill_name,
        "ts": float(ts) if ts is not None else time.time(),
        "rollouts": list(rollouts or []),
        "outcome": outcome or "rejected",
        "failure_reason": failure_reason or "",
        "proposal_summary": (proposal_summary or "")[:240],
    }
    try:
        memory_id = save_fn(text, DEFAULT_AREA, metadata)
    except Exception as e:
        _last_error = f"memory_save_failed: {type(e).__name__}: {e}"
        _last_record_at = time.time()
        _append_cycle_log(
            action="record", skill=skill_name, count=0, last_error=_last_error,
        )
        return {"ok": False, "error": "memory_save_failed", "detail": str(e)}
    _last_record_at = time.time()
    _last_error = None
    _total_recorded += 1
    _append_cycle_log(
        action="record",
        skill=skill_name,
        count=1,
        last_error=None,
        memory_id=str(memory_id),
        backend=kind,
    )
    return {
        "ok": True,
        "memory_id": str(memory_id),
        "skill": skill_name,
        "ts": metadata["ts"],
        "backend": kind,
    }


def load_failures(
    skill_name: str,
    since_ts: float = 0.0,
    limit: int = 20,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Return failures for `skill_name`, sorted by ts desc.

    The backend's vector search is the primary filter; we then apply
    a client-side `since_ts` cutoff. Result entries have the
    `{memory_id, skill, ts, proposal_summary, failure_reason, rollouts,
    outcome, similarity}` shape the spec requires.
    """
    global _last_load_at, _total_loaded, _last_error, _backend_kind
    cfg = _config()
    if not cfg["enabled"]:
        return []
    save_fn, load_fn, delete_fn, kind = _resolve_backend()
    if load_fn is None:
        _last_error = "memory_api_unavailable"
        return []
    _backend_kind = kind
    try:
        raw = load_fn(
            query=f"skillopt failure {skill_name}",
            threshold=float(min_similarity),
            limit=max(1, int(limit)),
            filter_=f"area=='{DEFAULT_AREA}' and skill=='{skill_name}'",
        )
    except Exception as e:
        _last_error = f"memory_load_failed: {type(e).__name__}: {e}"
        return []
    out: list[dict[str, Any]] = []
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        meta = r.get("metadata") or {}
        try:
            ts = float(meta.get("ts") or r.get("ts") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts < float(since_ts):
            continue
        out.append({
            "memory_id": r.get("id") or meta.get("memory_id") or "",
            "skill": meta.get("skill") or skill_name,
            "ts": ts,
            "proposal_summary": meta.get("proposal_summary") or "",
            "failure_reason": meta.get("failure_reason") or "",
            "rollouts": list(meta.get("rollouts") or []),
            "outcome": meta.get("outcome") or "rejected",
            "similarity": float(r.get("similarity") or 0.0),
        })
    out.sort(key=lambda x: x.get("ts") or 0.0, reverse=True)
    _last_load_at = time.time()
    _total_loaded += len(out)
    _last_error = None
    return out[: max(1, int(limit))]


def forget_failures(
    skill_name: str,
    before_ts: float | None = None,
) -> dict[str, Any]:
    """Delete failure entries for `skill_name`, optionally only older than `before_ts`.

    Returns `{ok, deleted_count, remaining_count}`. Never raises.
    """
    global _last_forget_at, _total_forgotten, _last_error, _backend_kind
    cfg = _config()
    if not cfg["enabled"]:
        return {"ok": True, "deleted_count": 0, "remaining_count": 0, "skipped": True}
    save_fn, load_fn, delete_fn, kind = _resolve_backend()
    if delete_fn is None or load_fn is None:
        _last_error = "memory_api_unavailable"
        return {"ok": False, "deleted_count": 0, "remaining_count": 0, "error": _last_error}
    _backend_kind = kind
    existing = load_failures(skill_name, since_ts=0.0, limit=cfg["max_per_skill"])
    to_delete: list[str] = []
    for r in existing:
        if before_ts is not None and float(r.get("ts") or 0.0) >= float(before_ts):
            continue
        if r.get("memory_id"):
            to_delete.append(r["memory_id"])
    if not to_delete:
        _last_forget_at = time.time()
        return {"ok": True, "deleted_count": 0, "remaining_count": len(existing)}
    try:
        n = int(delete_fn(to_delete))
    except Exception as e:
        _last_error = f"memory_delete_failed: {type(e).__name__}: {e}"
        return {"ok": False, "deleted_count": 0, "remaining_count": len(existing), "error": _last_error}
    _last_forget_at = time.time()
    _total_forgotten += int(n)
    _last_error = None
    remaining = max(0, len(existing) - int(n))
    _append_cycle_log(
        action="forget",
        skill=skill_name,
        count=int(n),
        last_error=None,
        remaining=remaining,
        backend=kind,
    )
    return {"ok": True, "deleted_count": int(n), "remaining_count": remaining}


def get_failure_status(skill_name: str) -> dict[str, Any]:
    """Return a one-shot status block for `skill_name`.

    Used by the dashboard and the per-skill status endpoint.
    """
    entries = load_failures(skill_name, since_ts=0.0, limit=DEFAULT_MAX_PER_SKILL)
    total = len(entries)
    oldest_ts = min((e["ts"] for e in entries), default=0.0)
    newest_ts = max((e["ts"] for e in entries), default=0.0)
    cfg = _config()
    recent = [e for e in entries if e["ts"] >= (time.time() - 86400)]
    rate = (len(recent) / 24.0) if total else 0.0
    return {
        "skill": skill_name,
        "total_failures": total,
        "oldest_ts": oldest_ts,
        "newest_ts": newest_ts,
        "recent_failure_rate": round(rate, 4),
        "enabled": cfg["enabled"],
        "backend": get_backend_kind(),
        "last_error": _last_error,
    }


def build_failure_context(
    skill_name: str,
    max_items: int | None = None,
) -> str:
    """Return a `[FAILURE MEMORY]` block to inject into the next outer-loop prompt.

    Empty string when no failures exist (caller should skip the block).
    Each entry is trimmed to ~200 chars so the prompt doesn't bloat.
    """
    cfg = _config()
    if not cfg["enabled"]:
        return ""
    cap = int(max_items) if max_items is not None else int(cfg["max_items_in_prompt"])
    if cap <= 0:
        return ""
    entries = load_failures(
        skill_name, since_ts=0.0, limit=cap, min_similarity=cfg["min_similarity"],
    )
    if not entries:
        return ""
    lines: list[str] = []
    lines.append(f"[FAILURE MEMORY \u2014 {len(entries)} recent failure(s) for skill {skill_name!r}]")
    for e in entries:
        ts = float(e.get("ts") or 0.0)
        date = time.strftime("%Y-%m-%d", time.localtime(ts)) if ts > 0 else "unknown"
        summary = str(e.get("proposal_summary") or "").strip()
        reason = str(e.get("failure_reason") or "").strip()
        outcome = str(e.get("outcome") or "rejected").strip()
        if len(summary) > 200:
            summary = summary[:197] + "..."
        if len(reason) > 200:
            reason = reason[:197] + "..."
        lines.append(
            f"- {date}: Proposal {summary!r} failed because {reason}. "
            f"Outcome: {outcome}."
        )
    lines.append("[END FAILURE MEMORY]")
    return "\n".join(lines)


# ----------------------------------------------------------------------- #
# Public API: status snapshot (consumed by sleep_runner.get_status_snapshot)
# ----------------------------------------------------------------------- #

def get_status_block() -> dict[str, Any]:
    """One-shot status block for the dashboard / status endpoint."""
    cfg = _config()
    per_skill: dict[str, dict[str, Any]] = {}
    try:
        from helpers.sleep_runner import a0_skills_dir  # type: ignore
        skills_root = a0_skills_dir()
    except Exception:
        skills_root = None
    candidates: set[str] = set()
    base = _runs_dir() / "failure_memory"
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir() and any(d.glob("*.json")):
                # safe_skill back to original skill name: the folder
                # uses re.sub to sanitise, but for the dashboard we
                # just need the dir name (we re-load with that name).
                candidates.add(d.name)
    for s in sorted(candidates):
        per_skill[s] = get_failure_status(s)
    return {
        "enabled": cfg["enabled"],
        "backend": get_backend_kind(),
        "last_error": _last_error,
        "totals": {
            "recorded": _total_recorded,
            "loaded": _total_loaded,
            "forgotten": _total_forgotten,
            "skipped": _total_skipped,
        },
        "last_record_at": _last_record_at,
        "last_load_at": _last_load_at,
        "last_forget_at": _last_forget_at,
        "per_skill": per_skill,
    }


def cleanup_old_failures(skill_name: str, max_age_seconds: int | None = None) -> dict:
    """Background cleanup: forget failures older than `max_age_seconds`.

    The auto-loop calls this every 6h per skill. Returns the same shape
    as `forget_failures()`. Never raises.
    """
    cfg = _config()
    age = int(max_age_seconds) if max_age_seconds is not None else int(cfg["max_age_seconds"])
    if age <= 0:
        return {"ok": True, "deleted_count": 0, "remaining_count": 0, "skipped": True}
    return forget_failures(skill_name, before_ts=time.time() - age)


# ----------------------------------------------------------------------- #
# Cycle log (source of truth - per ROADMAP engineering principle 5)
# ----------------------------------------------------------------------- #

def _append_cycle_log(
    *,
    action: str,
    skill: str,
    count: int,
    last_error: str | None,
    skipped: bool = False,
    memory_id: str | None = None,
    remaining: int | None = None,
    backend: str | None = None,
) -> None:
    """Append one line to logs/runs/failure_memory.log. Best-effort."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        parts = [
            ts,
            f"action={action}",
            f"skill={skill!r}",
            f"count={count}",
        ]
        if memory_id:
            parts.append(f"memory_id={memory_id}")
        if remaining is not None:
            parts.append(f"remaining={remaining}")
        if backend:
            parts.append(f"backend={backend}")
        if skipped:
            parts.append("skipped=true")
        if last_error:
            parts.append(f"last_error={last_error}")
        with open(_cycle_log_path(), "a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------- #
# Test reset
# ----------------------------------------------------------------------- #

def reset_for_tests() -> None:
    """Drop all module-level state. Used by the smoke tests."""
    global _last_record_at, _last_load_at, _last_forget_at
    global _total_recorded, _total_loaded, _total_forgotten, _total_skipped
    global _last_error, _backend_kind
    global _save_fn, _load_fn, _delete_fn
    _last_record_at = 0.0
    _last_load_at = 0.0
    _last_forget_at = 0.0
    _total_recorded = 0
    _total_loaded = 0
    _total_forgotten = 0
    _total_skipped = 0
    _last_error = None
    _backend_kind = "unset"
    _save_fn = None
    _load_fn = None
    _delete_fn = None
