"""
SkillOpt - governance status endpoint (v1.7.0, Solution C Phase C4).

Route: POST /api/plugins/skillopt/governance_status
  body: {skill?}

Read-only snapshot for the dashboard's governance section.

- No `skill`: returns `governance.get_governance_status()` (the full
  block: default_policy, opted_in, opted_out, governed, last_decisions).
- With `skill`: returns the per-skill effective policy (load_skill_policy),
  marker presence (.optin/.optout/.policy.json), the eligibility verdict
  (check_skill_eligible), the most recent `human_decision` row, and —
  v1.8.34 — `pending_proposal`: which staged proposal this skill's pending
  approval is actually about, plus its summary. Before v1.8.34 this route
  returned no proposal context at all, so the Governance card could only
  render a skill NAME and "Approve" was a blind decision.
"""

import json
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary_for(path: str, skill: str):
    try:
        from usr.plugins.skillopt.helpers import proposal_view  # type: ignore
    except Exception:
        try:
            from helpers import proposal_view  # type: ignore
        except Exception:
            return None
    try:
        return proposal_view.get_summary(path, skill=skill)
    except Exception:
        return None


def _pending_proposal(governance, skill: str) -> dict | None:
    """`{proposal_id, decision, staged_at, summary}` or None.

    The summary is read here rather than by the client so the Governance
    card and the Staged card render the SAME document from ONE fetch — two
    independently-fetched copies could disagree, and a reviewer acting on a
    stale diff is worse than no diff at all."""
    try:
        found = governance.find_pending_proposal(skill)
    except Exception:
        return None
    if not found:
        return None
    out = dict(found)
    src = found.get("source")
    if src:
        out["summary"] = _summary_for(src, skill)
    else:
        out["summary"] = None
    return out


def _last_human_decision(governance, skill: str) -> dict | None:
    """The most recent HUMAN decision for `skill`, or None.

    v1.8.47 (re-audit R25): this used to be a second, hand-rolled reader
    that disagreed with the gate in three ways at once - it read the LIVE log
    only (so it went blind at the 5 MB rotation while `_human_approved`, which
    reads the index and the rotated generations, still saw the approval), it
    applied no `MACHINE_DECIDED_BY` filter (so an `autonomous` row was
    presented to the operator as a human decision), and it took the last LINE
    rather than the newest by timestamp. Two readers of one ledger is how R8
    happened.

    So it delegates now. `governance._last_human_decision` is the single
    reader: it consults every generation plus the durable index, filters
    machine rows, and picks newest-by-parsed-epoch.
    """
    try:
        decision = governance._last_human_decision(skill)
    except Exception:
        return None
    if not decision or decision is governance._UNREADABLE:
        return None
    try:
        approved, ts = decision
        return {"approved": bool(approved), "ts": ts,
                "decided_by": "user"}
    except Exception:
        return None


class GovernanceStatus(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
            if skill:
                policy = governance.load_skill_policy(skill)
                sd = governance._skill_dir(skill)
                markers = {
                    "optin": (sd / ".skillopt.optin").is_file(),
                    "optout": (sd / ".skillopt.optout").is_file(),
                    "policy_json": (sd / ".skillopt.policy.json").is_file(),
                }
                eligible, reason = governance.check_skill_eligible(skill)
                return {
                    "ok": True,
                    "skill": skill,
                    "policy": policy,
                    "markers": markers,
                    "eligible": eligible,
                    "reason": reason,
                    "last_human_decision": _last_human_decision(governance, skill),
                    "pending_proposal": _pending_proposal(governance, skill),
                    "timestamp": _now(),
                }
            status = governance.get_governance_status()
            # v1.8.34: the global block gains the same proposal linkage the
            # per-skill block has, so a client that asks once for the whole
            # status does not have to fan out over every skill.
            if isinstance(status, dict):
                try:
                    status["pending_approval"] = {
                        s: _pending_proposal(governance, s)
                        for s in sorted(
                            set(status.get("opted_in") or [])
                            | set(status.get("governed") or [])
                        )
                    }
                except Exception:
                    pass
            return {"ok": bool(status.get("available", False)), **status,
                    "timestamp": _now()}
        except Exception as e:
            return {"ok": False, "error": f"governance_status raised: {e}",
                    "timestamp": _now()}