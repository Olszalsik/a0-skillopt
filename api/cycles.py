"""
SkillOpt - cycles LIST endpoint (v1.4.0-Dev, Day-5 item 7).

Route: GET /api/plugins/skillopt/cycles
  ?limit=50&skill=<name>&since_ts=<iso>&outcome=<adopted|rejected|skipped|errored|unknown>

Returns the most recent N cycle entries (newest-first), filtered.

v1.8.37 (audit F4) - why the single-cycle handler moved out of this file
-------------------------------------------------------------------------
This module used to declare BOTH `Cycles` (list) and `Cycle` (one entry by
id). The framework binds ONE handler per `api/<name>.py` module, taking
`classes[0]` from `load_classes_from_file`, which iterates
`reversed(inspect.getmembers(...))` - the **alphabetically last** matching
class. `'Cycle' < 'Cycles'`, so the LIST handler won here and the documented
`/cycle/<id>` detail route was unreachable: it silently answered with the
list handler's response.

This is the second instance of that defect in this plugin; `api/fragments.py`
had the mirror-image problem (the write handler won there). Both were found
by `t_v1837_api_dispatch_matches_intent`, which simulates the framework's
selection across every `api/*.py` - a check the suite previously had no way
to make, because every other test imports handler classes by name.

The single-cycle handler now lives in `api/cycle.py`. **One handler class per
module**: adding a second `ApiHandler` subclass to any `api/*.py` silently
disables whichever class sorts first, with no error anywhere.

Read-only. Backed by helpers/cycle_history.py. Plugin-local.
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


__all__ = ["Cycles"]
