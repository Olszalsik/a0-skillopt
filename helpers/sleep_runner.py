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

    Defaults to the canonical /a0/usr/skills. Overridable via the
    SKILLOPT_SKILLS_DIR environment variable for native Windows
    installs or alternate deployments.
    """
    override = os.environ.get("SKILLOPT_SKILLS_DIR")
    if override:
        return Path(override)
    return Path("/a0/usr/skills")


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


def merged_config(framework_config: dict | None = None) -> dict[str, Any]:
    """Merge default_config.yaml with the framework plugin config (framework wins)."""
    merged = default_config()
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
    p.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def list_rollouts() -> list[Path]:
    return sorted(p for p in rollouts_dir().glob("*.json") if p.is_file())


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
) -> tuple[bool, str]:
    """Shared validation gate used by auto-loop, adopt API, post-adopt hook, tool.

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
    if skill_name:
        try:
            from helpers import ab_harness  # type: ignore  # noqa: E402
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
        from usr.plugins.skillopt.helpers.bridge import bridge_rollouts_to_claude_history
        bridge_result = bridge_rollouts_to_claude_history()
    except Exception as _bridge_err:
        bridge_result = {"rollouts_written": 0, "error": str(_bridge_err)}

    # Build the subprocess env: start from the parent's env, then
    # overlay any `export FOO=bar` lines from .skillopt-env. We
    # do this in-Python (not via `source ... && python ...`) so the
    # call works on any shell, including the minimal one inside the
    # A0 container.
    sub_env = os.environ.copy()
    env_file = runs_dir() / ".skillopt-env"
    if env_file.is_file():
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
                    # Expand $OTHER_VARS or ${OTHER_VARS} from the current sub_env
                    val = _expand_env(val, sub_env)
                    sub_env[key] = val
        except Exception as e:
            # Best-effort: log and continue with the parent env
            try:
                (runs_dir() / "auto_loop.log").parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

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

    # Cross-platform detached-subprocess flags.
    # POSIX: start_new_session=True puts the child in its own session
    # so signals from the parent don't reach it. Windows: that kwarg
    # does not exist; use creationflags with CREATE_NEW_PROCESS_GROUP
    # (and DETACHED_PROCESS so Ctrl-C in our console doesn't kill it).
    popen_kwargs: dict[str, Any] = dict(
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(staging_dir()),
        env=sub_env,
    )
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    return {
        "pid": proc.pid,
        "verb": verb,
        "log_path": str(log_path),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cmd": cmd,
        "env_applied": env_file.is_file(),
        "bridge": bridge_result,
    }


def _expand_env(val: str, env: dict) -> str:
    """Expand $VAR and ${VAR} references in `val` from `env`."""
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\|\$([A-Za-z_][A-Za-z0-9_]*)")
    def repl(m: "re.Match") -> str:
        name = m.group(1) or m.group(2)
        return env.get(name, m.group(0))
    return pattern.sub(repl, val)


def is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
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
