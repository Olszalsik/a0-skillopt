"""
SkillOpt - reject endpoint (v1.7.0, Solution C Phase C3).

Route: POST /api/plugins/skillopt/reject   body: {proposal_id, reason?}

Records a human REJECT decision on a staged proposal: appends a `rejected`
cycle_history entry + an adoptions.log row (mirroring adopt.py:_append_audit),
AND - since v1.8.36 (audit F2) - moves the proposal out of the pending queue
and records the denial in the governance ledger.

v1.8.36 (audit F2) - why the behaviour changed
------------------------------------------------
This used to be ADVISORY ONLY: it wrote two rows and returned, leaving the
file in staging top level, which is exactly what `find_staged_proposals()`
scans. There was no "human said no" rail anywhere, and the only approval gate
(`require_human_approval`) is skipped by the autonomous override - so on a
later tick the drain re-read the same rejected document and wrote it into the
live `SKILL.md`. That contradicted the documented rule that rejects do not
stay in staging, and it re-created the failure `quarantine_staged_proposal`
exists for.

The old docstring's defence - "the reject is an audit decision, not a delete" -
is still true of the FILE, which is preserved verbatim under
`staging/rejected/`. What was wrong was leaving it in the PENDING queue.
Rejection is now reversible by re-staging; it is simply no longer invisible.

Idempotent: re-rejecting the same proposal_id just appends another row.
"""

import json
import time
from datetime import datetime, timezone

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore

AUDIT_LOG = "adoptions.log"


def _append_audit(entry: dict) -> None:
    p = sleep_runner.runs_dir() / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Reject(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        proposal_id = (data.get("proposal_id") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not proposal_id:
            return {"ok": False, "error": "missing 'proposal_id'", "timestamp": _now()}

        # v1.8.36: resolve the staged proposal and validate the id against the
        # real queue. Previously an arbitrary string became the `skill` on both
        # audit rows, and because the ledger is keyed on proposal_id a row
        # naming a not-yet-staged stem made `find_pending_proposal` report the
        # skill's NEXT proposal as already rejected - so the Governance card
        # silently stopped prompting (audit L3).
        src = None
        try:
            for p in sleep_runner.find_staged_proposals():
                if p.stem == proposal_id:
                    src = p
                    break
        except Exception:
            src = None
        if src is None:
            return {
                "ok": False,
                "error": (f"no staged proposal with id {proposal_id!r}; "
                          f"nothing to reject"),
                "timestamp": _now(),
            }
        skill = src.stem if src.suffix == ".md" else proposal_id

        entry = {
            "skill": skill,
            "proposal_id": proposal_id,
            "decision": "rejected",
            "reason": reason or "user rejected via UI",
            "passed": False,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _append_audit(entry)
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            cycle_history.record_cycle_entry({
                "skill": skill,
                "outcome": "rejected",
                "outcome_detail": reason or "user rejected via UI",
                "proposal_id": proposal_id,
                "source": "reject_api",
            })
        except Exception:
            pass

        # v1.8.36 (audit F2): move it OUT of the pending queue, preserving the
        # document itself (and every sidecar) under staging/rejected/. The
        # sidecars matter: without them a later re-stage would resurrect a
        # proposal whose gate provenance is unknown, which is the orphan
        # deadlock documented in sleep_runner._move_staged_proposal.
        quarantined = None
        quarantine_ok = False
        try:
            moved = sleep_runner.quarantine_staged_proposal(src, tag="reject")
            quarantined = str(moved) if moved is not None else None
            quarantine_ok = moved is not None
        except Exception as e:
            quarantined = f"error: {type(e).__name__}: {e}"

        # v1.8.36 (audit F2): record the denial in the governance ledger, bound
        # to this proposal_id. This is what arms the rejection rail, and the
        # binding is what lets a future proposal for the same skill be judged
        # on its own merits.
        governance_ok = None
        governance_error = None
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore
            res = governance.mark_human_decision(
                skill, False, "user",
                proposal_id=proposal_id,
                note=reason or "user rejected via UI",
            )
            governance_ok = bool(res.get("ok"))
            if not governance_ok:
                governance_error = str(res.get("error") or "ledger write failed")
        except Exception as e:
            governance_ok = False
            governance_error = f"{type(e).__name__}: {e}"

        # v1.8.45 (re-audit R18): report the COMBINED outcome, not `ok:
        # True` over two silent partial failures.
        #
        # Both halves matter and they fail differently:
        #   quarantine failed -> the document is STILL QUEUED, and without the
        #     ledger row there is also no rail, so the drain can adopt it.
        #   ledger failed     -> no rail, so even a quarantined document
        #     could be re-staged and adopted.
        #
        # Returning success in either case told the operator the proposal was
        # out of the queue when it was not - the same defect F10 fixed for the
        # opt-in marker, and the same shape as R1's gate.
        both_ok = quarantine_ok and governance_ok is True
        out = {
            "ok": both_ok,
            **entry,
            "quarantined_to": quarantined,
            "governance_recorded": governance_ok,
            "timestamp": _now(),
        }
        if not both_ok:
            problems = []
            if not quarantine_ok:
                problems.append(
                    f"the proposal was NOT moved out of the queue ({quarantined})")
            if governance_ok is not True:
                problems.append(
                    f"the denial was NOT recorded, so the rejection rail is "
                    f"inert ({governance_error})")
            out["error"] = (
                "rejection recorded INCOMPLETELY: " + "; ".join(problems)
                + ". The document may still be adoptable - re-run the reject "
                  "or remove it from staging by hand.")
        return out