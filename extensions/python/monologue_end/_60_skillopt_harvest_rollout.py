"""
SkillOpt - rollout harvester.

v1.1.0 — the most important missing piece. The plugin was a no-op
because nothing ever wrote a real rollout to logs/rollouts/. This
hook runs on every monologue_end (i.e. after every chat turn) and
persists a small JSON record of the completed task into the
plugin's logs/rollouts/ directory, so the Sleep engine's `harvest`
verb has something to read.

What we record (the minimum SkillOpt needs to mine patterns):
- id            — uuid4 hex
- ts            — epoch seconds
- task          — the user's original message
- task_type     — best-effort skill hint (from the prompt + tools used)
- skill_used    — first skill explicitly named in the prompt, or empty
- outcome       — 'success' | 'partial' | 'failure' (heuristic)
- trajectory    — list of tool/response steps (truncated for size)
- model         — model name from the agent
- duration_s    — wall-clock seconds the monologue took

Idempotency: we never write a duplicate id; if the same chat ends
twice (retries), the second write is a no-op.
"""

from __future__ import annotations

import json
import logging
import re
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


def _heuristic_outcome(messages: list, last_response: str) -> str:
    """Best-effort outcome classification from the conversation.

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
    """
    if not last_response:
        return "failure"
    text = (last_response or "").lower()
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


def _extract_skill_hint(user_text: str) -> str:
    """If the user's prompt explicitly names a skill, return its name.

    Order of patterns (first match wins):
    1. Inline `skill: <name>` / `skill # <name>`.
    2. `the X skill` / `X skill`.
    3. Hyphenated token with 2+ hyphens (e.g. trading-veto-gate-system) —
       normal English words rarely have 2+ hyphens, so this is a strong
       signal for an Agent Zero skill name.
    """
    if not user_text:
        return ""
    # 1. Inline "skill: <name>" or "skill # <name>"
    m = re.search(r"\bskill\s*[:#]\s*[`\"]?([A-Za-z0-9_\-]+)[`\"]?", user_text, re.IGNORECASE)
    if m:
        return m.group(1)
    # 2. "the X skill" / "X skill"
    m = re.search(r"\bthe\s+([A-Za-z0-9_\-]+)\s+skill\b", user_text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Za-z0-9_\-]+)\s+skill\b", user_text, re.IGNORECASE)
    if m:
        return m.group(1)
    # 3. Hyphenated token with 2+ hyphens (very strong skill-name signal)
    m = re.search(r"\b([A-Za-z0-9]+(?:-[A-Za-z0-9]+){2,})\b", user_text)
    if m:
        return m.group(1)
    return ""


def _truncate(s: str, n: int = 400) -> str:
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[: n - 1] + "\u2026"


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


def execute(*args, **kwargs):  # type: ignore[no-untyped-def]
    """monologue_end hook entry point.

    The framework passes a single positional arg (the agent context)
    in most versions, and/or kwargs like `agent`, `loop_data`,
    `messages`. We accept both shapes for v2.5 + legacy compat.
    """
    sr = _sr()
    if sr is None:
        return

    # Extract what we can from the framework's context.
    agent = kwargs.get("agent") or (args[0] if args else None)
    loop_data = kwargs.get("loop_data") or getattr(agent, "loop_data", None)
    messages = kwargs.get("messages") or (getattr(loop_data, "messages", None) if loop_data else None) or []

    # Find the first user message (the task) and the last assistant message (the outcome).
    user_msg = ""
    asst_msg = ""
    for m in (messages or []):
        role = (m.get("role") if isinstance(m, dict) else getattr(m, "role", None)) or ""
        content = (m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) or ""
        if isinstance(content, list):
            content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
        if role == "user" and not user_msg:
            user_msg = content
        if role == "assistant":
            asst_msg = content

    if not user_msg:
        # Empty conversations don't teach the engine anything.
        return

    # Build a compact trajectory from tool calls (cap at 5 to keep rollouts small).
    traj: list[dict[str, Any]] = []
    for m in (messages or [])[-12:]:
        if not isinstance(m, dict):
            continue
        role = m.get("role") or ""
        if role in ("tool", "function"):
            traj.append({"role": role, "content": _truncate(m.get("content") or "", 200)})
        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            for tc in tool_calls[:3]:
                fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
                traj.append({"role": "tool_call", "name": fn.get("name", ""), "args": _truncate(fn.get("arguments", "") or "", 120)})
    traj = traj[:5]

    model = _safe_call(
        lambda: getattr(getattr(agent, "config", None), "chat_model", None),
        lambda: getattr(loop_data, "model_name", None) if loop_data else None,
    ) or ""

    start_ts = _safe_call(
        lambda: getattr(loop_data, "start_time", None) if loop_data else None,
    ) or time.time()
    duration_s = round(time.time() - float(start_ts), 1) if start_ts else 0.0

    heuristic_label = _heuristic_outcome(messages, asst_msg)
    record = {
        "id": uuid.uuid4().hex,
        "ts": time.time(),
        "task": _truncate(user_msg, 600),
        "task_type": _extract_skill_hint(user_msg) or "general",
        "skill_used": _extract_skill_hint(user_msg),
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
        skill_used = record.get("skill_used") or ""
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
