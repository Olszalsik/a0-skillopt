"""
SkillOpt - adopt endpoint.

Route: POST /api/plugins/skillopt/adopt

Promotes the most recent staged proposal to /a0/usr/skills/<name>/SKILL.md
after a final validation-gate sanity check. The post-adopt hook writes
a one-line audit entry to logs/runs/.

v1.1.0 changes:
- Runs the proposal through the shared `validate_proposal()` gate
  (whitespace-normalised equality, mandatory example block, shrink
  ceiling, optional held-out enforcement) BEFORE copying. The v1.0
  endpoint accepted a 1904->1904 byte-identical 'qa' adoption.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


AUDIT_LOG = "adoptions.log"


def _append_audit(entry: dict) -> None:
    p = sleep_runner.runs_dir() / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _latest_sleep_log() -> Path | None:
    runs_root = sleep_runner.runs_dir()
    if not runs_root.is_dir():
        return None
    logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


class Adopt(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        staged = sleep_runner.find_staged_proposals()
        if not staged:
            return {
                "ok": False,
                "error": "no staged proposals — run a sleep cycle first",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        src = staged[0]
        skill_name = src.stem if src.suffix == ".md" else "unknown"
        target = sleep_runner.a0_skills_dir() / skill_name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        proposed = src.read_text(encoding="utf-8")
        current = ""
        if target.is_file():
            current = target.read_text(encoding="utf-8")

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
            skill_name=skill_name,  # v1.2.0: enables the A/B harness stage
        )

        entry = {
            "skill": skill_name,
            "source": str(src),
            "target": str(target),
            "proposed_size": len(proposed),
            "current_size": len(current),
            "passed": ok,
            "reason": reason,
            "held_out": held_out,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _append_audit(entry)

        if not ok:
            return {
                "ok": False,
                "error": f"validation gate rejected proposal: {reason}",
                "entry": entry,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        target.write_text(proposed, encoding="utf-8")
        return {
            "ok": True,
            **entry,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
