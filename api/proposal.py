"""
SkillOpt - single proposal detail endpoint (v1.8.34).

Route: POST /api/plugins/skillopt/proposal
  body: {proposal_id, full?}

Returns the bounded ProposalSummary for ONE staged proposal: intent, the
one-paragraph rationale, the evidence strip, and a unified diff against the
live SKILL.md.

WHY THIS ROUTE EXISTS SEPARATELY FROM /staged
---------------------------------------------
/staged returns a bounded diff for every queued proposal so the list is
cheap to render. "View the full proposed changes" needs more than that
bound, and re-sending every proposal to render one of them would be both
slow and a much larger response than the reviewer asked for. So the list
route stays bounded and this route serves one proposal at a request for the
larger view.

SECURITY CONTRACT (read before changing)
----------------------------------------
`proposal_id` is resolved ONLY by matching a stem against
`sleep_runner.find_staged_proposals()`. A caller-supplied path is NEVER
read, joined, or resolved. The plugin serves skill-adjacent user content
over HTTP, so accepting `{"proposal_id": "../../usr/.env"}` and reading it
would be an arbitrary file read.

This is an ordinary ApiHandler POST and inherits the framework's auth and
CSRF defaults. There is no exemption here and none may be added — the DOX
records that the last read-only-looking route to try claiming one could
spawn a subprocess, which is why it is GET-only now.
"""

from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_staged(proposal_id: str):
    """Stem-match `proposal_id` against the staged queue. None on any miss.

    This is the whole security boundary: the id is compared for EQUALITY
    against real stems. It is never concatenated into a path, normalised to
    an absolute path, or otherwise trusted as a locator.
    """
    if not proposal_id:
        return None
    try:
        staged = sleep_runner.find_staged_proposals()
    except Exception:
        return None
    wanted = str(proposal_id).strip()
    for p in staged:
        stem = p.stem if p.suffix in (".md", ".proposed") else p.name
        if stem == wanted:
            return p
    return None


class Proposal(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        proposal_id = str(data.get("proposal_id") or "").strip()
        want_full = bool(data.get("full"))
        if not proposal_id:
            return {"ok": False, "error": "missing 'proposal_id'",
                    "timestamp": _now()}
        src = _resolve_staged(proposal_id)
        if src is None:
            return {"ok": False,
                    "error": f"no staged proposal with id {proposal_id!r}",
                    "timestamp": _now()}
        skill = src.stem if src.suffix in (".md", ".proposed") else src.name
        try:
            from usr.plugins.skillopt.helpers import proposal_view  # type: ignore
        except Exception:
            try:
                from helpers import proposal_view  # type: ignore
            except Exception:
                return {"ok": False, "error": "proposal_view unavailable",
                        "proposal_id": proposal_id, "timestamp": _now()}

        # get_summary() serves the persisted sidecar, computing it on read
        # when absent (every proposal staged before v1.8.34 has none) and
        # persisting best-effort. It never raises.
        summary = proposal_view.get_summary(src, skill=skill)
        if summary is None:
            return {"ok": False, "error": "summary unavailable",
                    "proposal_id": proposal_id, "timestamp": _now()}

        out = {
            "ok": True,
            "proposal_id": proposal_id,
            "skill": skill,
            "summary": summary,
            "timestamp": _now(),
        }
        if want_full:
            # Recompute the diff at the hard ceiling rather than trusting the
            # sidecar's already-truncated text, but still cap it: `full`
            # means "more than the list view", not "unbounded".
            try:
                proposed = src.read_text(encoding="utf-8", errors="replace")
                current = ""
                try:
                    target = sleep_runner.safe_skill_md(skill)
                    if target.is_file():
                        current = target.read_text(encoding="utf-8",
                                                   errors="replace")
                except Exception:
                    current = ""
                out["full_diff"] = proposal_view.compute_diff(
                    current, proposed,
                    max_lines=proposal_view.HARD_DIFF_MAX_LINES,
                    max_chars=proposal_view.HARD_DIFF_MAX_CHARS,
                )
            except Exception as e:
                out["full_diff"] = {
                    "available": False,
                    "truncated": False,
                    "added_lines": 0,
                    "removed_lines": 0,
                    "hunk_count": 0,
                    "text": "",
                    "error": f"{type(e).__name__}: {e}",
                }
        return out