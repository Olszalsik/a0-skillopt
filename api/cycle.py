"""
SkillOpt - single-cycle DETAIL endpoint (v1.4.0-Dev, Day-5 item 7).

Route: GET /api/plugins/skillopt/cycle/<id>   body/query: {cycle_id | id}

Returns one cycle-history entry by cycle_id, or `{ok: False, error}`.

v1.8.37 (audit F4) - why this handler has its own module
--------------------------------------------------------
It used to live in `api/cycles.py` next to the list handler. The framework
binds one handler per module, taking `classes[0]` from a
`reversed(inspect.getmembers(...))` scan - the alphabetically LAST class.
`'Cycle' < 'Cycles'`, so the list handler won and this documented detail
route was unreachable: `/cycle/<id>` silently answered with a LIST payload.

One handler class per module means the dispatch order stops mattering.
Read-only, plugin-local.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


__all__ = ["Cycle"]
