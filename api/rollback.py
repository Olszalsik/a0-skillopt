"""
SkillOpt - rollback endpoint (v1.7.0, Solution C Phase C3).

Route: POST /api/plugins/skillopt/rollback   body: {skill}

Restores the most recent whole-file `_default` snapshot for <skill> to
<a0>/usr/skills/<skill>/SKILL.md, reversing a prior /adopt. Backed by
`fragment_store.restore_default_snapshot`, which snapshots the current
(pre-rollback) bytes first so the rollback is itself reversible.

This is the whole-file rollback; the existing /fragments/rollback endpoint
handles per-fragment rollback for fragment-aware skills.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Rollback(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        if not skill:
            return {"ok": False, "error": "missing 'skill'", "timestamp": _now()}
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
            result = fragment_store.restore_default_snapshot(skill)
            if result.get("ok"):
                # Audit the rollback so the cycle history is honest about
                # the live skill's provenance.
                try:
                    from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
                    cycle_history.record_cycle_entry({
                        "skill": skill,
                        "outcome": "rolled_back",
                        "outcome_detail": f"restored from {result.get('restored_from', '')}",
                        "source": "rollback_api",
                    })
                except Exception:
                    pass
            return {"ok": bool(result.get("ok")), **result, "timestamp": _now()}
        except Exception as e:
            return {"ok": False, "error": f"rollback raised: {e}",
                    "timestamp": _now()}