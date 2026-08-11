#!/usr/bin/env python3
"""
SkillOpt LLM-judge rollout labelling pass (v1.8.0, P4).

Walks the harvested rollouts and writes an LLM-judged outcome label
(`judge_label` ∈ {success,partial,failure}) into each rollout JSON in place,
so the DistilBERT reward-model trainer (`scripts/train_reward_model.py`,
P5) has labelled training data. Idempotent: a rollout that already has a
`judge_label` is skipped unless `--force`. Atomic per file (temp +
os.replace) so a crash mid-run never corrupts a rollout.

After labelling, prints an advisory judge-vs-heuristic agreement report:
for rollouts that have BOTH a `judge_label` and a stored heuristic
`outcome`, what fraction agree. This is advisory only — it never blocks and
never exits non-zero on disagreement. `calibrate_judge.cohen_kappa` is NOT
reused here because it hardcodes the win/lose/tie label set; we compute
plain observed-agreement % over the success/partial/failure space instead.

Usage:
  python scripts/label_rollouts.py                 # label all unlabelled
  python scripts/label_rollouts.py --limit 20      # newest 20 only
  python scripts/label_rollouts.py --force         # re-label everything
  python scripts/label_rollouts.py --dry-run       # show what would happen
  python scripts/label_rollouts.py --model gemma4:31b

Exit codes:
  0 - at least one rollout was labelled (or all already labelled / dry-run ok)
  1 - no rollouts found OR every label attempt failed
  2 - bad --rollouts-dir (not a directory)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_NAME = "skillopt"

_HEURISTIC_LABELS = ("success", "partial", "failure")


def _plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_path() -> None:
    root = str(_plugin_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _iter_rollouts(rollouts_dir: Path) -> list[Path]:
    """Rollout JSON files, NEWEST first (by mtime, tiebreak by name)."""
    files = [p for p in rollouts_dir.glob("*.json") if p.is_file()]
    files.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return files


def _agreement_report(labelled: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed-agreement % of judge_label vs the stored heuristic `outcome`.

    Advisory only — never blocks. Only rollouts that have BOTH a judge_label
    and an `outcome` in the heuristic label set are counted.
    """
    compared = 0
    agreed = 0
    for rec in labelled:
        jl = rec.get("judge_label")
        he = rec.get("outcome")
        if jl in _HEURISTIC_LABELS and he in _HEURISTIC_LABELS:
            compared += 1
            if jl == he:
                agreed += 1
    pct = (100.0 * agreed / compared) if compared else None
    return {"compared": compared, "agreed": agreed, "agreement_pct": pct}


def run(args: argparse.Namespace) -> int:
    _ensure_path()
    from helpers import llm_judge  # type: ignore

    rdir = Path(args.rollouts_dir)
    if not rdir.is_dir():
        print(f"[label] bad --rollouts-dir: {rdir} (not a directory)", file=sys.stderr)
        return 2

    files = _iter_rollouts(rdir)
    if not files:
        print(f"[label] no rollouts in {rdir}")
        return 1

    limit = int(args.limit or 0)
    if limit and limit > 0:
        files = files[:limit]

    labelled_recs: list[dict[str, Any]] = []
    n_labelled = n_skipped = n_errors = 0

    for p in files:
        if args.dry_run:
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                print(f"  [dry-run] {p.name}: <unreadable>")
                continue
            has = rec.get("judge_label") in _HEURISTIC_LABELS
            if has and not args.force:
                print(f"  [dry-run] {p.name}: SKIP (already {rec.get('judge_label')})")
                n_skipped += 1
            else:
                print(f"  [dry-run] {p.name}: WOULD LABEL")
            continue

        res = llm_judge.label_rollout_file(p, force=args.force, model=args.model)
        if res.get("labelled"):
            n_labelled += 1
            print(f"  {p.name}: labelled={res.get('label')}")
            try:
                labelled_recs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        elif res.get("skipped"):
            n_skipped += 1
            print(f"  {p.name}: skip (already {res.get('label')})")
            try:
                labelled_recs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
        else:
            n_errors += 1
            print(f"  {p.name}: ERROR {res.get('error')}", file=sys.stderr)

    print(
        f"\n[label] done: labelled={n_labelled} skipped={n_skipped} "
        f"errors={n_errors} (of {len(files)} visited)"
    )

    if labelled_recs:
        rep = _agreement_report(labelled_recs)
        if rep["compared"]:
            print(
                f"[label] judge-vs-heuristic agreement: {rep['agreed']}/{rep['compared']} "
                f"= {rep['agreement_pct']:.1f}% (advisory)"
            )
        else:
            print("[label] judge-vs-heuristic agreement: n/a (no rollouts had both labels)")

    if args.dry_run:
        return 0
    if n_labelled == 0 and n_skipped == 0:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="label_rollouts.py",
        description="Label harvested rollouts with an LLM-judged outcome (v1.8.0).",
    )
    root = _plugin_root()
    ap.add_argument(
        "--rollouts-dir", default=str(root / "logs" / "rollouts"),
        help="rollouts directory (default: <plugin>/logs/rollouts)",
    )
    ap.add_argument("--limit", type=int, default=0, help="label only the newest N (0 = all)")
    ap.add_argument("--force", action="store_true", help="re-label even if judge_label present")
    ap.add_argument("--model", default=None, help="LLM model (default: optimizer model / SKILLOPT_JUDGE_MODEL)")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, write nothing")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())