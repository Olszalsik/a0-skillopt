"""
SkillOpt - fragment ROLLBACK endpoint (v1.2.0, Day-3 item 3).

Route: POST /api/plugins/skillopt/fragments/rollback
  body: {skill, fragment_id, target_version}

Rolls one addressed fragment of a skill back to a previous version.

v1.8.37 (audit F4) - why this handler moved into its own module
----------------------------------------------------------------
It used to live in `api/fragments.py` alongside the read handler. The
framework binds ONE handler per `api/<name>.py` module, taking `classes[0]`
from `load_classes_from_file`, which iterates
`reversed(inspect.getmembers(...))` - the **alphabetically last** matching
class. `'Fragments' < 'FragmentsRollback'`, so the WRITE handler won and the
documented read route was unreachable.

One handler class per module means the dispatch order stops mattering. Keep
it that way: adding a second `ApiHandler` subclass to any `api/*.py` silently
disables whichever class sorts first, with no error anywhere.

Writes go to the plugin's own `fragments/` dir; this endpoint never writes to
the user's A0 skills dir except through the fragment store's own snapshotting
path.
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


class FragmentsRollback(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get("skill") or "").strip()
        fragment_id = (data.get("fragment_id") or "").strip()
        target_version = (data.get("target_version") or "").strip()
        if not (skill and fragment_id and target_version):
            return {
                "ok": False,
                "error": "missing one of: skill, fragment_id, target_version",
                "timestamp": _now(),
            }
        skill_path = _resolve_skill_path(skill)
        if not skill_path:
            return {
                "ok": False,
                "error": f"skill {skill!r} not found in {sleep_runner.a0_skills_dir()}",
                "timestamp": _now(),
            }
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
            result = fragment_store.rollback_fragment(
                skill_path, fragment_id, target_version,
            )
            return {"ok": bool(result.get("ok")), **result,
                    "timestamp": _now()}
        except Exception as e:
            return {"ok": False, "error": f"rollback raised: {e}",
                    "timestamp": _now()}
