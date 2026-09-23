#!/usr/bin/env python3
'''SkillOpt - gated sleep cycle runner (v1.8.12 follow-up).

End-to-end, bounded execution of the judge -> held-out gate ->
human-approval pipeline against the live harvested rollouts:

  ingestion : count live rollouts, select the target skill
  judge     : resolve the judge endpoint, bounded connectivity probe,
              then judge a small sample of rollouts (never persisted)
  gate      : run the OFFICIAL skillopt_sleep engine cycle; its own
              monotonic held-out gate is authoritative (report.json is
              the verdict source); NO direct-optimizer fallback
  staging   : verify the staged proposal + official gate marker and
              report PENDING_HUMAN_APPROVAL

This runner NEVER adopts: it does not import or call the adopt path,
regardless of auto_adopt config. Adoption stays a separate human action
(/adopt or governance approve in the WebUI).

Structured JSON lines are printed to stdout and appended to
logs/runs/gated_cycle_<ts>.jsonl (inside the plugin only).

Exit codes: 0 staged (pending approval) | 1 infra failure | 2 judge
failure (unreachable endpoint or no valid labels) | 3 official gate
rejected | 4 no rollouts.
'''
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if __package__ in (None, ''):
    sys.path.insert(0, str(_PLUGIN_ROOT))
try:
    from helpers import judge_client, llm_judge, official_adapter, sleep_runner  # type: ignore
except ImportError:  # pragma: no cover - framework-runtime context
    from usr.plugins.skillopt.helpers import judge_client, llm_judge, official_adapter, sleep_runner  # type: ignore

_LOG: list = []
_LOG_PATH: Path | None = None


def emit(phase: str, **kw) -> dict:
    rec = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'phase': phase}
    rec.update(kw)
    _LOG.append(rec)
    line = json.dumps(rec, ensure_ascii=False, default=str)
    print(line, flush=True)
    if _LOG_PATH is not None:
        try:
            with _LOG_PATH.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        except Exception:  # noqa: BLE001
            pass
    return rec


def finish(code: int, verdict: str, **kw) -> int:
    emit('summary', verdict=verdict, exit_code=code, **kw)
    return code


def _rollout_skill(rec: dict) -> str:
    return str(rec.get('skill_used') or rec.get('skill') or rec.get('skill_name') or '').strip()


def main() -> int:
    global _LOG_PATH
    ap = argparse.ArgumentParser(description='Run one gated SkillOpt sleep cycle end-to-end (never adopts).')
    ap.add_argument('--skill', default='', help='target skill name (default: the live skill with the most rollouts)')
    ap.add_argument('--judge-sample', type=int, default=3, help='max rollouts to judge in the judge phase')
    ap.add_argument('--timeout', type=int, default=600, help='official engine poll timeout in seconds')
    ap.add_argument('--skip-judge', action='store_true', help='skip the judge phase (hermetic/CI use only)')
    args = ap.parse_args()

    runs_dir = sleep_runner.runs_dir()
    _LOG_PATH = runs_dir / ('gated_cycle_' + time.strftime('%Y%m%dT%H%M%S') + '.jsonl')
    emit('start', plugin_root=str(sleep_runner.plugin_root()), log_file=str(_LOG_PATH),
         python=sys.executable, argv=' '.join(sys.argv[1:]))

    # ---- phase: ingestion ------------------------------------------------
    rdir = sleep_runner.rollouts_dir()
    files = sleep_runner.list_rollouts()
    counts: dict = {}
    parsed = 0
    labeled = 0
    for f in files:
        try:
            rec = json.loads(Path(f).read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(rec, dict):
            continue
        parsed += 1
        if rec.get('judge_label') in llm_judge._VALID_LABELS:
            labeled += 1
        name = _rollout_skill(rec)
        if name:
            counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    if args.skill:
        target = args.skill
    else:
        target = ''
        for name, _cnt in top:
            if official_adapter._resolve_skill_path(name):
                target = name
                break
    target_path = ''
    if target:
        try:
            target_path = official_adapter._resolve_skill_path(target) or ''
        except Exception:  # noqa: BLE001
            target_path = ''
    emit('ingestion', rollouts_dir=str(rdir), total=len(files), parsed=parsed,
         labeled=labeled, unlabeled=parsed - labeled, distinct_skills=len(counts),
         top_skills=[[n, c] for n, c in top[:5]],
         target_skill=target, target_skill_path=target_path)
    if not files:
        return finish(4, 'NO_ROLLOUTS', detail='no live rollouts found at ' + str(rdir))

    # ---- phase: judge ----------------------------------------------------
    if args.skip_judge:
        emit('judge', skipped=True, note='judge phase skipped by --skip-judge (hermetic run)')
    else:
        res = judge_client.resolve_judge_endpoint()
        emit('judge_endpoint', ok=res['ok'], model=res['model'], base_url=res['base_url'],
             api_key_set=res['api_key_set'], api_key_source=res['api_key_source'],
             env_file=res['env_file'], errors=res['errors'])
        ping = judge_client.ping_judge(timeout_s=45.0)
        emit('judge_ping', ok=ping['ok'], model=ping['model'], base_url=ping['base_url'],
             latency_s=ping['latency_s'], content_ok=ping['content_ok'],
             content_snippet=ping['content_snippet'], warning=ping['warning'], error=ping['error'])
        if not ping['ok']:
            return finish(2, 'JUDGE_FAILED', detail='judge endpoint unreachable: ' + ping['error'])
        ordered = []
        for f in reversed(files):  # newest first
            try:
                rec = json.loads(Path(f).read_text(encoding='utf-8'))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(rec, dict):
                continue
            if target:
                used = _rollout_skill(rec)
                if used and used != target:
                    continue
            ordered.append(f)
        jr = judge_client.judge_rollouts(ordered, limit=max(1, int(args.judge_sample)))
        emit('judge_evaluations', ok=jr['ok'], evaluated=jr['evaluated'], valid=jr['valid'],
             results=jr['results'], error=jr['error'])
        if not jr['ok']:
            return finish(2, 'JUDGE_FAILED', detail=jr['error'])

    # ---- phase: gate (official engine, authoritative) --------------------
    cfg = sleep_runner.merged_config()
    verb = str(cfg.get('official_run_verb') or 'run')
    backend = str(cfg.get('official_backend') or 'engine-default (mock)')
    emit('gate_start', engine='official', verb=verb, backend=backend, target=target,
         timeout_s=int(args.timeout),
         note='official held-out gate is authoritative; no ungated direct fallback in this runner')
    t0 = time.monotonic()
    res = official_adapter.run_official_sleep_cycle(target=target, cfg=cfg, timeout_s=int(args.timeout))
    dur = round(time.monotonic() - t0, 1)
    gate = res.get('gate') or {}
    emit('gate_result', ok=res.get('ok'), engine=res.get('engine'), duration_s=dur,
         gate_rejected=res.get('gate_rejected'), fallback_to_direct=res.get('fallback_to_direct'),
         reason=res.get('reason'), gate=gate, held_out=res.get('held_out'),
         pid=res.get('pid'), log_path=res.get('log_path'),
         official_staging_dir=res.get('official_staging_dir'))
    if res.get('fallback_to_direct'):
        return finish(1, 'INFRA_FAILED',
                      detail='official engine infra failure: ' + str(res.get('reason')) + ' (no ungated direct fallback)')
    if res.get('gate_rejected'):
        return finish(3, 'GATE_REJECTED', detail=str(res.get('reason')), gate=gate)
    if not res.get('ok'):
        return finish(1, 'INFRA_FAILED',
                      detail='official engine returned unexpected result: ' + json.dumps(res, default=str)[:400])

    # ---- phase: staging / human approval ---------------------------------
    staged_path = res.get('staged_path') or ''
    staged_size = res.get('staged_size') or 0
    marker = None
    if staged_path:
        try:
            marker = sleep_runner.read_official_gate_marker(staged_path)
        except Exception as e:  # noqa: BLE001
            marker = {'error': type(e).__name__ + ': ' + str(e)}
    gcfg = (cfg.get('governance') or {}).get('default_policy') or {}
    auto_adopt = bool(cfg.get('auto_adopt'))
    require_human = bool(gcfg.get('require_human_approval', True))
    emit('staging', staged_path=staged_path, staged_size=staged_size,
         official_gate_marker=marker, auto_adopt=auto_adopt,
         require_human_approval=require_human, status='PENDING_HUMAN_APPROVAL',
         adopt_invoked=False,
         note='adoption is a separate human action (/adopt or governance approve); this runner never adopts')
    if not staged_path:
        return finish(1, 'INFRA_FAILED', detail='gate accepted but no staged path returned')
    return finish(0, 'STAGED',
                  next_action='review ' + staged_path + ' then adopt via the WebUI /adopt endpoint')


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
