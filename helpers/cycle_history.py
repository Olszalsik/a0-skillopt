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
import threading
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
    """Is the cycle_history writer switched on? Default ON.

    v1.8.37 (audit F12). This read its config from
    `usr.plugins.skillopt.helpers.config_loader`, and **there is no
    `config_loader.py` in this tree** - so the import raised on every call,
    the `except` returned `True`, and `cycle_history_enabled: false` did
    nothing. The key has been dead since it was declared.
    """
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except ImportError:
        try:
            from helpers import sleep_runner  # type: ignore
        except Exception:
            return True
    try:
        return bool(sleep_runner.merged_config().get("cycle_history_enabled",
                                                      True))
    except Exception:
        return True


def _max_entries() -> int:
    """Resolve the cycle_history_max_entries cap (0 = no compaction).

    v1.8.37 (audit F12): same dead `config_loader` import as `_enabled()` -
    `cycle_history_max_entries` was permanently pinned at 500.
    """
    try:
        from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
    except ImportError:
        try:
            from helpers import sleep_runner  # type: ignore
        except Exception:
            return 500
    try:
        raw = sleep_runner.merged_config().get("cycle_history_max_entries", 500)
        # `0` means "no compaction" per this function's own contract, so it
        # must NOT be collapsed by an `or 500` - that idiom turns a
        # deliberately-disabled compaction back on, which is the opposite of
        # what the operator asked for. Only a genuinely absent or None value
        # takes the default.
        if raw is None:
            raw = 500
        return max(0, int(raw))
    except Exception:
        return 500


_CYCLE_LOCK = threading.RLock()


def _compact_jsonl(max_entries: int) -> dict:
    """v1.6.0 compaction: if the JSONL exceeds max_entries lines, move the
    oldest (total - max_entries) lines to a cold `cycle_history.archive.jsonl`
    so the hot file stays bounded. The archive is append-only and never
    re-compacted. Best-effort: a failure returns {ok: False, error} and
    never raises (compaction must not break recording).

    v1.8.48 (audit G3): this rewrites the APPEND TARGET wholesale, and five
    writers append to it (the drain thread plus three HTTP handlers) with no
    lock - the module imported no `threading` at all. A row appended between
    the read and the `os.replace` was dropped from BOTH files: silently, and
    unrecoverably. Two concurrent compactions also duplicated the same cold
    block into the archive. This is the failure the approvals index was
    created to prevent, reintroduced in a second file.
    """
    out = {"ok": True, "compacted": 0, "archived": 0}
    if max_entries <= 0:
        return out
    with _CYCLE_LOCK:
        jsonl = _jsonl_path()
        if not jsonl.is_file():
            return out
        try:
            with open(jsonl, "r", encoding="utf-8") as f:
                lines = f.readlines()
            total = len(lines)
            if total <= max_entries:
                return out
            overflow = total - max_entries
            cold = lines[:overflow]
            hot = lines[overflow:]
            archive = _runs_dir() / "cycle_history.archive.jsonl"
            with open(archive, "a", encoding="utf-8") as f:
                f.writelines(cold)
            # Atomic-ish rewrite of the hot file with the retained entries.
            tmp = jsonl.with_suffix(jsonl.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(hot)
            os.replace(tmp, jsonl)
            out["compacted"] = overflow
            out["archived"] = len(cold)
        except Exception as e:
            out = {"ok": False, "error": f"compaction failed: {e}"}
    return out


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
    entry.setdefault("version", "1.6.1")
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
        # v1.8.48 (audit G3): the append and the compaction that rewrites
        # this file must be mutually exclusive, or a row can be lost between
        # the compaction's read and its `os.replace`.
        with _CYCLE_LOCK:
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
        # v1.6.0: enforce cycle_history_max_entries by rotating overflow
        # to a cold archive. Best-effort; never raises (a compaction bug
        # must not break the recording above, which already succeeded).
        try:
            compaction = _compact_jsonl(_max_entries())
        except Exception:
            compaction = {"ok": False, "error": "compaction raised"}
        # Approximate the 1-based line number (post-compaction)
        with open(jsonl, "rb") as fr:
            line_no = sum(1 for _ in fr)
        return {
            "ok": True,
            "cycle_id": entry["cycle_id"],
            "line_no": line_no,
            "path": str(jsonl),
            "compaction": compaction,
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
        "archive_path": str(_runs_dir() / "cycle_history.archive.jsonl"),
        "total_entries": 0,
        "archived_entries": 0,
        "max_entries": _max_entries(),
        "file_size_bytes": 0,
        "last_cycle_id": None,
        "last_cycle_ts": None,
        "last_outcome": None,
    }
    # Count archived entries (cold file), best-effort.
    archive = _runs_dir() / "cycle_history.archive.jsonl"
    if archive.is_file():
        try:
            with open(archive, "r", encoding="utf-8") as f:
                out["archived_entries"] = sum(1 for _ in f)
        except Exception:
            pass
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
