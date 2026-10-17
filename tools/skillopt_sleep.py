"""SkillOpt Sleep tool — the headliner feature.

Launches a `python -m skillopt_sleep` cycle as a background
subprocess so the agent loop is never blocked. The verb determines
what the cycle does:

dry-run harvest + mine + replay, report only (no skill edits)
run full nightly cycle; proposal is staged for review
status read state + latest proposal (sync, no subprocess)
adopt apply the latest staged proposal (validation-gated;
 the post-adopt hook will run the final gate check)
harvest debug: print mined tasks
schedule install a nightly cron entry (currently a stub for v1)

The tool returns immediately with the subprocess PID and log path
so the agent can check progress later or surface it to the user.

Args:
verb: one of dry-run | run | status | adopt (default: status)
skill: optional skill name to scope the cycle to (otherwise: all)

v1.1.0 changes:
- `verb=adopt` now runs the shared `validate_proposal()` gate before
  copying, instead of an inline check that let byte-identical
  'improvements' through.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from helpers.tool import Response, Tool  # type: ignore

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


VALID_VERBS = ("dry-run", "run", "status", "adopt", "harvest")


def _latest_sleep_log() -> Path | None:
    runs_root = sleep_runner.runs_dir()
    if not runs_root.is_dir():
        return None
    logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


class SkilloptSleep(Tool):
    async def execute(self, **kwargs) -> Response:
        verb = (self.args.get("verb") or "status").strip()
        skill = (self.args.get("skill") or "").strip()

        if verb not in VALID_VERBS:
            return Response(
                message=(
                    f"Unknown verb: {verb!r}. "
                    f"Valid verbs: {', '.join(VALID_VERBS)}"
                ),
                break_loop=False,
            )

        # Sync verbs (status, adopt) run inline and return immediately.
        if verb == "status":
            return self._status()
        if verb == "adopt":
            # v1.8.45 (re-audit R12): pass the target through.
            # The argument was accepted and then discarded, so
            # `verb=adopt skill="foo"` adopted the newest
            # staged proposal instead - audit M5 fixed exactly
            # this on the HTTP route, and the agent path is the
            # copy with no human reading a diff.
            return self._adopt(skill_name=(skill or "").strip() or None)

        # Async verbs (dry-run, run, harvest) launch a background subprocess.
        # v1.8.1 fix: the CLI has no `--skill` flag (verified against
        # microsoft/skillopt — the flag is `--target-skill-path`), so the old
        # `--skill <name>` extra made the subprocess die on an argparse
        # error. Resolve the skill to its live SKILL.md path instead, and
        # omit the flag entirely when the skill dir doesn't exist.
        extra_args: list[str] = []
        if skill:
            try:
                from usr.plugins.skillopt.helpers import official_adapter  # type: ignore
                skill_path = official_adapter._resolve_skill_path(skill)
            except Exception:
                skill_path = None
            if skill_path:
                extra_args += ["--target-skill-path", skill_path]

        run = sleep_runner.launch_sleep_subprocess(verb, extra_args=extra_args)
        return Response(
            message=(
                f"Sleep cycle launched: verb={verb}, pid={run['pid']}, "
                f"log={run['log_path']}\n\n"
                f"The cycle runs in the background. Re-call with "
                f"verb=status to check progress, or tail the log "
                f"at {run['log_path']}."
            ),
            break_loop=False,
        )

    # ------------------------------------------------------------------ #

    def _status(self) -> Response:
        snap = sleep_runner.get_status_snapshot()
        # Augment with latest log tail for quick eyeballing
        runs_root = sleep_runner.runs_dir()
        last_log = None
        if runs_root.is_dir():
            logs = sorted(runs_root.glob("sleep-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            if logs:
                last_log = str(logs[0])
                snap["last_log"] = last_log
                snap["last_log_tail"] = sleep_runner.tail_log(last_log, max_bytes=2048)
                snap["last_log_held_out"] = sleep_runner.parse_held_out(logs[0])
        return Response(
            message=json.dumps(snap, indent=2, default=str),
            break_loop=False,
        )

    def _adopt(self, skill_name: str | None = None) -> Response:
        """Promote the latest staged proposal after running the validation gate.

        For v1.1.0+, adoption runs the shared validate_proposal() gate
        before copying. Rejections are reported back as a failure
        response so the user sees the reason.

        v1.8.36 (audit M8): this path now runs the SAME governance
        eligibility chain as the HTTP route and the background loop. It
        previously went straight from `find_staged_proposals()` to
        `validate_proposal()` to `adopt_write_skill()`, so an agent (or
        anything able to drive a tool call) could write a live SKILL.md for a
        skill carrying `.skillopt.optout`, `mode: immutable`,
        `.skillopt.pause_until`, `min_interval_seconds` or
        `daily_budget_cents` - all six rails the drain honours. The child DOX
        states the intent plainly: "the agent-callable adopt tool is as
        exposed as the background loop". It was not.
        """
        cfg = sleep_runner.merged_config()
        staged = sleep_runner.find_staged_proposals()
        # v1.8.45 (re-audit R12): resolve BY STEM EQUALITY when a
        # skill was named, and ERROR when it matches nothing - the
        # same rule `api/adopt.py` uses. Falling back to the newest
        # staged file would let a caller adopt a DIFFERENT skill
        # than the one they asked for.
        if skill_name:
            staged = [p for p in staged if p.stem == skill_name]
            if not staged:
                return Response(
                    message=(f"No staged proposal for skill "
                             f"{skill_name!r}; refusing to adopt a "
                             f"different one."),
                    break_loop=False,
                )
        if not staged:
            return Response(
                message=(
                    "No staged proposal found. Run `skillopt_sleep verb=dry-run` "
                    "or `verb=run` first to generate one."
                ),
                break_loop=False,
            )
        # Pick the most recently modified one
        staged.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        src = staged[0]
        skill_name = src.stem if src.suffix == ".md" else "unknown"
        proposed = src.read_text(encoding="utf-8")
        # v1.8.48 (audit U10): no `target`/`mkdir`/`current` before the
        # governance gate. `adopt_write_skill` creates the parent directory
        # itself, so the mkdir here was redundant AND a side effect ahead of
        # the decision - a refused or deferred adopt left an empty
        # `usr/skills/<name>/` behind. `api/adopt.py` removed exactly this in
        # v1.8.41; the tool kept it.
        current = ""

        # v1.8.36 (audit M8): governance first, exactly as the drain does.
        # Resolved with no argument so the operator's persisted mode is used
        # (see AGENTS.md Local Contracts, audit F1).
        # v1.8.41 (re-audit R1): delegate to the SAME helper the HTTP route
        # uses. This chain was written out longhand here in v1.8.36 and never
        # added to `api/adopt.py` — the divergence the re-audit found. Two
        # spellings of the same gate is one too many, so there is now one,
        # and the agent path and the human path cannot drift apart again.
        try:
            from usr.plugins.skillopt.api import adopt as _adopt_api  # type: ignore
        except Exception:
            try:
                from api import adopt as _adopt_api  # type: ignore
            except Exception:
                _adopt_api = None  # type: ignore[assignment]
        if _adopt_api is not None and hasattr(_adopt_api, '_check_governance'):
            _eligible, gov_reason, gov_error = _adopt_api._check_governance(
                skill_name)
        else:
            # Fall back to the local chain only if the route module cannot be
            # imported at all; the semantics are identical either way.
            try:
                from usr.plugins.skillopt.helpers import governance  # type: ignore
            except Exception:
                try:
                    from helpers import governance  # type: ignore
                except Exception:
                    return Response(
                        message=("Adoption ABORTED: the governance module is "
                                 "unavailable, so no rail could be evaluated. "
                                 "Refusing rather than writing a live skill "
                                 "with governance silently disabled."),
                        break_loop=False,
                    )
            try:
                _autonomous = bool(
                    governance.resolve_evolution_mode().get("autonomous"))
            except Exception:
                _autonomous = False
            try:
                _eligible, gov_reason = governance.check_skill_eligible(
                    skill_name, auto_adopt=_autonomous)
                gov_error = ""
            except Exception as e:
                # Fail CLOSED, matching the drain (audit F8).
                return Response(
                    message=(f"Adoption ABORTED for skill `{skill_name}`.\n"
                             f"  governance check raised: "
                             f"{type(e).__name__}: {e}\n"
                             f"  Refusing rather than writing a live skill "
                             f"with the rails unevaluated."),
                    break_loop=False,
                )
        if gov_error:
            return Response(
                message=(f"Adoption ABORTED for skill `{skill_name}`.\n"
                         f"  {gov_error}\n"
                         f"  Refusing rather than writing a live skill with "
                         f"the rails unevaluated."),
                break_loop=False,
            )
        if not _eligible:
            return Response(
                message=(f"Adoption REFUSED for skill `{skill_name}` by "
                         f"governance.\n"
                         f"  reason: {gov_reason}\n"
                         f"  The live SKILL.md was left untouched. Every rail "
                         f"the background loop honours applies here too."),
                break_loop=False,
            )

        # v1.8.36 (audit M7): real-gate single-flight, as the drain enforces.
        try:
            if sleep_runner.find_pending_real_gate_sidecar() is not None:
                return Response(
                    message=("Adoption DEFERRED: a real-gate replay is in "
                             "flight and adoption is single-flight. Try again "
                             "once it finishes."),
                    break_loop=False,
                )
        except Exception as _sf_err:
            # v1.8.50 (audit F4): this was a bare `pass`, so the comment's
            # claim - "the drain defers here" - was false: a single-flight
            # probe that raised let the adopt proceed, while the API route's
            # identical probe DEFERS on the same fault. Two callers, two
            # answers, and the permissive one was the one an agent drives
            # automatically. Fail CLOSED like the route, and say so.
            return Response(
                message=(f"Adoption DEFERRED: the real-gate single-flight "
                         f"probe failed ({_sf_err}), and an unverified probe is "
                         f"not a pass. Adoption is single-flight; try again "
                         f"once the gate state is readable."),
                break_loop=False,
            )

        last_log = _latest_sleep_log()
        held_out = sleep_runner.parse_held_out(last_log) if last_log else None
        # v1.8.48 (audit U10): read-only from here to the write. The target is
        # resolved after the gate so nothing touches the skills tree for a
        # refused or deferred adoption.
        target = sleep_runner.safe_skill_md(skill_name)
        current = ""
        if target.is_file():
            try:
                current = target.read_text(encoding="utf-8")
            except OSError:
                current = ""
        # v1.8.1: honour the official-gate provenance marker (see api/adopt.py).
        marker = sleep_runner.read_official_gate_marker(src)
        official_gated = bool(marker.get("official_gated")) if marker else False
        ok, reason = sleep_runner.validate_proposal(
            proposed,
            current,
            min_chars=int(cfg.get("gate_min_chars", 200)),
            min_improvement_pp=float(cfg.get("gate_min_improvement_pp", 0.0)),
            max_shrink_ratio=float(cfg.get("gate_max_shrink_ratio", 0.5)),
            held_out=held_out,
            # v1.8.36 (audit M8): the A/B harness stage needs this. The HTTP
            # route passes it and this path did not, so the two adopt paths
            # were running DIFFERENT gates.
            skill_name=skill_name,
            official_gated=official_gated,
        )
        if not ok:
            return Response(
                message=(
                    f"Validation gate REJECTED proposal for skill `{skill_name}`.\n"
                    f"  source: {src}\n"
                    f"  reason: {reason}\n"
                    f"  proposed_size: {len(proposed)} chars\n"
                    f"  current_size:  {len(current)} chars\n"
                    f"  held_out: {held_out}\n"
                ),
                break_loop=False,
            )
        # v1.8.24 (P0): snapshot + atomic replace. This path had neither, and
        # it is agent-callable, so an unguarded write here is the same
        # unrecoverable case the auto-loop had.
        _w = sleep_runner.adopt_write_skill(skill_name, proposed)
        if not _w.get("ok"):
            return Response(
                message=(
                    f"Adoption ABORTED for skill `{skill_name}`.\n"
                    f"  error: {_w.get('error')}\n"
                    f"  The live SKILL.md was left untouched."
                ),
                break_loop=False,
            )
        # v1.8.36 (audit M7): consume the proposal so the drain does not
        # re-attempt it on the next tick, and so no sidecar is orphaned.
        consumed = None
        try:
            moved = sleep_runner.consume_staged_proposal(src)
            consumed = str(moved) if moved is not None else None
        except Exception:
            consumed = None
        return Response(
            message=(
                f"Adopted proposal for skill `{skill_name}`.\n"
                f"  source: {src}\n"
                f"  target: {target}\n"
                f"  size: {target.stat().st_size if target.is_file() else 0} bytes\n"
                f"  reason: {reason}\n"
                f"  governance: {gov_reason}\n"
                f"  consumed_to: {consumed}\n"
                f"  at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                # v1.8.48 (audit U4): act on the writer's `redacted` flag. The
                # drain records it and the HTTP route surfaces it; the tool
                # said "Adopted" for a document that is NOT byte-identical to
                # what was proposed, with no trace. A flag nobody reads is the
                # same defect as a gate nobody runs.
                + ("  NOTE: output redaction altered the document before it "
                   "was written - the live file is not byte-identical to the "
                   "proposal.\n" if _w.get("redacted") else "")
            ),
            break_loop=False,
        )
