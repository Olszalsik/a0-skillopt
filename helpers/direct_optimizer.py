"""
Direct skill optimizer - Option 3 implementation.

Bypasses the SkillOpt Sleep engine entirely. Reads A0 rollouts
directly, groups by skill, calls the ollama_cloud LLM (OpenAI-
compatible) to propose an improved skill document, and writes it
to staging/.

This is a self-contained, minimal implementation of the SkillOpt
paper's core idea:
1. Read rollouts (task, trajectory, outcome)
2. Group by skill used
3. For each skill with enough data, call the LLM with:
- the current skill document
- a summary of successful rollouts
- a summary of failed rollouts
4. The LLM proposes an improved skill document
5. Write the proposal to staging/<skill>.md
6. Write a critique file describing what changed and why
   (logs/runs/critiques/<skill>_<ts>.md) so the user can review
   before auto-adopt. v1.1.0 - per the SkillOpt paper, this is
   the most important piece of "self-evolution" transparency.

The existing validation gate (auto_loop._auto_adopt and the
post-adopt hook) handles promotion.

v1.1.0 changes:
- Default model pulled from SKILLOPT_OPTIMIZER_MODEL env or the
  new `optimizer_model` default config key. The hardcoded
  `gemma4:31b` placeholder is gone.
- Writes a per-cycle critique Markdown file so the user can review
  before auto-adopt.

v1.6.0 — FALLBACK ROLE (Solution B):
- The auto-loop now prefers the official `skillopt_sleep` engine via
  helpers/official_adapter.py when `use_official_engine` is true and
  the package is importable. This module is the FALLBACK: it runs when
  the official package is absent or an official run fails
  (official_adapter returns fallback_to_direct=True). It is also still
  used per-skill when only some skills fall back. The function
  signatures (optimize_skill / run_direct_cycle) are unchanged so the
  existing tests + adopt path keep working. Do not assume this is the
  primary optimizer in v1.6.0+; it is the safety net.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore


A0_SKILLS_DIR = Path("/a0/usr/skills")


def _read_skill_doc(skill_name: str) -> str:
    p = A0_SKILLS_DIR / skill_name / "SKILL.md"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def _read_env_file() -> dict:
    """Read the .skillopt-env file and parse export statements."""
    env_file = sleep_runner.runs_dir() / ".skillopt-env"
    out: dict = {}
    if not env_file.is_file():
        return out
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
            if val.startswith("$") or "${" in val:
                import re
                pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\|\$([A-Za-z_][A-Za-z0-9_]*)")
                def repl(m):
                    name = m.group(1) or m.group(2)
                    return os.environ.get(name, m.group(0))
                val = pattern.sub(repl, val)
            out[key] = val
    return out


def _default_model() -> str:
    """Resolve the optimizer model from env, env-file, config, fallback."""
    env = _read_env_file()
    env_model = env.get("SKILLOPT_OPTIMIZER_MODEL") or os.environ.get("SKILLOPT_OPTIMIZER_MODEL")
    if env_model:
        return env_model
    cfg = sleep_runner.merged_config()
    return cfg.get("optimizer_model") or "minimax-m3"


_OPTIMIZER_SYSTEM = (
    "You are an expert at improving Agent Zero skill documents. You receive "
    "the current skill, a list of successful uses, and a list of failed uses. "
    "You output ONLY the improved skill document - no preamble, no explanation, "
    "no markdown fences."
)


def _call_llm(prompt: str, model: str, max_tokens: int = 2000, system: str | None = None) -> str:
    """Call the OpenAI-compatible LLM via the openai package.

    ``system`` overrides the default optimizer system prompt - the LLM judge
    (helpers/llm_judge.py) passes its own classification instruction. Defaults
    to the skill-improvement prompt for backwards compatibility.
    """
    env = _read_env_file()
    base_url = env.get("AZURE_OPENAI_ENDPOINT", "https://ollama.com/v1")
    api_key = env.get("AZURE_OPENAI_API_KEY", "")
    if not api_key:
        api_key = os.environ.get("OLLAMA_API_KEY") or os.environ.get("API_KEY_OLLAMA_CLOUD") or ""
    if not api_key:
        raise RuntimeError(
            "No LLM API key found. Set OLLAMA_API_KEY in the container env, "
            "or write it to /a0/usr/plugins/skillopt/logs/runs/.skillopt-env."
        )
    from openai import OpenAI
    client = OpenAI(base_url=base_url, api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system or _OPTIMIZER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _build_prompt(skill_name: str, current_skill: str, successes: list, failures: list) -> str:
    """Build the prompt for the LLM to propose an improved skill."""
    parts = []
    parts.append(f"Skill: {skill_name}\n")
    if current_skill:
        parts.append("=== CURRENT SKILL DOCUMENT ===\n")
        parts.append(current_skill)
        parts.append("\n\n")
    else:
        parts.append("(No current skill document - propose a new one.)\n\n")
    parts.append(f"=== SUCCESSFUL USES ({len(successes)}) ===\n")
    for i, r in enumerate(successes[:8]):
        task = r.get("task", "")
        traj = r.get("trajectory") or r.get("steps") or []
        parts.append(f"  {i+1}. Task: {task}\n")
        if traj:
            parts.append(f"  Steps: {' | '.join(str(s) for s in traj[:5])}\n")
        parts.append("\n")
    parts.append(f"=== FAILED USES ({len(failures)}) ===\n")
    for i, r in enumerate(failures[:8]):
        task = r.get("task", "")
        traj = r.get("trajectory") or r.get("steps") or []
        parts.append(f"  {i+1}. Task: {task}\n")
        if traj:
            parts.append(f"  Steps: {' | '.join(str(s) for s in traj[:5])}\n")
        parts.append("\n")
    parts.append("=== INSTRUCTIONS ===\n")
    parts.append(
        "Analyze the successful and failed uses. Identify what the failed "
        "uses did wrong that the successful ones did right. Then output an "
        "IMPROVED version of the skill document that addresses the failure "
        "modes while keeping the successful patterns.\n\n"
        "Output ONLY the improved skill document text - no preamble, no "
        "explanation, no markdown code fences. Start directly with a "
        "top-level heading.\n"
    )
    return "".join(parts)


def _extract_skill_text(llm_response: str) -> str:
    """Strip any accidental markdown fences or preamble from the LLM response."""
    text = llm_response.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl+1:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _write_critique(skill_name: str, current: str, improved: str, successes: int, failures: int, model: str) -> Path | None:
    """Write a human-readable critique file alongside the staged proposal.

    v1.1.0 transparency: the user reviews this before auto-adopt.
    """
    cfg = sleep_runner.merged_config()
    critique_dir_rel = cfg.get("critique_dir", "logs/runs/critiques")
    if not os.path.isabs(critique_dir_rel):
        critique_dir = sleep_runner.plugin_root() / critique_dir_rel
    else:
        critique_dir = Path(critique_dir_rel)
    try:
        critique_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%S")
        p = critique_dir / f"{skill_name}_{ts}.md"
        # Tiny diff summary: number of new/removed/changed lines
        cur_lines = set((current or "").splitlines())
        imp_lines = set((improved or "").splitlines())
        added = sorted(imp_lines - cur_lines)
        removed = sorted(cur_lines - imp_lines)
        p.write_text(
            (
                f"# SkillOpt Critique - {skill_name}\n\n"
                f"- generated: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
                f"- model: `{model}`\n"
                f"- rollouts used: {successes} successes, {failures} failures\n"
                f"- current size: {len(current)} chars / {len(cur_lines)} lines\n"
                f"- proposed size: {len(improved)} chars / {len(imp_lines)} lines\n"
                f"- net change: +{len(added)} / -{len(removed)} lines\n\n"
                f"## What the model was told\n\n"
                f"Analyze the {successes} successful and {failures} failed uses "
                f"of `{skill_name}`, identify what the failed uses did wrong "
                f"that the successful ones did right, and propose a new "
                f"version of the skill document.\n\n"
                f"## First 20 added lines (preview)\n\n"
                + "\n".join(f"+ {ln}" for ln in added[:20])
                + "\n\n## First 20 removed lines\n\n"
                + "\n".join(f"- {ln}" for ln in removed[:20])
                + "\n"
            ),
            encoding="utf-8",
        )
        return p
    except Exception:
        return None


def optimize_skill(skill_name: str, min_rollouts: int = 3, model: str | None = None,
                   *, custom_prompt: str | None = None) -> dict:
    """Run the direct optimizer for one skill. Returns a status dict.

    v1.3.0 (Day-4 item 4): when `custom_prompt` is provided, it replaces
    the default `_build_prompt(...)` output. The auto-loop uses this
    to feed a targeted prompt (built from inner-loop suggestions) into
    the LLM, so the LLM produces a minimal edit instead of a full
    rewrite. `custom_prompt` is expected to already contain the
    current SKILL.md + the per-rollout suggestions.
    """
    model = model or _default_model()

    rollouts = []
    for rp in sleep_runner.rollouts_dir().glob("*.json"):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if r.get("skill_used") == skill_name:
            rollouts.append(r)

    if len(rollouts) < min_rollouts:
        return {"ok": False, "skill": skill_name, "reason": f"only {len(rollouts)} rollouts (need {min_rollouts})", "rollouts": len(rollouts)}

    successes = [r for r in rollouts if r.get("outcome") == "success"]
    failures = [r for r in rollouts if r.get("outcome") in ("failure", "partial")]
    if not successes and not failures:
        return {"ok": False, "skill": skill_name, "reason": "no successes or failures to learn from"}

    current_skill = _read_skill_doc(skill_name)
    if custom_prompt is not None and custom_prompt.strip():
        prompt = custom_prompt
        prompt_source = "targeted"  # targeted = inner-loop driven
    else:
        prompt = _build_prompt(skill_name, current_skill, successes, failures)
        prompt_source = "generic"   # generic = full rewrite from rollouts

    try:
        improved = _call_llm(prompt, model=model)
    except Exception as e:
        return {"ok": False, "skill": skill_name, "reason": f"LLM call failed: {e}"}

    improved = _extract_skill_text(improved)
    if not improved or len(improved) < 100:
        return {"ok": False, "skill": skill_name, "reason": f"LLM output too short ({len(improved)} chars)"}

    staging_path = sleep_runner.staging_dir() / f"{skill_name}.md"
    staging_path.write_text(improved, encoding="utf-8")
    critique_path = _write_critique(skill_name, current_skill, improved, len(successes), len(failures), model)
    return {
        "ok": True,
        "skill": skill_name,
        "staging_path": str(staging_path),
        "critique_path": str(critique_path) if critique_path else None,
        "rollouts_used": len(rollouts),
        "successes": len(successes),
        "failures": len(failures),
        "improved_size": len(improved),
        "current_size": len(current_skill),
        "model": model,
    }


def run_direct_cycle(target: str = "", min_rollouts: int = 3,
                     *, custom_prompts: dict[str, str] | None = None) -> dict:
    """Run the direct optimizer across all skills (or just `target`).

    Returns a per-skill status dict; logs to auto_loop.log via sleep_runner.

    v1.3.0 (Day-4 item 4): when `custom_prompts` is provided, the per-skill
    entry is used as the LLM prompt instead of the default built prompt.
    The auto-loop passes a {skill_name: targeted_prompt} dict built from
    inner-loop suggestions. Skills not in the dict use the default prompt.
    """
    rollouts: dict[str, list[dict]] = defaultdict(list)
    for rp in sleep_runner.rollouts_dir().glob("*.json"):
        try:
            r = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        sk = (r.get("skill_used") or "").strip()
        if not sk:
            continue
        if target and sk != target:
            continue
        rollouts[sk].append(r)

    results: list[dict] = []
    for skill_name, skill_rollouts in rollouts.items():
        if len(skill_rollouts) < min_rollouts:
            results.append({
                "ok": False,
                "skill": skill_name,
                "reason": f"only {len(skill_rollouts)} rollouts (need {min_rollouts})",
            })
            continue
        cp = (custom_prompts or {}).get(skill_name)
        res = optimize_skill(skill_name, min_rollouts=min_rollouts, custom_prompt=cp)
        results.append(res)
    return {
        "ok": True,
        "skills_processed": len(results),
        "results": results,
    }
