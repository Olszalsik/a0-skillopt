"""
SkillOpt - cycles API endpoint (v1.4.0-Dev, Day-5 item 7).

Routes (under /api/plugins/skillopt/):
  GET /cycles?limit=50&skill=<name>&since_ts=<iso>&outcome=<one of adopted|rejected|skipped|errored|unknown>
      Returns the most recent N cycle entries (newest-first), filtered.
  GET /cycle/<id>          Returns one cycle entry by cycle_id, or {ok:False}.

Backed by helpers/cycle_history.py. Read-only. Plugin-local.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Cycles(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        try:
            limit = int(data.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        skill = (data.get("skill") or "").strip() or None
        since_ts = (data.get("since_ts") or "").strip() or None
        outcome = (data.get("outcome") or "").strip() or None
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            entries = cycle_history.read_cycle_history(
                limit=limit, skill=skill, since_ts=since_ts, outcome=outcome,
            )
            return {
                "ok": True,
                "count": len(entries),
                "limit": limit,
                "skill": skill,
                "outcome": outcome,
                "since_ts": since_ts,
                "entries": entries,
                "timestamp": _now(),
            }
        except Exception as e:
            return {"ok": False, "error": f"cycle_history read failed: {e}",
                    "timestamp": _now()}


class Cycle(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        cycle_id = (data.get("cycle_id") or data.get("id") or "").strip()
        if not cycle_id:
            return {"ok": False, "error": "missing 'cycle_id'", "timestamp": _now()}
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            entry = cycle_history.read_cycle(cycle_id)
            if entry is None:
                return {
                    "ok": False,
                    "error": f"cycle_id {cycle_id!r} not found",
                    "timestamp": _now(),
                }
            return {
                "ok": True,
                "cycle_id": cycle_id,
                "entry": entry,
                "timestamp": _now(),
            }
        except Exception as e:
            return {"ok": False, "error": f"cycle_history read_cycle failed: {e}",
                    "timestamp": _now()}


__all__ = ["Cycles", "Cycle"]
