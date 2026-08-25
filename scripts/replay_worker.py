#!/usr/bin/env python3
"""
SkillOpt replay worker - v1.8.0 real A0-agent-loop replay executor (P1).

This is the subprocess entrypoint that `helpers/replay_harness._real_score`
shells out to. It runs ONE held-out task through a real Agent Zero monologue
under a given skill, scores the final response with the reward model, and
writes a single JSON result to `--out`. The parent (`_real_score`) calls it
2*N times (current + proposed skill x N held-out tasks) and collects the
scores into the counterfactual verdict.

Why a subprocess (not in-process)?
  - Side-effect containment: the worker runs in a temp working directory so
    a replay agent that calls file/shell tools cannot mutate the host. (The
    agent's workdir is pinned via `initialize_agent(override_settings=
    {"workdir_path": ...})` AND we `os.chdir` to it as belt-and-suspenders.)
  - Clean async boundary: the worker owns its own event loop, so there is no
    "asyncio event loop already running" problem when `_real_score` is called
    from the auto-loop thread or an async WebUI handler.
  - Recursion isolation: `SKILLOPT_REPLAY_MODE=1` is set in the parent AND in
    the child, so the replay agent's own `monologue_end` (harvester) and
    `agent_init` (auto-loop starter) short-circuit and neither pollute the
    training set nor spawn a nested optimizer loop.

A0 imports are LAZY (inside functions, never at module top) so this module is
importable by the smoke tests without the A0 framework present.

CLI:
  <a0_venv_python> scripts/replay_worker.py \\
      --skill-name <name> --skill-md <file> --task <file> --out <score.json>

`--task` may be a JSON dict ({"task": "...", ...}) or plain text (treated as
{"task": <text>}). `--out` always receives a JSON object:
  success path : {"score": 0.0|0.5|1.0, "outcome", "source", "confidence", "response"}
  error path   : {"score": null, "error": "...", "traceback": "..."}
Exit code 0 on success, 1 on any failure (the envelope is still written).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path
from typing import Any

PLUGIN_NAME = "skillopt"

# outcome -> base score. Mirrors helpers/replay_harness._OUTCOME_BASE. Kept
# locally so a failed import of replay_harness never breaks the worker; the
# values are part of the plugin's frozen 3-class contract.
_OUTCOME_BASE = {"success": 1.0, "partial": 0.5, "failure": 0.0}

_syspath_done = False


def _plugin_root() -> Path:
    """<plugin> dir = parent of scripts/."""
    return Path(__file__).resolve().parent.parent


def _a0_root() -> Path:
    """The Agent Zero project root.

    Canonical layout is <a0>/usr/plugins/<plugin>, so a0_root = plugin_root
    .parent.parent.parent. Overridable via SKILLOPT_A0_ROOT for non-standard
    deployments.
    """
    override = os.environ.get("SKILLOPT_A0_ROOT")
    if override:
        return Path(override)
    return _plugin_root().parent.parent.parent


def _setup_syspath() -> None:
    """Pin sys.path so A0 framework's `helpers` package wins the namespace.

    Idempotent. MUST run at module import time (not lazily) so that any
    import chain that touches A0 core (e.g. ``from initialize import
    initialize_agent`` -> ``from agent import Agent`` -> ``from helpers
    import dotenv``) resolves `helpers` to /a0/helpers/, NOT to the plugin's
    /a0/usr/plugins/skillopt/helpers/ (namespace collision).

    Behaviour:
    - Strip /a0/usr/plugins/skillopt (the plugin dir) from sys.path
    COMPLETELY. We do not need it on sys.path because plugin-local modules
    (reward_model, replay_harness, etc.) are loaded directly via
    importlib.util in _load_plugin_helper().
    - Insert /a0 at sys.path[0] so it is found first.
    - Clear every cached `helpers` / `helpers.*` entry from sys.modules so
    any subsequent ``from helpers import ...`` re-resolves against /a0.
    """
    global _syspath_done
    if _syspath_done:
        return
    a0 = str(_a0_root())
    plug = str(_plugin_root())
    # 1. Remove the plugin dir from sys.path entirely (it shadows `helpers`).
    sys.path[:] = [p for p in sys.path if p != plug and p != a0]
    # 2. Put A0 at sys.path[0].
    sys.path.insert(0, a0)
    # 3. Drop any cached `helpers` modules so the next import re-resolves.
    for key in list(sys.modules.keys()):
        if key == "helpers" or key.startswith("helpers."):
            del sys.modules[key]
    _syspath_done = True


# CRITICAL: invoke at module import time, BEFORE any A0 import can fire.
# A0 core does lazy imports inside functions, but downstream code paths
# (e.g. asyncio.run -> _run_monologue -> `from initialize import ...`)
# reach into /a0 via `from helpers import dotenv` synchronously during
# AgentContext construction.
#
# IMPORTANT: _setup_syspath() is NOT called at module import time. Doing so
# would break smoke tests and any in-process import of this module, because
# it strips the plugin path from sys.path and clears all cached `helpers`
# modules — making subsequent `from helpers import direct_optimizer` (etc.)
# resolve to A0's helpers/ instead of the plugin's.
# Instead, _setup_syspath() is called inside _run_monologue() and
# _score_response(), right before A0 imports are needed. The subprocess is
# spawned with PYTHONPATH=/a0 (no plugin path) by replay_harness._real_score,
# so the worker never has the plugin path on sys.path to begin with.


def _load_plugin_helper(name: str):
    """Load a helper module from the plugin's helpers/ dir via importlib.

    Needed because the plugin's helpers/ package shares the name 'helpers'
    with A0's framework helpers/. With A0 root at sys.path[0], 'helpers'
    resolves to /a0/helpers/. This function loads directly from the plugin
    path, avoiding the namespace collision.
    """
    import importlib.util
    plug = _plugin_root()
    mod_path = plug / 'helpers' / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f'skillopt_helpers.{name}', mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_task(path: str) -> dict[str, Any]:
    """Load the task file. JSON dict -> as-is; plain text -> {"task": text}."""
    raw = Path(path).read_text(encoding="utf-8")
    stripped = raw.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {"task": raw}


def _inject_skill(agent: Any, skill_name: str, skill_md: str) -> None:
    """Seed the agent's history so it 'sees' the skill as loaded.

    Mirrors what tools/skills_tool._load does, but driven directly from a
    raw skill_md (a counterfactual skill text, not a disk skill):

      1. skills.add_loaded_skill_name(agent, name)  -> the loaded-skills ledger
      2. agent.hist_add_tool_result("skills_tool", skill_md,
           skill_instructions={...content_included:True...})
         -> a history tool-result message skill_instruction_name() matches

    IMPORTANT: hist_add_tool_result has NO `additional` param - it takes
    **kwargs. `skill_instructions` is a TOP-LEVEL kwarg here, NOT nested
    under `additional` (that unwrap happens inside Tool.after_execution,
    which we bypass when driving history directly). The earlier stub
    docstring in replay_harness.py got this wrong; this is the corrected
    recipe.
    """
    _setup_syspath()
    import helpers.skills as skills  # type: ignore

    skills.add_loaded_skill_name(agent, skill_name, limit=skills.MAX_ACTIVE_SKILLS)
    agent.hist_add_tool_result(
        "skills_tool",
        skill_md,
        id=uuid.uuid4().hex,
        skill_instructions={
            "name": skill_name,
            "path": "<replay>",
            "source": "replay:inject",
            "content_included": True,
        },
    )


async def _run_monologue(
    skill_name: str, skill_md: str, task_text: str, workdir: str
) -> dict[str, str]:
    """Run one A0 monologue under the given skill. Returns {response, task}.

    Sets the recursion guard BEFORE creating the AgentContext (agent_init
    fires synchronously inside Agent.__init__) and removes the context from
    the class-level registry in finally so a long-lived parent process does
    not accumulate replay contexts.
    """
    _setup_syspath()
    from initialize import initialize_agent  # type: ignore
    from agent import AgentContext, UserMessage  # type: ignore

    os.environ["SKILLOPT_REPLAY_MODE"] = "1"
    # Belt-and-suspenders containment: file tools read workdir from
    # settings["workdir_path"], but chdir is free and covers any tool that
    # resolves a relative path from cwd.
    os.chdir(workdir)

    config = initialize_agent(override_settings={"workdir_path": str(workdir)})
    ctx = AgentContext(config=config)
    try:
        agent = ctx.agent0
        _inject_skill(agent, skill_name, skill_md)
        task = ctx.communicate(UserMessage(message=task_text))
        response = await task.result()
        # loop_data.last_response holds the final AI turn's text (the
        # response-tool message). Fall back to the communicate() return.
        last = ""
        try:
            last = (ctx.agent0.loop_data.last_response or "") if ctx.agent0 else ""
        except Exception:
            last = ""
        return {"response": last or response or "", "task": task_text}
    finally:
        try:
            AgentContext.remove(ctx.id)
        except Exception:
            pass


def _score_response(response: str, task_text: str) -> dict[str, Any]:
    """Score the final response via the reward model. Never raises.

    Returns the score_rollout dict. When no model is trained this falls back
    to the heuristic (same path the harvester uses), so the worker is useful
    even before P5/P6 land.
    """
    reward_model = _load_plugin_helper('reward_model')

    return reward_model.score_rollout(
        {"task": task_text, "last_response": response, "outcome": ""}
    )


def _write_out(out_path: str, payload: dict[str, Any]) -> None:
    """Best-effort write of the result envelope to --out."""
    try:
        Path(out_path).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        # Last resort: print so the parent at least sees it in captured stdout.
        print(f"[{PLUGIN_NAME}] FAILED to write --out {out_path}: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="SkillOpt replay worker: run one A0 monologue under a skill and score it.",
    )
    p.add_argument("--skill-name", required=True, help="The skill name to inject.")
    p.add_argument("--skill-md", required=True, help="Path to the skill markdown text to test.")
    p.add_argument("--task", required=True, help="Path to the task (JSON dict or plain text).")
    p.add_argument("--out", required=True, help="Path to write the result JSON envelope.")
    p.add_argument("--workdir", default=None, help="Working dir for the replay agent. Default: a fresh temp dir.")
    args = p.parse_args(argv)

    # Resolve a workdir. Auto-created temp dirs are cleaned in finally; an
    # explicit --workdir is left in place (the caller owns it).
    owns_workdir = not args.workdir
    workdir = args.workdir or tempfile.mkdtemp(prefix="skillopt_replay_")

    try:
        task_rec = _load_task(args.task)
        task_text = str(task_rec.get("task") or "").strip()
        if not task_text:
            raise ValueError("task file has no `task` text")
        skill_md = Path(args.skill_md).read_text(encoding="utf-8")

        print(f"[{PLUGIN_NAME}] replay worker: skill={args.skill_name} workdir={workdir}")
        result = asyncio.run(
            _run_monologue(args.skill_name, skill_md, task_text, workdir)
        )
        response = result.get("response") or ""

        score = _score_response(response, task_text)
        outcome = str(score.get("outcome") or "failure")
        payload = {
            "score": _OUTCOME_BASE.get(outcome, 0.5),
            "outcome": outcome,
            "source": score.get("source"),
            "confidence": score.get("confidence"),
            "response": response[:2000],
        }
        _write_out(args.out, payload)
        print(
            f"[{PLUGIN_NAME}] replay worker OK: outcome={outcome} "
            f"source={score.get('source')} score={payload['score']}"
        )
        return 0
    except Exception as e:  # noqa: BLE001
        _write_out(
            args.out,
            {
                "score": None,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc()[:4000],
            },
        )
        print(f"[{PLUGIN_NAME}] replay worker ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if owns_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())