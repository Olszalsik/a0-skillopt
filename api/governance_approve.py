"""
SkillOpt - governance approve endpoint (v1.7.0, Solution C Phase C4).

Route: POST /api/plugins/skillopt/governance_approve
  body: {skill, approved, decided_by?, proposal_id?, summary_id?}

Records a human approval/rejection via `governance.mark_human_decision`
(the same row `_human_approved` reads to pass gate step 7). When
`approved=True` it also touches `.skillopt.optin` (idempotent) so the
opt-in marker and the approval ledger agree — a skill that was
auto-opted-in (markers created, but `require_human_approval_pending`)
becomes fully eligible after this call.

`approved=False` records a deny (the skill stays
`require_human_approval_pending`). Idempotent: re-approving just appends
another `human_decision` row (the most recent wins).

v1.8.34: `proposal_id` / `summary_id` are optional. Supplied, they bind the
decision to the exact proposal the reviewer read, which is what lets
`governance.find_pending_proposal` distinguish a pending decision from a
decided one. Omitted, behaviour is unchanged.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_approved(value):
    """Strict boolean for the decision field. None when unusable.

    v1.8.36 (audit F10). `bool(value)` is wrong in both directions here:
    `bool("false")` is True, which would APPROVE on an explicit denial. Only
    real booleans, 0/1, and the unambiguous string spellings are accepted -
    the same dialect `api/config.py::_coerce_like_default` uses, so there is
    one coercion rule in the plugin rather than three.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0"):
            return False
    return None


class GovernanceApprove(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        if not skill:
            return {"ok": False, "error": "missing 'skill'", "timestamp": _now()}

        # v1.8.36 (audit F10): `approved` is REQUIRED and strictly coerced.
        # It used to be `bool(data.get("approved", True))`, which failed open
        # twice on a state-mutating, intent-bearing endpoint:
        #   - a body with no `approved` key approved the skill, AND
        #   - `{"approved": "false"}` approved it, because bool("false") is True.
        # That is the exact coercion defect `api/config.py::_coerce_like_default`
        # and the mode resolver were hardened against. A missing field here is
        # not a default to guess - it is an absent intent.
        if "approved" not in data:
            return {
                "ok": False,
                "error": ("missing 'approved'; it must be explicitly true or "
                          "false (this endpoint records a human decision)"),
                "timestamp": _now(),
            }
        _coerced = _coerce_approved(data.get("approved"))
        if _coerced is None:
            return {
                "ok": False,
                "error": (f"'approved' must be a boolean, got "
                          f"{data.get('approved')!r}"),
                "timestamp": _now(),
            }
        approved = _coerced

        # v1.8.36 (audit F10): validate `skill` before it reaches a path
        # builder. `api/governance_pause.py` already did this "so the endpoint
        # can never be used for path escape". No traversal was possible - the
        # marker write is redirected to `_invalid_` - but for an unsafe name
        # that write then RAISED (no mkdir in production), was swallowed, and
        # the route still returned ok: true: a partial effect reported as
        # complete.
        try:
            from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
            if not sleep_runner.is_safe_skill_name(skill):
                return {
                    "ok": False,
                    "error": f"invalid skill name: {skill!r}",
                    "timestamp": _now(),
                }
        except Exception as e:
            return {
                "ok": False,
                "error": f"could not validate skill name: {e}",
                "timestamp": _now(),
            }

        decided_by = (data.get("decided_by") or "user").strip() or "user"
        # v1.8.34: optional proposal binding. When the card supplied the id of
        # the document the reviewer actually read, the ledger row names it,
        # so find_pending_proposal() can later tell "pending" from "decided"
        # and the audit trail points at a real file rather than only a skill.
        # Omitted by older clients, which keeps their behaviour identical.
        proposal_id = (data.get("proposal_id") or "").strip() or None
        summary_id = (data.get("summary_id") or "").strip() or None
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
            res = governance.mark_human_decision(
                skill, approved, decided_by,
                proposal_id=proposal_id, summary_id=summary_id,
            )
            if approved:
                # Touch the opt-in marker so the marker and the ledger agree.
                # v1.8.36: the result is REPORTED rather than swallowed. A
                # silent failure here left the ledger saying "approved" while
                # the marker said otherwise, and the route claimed success.
                marker_ok, marker_error = True, ""
                try:
                    sd = governance._skill_dir(skill)
                    if governance._TEST_SKILLS_DIR is None:
                        sd.mkdir(parents=True, exist_ok=True)
                    if not (sd / ".skillopt.optout").is_file():
                        (sd / ".skillopt.optin").write_text("", encoding="utf-8")
                except Exception as e:
                    marker_ok, marker_error = False, f"{type(e).__name__}: {e}"
                if not marker_ok:
                    return {
                        "ok": False,
                        "error": ("decision recorded but the opt-in marker "
                                  f"could not be written: {marker_error}"),
                        "skill": skill,
                        "approved": approved,
                        "decided_by": decided_by,
                        "proposal_id": proposal_id,
                        "ts": res.get("ts"),
                        "marker_written": False,
                        "timestamp": _now(),
                    }
            return {
                "ok": bool(res.get("ok")),
                "skill": skill,
                "approved": approved,
                "decided_by": decided_by,
                "proposal_id": proposal_id,
                "ts": res.get("ts"),
                "error": res.get("error"),
                "timestamp": _now(),
            }
        except Exception as e:
            return {"ok": False, "error": f"governance_approve raised: {e}",
                    "timestamp": _now()}