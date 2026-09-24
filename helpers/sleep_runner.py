"""
SkillOpt Sleep engine runner + shared validation-gate utility.

The `skillopt-sleep` CLI is shipped as a Python module
(`python -m skillopt_sleep <verb>`). The `skillopt_sleep` package is
installed in the A0 venv at /opt/venv-a0. In v0.2.0+ it ships as a
separate top-level package alongside the `skillopt` library.

This helper centralises:
- resolving the correct invocation (cross-platform A0 venv)
- reading the merged config (defaults + framework plugin config)
- launching a Sleep cycle in a background subprocess (Windows-safe)
- tailing the log file so the WebUI can show progress
- locating the staged proposal after a `run` cycle completes
- the SHARED validation gate used by auto-loop, adopt endpoint,
  post-adopt hook, and the skillopt_sleep tool.

v1.1.0 changes:
- Cross-platform `_a0_python()` (Linux /opt/venv-a0 + Windows .venv)
- start_new_session only on POSIX; Windows uses CREATE_NEW_PROCESS_GROUP
- subprocess cwd set to staging_dir() so the engine's `consolidate`
  verb writes its best_skill.md artifact where the rest of the
  pipeline expects to find it.
- `parse_held_out()` reads the engine's held-out score from the log
  (e.g. `held-out 0.412 -> 0.487`) and surfaces it for the gate.
- `validate_proposal()` is the new shared gate: byte-identical
  rejection with whitespace normalisation, mandatory example block
  check, and a 50% shrink ceiling. Used by auto-loop, adopt API,
  post-adopt hook, and the tool.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

PLUGIN_NAME = "skillopt"


def _here() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _a0_python() -> str:
    """Return the Python that should be used to run `skillopt_sleep`.

    Cross-platform detection. Mirrors the logic in hooks.py so the
    install and the runtime use the same interpreter.
    """
    candidates: list[str] = []
    env_py = os.environ.get("A0_VENV_PYTHON")
    if env_py:
        candidates.append(env_py)
    if sys.platform == "win32":
        cwd = os.getcwd()
        candidates.append(os.path.join(cwd, ".venv", "Scripts", "python.exe"))
        candidates.append(os.path.join(cwd, "venv", "Scripts", "python.exe"))
    else:
        candidates.append("/opt/venv-a0/bin/python")
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return sys.executable


def plugin_root() -> Path:
    """Absolute path to the installed plugin directory."""
    return Path(_here())


def rollouts_dir() -> Path:
    p = plugin_root() / "logs" / "rollouts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def staging_dir() -> Path:
    p = plugin_root() / "staging"
    p.mkdir(parents=True, exist_ok=True)
    return p


def runs_dir() -> Path:
    p = plugin_root() / "logs" / "runs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def a0_skills_dir() -> Path:
    """Path to Agent Zero's user-facing skills directory.

    v1.8.1 fix: the default was the hardcoded Linux path Path("/a0/usr/skills"),
    which never exists on Windows — every consumer silently saw an empty
    skills list there. plugin_root() is <project>/usr/plugins/skillopt, so
    the fallback is plugin_root().parent.parent / "skills" (= <project>/usr/skills),
    which matches both the framework install and the standalone extension
    layout. Overridable via the SKILLOPT_SKILLS_DIR environment variable
    for alternate deployments.
    """
    override = os.environ.get("SKILLOPT_SKILLS_DIR")
    if override:
        return Path(override)
    return plugin_root().parent.parent / "skills"


def default_config() -> dict[str, Any]:
    """Parse the bundled default_config.yaml as a dict (no PyYAML -> light parse)."""
    p = plugin_root() / "default_config.yaml"
    out: dict[str, Any] = {}
    if not p.is_file():
        return out
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        elif val.startswith('"') and val.endswith('"'):
            out[key] = val[1:-1]
        else:
            try:
                out[key] = int(val)
            except ValueError:
                try:
                    out[key] = float(val)
                except ValueError:
                    out[key] = val
    return out


def _framework_registry_config() -> dict[str, Any]:
    """Read the plugin's live framework config (the user's WebUI settings).

    v1.8.1: previously ONLY the auto-loop saw the framework config (it
    passed get_config() explicitly); every other consumer — the shared
    validation gate, /adopt, the skillopt_sleep tool, the post-adopt
    hook, reward_model — read default_config.yaml only, so user-tuned
    gate / replay keys were silently ignored. This resolver merges the
    framework plugin-config registry (``helpers.plugins.get_plugin_config``)
    best-effort. It is skipped when the registry is unavailable (the smoke
    harness / standalone scripts, where ``helpers`` resolves to the
    plugin-local package with no ``plugins`` module) or when the
    ``SKILLOPT_NO_FRAMEWORK_CONFIG=1`` kill switch is set. Cached for a
    few seconds so a per-gate-call read stays cheap. Never raises.
    """
    if os.environ.get("SKILLOPT_NO_FRAMEWORK_CONFIG", "").strip().lower() in ("1", "true", "yes"):
        return {}
    now = time.time()
    cached = _FRAMEWORK_CFG_CACHE["cfg"]
    if isinstance(cached, dict) and (now - _FRAMEWORK_CFG_CACHE["at"]) < _FRAMEWORK_CFG_TTL:
        return cached
    cfg: dict[str, Any] = {}
    try:
        from helpers import plugins as plugins_helper  # type: ignore  # noqa: E402
        loaded = plugins_helper.get_plugin_config(PLUGIN_NAME) or {}
        if isinstance(loaded, dict):
            cfg = loaded
    except Exception:
        cfg = {}
    _FRAMEWORK_CFG_CACHE["at"] = now
    _FRAMEWORK_CFG_CACHE["cfg"] = cfg
    return cfg


_FRAMEWORK_CFG_CACHE: dict[str, Any] = {"at": 0.0, "cfg": {}}
_FRAMEWORK_CFG_TTL = 5.0


def merged_config(framework_config: dict | None = None) -> dict[str, Any]:
    """Merge config sources; later wins:
      1. default_config.yaml (shipped defaults)
      2. framework plugin-config registry (the user's WebUI settings;
         v1.8.1 — skipped when unavailable so the smoke harness and
         standalone scripts keep the byte-for-byte YAML-only behaviour)
      3. the explicit ``framework_config`` argument (highest priority)
    """
    merged = default_config()
    # v1.8.19 (P4): a RAISING registry read must not bubble up - in
    # agent_init._get_config an exception here falls through to the bare
    # config.json parity fallback, which is exactly {} when the config
    # page wiped it (the v1.8.17 silent-spin scenario). Treat a failed
    # registry read as empty; the YAML defaults always survive.
    try:
        reg = _framework_registry_config()
    except Exception:
        reg = {}
    if reg:
        merged.update(reg)
    if isinstance(framework_config, dict):
        merged.update(framework_config)
    return merged


def _resolve_skillopt_sleep_module() -> list[str]:
    """Return the argv prefix that runs skillopt_sleep as a module.

    In SkillOpt 0.2.0+ `skillopt_sleep` is a separate top-level
    package that ships alongside the `skillopt` library. The CLI is
    invoked as `python -m skillopt_sleep <verb>`. (Earlier analysis
    suggested `python -m skillopt sleep`; that form does not exist -
    `skillopt` is a library package with no `__main__` - and using
    it would break the invocation.)
    """
    return [_a0_python(), "-m", "skillopt_sleep"]


def write_rollout(record: dict[str, Any]) -> Path:
    """Persist a single agent rollout to logs/rollouts/<id>.json.

    The Sleep engine's `harvest` verb reads this directory.
    """
    rid = record.get("id") or uuid.uuid4().hex
    record["id"] = rid
    p = rollouts_dir() / f"{rid}.json"
    tmp_p = p.with_name(p.name + ".tmp")
    tmp_p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_p.replace(p)  # v1.8.10: atomic write so readers never see a partial rollout
    return p


def list_rollouts() -> list[Path]:
    return sorted(p for p in rollouts_dir().glob("*.json") if p.is_file())


def _load_held_out(skill_name: str) -> list[dict[str, Any]]:
    """Return the most recent rollouts attributed to `skill_name`, newest
    first, capped at `replay_held_out_n` (default 8). These are the
    counterfactual replay set the local replay gate (stage 0.7) re-scores
    under the current vs proposed skill.

    Filters on the authoritative `skill_used` field written by the v1.7.0
    harvester (C1). Rollouts that fail to parse are skipped. Returns []
    when no rollouts match (the replay gate then returns ok=False and the
    structural gate runs).
    """
    cfg = merged_config()
    n = int(cfg.get("replay_held_out_n", 8) or 0)
    out: list[dict[str, Any]] = []
    for p in reversed(list_rollouts()):
        if n and len(out) >= n:
            break
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(rec, dict) and rec.get("skill_used") == skill_name:
            out.append(rec)
    return out


def list_skills_available() -> list[dict[str, str]]:
    """Enumerate the skills currently installed for A0, with their SKILL.md path."""
    out: list[dict[str, str]] = []
    skills_root = a0_skills_dir()
    if not skills_root.is_dir():
        return out
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            continue
        skill_md = child / "SKILL.md"
        if skill_md.is_file():
            out.append({
                "name": child.name,
                "skill_md": str(skill_md),
                "size_kb": round(skill_md.stat().st_size / 1024.0, 1),
            })
    return out


def find_staged_proposals() -> list[Path]:
    """Return staged skill proposals waiting for validation/adoption."""
    out: list[Path] = []
    sd = staging_dir()
    for child in sorted(sd.iterdir()):
        if child.is_file() and child.suffix in (".md", ".proposed"):
            out.append(child)
        elif child.is_dir() and (child / "SKILL.md").is_file():
            out.append(child / "SKILL.md")
    return out


# ----------------------------------------------------------------------- #
# v1.8.21: staged-proposal lifecycle moves (drain, not head-of-line)
# ----------------------------------------------------------------------- #

def _move_staged_proposal(src: str | os.PathLike, subdir: str, tag: str) -> Path | None:
    """Move a staged proposal out of staging/ into staging/<subdir>/ with a
    timestamped name, carrying its official-gate marker sidecar along.
    staging/ itself stays the pending queue; find_staged_proposals() scans
    only its top level, so moved files can never block the queue again.
    Best-effort: returns the destination path, or None when the source is
    already gone (moved by a concurrent actor)."""
    src_path = Path(src)
    marker = src_path.with_suffix(src_path.suffix + OFFICIAL_GATE_MARKER_SUFFIX)
    if not src_path.is_file():
        return None
    dest_dir = staging_dir() / subdir
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    dest = dest_dir / ("%s__%s%s%s" % (src_path.stem, tag, ts, src_path.suffix))
    n = 1
    while dest.exists():
        dest = dest_dir / ("%s__%s%s_%d%s" % (src_path.stem, tag, ts, n, src_path.suffix))
        n += 1
    try:
        shutil.move(str(src_path), str(dest))
    except Exception:
        return None
    # Sidecars travel with the proposal: the official-gate marker (v1.8.1),
    # and the v1.8.22 real-gate verdict sidecar + frozen held-out tasks
    # file. Without this, a pending-real-gate proposal consumed or
    # quarantined by the manual /adopt path would strand its sidecar in
    # staging top level — and find_pending_real_gate_sidecar() (single
    # flight) would then block every future real-gate spawn forever.
    for _sidecar, _suffix in (
        (marker, OFFICIAL_GATE_MARKER_SUFFIX),
        (_real_gate_sidecar_path(src_path), REAL_GATE_SIDECAR_SUFFIX),
        (_real_gate_tasks_path(src_path), REAL_GATE_TASKS_SUFFIX),
    ):
        try:
            if _sidecar.is_file():
                shutil.move(
                    str(_sidecar),
                    str(dest.with_suffix(dest.suffix + _suffix)),
                )
        except Exception:
            pass
    return dest


def quarantine_staged_proposal(src: str | os.PathLike, tag: str = "reject") -> Path | None:
    """v1.8.21: move a REJECTED staged proposal out of staging/ (staging/
    rejected/). Re-attempting identical bytes next tick is deterministic
    waste and, with the old head-of-line _auto_adopt, one malformed
    proposal blocked every staged proposal behind it (proven live
    2026-09-23: a degenerate proposal re-rejected 14x over ~14h)."""
    return _move_staged_proposal(src, "rejected", tag)


def consume_staged_proposal(src: str | os.PathLike) -> Path | None:
    """v1.8.21: move an ADOPTED staged proposal out of staging/
    (staging/adopted/) so it cannot be re-attempted against the now-
    identical live skill (which would no-op-reject it next tick)."""
    return _move_staged_proposal(src, "adopted", "adopt")


# ----------------------------------------------------------------------- #
# v1.1.0: shared validation gate
# ----------------------------------------------------------------------- #

_WS_NORMALISE_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Collapse all whitespace and lowercase for a structural-equality check.

    Catches the byte-identical-but-whitespace-changed case the v1.0 gate
    let through (a 1904-byte 'qa' adoption where proposed == current).
    """
    return _WS_NORMALISE_RE.sub(" ", text or "").strip().lower()


def has_example_block(text: str) -> bool:
    """Return True if `text` contains a triple-backtick code block.

    The SkillOpt prompt template always emits an example block; a
    proposal without one is malformed or stripped.
    """
    return "```" in (text or "")


def parse_held_out(log_path: str | os.PathLike | None) -> dict[str, Any] | None:
    """Parse the most recent `held-out X -> Y` line from a Sleep log.

    Returns {"before": 0.412, "after": 0.487, "delta_pp": 7.5} on
    success, or None if no held-out line is found. Used by the gate
    to enforce `gate_min_improvement_pp`.
    """
    if not log_path:
        return None
    p = Path(log_path)
    if not p.is_file():
        return None
    try:
        # Read last 32KB to find the most recent held-out line.
        with open(p, "rb") as f:
            try:
                f.seek(-32768, 2)
            except OSError:
                f.seek(0)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None
    pattern = re.compile(
        r"held[- ]out\s+([0-9]*\.?[0-9]+)\s*->\s*([0-9]*\.?[0-9]+)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(data))
    if not matches:
        return None
    m = matches[-1]
    try:
        before = float(m.group(1))
        after = float(m.group(2))
    except ValueError:
        return None
    delta_pp = (after - before) * 100.0
    return {"before": before, "after": after, "delta_pp": round(delta_pp, 2)}


def validate_proposal(
    proposed: str,
    current: str,
    min_chars: int = 200,
    min_improvement_pp: float = 0.0,
    max_shrink_ratio: float = 0.5,
    held_out: dict[str, Any] | None = None,
    *,
    skill_name: str | None = None,
    skill_path: str | os.PathLike | None = None,
    official_gated: bool = False,
    ab_harness_enabled: bool | None = None,
) -> tuple[bool, str]:
    """Shared validation gate used by auto-loop, adopt API, post-adopt hook, tool.

    v1.8.19 (P3, audit RC4): `ab_harness_enabled` lets the caller pass its
    already-resolved harness flag (auto-loop computes it from the merged
    config). None keeps the legacy behaviour (stage 0 consults the harness's
    own config); False skips stage 0 entirely — this closes the dead-guard
    gap where auto-loop computed `ab_enabled` but the harness ran anyway
    with the deterministic stub judge and rejected real proposals.

    Reject conditions (in order):
    0. v1.2.0 A/B harness (only when `skill_name` is passed AND the
       harness has enough rollouts). If the harness can run AND the
       proposed skill loses the paired test, reject with reason
       starting with 'ab_harness_rejected'. If the harness cannot run
       (no rollouts, no judge, harness disabled) we log it and fall
       through to the structural stages below. The harness is never
       allowed to CRASH the gate; a bug in the harness returns
       can_run=False and the structural stages run unchanged.
       v1.6.0: the harness is ADVISORY ONLY (ab_harness_enabled defaults
       false) — the official engine's own gate is authoritative.
    0.5. v1.2.0 per-fragment gate (only when `skill_path` is passed).
       Decomposes the SKILL.md into named fragments via
       `fragment_store.read_fragments()`, and runs the structural
       checks (byte equality, whitespace-normalised equality,
       headers, example block, min_chars, shrink ceiling) on each
       fragment where the proposed text differs from the current.
       A single failing fragment rejects the whole proposal. If no
       fragment differs (proposed == current everywhere) we reject
       with `no_op` (preserving the byte-identical stage 5 below).
    1. Empty proposal.
    2. No markdown headers (likely malformed).
    3. Below min_chars (catches stubs / truncations).
    4. No triple-backtick example block (the engine always emits one).
    5. Byte-identical to current.
    6. Whitespace-normalised identical to current (catches 1904->1904
       'no-op' adoptions that differ only in whitespace).
    7. Shrinks by more than max_shrink_ratio vs. current.
    8. Held-out score is provided but its delta is below
       min_improvement_pp. If held_out is None we skip this check
       (the Sleep engine didn't surface one, e.g. for the direct
       optimizer path which has no numeric gate).
       v1.6.0 (Phase 2): when `official_gated` is True, stage 8 is
       SKIPPED entirely — the official Sleep engine already enforced
       its strict monotonic held-out gate before staging the proposal,
       so re-gating locally would double-count and could reject a
       proposal the authoritative gate accepted. The structural stages
       (1-7) still run as the cheap pre-filter; only the numeric
       held-out stage is delegated upstream.

    Backwards compat: existing callers that don't pass `skill_name`
    AND don't pass `skill_path` AND don't pass `official_gated` see the
    original v1.1.0 behaviour exactly. The A/B stage and the per-fragment
    stage are both opt-in and run before any expensive structural check.
    `official_gated` only short-circuits the held-out stage.
    """
    # v1.2.0: stage 0 - A/B harness. Wrapped in try/except so a
    # harness bug can never crash the gate. The harness returns
    # can_run=False (not an exception) when it has no data or no
    # judge; in that case we fall through to the structural stages.
    # v1.8.19: the caller may pass ab_harness_enabled=False to skip
    # stage 0 entirely (the harness's own config already keeps it
    # advisory-only by default; this honours the caller's resolution).
    if skill_name and ab_harness_enabled is not False:
        try:
            try:
                from usr.plugins.skillopt.helpers import ab_harness  # type: ignore  # noqa: E402
            except ImportError:
                from helpers import ab_harness  # type: ignore  # noqa: E402
            # v1.8.6: callers now always pass skill_name (the policy scope gate
            # needs it), but stage 0 stays unconditional exactly as in v1.8.5:
            # run_paired_test returns can_run=False when no judge/data is
            # configured, which keeps the harness advisory-only under default
            # configs. The ab_harness_enabled guard lives at the auto_loop
            # call-site, not here.
            ab_result = ab_harness.run_paired_test(
                skill_name=skill_name,
                proposed_text=proposed or "",
                current_text=current or "",
            )
            if ab_result.get("can_run"):
                if not ab_result.get("passed"):
                    return False, (
                        f"ab_harness_rejected: {ab_result.get('reason', 'unknown')}"
                    )
                # passed=True: continue to the structural stages below
            else:
                # can_run=False: harness skipped (no rollouts, no judge).
                # The structural gate is the safety net. No reject.
                pass
        except Exception as e:
            # Belt-and-braces: never let the harness crash the gate.
            try:
                import logging as _logging
                _logging.getLogger("skillopt.sleep_runner").debug(
                    "[skillopt] ab_harness raised in validate_proposal: %s", e
                )
            except Exception:
                pass

    # v1.7.0: stage 0.7 - local replay gate (Solution C, Phase C2). The
    # authoritative counterfactual gate for the LOCAL / direct-optimizer
    # path. Runs only when the skill is known, the official engine did
    # NOT already gate this proposal (official_gated=False — the upstream
    # gate is authoritative and we must not double-count), and the local
    # replay gate is enabled (replay_local_gate_enabled, default true).
    # v1.8.0: the executor is the deterministic mock (no LLM) by default,
    # OR the real A0-agent-loop subprocess executor when
    # replay_real_executor_enabled is true (see helpers/replay_harness.py
    # + scripts/replay_worker.py). As with stage 0, a harness bug can
    # never crash the gate — on ok=False (insufficient rollouts / executor
    # unavailable) we fall through to the structural stages; on ok=True +
    # accepted=False we REJECT.
    if skill_name and not official_gated:
        try:
            _cfg = merged_config()
            if bool(_cfg.get("replay_local_gate_enabled", True)):
                try:
                    from usr.plugins.skillopt.helpers import replay_harness  # type: ignore  # noqa: E402
                except ImportError:
                    from helpers import replay_harness  # type: ignore  # noqa: E402
                # v1.8.19 (P3, audit RC4): executor-aware bar. The mock
                # executor scores real proposals as keyword-relevance noise
                # (Sep-19 real proposal: 2.08pp, rejected at the 5.0pp bar
                # meant for REAL replay scores). The mock gets its own low
                # bar (replay_mock_gate_min_improvement_pp, default 1.0):
                # real improvements pass, outright regressions (lift < 0)
                # still reject. The real executor keeps the full
                # gate_min_improvement_pp bar.
                #
                # v1.8.22: two changes.
                # (a) EXECUTOR REROUTE — when the ASYNC real gate owns the
                #     real confirmation (replay_real_gate_enabled, requires
                #     replay_real_executor_enabled), stage 0.7 must NOT run
                #     the real executor synchronously here: the drain would
                #     block for up to 2xN monologues AND the async worker
                #     would re-run the same counterfactual (double spend).
                #     The in-gate executor is the cheap deterministic mock;
                #     the real confirmation happens in the drain's async
                #     stage (_real_gate_spawn).
                # (b) MOCK VERDICT ADVISORY — with replay_mock_enforce false
                #     (the default), the mock verdict is LOGGED, not
                #     enforced: its keyword-relevance lift on real proposals
                #     is noise (2026-09-23 drain: 5 of 7 proposals rejected
                #     as mock "regression"). True restores the v1.8.19 hard
                #     1.0pp mock bar. The legacy synchronous-real path
                #     (async gate OFF, replay_real_executor_enabled ON)
                #     keeps enforcing the full gate_min_improvement_pp bar.
                _real_enabled = bool(_cfg.get("replay_real_executor_enabled", False))
                _async_gate = bool(_cfg.get("replay_real_gate_enabled", False)) and _real_enabled
                _is_mock = (not _real_enabled) or _async_gate
                _enforce = (
                    bool(_cfg.get("replay_mock_enforce", False))
                    if _is_mock
                    else True  # legacy synchronous-real path keeps its bar
                )
                _rc_cfg = dict(_cfg)
                if _is_mock:
                    _rc_cfg["gate_min_improvement_pp"] = float(
                        _cfg.get("replay_mock_gate_min_improvement_pp", 1.0)
                    )
                _held = _load_held_out(skill_name)
                _replay = replay_harness.run_counterfactual(
                    skill_name=skill_name,
                    current_skill_md=current or "",
                    proposed_skill_md=proposed or "",
                    held_out_tasks=_held,
                    executor=("real" if not _is_mock else "mock"),
                    config=_rc_cfg,
                )
                if _replay.get("ok"):
                    if not _replay.get("accepted"):
                        if _is_mock and not _enforce:
                            try:
                                import logging as _logging
                                _logging.getLogger("skillopt.sleep_runner").info(
                                    "[skillopt] mock replay gate (advisory, not "
                                    "enforcing) for %s: %s",
                                    skill_name,
                                    _replay.get("reason", "unknown"),
                                )
                            except Exception:
                                pass
                        else:
                            return False, (
                                f"replay_gate_rejected: {_replay.get('reason', 'unknown')}"
                            )
                    # accepted=True: continue to the structural stages.
                # ok=False: insufficient rollouts / executor unavailable ->
                # fall through to the structural gate (loud-not-crash).
        except Exception as e:
            try:
                import logging as _logging
                _logging.getLogger("skillopt.sleep_runner").debug(
                    "[skillopt] local replay gate raised: %s", e
                )
            except Exception:
                pass

    # v1.8.6: stage 0.75 - per-skill policy scope gate (ROADMAP item 8).
    # Enforces the governance overlay SCOPE keys: allowed_fragments (only
    # these fragment ids may change), max_verbosity_delta_ratio (relative
    # growth cap vs current), forbid_patterns (banned literal substrings).
    # Scope is a USER constraint, not a quality judgment: it runs even when
    # official_gated=True (the official engine judges quality and must not
    # override what the user allows us to touch).
    if skill_name:
        try:
            _gov = sys.modules.get("helpers.governance")
            if _gov is None:
                try:
                    from usr.plugins.skillopt.helpers import governance as _gov
                except ImportError:
                    from helpers import governance as _gov
            _pol = _gov.load_skill_policy(skill_name)
            _maxverb = _pol.get("max_verbosity_delta_ratio")
            if _maxverb is not None and current:
                _ratio = float(_maxverb)
                if _ratio >= 0 and len(proposed or "") > len(current) * (1.0 + _ratio):
                    return False, ("policy_scope_verbosity_exceeded: "
                        + str(len(proposed or "")) + " > "
                        + str(int(len(current) * (1.0 + _ratio))))
            _forbid = _pol.get("forbid_patterns") or []
            for _pat in _forbid:
                if str(_pat) and str(_pat) in (proposed or ""):
                    return False, "policy_scope_forbidden_pattern: " + str(_pat)
            _allowed = _pol.get("allowed_fragments") or []
            if _allowed:
                _md = _gov._skill_dir(skill_name) / "SKILL.md"
                if _md.is_file():
                    _fs = sys.modules.get("helpers.fragment_store")
                    if _fs is None:
                        try:
                            from usr.plugins.skillopt.helpers import fragment_store as _fs
                        except ImportError:
                            from helpers import fragment_store as _fs
                    _named = [f for f in _fs.read_fragments(str(_md))
                        if f.get("id") != "_default"]
                    if _named:
                        import tempfile as _tfs
                        _fdS, _tpS = _tfs.mkstemp(suffix=".md", prefix="skillopt_scope_")
                        try:
                            os.close(_fdS)
                            Path(_tpS).write_text(proposed or "", encoding="utf-8")
                            _prop = _fs.read_fragments(_tpS)
                        finally:
                            try:
                                os.unlink(_tpS)
                            except Exception:
                                pass
                        _cur = {f.get("id"): f.get("text", "") for f in _named}
                        _new = {f.get("id"): f.get("text", "") for f in _prop}
                        for _fid, _ctext in _cur.items():
                            if _fid in _allowed:
                                continue
                            if _new.get(_fid, "") != _ctext:
                                return False, ("policy_scope_fragment_not_allowed: "
                                    + str(_fid))
                        for _fid in _new:
                            if (_fid not in _cur and _fid != "_default"
                                    and _fid not in _allowed):
                                return False, ("policy_scope_new_fragment_not_allowed: "
                                    + str(_fid))
        except Exception as _scope_err:
            try:
                import logging as _logging
                _logging.getLogger("skillopt.sleep_runner").debug(
                    "[skillopt] policy scope gate raised: %s", _scope_err)
            except Exception:
                pass
    # v1.2.0: stage 0.5 - per-fragment gate. Only when skill_path is
    # provided AND fragment_per_fragment_gate is enabled. We read the
    # current fragments, compare each to the proposed (resolved) text,
    # and run the structural checks on CHANGED fragments only. A
    # failure here is reported as `fragment_<id>_<check>_<reason>`.
    # When the per-fragment gate runs (i.e. the skill has named
    # fragments), it REPLACES the whole-file check below - the
    # whole-file stages are the fallback for skills without
    # fragments. A non-fragment caller that doesn't pass skill_path
    # sees the original v1.1.0 gate exactly.
    per_fragment_ran = False
    if skill_path:
        try:
            cfg = merged_config()
            if bool(cfg.get("fragment_per_fragment_gate", True)):
                try:
                    from usr.plugins.skillopt.helpers import fragment_store  # type: ignore  # noqa: E402
                except ImportError:
                    from helpers import fragment_store  # type: ignore  # noqa: E402
                fragments = fragment_store.read_fragments(skill_path)
                if len(fragments) > 1 or (fragments and fragments[0].get("id") != "_default"):
                    per_fragment_ran = True
                    # Parse the proposed text directly (write to a temp
                    # file, read fragments from it). This is the v1.2.0
                    # fix: the previous implementation looked for a
                    # `.proposed` file on disk, which doesn't exist in
                    # the test harness. Writing the proposed text to a
                    # temp file lets us read its fragments without
                    # touching the real filesystem.
                    import tempfile as _tf
                    _tmp_fd, _tmp_path = _tf.mkstemp(suffix=".md", prefix="skillopt_prop_")
                    try:
                        with os.fdopen(_tmp_fd, "w", encoding="utf-8") as _f:
                            _f.write(proposed)
                        proposed_fragments = fragment_store.read_fragments(_tmp_path)
                    finally:
                        try:
                            os.unlink(_tmp_path)
                        except Exception:
                            pass
                    by_id = {f.get("id"): f for f in proposed_fragments}
                    for f in fragments:
                        fid = f.get("id")
                        cur_text = f.get("text", "")
                        new_f = by_id.get(fid)
                        if new_f is None:
                            continue
                        new_text = new_f.get("text", "")
                        if new_text == cur_text:
                            continue  # unchanged fragment - skip per-fragment checks
                        # Run the structural checks on the new fragment text
                        check_failed = _per_fragment_structural_check(
                            new_text, cur_text, fid,
                            min_chars=min_chars,
                            max_shrink_ratio=max_shrink_ratio,
                        )
                        if check_failed:
                            return False, check_failed
        except Exception as e:
            try:
                import logging as _logging
                _logging.getLogger("skillopt.sleep_runner").debug(
                    "[skillopt] per-fragment gate raised: %s", e
                )
            except Exception:
                pass

    # When the per-fragment gate ran, it IS the gate - skip the
    # whole-file check below. The whole-file check is the fallback
    # for skills without fragments (single implicit _default fragment,
    # or no skill_path at all). This preserves the v1.1.0 gate for
    # non-fragment callers and makes the per-fragment gate the
    # authoritative check for fragment-aware callers.
    if not per_fragment_ran:
        if not proposed or not proposed.strip():
            return False, "proposed skill is empty"
        if not any(line.lstrip().startswith("#") for line in proposed.splitlines()):
            return False, "proposed skill has no markdown headers - likely malformed"
        if len(proposed) < min_chars:
            return False, f"proposed skill is too short ({len(proposed)} chars < {min_chars})"
        if not has_example_block(proposed):
            return False, "proposed skill has no triple-backtick example block (engine always emits one)"
        if current:
            if proposed == current:
                return False, "proposed skill is byte-identical to current (no-op)"
            if _normalise(proposed) == _normalise(current):
                return False, "proposed skill equals current after whitespace normalisation (no-op)"
            if len(current) > 0 and len(proposed) < len(current) * max_shrink_ratio:
                return False, (
                    f"proposed skill shrank by more than "
                    f"{int(max_shrink_ratio*100)}% ({len(current)} -> {len(proposed)} chars)"
                )
        if not official_gated and min_improvement_pp > 0 and held_out is not None:
            delta = held_out.get("delta_pp")
            if delta is None or delta < min_improvement_pp:
                return False, (
                    f"held-out improvement {delta}pp < required {min_improvement_pp}pp"
                )
        return True, "ok"
    # When per_fragment_ran is True, the per-fragment gate already
    # returned accept/reject above. If we reach here, all changed
    # fragments passed the per-fragment structural check, so the
    # proposal is accepted.
    return True, "ok"


def _per_fragment_structural_check(
    new_text: str,
    cur_text: str,
    fragment_id: str,
    *,
    min_chars: int,
    max_shrink_ratio: float,
) -> str | None:
    """Run the structural checks on a single fragment. Returns a
    reject reason string on failure, or None if the fragment passes.

    v1.2.0: this is the per-fragment stage 0.5 of the gate. It is
    deliberately a subset of the whole-file checks: we enforce
    byte-equality (caught upstream), whitespace-normalised equality,
    header presence, min_chars (scaled to the fragment size), and a
    per-fragment shrink ceiling. We do NOT enforce the example block
    here - that's a whole-file signal. A single fragment may be just
    a heading and paragraph with no code block; requiring one would
    reject in-place edits that only change prose. We do NOT enforce
    held-out here either - that's a whole-file signal.
    """
    if not new_text or not new_text.strip():
        return f"fragment_{fragment_id}_empty: proposed fragment is empty"
    if not any(line.lstrip().startswith("#") for line in new_text.splitlines()):
        return f"fragment_{fragment_id}_no_headers: proposed fragment has no markdown headers"
    if len(new_text) < min_chars // 4:  # fragments can be smaller than the whole skill
        return f"fragment_{fragment_id}_too_short: {len(new_text)} chars < {min_chars // 4} chars (min for a fragment)"
    if cur_text:
        if new_text == cur_text:
            return f"fragment_{fragment_id}_byte_identical: fragment unchanged"
        if _normalise(new_text) == _normalise(cur_text):
            return f"fragment_{fragment_id}_no_op: fragment unchanged after whitespace normalisation"
        if len(cur_text) > 0 and len(new_text) < len(cur_text) * max_shrink_ratio:
            return (
                f"fragment_{fragment_id}_shrunk: {len(cur_text)} -> {len(new_text)} chars "
                f"(>{int((1 - max_shrink_ratio) * 100)}% reduction)"
            )
    return None


def build_subprocess_env(env_file: Path | None = None) -> dict[str, str]:
    """Build the env dict for a skillopt subprocess.

    Starts from ``os.environ.copy()`` and overlays any ``export FOO=bar``
    lines from ``logs/runs/.skillopt-env`` (with ``$VAR``/``${VAR}``
    references expanded against the env being built). Done in-Python rather
    than via ``source ... && python ...`` so the call works on any shell,
    including the minimal one inside the A0 container.

    Shared by the detached Sleep cycle launcher
    (:func:`launch_sleep_subprocess`) and the blocking replay-worker spawn
    (``helpers/replay_harness._real_score``). Best-effort: on any parse
    error the parent env is returned unchanged.
    """
    sub_env = os.environ.copy()
    env_file = env_file or (runs_dir() / ".skillopt-env")
    if not env_file.is_file():
        return sub_env
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                val = _expand_env(val, sub_env)
                sub_env[key] = val
    except Exception:
        # Best-effort: continue with the parent env
        pass
    return sub_env


def launch_sleep_subprocess(
    verb: str,
    extra_args: list[str] | None = None,
    log_name: str | None = None,
) -> dict[str, Any]:
    """Launch a `skillopt_sleep` cycle in a background subprocess.

    Sources `logs/runs/.skillopt-env` if present, so the Sleep
    subprocess picks up the same backend credentials the user
    configured (OpenAI-compatible endpoint, Anthropic key, etc.).

    Also bridges the plugin's rollouts into the format the Sleep
    engine's `harvest` verb expects (Claude Code's history.jsonl).
    Without this bridge, the Sleep engine sees 0 sessions because
    it doesn't know to look at our rollouts/ directory.

    v1.1.0:
    - cwd is set to staging_dir() so the engine's `consolidate` verb
      writes its best_skill.md into the place the rest of the
      pipeline expects.
    - Detached correctly on Windows (CREATE_NEW_PROCESS_GROUP |
      DETACHED_PROCESS) and POSIX (start_new_session=True).

    Returns a small dict describing the run (pid, log path, started_at).
    The caller can later poll `is_running(pid)` and tail the log file.
    """
    cmd = _resolve_skillopt_sleep_module() + [verb]
    if extra_args:
        cmd += list(extra_args)

    # Bridge our rollouts into Claude Code's history format so the
    # Sleep engine's `harvest` verb can find them. This is the only
    # way to feed the engine from A0 today - `transcript_source`
    # in the engine config is hardcoded to "claude" / "codex" / "auto".
    try:
        try:
            from usr.plugins.skillopt.helpers.bridge import bridge_rollouts_to_claude_history  # framework layout: /a0 on sys.path
        except ImportError:  # plugin-root layout: runner/wrapper context imports helpers directly
            from helpers.bridge import bridge_rollouts_to_claude_history  # type: ignore
        bridge_result = bridge_rollouts_to_claude_history()
    except Exception as _bridge_err:
        bridge_result = {"rollouts_written": 0, "error": str(_bridge_err)}

    # v1.8.10 fix: point the engine transcript source at the plugin-local
    # bridge cache. Without this the engine reads the default ~/.claude,
    # finds none of our bridged sessions and mines 0 tasks every cycle.
    _bridge_root = str(bridge_result.get("bridge_root") or "")
    if _bridge_root:
        import os as _os
        if _bridge_root != _os.path.expanduser("~/.claude"):
            cmd += ["--claude-home", _bridge_root]
    # Build the subprocess env: start from the parent's env, then overlay any
    # `export FOO=bar` lines from .skillopt-env. Shared with the replay-worker
    # spawn via build_subprocess_env() so both paths apply the same credentials.
    env_file = runs_dir() / ".skillopt-env"
    sub_env = build_subprocess_env(env_file)

    ts = time.strftime("%Y%m%dT%H%M%S")
    log_name = log_name or f"sleep-{verb}-{ts}.log"
    log_path = runs_dir() / log_name
    log_fh = open(log_path, "ab", buffering=0)
    header = (
        f"$ {' '.join(cmd)}\n"
        f"# started at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        f"# env: AZURE_OPENAI_ENDPOINT={sub_env.get('AZURE_OPENAI_ENDPOINT', '<unset>')}, "
        f"AZURE_OPENAI_API_KEY={'set' if sub_env.get('AZURE_OPENAI_API_KEY') else '<unset>'}, "
        f"SKILLOPT_OPTIMIZER_MODEL={sub_env.get('SKILLOPT_OPTIMIZER_MODEL', '<unset>')}\n"
        f"# bridge: {bridge_result['rollouts_written']} rollouts -> "
        f"{bridge_result.get('history_path', '?')}\n"
    ).encode("utf-8")
    log_fh.write(header)

    # Cross-platform detached-subprocess flags — factored into
    # _detached_popen_kwargs() in v1.8.22 so the real-gate worker
    # launcher shares the exact same recipe.
    popen_kwargs: dict[str, Any] = dict(
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(staging_dir()),
        env=sub_env,
        **_detached_popen_kwargs(),
    )

    proc = subprocess.Popen(cmd, **popen_kwargs)
    # v1.8.1: enforce the (previously dead) max_runs_retained retention so
    # every cycle doesn't leave a log behind forever. Best-effort.
    try:
        enforce_run_log_retention()
    except Exception:
        pass
    return {
        "pid": proc.pid,
        "verb": verb,
        "log_path": str(log_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd": cmd,
        "env_applied": env_file.is_file(),
        "bridge": bridge_result,
    }


def _dotenv_fallback(env_file: "Path | None" = None) -> dict:
    """Parse the project's ``usr/.env`` (framework dotenv) as a fallback.

    v1.8.4 SECURITY: optimizer credentials moved from the plugin-local
    ``logs/runs/.skillopt-env`` (plaintext) to ``<project>/usr/.env``
    (chmod 600, outside the plugin repo). ``.skillopt-env`` now holds
    ``$VAR``/``${VAR}`` references only. The live backend or bare-python
    subprocesses started before the migration (e.g. replay workers) do
    not carry the referenced names in ``os.environ``, so expansion falls
    back to parsing the file directly. Portable: resolved relative to
    this plugin root, no hardcoded paths. Best-effort: on any error
    returns ``{}`` and the caller keeps the literal ``$VAR`` text.
    """
    try:
        path = env_file or (plugin_root().parent.parent / ".env")
        if not path.is_file():
            return {}
        out: dict = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip().strip(chr(34)).strip(chr(39))
        return out
    except Exception:
        return {}


def _expand_env(val: str, env: dict) -> str:
    """Expand $VAR and ${VAR} references in `val` from `env`.

    v1.8.1 fix: the previous pattern escaped the alternation pipe
    (``\\}\\|\\$``), which made it a literal ``${FOO}|$BAR`` match — plain
    ``$VAR`` / ``${VAR}`` references were NEVER expanded. Shared by
    sleep_runner.build_subprocess_env and direct_optimizer._read_env_file.

    v1.8.4: names unresolved in both ``env`` and ``os.environ`` fall back
    to the project's ``usr/.env`` (framework dotenv), so ``$VAR``
    indirection also resolves in processes started before the credential
    migration and in bare-python subprocesses.
    """
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
    fallback: dict = {}
    fallback_loaded = False

    def repl(m: "re.Match") -> str:
        nonlocal fallback, fallback_loaded
        name = m.group(1) or m.group(2)
        if name in env:
            return env[name]
        if not fallback_loaded:
            fallback_loaded = True
            fallback = _dotenv_fallback()
        if name in fallback:
            return fallback[name]
        return m.group(0)
    return pattern.sub(repl, val)


def is_running(pid: int) -> bool:
    """Liveness probe for a detached subprocess pid.

    v1.8.1 fix: ``os.kill(pid, 0)`` is NOT a status check on Windows — it
    maps to ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` (a Ctrl+C
    delivery), and TerminateProcess semantics for any other signal value.
    Use OpenProcess/GetExitCodeProcess via ctypes instead. POSIX keeps the
    signal-0 probe (there it is the documented non-destructive check).
    """
    if not pid or int(pid) <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            k32.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if not k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, we just may not signal it
    except OSError:
        return False
    return True


def tail_log(path: str | os.PathLike, max_bytes: int = 8192) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        data = f.read()
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return repr(data)


# ----------------------------------------------------------------------- #
# v1.8.1: official-engine provenance markers for staged proposals
# ----------------------------------------------------------------------- #

OFFICIAL_GATE_MARKER_SUFFIX = ".gate.json"


def _gate_marker_path(staged_path: str | os.PathLike) -> Path:
    """`<staging>/<skill>.md` -> `<staging>/<skill>.md.gate.json` (not matched
    by find_staged_proposals, which filters on .md/.proposed suffixes)."""
    p = Path(staged_path)
    return p.with_suffix(p.suffix + OFFICIAL_GATE_MARKER_SUFFIX)


def write_official_gate_marker(
    staged_path: str | os.PathLike,
    *,
    skill_name: str,
    gate: dict[str, Any] | None = None,
) -> Path:
    """Record that a staged proposal was produced (and gate-accepted) by the
    OFFICIAL Sleep engine.

    v1.8.1: provenance used to live only in auto-loop in-process state
    (``state["last_engine"]``), which is per-skill-last-run — multi-skill
    ticks could mark a direct-engine proposal as official-gated or vice
    versa, and the manual /adopt path had no way to know at all. The
    marker travels WITH the proposal file. Best-effort: never raises.
    """
    marker = _gate_marker_path(staged_path)
    payload = {
        "official_gated": True,
        "skill": skill_name,
        "proposal_id": Path(staged_path).stem,
        "gate_action": (gate or {}).get("gate_action"),
        "baseline_score": (gate or {}).get("baseline_score"),
        "candidate_score": (gate or {}).get("candidate_score"),
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return marker


def read_official_gate_marker(staged_path: str | os.PathLike) -> dict[str, Any] | None:
    """Return the official-gate marker dict for a staged proposal, or None
    when absent/unreadable (i.e. the proposal is NOT official-gated)."""
    marker = _gate_marker_path(staged_path)
    if not marker.is_file():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def clear_official_gate_marker(staged_path: str | os.PathLike) -> None:
    """Remove the marker after the proposal has been consumed (adopted)."""
    try:
        marker = _gate_marker_path(staged_path)
        if marker.exists():
            marker.unlink()
    except Exception:
        pass


# ----------------------------------------------------------------------- #
# v1.8.22: async real-executor confirmation gate (verdict sidecars)
# ----------------------------------------------------------------------- #

REAL_GATE_SIDECAR_SUFFIX = ".md.realgate.json"
REAL_GATE_TASKS_SUFFIX = ".md.realgate.tasks.json"


def _real_gate_sidecar_path(staged_path: str | os.PathLike) -> Path:
    """`<staging>/<skill>.md` -> `<staging>/<skill>.md.realgate.json` (not
    matched by find_staged_proposals, which filters on .md/.proposed)."""
    p = Path(staged_path)
    return p.with_suffix(p.suffix + REAL_GATE_SIDECAR_SUFFIX)


def _real_gate_tasks_path(staged_path: str | os.PathLike) -> Path:
    """`<staging>/<skill>.md` -> `<staging>/<skill>.md.realgate.tasks.json`
    — the held-out task set FROZEN at spawn time, so the worker's
    measurement and the harvester's drift check share one immutable set."""
    p = Path(staged_path)
    return p.with_suffix(p.suffix + REAL_GATE_TASKS_SUFFIX)


def write_real_gate_sidecar(staged_path: str | os.PathLike, payload: dict[str, Any]) -> Path | None:
    """Write/update the real-gate verdict sidecar for a staged proposal.

    Write-then-rename (os.replace) so a reader never sees a torn JSON —
    the sidecar is the single source of truth for pending/done/failed
    across restarts. Best-effort: returns the path, or None on failure.
    """
    try:
        p = _real_gate_sidecar_path(staged_path)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, p)
        return p
    except Exception:
        return None


def read_real_gate_sidecar(staged_path: str | os.PathLike) -> dict[str, Any] | None:
    """Return the real-gate sidecar dict for a staged proposal, or None
    when absent/unreadable (a corrupt sidecar is treated as ABSENT: the
    caller falls through to the normal drain path — never deadlock)."""
    p = _real_gate_sidecar_path(staged_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        try:
            p.unlink()
        except Exception:
            pass
        return None


def find_pending_real_gate_sidecar() -> dict[str, Any] | None:
    """Single-flight primitive: scan staging/ top level for a real-gate
    sidecar with status == 'pending' whose worker pid is STILL RUNNING.
    Returns its payload, or None when no live gate is in flight.

    Derived from the filesystem (not in-memory state), so it survives A0
    restarts and is correct from any process. A 'pending' sidecar with a
    DEAD pid is deliberately NOT returned: the harvester's stale timeout
    owns that case, so a dead worker cannot block future spawns forever.
    Best-effort: never raises."""
    try:
        sd = staging_dir()
        if not sd.is_dir():
            return None
        for child in sd.glob("*" + REAL_GATE_SIDECAR_SUFFIX):
            try:
                data = json.loads(child.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("status") != "pending":
                continue
            if is_running(int(data.get("pid") or 0)):
                return data
    except Exception:
        pass
    return None


def _detached_popen_kwargs() -> dict[str, Any]:
    """Cross-platform detached-subprocess flags (v1.8.22, factored out of
    launch_sleep_subprocess so the real-gate worker uses the same recipe).
    POSIX: start_new_session=True — signals from the parent don't reach
    the child. Windows: CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS so a
    Ctrl-C in the parent console cannot kill it."""
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def launch_real_gate_worker(
    *,
    skill_name: str,
    staged_path: str | os.PathLike,
    sidecar_path: str | os.PathLike,
    tasks_file: str | os.PathLike,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Spawn scripts/replay_gate_worker.py DETACHED to run the real replay
    counterfactual for one staged proposal and write its verdict sidecar.

    Mirrors launch_sleep_subprocess: .skillopt-env credentials, cwd
    staging_dir(), detached flags (win: DETACHED_PROCESS | CREATE_NEW_
    PROCESS_GROUP, posix: start_new_session), stdout redirected to a
    dedicated run log (a detached child with no stdout handle dies on its
    first write on Windows). NEVER adopts — the drain harvests the
    sidecar verdict on a later tick.

    Returns {pid, log_path, started_at, cmd}. Raises on spawn failure
    (the caller has already written the pending sidecar and cleans up)."""
    cmd = [
        _a0_python(),
        str(plugin_root() / "scripts" / "replay_gate_worker.py"),
        "--skill-name", str(skill_name),
        "--staged-path", str(staged_path),
        "--sidecar", str(sidecar_path),
        "--tasks-file", str(tasks_file),
    ]
    if extra_args:
        cmd += [str(a) for a in extra_args]
    env = build_subprocess_env()
    ts = time.strftime("%Y%m%dT%H%M%S")
    log_path = runs_dir() / f"real_gate_worker_{ts}_{skill_name}.log"
    log_fh = open(log_path, "ab", buffering=0)
    log_fh.write(
        (f"$ {' '.join(cmd)}\n"
         f"# started at {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n").encode("utf-8")
    )
    popen_kwargs: dict[str, Any] = dict(
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(staging_dir()),
        env=env,
        **_detached_popen_kwargs(),
    )
    proc = subprocess.Popen(cmd, **popen_kwargs)
    try:
        enforce_run_log_retention()
    except Exception:
        pass
    return {
        "pid": proc.pid,
        "log_path": str(log_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd": cmd,
    }


def rotate_log_if_large(path: str | os.PathLike, max_bytes: int = 5_000_000) -> bool:
    """Rotate a plugin log to `<name>.1` when it exceeds max_bytes.

    v1.8.1: plugin logs (auto_loop.log, inner_loop.log, governance.log,
    adoptions.log, post_adopt.log) previously grew without bound and some
    were fully re-read on every check. One rotated generation is kept;
    older data is dropped. Best-effort: never raises.
    """
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size < max_bytes:
            return False
        rotated = p.with_suffix(p.suffix + ".1")
        try:
            if rotated.exists():
                rotated.unlink()
        except OSError:
            pass
        os.replace(p, rotated)
        # Recreate an empty live log so readers never see a missing file
        # (append-mode writers would recreate it, but tailing readers
        # between rotations would not).
        try:
            p.touch()
        except Exception:
            pass
        return True
    except Exception:
        return False


def enforce_run_log_retention() -> int:
    """Prune old `sleep-*.log` run logs per `max_runs_retained` (0 disables).

    v1.8.1: the `max_runs_retained` config key existed but no code read it,
    so every cycle left a log behind forever. Best-effort: never raises.
    """
    try:
        keep = int(merged_config().get("max_runs_retained", 10) or 0)
    except Exception:
        keep = 10
    if keep <= 0:
        return 0
    try:
        logs = sorted(
            runs_dir().glob("sleep-*.log"),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
    except Exception:
        return 0
    removed = 0
    for p in logs[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


# ----------------------------------------------------------------------- #
# Day-4 item 5: status snapshot helpers (per-skill cadence + budget)
# ----------------------------------------------------------------------- #

def _cadence_snapshot() -> dict:
    """Build the `cadence` block for get_status_snapshot()."""
    try:
        from usr.plugins.skillopt.helpers import cadence  # type: ignore
    except ImportError:
        return {"enabled": False, "reason": "cadence module not loaded"}
    try:
        skills = cadence.list_skills_with_state()
    except Exception:
        skills = []
    per_skill = {}
    for s in skills:
        try:
            st = cadence.load_per_skill_state(s)
            new_n = cadence.count_new_rollouts(s, st["last_run_at"])
            per_skill[s] = {
                "new_rollouts": new_n,
                "last_run_at": st["last_run_at"],
                "next_run_in_s": cadence.compute_next_run(new_n),
                "total_cycles": st["total_cycles"],
            }
        except Exception:
            continue
    return {
        "enabled": True,
        "target_rollouts": cadence.DEFAULT_TARGET,
        "floor_s": cadence.DEFAULT_FLOOR_S,
        "ceiling_s": cadence.DEFAULT_CEILING_S,
        "per_skill": per_skill,
    }


def _budget_snapshot() -> dict:
    """Build the `budget` block for get_status_snapshot()."""
    try:
        from usr.plugins.skillopt.helpers import budget  # type: ignore
    except ImportError:
        return {"enabled": False, "reason": "budget module not loaded"}
    try:
        from usr.plugins.skillopt.helpers import cadence  # type: ignore
        skills = cadence.list_skills_with_state()
    except Exception:
        skills = []
    per_skill = {}
    for s in skills:
        try:
            bt = budget.BudgetTracker(skill_name=s)
            per_skill[s] = bt.get_status()
        except Exception:
            continue
    return {
        "enabled": True,
        "daily_cap_cents": budget.DEFAULT_DAILY_CAP_CENTS,
        "cost_per_call_cents": budget.DEFAULT_COST_PER_CALL_CENTS,
        "per_skill": per_skill,
    }



def get_status_snapshot() -> dict[str, Any]:
    """One-shot summary used by the API endpoint, the banner, and the dashboard."""
    rollouts = list_rollouts()
    skills = list_skills_available()
    staged = [str(p.relative_to(plugin_root())) for p in find_staged_proposals()]
    pkg_info: dict[str, Any] = {}
    try:
        import skillopt_sleep # type: ignore
        pkg_info["present"] = True
        pkg_info["version"] = getattr(skillopt_sleep, "__version__", "unknown")
    except Exception as e:
        pkg_info["present"] = False
        pkg_info["error"] = str(e)
    # Include last auto-loop log tail so the dashboard can show errors
    auto_log = runs_dir() / "auto_loop.log"
    last_err: str | None = None
    if auto_log.is_file():
        try:
            tail = tail_log(auto_log, max_bytes=4096)
            for line in tail.splitlines():
                if "error" in line.lower() or "traceback" in line.lower():
                    last_err = line
        except Exception:
            pass
    # v1.2.0: surface the reward model status. The dashboard reads
    # this so the user can tell at a glance whether the harvester is
    # using a trained model or falling back to the v1.1.0 heuristic.
    # Try the production import path first (when the plugin is
    # installed at usr/plugins/skillopt/), then fall back to a
    # plugin-local import (works in the test harness and during
    # dev work from the plugin root).
    reward_status: dict[str, Any] = {"present": False, "loaded": False}
    try:
        try:
            from usr.plugins.skillopt.helpers import reward_model  # type: ignore  # noqa: E402
        except Exception:
            from helpers import reward_model  # type: ignore  # noqa: E402
        reward_status = reward_model.get_model_status()
    except Exception as e:
        reward_status["error"] = str(e)
    # v1.2.0: surface the A/B harness status. Same import strategy.
    ab_status: dict[str, Any] = {"enabled": True, "can_run_last": False}
    try:
        try:
            from usr.plugins.skillopt.helpers import ab_harness  # type: ignore  # noqa: E402
        except Exception:
            from helpers import ab_harness  # type: ignore  # noqa: E402
        ab_status = ab_harness.get_ab_status()
    except Exception as e:
        ab_status["error"] = str(e)
    # v1.2.0 (Day-3 item 3): surface the fragment store status.
    fragments_status: dict[str, Any] = {"present": False}
    try:
        try:
            from usr.plugins.skillopt.helpers import fragment_store  # type: ignore  # noqa: E402
        except Exception:
            from helpers import fragment_store  # type: ignore  # noqa: E402
        fragments_status = fragment_store.get_fragments_status()
    except Exception as e:
        fragments_status["error"] = str(e)
    # v1.3.0 (Day-4 item 4): surface the inner-loop status. The
    # dashboard reads this so the user can see at a glance whether
    # the per-rollout suggestion engine is alive, how many
    # suggestions it has produced, and which skills have pending
    # suggestions. Inner-loop errors are surfaced via `last_error`
    # in this block; the outer dashboard also picks up the tail of
    # auto_loop.log for the inner-loop log lines.
    inner_status: dict[str, Any] = {"enabled": True}
    try:
        try:
            from usr.plugins.skillopt.helpers import inner_loop  # type: ignore  # noqa: E402
        except Exception:
            from helpers import inner_loop  # type: ignore  # noqa: E402
        inner_status = inner_loop.get_inner_status()
    except Exception as e:
        inner_status["error"] = str(e)
    # v1.3.0 (Day-4 item 6): surface the failure-memory status. The
    # dashboard reads this so the user can see at-a-glance whether the
    # memory backend (A0 vector store or local JSON fallback) is alive,
    # how many failures have been recorded, and which skills have
    # pending failure context. Errors are surfaced via  in
    # this block; the cycle log lives at logs/runs/failure_memory.log.
    failure_status: dict[str, Any] = {"enabled": True}
    try:
        try:
            from usr.plugins.skillopt.helpers import failure_memory  # type: ignore  # noqa: E402
        except Exception:
            from helpers import failure_memory  # type: ignore  # noqa: E402
        failure_status = failure_memory.get_status_block()
    except Exception as e:
        failure_status["error"] = str(e)
    # v1.4.0-Dev (Day-5 item 7): cycle_history block. Mirrors the
    # failure_memory / cadence / budget pattern: lazy import, try/except
    # so a missing helper surfaces as {"available": False, "error": ...}
    # rather than crashing the status snapshot. Backed by
    # logs/runs/cycle_history.{jsonl,log}; see helpers/cycle_history.py.
    cycle_history_status: dict[str, Any] = {"enabled": True, "available": True}
    try:
        try:
            from usr.plugins.skillopt.helpers import cycle_history  # type: ignore  # noqa: E402
        except Exception:
            from helpers import cycle_history  # type: ignore  # noqa: E402
        cycle_history_status = cycle_history.get_history_status()
    except Exception as e:
        cycle_history_status["available"] = False
        cycle_history_status["error"] = str(e)
    # v1.5.0-Dev (Day-5 item 8): governance block. Same lazy-import +
    # try/except convention as the other helper blocks. Backed by
    # helpers/governance.py + logs/runs/governance.log + the per-skill
    # .skillopt.optout / .skillopt.optin / .skillopt.policy.json markers
    # under <a0>/usr/skills/<name>/. The block is read-only and best-
    # effort: a missing helper or missing skills dir surfaces as
    # {"available": False, "error": ...} instead of crashing.
    governance_status: dict[str, Any] = {"enabled": True, "available": True}
    try:
        try:
            from usr.plugins.skillopt.helpers import governance  # type: ignore  # noqa: E402
        except Exception:
            from helpers import governance  # type: ignore  # noqa: E402
        governance_status = governance.get_governance_status()
    except Exception as e:
        governance_status["available"] = False
        governance_status["error"] = str(e)
    snap: dict[str, Any] = {
        "rollout_count": len(rollouts),
        "rollouts_path": str(rollouts_dir()),
        "skills_available": skills,
        "skills_count": len(skills),
        "staged_proposals": staged,
        "package": pkg_info,
        "plugin_root": str(plugin_root()),
        "a0_python": _a0_python(),
        "platform": sys.platform,
        "reward_model": reward_status,
        "ab_harness": ab_status,
        "fragments": fragments_status,
        "inner_loop": inner_status,
        # Day-4 item 5: per-skill cadence + per-skill budget
        "cadence": _cadence_snapshot(),
        "budget": _budget_snapshot(),
        # Day-4 item 6: failure memory (per-skill attribution)
        "failure_memory": failure_status,
        # Day-5 item 7: per-cycle history (append-only JSONL of every
        # _auto_adopt() outcome; the per-cycle dashboard mount reads it).
        "cycle_history": cycle_history_status,
        # Day-5 item 8: per-skill governance (opt-out + per-skill policy)
        "governance": governance_status,
    }
    if last_err:
        snap["last_auto_loop_error"] = last_err
    return snap


# v1.8.20 (P5): POSIX zombie-aware liveness for the detached engine child.
# The official gate poll loop waits on is_running(pid); the exited-but-
# unreaped engine (zombie, direct child not yet waited on) still answers
# os.kill(pid, 0), which spun the loop until the full timeout even though
# the engine had already written report.json. Treat /proc state Z as not
# running (Linux only); other platforms delegate to the original probe.
_is_running_original = is_running


def _is_running_zombie_aware(pid: int) -> bool:
    if not pid or int(pid) <= 0:
        return False
    if sys.platform.startswith("linux"):
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            with open("/proc/%d/stat" % int(pid), "rb") as fh:
                data = fh.read().decode("utf-8", "replace")
            rest = data[data.rfind(")") + 2:] if ")" in data else data
            state = rest.split(" ", 1)[0] if rest else ""
            if state == "Z":
                return False
        except Exception:
            pass
        return True
    return _is_running_original(pid)


is_running = _is_running_zombie_aware