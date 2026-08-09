"""
SkillOpt - sleep cycle endpoint.

Route: POST /api/plugins/skillopt/sleep
Body (JSON): { "verb": "dry-run"|"run"|"harvest", "skill": "<name>"? }

Launches a `python -m skillopt_sleep <verb>` cycle in a background
subprocess. Returns immediately with the PID and log path so the
WebUI can poll for status.

For sync verbs (status, adopt) use the /status or /adopt endpoints.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


ASYNC_VERBS = ("dry-run", "run", "harvest")


class Sleep(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        verb = (data.get("verb") or "").strip()
        skill = (data.get("skill") or "").strip()

        if verb not in ASYNC_VERBS:
            return {
                "ok": False,
                "error": f"verb must be one of {', '.join(ASYNC_VERBS)}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        extra = ["--skill", skill] if skill else []
        run = sleep_runner.launch_sleep_subprocess(verb, extra_args=extra)
        run["ok"] = True
        run["timestamp"] = datetime.now(timezone.utc).isoformat()
        return run
