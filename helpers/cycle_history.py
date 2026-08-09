"""
SkillOpt - cycle history helper (v1.4.0-Dev, Day-5 item 7).

The cycle history is the user-facing record of every Sleep cycle the
auto-loop ran. Where the v1.3.0 adoptions.log is a one-line-per-event
audit trail, cycle_history.jsonl is the rich record — every cycle gets
a JSON object with the outcome, gate decisions, A/B harness result,
reward model prediction, inner-loop + failure-memory context, budget
impact, and links to the rollouts, the staged proposal, and the
audit-log line that points back to this cycle.

CRITICAL RULES (do not break):
1. Append-only JSONL — one complete JSON object per line. Partial
   writes are handled by skipping lines that fail json.loads on read.
2. Plugin-local only — files at <plugin>/logs/runs/cycle_history.{jsonl,log}
3. Loud-not-crash — every function returns structured {ok, error} on failure
4. Backwards-compat — existing logs/runs/adoptions.log writes continue unchanged
5. Cycle log companion — every record_cycle_entry() also appends one line
   to cycle_history.log, same pattern as fragments.log / ab_harness.log / etc.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _runs_dir() -> Path:
    """Locate <plugin>/logs/runs/. Lazy import keeps the helper
    importable in isolation (smoke runner, test harnesses)."""
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
        p = sleep_runner.runs_dir()
    except Exception:
        here = Path(__file__).resolve()
        for ancestor in [here] + list(here.parents):
            candidate = ancestor / "logs" / "runs"
            if candidate.is_dir():
                p = candidate
                break
        else:
            p = here.parent / "logs" / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _jsonl_path() -> Path:
    return _runs_dir() / "cycle_history.jsonl"


def _log_path() -> Path:
    return _runs_dir() / "cycle_history.log"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _enabled() -> bool:
    try:
        from usr.plugins.skillopt.helpers import config_loader  # type: ignore
        cfg = config_loader.load_config()
        return bool(cfg.get("cycle_history_enabled", True))
    except Exception:
        return True


def record_cycle_entry(cycle_entry: dict) -> dict:
    """Append one cycle entry to logs/runs/cycle_history.jsonl.

    Fills in `cycle_id`, `ts`, `version` if missing. Returns
    `{ok, cycle_id, line_no, path}`. If disabled, returns
    `{ok: False, skipped: True}`.
    """
    if not _enabled():
        return {"ok": False, "skipped": True, "reason": "cycle_history disabled by config"}
    if not isinstance(cycle_entry, dict):
        return {"ok": False, "error": f"cycle_entry must be a dict, got {type(cycle_entry).__name__}"}

    entry = dict(cycle_entry)
    entry.setdefault("cycle_id", _short_id())
    entry.setdefault("ts", _now_iso())
    entry.setdefault("version", "1.4.0-dev")
    entry.setdefault("skill", "")
    entry.setdefault("outcome", "unknown")
    entry.setdefault("outcome_detail", "")
    entry.setdefault("gate_reasons", [])
    entry.setdefault("gate_stages_passed", [])
    entry.setdefault("llm_calls", 0)
    entry.setdefault("runtime_seconds", 0.0)

    jsonl = _jsonl_path()
    log = _log_path()
    try:
        line = json.dumps(entry, ensure_ascii=False, sort_keys=False)
        with open(jsonl, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        with open(log, "a", encoding="utf-8") as f:
            f.write(
                f"{entry['ts']}\t{entry['cycle_id']}\t"
                f"skill={entry['skill']}\toutcome={entry['outcome']}\t"
                f"runtime_s={entry['runtime_seconds']}\n"
            )
        # Approximate the 1-based line number
        with open(jsonl, "rb") as fr:
            line_no = sum(1 for _ in fr)
        return {
            "ok": True,
            "cycle_id": entry["cycle_id"],
            "line_no": line_no,
            "path": str(jsonl),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"cycle_history write failed: {e}",
            "exception_type": type(e).__name__,
        }


def read_cycle_history(
    limit: int = 50,
    skill: str | None = None,
    since_ts: str | None = None,
    outcome: str | None = None,
) -> list[dict]:
    """Return the most recent N cycle entries, newest-first.

    Filters: skill, outcome (exact match), since_ts (entry.ts >= since_ts).
    Malformed lines skipped silently (partial-write recovery).
    Returns [] if the file is missing or has no matching entries.
    """
    jsonl = _jsonl_path()
    if not jsonl.is_file():
        return []
    out: list[dict] = []
    try:
        with open(jsonl, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if skill and entry.get("skill") != skill:
                    continue
                if outcome and entry.get("outcome") != outcome:
                    continue
                if since_ts and (entry.get("ts") or "") < since_ts:
                    continue
                out.append(entry)
    except Exception:
        return []
    out.reverse()
    return out[: max(1, int(limit))]


def read_cycle(cycle_id: str) -> dict | None:
    """Read a single cycle entry by its cycle_id. None if not found."""
    if not cycle_id:
        return None
    jsonl = _jsonl_path()
    if not jsonl.is_file():
        return None
    try:
        with open(jsonl, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("cycle_id") == cycle_id:
                    return entry
    except Exception:
        return None
    return None


def get_history_status() -> dict:
    """Status block for get_status_snapshot()."""
    jsonl = _jsonl_path()
    log = _log_path()
    enabled = _enabled()
    out: dict[str, Any] = {
        "enabled": enabled,
        "file_path": str(jsonl),
        "log_path": str(log),
        "total_entries": 0,
        "file_size_bytes": 0,
        "last_cycle_id": None,
        "last_cycle_ts": None,
        "last_outcome": None,
    }
    if not jsonl.is_file():
        return out
    try:
        out["file_size_bytes"] = jsonl.stat().st_size
        last_entry = None
        total = 0
        with open(jsonl, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                total += 1
                last_entry = entry
        out["total_entries"] = total
        if last_entry:
            out["last_cycle_id"] = last_entry.get("cycle_id")
            out["last_cycle_ts"] = last_entry.get("ts")
            out["last_outcome"] = last_entry.get("outcome")
    except Exception as e:
        out["read_error"] = str(e)
    return out


def reset_for_tests() -> None:
    """Wipe both files. Used by smoke tests."""
    for p in (_jsonl_path(), _log_path()):
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass


__all__ = [
    "record_cycle_entry",
    "read_cycle_history",
    "read_cycle",
    "get_history_status",
    "reset_for_tests",
]
