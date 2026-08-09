"""
SkillOpt - post-adopt validation gate.

Runs after `skillopt_sleep verb=adopt` (or a direct API call to
/api/plugins/skillopt/adopt) and performs a final sanity check before
the proposal is considered final. This is the safety net that keeps
silent regressions from sneaking into the agent's skills.

v1.1.0 changes:
- Delegates to the shared `validate_proposal()` in sleep_runner
  (whitespace-normalised equality, mandatory example block, shrink
  ceiling, optional held-out enforcement). The v1.0 inline check
  accepted a 1904->1904 byte-identical 'qa' adoption.
"""

import json
import time
from pathlib import Path

from usr.plugins.skillopt.helpers import sleep_runner # type: ignore


PLUGIN_NAME = "skillopt"


def _read(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def _latest_sleep_log() -> Path | None:
    runs_root = sleep_runner.runs_dir()
    if not runs_root.is_dir():
        return None
    logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def execute(context: dict, **kwargs): # type: ignore[no-untyped-def]
    """Hook entry point. `context` is provided by the framework dispatcher.

    Expected context keys (best-effort):
    source: path to the staged proposal
    target: path to the current SKILL.md being replaced
    """
    src = context.get("source")
    dst = context.get("target")
    if not src or not dst:
        # Nothing to validate - silently exit (the framework may not have
        # provided paths if this hook was triggered by something other
        # than our adopt endpoint).
        return

    proposed = _read(src)
    current = _read(dst)
    cfg = sleep_runner.merged_config()
    last_log = _latest_sleep_log()
    held_out = sleep_runner.parse_held_out(last_log) if last_log else None
    ok, reason = sleep_runner.validate_proposal(
        proposed,
        current,
        min_chars=int(cfg.get("gate_min_chars", 200)),
        min_improvement_pp=float(cfg.get("gate_min_improvement_pp", 0.0)),
        max_shrink_ratio=float(cfg.get("gate_max_shrink_ratio", 0.5)),
        held_out=held_out,
    )

    audit_entry = {
        "hook": "_post_skill_adopt",
        "source": src,
        "target": dst,
        "passed": ok,
        "reason": reason,
        "proposed_size": len(proposed),
        "current_size": len(current),
        "held_out": held_out,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    audit_path = sleep_runner.runs_dir() / "post_adopt.log"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(audit_entry, ensure_ascii=False) + "\n")
