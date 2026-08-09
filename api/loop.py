"""
SkillOpt - auto-loop status endpoint.

Route: POST /api/plugins/skillopt/loop

Returns the current state of the background auto-loop daemon, so
the WebUI can show a live readout without polling subprocesses.
The daemon writes its state to logs/runs/.auto_loop_state.json;
this endpoint just reads and decorates it.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import auto_loop  # type: ignore


class Loop(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        try:
            state = auto_loop.get_loop_state()
        except Exception as e:
            return {
                "ok": False,
                "error": f"loop state read failed: {e}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        return {
            "ok": True,
            "loop": state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
