"""
SkillOpt - status endpoint.

Route: POST /api/plugins/skillopt/status

Returns a one-shot snapshot of the plugin state for the dashboard.
This is the read-only counterpart to the `skillopt_status` tool.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


class Status(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        snap = sleep_runner.get_status_snapshot()
        snap["timestamp"] = datetime.now(timezone.utc).isoformat()
        snap["note"] = (
            "Snapshot of SkillOpt plugin state. Use /sleep to launch a "
            "cycle, /adopt to promote a staged proposal, /config to "
            "read or update settings."
        )
        return snap
