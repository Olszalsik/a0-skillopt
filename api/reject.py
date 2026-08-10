"""
SkillOpt - reject endpoint (v1.7.0, Solution C Phase C3).

Route: POST /api/plugins/skillopt/reject   body: {proposal_id, reason?}

Records a human REJECT decision on a staged proposal: appends a `rejected`
cycle_history entry + an adoptions.log row (mirroring adopt.py:_append_audit).
Does NOT delete the staged file — the staged proposal stays on disk so the
user can re-review or re-run; the reject is an audit decision, not a delete.
Idempotent: re-rejecting the same proposal_id just appends another row.
"""

import json
import time
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore

AUDIT_LOG = "adoptions.log"


def _append_audit(entry: dict) -> None:
    p = sleep_runner.runs_dir() / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Reject(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        proposal_id = (data.get("proposal_id") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not proposal_id:
            return {"ok": False, "error": "missing 'proposal_id'", "timestamp": _now()}
        # Resolve the skill name from the staged proposal stem, if present.
        skill = proposal_id
        try:
            for p in sleep_runner.find_staged_proposals():
                if p.stem == proposal_id:
                    skill = p.stem if p.suffix == ".md" else proposal_id
                    break
        except Exception:
            pass
        entry = {
            "skill": skill,
            "proposal_id": proposal_id,
            "decision": "rejected",
            "reason": reason or "user rejected via UI",
            "passed": False,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _append_audit(entry)
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            cycle_history.record_cycle_entry({
                "skill": skill,
                "outcome": "rejected",
                "outcome_detail": reason or "user rejected via UI",
                "proposal_id": proposal_id,
                "source": "reject_api",
            })
        except Exception:
            pass
        return {"ok": True, **entry, "timestamp": _now()}