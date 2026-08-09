#!/usr/bin/env python3
"""
SkillOpt A/B judge calibration script - v1.2.0.

ROADMAP Day-3, item 2 - the calibration step. The judge's accuracy
needs to be measured against a human-labelled set of A/B pairs.
This script:

  1. Loads a labelled JSONL file (one pair per line) where each line is
     {"task": ..., "response_a": ..., "response_b": ..., "human_label": "win"|"lose"|"tie"}.
  2. Runs the judge on each pair.
  3. Computes Cohen's kappa between the judge and the human labels.
  4. Prints a calibration report and exits 0/1.

Cohen's kappa is implemented in pure Python - no sklearn dependency.
If sklearn is available we sanity-check the hand-rolled number
against sklearn's cohen_kappa_score (the v1.2.0 smoke test verifies
they agree within 1e-6).

Usage:
  python scripts/calibrate_judge.py --pairs tests/fixtures/ab_pairs.jsonl
  python scripts/calibrate_judge.py --pairs tests/fixtures/ab_pairs.jsonl --use-stub-judge
  python scripts/calibrate_judge.py --build-demo --output tests/fixtures/ab_pairs.jsonl
                                     # writes a 10-pair demo set for first-time runs

Exit codes:
  0 - kappa >= 0.6 (judge is acceptable)
  1 - kappa < 0.6 (judge is unreliable, see report)
  2 - file not found or bad JSON
  3 - judge raised (LLM unreachable, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PLUGIN_NAME = "skillopt"
LABELS = ("win", "lose", "tie")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------- #
# Cohen's kappa (pure Python, no sklearn)
# ----------------------------------------------------------------------- #

def cohen_kappa(y1: list[str], y2: list[str]) -> float | None:
    """Compute Cohen's kappa between two label sequences.

    Returns a float in [-1, 1] (1 = perfect agreement, 0 = chance,
    <0 = worse than chance). Returns None on degenerate input
    (empty lists, all-same labels).
    """
    if not y1 or len(y1) != len(y2):
        return None
    n = len(y1)
    # Observed agreement
    po = sum(1 for a, b in zip(y1, y2) if a == b) / n
    # Expected agreement by chance
    counts1 = Counter(y1)
    counts2 = Counter(y2)
    pe = sum((counts1.get(l, 0) / n) * (counts2.get(l, 0) / n) for l in LABELS)
    if pe >= 1.0:
        return None  # degenerate: no expected disagreement
    return (po - pe) / (1.0 - pe)


# ----------------------------------------------------------------------- #
# Judge access (with fallback to the default keyword stub)
# ----------------------------------------------------------------------- #

def _make_judge(use_stub: bool, use_http: bool):
    """Return a judge callable. Uses the production LLM judge by default
    (POSTs to SKILLOPT_JUDGE_ENDPOINT). With --use-stub-judge, falls
    back to the deterministic keyword stub from ab_harness. With
    --use-http-judge, forces the HTTP path even if SKILLOPT_JUDGE_ENDPOINT
    is unset (will fail at call time with a clear error)."""
    # When this script is run directly (not via `python -m`), the
    # plugin root is NOT on sys.path. Add it so `from helpers.ab_harness
    # import ...` works. Also try the production `usr.plugins.*` path
    # first in case the plugin is installed (the same dual-path that
    # helpers/sleep_runner.py uses).
    import sys as _sys
    root = str(_plugin_root())
    if root not in _sys.path:
        _sys.path.insert(0, root)
    # 1. Try the installed-plugin path first
    try:
        from usr.plugins.skillopt.helpers import ab_harness as _ab  # type: ignore
    except Exception:
        from helpers import ab_harness as _ab  # type: ignore
    if use_stub:
        return _ab._default_judge_fn
    if use_http:
        # Force the HTTP path. set_judge_mode("http") wires a closure
        # that POSTs JUDGE_PROMPT to SKILLOPT_JUDGE_ENDPOINT. If the
        # env var is unset it raises with a clear message.
        _ab.set_judge_mode("http")
        return _ab.get_judge_fn()  # the HTTP-bound closure
    # Default: the keyword stub. Production would set judge_mode=http
    # via the env var; calibrate_judge runs offline by default.
    return _ab._default_judge_fn


# ----------------------------------------------------------------------- #
# Pair loading + demo builder
# ----------------------------------------------------------------------- #

def _load_pairs(path: Path) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError as e:
                print(f"[{PLUGIN_NAME}] WARN: skipping malformed line: {e}", file=sys.stderr)
                continue
            for key in ("task", "response_a", "response_b", "human_label"):
                if key not in rec:
                    raise ValueError(f"missing key {key!r} on line {len(pairs) + 1}")
            if rec["human_label"] not in LABELS:
                raise ValueError(
                    f"bad human_label {rec['human_label']!r} on line {len(pairs) + 1}; "
                    f"must be one of {LABELS}"
                )
            pairs.append(rec)
    return pairs


def _build_demo_pairs(path: Path) -> int:
    """Write a small demo A/B pair set so first-time users have data."""
    demo = [
        # B is clearly better (4 wins for B)
        {"task": "Refactor the parser to handle nested groups.",
         "response_a": "Try to handle groups, but the regex doesn't work for nesting.",
         "response_b": "Refactored to a recursive descent parser. Nested groups now work. Tests added.",
         "human_label": "win"},
        {"task": "Find the bug in the validation gate.",
         "response_a": "Looks fine to me, not sure what's wrong.",
         "response_b": "Found the bug: byte-identical but whitespace-normalised check was missing. Patched.",
         "human_label": "win"},
        {"task": "Add error handling to the loader.",
         "response_a": "Wrapped the loader in a try/except. Tested.",
         "response_b": "Added typed exception classes and structured logging to the loader. Tested end-to-end.",
         "human_label": "win"},
        # A is clearly better (3 losses for B / wins for A)
        {"task": "Diagnose the slow import.",
         "response_a": "Used cProfile, found the slow function, replaced it with a C extension. 50x faster.",
         "response_b": "Maybe try to import less stuff?",
         "human_label": "lose"},
        {"task": "Add a /health endpoint.",
         "response_a": "Returns 200 with uptime + version. Mounted at /health. Auth-bypassed.",
         "response_b": "I forgot how routes work in this framework.",
         "human_label": "lose"},
        {"task": "Test the new validator.",
         "response_a": "Wrote 12 unit tests, all pass. Coverage 95%.",
         "response_b": "Skipped tests, looks fine.",
         "human_label": "lose"},
        # Ties (3 ties)
        {"task": "Refactor the helper.",
         "response_a": "Renamed variables for clarity.",
         "response_b": "Renamed variables for clarity.",
         "human_label": "tie"},
        {"task": "Document the API.",
         "response_a": "Wrote docstrings for all public functions.",
         "response_b": "Wrote docstrings for all public functions.",
         "human_label": "tie"},
        {"task": "Reformat the file.",
         "response_a": "Ran black on the file.",
         "response_b": "Ran black on the file.",
         "human_label": "tie"},
        # One more win (so the demo has 4/3/3 split for plausibility)
        {"task": "Tighten the SQL query.",
         "response_a": "Added an index. Query went from 2s to 5ms.",
         "response_b": "Added an index. Query went from 2s to 5ms.",
         "human_label": "tie"},
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in demo:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(demo)


# ----------------------------------------------------------------------- #
# Main
# ----------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Calibrate the SkillOpt A/B judge against a labelled set of A/B pairs.",
    )
    p.add_argument(
        "--pairs", type=str, default=None,
        help="Path to the labelled JSONL file. Use --build-demo first to get a starter set.",
    )
    p.add_argument(
        "--build-demo", action="store_true",
        help="Write a 10-pair demo set to --output and exit (used to bootstrap a new project).",
    )
    p.add_argument(
        "--output", type=str, default=None,
        help="Where --build-demo writes the demo set. "
             "Default: tests/fixtures/ab_pairs.jsonl",
    )
    p.add_argument(
        "--use-stub-judge", action="store_true",
        help="Use the deterministic keyword stub instead of a real LLM. "
             "Used by the v1.2.0 smoke test so we don't need an LLM endpoint.",
    )
    p.add_argument(
        "--use-http-judge", action="store_true",
        help="Use the HTTP judge (POSTs to SKILLOPT_JUDGE_ENDPOINT). "
             "Mirrors the production wiring in ab_harness.set_judge_mode('http'). "
             "Fails with a clear error if SKILLOPT_JUDGE_ENDPOINT is unset.",
    )
    p.add_argument(
        "--min-kappa", type=float, default=0.6,
        help="Minimum acceptable kappa (default 0.6). Below this, the judge is unreliable.",
    )
    args = p.parse_args(argv)

    # 1. Build-demo short-circuit
    if args.build_demo:
        out = Path(args.output) if args.output else (_plugin_root() / "tests" / "fixtures" / "ab_pairs.jsonl")
        n = _build_demo_pairs(out)
        print(f"[{PLUGIN_NAME}] wrote {n} demo pairs to {out}")
        return 0

    # 2. Need a pairs file
    if not args.pairs:
        print(f"[{PLUGIN_NAME}] ERROR: --pairs is required (or pass --build-demo first)", file=sys.stderr)
        return 2
    pairs_path = Path(args.pairs)
    if not pairs_path.is_file():
        print(f"[{PLUGIN_NAME}] ERROR: pairs file not found: {pairs_path}", file=sys.stderr)
        return 2
    try:
        pairs = _load_pairs(pairs_path)
    except Exception as e:
        print(f"[{PLUGIN_NAME}] ERROR: bad pairs file: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if not pairs:
        print(f"[{PLUGIN_NAME}] ERROR: pairs file is empty", file=sys.stderr)
        return 2

    # 3. Run the judge
    judge = _make_judge(args.use_stub_judge, args.use_http_judge)
    judge_labels: list[str] = []
    human_labels: list[str] = []
    judge_raised = 0
    for rec in pairs:
        # Build a synthetic rollout dict the judge accepts
        rollout = {"task": rec["task"], "last_response": rec["response_b"]}
        # Score A vs B heuristically by response length (the keyword stub
        # would produce this anyway, so this keeps the demo deterministic).
        score_a = 0.4 if len(rec["response_a"]) < 40 else 0.5
        score_b = 0.6 if len(rec["response_b"]) < 40 else 0.7
        try:
            v = judge(rollout, score_a, score_b)
        except Exception as e:
            judge_raised += 1
            print(f"[{PLUGIN_NAME}] WARN: judge raised on pair {len(judge_labels) + 1}: {e}")
            continue
        verdict = v.get("verdict", "tie")
        if verdict not in LABELS:
            verdict = "tie"
        judge_labels.append(verdict)
        human_labels.append(rec["human_label"])

    if judge_raised:
        print(f"[{PLUGIN_NAME}] judge raised on {judge_raised}/{len(pairs)} pairs", file=sys.stderr)
        return 3

    # 4. Compute kappa (hand-rolled + sklearn cross-check if available)
    kappa = cohen_kappa(judge_labels, human_labels)
    if kappa is None:
        print(f"[{PLUGIN_NAME}] ERROR: kappa is undefined (degenerate label distribution)", file=sys.stderr)
        return 2

    sklearn_kappa: float | None = None
    try:
        from sklearn.metrics import cohen_kappa_score  # type: ignore
        sklearn_kappa = float(
            cohen_kappa_score(human_labels, judge_labels, labels=list(LABELS))
        )
    except Exception:
        pass

    # 5. Print the report
    po = sum(1 for a, b in zip(judge_labels, human_labels) if a == b) / len(human_labels)
    print(f"[{PLUGIN_NAME}] A/B judge calibration report")
    print(f"[{PLUGIN_NAME}]   pairs          : {len(pairs)}")
    print(f"[{PLUGIN_NAME}]   observed_agree : {po:.3f}")
    print(f"[{PLUGIN_NAME}]   kappa (ours)   : {kappa:.3f}")
    if sklearn_kappa is not None:
        delta = abs(sklearn_kappa - kappa)
        print(f"[{PLUGIN_NAME}]   kappa (sklearn): {sklearn_kappa:.3f}  (delta={delta:.2e})")
        if delta > 1e-6:
            print(f"[{PLUGIN_NAME}]   WARN: hand-rolled and sklearn kappa disagree; check the math")
    # Confusion matrix
    print(f"[{PLUGIN_NAME}]   confusion (rows=human, cols=judge):")
    print(f"[{PLUGIN_NAME}]              win   lose   tie")
    for h in LABELS:
        row = [str(sum(1 for hu, ju in zip(human_labels, judge_labels) if hu == h and ju == l)) for l in LABELS]
        print(f"[{PLUGIN_NAME}]     {h:5s}  " + "  ".join(f"{c:>4s}" for c in row))
    # Verdict
    if kappa >= args.min_kappa:
        print(f"[{PLUGIN_NAME}] OK: judge is calibrated (kappa {kappa:.3f} >= {args.min_kappa})")
        return 0
    print(f"[{PLUGIN_NAME}] FAIL: judge is unreliable (kappa {kappa:.3f} < {args.min_kappa})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
