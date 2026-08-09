"""Bridge: A0 rollouts -> plugin-local Claude Code history cache.

The SkillOpt Sleep engine's `harvest` verb reads from:
* <cache>/history.jsonl  — list of user prompts
* <cache>/projects/<slug>/<sessionId>.jsonl  — session transcripts

By default we now write to a PLUGIN-LOCAL cache at
`<plugin_root>/.cache/claude_code/` instead of polluting the host's
`~/.claude/` directory. The Sleep subprocess is launched with
`cwd=staging_dir()` and an env var `SKILLOPT_TRANSCRIPT_DIR` so the
engine picks up our cache without touching the user's home dir.

v1.0 (legacy) bridge wrote to `~/.claude/*`. We still support that
mode (set SKILLOPT_BRIDGE_TO_HOST=1) for users who want the engine
to share sessions with their real Claude Code install, but the
default is now plugin-local.

The bridge also handles the CWD-slug matching issue: the Sleep
engine's `harvest` filters sessions by `invoked_project` (the CWD
slug at engine start time). Since we launch the engine with
`cwd=staging_dir()`, the slug is derived from the staging path -
not the user's home dir. The bridge derives the same slug.

Idempotent: a rollout's id is recorded in .bridge_index.json so
re-running the bridge doesn't duplicate records.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
import uuid
from pathlib import Path

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _bridge_root() -> Path:
    """Plugin-local Claude Code history cache (the default)."""
    override = os.environ.get("SKILLOPT_TRANSCRIPT_DIR")
    if override:
        return Path(override)
    if os.environ.get("SKILLOPT_BRIDGE_TO_HOST", "").lower() in ("1", "true", "yes"):
        return Path(os.path.expanduser("~")) / ".claude"
    return sleep_runner.plugin_root() / ".cache" / "claude_code"


CLAUDE_HISTORY_PATH = None  # resolved at call time via _bridge_root()
CLAUDE_PROJECTS_DIR = None


def _project_slug(project_path: str) -> str:
    """Claude Code derives a project slug from the absolute project path.

    The slug replaces non-alphanumeric chars with hyphens. We mimic
    that so the Sleep engine can find the matching session files.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", project_path)


def _iso_ms(ts_ms: int) -> str:
    """epoch ms -> ISO 8601 with millisecond precision (Claude Code style)."""
    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc).isoformat()


def _rollout_to_records(rollout: dict) -> tuple:
    """Map an A0 rollout into (history_record, session_records).

    history_record -> one line for <cache>/history.jsonl
    session_records -> list of user/assistant records for the
    project's sessionId.jsonl file
    """
    task = (rollout.get("task") or rollout.get("task_type") or "").strip()
    outcome = (rollout.get("outcome") or "").strip()
    skill = (rollout.get("skill_used") or rollout.get("task_type") or "").strip()
    trajectory = rollout.get("trajectory") or rollout.get("steps") or []
    rollout_id = rollout.get("id") or uuid.uuid4().hex[:12]

    # Build the user prompt — enrich so the Sleep engine can mine patterns
    user_parts = [task]
    if outcome:
        user_parts.append("\n[outcome: " + outcome + "]")
    if skill:
        user_parts.append("\n[skill: " + skill + "]")
    if trajectory:
        user_parts.append("\n[steps: " + " | ".join(str(s) for s in trajectory[:5]) + "]")
    user_text = " ".join(user_parts)

    # Build the assistant response — a synthetic completion reflecting the outcome
    if outcome == "success":
        assistant_text = (
            "Completed the task using the " + skill + " skill. "
            "Outcome: success. Steps taken: "
            + ", ".join(str(s) for s in trajectory[:5])
            + ". The skill was effective and no corrections were needed."
        )
    elif outcome == "partial":
        assistant_text = (
            "Completed the task partially using the " + skill + " skill. "
            "Some steps were skipped or needed iteration. "
            "Steps: " + ", ".join(str(s) for s in trajectory[:5])
            + ". Consider adding more concrete examples or a validation step."
        )
    else:
        assistant_text = (
            "Attempted the task with the " + skill + " skill but did not complete it. "
            "Steps: " + ", ".join(str(s) for s in trajectory[:5])
            + ". The skill may need clearer instructions, better edge-case handling, "
            "or a fallback when the task is too large."
        )

    ts_raw = rollout.get("ts") or rollout.get("timestamp")
    if isinstance(ts_raw, (int, float)):
        ts_ms = int(ts_raw * 1000) if ts_raw < 1e12 else int(ts_raw)
    else:
        ts_ms = int(time.time() * 1000)
    ts_iso = _iso_ms(ts_ms)

    # CRITICAL: project_path must match the CWD the Sleep engine is
    # launched from (we set cwd=staging_dir() in launch_sleep_subprocess).
    # If they don't match, the engine's `harvest` verb filters our
    # sessions out by invoked_project.
    project_path = str(sleep_runner.staging_dir())
    session_id = "a0-" + str(rollout_id)

    history_record = {
        "display": task or (skill + " task"),
        "pastedContents": {},
        "timestamp": ts_ms,
        "project": project_path,
    }

    session_records = [
        {
            "type": "user",
            "message": {"role": "user", "content": user_text},
            "cwd": project_path,
            "gitBranch": "",
            "timestamp": ts_iso,
            "sessionId": session_id,
            "version": "1.0",
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": assistant_text},
            "cwd": project_path,
            "gitBranch": "",
            "timestamp": _iso_ms(ts_ms + 5000),
            "sessionId": session_id,
            "version": "1.0",
        },
    ]

    return history_record, session_records, session_id, project_path


def bridge_rollouts_to_claude_history() -> dict:
    """Convert A0 rollouts into Claude Code history + session files.

    Reads every *.json in logs/rollouts/, maps each to a Claude Code
    history record AND a session transcript, and writes both into
    the plugin-local cache (or the host's ~/.claude/ if the
    SKILLOPT_BRIDGE_TO_HOST env var is set).

    Idempotent via .bridge_index.json. Returns a small status dict.
    """
    root = _bridge_root()
    history_path = root / "history.jsonl"
    projects_dir = root / "projects"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    projects_dir.mkdir(parents=True, exist_ok=True)

    bridge_index_path = sleep_runner.runs_dir() / ".bridge_index.json"
    index: set = set()
    if bridge_index_path.is_file():
        try:
            index = set(json.loads(bridge_index_path.read_text(encoding="utf-8")))
        except Exception:
            index = set()

    rollouts_d = sleep_runner.rollouts_dir()
    new_history: list = []
    new_sessions: dict = {}  # session_path -> list of records
    skipped = 0
    for rp in rollouts_d.glob("*.json"):
        rid = rp.stem
        if rid in index:
            skipped += 1
            continue
        try:
            rollout = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        history_record, session_records, session_id, project_path = (
            _rollout_to_records(rollout)
        )
        new_history.append(history_record)
        slug = _project_slug(project_path)
        session_path = projects_dir / slug / (session_id + ".jsonl")
        session_path.parent.mkdir(parents=True, exist_ok=True)
        new_sessions.setdefault(str(session_path), []).extend(session_records)
        index.add(rid)

    if new_history:
        with open(history_path, "a", encoding="utf-8") as f:
            for rec in new_history:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for session_path, records in new_sessions.items():
            with open(session_path, "a", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    bridge_index_path.write_text(
        json.dumps(sorted(index), ensure_ascii=False), encoding="utf-8"
    )
    return {
        "rollouts_written": len(new_history),
        "rollouts_skipped_already_bridged": skipped,
        "history_path": str(history_path),
        "sessions_written": len(new_sessions),
        "total_in_index": len(index),
        "bridge_root": str(root),
        "is_plugin_local": str(root).startswith(str(sleep_runner.plugin_root())),
    }
