"""
SkillOpt - fragments READ endpoint (v1.2.0, Day-3 item 3).

Route: GET /api/plugins/skillopt/fragments?skill=<name>

Returns the addressed fragments of a skill, plus validation warnings.

v1.8.37 (audit F4) - why the rollback handler moved out of this file
------------------------------------------------------------------------
This module used to declare BOTH `Fragments` (read) and `FragmentsRollback`
(write). The framework resolves an `api/<name>.py` module to a single handler
via `load_classes_from_file`, which iterates
`reversed(inspect.getmembers(...))` and takes `classes[0]` - i.e. the
**alphabetically last** matching class. `'Fragments' < 'FragmentsRollback'`,
so `classes[0]` was `FragmentsRollback`.

Consequences, both silent:
  - the documented read route was **unreachable**: a GET returned the rollback
    handler's "missing one of: skill, fragment_id, target_version" error;
  - a fragment WRITE was reachable at a path that reads as a read, so the
    route's shape did not describe what it did.

The write handler now lives in `api/fragments_rollback.py` (route
`POST /fragments/rollback`), so **one handler class per module** and the
dispatch order stops mattering. `t_v1837_api_dispatch_matches_intent`
asserts that property across every `api/*.py`, because nothing else in the
suite could detect it - the other tests import handler classes by name and so
never exercise the dispatcher.

Reads/writes the v1.2.0 fragment store. Backed by
helpers/fragment_store.py. All persistence is plugin-local; we never
write to the user's A0 skills dir from this endpoint (we only read the
SKILL.md; writes go to the plugin's fragments/ dir as snapshots).
"""

from datetime import datetime, timezone
from pathlib import Path

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _resolve_skill_path(skill_name: str) -> Path | None:
    """Resolve a skill name to its SKILL.md path. None if missing."""
    if not skill_name:
        return None
    p = sleep_runner.safe_skill_md(skill_name)
    return p if p.is_file() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Fragments(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        if not skill:
            return {"ok": False, "error": "missing 'skill'", "timestamp": _now()}
        skill_path = _resolve_skill_path(skill)
        if not skill_path:
            return {
                "ok": False,
                "error": f"skill {skill!r} not found in {sleep_runner.a0_skills_dir()}",
                "timestamp": _now(),
            }
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
            frags = fragment_store.read_fragments(skill_path)
            warnings = fragment_store.validate_fragments(skill_path)
            return {
                "ok": True,
                "skill": skill,
                "skill_path": str(skill_path),
                "fragments": frags,
                "warnings": warnings,
                "count": len(frags),
                "timestamp": _now(),
            }
        except Exception as e:
            return {"ok": False, "error": f"fragment_store raised: {e}",
                    "timestamp": _now()}
