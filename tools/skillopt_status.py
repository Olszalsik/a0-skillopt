"""SkillOpt status tool.

Read-only. Returns a one-shot snapshot of the plugin state: how many
rollouts have been harvested, which A0 skills are eligible for
optimization, which proposals are staged, and whether the SkillOpt
Python package is importable.

Args (all optional):
  detail: "summary" (default) | "rollouts" | "skills" | "staged"
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers.tool import Response, Tool  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


class SkilloptStatus(Tool):
    async def execute(self, **kwargs) -> Response:
        detail = (self.args.get("detail") or "summary").lower()
        snap = sleep_runner.get_status_snapshot()

        if detail == "rollouts":
            rd = sleep_runner.rollouts_dir()
            snap["rollouts"] = sorted(p.name for p in rd.glob("*.json"))[-25:]
        elif detail == "skills":
            snap["skills"] = sleep_runner.list_skills_available()
        elif detail == "staged":
            snap["staged"] = [
                {
                    "path": str(p),
                    "size_kb": round(p.stat().st_size / 1024.0, 1),
                    "modified": p.stat().st_mtime,
                }
                for p in sleep_runner.find_staged_proposals()
            ]

        return Response(
            message=json.dumps(snap, indent=2, default=str),
            break_loop=False,
        )
