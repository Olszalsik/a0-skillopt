#!/usr/bin/env python3
'''SkillOpt - async real-executor confirmation gate worker (v1.8.22).

Runs the REAL replay counterfactual (helpers/replay_harness.run_counterfactual,
executor="real") for ONE staged proposal, detached from the auto-loop
thread, and writes the verdict into the proposal's real-gate sidecar
(<staged>.md.realgate.json). The auto-loop drain harvests the sidecar on
a later tick and adopts/quarantines on its verdict.

This worker NEVER adopts: it does not import or call the adopt path,
regardless of auto_adopt config (same contract as run_sleep_cycle.py).

IMPORTANT (v1.8.22 "TRAP A"): the worker's bare `helpers` import resolves
to the PLUGIN-LOCAL package, so `sleep_runner.merged_config()` inside the
worker CANNOT see config.json (the framework registry read fails silently
and returns {}). Every tunable knob is therefore CLI-passed by the
spawner (auto_loop._real_gate_spawn), which resolves merged_config
in-process where config.json IS visible. Do not "simplify" this back to
config reads — the 450s/3-task production budget would silently become
600s/4 again.

JSONL log (emit convention, mirrors run_sleep_cycle.py):
  logs/runs/real_gate_<ts>_<skill>.jsonl
  phases: start | per_task | verdict | summary

Exit codes: 0 = verdict produced (accepted OR rejected — the harvester
distinguishes via verdict.accepted) | 1 = infra failure (missing files,
worker crash; sidecar status=failed).
'''
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ''):
    sys.path.insert(0, str(_PLUGIN_ROOT))
try:
    from helpers import replay_harness, sleep_runner  # type: ignore
except ImportError:  # pragma: no cover - framework-runtime context
    from usr.plugins.skillopt.helpers import replay_harness, sleep_runner  # type: ignore

_LOG_PATH: Path | None = None


def emit(phase: str, **kw) -> dict:
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'phase': phase}
    rec.update(kw)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    if _LOG_PATH is not None:
        try:
            with _LOG_PATH.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except Exception:  # noqa: BLE001
            pass
    return rec


def _write_sidecar(sidecar: Path, payload: dict) -> None:
    """Write-then-rename so the harvester never reads torn JSON."""
    tmp = sidecar.with_suffix(sidecar.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    tmp.replace(sidecar)


def _evalkit_command() -> tuple[list[str] | None, str | None]:
    command = [sys.executable, "-m", "skillopt_sleep.evalkit"]
    try:
        help_result = subprocess.run(
            command + ["--help"], capture_output=True, text=True, timeout=15,
            check=False,
        )
    except Exception as exc:
        return None, "capability_probe_failed:" + type(exc).__name__
    help_text = (help_result.stdout or "") + (help_result.stderr or "")
    required = (
        "--manifest", "--a", "--b", "--allow-graded", "--boot", "--seed", "--json"
    )
    if help_result.returncode != 0 or any(flag not in help_text for flag in required):
        return None, "installed_evalkit_missing_required_options"
    return command, None


def _evalkit_report(per_task: list[dict], command: list[str] | None = None) -> dict:
    """Run upstream's optional paired evalkit over already-scored pairs.

    This is a post-hoc report only. Any discovery, input, timeout, or CLI
    failure is recorded as unavailable and cannot change the gate verdict.
    """
    pairs = []
    seen = set()
    for row in per_task:
        task_id = row.get("id")
        a = row.get("current")
        b = row.get("proposed")
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            return {"available": False, "reason": "invalid_or_duplicate_task_ids"}
        if (isinstance(a, bool) or isinstance(b, bool)
                or not isinstance(a, (int, float)) or not isinstance(b, (int, float))
                or not math.isfinite(a) or not math.isfinite(b)
                or not 0 <= a <= 1 or not 0 <= b <= 1):
            return {"available": False, "reason": "invalid_paired_scores"}
        seen.add(task_id)
        # The evalkit needs stable pairing only within this invocation. Do
        # not copy rollout IDs, which may contain user data, into its report.
        pairs.append((f"task-{len(pairs) + 1:04d}", float(a), float(b)))
    if not pairs:
        return {"available": False, "reason": "no_paired_scores"}

    if command is None:
        command, reason = _evalkit_command()
        if command is None:
            return {"available": False, "reason": reason or "evalkit_unavailable"}

    try:
        with tempfile.TemporaryDirectory(prefix="skillopt-evalkit-") as tmp_dir:
            tmp = Path(tmp_dir)
            manifest = tmp / "manifest.json"
            arm_a = tmp / "current.json"
            arm_b = tmp / "proposed.json"
            manifest.write_text(json.dumps([p[0] for p in pairs]), encoding="utf-8")
            arm_a.write_text(json.dumps({p[0]: p[1] for p in pairs}), encoding="utf-8")
            arm_b.write_text(json.dumps({p[0]: p[2] for p in pairs}), encoding="utf-8")
            result = subprocess.run(
                command + [
                    "--manifest", str(manifest), "--a", str(arm_a),
                    "--b", str(arm_b), "--allow-graded", "--boot", "10000",
                    "--seed", "42", "--json",
                ],
                capture_output=True, text=True, timeout=90, check=False,
            )
        if result.returncode != 0:
            return {
                "available": True, "ok": False,
                "reason": "evalkit_exit_" + str(result.returncode),
                "detail": (result.stderr or result.stdout or "")[:500],
            }
        report = json.loads(result.stdout)
        if not isinstance(report, dict):
            return {"available": True, "ok": False, "reason": "evalkit_returned_non_object"}
        # Restrict the persisted report to JSON data and keep its size bounded.
        compact = json.loads(json.dumps(report, allow_nan=False))
        encoded = json.dumps(compact, ensure_ascii=False)
        if len(encoded) > 12000:
            return {"available": True, "ok": False, "reason": "evalkit_report_too_large"}
        return {"available": True, "ok": True, "n": len(pairs), "report": compact}
    except Exception as exc:
        return {"available": True, "ok": False, "reason": type(exc).__name__ + ":" + str(exc)[:300]}


def _independent_evalkit_report(
    tasks_file: str, skill: str, current_md: str, proposed_md: str,
    gate_task_ids: set[str], cfg: dict,
) -> dict:
    """Score a curated fixed task manifest separate from the adoption gate."""
    path = Path(tasks_file).expanduser()
    try:
        raw = path.read_bytes()
        if len(raw) > 1_000_000:
            return {"available": False, "reason": "manifest_too_large"}
        tasks = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"available": False, "reason": "manifest_read_failed:" + type(exc).__name__}
    if not isinstance(tasks, list):
        return {"available": False, "reason": "manifest_not_a_list"}
    max_tasks = max(1, min(20, int(cfg.get("replay_evalkit_max_tasks", 4) or 4)))
    if len(tasks) > max_tasks:
        return {"available": False, "reason": "manifest_exceeds_max_tasks"}
    seen: set[str] = set()
    for task in tasks:
        task_id = task.get("id") if isinstance(task, dict) else None
        task_text = task.get("task") if isinstance(task, dict) else None
        if (not isinstance(task_id, str) or not task_id.strip()
                or task_id != task_id.strip() or len(task_id) > 256
                or task_id in seen):
            return {"available": False, "reason": "invalid_or_duplicate_manifest_ids"}
        if not isinstance(task_text, str) or not task_text.strip():
            return {"available": False, "reason": "manifest_task_text_missing"}
        if task_id in gate_task_ids:
            return {"available": False, "reason": "manifest_overlaps_gate_tasks"}
        seen.add(task_id)
    if len(tasks) < int(cfg.get("replay_min_n", 3) or 3):
        return {"available": False, "reason": "manifest_below_replay_min_n"}
    if cfg.get("replay_real_executor_enabled") is not True:
        return {"available": False, "reason": "independent_evalkit_requires_real_executor"}

    command, reason = _evalkit_command()
    if command is None:
        return {"available": False, "reason": reason or "evalkit_unavailable"}

    scored = replay_harness.run_counterfactual(
        skill_name=skill,
        current_skill_md=current_md,
        proposed_skill_md=proposed_md,
        held_out_tasks=tasks,
        executor="real",
        config={**cfg, "replay_real_max_tasks": max_tasks},
    )
    if not scored.get("ok"):
        return {"available": False, "reason": "independent_replay_failed:" + str(scored.get("reason", "unknown"))[:250]}
    report = _evalkit_report(scored.get("per_task") or [], command=command)
    return {
        "mode": "independent_fixed_manifest",
        "available": bool(report.get("available")),
        "ok": bool(report.get("ok")),
        "executor": "real",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "n": scored.get("n"),
        "paired_report": report,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='SkillOpt async real-gate worker')
    ap.add_argument('--skill-name', required=True)
    ap.add_argument('--staged-path', required=True,
                    help='absolute path of the staged proposal .md')
    ap.add_argument('--sidecar', required=True,
                    help='absolute path of the .md.realgate.json sidecar to update')
    ap.add_argument('--tasks-file', required=True,
                    help='absolute path of the frozen held-out tasks json (list)')
    # Knobs are CLI-passed because merged_config() cannot see config.json
    # from inside the worker process (TRAP A, module docstring).
    ap.add_argument('--executor', default='real', choices=('real', 'mock'),
                    help="mock: hermetic test path (deterministic, no LLM)")
    ap.add_argument('--per-task-timeout-s', type=float, default=600.0)
    ap.add_argument('--max-tasks', type=int, default=3)
    ap.add_argument('--gate-min-improvement-pp', type=float, default=5.0)
    ap.add_argument('--replay-min-n', type=int, default=3)
    ap.add_argument('--evalkit', action='store_true',
                    help='attach an optional official paired evalkit report')
    ap.add_argument('--evalkit-tasks-file', default=None,
                    help='optional independent reviewed JSON task list')
    ap.add_argument('--evalkit-max-tasks', type=int, default=4)
    ap.add_argument('--log-dir', default=None,
                    help='JSONL log dir (default: sleep_runner.runs_dir())')
    args = ap.parse_args()

    sidecar = Path(args.sidecar)
    staged = Path(args.staged_path)
    skill = args.skill_name

    # Read the pending sidecar to preserve spawn-time metadata.
    try:
        payload = json.loads(sidecar.read_text(encoding='utf-8'))
    except Exception:
        payload = {'real_gate': True, 'schema': 1}
    payload.update({
        'skill': skill,
        'proposal_path': str(staged),
        'status': 'pending',
    })

    log_dir = Path(args.log_dir) if args.log_dir else sleep_runner.runs_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime('%Y%m%dT%H%M%S')
        global _LOG_PATH
        _LOG_PATH = log_dir / f'real_gate_{ts}_{skill}.jsonl'
        payload['jsonl_path'] = str(_LOG_PATH)
        _write_sidecar(sidecar, payload)
        emit('start', skill=skill, staged_path=str(staged),
             executor=args.executor, per_task_timeout_s=args.per_task_timeout_s,
             max_tasks=args.max_tasks, replay_min_n=args.replay_min_n)
    except Exception as e:  # noqa: BLE001 - log setup must never kill the run
        print(f'[skillopt] real-gate worker: log setup failed: {e}', flush=True)

    # Infra guards: the proposal must still exist. The held-out set is
    # NOT re-derived from rollouts here (drift between spawn-time
    # selection and run-time measurement is structurally eliminated).
    try:
        if not staged.is_file():
            raise RuntimeError(f'staged proposal missing: {staged}')
        tasks_path = Path(args.tasks_file or payload.get('tasks_file') or '')
        if not tasks_path.is_file():
            # v1.8.48 (audit C4): `--tasks-file` is declared REQUIRED by this
            # module's own TRAP-A contract ("every tunable knob is CLI-passed
            # by the spawner") yet nothing read `args.tasks_file`. The path
            # was taken only from the SIDECAR, so when the sidecar could not
            # be read the code fell back to `Path('')` -> `Path('.')` and the
            # worker died with `frozen tasks file missing: .` even though the
            # correct absolute path was sitting in argv - exiting 1 with
            # `status: failed`, i.e. the documented fail-OPEN adopt.
            raise RuntimeError(f'frozen tasks file missing: {tasks_path}')
        held_out = json.loads(tasks_path.read_text(encoding='utf-8'))
        if not isinstance(held_out, list):
            raise RuntimeError('tasks file is not a json list')

        live_path = sleep_runner.safe_skill_md(skill)
        if not live_path.is_file():
            raise RuntimeError(f'live SKILL.md missing: {live_path}')
        current_md = live_path.read_text(encoding='utf-8')
        proposed_md = staged.read_text(encoding='utf-8')

        cfg = {
            'replay_real_executor_enabled': args.executor == 'real',
            'replay_real_per_task_timeout_s': args.per_task_timeout_s,
            'replay_real_max_tasks': args.max_tasks,
            'gate_min_improvement_pp': args.gate_min_improvement_pp,
            'replay_min_n': args.replay_min_n,
        }
        verdict = replay_harness.run_counterfactual(
            skill_name=skill,
            current_skill_md=current_md,
            proposed_skill_md=proposed_md,
            held_out_tasks=held_out,
            executor=args.executor,
            config=cfg,
        )
        for pt in (verdict.get('per_task') or []):
            emit('per_task', **pt)
        payload['status'] = 'done'
        payload['verdict'] = verdict
        if args.evalkit and verdict.get('ok'):
            try:
                if args.evalkit_tasks_file:
                    gate_ids = {
                        str(row.get('id')) for row in (verdict.get('per_task') or [])
                        if isinstance(row, dict) and row.get('id') is not None
                    }
                    eval_cfg = {
                        'replay_real_executor_enabled': args.executor == 'real',
                        'replay_real_per_task_timeout_s': args.per_task_timeout_s,
                        'replay_real_max_tasks': args.evalkit_max_tasks,
                        'replay_min_n': args.replay_min_n,
                        'gate_min_improvement_pp': args.gate_min_improvement_pp,
                        'replay_evalkit_max_tasks': args.evalkit_max_tasks,
                    }
                    payload['evalkit'] = _independent_evalkit_report(
                        args.evalkit_tasks_file, skill, current_md, proposed_md,
                        gate_ids, eval_cfg,
                    )
                else:
                    payload['evalkit'] = {
                        'mode': 'gate_pairs_descriptive',
                        **_evalkit_report(verdict.get('per_task') or []),
                    }
            except Exception as eval_exc:
                # Reporting is strictly post-gate and must never turn a
                # measured pass/reject into an infrastructure failure.
                payload['evalkit'] = {
                    'available': False,
                    'reason': 'report_error:' + type(eval_exc).__name__,
                }
        # v1.8.23: gate_passed now reflects the verdict: True iff the real
        # gate MEASURED a result (verdict.ok); False on could-not-measure.
        payload['gate_passed'] = bool(verdict.get('ok'))
        payload['finished_ts'] = time.time()
        payload['error'] = None
        _write_sidecar(sidecar, payload)
        emit('verdict', accepted=verdict.get('accepted'),
             lift_pp=verdict.get('lift_pp'), n=verdict.get('n'),
             reason=verdict.get('reason'))
        emit('summary', verdict=('accepted' if verdict.get('accepted')
                                 else 'rejected'), exit_code=0)
        return 0
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - any failure -> failed sidecar
        payload['status'] = 'failed'
        payload['error'] = f'{type(e).__name__}: {e}'[:500]
        # v1.8.23: a failed worker produced no verdict — say so.
        payload['gate_passed'] = False
        payload['finished_ts'] = time.time()
        _write_sidecar(sidecar, payload)
        emit('summary', verdict='failed', exit_code=1,
             error=payload['error'],
             traceback=traceback.format_exc(limit=5))
        return 1


if __name__ == '__main__':
    sys.exit(main())
