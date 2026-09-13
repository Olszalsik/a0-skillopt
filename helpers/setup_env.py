'''Credential-safe builder/sanitizer for the SkillOpt env file.

v1.8.5 SECURITY: extracted from tools/skillopt_setup.py so the smoke
suite can exercise the logic without importing framework Tool classes.

Contract (completes the v1.8.4 architecture):
- Credential values live ONLY in <project>/usr/.env (chmod 600, outside
  the plugin repo). logs/runs/.skillopt-env holds ${VAR} references for
  credential-ish keys plus non-secret literals (api version, auth mode,
  backend).
- sanitize_env_text() rewrites any plaintext credential value found in
  an existing file to a ${VAR} reference (provenance match against
  currently visible source variables when possible, self-${KEY}
  otherwise), so legacy files and re-runs converge to reference-only
  form instead of regressing.
- apply() never raises: it returns a structured {ok, error} dict so the
  tool can fail loud, not crash (roadmap principle 3).
- Values are never logged or echoed by this module.
'''

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

try:
    from helpers import sleep_runner  # type: ignore
except ImportError:  # pragma: no cover - framework-context import
    from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore

ENV_FILENAME = '.skillopt-env'
NL = chr(10)  # newline, kept explicit for deterministic serialization

# Common A0 env var names (vary by deployment - extend as needed).
A0_VAR_MAP = {
    'A0_CHAT_LLM_BASE_URL': 'AZURE_OPENAI_ENDPOINT',
    'A0_CHAT_LLM_API_KEY': 'AZURE_OPENAI_API_KEY',
    'A0_LLM_API_KEY': 'AZURE_OPENAI_API_KEY',
    'OPENAI_API_KEY': 'AZURE_OPENAI_API_KEY',
    'OPENAI_BASE_URL': 'AZURE_OPENAI_ENDPOINT',
    'ANTHROPIC_API_KEY': 'ANTHROPIC_API_KEY',
}

BACKENDS = ('auto', 'azure_openai', 'openai_compatible', 'claude', 'qwen', 'minimax')

_DEFAULT_API_VERSION = '2024-12-01-preview'
_REF_RE = re.compile(r'^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$')
_LINE_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$')

_HEADER_LINES = (
    '# SkillOpt LLM env - v1.8.5: credential values live in <project>/usr/.env',
    '# (chmod 600, outside the plugin repo). This file holds ${VAR} references',
    '# plus non-secret literals; resolution goes through',
    '# sleep_runner._expand_env (os.environ first, then the usr/.env fallback).',
)
_HEADER = NL.join(_HEADER_LINES) + NL + NL


def _is_secret_key(key: str) -> bool:
    u = key.strip().upper()
    return (
        u.endswith('_API_KEY')
        or '_API_KEY_' in u
        or 'TOKEN' in u
        or 'PASSWORD' in u
        or 'SECRET' in u
    )


def _dotenv_names() -> dict:
    # {name: value} from the framework usr/.env fallback (best-effort).
    try:
        return sleep_runner._dotenv_fallback() or {}
    except Exception:
        return {}


def _visible(name: str) -> bool:
    if os.environ.get(name):
        return True
    return bool(_dotenv_names().get(name))


def _source_names() -> dict:
    # Mapped source vars visible now; os.environ wins over usr/.env.
    # Callers must never log or echo the values.
    fb = _dotenv_names()
    out: dict = {}
    for src in A0_VAR_MAP:
        v = os.environ.get(src) or fb.get(src)
        if v:
            out[src] = v
    return out


def _ref_for(dst: str, sources: dict) -> str:
    # Reference value for a mapped destination key, or empty to omit.
    for src in A0_VAR_MAP:
        if A0_VAR_MAP[src] == dst and src in sources:
            return '${' + src + '}'
    if _visible(dst):
        return '${' + dst + '}'
    return ''


def build_env_block(backend: str, sources: dict | None = None) -> dict:
    # Build the env entries for a backend. Credential keys become
    # ${SOURCE} references; non-secret literals stay verbatim.
    sources = sources if sources is not None else _source_names()
    out: dict = {}
    for src in A0_VAR_MAP:
        dst = A0_VAR_MAP[src]
        if dst in out:
            continue
        ref = _ref_for(dst, sources)
        if ref:
            out[dst] = ref
    if not out.get('AZURE_OPENAI_API_VERSION'):
        ver = os.environ.get('A0_CHAT_LLM_API_VERSION') or _dotenv_names().get(
            'A0_CHAT_LLM_API_VERSION'
        )
        out['AZURE_OPENAI_API_VERSION'] = str(ver) if ver else _DEFAULT_API_VERSION
    if backend == 'openai_compatible':
        out['AZURE_OPENAI_AUTH_MODE'] = 'openai_compatible'
    elif backend == 'azure_openai':
        out.setdefault('AZURE_OPENAI_AUTH_MODE', 'api_key')
    out['SKILLOPT_BACKEND'] = backend
    return out


def ref_resolves(value: str) -> bool:
    # True when a ${NAME} reference is resolvable right now.
    m = _REF_RE.match(str(value).strip())
    if not m:
        return False
    return _visible(m.group(1))


def _strip_quotes(value: str) -> str:
    v = str(value)
    for q in (chr(39), chr(34)):
        if len(v) >= 2 and v.startswith(q) and v.endswith(q):
            return v[1:-1]
    return v


def _provenance_ref(key: str, value: str, sources: dict) -> str:
    # Prefer the mapped source whose CURRENT value matches the plaintext;
    # fall back to a self-reference (resolvable when the key itself lives
    # in usr/.env, the v1.8.4 layout).
    for src, sv in (sources or {}).items():
        if sv == value:
            return '${' + src + '}'
    return '${' + key + '}'


def sanitize_env_text(text: str, sources: dict | None = None) -> tuple:
    # Rewrite plaintext credential values to ${VAR} references.
    # Returns (new_text, fixed_key_names). Comments, blank lines,
    # existing references and non-secret literals are preserved.
    sources = sources if sources is not None else _source_names()
    fixed: list = []
    out_lines: list = []
    for raw in str(text or '').splitlines():
        m = _LINE_RE.match(raw)
        if not m or not _is_secret_key(m.group(1)):
            out_lines.append(raw)
            continue
        key = m.group(1)
        value = _strip_quotes(m.group(2))
        if not value:
            out_lines.append(raw)
            continue
        if _REF_RE.match(value):
            out_lines.append('export ' + key + '=' + value)
            continue
        ref = _provenance_ref(key, value, sources)
        out_lines.append('export ' + key + '=' + ref)
        if key not in fixed:
            fixed.append(key)
    body = (NL.join(out_lines) + NL) if out_lines else ''
    return body, fixed


def _serialize(key: str, value: str) -> str:
    v = str(value)
    if _REF_RE.match(v):
        return 'export ' + key + '=' + v
    if (
        any(c.isspace() for c in v)
        or chr(34) in v
        or chr(39) in v
        or '#' in v
    ):
        return 'export ' + key + '=' + chr(34) + v + chr(34)
    return 'export ' + key + '=' + v


def _merge(existing: str, block: dict, sources: dict) -> tuple:
    # Overlay block entries onto existing text; non-block lines survive.
    merged, fixed = sanitize_env_text(existing or '', sources)
    handled: set = set()
    out_lines: list = []
    for raw in merged.splitlines():
        m = _LINE_RE.match(raw)
        if m and m.group(1) in block:
            key = m.group(1)
            out_lines.append(_serialize(key, block[key]))
            handled.add(key)
        else:
            out_lines.append(raw)
    for key in block:
        if key not in handled:
            out_lines.append(_serialize(key, block[key]))
    body = (NL.join(out_lines) + NL) if out_lines else ''
    return body, fixed


def _key_names(text: str) -> list:
    names: list = []
    for raw in str(text or '').splitlines():
        m = _LINE_RE.match(raw)
        if m:
            names.append(m.group(1))
    return names


def apply(backend: str, env_file=None) -> dict:
    # Write the reference-only env file. Atomic, chmod 600, never raises.
    try:
        if backend not in BACKENDS or backend == 'auto':
            return {
                'ok': False,
                'error': 'backend must be one of: ' + ', '.join(BACKENDS[1:]),
            }
        if env_file is not None:
            target = Path(env_file)
        else:
            target = sleep_runner.runs_dir() / ENV_FILENAME
        target.parent.mkdir(parents=True, exist_ok=True)
        sources = _source_names()
        block = build_env_block(backend, sources)
        existing = ''
        if target.is_file():
            existing = target.read_text(encoding='utf-8')
        merged_text, fixed = _merge(existing, block, sources)
        if not existing.strip():
            merged_text = _HEADER + merged_text
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix='.skillopt-env.', suffix='.tmp'
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(merged_text)
        os.chmod(tmp, 0o600)
        os.replace(str(tmp), str(target))
        keys = sorted(set(block) | set(_key_names(existing)))
        unresolved = sorted(
            {
                str(v)[2:-1]
                for v in block.values()
                if _REF_RE.match(str(v)) and not ref_resolves(str(v))
            }
        )
        return {
            'ok': True,
            'path': str(target),
            'backend': backend,
            'keys': keys,
            'fixed': fixed,
            'unresolved': unresolved,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}
