"""
SkillOpt - adopt endpoint.

Route: POST /api/plugins/skillopt/adopt

Promotes a staged proposal to usr/skills/<name>/SKILL.md after a final
validation-gate sanity check. The post-adopt hook writes a one-line audit
entry to logs/runs/.

v1.1.0:
- Runs the proposal through the shared `validate_proposal()` gate
  (whitespace-normalised equality, mandatory example block, shrink
  ceiling, optional held-out enforcement) BEFORE copying. The v1.0
  endpoint accepted a 1904->1904 byte-identical 'qa' adoption.

v1.7.0 (Solution C, Phase C3):
- Accepts an optional `proposal_id` in the body. When present, the
  staged proposal whose filename stem matches `proposal_id` is adopted
  (instead of always the most-recent `staged[0]`). Falls back to
  `staged[0]` when `proposal_id` is absent or matches nothing — keeps the
  old "Adopt latest" behaviour backwards-compatible.
- Writes a whole-file `_default` snapshot via
  `fragment_store.snapshot_default(skill_name, current)` BEFORE
  overwriting the SKILL.md, so the adopt is reversible via the new
  /rollback endpoint (or the existing fragment-store restore). The v1.2.0
  `write_fragment` `_default` branch overwrote with NO snapshot.
- Records an `adopted` cycle_history entry so the dashboard's per-cycle
  audit reflects the human-in-the-loop adopt.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from helpers.api import ApiHandler  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


AUDIT_LOG = "adoptions.log"


def _append_audit(entry: dict) -> None:
    p = sleep_runner.runs_dir() / AUDIT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _latest_sleep_log() -> Path | None:
    runs_root = sleep_runner.runs_dir()
    if not runs_root.is_dir():
        return None
    logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def _check_governance(skill_name: str) -> tuple[bool, str, str]:
    """Run the shared governance gate. `(eligible, reason, error)`.

    v1.8.41 (re-audit R1). Extracted so `api/adopt.py` and
    `tools/skillopt_sleep.py` issue the **identical** call. The divergence
    this audit found was possible precisely because each path spelled its own
    copy: the tool grew a chain in v1.8.36 and the route was never given one,
    so "the three adopt paths are one gate" was false for as long as nobody
    diffed them.

    `error` is non-empty only when the gate could not be EVALUATED (module
    unimportable, or the check raised). Callers must treat that as a refusal:
    this is the path that writes a live skill, and "I could not tell" must
    never mean "yes".

    The mode is resolved with **no argument** so the operator's persisted
    config is authoritative - the same rule every production call site
    follows (see the precedence contract in AGENTS.md).
    """
    try:
        from usr.plugins.skillopt.helpers import governance  # type: ignore
    except Exception:
        try:
            from helpers import governance  # type: ignore
        except Exception as e:
            return False, "", (f"the governance module is unavailable "
                               f"({type(e).__name__}: {e}); no rail could be "
                               f"evaluated")
    try:
        autonomous = bool(governance.resolve_evolution_mode().get("autonomous"))
    except Exception as e:
        return False, "", (f"the operating mode could not be resolved "
                           f"({type(e).__name__}: {e})")
    try:
        eligible, reason = governance.check_skill_eligible(
            skill_name, auto_adopt=autonomous)
    except Exception as e:
        return False, "", (f"the governance check raised "
                           f"({type(e).__name__}: {e})")
    return bool(eligible), str(reason or ""), ""


def _resolve_staged(proposal_id: str | None):
    """Resolve a staged proposal path. Returns (path, skill_name) or (None, None).

    v1.8.36 (audit M5): when `proposal_id` is SUPPLIED and matches no staged
    stem, this now returns `(None, None)` instead of silently falling back to
    the newest proposal.

    The old fallback defeated the informed-approval contract this route
    exists to serve: a reviewer reads proposal `code-review`'s summary and
    diff on the Staged card, clicks Approve, and the server writes
    `browser-automation`'s proposal instead. The response said which skill it
    adopted, but the message was green (audit M6), so the mismatch was
    invisible. `api/proposal.py` already treats stem-equality as a hard
    boundary; this was the one route that treated a non-match as "no
    constraint".

    "Adopt latest" remains available for the genuinely argument-less call
    (`SkillOptDashboard.adopt()`), which is the documented back-compat path.
    """
    staged = sleep_runner.find_staged_proposals()
    if not staged:
        return None, None
    staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if proposal_id:
        pid = str(proposal_id).strip()
        for p in staged:
            if p.stem == pid:
                skill_name = p.stem if p.suffix == ".md" else "unknown"
                return p, skill_name
        # v1.8.36: a supplied id that matches nothing is an ERROR, not a hint.
        return None, None
    src = staged[0]
    skill_name = src.stem if src.suffix == ".md" else "unknown"
    return src, skill_name


class Adopt(ApiHandler):
    async def process(self, input_data, request):  # type: ignore[no-untyped-def]
        data = input_data or {}
        proposal_id = data.get("proposal_id")
        # v1.8.36 (audit M5): distinguish "no id supplied" (adopt latest, the
        # documented back-compat call) from "an id that matched nothing".
        if proposal_id is not None and not str(proposal_id).strip():
            proposal_id = None
        src, skill_name = _resolve_staged(proposal_id)
        if src is None:
            if proposal_id:
                return {
                    "ok": False,
                    "error": (f"no staged proposal with id "
                              f"{str(proposal_id).strip()!r}; refusing to "
                              f"adopt a different proposal"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            return {
                "ok": False,
                "error": "no staged proposals — run a sleep cycle first",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # v1.8.41 (re-audit R1): GOVERNANCE GATE.
        #
        # This route previously ran NO governance check at all - the file
        # contained zero references to `governance`. `check_skill_eligible` is
        # the ONLY implementation of `.skillopt.optout`, `mode: immutable`,
        # `.skillopt.pause_until`, `min_interval_seconds`,
        # `daily_budget_cents` and the v1.8.36 rejection rail, so clicking
        # Approve wrote a live `usr/skills/<n>/SKILL.md` for a skill the
        # operator had opted out of, paused, marked immutable, **or whose
        # proposal a human had explicitly rejected**. This is the one surface
        # with no agent and no second pair of eyes, so it was the worst place
        # for the gate to be missing.
        #
        # The call is deliberately IDENTICAL to `tools/skillopt_sleep.py` and
        # `auto_loop._skill_eligible_for_cycle`, including
        # `auto_adopt=resolve_evolution_mode()["autonomous"]` with no
        # argument, so the three paths cannot diverge again. Autonomy
        # bypassing `require_human_approval` here is consistent with the other
        # two: optout / immutable / pause / cadence / budget / human-rejected
        # all live ABOVE that bypass and still bite.
        #
        # Fails CLOSED in both directions, matching the agent tool: an
        # unimportable governance module and a raising check both refuse.
        # "I could not evaluate the rails" must never mean "yes" on the path
        # that writes a live file.
        gov_ok, gov_reason, gov_error = _check_governance(skill_name)
        if gov_error:
            # Could NOT EVALUATE. Distinct from a refusal: this is a broken
            # install or a raising check, not an operator decision, and the
            # operator needs to know which they are looking at.
            return {
                "ok": False,
                "error": (f"adoption DEFERRED for `{skill_name}`: "
                          f"{gov_error}. Refusing rather than writing a live "
                          f"skill with the rails unevaluated."),
                "governance": "unavailable",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        if not gov_ok:
            # Evaluated, and refused. `gov_reason` is the rail that fired.
            return {
                "ok": False,
                "error": (f"adoption REFUSED for `{skill_name}` by "
                          f"governance: {gov_reason}. The live SKILL.md was "
                          f"left untouched; every rail the background loop "
                          f"honours applies here too."),
                "governance": "refused",
                "reason": gov_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # v1.8.41: the single-flight wrapper above no longer swallows. It is a
        # fail-open wrapper, and R1's sibling in the register notes it would
        # fail open if the callee ever stopped being total. A pending replay
        # means a second writer for this proposal, so an unknown state must
        # refuse rather than proceed.
        try:
            in_flight = sleep_runner.find_pending_real_gate_sidecar() is not None
            sidecar_error = None
        except Exception as e:
            in_flight, sidecar_error = None, f"{type(e).__name__}: {e}"
        if sidecar_error is not None:
            return {
                "ok": False,
                "error": ("adoption DEFERRED: the real-gate single-flight "
                          f"check could not be evaluated ({sidecar_error}). "
                          f"Refusing rather than risk two writers."),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        if in_flight:
            return {
                "ok": False,
                "error": ("a real-gate replay is in flight; adoption is "
                          "single-flight. Try again once it finishes."),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # v1.8.41 (re-audit R1, sibling of L7): `adopt_write_skill` already
        # creates the parent directory, so doing it here was redundant AND a
        # side effect ahead of the gate - a refused adopt left an empty
        # `usr/skills/<name>/` behind. Read-only from here to the write.
        target = sleep_runner.safe_skill_md(skill_name)
        proposed = src.read_text(encoding="utf-8")
        current = ""
        if target.is_file():
            current = target.read_text(encoding="utf-8")

        cfg = sleep_runner.merged_config()
        last_log = _latest_sleep_log()
        held_out = sleep_runner.parse_held_out(last_log) if last_log else None
        # v1.8.1: honour the per-proposal official-gate provenance marker.
        # Previously a proposal staged by the OFFICIAL engine was re-gated
        # locally with held_out parsed from the LATEST sleep log (possibly
        # another skill's run) and could be wrongly rejected. When the
        # marker is present the official held-out verdict is authoritative
        # and only the cheap structural stages run (same rule as the
        # auto-loop).
        marker = sleep_runner.read_official_gate_marker(src)
        official_gated = bool(marker.get("official_gated")) if marker else False
        ok, reason = sleep_runner.validate_proposal(
            proposed,
            current,
            min_chars=int(cfg.get("gate_min_chars", 200)),
            min_improvement_pp=float(cfg.get("gate_min_improvement_pp", 0.0)),
            max_shrink_ratio=float(cfg.get("gate_max_shrink_ratio", 0.5)),
            held_out=held_out,
            skill_name=skill_name,  # v1.2.0: enables the A/B harness stage
            official_gated=official_gated,
        )

        entry = {
            "skill": skill_name,
            "source": str(src),
            "target": str(target),
            "proposal_id": src.stem,
            "proposed_size": len(proposed),
            "current_size": len(current),
            "passed": ok,
            "reason": reason,
            "held_out": held_out,
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        _append_audit(entry)

        if not ok:
            return {
                "ok": False,
                "error": f"validation gate rejected proposal: {reason}",
                "entry": entry,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        # v1.8.36 (audit F3): the live write goes through
        # `sleep_runner.adopt_write_skill()` - the ONLY sanctioned writer.
        #
        # The previous code snapshotted, swallowed a snapshot FAILURE, and
        # then wrote with `Path.write_text` (open-truncate-then-write). That is
        # the v1.8.24 P0 defect, on the one route a human clicks:
        #   - a failed snapshot left `/rollback` with no target while the
        #     overwrite proceeded anyway - the unrecoverable case, which
        #     `adopt_write_skill`'s own docstring says must refuse;
        #   - a container stop mid-write left a half-written live skill;
        #   - no `SKILL.md.tmp` cleanup on failure.
        # `adopt_write_skill` snapshots, refuses when the snapshot fails, then
        # writes temp + `os.replace`, and returns `{ok, error}`. Both the
        # drain and the agent tool already used it; this route did not.
        _w = sleep_runner.adopt_write_skill(skill_name, proposed)
        if not _w.get("ok"):
            # Record the refusal so the audit trail shows an attempted adopt
            # that did NOT land, rather than nothing at all.
            try:
                _append_audit({**entry, "passed": False,
                               "reason": f"write refused: {_w.get('error')}",
                               "adopted": False})
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"refusing to overwrite the live skill: "
                         f"{_w.get('error')}",
                "entry": entry,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        # `adopt_write_skill` already took the snapshot; report its result so
        # the UI can surface a failure rather than claiming success.
        # v1.8.48 (audit U9): this used to hardcode `ok: True` and never read
        # `_w`, so the settings page's "(snapshot FAILED)" branch could never
        # render - the M6 fix was present in the UI and unreachable in the
        # route. `adopt_write_skill` reports `snapshotted` on the success path
        # (and refuses outright when the snapshot fails), so read it.
        snapshot = {"ok": bool(_w.get("snapshotted")), "via": "adopt_write_skill"}

        # v1.8.36 (audit M7): consume the proposal. Previously it stayed in
        # staging after a successful manual adopt, so `find_staged_proposals()`
        # kept returning it and the drain re-attempted it on the next tick.
        # `consume_staged_proposal` also carries the sidecars (gate marker,
        # real-gate sidecar + tasks, proposal summary) so none is orphaned.
        consumed = None
        try:
            moved = sleep_runner.consume_staged_proposal(src)
            consumed = str(moved) if moved is not None else None
        except Exception as e:
            consumed = f"error: {type(e).__name__}: {e}"

        # v1.7.0: record an `adopted` cycle_history entry for the
        # dashboard's human-in-the-loop audit trail.
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore
            cycle_history.record_cycle_entry({
                "skill": skill_name,
                "outcome": "adopted",
                "outcome_detail": reason,
                "proposal_id": src.stem,
                "proposed_size": len(proposed),
                "current_size": len(current),
                "source": "adopt_api",
            })
        except Exception:
            pass

        return {
            "ok": True,
            **entry,
            "snapshot": snapshot,
        # v1.8.45 (audit R15): surface that output redaction altered the
        # document, so the operator sees a live skill that is not
        # byte-identical to what was proposed - and why.
        "redacted": bool(_w.get("redacted")),
            # Surfaced so the operator can see the proposal left the queue
            # (audit M7) rather than silently lingering for the drain.
            "consumed_to": consumed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }