"""
SkillOpt - governance approve endpoint (v1.7.0, Solution C Phase C4).

Route: POST /api/plugins/skillopt/governance_approve
  body: {skill, approved, decided_by?}

Records a human approval/rejection via `governance.mark_human_decision`
(the same row `_human_approved` reads to pass gate step 7). When
`approved=True` it also touches `.skillopt.optin` (idempotent) so the
opt-in marker and the approval ledger agree — a skill that was
auto-opted-in (markers created, but `require_human_approval_pending`)
becomes fully eligible after this call.

`approved=False` records a deny (the skill stays
`require_human_approval_pending`). Idempotent: re-approving just appends
another `human_decision` row (the most recent wins).
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class GovernanceApprove(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        if not skill:
            return {"ok": False, "error": "missing 'skill'", "timestamp": _now()}
        approved = bool(data.get("approved", True))
        decided_by = (data.get("decided_by") or "user").strip() or "user"
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
            res = governance.mark_human_decision(skill, approved, decided_by)
            if approved:
                # Touch the opt-in marker so the marker and the ledger agree.
                # Best-effort: never raises (a marker write failure must not
                # undo the recorded decision).
                try:
                    sd = governance._skill_dir(skill)
                    if governance._TEST_SKILLS_DIR is None:
                        sd.mkdir(parents=True, exist_ok=True)
                    if not (sd / ".skillopt.optout").is_file():
                        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
                except Exception:
                    pass
            return {
                "ok": bool(res.get("ok")),
                "skill": skill,
                "approved": approved,
                "decided_by": decided_by,
                "ts": res.get("ts"),
                "error": res.get("error"),
                "timestamp": _now(),
            }
        except Exception as e:
            return {"ok": False, "error": f"governance_approve raised: {e}",
                    "timestamp": _now()}