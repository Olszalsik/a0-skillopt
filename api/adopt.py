"""
SkillOpt - adopt endpoint.

Route: POST /api/plugins/skillopt/adopt

Promotes a staged proposal to /a0/usr/skills/<name>/SKILL.md after a final
validation-gate sanity check. The post-adopt hook writes a one-line audit
entry to logs/runs/.

v1.1.0:
- Runs the proposal through the shared `validate_proposal()` gate
  (whitespace-normalised equality, mandatory example block, shrink
  ceiling, optional held-out enforcement) BEFORE copying. The v1.0
  endpoint accepted a 1904->1904 byte-identical 'qa' adoption.

v1.7.0 (Solution C, Phase C3):
- Accepts an optional `proposal_id` in the body. When present, the
  staged proposal whose filename stem matches `proposal_id` is adopted
  (instead of always the most-recent `staged[0]`). Falls back to
  `staged[0]` when `proposal_id` is absent or matches nothing — keeps the
  old "Adopt latest" behaviour backwards-compatible.
- Writes a whole-file `_default` snapshot via
  `fragment_store.snapshot_default(skill_name, current)` BEFORE
  overwriting the SKILL.md, so the adopt is reversible via the new
  /rollback endpoint (or the existing fragment-store restore). The v1.2.0
  `write_fragment` `_default` branch overwrote with NO snapshot.
- Records an `adopted` cycle_history entry so the dashboard's per-cycle
  audit reflects the human-in-the-loop adopt.
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


def _resolve_staged(proposal_id: str | None):
    """Resolve a staged proposal path. When `proposal_id` is given, find
    the staged file whose stem matches it; otherwise return the most
    recent staged proposal. Returns (path, skill_name) or (None, None)."""
    staged = sleep_runner.find_staged_proposals()
    if not staged:
        return None, None
    staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if proposal_id:
        pid = str(proposal_id).strip()
        for p in staged:
            if p.stem == pid:
                skill_name = p.stem if p.suffix == ".md" else "unknown"
                return p, skill_name
        # proposal_id didn't match any staged stem -> fall through to latest
    src = staged[0]
    skill_name = src.stem if src.suffix == ".md" else "unknown"
    return src, skill_name


class Adopt(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        proposal_id = data.get("proposal_id")
        src, skill_name = _resolve_staged(proposal_id)
        if src is None:
            return {
                "ok": False,
                "error": "no staged proposals — run a sleep cycle first",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
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
            "proposal_id": src.stem,
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

        # v1.7.0: snapshot the pre-adopt whole-file bytes so the adopt is
        # reversible via /rollback. A snapshot failure is logged but does
        # NOT block the adopt — the gate passed, the user wants the edit.
        snapshot = {"ok": False, "error": "snapshot_default not called"}
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
            snapshot = fragment_store.snapshot_default(skill_name, current)
        except Exception as e:
            snapshot = {"ok": False, "error": f"snapshot_default raised: {e}"}

        target.write_text(proposed, encoding="utf-8")

        # v1.7.0: record an `adopted` cycle_history entry for the
        # dashboard's human-in-the-loop audit trail.
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            cycle_history.record_cycle_entry({
                "skill": skill_name,
                "outcome": "adopted",
                "outcome_detail": reason,
                "proposal_id": src.stem,
                "proposed_size": len(proposed),
                "current_size": len(current),
                "source": "adopt_api",
            })
        except Exception:
            pass

        return {
            "ok": True,
            **entry,
            "snapshot": snapshot,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }