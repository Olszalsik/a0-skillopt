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
import json
import sys
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
    ap.add_argument('--per-task-timeout-s', type=float, default=450.0)
    ap.add_argument('--max-tasks', type=int, default=3)
    ap.add_argument('--gate-min-improvement-pp', type=float, default=5.0)
    ap.add_argument('--replay-min-n', type=int, default=3)
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
        tasks_path = Path(payload.get('tasks_file') or '')
        if not tasks_path.is_file():
            raise RuntimeError(f'frozen tasks file missing: {tasks_path}')
        held_out = json.loads(tasks_path.read_text(encoding='utf-8'))
        if not isinstance(held_out, list):
            raise RuntimeError('tasks file is not a json list')

        live_path = sleep_runner.a0_skills_dir() / skill / 'SKILL.md'
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
        payload['finished_ts'] = time.time()
        _write_sidecar(sidecar, payload)
        emit('summary', verdict='failed', exit_code=1,
             error=payload['error'],
             traceback=traceback.format_exc(limit=5))
        return 1


if __name__ == '__main__':
    sys.exit(main())