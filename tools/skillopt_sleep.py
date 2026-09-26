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
            return self._adopt()

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

    def _adopt(self) -> Response:
        """Promote the latest staged proposal after running the validation gate.

        For v1.1.0+, adoption runs the shared validate_proposal() gate
        before copying. Rejections are reported back as a failure
        response so the user sees the reason.
        """
        staged = sleep_runner.find_staged_proposals()
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
        target = sleep_runner.a0_skills_dir() / skill_name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        proposed = src.read_text(encoding="utf-8")
        current = ""
        if target.is_file():
            current = target.read_text(encoding="utf-8")

        cfg = sleep_runner.merged_config()
        last_log = _latest_sleep_log()
        held_out = sleep_runner.parse_held_out(last_log) if last_log else None
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
        # v1.8.1: proposal consumed — clear its provenance marker.
        try:
            sleep_runner.clear_official_gate_marker(src)
        except Exception:
            pass
        return Response(
            message=(
                f"Adopted proposal for skill `{skill_name}`.\n"
                f"  source: {src}\n"
                f"  target: {target}\n"
                f"  size: {target.stat().st_size} bytes\n"
                f"  reason: {reason}\n"
                f"  at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
            ),
            break_loop=False,
        )
