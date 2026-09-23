'''SkillOpt - judge LLM client (v1.8.12 follow-up).

Reliable resolution of the judge LLM provider/endpoint configuration
plus a bounded connectivity probe, with graceful descriptive errors and
no API key material in any returned dict.

The canonical scoring path stays helpers/llm_judge.judge_outcome (which
routes through direct_optimizer._call_llm). This module mirrors that
exact resolution chain so a probe can never disagree with the call it
validates:

1. logs/runs/.skillopt-env -> AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY
2. container env           -> OLLAMA_API_KEY / API_KEY_OLLAMA_CLOUD
3. chat sentinel (v1.8.12) -> helpers/chat_model.effective_model()
   (the active Agent Zero chat model, incl. provider api_base/api_key)

SKILLOPT_JUDGE_ENDPOINT is surfaced as advisory only: in v1.8.12 it is
documented for the retired synthetic A/B judge and is NOT wired into the
live judge path.

Every public function returns an {ok: bool, ...} dict and never raises.
Label persistence intentionally stays with llm_judge.label_rollout_file /
scripts/label_rollouts.py (explicit operator action).
'''
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

try:  # two-path import: framework runtime vs repo/installed bare layout
    from usr.plugins.skillopt.helpers import chat_model as _cm
except ImportError:  # pragma: no cover
    from helpers import chat_model as _cm  # type: ignore

try:
    from usr.plugins.skillopt.helpers import direct_optimizer as _do
except ImportError:  # pragma: no cover
    from helpers import direct_optimizer as _do  # type: ignore

try:
    from usr.plugins.skillopt.helpers import llm_judge as _lj
except ImportError:  # pragma: no cover
    from helpers import llm_judge as _lj  # type: ignore


def _resolve_raw(model_hint: str | None = None) -> dict[str, Any]:
    '''Mirror direct_optimizer._call_llm resolution exactly. Never raises.'''
    out: dict[str, Any] = {'model': '', 'base_url': '', 'api_key': '', 'key_source': ''}
    try:
        env = _do._read_env_file()
        base = env.get('AZURE_OPENAI_ENDPOINT') or 'https://ollama.com/v1'
        key = env.get('AZURE_OPENAI_API_KEY') or ''
        src = 'skillopt-env' if key else ''
        if not key:
            key = os.environ.get('OLLAMA_API_KEY') or os.environ.get('API_KEY_OLLAMA_CLOUD') or ''
            if key:
                src = 'container-env'
        try:
            raw = _lj._judge_model(model_hint)
        except Exception as e:  # noqa: BLE001
            raw = ''
            out['resolution_error'] = type(e).__name__ + ': ' + str(e)
        # _call_llm re-resolves whatever it receives; mirror that (empty
        # resolves via the chat sentinel again, concrete passes through).
        model, conn = _cm.effective_model(raw or '')
        if conn:
            if conn.get('api_base'):
                base = str(conn['api_base'])
            if conn.get('api_key'):
                key = str(conn['api_key'])
                src = 'chat-provider'
        out['model'] = str(model or '')
        out['base_url'] = str(base)
        out['api_key'] = str(key or '')
        out['key_source'] = src
    except Exception as e:  # noqa: BLE001
        out['resolution_error'] = type(e).__name__ + ': ' + str(e)
    return out


def resolve_judge_endpoint(model_hint: str | None = None) -> dict[str, Any]:
    '''Describe the endpoint/model/key the judge calls will actually use.

    Masked: never includes the API key itself. ok=False carries a
    descriptive, human-readable reason per missing piece.
    '''
    raw = _resolve_raw(model_hint)
    errs: list[str] = []
    if raw.get('resolution_error'):
        errs.append('resolution: ' + raw['resolution_error'])
    if not raw.get('model'):
        errs.append('no judge model: SKILLOPT_JUDGE_MODEL / SKILLOPT_OPTIMIZER_MODEL unset and the chat sentinel found no active chat model')
    if not raw.get('api_key'):
        errs.append('no API key: set AZURE_OPENAI_API_KEY in logs/runs/.skillopt-env or OLLAMA_API_KEY in the container env')
    env_file = ''
    try:
        ef = _do.sleep_runner.runs_dir() / '.skillopt-env'
        if ef.is_file():
            env_file = str(ef)
    except Exception:  # noqa: BLE001
        pass
    return {
        'ok': bool(raw.get('model')) and bool(raw.get('api_key')) and bool(raw.get('base_url')),
        'model': raw.get('model', ''),
        'base_url': raw.get('base_url', ''),
        'api_key_set': bool(raw.get('api_key')),
        'api_key_source': raw.get('key_source', ''),
        'env_file': env_file,
        'advisory_judge_endpoint': os.environ.get('SKILLOPT_JUDGE_ENDPOINT', ''),
        'advisory_note': 'SKILLOPT_JUDGE_ENDPOINT is not wired into the live judge path in v1.8.12; listed for visibility only',
        'errors': errs,
    }


def ping_judge(model_hint: str | None = None, timeout_s: float = 45.0) -> dict[str, Any]:
    '''Bounded connectivity probe using the exact judge resolution.

    ok=True means the endpoint answered HTTP 200; content_ok=False with
    ok=True flags a reasoning-style model that returned empty content.
    '''
    res = resolve_judge_endpoint(model_hint)
    out: dict[str, Any] = {
        'ok': False, 'model': res.get('model', ''), 'base_url': res.get('base_url', ''),
        'latency_s': None, 'content_snippet': '', 'content_ok': False,
        'resolution': res, 'error': '', 'warning': '',
    }
    if not res.get('ok'):
        out['error'] = 'judge endpoint resolution failed: ' + '; '.join(res.get('errors') or ['unknown'])
        return out
    raw = _resolve_raw(model_hint)
    t0 = time.monotonic()
    try:
        from openai import OpenAI
        client = OpenAI(base_url=raw['base_url'], api_key=raw['api_key'], timeout=float(timeout_s), max_retries=1)
        resp = client.chat.completions.create(
            model=raw['model'],
            messages=[{'role': 'user', 'content': 'Connectivity probe. Reply with the single word: PONG'}],
            max_tokens=400,
            temperature=0.0,
        )
        content = str((resp.choices[0].message.content or '')).strip()
        out['latency_s'] = round(time.monotonic() - t0, 2)
        out['content_snippet'] = content[:80]
        out['content_ok'] = bool(content)
        out['ok'] = True
        if not content:
            out['warning'] = 'endpoint reachable but completion content empty (reasoning-style model may consume hidden tokens); judge JSON parsing may need retries'
    except Exception as e:  # noqa: BLE001
        out['latency_s'] = round(time.monotonic() - t0, 2)
        out['error'] = 'judge endpoint unreachable (base_url=' + raw['base_url'] + ', model=' + raw['model'] + '): ' + type(e).__name__ + ': ' + str(e)[:400]
    return out


_JUDGE_SYSTEM = getattr(_lj, 'JUDGE_SYSTEM', 'You are a strict rollout outcome judge. Reply with JSON only.')

def judge_rollout(rollout: dict, model: str | None = None) -> dict[str, Any]:
    '''Canonical judge scoring of one rollout, never raises.

    Reasoning-style judge models can exhaust the hardcoded max_tokens=300
    budget on hidden reasoning and answer with EMPTY content, which
    surfaces as a json-parse error. On exactly that failure class this
    wrapper retries the same prompt/system through direct_optimizer
    ._call_llm with escalating token budgets: the resolved judge model at
    800 then 1500 tokens, then the chat sentinel (active Agent Zero chat
    model) at 1500 as a last resort. Provenance records the model and
    budget that actually produced the label. Nothing is persisted here.
    '''
    base = _lj.judge_outcome(rollout, model=model)
    if base.get('label') is not None:
        return base
    err = str(base.get('error') or '')
    if 'json parse' not in err and 'empty' not in err.lower():
        return base
    judge_name = str(base.get('model') or '')
    candidates: list = []
    if judge_name:
        candidates += [(judge_name, 800), (judge_name, 6000)]
    candidates.append(('', 6000))
    last: dict[str, Any] = base
    for cand_model, mt in candidates:
        try:
            resolved, _conn = _cm.effective_model(cand_model)
            if not resolved:
                last = {'label': None, 'error': 'escalate@' + str(mt) + ': model resolution failed'}
                continue
            prompt = _lj._build_judge_prompt(rollout)
            raw = _do._call_llm(prompt, resolved, max_tokens=mt, system=_JUDGE_SYSTEM)
            parsed = _lj._parse_judge_response(raw)
        except Exception as e:  # noqa: BLE001
            last = {'label': None, 'error': 'escalate@' + str(mt) + ': ' + type(e).__name__ + ': ' + str(e)[:200]}
            continue
        if parsed.get('label') is not None:
            parsed['model'] = resolved
            parsed['escalated_max_tokens'] = mt
            return parsed
        last = {'label': None, 'error': 'escalate@' + str(mt) + ': ' + str(parsed.get('error') or 'no label in response')}
    return last


def judge_rollouts(paths: list, limit: int | None = None, model: str | None = None,
                   prefer_unlabeled: bool = True) -> dict[str, Any]:
    '''Judge up to `limit` rollouts; results are NOT persisted to disk.

    Prefers unlabeled rollouts (the ones the pipeline still needs).
    Returns {ok, evaluated, valid, results[], error} where valid counts
    results carrying a non-None label.
    '''
    out: dict[str, Any] = {'ok': False, 'evaluated': 0, 'valid': 0, 'results': [], 'error': ''}
    cands: list[dict] = []
    for p in list(paths or []):
        try:
            rec = json.loads(Path(p).read_text(encoding='utf-8'))
        except Exception as e:  # noqa: BLE001
            out['results'].append({'file': os.path.basename(str(p)), 'label': None,
                                   'error': 'read: ' + type(e).__name__ + ': ' + str(e)[:120]})
            continue
        if not isinstance(rec, dict):
            continue
        labeled = rec.get('judge_label') in _lj._VALID_LABELS
        cands.append({'path': p, 'rec': rec, 'labeled': labeled})
    unlabeled = [c for c in cands if not c['labeled']]
    pool = unlabeled if (prefer_unlabeled and unlabeled) else cands
    if limit is not None:
        pool = pool[: max(0, int(limit))]
    if not pool:
        out['error'] = 'no rollouts available to judge'
        return out
    for c in pool:
        r = judge_rollout(c['rec'], model=model)
        item: dict[str, Any] = {
            'file': os.path.basename(str(c['path'])),
            'labeled_before': c['labeled'],
            'label': r.get('label'),
            'confidence': r.get('confidence'),
            'reason': str(r.get('reason') or '')[:160],
            'model': r.get('model'),
            'escalated_max_tokens': r.get('escalated_max_tokens'),
        }
        if r.get('error'):
            item['error'] = r.get('error')
        out['results'].append(item)
    out['evaluated'] = len(pool)
    out['valid'] = sum(1 for r in out['results'] if r.get('label'))
    out['ok'] = out['valid'] > 0
    if not out['ok']:
        out['error'] = 'judge produced no valid labels across ' + str(out['evaluated']) + ' rollout(s)'
    return out
