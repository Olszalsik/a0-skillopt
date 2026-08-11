"""
SkillOpt - governance status endpoint (v1.7.0, Solution C Phase C4).

Route: POST /api/plugins/skillopt/governance_status
  body: {skill?}

Read-only snapshot for the dashboard's governance section.

- No `skill`: returns `governance.get_governance_status()` (the full
  block: default_policy, opted_in, opted_out, governed, last_decisions).
- With `skill`: returns the per-skill effective policy (load_skill_policy),
  marker presence (.optin/.optout/.policy.json), the eligibility verdict
  (check_skill_eligible), and the most recent `human_decision` row — the
  same signals the dashboard card renders.
"""

import json
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _last_human_decision(governance, skill: str) -> dict | None:
    log = governance._runs_dir() / "governance.log"
    if not log.is_file():
        return None
    try:
        with open(log, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("skill") == skill and e.get("event") == "human_decision":
                return {
                    "approved": e.get("approved"),
                    "decided_by": e.get("decided_by"),
                    "ts": e.get("ts_iso") or e.get("ts"),
                }
    except Exception:
        pass
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
                    "timestamp": _now(),
                }
            status = governance.get_governance_status()
            return {"ok": bool(status.get("available", False)), **status,
                    "timestamp": _now()}
        except Exception as e:
            return {"ok": False, "error": f"governance_status raised: {e}",
                    "timestamp": _now()}