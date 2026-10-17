"""
SkillOpt - proposal summaries for informed human approval (v1.8.34).

WHY THIS MODULE EXISTS
----------------------
Before v1.8.34 the approval surfaces showed a *name* and nothing else:
`webui/config.html`'s Governance card rendered only the skill name plus
its last decision reason, and the Staged card's one human-facing field was
a placeholder (`api/staged.py` had `"diff_summary": f"{size} bytes"`, which
the template never rendered). A reviewer could not tell *what* would change
or *why*, so "approve" was a blind decision.

Two things were missing, not just UI:

1. No proposal<->decision linkage. `find_staged_proposals()` scans staging
   by filename stem; the approval ledger (`logs/runs/governance.log`) is
   per-SKILL with no `proposal_id`. Neither side could find the other.
2. No diff / summary / intent code anywhere in `helpers/` (`difflib`,
   `unified_diff`, `rationale`, `intent` had zero hits).

This module owns the *derived* half of that: it turns a staged proposal
plus the gate evidence around it into one bounded, display-ready document.

DESIGN CONTRACTS (do not break)
--------------------------------
1. **Never raises.** Every public function returns a structured dict. A
   summary is display-only; a bug here must never stall a cycle, a drain,
   or an HTTP route. This mirrors the loud-not-crash rule in governance.py.
2. **Never gates.** Nothing here is read by `sleep_runner.validate_proposal`
   or by any eligibility check. `intent` in particular is advisory text: a
   wrong or absent classification must not change whether a proposal is
   adoptable. If you find yourself calling this from a gate, stop.
3. **Always bounded.** The diff and the summary have hard ceilings
   (`proposal_summary.diff_max_lines` / `diff_max_chars` /
   `summary_max_chars`). Truncation sets an explicit flag; it is never
   silent, because a reviewer who thinks they saw the whole diff when they
   did not is exactly the informed-approval failure this module fixes.
4. **Sidecar, not staging column.** The summary is persisted as
   `<proposal>.md.summary.json` beside the proposal, following the
   established `.gate.json` / real-gate sidecar pattern. A new suffix means
   `sleep_runner._move_staged_proposal` MUST carry it - an orphaned sidecar
   left in staging top level is the same failure mode as the real-gate
   orphan deadlock documented at auto_loop.py (restart rehydrate).
5. **Backfill-safe.** Every staged proposal on disk today predates this
   module. `get_summary()` computes on read when the sidecar is absent and
   persists best-effort, so there is no migration step and no blocking
   startup work.

INJECTION / TESTABILITY
-----------------------
`build_summary`/`get_summary` accept an `llm_hook`. The default is the
heuristic path with `intent.source == "heuristic"`, which keeps the smoke
suite deterministic (no LLM, no network, no clock) exactly as the child DOX
requires. LLM refinement only runs when `proposal_summary.intent_llm_enabled`
is explicitly turned on AND a hook is supplied; any hook exception degrades
to the heuristic result with `source` downgraded, never to an error.
"""

from __future__ import annotations

import difflib
import importlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# v1.8.44 (R19): bound on concurrently-abandoned intent hooks. A daemon
# thread cannot be cancelled, so an over-running hook leaves a thread behind;
# this caps how many may accumulate before the LLM refinement is skipped
# outright. See `refine_intent_with_llm`.
_INTENT_SLOTS_MAX = 4
_INTENT_SLOTS_INFLIGHT = 0
_INTENT_SLOTS_LOCK = threading.Lock()


def _acquire_intent_slot() -> bool:
    """Reserve one intent-hook slot. False when the cap is reached."""
    global _INTENT_SLOTS_INFLIGHT
    with _INTENT_SLOTS_LOCK:
        if _INTENT_SLOTS_INFLIGHT >= _INTENT_SLOTS_MAX:
            return False
        _INTENT_SLOTS_INFLIGHT += 1
        return True


def _release_intent_slot() -> None:
    global _INTENT_SLOTS_INFLIGHT
    with _INTENT_SLOTS_LOCK:
        if _INTENT_SLOTS_INFLIGHT > 0:
            _INTENT_SLOTS_INFLIGHT -= 1


def _intent_prompt(intent: dict[str, Any], diff: dict[str, Any],
                   evidence: dict[str, Any]) -> str:
    """The classification prompt. Extracted so `refine_intent_with_llm` can
    hand it to a worker thread without building it on the caller's path."""
    added = _added_text(diff)[:4000]
    return (
        "Classify the intent of this skill-document edit. Reply with ONLY "
        "a JSON object {\"category\": ..., \"headline\": ...} where category "
        f"is one of {list(INTENT_CATEGORIES)} and headline is under 90 chars.\n\n"
        f"Current heuristic guess: {intent.get('category')} - {intent.get('headline')}\n"
        f"Gate evidence: {json.dumps(evidence, default=str)[:800]}\n\n"
        f"Added lines (truncated):\n{added}"
    )


SUMMARY_SCHEMA = "skillopt.proposal_summary/1"
SUMMARY_SUFFIX = ".summary.json"
GENERATOR = "proposal_view"
GENERATOR_VERSION = "1.8.34"

# Intent vocabulary. Deliberately small and closed: the badge in the UI is
# driven off these exact strings, and an open vocabulary would make the UI
# grow an unbounded class list.
INTENT_CATEGORIES = (
    "bug_fix",
    "performance",
    "new_feature",
    "refactor",
    "docs",
    "unknown",
)

# Hard ceilings. Config can lower these but never raise them past the hard
# cap - these exist so a config typo cannot produce a 10 MB HTTP response.
HARD_SUMMARY_MAX_CHARS = 4000
HARD_DIFF_MAX_LINES = 5000
HARD_DIFF_MAX_CHARS = 200_000

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "persist": True,
    "summary_max_chars": 600,
    "diff_max_lines": 400,
    "diff_max_chars": 20000,
    "intent_llm_enabled": False,
    "intent_llm_timeout_s": 20,
}


# ----------------------------------------------------------------------- #
# Config
# ----------------------------------------------------------------------- #

def _config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective `proposal_summary` block, clamped to the hard ceilings.

    With no argument, reads the merged config the same way the rest of the
    plugin does. With an argument, treats it as an override of the merged
    section (used by callers and tests).

    Clamping runs on BOTH paths and never raises: a missing/!dict section,
    a non-numeric bound, or a caller-supplied garbage value must degrade to
    a usable configuration, never to a disabled subsystem. That is the
    v1.8.19 class of bug where a wiped config.json silently turned a feature
    off.
    """
    out = dict(DEFAULTS)
    if cfg is None:
        try:
            from usr.plugins.skillopt.helpers import sleep_runner  # type: ignore
            merged = sleep_runner.merged_config()
        except Exception:
            try:
                from helpers import sleep_runner  # type: ignore
                merged = sleep_runner.merged_config()
            except Exception:
                merged = None
        try:
            section = (merged or {}).get("proposal_summary") or {}
        except Exception:
            section = {}
    else:
        section = cfg
    if not isinstance(section, dict):
        return out
    for key in DEFAULTS:
        if key in section:
            out[key] = section[key]
    for key in ("enabled", "persist", "intent_llm_enabled"):
        try:
            out[key] = bool(out.get(key))
        except Exception:
            out[key] = bool(DEFAULTS[key])
    for key, hard in (
        ("summary_max_chars", HARD_SUMMARY_MAX_CHARS),
        ("diff_max_lines", HARD_DIFF_MAX_LINES),
        ("diff_max_chars", HARD_DIFF_MAX_CHARS),
        ("intent_llm_timeout_s", 300),
    ):
        try:
            val = int(out.get(key))
        except (TypeError, ValueError):
            val = int(DEFAULTS[key])
        out[key] = max(1, min(val, hard))
    return out


# ----------------------------------------------------------------------- #
# Sidecar path / IO
# ----------------------------------------------------------------------- #

def summary_path(staged_path: str | os.PathLike) -> Path:
    """`<staging>/<skill>.md` -> `<staging>/<skill>.md.summary.json`.

    Uses `with_suffix(suffix + ...)`, matching sleep_runner's own marker
    convention, so the name is never mistaken for a staged proposal by
    find_staged_proposals() (which filters on .md/.proposed).
    """
    p = Path(staged_path)
    return p.with_suffix(p.suffix + SUMMARY_SUFFIX)


def read_summary(staged_path: str | os.PathLike) -> dict[str, Any] | None:
    """Return the persisted summary, or None when absent/unreadable.

    A corrupt sidecar is treated as ABSENT (the caller recomputes), matching
    the real-gate sidecar's never-deadlock rule rather than raising.
    """
    p = summary_path(staged_path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or data.get("schema") != SUMMARY_SCHEMA:
        return None
    return data


def write_summary(staged_path: str | os.PathLike, doc: dict[str, Any]) -> Path | None:
    """Atomically persist a summary sidecar. Best-effort: returns None on any
    failure and never raises. Uses write-temp + os.replace, the same atomic
    pattern as the gate marker and the cycle history."""
    try:
        p = summary_path(staged_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, p)
        return p
    except Exception:
        return None


# ----------------------------------------------------------------------- #
# Diff
# ----------------------------------------------------------------------- #

def _content_lines(raw: list[str]) -> list[str]:
    """The +/- body lines, excluding the `---`/`+++` file headers.

    Without this, `+++ b/staged` counts as an ADDED line. That is not a
    cosmetic error: it makes a pure deletion look like an edit, which
    pushed classify_intent() past its remove-only branch and mislabelled a
    pure condensation as `unknown`.
    """
    out = []
    for ln in raw:
        if ln.startswith("@@"):
            continue
        if ln.startswith("--- ") or ln.startswith("+++ "):
            continue
        out.append(ln)
    return out


def compute_diff(
    current: str, proposed: str, *, max_lines: int = 400, max_chars: int = 20000
) -> dict[str, Any]:
    """Unified diff between the live SKILL.md and the staged proposal.

    Returns `{available, truncated, added_lines, removed_lines, hunk_count,
    text}`. Bounded by BOTH max_lines and max_chars; whichever binds first
    sets `truncated` and appends a visible marker to the text. The marker is
    reserved out of max_chars, so the returned text never exceeds the cap -
    a reviewer must be able to trust that the number is the number.
    """
    out: dict[str, Any] = {
        "available": False,
        "truncated": False,
        "added_lines": 0,
        "removed_lines": 0,
        "hunk_count": 0,
        "text": "",
    }
    try:
        max_lines = max(1, int(max_lines))
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_lines, max_chars = 400, 20000
    try:
        cur = (current or "").splitlines()
        prop = (proposed or "").splitlines()
        raw = list(
            difflib.unified_diff(
                cur,
                prop,
                fromfile="a/SKILL.md",
                tofile="b/staged",
                lineterm="",
                n=3,
            )
        )
    except Exception:
        return out
    if not raw:
        # Byte-identical or whitespace-only difference: an EMPTY diff is a
        # fact the reviewer needs, not an error, but `available=False` makes
        # the UI say "no textual difference" rather than render an empty box.
        return out
    out["available"] = True
    body = _content_lines(raw)
    out["added_lines"] = sum(1 for ln in body if ln.startswith("+"))
    out["removed_lines"] = sum(1 for ln in body if ln.startswith("-"))
    out["hunk_count"] = sum(1 for ln in raw if ln.startswith("@@"))

    marker = "\n... [diff truncated - view the full proposal file for the remainder]"
    truncated = False
    if len(raw) > max_lines:
        raw = raw[:max_lines]
        truncated = True
    text = "\n".join(raw)
    # Reserve the marker's own length so the total stays inside the cap.
    if len(text) + len(marker) > max_chars:
        text = text[: max(0, max_chars - len(marker))]
        truncated = True
    if truncated:
        text += marker
    out["truncated"] = truncated
    out["text"] = text
    return out


# ----------------------------------------------------------------------- #
# Intent classification (heuristic-first)
# ----------------------------------------------------------------------- #

# Signals are deliberately conservative and ordered: the first bucket that
# matches wins, and "unknown" is a legitimate, expected outcome. A wrong
# guess costs a reviewer ten seconds; a missing signal costs nothing.
_BUG_FIX_SIGNALS = (
    "traceback", "exception", "error handling", "error-handling", "fallback",
    "try:", "except", "validate", "guard", "fail loudly", "fail-closed",
    "fail closed", "silent failure", "silently", "edge case", "timeout",
    "retry", "null", "missing", "crash", "bug", "fix",
)
_PERF_SIGNALS = (
    "faster", "performance", "optimi", "reduce latency", "cache",
    "caching", "batched", "parallel", "efficien", "throughput", "token",
    "concise", "shorter", "compact", "redundant", "duplicate",
)
_NEW_FEATURE_SIGNALS = (
    "new section", "add a section", "## ", "example", "when to use",
    "new step", "add a step", "covers", "handles the case",
)
_DOCS_SIGNALS = ("wording", "clarif", "typo", "comment", "explanation", "describe")


def _added_text(diff: dict[str, Any]) -> str:
    """The added (+) lines of a rendered diff, lowercase-joined.

    The `+++ b/staged` header is excluded via the same `_content_lines`
    rule the counters use, so the classifier does not read a filename as
    new guidance."""
    try:
        lines = (diff.get("text") or "").splitlines()
    except Exception:
        return ""
    return "\n".join(
        ln[1:] for ln in _content_lines(lines) if ln.startswith("+")
    ).lower()


def classify_intent(
    diff: dict[str, Any], evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Deterministic intent classification. Never raises.

    Returns `{category, confidence, source, headline}`. `source` is
    "heuristic" here; the LLM refiner may upgrade it to "llm". An
    unavailable diff degrades to `unknown` with a warning rather than a
    guess.
    """
    evidence = evidence or {}
    try:
        added = _added_text(diff)
        added_n = int(diff.get("added_lines") or 0)
        removed_n = int(diff.get("removed_lines") or 0)
        if not added and added_n == 0:
            # Removed-only edit: a shrink, which is a distinct signal from
            # "no change" even though both have no added text.
            if removed_n == 0:
                return {
                    "category": "unknown",
                    "confidence": 0.0,
                    "source": "heuristic",
                    "headline": "No textual difference detected",
                }
            return {
                "category": "performance",
                "confidence": 0.4,
                "source": "heuristic",
                "headline": f"Condenses the document ({removed_n} lines removed, none added)",
            }

        def _hits(signals: tuple[str, ...]) -> int:
            return sum(1 for s in signals if s in added)

        bug = _hits(_BUG_FIX_SIGNALS)
        perf = _hits(_PERF_SIGNALS)
        feat = _hits(_NEW_FEATURE_SIGNALS)
        docs = _hits(_DOCS_SIGNALS)

        # Failure-memory evidence is a strong bug-fix signal: the engine
        # proposed this rewrite BECAUSE those failures were recorded.
        fm_hits = evidence.get("failure_memory_hits") or []
        if fm_hits:
            bug += 2

        scores = {"bug_fix": bug, "performance": perf,
                  "new_feature": feat, "docs": docs}
        best = max(scores, key=lambda k: scores[k])
        total = sum(scores.values()) or 1
        if scores[best] == 0:
            return {
                "category": "unknown",
                "confidence": 0.2,
                "source": "heuristic",
                "headline": (
                    f"Rewrites the document ({added_n} added / {removed_n} removed "
                    "lines) for an unclassified reason"
                ),
            }
        confidence = min(0.9, 0.4 + 0.5 * (scores[best] / total))
        headlines = {
            "bug_fix": "Addresses recorded failures / edge cases",
            "performance": "Optimizes or condenses the document",
            "new_feature": "Adds new guidance or coverage",
            "docs": "Clarifies or rewords existing guidance",
        }
        return {
            "category": best,
            "confidence": round(confidence, 2),
            "source": "heuristic",
            "headline": headlines[best],
        }
    except Exception:
        return {
            "category": "unknown",
            "confidence": 0.0,
            "source": "heuristic",
            "headline": "Intent could not be determined",
        }


def refine_intent_with_llm(
    intent: dict[str, Any],
    diff: dict[str, Any],
    evidence: dict[str, Any],
    *,
    hook: Callable[[str], str],
    timeout_s: int = 20,
) -> dict[str, Any]:
    """Optional LLM upgrade of the intent block. Degrades to `intent` verbatim.

    v1.8.44 (re-audit R19): `timeout_s` is now ACTUALLY USED. It was a
    documented parameter that nothing read, and `hook(prompt)` is a
    synchronous call into an HTTP LLM helper - so a hanging hook hung the
    staging write path indefinitely, with the docstring promising a timeout
    that did not exist.

    The hook is run on a worker thread with a hard deadline. A daemon thread
    cannot be cancelled, so an over-running hook is abandoned rather than
    joined - the result is simply not used, and the heuristic stands. Threads
    are bounded by a module-level cap so a pathological hook cannot spawn an
    unbounded number of them across repeated calls.

    Any failure - timeout, hook exception, malformed reply - returns the
    heuristic result unchanged. An LLM outage must never lose the summary the
    reviewer needs, and must never raise into a staging write.
    """
    try:
        if not callable(hook):
            return intent
        added = _acquire_intent_slot()
        if not added:
            # Too many abandoned hooks already in flight; do not add another.
            return intent
        holder: dict[str, Any] = {}
        t = threading.Thread(
            target=lambda: holder.update(
                {"raw": hook(_intent_prompt(intent, diff, evidence))}),
            daemon=True, name="skillopt-intent-llm")
        t.start()
        t.join(max(0.5, float(timeout_s or 0) or 20.0))
        if t.is_alive():
            _release_intent_slot()
            return intent          # abandoned: keep the heuristic
        _release_intent_slot()
        raw = holder.get("raw")
        if not raw or not isinstance(raw, str):
            return intent
        # Tolerate a fenced reply: models add ```json ... ``` unprompted.
        cleaned = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw.strip())
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            return intent
        category = str(data.get("category") or "").strip()
        if category not in INTENT_CATEGORIES:
            return intent
        out = dict(intent)
        out["category"] = category
        out["confidence"] = 0.75
        out["source"] = "llm"
        headline = str(data.get("headline") or "").strip()
        if headline:
            out["headline"] = headline[:120]
        return out
    except Exception:
        return intent


# ----------------------------------------------------------------------- #
# Evidence assembly
# ----------------------------------------------------------------------- #

def _import(mod: str):
    """Two-path import, matching the plugin-wide convention: the namespace
    path wins in the framework runtime, the bare path is the standalone
    fallback (and is the identity the smoke suite canonicalises onto)."""
    try:
        return importlib.import_module(f"usr.plugins.skillopt.helpers.{mod}")
    except Exception:
        try:
            return importlib.import_module(f"helpers.{mod}")
        except Exception:
            return None


def _latest_cycle_entry(skill: str) -> dict[str, Any]:
    """Most recent cycle_history entry for this skill. Read-only, never raises."""
    try:
        cycle_history = _import("cycle_history")
        if cycle_history is None:
            return {}
        entries = cycle_history.read_cycle_history(limit=200)
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("skill") == skill:
                return entry
        return {}
    except Exception:
        return {}


def _failure_memory_hits(skill: str, limit: int = 5) -> list[str]:
    """Short failure reasons recorded for this skill (bug-fix signal)."""
    try:
        fm = _import("failure_memory")
        if fm is None:
            return []
        rows = fm.load_failures(skill, limit=limit)
        out: list[str] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            reason = str(row.get("failure_reason") or "").strip()
            if reason:
                out.append(reason[:120])
        return out[:limit]
    except Exception:
        return []


def _ab_evidence() -> dict[str, Any]:
    try:
        ab = _import("ab_harness")
        if ab is None:
            return {}
        status = ab.get_ab_status() or {}
        last = status.get("last_result") or {}
        if not isinstance(last, dict) or not last:
            return {}
        return {
            "lift_pp": last.get("lift_pp"),
            "confidence": last.get("confidence"),
            "passed": last.get("passed"),
        }
    except Exception:
        return {}


def collect_evidence(
    staged_path: Path, skill: str, marker: dict[str, Any] | None,
    sidecar: dict[str, Any] | None,
) -> dict[str, Any]:
    """Gate + rollout evidence around a staged proposal. Never raises."""
    evidence: dict[str, Any] = {
        "rollouts_analyzed": 0,
        "rollouts_recent": 0,
        "n_held_out": None,
        "lift_pp": None,
        "gate_outcome": "absent",
        "official_gated": bool(marker.get("official_gated")) if isinstance(marker, dict) else False,
        "real_gate": None,
        "ab_harness": {},
        "failure_memory_hits": [],
        "gate_reason": "",
    }
    try:
        cyc = _latest_cycle_entry(skill)
        if cyc:
            evidence["n_held_out"] = cyc.get("n_held_out")
            evidence["lift_pp"] = cyc.get("lift_pp")
            evidence["gate_reason"] = str(cyc.get("outcome_detail") or "")
            outcome = str(cyc.get("outcome") or "")
            if outcome in ("adopted", "accepted", "staged"):
                evidence["gate_outcome"] = "accept"
            elif outcome in ("rejected", "quarantined"):
                evidence["gate_outcome"] = "reject"
        if isinstance(marker, dict):
            gate = marker.get("gate") or {}
            if isinstance(gate, dict) and type(gate.get("accepted")) is bool:
                evidence["gate_outcome"] = (
                    "accept" if gate.get("accepted") else "reject"
                )
            if evidence["lift_pp"] is None and isinstance(gate, dict):
                evidence["lift_pp"] = gate.get("delta_pp")
        if isinstance(sidecar, dict):
            # v1.8.48 (audit T4): the verdict is NESTED. The only writer
            # (`scripts/replay_gate_worker.py`) sets
            # `payload['status'] = 'done'` and `payload['verdict'] = verdict`,
            # and the real gate's `accepted` / pair count live inside that
            # verdict. Reading them off the top level returned None forever,
            # so both approval surfaces rendered a `done` verdict
            # identically whether the replay ACCEPTED or REJECTED the
            # proposal - the precise failure this module exists to prevent,
            # and invisible to any test that only checked the fields exist.
            _rv = sidecar.get("verdict")
            _rv = _rv if isinstance(_rv, dict) else {}
            evidence["real_gate"] = {
                "status": sidecar.get("status"),
                "accepted": _rv.get("accepted"),
                "n_pairs": _rv.get("n"),
            }
        evidence["ab_harness"] = _ab_evidence()
        evidence["failure_memory_hits"] = _failure_memory_hits(skill)
        # Rollout counts come from the cycle entry when present; they are
        # descriptive only and never affect a gate.
        try:
            links = cyc.get("links") or {}
            if isinstance(links, dict):
                evidence["rollouts_analyzed"] = int(links.get("rollout_count") or 0)
                evidence["rollouts_recent"] = int(links.get("new_rollouts") or 0)
        except Exception:
            pass
    except Exception:
        pass
    return evidence


# ----------------------------------------------------------------------- #
# Summary / rationale text
# ----------------------------------------------------------------------- #

def _derive_summary(
    skill: str, diff: dict[str, Any], evidence: dict[str, Any],
    intent: dict[str, Any], max_chars: int,
) -> str:
    """Deterministic one-paragraph summary. Always returns non-empty text.

    This is the sentence a reviewer reads first. It is built from hard
    numbers (lines changed, gate outcome, held-out n) rather than model
    prose, so it is correct even when every optional subsystem is off.
    """
    try:
        added = int(diff.get("added_lines") or 0)
        removed = int(diff.get("removed_lines") or 0)
        parts: list[str] = []
        parts.append(
            f"Rewrites the live SKILL.md for '{skill}': +{added} / -{removed} "
            f"line(s) across {int(diff.get('hunk_count') or 0)} changed section(s)."
        )
        gate_outcome = str(evidence.get("gate_outcome") or "absent")
        lift = evidence.get("lift_pp")
        n_out = evidence.get("n_held_out")
        if gate_outcome == "accept" and lift is not None:
            parts.append(
                f"The validation gate accepted it at +{round(float(lift), 1)}pp"
                + (f" over {n_out} held-out task(s)." if n_out else ".")
            )
        elif gate_outcome == "reject":
            parts.append("Note: the recorded gate verdict for this proposal was REJECT.")
        else:
            parts.append("No completed gate verdict is recorded for this proposal.")
        rg = evidence.get("real_gate") or {}
        if isinstance(rg, dict) and rg.get("status"):
            parts.append(
                f"Independent replay confirmation: {rg.get('status')}"
                + (" (accepted)." if rg.get("accepted") else ".")
            )
        headline = str(intent.get("headline") or "").strip()
        if headline and intent.get("category") != "unknown":
            parts.append(f"Assessed intent: {intent['category']} - {headline}.")
        text = " ".join(parts)
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "\u2026"
        return text or f"Proposal for '{skill}' is staged and awaiting a decision."
    except Exception:
        return f"Proposal for '{skill}' is staged and awaiting a decision."


def _derive_rationale(
    diff: dict[str, Any], evidence: dict[str, Any], intent: dict[str, Any]
) -> list[str]:
    """Evidence-anchored bullets. Every bullet must be checkable."""
    out: list[str] = []
    try:
        hits = evidence.get("failure_memory_hits") or []
        for reason in hits[:3]:
            out.append(f"Trigger: recorded failure - {reason}")
        if not hits:
            recent = evidence.get("rollouts_recent")
            if recent:
                out.append(f"Trigger: {recent} new rollout(s) since the last cycle.")
        added = int(diff.get("added_lines") or 0)
        removed = int(diff.get("removed_lines") or 0)
        out.append(
            f"Change: +{added} / -{removed} line(s) in "
            f"{int(diff.get('hunk_count') or 0)} section(s)."
        )
        lift = evidence.get("lift_pp")
        if lift is not None:
            out.append(
                f"Expected effect: measured at +{round(float(lift), 1)}pp on the "
                f"held-out set ({evidence.get('n_held_out') or 'n'} task(s))."
            )
        gate_reason = str(evidence.get("gate_reason") or "").strip()
        if gate_reason:
            out.append(f"Gate note: {gate_reason[:200]}")
        if not added and removed:
            out.append(
                "Expected effect: a pure condensation - no new guidance is added, "
                "so nothing new can be wrong."
            )
        if intent.get("category") == "unknown":
            out.append(
                "Intent was not classifiable from the diff; review the change itself."
            )
        return out[:6]
    except Exception:
        return []


# ----------------------------------------------------------------------- #
# Public entry points
# ----------------------------------------------------------------------- #

def build_summary(
    staged_path: str | os.PathLike,
    *,
    cfg: dict[str, Any] | None = None,
    skill: str | None = None,
    persist: bool | None = None,
    llm_hook: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Build a ProposalSummary for a staged proposal. Never raises.

    Returns the summary document. On any internal failure the caller still
    gets a well-formed document with `available: False` and a warning, so a
    dashboard render can never explode on malformed input.
    """
    conf = _config(cfg)
    src = Path(staged_path)
    pid = src.stem if src.suffix in (".md", ".proposed") else src.name
    skill = skill or pid
    warnings: list[str] = []
    doc: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "available": False,
        "proposal_id": pid,
        "skill": skill,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "mode_at_generation": _mode_snapshot(),
        "intent": {
            "category": "unknown",
            "confidence": 0.0,
            "source": "absent",
            "headline": "",
        },
        "summary": "",
        "rationale": [],
        "evidence": {},
        "diff": {"available": False, "truncated": False, "added_lines": 0,
                 "removed_lines": 0, "hunk_count": 0, "text": ""},
        "artifacts": {},
        "warnings": warnings,
    }
    try:
        if not src.is_file():
            warnings.append("proposal file is missing")
            doc["summary"] = _derive_summary(skill, doc["diff"], {}, doc["intent"],
                                             int(conf["summary_max_chars"]))
            return doc

        try:
            proposed = src.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            warnings.append(f"proposal unreadable: {type(e).__name__}")
            proposed = ""

        current = ""
        target: Path | None = None
        try:
            sleep_runner = _import("sleep_runner")
            if sleep_runner is not None:
                target = sleep_runner.safe_skill_md(skill)
                if target.is_file():
                    current = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            target = None
        if not current:
            warnings.append("no live SKILL.md found for this skill (first-write proposal)")

        diff = compute_diff(
            current, proposed,
            max_lines=int(conf["diff_max_lines"]),
            max_chars=int(conf["diff_max_chars"]),
        )
        doc["diff"] = diff

        marker = sidecar = None
        try:
            sleep_runner = _import("sleep_runner")
            if sleep_runner is not None:
                marker = sleep_runner.read_official_gate_marker(src)
                sidecar = sleep_runner.read_real_gate_sidecar(src)
                if marker is None:
                    warnings.append(
                        "official gate marker absent - gate provenance unknown"
                    )
        except Exception:
            pass

        evidence = collect_evidence(src, skill, marker, sidecar)
        doc["evidence"] = evidence

        intent = classify_intent(diff, evidence)
        if bool(conf.get("intent_llm_enabled")) and llm_hook is not None:
            intent = refine_intent_with_llm(
                intent, diff, evidence,
                hook=llm_hook,
                timeout_s=int(conf["intent_llm_timeout_s"]),
            )
        doc["intent"] = intent

        doc["summary"] = _derive_summary(
            skill, diff, evidence, intent, int(conf["summary_max_chars"])
        )
        doc["rationale"] = _derive_rationale(diff, evidence, intent)

        doc["artifacts"] = {
            "proposal_path": str(src),
            "proposal_rel": _rel(src),
            "summary_rel": _rel(summary_path(src)),
            "target_path": str(target) if target else "",
            "official_staging_dir": str(
                (marker or {}).get("official_staging_dir") or ""
            ),
        }
        doc["available"] = True
    except Exception as e:
        # Never propagate. A summary is cosmetic; a raise here would take
        # down a drain or an HTTP route.
        warnings.append(f"summary degraded: {type(e).__name__}: {e}")
        try:
            if not doc["summary"]:
                doc["summary"] = _derive_summary(
                    skill, doc["diff"], doc.get("evidence") or {},
                    doc["intent"], int(conf["summary_max_chars"]),
                )
        except Exception:
            doc["summary"] = f"Proposal for '{skill}' is staged and awaiting a decision."

    # Persist last, and only when the caller did not override it. The
    # sidecar is an optimisation + audit artifact, never a precondition for
    # the in-memory doc the caller is about to render.
    should_persist = bool(conf.get("persist", True)) if persist is None else bool(persist)
    if should_persist:
        write_summary(src, doc)
    return doc


def get_summary(
    staged_path: str | os.PathLike,
    *,
    cfg: dict[str, Any] | None = None,
    skill: str | None = None,
    allow_build: bool = True,
) -> dict[str, Any]:
    """Read the persisted summary, or compute+persist it when absent.

    This is the backfill entry point: every staged proposal on disk today
    predates the sidecar, and a reviewer must still get a real summary
    without a migration step. `allow_build=False` returns the empty
    document instead, for callers that must stay read-only.
    """
    conf = _config(cfg)
    if not conf.get("enabled", True):
        return _empty_summary(staged_path, skill, ["proposal summaries are disabled"])
    try:
        existing = read_summary(staged_path)
    except Exception:
        existing = None
    if existing is not None:
        return existing
    if not allow_build:
        return _empty_summary(staged_path, skill, ["no summary sidecar yet"])
    try:
        return build_summary(staged_path, cfg=conf, skill=skill)
    except Exception:
        return _empty_summary(staged_path, skill, ["summary build failed"])


def _empty_summary(
    staged_path: str | os.PathLike, skill: str | None, warnings: list[str]
) -> dict[str, Any]:
    src = Path(staged_path)
    pid = src.stem if src.suffix in (".md", ".proposed") else src.name
    name = skill or pid
    return {
        "schema": SUMMARY_SCHEMA,
        "available": False,
        "proposal_id": pid,
        "skill": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "mode_at_generation": _mode_snapshot(),
        "intent": {"category": "unknown", "confidence": 0.0,
                   "source": "absent", "headline": ""},
        "summary": f"Proposal for '{name}' is staged and awaiting a decision.",
        "rationale": [],
        "evidence": {},
        "diff": {"available": False, "truncated": False, "added_lines": 0,
                 "removed_lines": 0, "hunk_count": 0, "text": ""},
        "artifacts": {"proposal_path": str(src), "proposal_rel": _rel(src),
                      "summary_rel": _rel(summary_path(src)), "target_path": "",
                      "official_staging_dir": ""},
        "warnings": list(warnings),
    }


def _rel(p: Path) -> str:
    """Plugin-root-relative path for display, so the UI never has to render
    a container-specific absolute path as the primary affordance."""
    try:
        return str(p.relative_to(Path(__file__).resolve().parent.parent))
    except Exception:
        return str(p)


def _mode_snapshot() -> str:
    """Current resolved evolution mode, recorded for the audit trail.

    Defensive on purpose: the resolver arrives in v1.8.34 P3, and a summary
    must still build before then (and after a rollback).
    """
    try:
        governance = _import("governance")
        if governance is not None and hasattr(governance, "resolve_evolution_mode"):
            return str(governance.resolve_evolution_mode().get("mode") or "manual")
    except Exception:
        pass
    return "manual"


def annotate_staged(
    staged_path: str | os.PathLike, **kw: Any
) -> dict[str, Any] | None:
    """Best-effort wrapper for the two staging write points.

    Used by official_adapter (after `write_official_gate_marker`) and
    direct_optimizer (after `write_text`) so neither path has to know about
    the summary schema. A failure here must never fail the staging write
    that just succeeded - the proposal is already safely on disk.
    """
    try:
        conf = _config()
        if not conf.get("enabled", True):
            return None
        doc = build_summary(staged_path, cfg=conf, **kw)
        return doc
    except Exception:
        return None


__all__ = [
    "SUMMARY_SCHEMA",
    "SUMMARY_SUFFIX",
    "INTENT_CATEGORIES",
    "DEFAULTS",
    "summary_path",
    "read_summary",
    "write_summary",
    "compute_diff",
    "classify_intent",
    "refine_intent_with_llm",
    "collect_evidence",
    "build_summary",
    "get_summary",
    "annotate_staged",
]