"""
SkillOpt - staged proposals endpoint (v1.7.0, Solution C Phase C3).

Route: POST /api/plugins/skillopt/staged

Lists the staged proposals waiting for a human-in-the-loop decision,
enriched with the gate evidence the user needs to Approve/Reject:
skill, proposal_id, size, mtime, and the most recent cycle_history
entry for that skill (gate reason / lift / outcome / held-out).

The dashboard's "Staged proposals" section (config.html) renders this so
the user can review each proposal before promoting it via /adopt, or
reject it via /reject, or roll back a prior adopt via /rollback.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Staged(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        try:
            staged = sleep_runner.find_staged_proposals()
        except Exception as e:
            return {"ok": False, "error": f"find_staged_proposals raised: {e}",
                    "timestamp": _now()}
        staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        # Pull the most recent cycle_history entry per skill so each card
        # shows the gate reason / lift that produced the staged proposal.
        recent_by_skill: dict[str, dict] = {}
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            for entry in cycle_history.read_cycle_history(limit=200):
                skill = entry.get("skill")
                if skill and skill not in recent_by_skill:
                    recent_by_skill[skill] = entry
        except Exception:
            pass
        proposals = []
        for p in staged:
            skill = p.stem if p.suffix == ".md" else "unknown"
            try:
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            except Exception:
                size, mtime = 0, 0
            cyc = recent_by_skill.get(skill, {})
            proposals.append({
                "skill": skill,
                "proposal_id": p.stem,
                "source": str(p),
                "size": size,
                "mtime": mtime,
                "gate_reason": cyc.get("outcome_detail", ""),
                "lift_pp": cyc.get("lift_pp"),
                "n_held_out": cyc.get("n_held_out"),
                "last_outcome": cyc.get("outcome", ""),
                "diff_summary": f"{size} bytes",
            })
        return {
            "ok": True,
            "proposals": proposals,
            "count": len(proposals),
            "timestamp": _now(),
        }