"""
SkillOpt - staged proposals endpoint.

Route: POST /api/plugins/skillopt/staged

Lists the staged proposals waiting for a human-in-the-loop decision,
enriched with everything a reviewer needs to decide INFORMEDLY (v1.8.34):

- skill, proposal_id, size, mtime, and the most recent cycle_history
  entry for that skill (gate reason / lift / outcome / held-out);
- `summary`: the full bounded ProposalSummary from helpers/proposal_view
  (intent, one-paragraph rationale, evidence, unified diff). This is what
  replaced the old `"diff_summary": f"{size} bytes"` placeholder, which was
  a stringified byte count and was never even rendered by the template;
- `decision_state`: whether this exact proposal already has a decision.

v1.7.0 (Solution C, Phase C3) shipped the route with gate evidence only.
The approval cards rendered a skill NAME and nothing else, because the
proposal and the approval ledger were disjoint stores: `staging/` keys on
the filename stem, `governance.log` keys on the skill. `pending_approval`
closes that gap by resolving, per skill, which staged proposal the
Governance card is actually gating.
"""

import time
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _proposal_id(p) -> str:
    """The proposal's identity: its staging filename stem.

    Must stay byte-identical to `governance.find_pending_proposal`'s and
    `api/reject.py`'s resolution, or a decision recorded against one id
    will not match the staged file the card is rendering."""
    return p.stem if p.suffix in (".md", ".proposed") else p.name


def _recent_by_skill(limit: int = 200) -> dict:
    """Most recent cycle_history entry per skill. Best-effort."""
    out: dict[str, dict] = {}
    try:
        from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
        for entry in cycle_history.read_cycle_history(limit=limit):
            if not isinstance(entry, dict):
                continue
            skill = entry.get("skill")
            if skill and skill not in out:
                out[skill] = entry
    except Exception:
        pass
    return out


def _get_summary(path, skill: str):
    """Read-or-build the bounded proposal summary. Never raises.

    `get_summary` returns a well-formed document even when it cannot build
    one, so the template can always render *something* rather than crashing
    the whole card list."""
    try:
        from usr.plugins.skillopt.helpers import proposal_view  # type: ignore
    except Exception:
        try:
            from helpers import proposal_view  # type: ignore
        except Exception:
            return None
    try:
        return proposal_view.get_summary(path, skill=skill)
    except Exception:
        return None


class Staged(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        try:
            staged = sleep_runner.find_staged_proposals()
        except Exception as e:
            return {"ok": False, "error": f"find_staged_proposals raised: {e}",
                    "timestamp": _now()}
        try:
            staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            pass
        recent_by_skill = _recent_by_skill()
        proposals = []
        for p in staged:
            pid = _proposal_id(p)
            skill = pid
            try:
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            except Exception:
                size, mtime = 0, 0
            cyc = recent_by_skill.get(skill, {})
            summary = _get_summary(p, skill)
            proposals.append({
                "skill": skill,
                "proposal_id": pid,
                "source": str(p),
                "size": size,
                "mtime": mtime,
                "gate_reason": cyc.get("outcome_detail", ""),
                "lift_pp": cyc.get("lift_pp"),
                "n_held_out": cyc.get("n_held_out"),
                "last_outcome": cyc.get("outcome", ""),
                # v1.8.34: real, renderable summary. `diff_summary` is kept
                # as a deprecated one-line alias because an external consumer
                # may still read it; it is no longer a byte count.
                "summary": summary,
                "diff_summary": _headline(summary, size),
                "age_s": max(0, int(time.time() - mtime)) if mtime else None,
            })
        return {
            "ok": True,
            "proposals": proposals,
            "count": len(proposals),
            "pending_approval": _pending_approval_map(proposals,
                                                     recent_by_skill),
            "timestamp": _now(),
        }


def _headline(summary, size: int) -> str:
    """One-line description for the deprecated `diff_summary` alias."""
    if isinstance(summary, dict):
        text = str(summary.get("summary") or "").strip()
        if text:
            return text
    return f"{size} bytes"


def _pending_approval_map(proposals: list, recent_by_skill: dict) -> dict:
    """skill -> {proposal_id, staged_at, decision, source}.

    This is what the Governance card renders. Before v1.8.34 the card had no
    proposal context at all, so "Approve" on a skill was indistinguishable
    from approving an unseen document.

    The candidate set is the union of three sources, because any one alone
    misses cases: skills with governance markers (the card's own list),
    skills with cycle history, and — the one that matters — every skill that
    actually has a proposal sitting in staging. A brand-new auto-opted-in
    skill has no cycle_history entry yet, so without the third source its
    pending proposal would be invisible on the very tick it was created."""
    out: dict = {}
    try:
        from usr.plugins.skillopt.helpers import governance  # type: ignore
    except Exception:
        try:
            from helpers import governance  # type: ignore
        except Exception:
            return out
    status: dict = {}
    try:
        status = governance.get_governance_status() or {}
    except Exception:
        status = {}
    skills = set()
    for key in ("opted_in", "governed", "opted_out"):
        for name in (status.get(key) or []):
            skills.add(str(name))
    for name in recent_by_skill:
        skills.add(str(name))
    for p in proposals or []:
        if isinstance(p, dict) and p.get("skill"):
            skills.add(str(p["skill"]))
    for skill in sorted(skills):
        try:
            found = governance.find_pending_proposal(skill)
        except Exception:
            found = None
        if not found:
            continue
        entry = dict(found)
        # v1.8.34: attach the summary HERE too, not only in
        # api/governance_status.py. The Governance card in config.html reads
        # THIS map (it is fetched once by /staged and shared, so both cards
        # render the same document), so omitting it left the card's informed
        # block permanently empty in the live UI - the exact "no summary
        # available" state this feature exists to remove. The per-skill
        # governance route still resolves it for callers that ask directly.
        src = entry.get("source")
        if src:
            try:
                entry["summary"] = _get_summary(src, skill)
            except Exception:
                entry["summary"] = None
        else:
            entry["summary"] = None
        out[skill] = entry
    return out