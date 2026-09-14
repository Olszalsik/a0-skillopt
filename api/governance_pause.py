
# SkillOpt - governance pause endpoint (v1.8.8, roadmap open question 5).
#
# Route: POST /api/plugins/skillopt/governance_pause
#   body: {skill, action: 'pause' | 'resume', hours?}
#
# One-click pause/resume for the dashboard. 'pause' writes the
# .skillopt.pause_until marker (epoch seconds) that
# governance.check_skill_eligible step 1.5 already reads; 'resume'
# removes it. Both append a pause/resume row to governance.log.
# Skill names are validated (no separators, no leading dot, max 128)
# so the endpoint can never be used for path escape.

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_skill(skill: str) -> bool:
    if not skill or len(skill) > 128 or skill.startswith('.'):
        return False
    if '/' in skill or '..' in skill:
        return False
    return all(ch.isalnum() or ch in '._-' for ch in skill)


class GovernancePause(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        skill = (data.get('skill') or '').strip()
        action = (data.get('action') or 'pause').strip().lower()
        if not _valid_skill(skill):
            return {'ok': False, 'error': 'invalid skill name',
                    'timestamp': _now()}
        if action not in ('pause', 'resume'):
            return {'ok': False, 'error': 'action must be pause or resume',
                    'timestamp': _now()}
        try:
            hours = float(data.get('hours', 24))
        except (TypeError, ValueError):
            hours = 24.0
        hours = min(max(hours, 0.25), 720.0)
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
            if action == 'pause':
                res = governance.pause_skill(skill, hours)
            else:
                res = governance.resume_skill(skill)
            out = {'ok': bool(res.get('ok')), 'skill': skill,
                   'action': action, 'timestamp': _now()}
            if action == 'pause':
                out['hours'] = hours
                out['until'] = res.get('until')
            else:
                out['was_paused'] = res.get('was_paused')
            if res.get('error'):
                out['error'] = res.get('error')
            return out
        except Exception as e:
            return {'ok': False,
                    'error': 'governance_pause raised: ' + str(e),
                    'timestamp': _now()}
