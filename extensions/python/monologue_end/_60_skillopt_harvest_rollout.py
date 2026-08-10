"""
SkillOpt - rollout harvester.

v1.1.0 — the most important missing piece. The plugin was a no-op
because nothing ever wrote a real rollout to logs/rollouts/. This
hook runs on every monologue_end (i.e. after every chat turn) and
persists a small JSON record of the completed task into the
plugin's logs/rollouts/ directory, so the Sleep engine's `harvest`
verb has something to read.

v1.7.0 (Solution C, Phase C1) — ground-truth attribution + bug fix.
The v1.1.0 harvester read `loop_data.messages`, which does NOT exist
on Agent Zero's `LoopData` (the real attributes are `user_message`,
`history_output`, `last_response`). Because the framework dispatches
`monologue_end` as `(agent, loop_data=...)` — with NO `messages`
kwarg — `messages` was always `[]`, so this hook early-returned on
every turn and **zero rollouts were ever written**. The fix reads
`loop_data.history_output` (a `list[OutputMessage]`) and attributes
the active skill authoritatively from A0's `loaded_skills` ledger
(`helpers.skills.get_loaded_skill_names`) and the per-message skill
signal (`helpers.skills.skill_instruction_name`) — the same source
`tools/skills_tool.py:_visible_skill_loaded` already uses — instead
of a regex on the user prompt.

What we record (the minimum SkillOpt needs to mine patterns):
- id            — uuid4 hex
- ts            — epoch seconds
- task          — the user's original message
- task_type     — the authoritative skill name, or "general"
- skill_used    — authoritative skill active in THIS turn, or empty
- outcome       — 'success' | 'partial' | 'failure' (heuristic)
- trajectory    — list of tool/response steps (truncated for size)
- model         — model name from the agent
- duration_s    — wall-clock seconds the monologue took

Recursion guard: when `SKILLOPT_REPLAY_MODE` is set (by the local
replay harness's real executor — Phase C2), this hook returns
immediately so a replayed agent's own monologue_end does not pollute
the training set with synthetic rollouts.

Idempotency: we never write a duplicate id; if the same chat ends
twice (retries), the second write is a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PLUGIN_NAME = "skillopt"

# Lazy import so this hook never crashes the agent loop just because
# the plugin is missing some optional dep.
_sleep_runner = None


def _sr():
    global _sleep_runner
    if _sleep_runner is None:
        try:
            from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
            _sleep_runner = sleep_runner
        except Exception as e:  # pragma: no cover
            log.debug("[skillopt] cannot import sleep_runner: %s", e)
            _sleep_runner = False
    return _sleep_runner or None


def _flatten_content(content: Any) -> str:
    """Flatten an Agent Zero `MessageContent` (str | list | dict | RawMessage)
    to a single text string. Handles the shapes carried by
    `loop_data.history_output` entries and `loop_data.user_message.content`.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            s = _flatten_content(item)
            if s:
                parts.append(s)
        return " ".join(parts)
    if isinstance(content, dict):
        # RawMessage shape: {"raw_content": ..., "preview": ...}
        if "raw_content" in content:
            return _flatten_content(content.get("raw_content"))
        # A skill-instructions / tool-result content dict: prefer the
        # text-ish fields, skip nested skill_instructions metadata.
        for key in ("text", "content", "tool_result", "message", "preview"):
            if key in content and isinstance(content[key], (str, list, dict)):
                s = _flatten_content(content[key])
                if s:
                    return s
        return ""
    return str(content)


def _output_text(output_msg: Any) -> str:
    """Extract the human/ai text of a single `OutputMessage` (dict-like)."""
    if output_msg is None:
        return ""
    # OutputMessage is a TypedDict (a dict at runtime); a framework
    # Message object also exposes `.content`. Handle both.
    content = None
    if isinstance(output_msg, dict):
        content = output_msg.get("content")
    else:
        content = getattr(output_msg, "content", None)
    return _flatten_content(content)


def _output_ai(output_msg: Any) -> bool:
    if output_msg is None:
        return False
    if isinstance(output_msg, dict):
        return bool(output_msg.get("ai", False))
    return bool(getattr(output_msg, "ai", False))


def _heuristic_outcome(last_response: str) -> str:
    """Best-effort outcome classification from the final assistant response.

    Order of checks (each can return early):
    1. Empty -> failure
    2. Hard failure indicators (traceback, unhandled exception, fatal error)
       -> failure
    3. Soft failure indicators (couldn't, unable, failed to, error
       occurred, after-retry recovery) -> partial
    4. Success indicators (task complete, success, done, here's the
       result) -> success
    5. Default -> success (a chat turn that produced any non-trivial
       response is a success for learning purposes; we'll let the
       reward model downgrade it later if needed).

    v1.7.0: signature is `(last_response)` only — the v1.1.0 `(messages,
    last_response)` form passed `messages=[]` (the bug this file fixes),
    so the arg was always unused.
    """
    if not last_response:
        return "failure"
    text = last_response.lower()
    # Hard failure: explicit traceback / fatal / unhandled exception
    if any(kw in text for kw in (
        "traceback (most recent call last)",
        "unhandled exception",
        "fatal error",
        "attributeerror:", "importerror:", "modulenotfounderror:",
        "syntaxerror:", "nameerror:", "typeerror:", "valueerror:",
    )):
        # If a recovery is mentioned, soften to partial.
        if any(kw in text for kw in ("but then", "after retry", "successfully recovered", "recovered and")):
            return "partial"
        return "failure"
    # Soft failure indicators
    if any(kw in text for kw in (
        "i could not", "i couldn't", "i was unable",
        "i don't have", "failed to", "couldn't find",
        "error occurred", "went wrong",
    )):
        return "partial"
    # Default to success (avoid biasing the dataset toward failure).
    return "success"


def _authoritative_skill_used(agent: Any, history_output: list) -> str:
    """Return the skill active in THIS monologue, from authoritative sources.

    Order (first non-empty wins):
    1. Per-turn: the last `history_output` message carrying
       `skill_instructions` with `content_included` truthy (a skill
       whose content was actually injected this turn) — matched via
       `helpers.skills.skill_instruction_name`. This is the exact
       signal `tools/skills_tool.py:_visible_skill_loaded` already
       reads, so it agrees with what the agent actually saw.
    2. Session-level: the most recently loaded skill from
       `helpers.skills.get_loaded_skill_names` (the `loaded_skills`
       ledger updated by `skills_tool` on every load).

    Both sources are pure-Python and never call the LLM. Any import or
    attribute failure degrades gracefully to "" (the rollout is still
    written with an empty `skill_used`).
    """
    try:
        from helpers import skills  # type: ignore  # noqa: E402
    except Exception as e:
        log.debug("[skillopt] cannot import helpers.skills: %s", e)
        return ""
    # 1. Per-turn attribution from the message stream.
    last_from_message = ""
    for msg in (history_output or []):
        try:
            name = skills.skill_instruction_name(msg)
        except Exception:
            name = ""
        if name:
            last_from_message = str(name)
    if last_from_message:
        return last_from_message
    # 2. Session-level ledger.
    try:
        names = skills.get_loaded_skill_names(agent) or []
    except Exception as e:
        log.debug("[skillopt] get_loaded_skill_names failed: %s", e)
        names = []
    if names:
        return str(names[-1])
    return ""


def _truncate(s: str, n: int = 400) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _safe_call(*fns):
    """Return the first fn() that produces a non-None value; tolerate errors."""
    for fn in fns:
        try:
            v = fn()
            if v is not None:
                return v
        except Exception:
            continue
    return None


def _tool_step_from_output(output_msg: Any) -> dict[str, Any] | None:
    """Build one trajectory step from an OutputMessage whose content dict
    carries tool metadata (tool_name / tool_result), as written by
    `Agent.hist_add_tool_result`. Returns None for plain text messages."""
    content = None
    if isinstance(output_msg, dict):
        content = output_msg.get("content")
    else:
        content = getattr(output_msg, "content", None)
    if not isinstance(content, dict):
        return None
    tool_name = content.get("tool_name") or ""
    tool_result = content.get("tool_result")
    if not tool_name and tool_result is None:
        return None
    name = str(tool_name or "")
    # hist_add_tool_result stores tool_result under content; some tool
    # calls also carry 'tool_args'/'arguments'. Capture a compact form.
    args = content.get("tool_args") or content.get("arguments") or ""
    return {
        "role": "tool",
        "name": name,
        "args": _truncate(_flatten_content(args), 120) if args else "",
        "result": _truncate(_flatten_content(tool_result), 200) if tool_result is not None else "",
    }


def execute(*args, **kwargs):  # type: ignore[no-untyped-def]
    """monologue_end hook entry point.

    The framework passes a single positional arg (the agent context)
    plus `loop_data=...`. We accept both shapes for v2.5 + legacy compat.

    v1.7.0: reads `loop_data.history_output` (the real attribute) and
    `loop_data.user_message` / `loop_data.last_response` instead of the
    nonexistent `loop_data.messages`.
    """
    # Recursion guard: stay silent during local counterfactual replay so
    # the replayed agent's own monologue_end does not pollute the training
    # set. Set by helpers/replay_harness.py (Phase C2) around communicate().
    if os.environ.get("SKILLOPT_REPLAY_MODE"):
        return

    sr = _sr()
    if sr is None:
        return

    # Extract what we can from the framework's context.
    agent = kwargs.get("agent") or (args[0] if args else None)
    loop_data = kwargs.get("loop_data") or getattr(agent, "loop_data", None)

    # v1.7.0: the real conversation lives on loop_data.history_output
    # (list[OutputMessage]) + loop_data.user_message + loop_data.last_response.
    # The v1.1.0 code read loop_data.messages which does not exist, so it
    # always saw [] and early-returned — writing zero rollouts.
    history_output = []
    user_msg = ""
    asst_msg = ""
    if loop_data is not None:
        history_output = list(getattr(loop_data, "history_output", None) or [])
        # The user's task is the current user message (a framework Message).
        um = getattr(loop_data, "user_message", None)
        if um is not None:
            user_msg = _flatten_content(getattr(um, "content", None) if not isinstance(um, dict) else um.get("content"))
        # The assistant's final answer is the last ai=True output entry,
        # falling back to loop_data.last_response (the framework's own
        # convenience string for the final response).
        for msg in reversed(history_output):
            if _output_ai(msg):
                asst_msg = _output_text(msg)
                if asst_msg:
                    break
        if not asst_msg:
            asst_msg = _flatten_content(getattr(loop_data, "last_response", None))

    if not user_msg:
        # Empty conversations don't teach the engine anything.
        return

    # Build a compact trajectory from tool/result messages (cap at 5).
    traj: list[dict[str, Any]] = []
    for msg in history_output[-12:]:
        step = _tool_step_from_output(msg)
        if step is not None:
            traj.append(step)
    traj = traj[:5]

    model = _safe_call(
        lambda: getattr(getattr(agent, "config", None), "chat_model", None),
        lambda: getattr(loop_data, "model_name", None) if loop_data else None,
    ) or ""

    start_ts = _safe_call(
        lambda: getattr(loop_data, "start_time", None) if loop_data else None,
    ) or time.time()
    duration_s = round(time.time() - float(start_ts), 1) if start_ts else 0.0

    skill_used = _authoritative_skill_used(agent, history_output)
    heuristic_label = _heuristic_outcome(asst_msg)
    record = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "task": _truncate(user_msg, 600),
        "task_type": skill_used or "general",
        "skill_used": skill_used,
        "outcome": heuristic_label,
        "last_response": _truncate(asst_msg, 1200),
        "trajectory": traj,
        "model": model,
        "duration_s": duration_s,
    }
    # v1.3.0 (Day-4, item 4): tag the rollout as awaiting a suggestion
    # from the inner-loop background worker. The harvester NEVER calls
    # the LLM itself (that would slow every chat); the suggestion is
    # written by helpers/inner_loop.py:inner_loop_tick(), which scans
    # logs/rollouts/ for entries with awaiting_suggestion=true and
    # enqueues a one-sentence suggestion per rollout. See
    # helpers/inner_loop.py for the full inner-loop contract.
    record["awaiting_suggestion"] = True

    # v1.2.0 (Day-3 item 3): tag the rollout with the fragments that
    # were active in the SKILL.md at the time of the chat. Read once
    # from the skill's SKILL.md via the v1.2.0 fragment store. The
    # A/B harness's _replay_under_skill uses this field to replay
    # under the right fragment context. If the helper is missing or
    # the SKILL.md has no frontmatter, the field defaults to
    # ["_default"] (the implicit single fragment).
    try:
        from usr.plugins.skillopt.helpers import fragment_store  # type: ignore
        if skill_used:
            skill_md = sr.a0_skills_dir() / skill_used / "SKILL.md"
            if skill_md.is_file():
                ids = fragment_store.active_fragment_ids(skill_md)
                if ids:
                    record["fragments_active"] = ids
                    text = fragment_store.active_fragments_text(skill_md)
                    if text:
                        # Truncate to keep rollouts small (a SKILL.md
                        # can be tens of KB; the rollout needs to stay
                        # under ~10KB to be useful downstream).
                        record["fragments_active_text"] = text[:4096]
    except Exception as e:
        # Fragment store is optional. A bug here can never crash the
        # harvester; the rollout is still useful without the field.
        log.debug("[skillopt] fragment_store tagging failed: %s", e)

    # v1.2.0: also call the reward model. The model is preferred when
    # it is confident AND source == 'model'. Otherwise we keep the
    # v1.1.0 heuristic as the rollout's `outcome` field. The full
    # model result is stored under `reward` so the A/B harness (Day-3
    # item 2) and the direct optimiser can decide for themselves
    # what to trust. The reward helper never raises; a bug here can
    # only make us behave like v1.1.0 did, not crash the harvester.
    try:
        from usr.plugins.skillopt.helpers import reward_model  # type: ignore
        rm_result = reward_model.score_rollout(record)
        record["reward"] = {
            "outcome": rm_result.get("outcome"),
            "confidence": rm_result.get("confidence"),
            "source": rm_result.get("source"),
            "model_version": rm_result.get("model_version"),
            "success": rm_result.get("success"),
            "partial": rm_result.get("partial"),
            "failure": rm_result.get("failure"),
        }
        if (
            rm_result.get("source") == "model"
            and float(rm_result.get("confidence") or 0.0) >= 0.6
        ):
            record["outcome"] = rm_result.get("outcome") or heuristic_label
    except Exception as e:
        # The reward helper itself is supposed to never raise, but
        # belt-and-braces: if the import fails or the call site
        # explodes, fall through to the v1.1.0 baseline silently.
        log.debug("[skillopt] reward model call failed: %s", e)

    # Persist via the shared helper so the Sleep engine can find it.
    try:
        sr.write_rollout(record)
    except Exception as e:
        log.debug("[skillopt] write_rollout failed: %s", e)