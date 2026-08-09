"""
SkillOpt - fragments API endpoint (v1.2.0, Day-3 item 3).

Routes (under /api/plugins/skillopt/):
  GET  /fragments?skill=<name>   - return fragments of <name> skill
  POST /fragments/rollback         - body: {skill, fragment_id, target_version}

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
    p = sleep_runner.a0_skills_dir() / skill_name / "SKILL.md"
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
